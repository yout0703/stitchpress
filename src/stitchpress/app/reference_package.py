"""Reference-app complete output profile; separate from the manufacturer's N+6 bundle."""

import copy
import json
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from time import monotonic
from uuid import uuid4

from PIL import Image

from stitchpress.core.cards import symbol_pages
from stitchpress.core.commerce import CommerceRenderer, CommerceSettings
from stitchpress.core.conversion import normalize_image
from stitchpress.core.errors import Cancelled, StitchPressError
from stitchpress.core.identity import canonical_bytes, digest, safe_filename, sha256, sha256_file
from stitchpress.core.importers import import_chart
from stitchpress.core.post import PostSettings, ThreadPlan, save_rgb, write_thread_workbook
from stitchpress.core.reading import reading_layout
from stitchpress.core.render import MAX_RENDER_PIXELS, Renderer, stitch_preview
from stitchpress.core.streaming import MAX_STREAM_PIXELS, save_production_stream
from stitchpress.platform.files import atomic_write, exclusive_lock

from .commerce import commerce_context
from .jobs import verify_delivery
from .post_settings import save_settings


def render_context(project):
    context = commerce_context(project)
    renderer = Renderer(
        context["design"],
        context["library"],
        context["mapping"],
        context["fabric"],
        code=context["code"],
        title=context["title"],
        font_data=context["font_data"],
        text_layers=context["text_layers"],
        font_assets=context["font_assets"],
    )
    return context, renderer


def input_pattern(project):
    record = project.entities.get("pattern_input")
    if (
        not record
        or record.get("format") not in {"xsd", "oxs"}
        or record.get("asset") not in project.assets
    ):
        raise StitchPressError("输出原稿需要原始图纸记录；请从作品页导入全针 XSD 或 OXS")
    raw = project.assets[record["asset"]]
    if sha256(raw) != record.get("sha256"):
        raise StitchPressError("原始图纸字节已改变")
    return record, raw


def post_fingerprint(project):
    from .post_settings import settings_document

    # A fingerprint is a pure content digest. Saving/exporting still validates the
    # full container; UI invalidation must never compress a project on every click.
    record, _ = settings_document(project, "resources", validate=False)
    record.pop("origin_project_id")
    from .commerce_templates import template_font_sha

    if font_sha := template_font_sha(project):
        record["commerce_font_sha"] = font_sha
    return digest(record)


def preflight(project, options=None, *, demo=False):
    options = options or PostSettings(**project.entities.get("post", {}))
    context, renderer = render_context(project)
    design, library = context["design"], context["library"]
    if not any(design.counts):
        raise StitchPressError("母版没有有效针格")
    if not context["code"].strip():
        raise StitchPressError("请先确认货号，保留前导零")
    if "original" in options.targets:
        original, raw = input_pattern(project)
        if import_chart(raw, original["format"]).content_sha != design.content_sha:
            format_name = original["format"].upper()
            raise StitchPressError(
                f"当前针格已改变，原{format_name}不能冒充当前版本；请选择不含原稿的目标或恢复原稿"
            )
    source = project.entities.get("source")
    commerce = CommerceSettings(**project.entities.get("commerce", {}))
    contents = [
        commerce.squares[int(t[-1]) - 1] if t.startswith("square") else t.removeprefix("detail-")
        for t in options.commerce_targets
    ]
    needs_source = "source" in options.targets or (
        "commerce" in options.targets
        and (commerce.art_source == "original" or "compare" in contents)
    )
    if needs_source and not source:
        raise StitchPressError("所选对应图JPG/完整商品图缺少来源图片；请绑定或明确取消该目标")
    if not demo:
        if library.usage != "customer-imported" or not library.icc:
            raise StitchPressError("正式生产需要客户符号库及有效CMYK ICC；示例库仅供草稿检查")
        if needs_source and not source.get("correspondence_confirmed", False):
            raise StitchPressError("请人工确认原图与图纸对应，哈希不能证明是同一作品")
        review = project.entities.get("post_review", {})
        configured = PostSettings(**project.entities.get("post", {}))
        # A reviewed full set may be exported as a subset; geometry/coefficients cannot change.
        comparable = replace(
            options, targets=configured.targets, commerce_targets=configured.commerce_targets
        )
        if (
            comparable != configured
            or not set(options.targets) <= set(configured.targets)
            or not set(options.commerce_targets) <= set(configured.commerce_targets)
        ):
            raise StitchPressError("输出覆盖参数未经过当前核对，请先应用并重新确认")
        if (
            review.get("input_sha") != post_fingerprint(project)
            or review.get("options_sha") != digest(configured.to_dict())
            or not review.get("reviewer", "").strip()
        ):
            raise StitchPressError("请在导出前核对当前原稿/符号/尺寸并登记确认")
    if "threads" in options.targets and (
        not options.thread_plans or any(not ThreadPlan(**p).confirmed for p in options.thread_plans)
    ):
        raise StitchPressError("配线目标需要至少一套已确认的CT/每米针数规则")
    reading_targets = set(options.targets) & {"whole", "symbols", "card", "pages"}
    try:
        reading, plan = reading_layout(renderer, context["reference"])
    except StitchPressError:
        if reading_targets:
            raise
        reading, plan = renderer, None
    if "pages" in options.targets and len(plan.pages) > 200:
        raise StitchPressError("本地查图暂限200页，不能静默删页")
    if reading_targets and plan.paper_px[0] * plan.paper_px[1] > MAX_RENDER_PIXELS:
        raise StitchPressError("查图单页超过当前8000万像素上限")
    pixels = renderer.layout.canvas_px[0] * renderer.layout.canvas_px[1]
    if "production" in options.targets and pixels > MAX_STREAM_PIXELS:
        raise StitchPressError("生产超过当前2.5亿像素保护上限")
    if "whole" in options.targets:
        _, whole = reading_layout(
            renderer, replace(context["reference"], paper="whole", overlap=0, landscape=False)
        )
        if len(whole.pages) != 1 or whole.paper_px[0] * whole.paper_px[1] > MAX_RENDER_PIXELS:
            raise StitchPressError("整张查图超过当前单页保护限值，请使用分页目标")
    from stitchpress.core.layout import physical_layout

    physical = physical_layout(design.width, design.height, context["fabric"])
    targets = list(options.targets)
    return dict(
        targets=targets,
        design_sha=design.content_sha,
        production_px=renderer.layout.canvas_px,
        production_dpi=context["fabric"].dpi,
        reference_dpi=reading.fabric.dpi,
        paper_px=plan.paper_px if plan else None,
        page_count=len(plan.pages) if plan else 0,
        capacity=plan.capacity if plan else None,
        artwork_cm=(physical.width_cm, physical.height_cm),
        cloth_cm=physical.canvas_cm,
        stitches=sum(design.counts),
        colors=len(context["mapping"]),
        ct_plans=[p["ct"] for p in options.thread_plans],
        commerce_files=len(options.commerce_targets) if "commerce" in targets else 0,
        estimate_bytes=max(
            32 * 1024**2,
            6
            * (
                (pixels * 4 if "production" in targets else 0)
                + (
                    plan.paper_px[0]
                    * plan.paper_px[1]
                    * 4
                    * (len(plan.pages) if "pages" in targets else 1)
                    if plan and reading_targets
                    else 0
                )
            ),
        ),
        demo=demo,
        settings_sha=post_fingerprint(project),
    )


def export_reference(
    project,
    destination,
    work,
    *,
    options=None,
    demo=False,
    cancel=lambda: False,
    progress=lambda event: None,
):
    from stitchpress.platform.licensing import require_license

    require_license()
    options = options or PostSettings(**project.entities.get("post", {}))
    summary = preflight(project, options, demo=demo)
    destination, work = Path(destination), Path(work)
    destination.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise StitchPressError("目标已存在，请选择新版本目录")
    if shutil.disk_usage(destination.parent).free < summary["estimate_bytes"]:
        raise StitchPressError("可用磁盘空间不足以生成所选参考目标")
    context, renderer = render_context(project)
    if set(options.targets) & {"whole", "symbols", "card", "pages"}:
        reading, plan = reading_layout(renderer, context["reference"])
    else:
        reading, plan = renderer, None
    identity = uuid4().hex
    started = monotonic()
    journal = work / f"post-{identity}.json"
    state = dict(
        job_id=identity,
        pipeline="reference-post-v1",
        project_id=project.project_id,
        destination=str(destination),
        snapshot=summary,
        input_fingerprint=summary["settings_sha"],
        status="planning",
        artifacts=[],
        demo=demo,
        visual_review="pending",
        factory_print_test="pending",
    )
    staging = Path(tempfile.mkdtemp(prefix=".stitchpress-post-", dir=destination.parent))
    name = ("DRAFT-" if demo else "") + safe_filename(context["code"])

    def stage(value):
        if cancel():
            raise Cancelled("参考出图已取消，未发布不完整集合")
        state["status"] = value
        atomic_write(journal, canonical_bytes(state))
        progress(dict(stage=value, artifacts=len(state["artifacts"])))

    def record(path, checks):
        state["artifacts"].append(
            dict(
                name=path.relative_to(staging).as_posix(),
                bytes=path.stat().st_size,
                sha256=sha256_file(path),
                checks=checks,
            )
        )
        atomic_write(journal, canonical_bytes(state))

    def image_file(image, path, dpi, *, icc=None):
        try:
            return save_rgb(image, path, dpi=dpi, quality=options.jpeg_quality, source_icc=icc)
        finally:
            image.close()

    try:
        stage("planning")
        if "original" in options.targets:
            stage("copying-original")
            original, raw = input_pattern(project)
            path = staging / f"{name}.{original['format']}"
            atomic_write(path, raw)
            record(path, dict(original_sha=sha256(raw), byte_preserved=True))
        if "source" in options.targets:
            stage("rendering-corresponding-image")
            record_source = project.entities["source"]
            raw = project.assets[record_source["asset"]]
            image, normal = normalize_image(raw, context.get("input_icc"))
            white = Image.new("RGBA", image.size, "white")
            white.alpha_composite(image)
            image.close()
            path = staging / f"{name}.jpg"
            checks = image_file(white, path, context["fabric"].dpi)
            checks["normalization"] = normal
            record(path, checks)
        if "production" in options.targets:
            stage("rendering-production")
            path = staging / f"{name}=画布文件.tif"
            checks = save_production_stream(
                renderer,
                path,
                cancel=cancel,
                progress=progress,
                bigtiff=options.tiff_container == "bigtiff",
            )
            record(path, checks)
        if "whole" in options.targets:
            stage("rendering-whole-chart")
            whole = replace(context["reference"], paper="whole", overlap=0, landscape=False)
            full, whole_plan = reading_layout(renderer, whole)
            if len(whole_plan.pages) != 1:
                raise StitchPressError("整张查图布局没有完整容纳，不能偷偷分页")
            image, raster = full.reference(
                whole_plan.pages[0], whole_plan, style=whole.style, minor_grid=whole.minor_grid
            )
            path = staging / f"{name}=图纸文件.tif"
            try:
                checks = full.save_tiff(image, path, raster)
            finally:
                image.close()
            record(path, checks)
        for kind in ("symbols", "card"):
            if kind not in options.targets:
                continue
            stage("rendering-" + kind)
            for number, image in enumerate(symbol_pages(reading, kind), 1):
                stage(f"rendering-{kind}-{number}")
                path = (
                    staging
                    / f"{name}={'符号表' if kind == 'symbols' else '速查卡'}-{number:02}.tif"
                )
                image.save(
                    path,
                    "TIFF",
                    compression="tiff_lzw",
                    dpi=(reading.fabric.dpi,) * 2,
                    icc_profile=renderer.library.icc,
                )
                with Image.open(path) as actual:
                    actual.load()
                    if (
                        actual.mode != "CMYK"
                        or actual.size != image.size
                        or actual.tobytes() != image.tobytes()
                        or actual.info.get("icc_profile") != renderer.library.icc
                    ):
                        raise StitchPressError("符号表/卡TIFF反读失败")
                image.close()
                record(
                    path,
                    dict(kind=kind, mapping_sha=digest(context["mapping"]), dpi=reading.fabric.dpi),
                )
        if "threads" in options.targets:
            for spec in options.thread_plans:
                stage("rendering-threads-" + spec["ct"])
                path = staging / f"{name}={safe_filename(spec['ct'])}CT配线表.xlsx"
                record(
                    path,
                    write_thread_workbook(
                        context["design"],
                        context["library"],
                        context["mapping"],
                        context["fabric"],
                        title=context["title"],
                        code=context["code"],
                        plan=spec,
                        path=path,
                    ),
                )
        if "effect" in options.targets:
            stage("rendering-dot-effect")
            path = staging / f"{name}=点图效果.bmp"
            image = stitch_preview(context["design"], cell=options.effect_cell, style="dot")
            record(path, image_file(image, path, context["fabric"].dpi))
        if "pages" in options.targets:
            folder = staging / "分页图纸"
            folder.mkdir()
            for page in plan.pages:
                stage(f"rendering-page-{page.number}")
                image, raster = reading.reference(
                    page,
                    plan,
                    style=context["reference"].style,
                    minor_grid=context["reference"].minor_grid,
                )
                checks = reading.verify_cells(image, raster)
                path = folder / f"{name}-P{page.number:03}.jpg"
                checks.update(image_file(image, path, reading.fabric.dpi, icc=renderer.library.icc))
                record(path, checks)
        if "commerce" in options.targets:
            settings = CommerceSettings(**project.entities.get("commerce", {}))
            composer = CommerceRenderer(
                commerce_context(project), settings, project.assets, demo=demo
            )
            folder = staging / "电商图片"
            folder.mkdir()
            try:
                for target in options.commerce_targets:
                    stage("rendering-" + target)
                    image, checks = composer.render(target)
                    path = folder / f"{name}-{target}.jpg"
                    checks.update(
                        image_file(
                            image,
                            path,
                            settings.square_dpi
                            if target.startswith("square")
                            else settings.detail_dpi,
                        )
                    )
                    record(path, checks)
            finally:
                composer.subject.close()
        if "settings" in options.targets:
            stage("saving-settings")
            saved = copy.deepcopy(project)
            saved.entities["post"] = options.to_dict()
            path = staging / f"{name}=作品设置.json"
            save_settings(saved, path)
            # Sidecars are explicit resources of this settings file.
            record(path, dict(format="StitchPress.Reference.Settings.v1"))
            for asset in json.loads(path.read_bytes())["assets"].values():
                item = staging / asset["file"]
                if item.relative_to(staging).as_posix() not in {
                    r["name"] for r in state["artifacts"]
                }:
                    record(item, dict(settings_resource=True))
            path.with_suffix(".json.lock").unlink(missing_ok=True)
        stage("verifying")
        state["expected_files"] = len(state["artifacts"])
        verify_delivery(staging, state)
        # Manifest intentionally does not hash itself. Every other published byte is covered.
        report = staging / f"{name}=出图核验.json"
        atomic_write(
            report,
            canonical_bytes(
                {
                    **state,
                    "status": "completed",
                    "self_hash_excluded": True,
                    "selected_targets": options.targets,
                }
            ),
        )
        record(report, dict(manifest=True, self_hash_excluded=True))
        state["expected_files"] = len(state["artifacts"])
        verify_delivery(staging, state)
        stage("publishing")
        with exclusive_lock(destination.parent / f".{destination.name}.publish.lock"):
            if destination.exists():
                raise StitchPressError("目标已由其他任务建立，未覆盖")
            require_license()
            staging.rename(destination)
        state.update(
            status="completed",
            files=len(state["artifacts"]),
            elapsed_seconds=round(monotonic() - started, 3),
        )
        atomic_write(journal, canonical_bytes(state))
        return state
    except BaseException as exc:
        state.update(status="cancelled" if isinstance(exc, Cancelled) else "failed", error=str(exc))
        atomic_write(journal, canonical_bytes(state))
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from time import monotonic
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from stitchpress import __version__
from stitchpress.core.configuration import ReferenceSettings, ShowcaseSettings
from stitchpress.core.documents import write_counts, write_pdf
from stitchpress.core.errors import Cancelled, StitchPressError
from stitchpress.core.identity import canonical_bytes, digest, safe_filename, sha256, sha256_file
from stitchpress.core.layout import Fabric, PagePlan, decimal, px
from stitchpress.core.models import Design, ReviewRecord, ThreadColor, validate_catalog_membership
from stitchpress.core.render import MAX_RENDER_PIXELS, Renderer, stitch_preview
from stitchpress.core.resources import SymbolLibrary, bundled_font
from stitchpress.core.streaming import MAX_STREAM_PIXELS, save_production_stream
from stitchpress.platform.files import atomic_write, exclusive_lock

from .artifact_cache import ArtifactCache

PIPELINE_REVISION = "local-export-v5"
FULL_TARGETS = frozenset({"production", "reference", "pdf", "counts", "showcase", "notes"})


def export_plan(
    design,
    library,
    mapping,
    fabric,
    reference=None,
    *,
    code="",
    font_data=None,
    title="",
    text_layers=None,
    font_assets=None,
    targets=FULL_TARGETS,
):
    reference = reference or ReferenceSettings()
    renderer = Renderer(
        design,
        library,
        mapping,
        fabric,
        code=code,
        font_data=font_data,
        title=title,
        text_layers=text_layers,
        font_assets=font_assets,
    )
    paper_cm = reference.paper_cm
    needs_pages = bool(set(targets) & {"reference", "pdf", "notes"})
    if needs_pages:
        from stitchpress.core.reading import reading_layout

        renderer.reading, plan = reading_layout(renderer, reference)
    else:
        plan = PagePlan(
            paper_cm,
            tuple(px(decimal(v), reference.dpi or fabric.dpi) for v in paper_cm),
            (0, 0),
            0,
            0,
            0,
            0,
            (0, 0),
            (),
        )
    if len(plan.pages) > 100:
        raise StitchPressError("试验版暂限100张查图页，请减少当前任务规模")
    # Conservative working-space estimate, including decoded rasters, PDF, staging and cache.
    production = renderer.layout.canvas_px[0] * renderer.layout.canvas_px[1]
    page_pixels = plan.paper_px[0] * plan.paper_px[1]
    if ("production" in targets and production > MAX_STREAM_PIXELS) or (
        needs_pages and page_pixels > MAX_RENDER_PIXELS
    ):
        raise StitchPressError("生产超过2.5亿或单页超过6000万像素当前上限；不能静默缩小")
    estimated_bytes = max(
        16 * 1024**2,
        6
        * ((production * 4 if "production" in targets else 0) + page_pixels * 4 * len(plan.pages)),
    )
    return (
        renderer,
        plan,
        {
            "production_px": renderer.layout.canvas_px,
            "production_streaming": production > MAX_RENDER_PIXELS,
            "reference_dpi": reference.dpi or fabric.dpi,
            "paper_px": plan.paper_px,
            "page_count": len(plan.pages),
            "files": len(plan.pages) + 6,
            "capacity": plan.capacity,
            "estimated_workspace_bytes": estimated_bytes,
            "factory_reference_compatible": reference.factory_compatible,
            "plan": plan.to_dict(),
        },
    )


def export_snapshot(context, *, targets=FULL_TARGETS, demo=False):
    """One source of truth for export receipts and workflow freshness checks."""
    design, library = context["design"], context["library"]
    mapping, fabric = context["mapping"], context["fabric"]
    code, title = context["code"], context["title"]
    reference = context.get("reference") or ReferenceSettings()
    showcase_options = context.get("showcase_options") or ShowcaseSettings()
    text_layers, material_rules = context.get("text_layers"), context.get("material_rules", ())
    review_context, font_data = context.get("review_context"), context.get("font_data")
    catalog = context.get("catalog", ())
    return {
        "design_sha": design.content_sha,
        "library": library.revision,
        "icc_sha": sha256(library.icc) if library.icc else None,
        "mapping": mapping,
        "fabric": asdict(fabric),
        "code": code,
        "title": title,
        "engine": __version__,
        "pipeline": PIPELINE_REVISION,
        "reference": reference.to_dict(),
        "showcase": showcase_options.to_dict(),
        "targets": sorted(targets),
        "text_layers": text_layers or {},
        "material_rules": material_rules,
        "review_context": review_context,
        "font_sha": sha256(font_data or bundled_font().read_bytes()),
        "font_assets": {k: sha256(v) for k, v in context.get("font_assets", {}).items()},
        "symbol_pixels": {s.key: s.pixel_sha for s in library.symbols},
        "catalog_membership": [c.key for c in catalog],
        "demo": demo,
    }


def export_package(
    *,
    design: Design,
    library: SymbolLibrary,
    mapping: dict[str, str],
    fabric: Fabric,
    code: str,
    title: str,
    destination: Path,
    work: Path,
    review: ReviewRecord | None,
    review_context: dict | None = None,
    catalog: tuple[ThreadColor, ...] = (),
    demo: bool = False,
    reference: ReferenceSettings | None = None,
    showcase_options: ShowcaseSettings | None = None,
    targets: frozenset[str] | None = None,
    font_data: bytes | None = None,
    font_assets: dict | None = None,
    text_layers: dict | None = None,
    material_rules: tuple | list = (),
    cache_root: Path | None = None,
    request_id: str | None = None,
    cancel: Callable[[], bool] = lambda: False,
    progress: Callable[[dict], None] = lambda event: None,
) -> dict:
    reference = reference or ReferenceSettings()
    showcase_options = showcase_options or ShowcaseSettings()
    targets = FULL_TARGETS if targets is None else frozenset(targets)
    if not targets or not targets <= FULL_TARGETS:
        raise StitchPressError("请选择已支持的导出目标，不能发布空交付")
    if not demo:
        if review is None:
            raise StitchPressError("母版尚未完成具名审核冻结，不能生产")
        review.validate(design)
        review.validate_context(review_context)
        validate_catalog_membership(design, catalog)
        if not library.icc or library.usage != "customer-imported":
            raise StitchPressError("正式印花需要导入客户符号和CMYK ICC，演示库不可生产")
        if fabric.dpi != 300:
            raise StitchPressError("当前厂家兼容模板固定300DPI；其他DPI尚未验收")
    if not any(design.counts):
        raise StitchPressError("母版没有有效针格")
    renderer, plan, estimate = export_plan(
        design,
        library,
        mapping,
        fabric,
        reference,
        code=code,
        font_data=font_data,
        title=title,
        text_layers=text_layers,
        font_assets=font_assets,
        targets=targets,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise StitchPressError("目标版本已存在，请选择新的版本目录，不能覆盖旧交付")
    if shutil.disk_usage(destination.parent).free < estimate["estimated_workspace_bytes"]:
        raise StitchPressError("可用空间不足以容纳保守估算的工作文件、缓存与交付")
    started = monotonic()
    snapshot = export_snapshot(
        dict(
            design=design,
            library=library,
            mapping=mapping,
            fabric=fabric,
            code=code,
            title=title,
            reference=reference,
            showcase_options=showcase_options,
            text_layers=text_layers,
            material_rules=material_rules,
            review_context=review_context,
            font_data=font_data,
            font_assets=font_assets or {},
            catalog=catalog,
        ),
        targets=targets,
        demo=demo,
    )
    job_id = uuid4().hex
    expected_files = (
        (len(plan.pages) if "reference" in targets else 0)
        + sum(k in targets for k in ("production", "pdf", "counts", "showcase"))
        + (2 if "notes" in targets else 0)
    )
    journal = work / f"job-{job_id}.json"
    state = {
        "job_id": job_id,
        "request_id": request_id,
        "destination": str(destination),
        "expected_files": expected_files,
        "input_fingerprint": digest(snapshot),
        "snapshot": snapshot,
        "status": "validating",
        "visual_review": "pending",
        "factory_print_test": "pending",
        "artifacts": [],
        "reused": 0,
        "estimate": estimate,
    }

    def stage(name):
        if cancel():
            raise Cancelled("任务已取消；上次完整交付和母版保持可用")
        state["status"] = name
        atomic_write(journal, canonical_bytes(state))
        progress({"stage": name, "artifacts": len(state["artifacts"]), "reused": state["reused"]})

    def record(path, base, verification):
        state["artifacts"].append(
            {
                "name": path.relative_to(base).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "checks": verification,
                "reused": False,
            }
        )

    cache = ArtifactCache(cache_root) if cache_root else None

    def artifact(path, base, dependencies, generate, *, published=True):
        fingerprint = digest({"pipeline": PIPELINE_REVISION, "dependencies": dependencies})
        checks = cache.restore(fingerprint, path) if cache else None
        reused = checks is not None
        if not reused:
            checks = generate()
            if cache:
                cache.put(fingerprint, path, checks)
        else:
            state["reused"] += 1
        if published:
            record(path, base, checks)
            state["artifacts"][-1].update(input_fingerprint=fingerprint, reused=reused)

    render_dependencies = {
        k: v
        for k, v in snapshot.items()
        if k
        not in {
            "reference",
            "showcase",
            "targets",
            "title",
            "text_layers",
            "material_rules",
            "review_context",
            "catalog_membership",
            "demo",
        }
    }
    staging = Path(tempfile.mkdtemp(prefix=".stitchpress-", dir=destination.parent))
    try:
        stage("planning")
        name = safe_filename(code or title)
        pages_folder = "A4_TIFF_Pages" if reference.paper == "A4" else "Reference_TIFF_Pages"
        for folder in ("Production", pages_folder, "Showcase"):
            (staging / folder).mkdir()
        if "production" in targets:
            stage("rendering-production")
            path = staging / "Production" / f"{name}-Production-v01.tif"

            def production():
                if estimate["production_streaming"]:
                    return save_production_stream(renderer, path, cancel=cancel, progress=progress)
                image, raster = renderer.production()
                try:
                    return renderer.save_tiff(image, path, raster)
                finally:
                    image.close()

            artifact(
                path,
                staging,
                {
                    "kind": "production",
                    **render_dependencies,
                    "text": {
                        k: v for k, v in (text_layers or {}).items() if k.startswith("production")
                    },
                    "title": title
                    if any(
                        "{title}" in v.get("template", "")
                        for k, v in (text_layers or {}).items()
                        if k.startswith("production")
                    )
                    else "",
                },
                production,
            )
        page_paths = []
        page_directory = (
            staging / pages_folder if "reference" in targets else work / "reference-pages" / job_id
        )
        page_directory.mkdir(parents=True, exist_ok=True)
        for page in plan.pages if targets & {"reference", "pdf"} else ():
            stage(f"rendering-page-{page.number}")
            path = page_directory / f"{name}-{reference.paper}-P{page.number:02}-v01.tif"

            def reference_page(page=page, path=path):
                image, raster = renderer.reference(
                    page, plan, style=reference.style, minor_grid=reference.minor_grid
                )
                try:
                    return renderer.save_tiff(image, path, raster)
                finally:
                    image.close()

            artifact(
                path,
                staging,
                {
                    "kind": "page",
                    **render_dependencies,
                    "reference": reference.to_dict(),
                    "page": asdict(page),
                    "text": {
                        k: v for k, v in (text_layers or {}).items() if k.startswith("reference")
                    },
                    "title": title
                    if any(
                        "{title}" in v.get("template", "")
                        for k, v in (text_layers or {}).items()
                        if k.startswith("reference")
                    )
                    else "",
                },
                reference_page,
                published="reference" in targets,
            )
            page_paths.append(path)
        stage("rendering-documents")
        if "pdf" in targets:
            path = staging / f"{name}-Reference-v01.pdf"
            artifact(
                path,
                staging,
                {
                    "kind": "pdf",
                    "pages": [sha256_file(p) for p in page_paths],
                    "dpi": reference.dpi or fabric.dpi,
                    "paper": plan.paper_cm,
                    "icc": snapshot["icc_sha"],
                },
                lambda: write_pdf(
                    page_paths, path, reference.dpi or fabric.dpi, plan.paper_cm, library.icc
                ),
            )
        if "counts" in targets:
            path = staging / f"{name}-Stitch-Counts-v01.xlsx"
            artifact(
                path,
                staging,
                {
                    "kind": "counts",
                    "design": design.content_sha,
                    "code": code,
                    "material_rules": material_rules,
                },
                lambda: write_counts(design, code, path, material_rules),
            )
        if "showcase" in targets:
            path = staging / "Showcase" / f"{name}-Showcase-v01.png"

            def write_showcase():
                image = stitch_preview(
                    design, cell=showcase_options.cell, style=showcase_options.style
                )
                try:
                    image.save(path)
                    if image.width * 4 != image.height * 3:
                        raise StitchPressError("模拟图必须严格3:4")
                    return {
                        "size": image.size,
                        "source": design.content_sha,
                        "one_x_per_occupied_cell": showcase_options.style == "x",
                        "style": showcase_options.style,
                    }
                finally:
                    image.close()

            artifact(
                path,
                staging,
                {"kind": "showcase", "design": design.content_sha, **showcase_options.to_dict()},
                write_showcase,
            )
        facts = {
            "code": code,
            "title": title,
            "grid": [design.width, design.height],
            "colors": len(mapping),
            "stitches": sum(design.counts),
            "pitch_cm": [fabric.sx, fabric.sy],
            "nominal_ct": fabric.nominal_ct,
            "design_cm": [renderer.layout.width_cm, renderer.layout.height_cm],
            "fabric_cm": renderer.layout.canvas_cm,
            "canvas_px": renderer.layout.canvas_px,
            "margins_cm": [fabric.left, fabric.top, fabric.right, fabric.bottom],
            "page_count": len(plan.pages),
            "page_windows": [p.core for p in plan.pages],
            "scale100_cm": renderer.layout.scale100_cm,
            "dpi": fabric.dpi,
            "icc_sha": snapshot["icc_sha"],
            "master_sha": design.content_sha,
            "review_context": review_context,
            "origin": design.origin,
            "demo": demo,
            "reference": reference.to_dict(),
            "showcase": showcase_options.to_dict(),
            "targets": sorted(targets),
            "material_rules": material_rules,
            "text_layers": text_layers or {},
            "package_type": "selected-outputs"
            if targets != FULL_TARGETS
            else "factory-compatible-N+6"
            if reference.factory_compatible and showcase_options.style == "x"
            else "custom-reference-N+6",
        }
        for language in ("EN", "CN") if "notes" in targets else ():
            path = staging / f"READ_ME_{language}.txt"
            caption = (
                "演示/待试印；非量产验收。实际大小/100%打印，关闭适合页面。\n"
                "模拟图由母版确定性生成，非实绣照片。未配置客户材料规则时仅给针数；"
                "如有材料估算，口径与来源见material_rules和表格，不代表厂家验证。\n"
                "图片转换/重建并不等于恢复原供应商针格。\n"
                if language == "CN"
                else "Demo / factory test pending. Print Actual Size / 100%; disable Fit/Shrink.\n"
                "Showcase is a deterministic chart simulation, not a stitched photograph.\n"
                "Counts are full crosses; blank cells excluded. Optional thread estimates use "
                "only the configured customer rules listed in material_rules and the workbook.\n"
                "Image reconstruction does not recover an original supplier's stitch grid.\n"
            )
            path.write_text(
                caption + json.dumps(facts, ensure_ascii=False, indent=2) + "\n", "utf-8"
            )
            record(path, staging, {"shared_facts": digest(facts)})
        stage("verifying")
        expected = expected_files
        actual = [p for p in staging.rglob("*") if p.is_file()]
        if len(actual) != expected:
            raise StitchPressError("所选产物清单不完整")
        for item in state["artifacts"]:
            if sha256_file(staging / item["name"]) != item["sha256"]:
                raise StitchPressError("发布前产物被改写")
        # Factory ZIP contains only the N+6 files; receipts stay in the work area.
        stage("publishing")
        with exclusive_lock(destination.parent / f".{destination.name}.publish.lock"):
            if destination.exists():
                raise StitchPressError("目标目录已由其他任务建立，未覆盖")
            os.rename(staging, destination)
        state.update(
            status="completed",
            files=expected,
            page_plan=plan.to_dict(),
            elapsed_seconds=round(monotonic() - started, 3),
            destination=str(destination),
        )
        atomic_write(journal, canonical_bytes(state))
        return state
    except BaseException as exc:
        state.update(
            status="cancelled" if isinstance(exc, Cancelled) else "failed",
            error=str(exc),
            elapsed_seconds=round(monotonic() - started, 3),
        )
        atomic_write(journal, canonical_bytes(state))
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        temporary_pages = work / "reference-pages" / job_id
        if temporary_pages.exists():
            shutil.rmtree(temporary_pages)


def zip_package(directory: Path, target: Path, receipt: dict | None = None) -> dict:
    if receipt:
        from .jobs import verify_delivery

        verify_delivery(directory, receipt)
    if target.exists():
        raise StitchPressError("ZIP已存在，请另存版本")
    if (
        directory.resolve() == target.parent.resolve()
        or directory.resolve() in target.resolve().parents
    ):
        raise StitchPressError("ZIP须保存在交付目录之外，不能把自己打进包内")
    if any(p.is_symlink() for p in directory.rglob("*")):
        raise StitchPressError("交付不能包含链接")
    files = sorted(p for p in directory.rglob("*") if p.is_file())
    if not files:
        raise StitchPressError("不能压缩空交付")
    expected = {p.relative_to(directory).as_posix(): sha256_file(p) for p in files}
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".stitchpress-zip-", suffix=".tmp", dir=target.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        with ZipFile(temporary, "w", ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, path.relative_to(directory).as_posix())
        with ZipFile(temporary) as archive:
            if archive.testzip() is not None or len(archive.infolist()) != len(files):
                raise StitchPressError("ZIP CRC/数量核验失败")
            for member in archive.infolist():
                content = hashlib.sha256()
                with archive.open(member) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        content.update(chunk)
                if content.hexdigest() != expected[member.filename]:
                    raise StitchPressError("压缩期间交付内容改变，未发布ZIP")
        with exclusive_lock(target.with_suffix(target.suffix + ".lock")):
            if target.exists():
                raise StitchPressError("ZIP已由另一任务建立，未覆盖")
            os.rename(temporary, target)
        return {"files": len(files), "sha256": sha256_file(target), "member_sha256": expected}
    finally:
        temporary.unlink(missing_ok=True)

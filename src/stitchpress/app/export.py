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
from stitchpress.core.documents import write_counts, write_pdf
from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import canonical_bytes, digest, safe_filename, sha256
from stitchpress.core.layout import Fabric, page_plan
from stitchpress.core.models import Design, ReviewRecord, ThreadColor, validate_catalog_membership
from stitchpress.core.render import Renderer, stitch_preview
from stitchpress.core.resources import SymbolLibrary, bundled_font
from stitchpress.platform.files import atomic_write


class Cancelled(StitchPressError):
    pass


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
    catalog: tuple[ThreadColor, ...] = (),
    demo: bool = False,
    cancel: Callable[[], bool] = lambda: False,
    progress: Callable[[dict], None] = lambda event: None,
) -> dict:
    if not demo:
        if review is None:
            raise StitchPressError("母版尚未完成具名审核冻结，不能生产")
        review.validate(design)
        validate_catalog_membership(design, catalog)
        if not library.icc or library.usage != "customer-imported":
            raise StitchPressError("正式印花需要导入客户符号和CMYK ICC，演示库不可生产")
        if fabric.dpi != 300:
            raise StitchPressError("当前厂家兼容模板固定300DPI；其他DPI尚未验收")
    if not any(design.counts):
        raise StitchPressError("母版没有有效针格")
    destination.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise StitchPressError("目标版本已存在，请选择新的版本目录，不能覆盖旧交付")
    started = monotonic()
    snapshot = {
        "design_sha": design.content_sha,
        "library": library.revision,
        "icc_sha": sha256(library.icc) if library.icc else None,
        "mapping": mapping,
        "fabric": asdict(fabric),
        "code": code,
        "title": title,
        "engine": __version__,
        "font_sha": sha256(bundled_font().read_bytes()),
        "symbol_pixels": {s.key: s.pixel_sha for s in library.symbols},
        "catalog_membership": [c.key for c in catalog],
        "demo": demo,
    }
    job_id = uuid4().hex
    journal = work / f"job-{job_id}.json"
    state = {
        "job_id": job_id,
        "input_fingerprint": digest(snapshot),
        "snapshot": snapshot,
        "status": "validating",
        "visual_review": "pending",
        "factory_print_test": "pending",
        "artifacts": [],
    }

    def stage(name):
        if cancel():
            raise Cancelled("任务已取消；上次完整交付和母版保持可用")
        state["status"] = name
        atomic_write(journal, canonical_bytes(state))
        progress({"stage": name, "artifacts": len(state["artifacts"])})

    def record(path, base, verification):
        state["artifacts"].append(
            {
                "name": path.relative_to(base).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256(path.read_bytes()),
                "checks": verification,
            }
        )

    staging = Path(tempfile.mkdtemp(prefix=".stitchpress-", dir=destination.parent))
    try:
        stage("planning")
        renderer = Renderer(design, library, mapping, fabric, code=code)
        plan = page_plan(
            design.width,
            design.height,
            len(mapping),
            fabric,
            legend_entry_width=renderer.legend_entry_width(),
        )
        if len(plan.pages) > 100:
            raise StitchPressError("试验版暂限100张查图页，请减少当前任务规模")
        name = safe_filename(code or title)
        for folder in ("Production", "A4_TIFF_Pages", "Showcase"):
            (staging / folder).mkdir()
        stage("rendering-production")
        image, raster = renderer.production()
        path = staging / "Production" / f"{name}-Production-v01.tif"
        checks = renderer.save_tiff(image, path, raster)
        record(path, staging, checks)
        image.close()
        page_paths = []
        for page in plan.pages:
            stage(f"rendering-page-{page.number}")
            image, raster = renderer.reference(page, plan)
            path = staging / "A4_TIFF_Pages" / f"{name}-A4-P{page.number:02}-v01.tif"
            checks = renderer.save_tiff(image, path, raster)
            record(path, staging, checks)
            page_paths.append(path)
            image.close()
        stage("rendering-documents")
        path = staging / f"{name}-Reference-v01.pdf"
        record(path, staging, write_pdf(page_paths, path, fabric.dpi, plan.paper_cm, library.icc))
        path = staging / f"{name}-Stitch-Counts-v01.xlsx"
        record(path, staging, write_counts(design, code, path))
        path = staging / "Showcase" / f"{name}-Showcase-v01.png"
        image = stitch_preview(design)
        image.save(path)
        if image.width * 4 != image.height * 3:
            raise StitchPressError("模拟图必须严格3:4")
        record(
            path,
            staging,
            {"size": image.size, "source": design.content_sha, "one_x_per_occupied_cell": True},
        )
        image.close()
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
            "origin": design.origin,
            "demo": demo,
        }
        for language in ("EN", "CN"):
            path = staging / f"READ_ME_{language}.txt"
            caption = (
                "演示/待试印；非量产验收。实际大小/100%打印，关闭适合页面。\n"
                "X图为母版确定性模拟，非实绣照片。配线仅为准确针数，不估算米数。\n"
                "图片转换/重建并不等于恢复原供应商针格。\n"
                if language == "CN"
                else "Demo / factory test pending. Print Actual Size / 100%; disable Fit/Shrink.\n"
                "Showcase is a deterministic chart simulation, not a stitched photograph.\n"
                "Counts are full crosses; blank cells excluded. No thread length estimate.\n"
                "Image reconstruction does not recover an original supplier's stitch grid.\n"
            )
            path.write_text(
                caption + json.dumps(facts, ensure_ascii=False, indent=2) + "\n", "utf-8"
            )
            record(path, staging, {"shared_facts": digest(facts)})
        stage("verifying")
        expected = len(plan.pages) + 6
        actual = [p for p in staging.rglob("*") if p.is_file()]
        if len(actual) != expected:
            raise StitchPressError("N+6产物清单不完整")
        for item in state["artifacts"]:
            if sha256((staging / item["name"]).read_bytes()) != item["sha256"]:
                raise StitchPressError("发布前产物被改写")
        # Factory ZIP contains only the N+6 files; receipts stay in the work area.
        stage("publishing")
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


def zip_package(directory: Path, target: Path) -> dict:
    if target.exists():
        raise StitchPressError("ZIP已存在，请另存版本")
    files = sorted(p for p in directory.rglob("*") if p.is_file())
    temporary = target.with_suffix(".zip.tmp")
    try:
        with ZipFile(temporary, "w", ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, path.relative_to(directory).as_posix())
        with ZipFile(temporary) as archive:
            if archive.testzip() is not None or len(archive.infolist()) != len(files):
                raise StitchPressError("ZIP CRC/数量核验失败")
        os.rename(temporary, target)
        return {"files": len(files), "sha256": sha256(target.read_bytes())}
    finally:
        temporary.unlink(missing_ok=True)

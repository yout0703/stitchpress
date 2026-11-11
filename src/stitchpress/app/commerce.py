"""Isolated merchandising output with per-target cache and atomic publication."""

import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageCms

from stitchpress.core.commerce import TARGETS, CommerceRenderer, CommerceSettings
from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import canonical_bytes, digest, safe_filename, sha256, sha256_file
from stitchpress.platform.files import atomic_write, exclusive_lock

from .artifact_cache import ArtifactCache
from .context import export_context
from .export import Cancelled


def commerce_context(project):
    context = export_context(project)
    source = project.entities.get("source")
    profile = project.entities.get("input_profile")
    context["source_data"] = project.assets[source["asset"]] if source else None
    context["input_icc"] = project.assets[profile["asset"]] if profile else None
    return context


def export_commerce(
    project,
    destination,
    work,
    *,
    targets=TARGETS,
    demo=False,
    cache_root=None,
    cancel=lambda: False,
    progress=lambda event: None,
):
    if not targets or len(set(targets)) != len(targets) or not set(targets) <= set(TARGETS):
        raise StitchPressError("商品导出目标为空、重复或无效")
    settings = CommerceSettings(**project.entities.get("commerce", {}))
    context = commerce_context(project)
    if not demo:
        if not context["review"]:
            raise StitchPressError("正式商品素材须绑定已人工审核母版；未审核可使用明确草稿模式")
        context["review"].validate(context["design"])
    destination = Path(destination)
    work = Path(work)
    if destination.exists():
        raise StitchPressError("商品目标已存在，请另存新版本")
    destination.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent).free < 160 * 1024**2:
        raise StitchPressError("商品导出保守工作空间不足160MiB")
    renderer = CommerceRenderer(context, settings, project.assets, demo=demo)
    cache = ArtifactCache(cache_root) if cache_root else None
    identity = uuid4().hex
    journal = work / f"commerce-{identity}.json"
    receipt = {
        "job_id": identity,
        "pipeline": "commerce-v1",
        "project_id": project.project_id,
        "input_fingerprint": digest(
            {
                "design": context["design"].content_sha,
                "settings": settings.to_dict(),
                "metadata": project.entities["metadata"],
            }
        ),
        "destination": str(destination),
        "expected_files": len(targets),
        "artifacts": [],
        "visual_review": "pending",
        "factory_print_test": "not-applicable",
        "demo": demo,
        "reused": 0,
    }
    staging = Path(tempfile.mkdtemp(prefix=".stitchpress-commerce-", dir=destination.parent))
    try:
        for target in targets:
            if cancel():
                raise Cancelled("商品任务已取消，未发布不完整集合")
            receipt["status"] = "rendering-" + target
            atomic_write(journal, canonical_bytes(receipt))
            progress({"stage": receipt["status"]})
            name = f"{safe_filename(context['code'] or 'StitchPress')}-{target}.png"
            path = staging / name
            content = (
                settings.squares[int(target[-1]) - 1]
                if target.startswith("square")
                else target.removeprefix("detail-")
            )
            config = {
                k: v
                for k, v in settings.to_dict().items()
                if k not in {"scenes", "squares", "closeup_x", "closeup_y", "closeup_zoom", "care"}
            }
            deps = {
                "pipeline": "commerce-v1",
                "target": target,
                "content": content,
                "design_sha": context["design"].content_sha,
                "settings": config,
                "metadata": project.entities["metadata"],
                "font": sha256(context["font_data"])
                if context["font_data"]
                else "bundled-noto-sans",
                "demo": demo,
                "template_facts": renderer.fields,
            }
            if content.startswith("scene-"):
                scene = settings.scenes[int(content[-1]) - 1]
                deps["scene"] = scene
                asset = scene.get("background_asset")
                deps["background_sha"] = sha256(project.assets[asset]) if asset else None
            if content == "compare":
                deps.update(
                    source=sha256(context["source_data"]) if context["source_data"] else None,
                    input_icc=sha256(context["input_icc"]) if context["input_icc"] else None,
                )
            if content in {"closeup", "print"}:
                deps["closeup"] = [settings.closeup_x, settings.closeup_y, settings.closeup_zoom]
            if content == "print":
                deps.update(library=context["library"].revision, mapping=context["mapping"])
            if content == "spec":
                deps["fabric"] = project.entities["fabric"]
            if content == "care":
                deps["care"] = settings.care
            deps["showcase"] = context["showcase_options"].to_dict()
            key = digest(deps)
            checks = cache.restore(key, path) if cache else None
            reused = checks is not None
            if not reused:
                image, checks = renderer.render(target)
                icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
                image.save(
                    path,
                    icc_profile=icc,
                    dpi=(settings.square_dpi,) * 2 if target.startswith("square") else (150, 150),
                )
                with Image.open(path) as actual:
                    actual.load()
                    if (
                        actual.mode != "RGB"
                        or actual.size != image.size
                        or actual.tobytes() != image.tobytes()
                        or actual.info.get("icc_profile") != icc
                    ):
                        raise StitchPressError("商品PNG实际读回不一致")
                image.close()
                if cache:
                    cache.put(key, path, checks)
            receipt["reused"] += int(reused)
            receipt["artifacts"].append(
                {
                    "name": name,
                    "sha256": sha256_file(path),
                    "bytes": path.stat().st_size,
                    "checks": checks,
                    "input_fingerprint": key,
                    "reused": reused,
                }
            )
            atomic_write(journal, canonical_bytes(receipt))
        if cancel():
            raise Cancelled("商品任务已取消，未发布")
        from .jobs import verify_delivery

        verify_delivery(staging, receipt)
        with exclusive_lock(destination.parent / f".{destination.name}.publish.lock"):
            if destination.exists():
                raise StitchPressError("目标由其他任务建立，未覆盖")
            receipt["status"] = "publishing"
            atomic_write(journal, canonical_bytes(receipt))
            staging.rename(destination)
        receipt.update(status="completed", files=len(targets))
        atomic_write(journal, canonical_bytes(receipt))
        return receipt
    except Exception as exc:
        receipt.update(
            status="cancelled" if isinstance(exc, Cancelled) else "failed", error=str(exc)
        )
        atomic_write(journal, canonical_bytes(receipt))
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        renderer.subject.close()

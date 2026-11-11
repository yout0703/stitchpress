"""Allowlisted process protocol. Workers write only their task result, never project files."""

import json
from pathlib import Path

from stitchpress.core.identity import canonical_bytes
from stitchpress.platform.files import atomic_write


def run(job_path: Path) -> int:
    job = json.loads(job_path.read_bytes())
    output = Path(job["output"])
    try:
        if job["operation"] == "symbol-card":
            from .store import Project
            from .symbols import export_symbol_card

            result = export_symbol_card(
                Project.decode(Path(job["project"]).read_bytes()), Path(job["target"])
            )
        elif job["operation"] == "zip":
            from .export import zip_package

            result = zip_package(Path(job["directory"]), Path(job["target"]), job["receipt"])
        elif job["operation"] in {"plan", "preview", "quality", "showcase-preview"}:
            from stitchpress.core.quality import inspect_design
            from stitchpress.core.render import screen_rgb, stitch_preview

            from .context import export_context
            from .export import export_plan
            from .store import Project

            project = Project.decode(Path(job["project"]).read_bytes())
            context = export_context(project)
            design = context["design"]
            if job["operation"] == "showcase-preview":
                options = context["showcase_options"]
                image = stitch_preview(design, cell=options.cell, style=options.style)
                preview = output.parent / "showcase-preview.png"
                image.save(preview)
                result = {
                    "preview": str(preview),
                    "size": image.size,
                    "style": options.style,
                    "design_sha": design.content_sha,
                }
                image.close()
            elif job["operation"] == "quality":
                result = inspect_design(design)
            else:
                renderer, plan, result = export_plan(
                    design,
                    context["library"],
                    context["mapping"],
                    context["fabric"],
                    context["reference"],
                    code=context["code"],
                    font_data=context["font_data"],
                )
                if job["operation"] == "preview":
                    number = job.get("page_number", 1)
                    if type(number) is not int or not 1 <= number <= len(plan.pages):
                        raise ValueError("查图页码越界")
                    image, raster = renderer.reference(
                        plan.pages[number - 1],
                        plan,
                        style=context["reference"].style,
                        minor_grid=context["reference"].minor_grid,
                    )
                    checks = renderer.verify_cells(image, raster)
                    preview = output.parent / f"page-{number:03}.png"
                    rgb = screen_rgb(image, context["library"].icc)
                    rgb.save(preview)
                    image.close()
                    rgb.close()
                    result.update(preview=str(preview), page_number=number, checks=checks)
        elif job["operation"] == "convert":
            from stitchpress.core.conversion import ConversionOptions, convert_image
            from stitchpress.core.identity import sha256
            from stitchpress.core.masks import annotation_masks
            from stitchpress.core.resources import load_catalog

            source = Path(job["image"]).read_bytes()
            options = ConversionOptions(**job["options"])
            masks = annotation_masks(
                job.get("annotations"), options.width, options.height, sha256(source)
            )
            design, record = convert_image(
                source,
                load_catalog(Path(job["catalog"]).read_bytes(), job.get("catalog_brand", "DMC")),
                options,
                blank_mask=masks["blank"],
                subject_mask=masks["subject"],
                override_icc=Path(job["input_icc"]).read_bytes() if job.get("input_icc") else None,
            )
            result = {"design": design.to_dict(), "conversion": record}
        elif job["operation"] == "analyze":
            from stitchpress.platform.settings import load_secret

            from .ai import ServiceProfile, analyze

            profile = ServiceProfile(**job["profile"])
            result = analyze(
                profile,
                load_secret(profile.credential_key) or "",
                job["prompt"],
                Path(job["image"]).read_bytes() if job.get("image") else None,
            )
        else:
            raise ValueError("未知工作进程操作")
        atomic_write(
            output,
            canonical_bytes(
                {"status": "completed", "result": result, "base_sha": job.get("base_sha")}
            ),
        )
        print(json.dumps({"stage": "completed"}), flush=True)
        return 0
    except Exception as exc:
        from .ai import RemoteUnknown

        atomic_write(
            output,
            canonical_bytes(
                {
                    "status": "remote_unknown" if isinstance(exc, RemoteUnknown) else "failed",
                    "error": str(exc),
                    "base_sha": job.get("base_sha"),
                }
            ),
        )
        print(json.dumps({"stage": "failed", "error": str(exc)}, ensure_ascii=False), flush=True)
        return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(run(Path(sys.argv[1])))

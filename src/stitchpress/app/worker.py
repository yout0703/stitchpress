"""Allowlisted process protocol. Workers write only their task result, never project files."""

import json
from pathlib import Path

from stitchpress.core.identity import canonical_bytes
from stitchpress.platform.files import atomic_write


def run(job_path: Path) -> int:
    job = json.loads(job_path.read_bytes())
    output = Path(job["output"])
    try:
        if job["operation"] in {"post-plan", "post-preview", "reference-export"}:
            from stitchpress.core.post import PostSettings

            from .reference_package import export_reference, preflight
            from .reference_preview import preview_reference
            from .store import Project

            project = Project.decode(Path(job["project"]).read_bytes())
            if job["operation"] == "post-plan":
                result = preflight(project, demo=True)
            elif job["operation"] == "post-preview":
                result = preview_reference(
                    project,
                    job["target"],
                    output.parent / "post-preview.png",
                    job.get("page_number", 1),
                )
            else:
                result = export_reference(
                    project,
                    job["target"],
                    output.parent,
                    options=PostSettings(**job["options"]),
                    demo=job.get("demo", False),
                    cancel=lambda: Path(job["cancel_file"]).exists(),
                    progress=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True),
                )
        elif job["operation"] in {"blueprint-import", "blueprint-composite"}:
            from stitchpress.core.blueprints import prepare_blueprint

            from .store import Project

            path = Path(job["file"])
            if path.stat().st_size > 64 * 1024**2:
                raise ValueError("蓝图文件超过64MiB上限")
            raw = path.read_bytes()
            project = Project.decode(Path(job["project"]).read_bytes())
            source = project.entities.get("source")
            from stitchpress.core.identity import sha256

            parent_sha = sha256(project.assets[source["asset"]]) if source else None
            composite = None
            if job["operation"] == "blueprint-composite":
                from stitchpress.core.conversion import compose_local_image
                from stitchpress.core.models import Design

                from .revisions import protected_cells

                base = Design.from_dict(project.entities["design"])
                x0, y0, x1, y1 = job["region"]
                if any(x0 <= x < x1 and y0 <= y < y1 for x, y in protected_cells(project, base)):
                    raise ValueError("选区触及保护区/负空间，请先明确修改标注")
                profile = project.entities.get("input_profile")
                merged, composite = compose_local_image(
                    project.assets[source["asset"]],
                    raw,
                    tuple(job["region"]),
                    (base.width, base.height),
                    original_icc=project.assets[profile["asset"]] if profile else None,
                )
                edited = output.parent / "edited-original.image"
                atomic_write(edited, raw)
                raw = merged
            normalized, record = prepare_blueprint(
                raw,
                origin="local-composite" if composite else job["origin"],
                note=job.get("note", ""),
                provider=job.get("provider", ""),
                model=job.get("model", ""),
                parent_sha=parent_sha,
            )
            original = output.parent / "blueprint-original.image"
            converted = output.parent / "blueprint-normalized.png"
            atomic_write(original, raw)
            atomic_write(converted, normalized)
            result = {
                "record": record,
                "original_path": str(original),
                "normalized_path": str(converted),
                "label": path.name,
                **({"composite": composite, "edited_path": str(edited)} if composite else {}),
            }
        elif job["operation"] in {"compare-candidate", "migrate-candidate", "convert-region"}:
            from stitchpress.core.models import Design
            from stitchpress.core.resources import library_from_assets, load_catalog

            from .revisions import compare_candidate, protected_cells
            from .store import Project

            project = Project.decode(Path(job["project"]).read_bytes())
            if job["operation"] == "compare-candidate":
                result = compare_candidate(project, job["candidate_key"])
            else:
                library = library_from_assets(project.entities["library"], project.assets)
                if job["operation"] == "migrate-candidate":
                    from stitchpress.core.proposals import apply_migration

                    parent = project.entities[job["candidate_key"]]
                    design = Design.from_dict(parent["design"])
                    result_design, patch = apply_migration(
                        design,
                        job["proposal"],
                        [tuple(xy) for xy in job["selected"]],
                        capacity=len(library.symbols),
                        protected=protected_cells(project, design),
                    )
                    result = {
                        "design": result_design.to_dict(),
                        "patch": patch,
                        "conversion": parent["conversion"],
                        "parent_candidate": job["candidate_key"],
                    }
                else:
                    from stitchpress.core.conversion import ConversionOptions, convert_region
                    from stitchpress.core.identity import sha256
                    from stitchpress.core.masks import annotation_masks

                    catalog = project.entities.get("catalog")
                    if not catalog:
                        raise ValueError("局部转换需要已导入目录")
                    options = ConversionOptions(**job["options"])
                    base = Design.from_dict(project.entities["design"])
                    source = project.entities["source"]
                    profile = project.entities.get("input_profile")
                    design, patch = convert_region(
                        project.assets[source["asset"]],
                        load_catalog(project.assets[catalog["asset"]], catalog.get("brand", "DMC")),
                        options,
                        base,
                        tuple(job["region"]),
                        capacity=len(library.symbols),
                        override_icc=project.assets[profile["asset"]] if profile else None,
                        protected=protected_cells(project, base),
                        subject_mask=annotation_masks(
                            project.entities.get("annotations"),
                            base.width,
                            base.height,
                            sha256(project.assets[source["asset"]]),
                        )["subject"],
                    )
                    result = {"design": design.to_dict(), "patch": patch, "conversion": patch}
        elif job["operation"] == "symbol-folder":
            import copy

            from .reference_actions import import_symbol_folder
            from .store import Project

            project = Project.decode(Path(job["project"]).read_bytes())
            imported = import_symbol_folder(project, job["folder"])
            candidate = copy.deepcopy(project)
            candidate.entities["library"] = imported["record"]
            candidate.entities["mapping"] = imported["mapping"]
            candidate.entities.pop("manual_mapping", None)
            candidate.entities.pop("freeze", None)
            candidate.assets.update(imported["assets"])
            path = output.parent / "symbol-candidate.stitchpress"
            atomic_write(path, candidate.encode())
            result = dict(project=str(path), receipts=imported["receipts"])
        elif job["operation"] == "commerce-gallery":
            from stitchpress.core.commerce import CommerceSettings

            from .commerce import commerce_context
            from .commerce_gallery import gallery_images
            from .store import Project

            project = Project.decode(Path(job["project"]).read_bytes())
            result = gallery_images(
                commerce_context(project),
                CommerceSettings(**project.entities.get("commerce", {})),
                project.assets,
                output.parent / "gallery",
            )
        elif job["operation"] in {"commerce-export", "commerce-preview"}:
            from stitchpress.core.commerce import CommerceRenderer, CommerceSettings

            from .commerce import commerce_context, export_commerce
            from .store import Project

            project = Project.decode(Path(job["project"]).read_bytes())
            if job["operation"] == "commerce-export":
                result = export_commerce(
                    project,
                    Path(job["target"]),
                    output.parent,
                    targets=job["targets"],
                    demo=job.get("demo", False),
                    cache_root=output.parent.parent.parent / "cache" / "commerce",
                    cancel=lambda: Path(job["cancel_file"]).exists(),
                )
            else:
                renderer = CommerceRenderer(
                    commerce_context(project),
                    CommerceSettings(**project.entities.get("commerce", {})),
                    project.assets,
                    demo=job.get("demo", True),
                )
                image, checks = renderer.render(job["target"])
                preview = output.parent / "commerce-preview.png"
                image.save(preview)
                image.close()
                renderer.subject.close()
                result = {"preview": str(preview), "checks": checks}
        elif job["operation"] == "symbol-card":
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
                    title=context["title"],
                    text_layers=context["text_layers"],
                    font_assets=context["font_assets"],
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
                input_icc=Path(job["input_icc"]).read_bytes() if job.get("input_icc") else None,
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
        from stitchpress.core.errors import Cancelled

        from .ai import RemoteUnknown

        atomic_write(
            output,
            canonical_bytes(
                {
                    "status": "remote_unknown"
                    if isinstance(exc, RemoteUnknown)
                    else "cancelled"
                    if isinstance(exc, Cancelled)
                    else "failed",
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

import argparse
import json
import platform
from pathlib import Path

from . import __version__
from .core.errors import StitchPressError


def main(argv=None):
    parser = argparse.ArgumentParser(prog="stitchpress", description="绣印工坊本地开发版")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("gui")
    demo = sub.add_parser("demo")
    demo.add_argument("output", type=Path)
    demo.add_argument("--width", type=int, default=48)
    demo.add_argument("--height", type=int, default=64)
    demo.add_argument("--colors", type=int, default=8)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("project", type=Path)
    export = sub.add_parser("export")
    export.add_argument("project", type=Path)
    export.add_argument("destination", type=Path)
    export.add_argument("--demo", action="store_true", help="演示模式；不具备正式生产资格")
    export.add_argument("--cancel-file", type=Path)
    export.add_argument("--work", type=Path, default=Path(".local/jobs"))
    resources = sub.add_parser("import-factory")
    for arg in ("profile", "manifest", "catalog", "output"):
        resources.add_argument(arg, type=Path)
    args = parser.parse_args(argv)
    try:
        return execute(args)
    except (StitchPressError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)
        return 2


def execute(args):
    from .app.fixtures import save_demo
    from .app.store import Project
    from .core.models import Design, ReviewRecord

    if args.command == "gui":
        from .app.desktop import main as desktop

        return desktop()
    if args.command == "doctor":
        import sys

        from PIL import features
        from PySide6.QtCore import qVersion

        from .platform.launch import is_compiled, worker_program

        data = {
            "app": __version__,
            "python": platform.python_version(),
            "os": platform.system(),
            "arch": platform.machine(),
            "qt": qVersion(),
            "libtiff": features.check("libtiff"),
            "littlecms": features.check("littlecms2"),
            "compiled": is_compiled(),
            "runtime_executable": sys.executable,
            "worker_program": worker_program(),
        }
    elif args.command == "demo":
        if args.output.exists():
            raise StitchPressError("目标作品已存在，请另存路径")
        save_demo(args.output, args.width, args.height, args.colors)
        data = {"saved": str(args.output), "demo_only": True}
    elif args.command == "import-factory":
        from .app.resource_import import import_factory

        data = import_factory(args.profile, args.manifest, args.catalog, args.output)
    elif args.command == "inspect":
        project = Project.decode(args.project.read_bytes())
        data = {
            "project_id": project.project_id,
            "commit_seq": project.commit_seq,
            "entities": list(project.entities),
            "assets": len(project.assets),
        }
        if "design" in project.entities:
            design = Design.from_dict(project.entities["design"])
            data.update(
                width=design.width,
                height=design.height,
                counts=design.counts,
                stitches=sum(design.counts),
                master_sha=design.content_sha,
            )
    elif args.command == "export":
        from .app.export import export_package
        from .core.layout import Fabric
        from .core.resources import assign_symbols, library_from_assets

        project = Project.decode(args.project.read_bytes())
        design = Design.from_dict(project.entities["design"])
        library = library_from_assets(project.entities["library"], project.assets)
        mapping = project.entities.get("mapping") or assign_symbols(design, library)
        review_data = project.entities.get("freeze")
        if review_data:
            review_data["detail_regions"] = tuple(tuple(r) for r in review_data["detail_regions"])
        review = ReviewRecord(**review_data) if review_data else None
        result = export_package(
            design=design,
            library=library,
            mapping=mapping,
            fabric=Fabric(**project.entities["fabric"]),
            code=project.entities["metadata"]["code"],
            title=project.entities["metadata"]["title"],
            destination=args.destination,
            work=args.work,
            review=review,
            demo=args.demo,
            cancel=lambda: bool(args.cancel_file and args.cancel_file.exists()),
            progress=lambda event: print(json.dumps(event), flush=True),
        )
        data = {k: result[k] for k in ("status", "files", "elapsed_seconds", "visual_review")}
    else:
        return 2
    print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

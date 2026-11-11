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
    migrate = sub.add_parser("migrate", help="旧作品升级为独立副本，保留原文件")
    migrate.add_argument("project", type=Path)
    migrate.add_argument("output", type=Path)
    for command in ("check", "plan"):
        local = sub.add_parser(command)
        local.add_argument("project", type=Path)
    exchange = sub.add_parser("oxs")
    exchange.add_argument("project", type=Path)
    exchange.add_argument("output", type=Path)
    batch = sub.add_parser("batch-export")
    batch.add_argument("manifest", type=Path)
    batch.add_argument("--work", type=Path, required=True)
    batch.add_argument("--cache", type=Path)
    batch.add_argument("--resume", action="store_true")
    batch.add_argument("--cancel-file", type=Path)
    archive = sub.add_parser("zip")
    archive.add_argument("directory", type=Path)
    archive.add_argument("receipt", type=Path)
    archive.add_argument("output", type=Path)
    commerce = sub.add_parser("commerce")
    commerce.add_argument("project", type=Path)
    commerce.add_argument("destination", type=Path)
    commerce.add_argument("--work", type=Path, required=True)
    commerce.add_argument("--cache", type=Path)
    commerce.add_argument("--demo", action="store_true")
    commerce.add_argument("--targets", help="逗号分隔detail-scene-1等目标；默认7+5全套")
    commerce.add_argument("--cancel-file", type=Path)
    export = sub.add_parser("export")
    export.add_argument("project", type=Path)
    export.add_argument("destination", type=Path)
    export.add_argument("--demo", action="store_true", help="演示模式；不具备正式生产资格")
    export.add_argument("--cancel-file", type=Path)
    export.add_argument("--work", type=Path, default=Path(".local/jobs"))
    export.add_argument("--cache", type=Path, help="可复用核验产物目录；损坏缓存重新生成")
    export.add_argument(
        "--targets", help="补件目标：production,reference,pdf,counts,showcase,notes"
    )
    post = sub.add_parser("reference-export", help="一期参考全套输出，独立于N+6")
    post.add_argument("project", type=Path)
    post.add_argument("destination", type=Path)
    post.add_argument("--work", type=Path, required=True)
    post.add_argument("--demo", action="store_true")
    post.add_argument("--cancel-file", type=Path)
    post.add_argument("--targets", help="逗号分隔参考目标；不传则采用作品设置")
    resources = sub.add_parser("import-factory")
    for arg in ("profile", "manifest", "catalog", "output"):
        resources.add_argument(arg, type=Path)
    args = parser.parse_args(argv)
    try:
        return execute(args)
    except (StitchPressError, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)
        return 2


def execute(args):
    from .app.fixtures import save_demo
    from .app.store import Project
    from .core.models import Design

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
    elif args.command == "reference-export":
        from dataclasses import replace

        from .app.reference_package import export_reference
        from .core.post import PostSettings

        project = Project.decode(args.project.read_bytes())
        options = PostSettings(**project.entities.get("post", {}))
        if args.targets:
            options = replace(options, targets=tuple(args.targets.split(",")))
        data = export_reference(
            project,
            args.destination,
            args.work,
            options=options,
            demo=args.demo,
            cancel=lambda: bool(args.cancel_file and args.cancel_file.exists()),
            progress=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True),
        )
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
            "schema_version": project.source_schema,
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
    elif args.command == "migrate":
        from .app.store import migrate_project

        data = migrate_project(args.project, args.output)
    elif args.command == "check":
        from .core.quality import inspect_design

        project = Project.decode(args.project.read_bytes())
        data = inspect_design(Design.from_dict(project.entities["design"]))
    elif args.command == "commerce":
        from .app.commerce import export_commerce
        from .core.commerce import TARGETS

        project = Project.decode(args.project.read_bytes())
        data = export_commerce(
            project,
            args.destination,
            args.work,
            targets=tuple(args.targets.split(",")) if args.targets else TARGETS,
            demo=args.demo,
            cache_root=args.cache,
            cancel=lambda: bool(args.cancel_file and args.cancel_file.exists()),
            progress=lambda event: print(json.dumps(event), flush=True),
        )
    elif args.command == "plan":
        from .app.context import export_context
        from .app.export import export_plan

        context = export_context(Project.decode(args.project.read_bytes()))
        _, _, data = export_plan(
            context["design"],
            context["library"],
            context["mapping"],
            context["fabric"],
            context["reference"],
            code=context["code"],
            font_data=context["font_data"],
            title=context["title"],
            text_layers=context["text_layers"],
        )
    elif args.command == "oxs":
        from .core.importers import export_oxs, import_oxs
        from .platform.files import atomic_write

        if args.output.exists():
            raise StitchPressError("目标OXS已存在，请另存版本")
        project = Project.decode(args.project.read_bytes())
        design = Design.from_dict(project.entities["design"])
        payload = export_oxs(design)
        actual = import_oxs(payload)
        if actual.labels != design.labels or [(c.brand, c.code, c.rgb) for c in actual.palette] != [
            (c.brand, c.code, c.rgb) for c in design.palette
        ]:
            raise StitchPressError("OXS反读不符")
        atomic_write(args.output, payload)
        data = {
            "saved": str(args.output),
            "master_sha": design.content_sha,
            "full_cross_only": True,
            "external_interoperability": "not_verified",
        }
    elif args.command == "batch-export":
        from .app.jobs import run_batch

        state = run_batch(
            args.manifest,
            args.work,
            cache=args.cache,
            resume=args.resume,
            cancel=lambda: bool(args.cancel_file and args.cancel_file.exists()),
            progress=lambda event: print(json.dumps(event), flush=True),
        )
        data = {
            "status": state["status"],
            "completed": sum(j["status"] == "completed" for j in state["jobs"]),
            "total": len(state["jobs"]),
            "work": str(args.work),
        }
    elif args.command == "zip":
        from .app.export import zip_package

        data = zip_package(
            args.directory, args.output, receipt=json.loads(args.receipt.read_bytes())
        )
    elif args.command == "export":
        from .app.context import export_context
        from .app.export import export_package

        project = Project.decode(args.project.read_bytes())
        result = export_package(
            **export_context(project),
            destination=args.destination,
            work=args.work,
            demo=args.demo,
            cache_root=args.cache,
            targets=frozenset(args.targets.split(",")) if args.targets else None,
            cancel=lambda: bool(args.cancel_file and args.cancel_file.exists()),
            progress=lambda event: print(json.dumps(event), flush=True),
        )
        data = {k: result[k] for k in ("status", "files", "elapsed_seconds", "visual_review")}
    else:
        return 2
    print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)
    return 2 if data.get("status") in {"partial", "cancelled", "failed"} else 0


if __name__ == "__main__":
    raise SystemExit(main())

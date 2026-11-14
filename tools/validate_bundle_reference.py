"""Native bundle R1 validation on a self-authored XSD; private library is optional."""

import argparse
import io
import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path

from PIL import Image

from stitchpress.app.fixtures import new_project
from stitchpress.app.jobs import verify_delivery
from stitchpress.app.post_settings import load_settings
from stitchpress.app.reference_actions import import_pattern
from stitchpress.app.reference_package import post_fingerprint
from stitchpress.app.store import Project
from stitchpress.core.commerce import FRAMES, SCENES, TARGETS, CommerceSettings
from stitchpress.core.configuration import ReferenceSettings
from stitchpress.core.identity import canonical_bytes, digest, sha256_file
from stitchpress.core.layout import Fabric
from stitchpress.core.models import Design
from stitchpress.core.post import POST_TARGETS, PostSettings, ThreadPlan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--factory-project", type=Path, help="Local-only customer library; never distribute"
    )
    args = parser.parse_args()
    binary, root = args.binary.resolve(), args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    initial = new_project()
    if args.factory_project:
        resources = Project.decode(args.factory_project.read_bytes())
        initial.assets.update(resources.assets)
        for key in ("library", "catalog", "source_profile"):
            if key in resources.entities:
                initial.entities[key] = resources.entities[key]
    project = import_pattern(
        initial, Path(__file__).parents[1] / "fixtures" / "s01-independent.xsd"
    )
    # A clean Windows machine must render Chinese without any installed CJK font.
    project.entities["metadata"] = dict(title="中文花园 · Independent fixture", code="客户0026")
    project.entities["fabric"] = asdict(Fabric.from_ct("9"))
    design = Design.from_dict(project.entities["design"])
    source = Image.new("RGB", (80, 60), "white")
    from PIL import ImageDraw

    draw = ImageDraw.Draw(source)
    for y, row in enumerate(design.labels):
        for x, index in enumerate(row):
            if index >= 0:
                draw.rectangle(
                    (x * 20, y * 20, x * 20 + 19, y * 20 + 19), fill=design.palette[index].rgb
                )
    data = io.BytesIO()
    source.save(data, "PNG")
    source.close()
    project.assets["reference-original.png"] = data.getvalue()
    project.entities["source"] = dict(
        asset="reference-original.png", correspondence_confirmed=True, filename="AK0026.png"
    )
    project.entities["reference"] = ReferenceSettings(
        dpi=150, cell_mm="2.5", margins_mm=("12.7",) * 4
    ).to_dict()
    project.entities["commerce"] = CommerceSettings(
        art_source="dot", detail_width=1440, detail_height=1920, detail_dpi=200
    ).to_dict()
    options = PostSettings(
        targets=POST_TARGETS,
        thread_plans=tuple(
            asdict(
                ThreadPlan(
                    str(ct),
                    "180",
                    "Self-authored numerical fixture, not customer coefficient",
                    True,
                )
            )
            for ct in (9, 14)
        ),
    )
    project.entities["post"] = options.to_dict()
    project.entities["post_review"] = dict(
        input_sha=post_fingerprint(project),
        options_sha=digest(options.to_dict()),
        reviewer="Local digital QA only",
    )
    input_path = root / "独立全针样例.stitchpress"
    input_path.write_bytes(project.encode())
    original_sha = sha256_file(input_path)
    env = {
        k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }

    def worker(name, operation, *, expected_status="completed", **fields):
        folder = root / name
        folder.mkdir()
        output = folder / "result.json"
        job = dict(operation=operation, project=str(input_path), output=str(output), **fields)
        path = folder / "job.json"
        path.write_bytes(canonical_bytes(job))
        proc = subprocess.run(
            [str(binary), "--internal-worker", str(path)],
            cwd=folder,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        (folder / "process.log").write_text(proc.stdout + "\n" + proc.stderr)
        response = json.loads(output.read_bytes()) if output.exists() else {}
        if expected_status != "completed":
            assert proc.returncode == 2 and response.get("status") == expected_status, response
            return response
        if proc.returncode or response.get("status") != "completed":
            raise RuntimeError(f"Native {operation} failed: {response}; inspect {folder}")
        return response["result"]

    plan = worker("plan", "post-plan")
    assert plan["stitches"] == 6 and plan["reference_dpi"] == 150 and plan["production_dpi"] == 300
    assert plan["artwork_cm"] == ["1.128888", "0.846666"]  # 4×3 cells at 9 CT.
    previews = []
    for target in ("production", "whole", "pages", "symbols", "card", "effect", *TARGETS):
        result = worker("preview-" + target, "post-preview", target=target, page_number=1)
        with Image.open(result["preview"]) as image:
            assert image.format == "PNG" and image.mode == "RGB"
        previews.append(result["checks"])
    gallery = worker("gallery", "commerce-gallery")
    expected_gallery = [
        (kind, index, name)
        for kind, names in (("scene", SCENES), ("frame", FRAMES))
        for index, name in enumerate(names)
    ]
    assert [
        (item["kind"], item["index"], item["name"]) for item in gallery["items"]
    ] == expected_gallery
    for item in gallery["items"]:
        with Image.open(item["file"]) as image:
            assert image.format == "PNG" and image.size == (480, 160)
    destination = root / "参考全套-中文路径"
    receipt = worker(
        "export",
        "reference-export",
        target=str(destination),
        options=options.to_dict(),
        demo=not bool(args.factory_project),
        cancel_file=str(root / "cancel.flag"),
    )
    cancellation = root / "cancel.flag"
    cancellation.write_bytes(b"cancel")
    cancelled = worker(
        "cancelled",
        "reference-export",
        expected_status="cancelled",
        target=str(root / "not-published"),
        options=options.to_dict(),
        demo=not bool(args.factory_project),
        cancel_file=str(cancellation),
    )
    assert not (root / "not-published").exists()
    cancellation.unlink()
    failed = worker("failed", "not-a-supported-operation", expected_status="failed")
    assert "未知工作进程操作" in failed["error"]
    if args.factory_project:
        changed = {**options.to_dict(), "jpeg_quality": 96}
        rejected = worker(
            "changed-review",
            "reference-export",
            expected_status="failed",
            target=str(root / "not-reviewed"),
            options=changed,
            demo=False,
            cancel_file=str(cancellation),
        )
        assert "核对" in rejected["error"] and not (root / "not-reviewed").exists()
    checks = verify_delivery(destination, receipt)
    settings = next(destination.glob("*作品设置.json"))
    restored = load_settings(settings)
    assert Design.from_dict(restored.entities["design"]).content_sha == design.content_sha
    assert restored.entities["mapping"] == project.entities["mapping"]
    assert restored.entities["fabric"] == project.entities["fabric"]
    assert sha256_file(input_path) == original_sha
    summary = dict(
        binary_sha=sha256_file(binary),
        input_sha=original_sha,
        receipt=receipt,
        verification=checks,
        previews=previews,
        gallery_items=len(gallery["items"]),
        cancellation_status=cancelled["status"],
        failure_status=failed["status"],
        review_override_rejected=bool(args.factory_project),
        source_project_unchanged=True,
        builtin_color_library=not bool(args.factory_project),
        native_os=os.name,
        visual="pending",
        windows="pending",
        factory="pending",
    )
    (root / "verification.json").write_bytes(canonical_bytes(summary))
    print(
        json.dumps(
            dict(
                files=receipt["files"],
                previews=len(previews),
                gallery_items=len(gallery["items"]),
                output=str(root),
                binary_sha=summary["binary_sha"],
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

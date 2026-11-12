"""Exercise all commerce templates through the actual packaged worker, offline."""

import argparse
import io
import json
import os
import subprocess
from pathlib import Path

from PIL import Image

from stitchpress.app.commerce_templates import apply_template
from stitchpress.app.fixtures import demo_project
from stitchpress.app.jobs import verify_delivery
from stitchpress.core.commerce import TARGETS, CommerceSettings
from stitchpress.core.commerce_templates import templates
from stitchpress.core.identity import canonical_bytes, sha256_file
from stitchpress.core.post import PostSettings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    binary, root = args.binary.resolve(), args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    project = demo_project(16, 24, 3)
    project.entities["metadata"] = dict(title="白色花园与蓝色蝴蝶", code="000123")
    source = io.BytesIO()
    Image.new("RGB", (80, 120), "#f5f3ed").save(source, "PNG")
    project.assets["source.png"] = source.getvalue()
    project.entities["source"] = dict(asset="source.png", correspondence_confirmed=True)
    env = {
        k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    results = []
    for template in templates():
        project.entities["commerce"] = apply_template(
            CommerceSettings(), template, keep_scenes=False
        ).to_dict()
        path = root / (template["id"] + ".stitchpress")
        path.write_bytes(project.encode())
        before = sha256_file(path)
        for label, targets in [
            ("all", TARGETS),
            ("detail", TARGETS[:7]),
            ("square", TARGETS[7:]),
            ("one", TARGETS[:1]),
        ]:
            folder = root / (template["id"] + "-" + label)
            folder.mkdir()
            output = folder / "result.json"
            destination = folder / "中文商品成品"
            options = PostSettings(targets=("commerce",), commerce_targets=targets)
            job = dict(
                operation="reference-export",
                project=str(path),
                output=str(output),
                target=str(destination),
                options=options.to_dict(),
                demo=True,
                cancel_file=str(folder / "cancel"),
            )
            jobpath = folder / "job.json"
            jobpath.write_bytes(canonical_bytes(job))
            proc = subprocess.run(
                [str(binary), "--internal-worker", str(jobpath)],
                cwd=folder,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=180,
            )
            (folder / "process.log").write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
            response = json.loads(output.read_bytes()) if output.exists() else {}
            assert proc.returncode == 0 and response.get("status") == "completed", response
            receipt = response["result"]
            verify_delivery(destination, receipt)
            images = list(destination.rglob("*.jpg"))
            assert len(images) == len(targets)
            for image_path in images:
                with Image.open(image_path) as img:
                    assert img.mode == "RGB" and img.info.get("icc_profile")
                    assert img.size == (
                        (800, 800) if "square-" in image_path.name else (1200, 1600)
                    )
                    assert img.info["dpi"] == (150, 150)
            results.append(dict(template=template["id"], group=label, jpegs=len(images)))
        assert sha256_file(path) == before
    summary = dict(
        binary_sha=sha256_file(binary),
        groups=results,
        jpegs=sum(v["jpegs"] for v in results),
        visual_review="separate",
        source_unchanged=True,
    )
    (root / "verification.json").write_bytes(canonical_bytes(summary))
    print(json.dumps(dict(groups=len(results), jpegs=summary["jpegs"], status="passed")))


if __name__ == "__main__":
    main()

"""Exercise native streaming, custom text/materials and 7+5 without Python env inheritance."""

import argparse
import io
import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path

import numpy as np
from PIL import Image

from stitchpress.app.fixtures import demo_project
from stitchpress.core.identity import sha256_file
from stitchpress.core.materials import MaterialRule
from stitchpress.core.text import TextLayer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    binary = args.binary.resolve()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    env = {
        k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }

    def run(name, arguments):
        process = subprocess.run(
            [str(binary), "--internal-cli", *arguments],
            capture_output=True,
            text=True,
            cwd=root,
            env=env,
            timeout=180,
        )
        (root / f"{name}.log").write_text(process.stdout + "\n" + process.stderr)
        if process.returncode:
            raise RuntimeError(f"{name} failed; see log in {root}")

    large = root / "large.stitchpress"
    run("large-demo", ["demo", str(large), "--width", "400", "--height", "420", "--colors", "30"])
    run(
        "stream-production",
        [
            "export",
            str(large),
            str(root / "stream-package"),
            "--targets",
            "production",
            "--demo",
            "--work",
            str(root / "stream-work"),
        ],
    )
    stream = json.loads(next((root / "stream-work").glob("job-*.json")).read_bytes())
    if stream["files"] != 1 or not stream["artifacts"][0]["checks"]["pixel_sha_verified"]:
        raise RuntimeError("Native streaming verification absent")
    project = demo_project(48, 64, 8)
    labels = project.entities["design"]["labels"]
    palette = project.entities["design"]["palette"]
    pixels = np.asarray(
        [[palette[i]["rgb"] if i >= 0 else (255, 255, 255) for i in row] for row in labels],
        dtype=np.uint8,
    )
    image = Image.fromarray(pixels)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    image.close()
    project.assets["sources/blueprint.png"] = buffer.getvalue()
    project.entities["source"] = {
        "asset": "sources/blueprint.png",
        "role": "self-authored-test-blueprint",
    }
    project.entities["text_layers"] = {
        "reference_top": asdict(
            TextLayer(template="{code} | {width} x {height} | Page {page}/{pages}")
        )
    }
    project.entities["material_rules"] = {
        "rules": [
            asdict(
                MaterialRule(
                    name="Synthetic native test; not factory rule",
                    nominal_ct="14",
                    strands=2,
                    stitches_per_work_metre="100",
                    loss_percent="10",
                    pack_work_metres="0.5",
                    source_note="Self-authored unit fixture only",
                    confirmed=True,
                )
            )
        ]
    }
    small = root / "portable.stitchpress"
    small.write_bytes(project.encode())
    run(
        "custom-documents",
        [
            "export",
            str(small),
            str(root / "documents"),
            "--targets",
            "reference,pdf,counts",
            "--demo",
            "--work",
            str(root / "document-work"),
        ],
    )
    run(
        "commerce",
        [
            "commerce",
            str(small),
            str(root / "commerce"),
            "--demo",
            "--work",
            str(root / "commerce-work"),
        ],
    )
    commerce = json.loads(next((root / "commerce-work").glob("commerce-*.json")).read_bytes())
    if commerce["files"] != 12:
        raise RuntimeError("Native commerce set incomplete")
    for item in commerce["artifacts"]:
        with Image.open(root / "commerce" / item["name"]) as actual:
            expected = (800, 800) if "square-" in item["name"] else (1200, 1600)
            if actual.size != expected:
                raise RuntimeError("Native commerce dimensions differ")
    report = {
        "native_binary_sha256": sha256_file(binary),
        "stream": {
            "pixels": stream["estimate"]["production_px"],
            "checks": stream["artifacts"][0]["checks"],
            "files": stream["files"],
        },
        "custom_reference_pdf_and_materials": "passed-native-readback",
        "commerce_files": commerce["files"],
        "windows": "pending",
        "factory": "pending",
        "paid_ai_calls": 0,
    }
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()

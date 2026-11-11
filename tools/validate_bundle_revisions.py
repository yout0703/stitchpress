"""Run actual packaged revision/blueprint workers on self-authored, exactly known cells."""

import argparse
import io
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

from PIL import Image

from stitchpress.app.fixtures import demo_project
from stitchpress.core.identity import canonical_bytes, sha256_file
from stitchpress.core.models import Design, PatchCell, apply_patch, new_design
from stitchpress.core.resources import load_catalog


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    binary, root = args.binary.resolve(), args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    env = {
        k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    catalog_data = canonical_bytes(
        [
            dict(code="001", name="black test", rgb=[0, 0, 0]),
            dict(code="002", name="white test", rgb=[255, 255, 255]),
        ]
    )
    colors = load_catalog(catalog_data)
    base = new_design(4, 4, colors, [[0] * 4 for _ in range(4)], origin="converted")
    project = demo_project(4, 4, 2)
    project.entities["design"] = base.to_dict()
    project.assets["catalog.json"] = catalog_data
    project.entities["catalog"] = dict(asset="catalog.json", brand="DMC")
    data = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(data, "PNG")
    project.assets["source.png"] = data.getvalue()
    project.entities["source"] = dict(asset="source.png")
    source = root / "编辑图.png"
    source.write_bytes(data.getvalue())

    def save(name, value):
        path = root / (name + ".stitchpress")
        path.write_bytes(value.encode())
        return path

    input_path = save("局部输入", project)
    original_sha = sha256_file(input_path)

    def worker(name, operation, project_path=input_path, **fields):
        work = root / name
        work.mkdir()
        output = work / "result.json"
        job = dict(operation=operation, project=str(project_path), output=str(output), **fields)
        task = work / "job.json"
        task.write_bytes(canonical_bytes(job))
        result = subprocess.run(
            [str(binary), "--internal-worker", str(task)],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        (work / "process.log").write_text(result.stdout + "\n" + result.stderr)
        record = json.loads(output.read_bytes()) if output.exists() else {}
        if result.returncode or record.get("status") != "completed":
            raise RuntimeError(f"Packaged {operation} failed; inspect {work}")
        return record["result"]

    regional = worker(
        "region",
        "convert-region",
        options=dict(width=4, height=4, max_colors=2),
        region=[1, 1, 3, 3],
    )
    grid = Design.from_dict(regional["design"])
    for y, row in enumerate(grid.labels):
        for x, index in enumerate(row):
            expected = (255, 255, 255) if 1 <= x < 3 and 1 <= y < 3 else (0, 0, 0)
            if grid.palette[index].rgb != expected:
                raise RuntimeError("Packaged local quantization changed outer semantics")
    imported = worker("blueprint", "blueprint-import", file=str(source), origin="self-authored")
    if imported["record"]["normalized_size"] != [40, 40]:
        raise RuntimeError("Packaged blueprint dimensions changed")
    # Deliberately edit the entire candidate. The composite must still keep its outside black.
    black = io.BytesIO()
    Image.new("RGB", (40, 40), "black").save(black, "PNG")
    project.assets["source.png"] = black.getvalue()
    composite_input = save("合成输入", project)
    composed = worker(
        "composite", "blueprint-composite", composite_input, file=str(source), region=[1, 1, 3, 3]
    )
    with Image.open(composed["normalized_path"]) as image:
        for y in range(40):
            for x in range(40):
                expected = (255, 255, 255, 255) if 10 <= x < 30 and 10 <= y < 30 else (0, 0, 0, 255)
                if image.getpixel((x, y)) != expected:
                    raise RuntimeError("Packaged local composite changed outer pixels")
    patched = apply_patch(
        base, base_sha=base.content_sha, cells=(PatchCell(0, 0, 0, 1),), allowed=frozenset({(0, 0)})
    )
    project.entities["design"] = patched.to_dict()
    project.entities["revision:base"] = dict(design=base.to_dict())
    candidate = replace(base, revision_id="new-full-conversion")
    project.entities["candidate:full"] = dict(
        design=candidate.to_dict(), conversion=dict(result_sha=candidate.content_sha)
    )
    migration_input = save("重转输入", project)
    comparison = worker(
        "compare", "compare-candidate", migration_input, candidate_key="candidate:full"
    )
    if comparison["migration"]["entries"][0]["status"] != "ready":
        raise RuntimeError("Packaged S19 did not offer the matching prior edit")
    migrated = worker(
        "migration",
        "migrate-candidate",
        migration_input,
        candidate_key="candidate:full",
        proposal=comparison["migration"],
        selected=[[0, 0]],
    )
    if Design.from_dict(migrated["design"]).labels != patched.labels:
        raise RuntimeError("Packaged patch migration produced wrong cells")
    if sha256_file(input_path) != original_sha:
        raise RuntimeError("Worker modified the frozen project")
    report = dict(
        binary_sha=sha256_file(binary),
        local_quantization="passed",
        blueprint_import="passed",
        composite_outer_pixels="unchanged",
        semantic_migration="passed",
        frozen_input="unchanged",
        real_ai="not_called",
        windows="pending",
    )
    (root / "report.json").write_bytes(canonical_bytes(report))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()

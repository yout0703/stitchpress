"""Native full-flow large-document QA with self-authored inputs, never customer assets."""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from zipfile import ZipFile

import tifffile
from large_patterns import make_large_xsd
from PIL import Image

from stitchpress.app.background_save import read_snapshot, write_snapshot
from stitchpress.app.fixtures import new_project, sample_design
from stitchpress.app.jobs import verify_delivery
from stitchpress.app.post_settings import load_settings
from stitchpress.core.configuration import ReferenceSettings
from stitchpress.core.identity import canonical_bytes, sha256_file
from stitchpress.core.importers import export_oxs
from stitchpress.core.layout import Fabric
from stitchpress.core.models import Design
from stitchpress.core.post import PostSettings
from stitchpress.core.resources import assign_symbols, builtin_library


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path, nargs="?")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1")
    for key in ("PYTHONHOME", "PYTHONPATH"):
        environment.pop(key, None)
    measurements = {}
    executable = [str(args.binary.resolve())] if args.binary else [sys.executable]

    def worker(name, operation, **fields):
        folder = root / name
        folder.mkdir()
        output = folder / "result.json"
        job = folder / "job.json"
        job.write_bytes(
            canonical_bytes(
                dict(operation=operation, output=str(output), measure_resources=True, **fields)
            )
        )
        command = executable + (
            ["--internal-worker"] if args.binary else ["-m", "stitchpress.app.worker"]
        )
        proc = subprocess.run(
            command + [str(job)], env=environment, capture_output=True, timeout=600
        )
        (folder / "process.log").write_bytes(proc.stdout + b"\n" + proc.stderr)
        response = json.loads(output.read_bytes()) if output.exists() else {}
        if proc.returncode or response.get("status") != "completed":
            raise RuntimeError(f"{name}: {response}; see {folder}")
        measurements[name] = response["metrics"]
        assert response["metrics"]["peak_rss_bytes"] > 0
        print(name, measurements[name], flush=True)
        return response["result"]

    pattern = root / "百万格-自有压力样例.xsd"
    make_large_xsd(pattern)
    base = new_project()
    base.assets["qa/padding.bin"] = b"Q" * (270 * 1024**2)
    image = Image.new("RGB", (6500, 6500), (245, 235, 220))
    data = io.BytesIO()
    image.save(data, "JPEG")
    image.close()
    base.assets["qa/source.jpg"] = data.getvalue()
    stage = root / "initial"
    stage.mkdir()
    snapshot = write_snapshot(base, stage)
    del base
    imported = worker("import-xsd", "import-pattern", file=str(pattern), snapshot=str(snapshot))
    project = read_snapshot(Path(imported["snapshot"]))
    design = Design.from_dict(project.entities["design"])
    assert (design.width, design.height, design.counts) == (1200, 900, (360000, 360000))
    project.entities["metadata"] = dict(title="百万格压力验证", code="LARGE-QA")
    project.entities["fabric"] = asdict(replace(Fabric.from_ct("14"), dpi=72))
    project.entities["reference"] = ReferenceSettings(paper="A2", dpi=72).to_dict()
    project.entities["source"] = dict(
        asset="qa/source.jpg", filename="自有大图.jpg", correspondence_confirmed=False
    )
    oxs = root / "百万格-自有压力样例.oxs"
    oxs.write_bytes(export_oxs(design))
    assert oxs.stat().st_size > 16 * 1024**2
    imported_oxs = worker("import-oxs", "import-pattern", file=str(oxs), snapshot=str(snapshot))
    restored = Design.from_dict(read_snapshot(Path(imported_oxs["snapshot"])).entities["design"])
    assert restored.labels == design.labels and restored.counts == design.counts
    assert [(p.brand, p.code, p.rgb) for p in restored.palette] == [
        (p.brand, p.code, p.rgb) for p in design.palette
    ]
    del restored
    stage = root / "ready"
    stage.mkdir()
    snapshot = write_snapshot(project, stage)
    encoded = worker("save", "encode-project", snapshot=str(snapshot))
    saved = root / "百万格作品.stitchpress"
    shutil.copyfile(encoded["candidate"], saved)
    with ZipFile(saved) as archive:
        uncompressed = sum(item.file_size for item in archive.infolist())
        assert uncompressed > 256 * 1024**2
    reopened = worker("reopen", "load-project", file=str(saved))
    restored = read_snapshot(Path(reopened["snapshot"]))
    assert restored.entities == project.entities
    assert restored.assets == project.assets
    del restored
    source_sha = sha256_file(saved)
    preview_checks = []
    for target in ("production", "whole", "pages", "effect"):
        result = worker(
            "preview-" + target, "post-preview", project=str(saved), target=target, page_number=1
        )
        with Image.open(result["preview"]) as preview:
            assert max(preview.size) <= (4096 if target == "pages" else 1600)
        preview_checks.append(result["checks"])
    options = PostSettings(
        targets=("original", "source", "production", "whole", "pages", "effect", "settings")
    )
    destination = root / "百万格全流程输出"
    receipt = worker(
        "export",
        "reference-export",
        project=str(saved),
        target=str(destination),
        options=options.to_dict(),
        demo=True,
        cancel_file=str(root / "cancel.flag"),
    )
    verification = verify_delivery(destination, receipt)
    settings = load_settings(next(destination.glob("*作品设置.json")))
    assert settings.entities["design"] == project.entities["design"]
    assert settings.entities["fabric"] == project.entities["fabric"]
    assert settings.assets == {k: v for k, v in project.assets.items() if k != "qa/padding.bin"}
    ui_report = root / "ui-report.json"
    ui_args = ["large-ui-check", str(pattern), str(saved), str(root / "界面另存作品.stitchpress")]
    if args.binary:
        command = executable + ["--internal-cli-report", str(ui_report), *ui_args]
    else:
        command = executable + [
            "-c",
            "from stitchpress.cli import main; import sys; sys.exit(main(sys.argv[1:]))",
            *ui_args,
        ]
    proc = subprocess.run(command, env=environment, capture_output=True, timeout=600)
    (root / "ui-process.log").write_bytes(proc.stdout + b"\n" + proc.stderr)
    if args.binary:
        wrapper = json.loads(ui_report.read_bytes())
        assert proc.returncode == wrapper["exit_code"] == 0, wrapper
        ui = json.loads(wrapper["stdout"])
    else:
        assert proc.returncode == 0, proc.stderr.decode(errors="replace")
        ui = json.loads(proc.stdout)
    ui_report.write_bytes(canonical_bytes(ui))
    assert sha256_file(saved) == source_sha
    # Second input exercises a true >250 MP production raster and >80 MP reading raster.
    large_raster = new_project()
    large_design = sample_design(450, 1100, 2)
    large_raster.entities["design"] = large_design.to_dict()
    large_raster.entities["mapping"] = assign_symbols(large_design, builtin_library())
    large_raster.entities["fabric"] = asdict(Fabric.from_ct("14"))
    large_raster.entities["reference"] = ReferenceSettings(dpi=300).to_dict()
    raster_path = root / "大画布作品.stitchpress"
    raster_path.write_bytes(large_raster.encode())
    for target in ("production", "whole"):
        result = worker(
            "raster-preview-" + target, "post-preview", project=str(raster_path), target=target
        )
        with Image.open(result["preview"]) as preview:
            assert max(preview.size) <= 1600
    raster_options = PostSettings(targets=("production", "whole", "settings"))
    raster_destination = root / "大画布输出"
    raster_receipt = worker(
        "raster-export",
        "reference-export",
        project=str(raster_path),
        target=str(raster_destination),
        options=raster_options.to_dict(),
        demo=True,
        cancel_file=str(root / "cancel.flag"),
    )
    raster_verification = verify_delivery(raster_destination, raster_receipt)
    raster_sizes = {}
    for path in raster_destination.glob("*.tif"):
        with tifffile.TiffFile(path) as container:
            raster_sizes[path.name] = list(container.pages[0].shape[:2])
    assert any(h * w > 250_000_000 for h, w in raster_sizes.values())
    for artifact in raster_receipt["artifacts"]:
        checks = artifact["checks"]
        if "maximum_band_bytes" in checks:
            assert checks["maximum_band_bytes"] <= 8 * 1024**2
            assert checks["counts"] == list(large_design.counts)
            assert checks["pixel_sha_verified"]
    summary = dict(
        native_os=os.name,
        measurements=measurements,
        ui=ui,
        grid=[1200, 900],
        counts=list(design.counts),
        source_unchanged=True,
        xsd_bytes=pattern.stat().st_size,
        oxs_bytes=oxs.stat().st_size,
        project_uncompressed_bytes=uncompressed,
        original_pixels=6500 * 6500,
        previews=preview_checks,
        verification=verification,
        raster_verification=raster_verification,
        raster_sizes=raster_sizes,
        customer_original_tested=False,
        physical_print_tested=False,
    )
    (root / "verification.json").write_bytes(canonical_bytes(summary))
    # Remove only this tool's raw staging copies after successful verification.
    for name in ("initial", "ready", "import-xsd/loaded", "import-oxs/loaded", "reopen/loaded"):
        shutil.rmtree(root / name)
    print(json.dumps(dict(output=str(root), metrics=measurements, ui=ui), ensure_ascii=False))


if __name__ == "__main__":
    main()

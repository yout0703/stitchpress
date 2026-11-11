"""Fresh-process streaming benchmark; original test geometry, no customer art."""

import argparse
import json
import platform
import resource
import subprocess
import sys
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from time import monotonic

from stitchpress.app.fixtures import sample_design
from stitchpress.app.store import Project
from stitchpress.core.identity import sha256_file
from stitchpress.core.layout import Fabric
from stitchpress.core.render import Renderer
from stitchpress.core.resources import assign_symbols, library_from_assets, load_catalog
from stitchpress.core.streaming import save_production_stream


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", help="internal WIDTHxHEIGHT child")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if not args.case:
        results = []
        for case in ("48x64", "240x320", "400x420"):
            result = subprocess.run(
                [
                    sys.executable,
                    __file__,
                    "--resources",
                    str(args.resources.resolve()),
                    "--output",
                    str(args.output.resolve()),
                    "--case",
                    case,
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode:
                raise RuntimeError(result.stderr or result.stdout)
            data = json.loads(result.stdout)
            results.append(data)
            print(json.dumps(data, ensure_ascii=False), flush=True)
        report = {
            "cases": results,
            "os": platform.platform(),
            "versions": {
                name: version(name) for name in ("Pillow", "tifffile", "imagecodecs", "numpy")
            },
            "windows": "pending",
            "factory": "pending",
            "bigtiff": "unsupported-unverified",
            "support_note": "Measured cases only; resource guards are not a performance promise",
        }
        (args.output / "benchmark.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        return
    width, height = (int(n) for n in args.case.split("x"))
    project = Project.decode(args.resources.read_bytes())
    library = library_from_assets(project.entities["library"], project.assets)
    catalog = load_catalog(project.assets[project.entities["catalog"]["asset"]])
    design = replace(sample_design(width, height, 30), palette=tuple(catalog[:30]))
    renderer = Renderer(design, library, assign_symbols(design, library), Fabric(), code="BENCH")
    target = args.output / f"{args.case}-stream-cmyk.tif"
    if target.exists():
        raise RuntimeError("Use a new benchmark version; existing output will not be overwritten")
    started = monotonic()
    checks = save_production_stream(renderer, target)
    elapsed = monotonic() - started
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        rss *= 1024
    print(
        json.dumps(
            {
                "grid": [width, height],
                "pixels": renderer.layout.canvas_px,
                "elapsed_seconds": round(elapsed, 3),
                "peak_rss_bytes": rss,
                "sha256": sha256_file(target),
                "bytes": target.stat().st_size,
                "checks": checks,
            }
        )
    )


if __name__ == "__main__":
    main()

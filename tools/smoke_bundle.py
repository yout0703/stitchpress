"""Exercise the actual native executable, including its process dispatcher and font data."""

import argparse
import json
import os
import subprocess
import tomllib
from pathlib import Path

from PIL import Image


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("--output", type=Path, default=Path("reports/local/bundle-smoke-v01"))
    args = parser.parse_args()
    binary = args.binary.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = {
        k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }

    def run(arguments):
        report_path = output / "cli-response.json"
        cli = arguments[0] == "--internal-cli"
        if cli:
            report_path.unlink(missing_ok=True)
            arguments = ["--internal-cli-report", str(report_path), *arguments[1:]]
        result = subprocess.run(
            [str(binary), *arguments],
            cwd=output,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode or result.stderr.strip():
            raise RuntimeError(f"Native executable failed: {result.returncode}\n{result.stderr}")
        if cli:
            report = json.loads(report_path.read_bytes())
            if report["exit_code"] or report["stderr"].strip():
                raise RuntimeError(f"Native CLI report failed: {report}")
            return report["stdout"]
        return result.stdout

    doctor = json.loads(run(["--internal-cli", "doctor"]))
    declared = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    if doctor["app"] != declared:
        raise RuntimeError(f"Packaged version mismatch: expected {declared}, got {doctor['app']}")
    if not doctor["compiled"] or Path(doctor["worker_program"]).resolve() != binary:
        raise RuntimeError("Compiled worker launcher does not resolve to the native binary")
    library = doctor["builtin_symbol_library"]
    if not doctor["qt_chinese_translation_available"]:
        raise RuntimeError("Chinese standard-dialog translations are missing from the bundle")
    if library["symbols"] != 152 or not library["icc_sha"] or "彩色" not in library["name"]:
        raise RuntimeError("Packaged application is missing the built-in color symbol library")
    sample = output / "原生子进程.stitchpress"
    if not sample.exists():
        run(
            [
                "--internal-cli",
                "demo",
                str(sample),
                "--width",
                "12",
                "--height",
                "16",
                "--colors",
                "2",
            ]
        )
    destination = output / "package"
    if not destination.exists():
        run(
            [
                "--internal-cli",
                "export",
                str(sample),
                str(destination),
                "--demo",
                "--work",
                str(output / "jobs"),
            ]
        )
    files = list(p for p in destination.rglob("*") if p.is_file())
    if len(files) != 7:
        raise RuntimeError("Native export failed N+6 check")
    image = Image.new("RGB", (3, 2), (0, 0, 0))
    image.putpixel((2, 1), (255, 255, 255))
    image.save(output / "source.png")
    (output / "catalog.json").write_text(
        json.dumps(
            [
                {"code": "001", "name": "black", "rgb": [0, 0, 0]},
                {"code": "002", "name": "white", "rgb": [255, 255, 255]},
            ]
        ),
        encoding="utf-8",
    )
    job = {
        "operation": "convert",
        "image": str(output / "source.png"),
        "catalog": str(output / "catalog.json"),
        "output": str(output / "result.json"),
        "options": {"width": 3, "height": 2, "max_colors": 2},
        "base_sha": "smoke",
    }
    (output / "job.json").write_text(json.dumps(job), encoding="utf-8")
    run(["--internal-worker", str(output / "job.json")])
    result = json.loads((output / "result.json").read_bytes())
    if result["result"]["design"]["labels"] != [[0, 0, 0], [0, 0, 1]]:
        raise RuntimeError("Native conversion worker produced an unexpected matrix")
    report = {
        "doctor": doctor,
        "native_cli": "passed",
        "export_files": len(files),
        "native_conversion_worker": "passed",
        "clean_machine_install": "pending",
        "windows": "pending",
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()

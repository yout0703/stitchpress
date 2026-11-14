"""Use the real trial executable; a fresh machine must not generate without a license."""

import argparse
import json
import os
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    binary, output = args.executable.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    environment = {
        k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    checks = []

    def run(name, command, expected):
        report = output / (name + ".json")
        process = subprocess.run(
            [str(binary), "--internal-cli-report", str(report), *command],
            cwd=output,
            env=environment,
            capture_output=True,
            timeout=60,
        )
        data = json.loads(report.read_bytes())
        assert process.returncode == data["exit_code"] == expected, (name, data, process.stderr)
        assert not process.stderr.strip() and not data["stderr"].strip(), (name, data)
        checks.append(name)
        return json.loads(data["stdout"])

    doctor = run("doctor", ["doctor"], 0)
    assert doctor["edition"] == "trial" and doctor["compiled"] is True
    assert doctor["qt_chinese_translation_available"]
    assert doctor["builtin_output_font"] == dict(
        name="NotoSansCJKsc-Regular.otf", chinese_glyphs_verified=True
    )
    library = doctor["builtin_symbol_library"]
    assert library["symbols"] == 152 and library["icc_sha"] and "彩色" in library["name"]
    status = run("status", ["license-status"], 0)
    assert status["code"] == "unactivated", status
    run("request", ["license-request", str(output / "device-request.json")], 0)
    run("deny-demo", ["demo", str(output / "forbidden.stitchpress")], 2)
    run(
        "deny-export",
        [
            "reference-export",
            "absent",
            str(output / "forbidden-output"),
            "--work",
            str(output / "work"),
            "--demo",
        ],
        2,
    )
    assert not (output / "forbidden.stitchpress").exists()
    assert not (output / "forbidden-output").exists()
    assert not (output / "work").exists()
    result = {
        "checks": checks,
        "edition": doctor["edition"],
        "business_locked_without_license": True,
        "builtin_symbol_library": library,
    }
    (output / "verification.json").write_text(json.dumps(result, indent=2), "utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()

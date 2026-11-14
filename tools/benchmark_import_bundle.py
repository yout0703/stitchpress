"""Run import and many-color UI timings inside the actual native executable."""

import argparse
import json
import os
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("--pattern", type=Path, default=Path("fixtures/s01-independent.xsd"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    environment["QT_QPA_PLATFORM"] = "offscreen"
    process = subprocess.run(
        [
            str(args.binary.resolve()),
            "--internal-cli-report",
            str(output),
            "import-benchmark",
            str(args.pattern.resolve()),
        ],
        env=environment,
        capture_output=True,
        timeout=120,
    )
    report = json.loads(output.read_bytes())
    assert process.returncode == report["exit_code"] == 0, report
    data = json.loads(report["stdout"])
    assert data["builtin_symbols"] == 152 and data["source_unchanged"]
    assert data["many_color_grid"] == [220, 220]
    print(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    main()

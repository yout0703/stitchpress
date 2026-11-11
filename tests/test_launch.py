import sys
from pathlib import Path

from stitchpress.platform import launch


def test_source_and_compiled_worker_modes(monkeypatch):
    assert not launch.is_compiled()
    assert launch.worker_arguments("worker", ["job.json"]) == [
        "-m",
        "stitchpress",
        "--internal-worker",
        "job.json",
    ]
    monkeypatch.setattr(launch, "__compiled__", object(), raising=False)
    monkeypatch.setattr(sys, "argv", ["/tmp/test/StitchPressDesktop"])
    assert launch.is_compiled()
    assert launch.worker_arguments("cli", ["doctor"]) == ["--internal-cli", "doctor"]
    assert Path(launch.worker_program()) == Path("/tmp/test/StitchPressDesktop").resolve()


def test_windowed_cli_file_report_does_not_require_stdout(tmp_path, monkeypatch):
    import json

    from stitchpress.__main__ import main

    report = tmp_path / "中文回执.json"
    monkeypatch.setattr(
        sys, "argv", ["StitchPress", "--internal-cli-report", str(report), "doctor"]
    )
    assert main() == 0
    response = json.loads(report.read_bytes())
    assert response["exit_code"] == 0 and not response["stderr"]
    doctor = json.loads(response["stdout"])
    assert doctor["app"] and doctor["libtiff"]

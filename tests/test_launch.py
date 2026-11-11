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

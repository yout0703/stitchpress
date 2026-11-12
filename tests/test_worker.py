import io
import json
import sys

import pytest
from PIL import Image

from stitchpress.__main__ import main


def test_windowed_worker_saves_result_when_console_streams_are_absent(tmp_path, monkeypatch):
    source = tmp_path / "原图.png"
    Image.new("RGB", (2, 2), "black").save(source)
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps([{"code": "310", "name": "black", "rgb": [0, 0, 0]}]))
    output = tmp_path / "result.json"
    job_path = tmp_path / "job.json"
    job_path.write_text(
        json.dumps(
            {
                "operation": "convert",
                "image": str(source),
                "catalog": str(catalog),
                "output": str(output),
                "options": {"width": 2, "height": 2, "max_colors": 1},
            }
        )
    )
    with monkeypatch.context() as context:
        context.setattr(sys, "stdout", None)
        context.setattr(sys, "stderr", None)
        context.setattr(sys, "argv", ["StitchPress", "--internal-worker", str(job_path)])
        assert main() == 0
        sys.stdout.close()
        sys.stderr.close()
    result = json.loads(output.read_bytes())
    assert result["status"] == "completed"
    assert result["result"]["design"]["labels"] == [[0, 0], [0, 0]]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_worker_error_exits_with_chinese_report_on_legacy_console(tmp_path, monkeypatch, status):
    from stitchpress.app.fixtures import demo_project
    from stitchpress.app.reference_actions import import_pattern
    from stitchpress.core.post import PostSettings

    output = tmp_path / "result.json"
    job = {"operation": "unknown", "output": str(output)}
    target = tmp_path / "未发布"
    if status == "cancelled":
        project = import_pattern(demo_project(), "fixtures/s01-independent.xsd")
        project_path = tmp_path / "作品.stitchpress"
        project_path.write_bytes(project.encode())
        cancel = tmp_path / "cancel.flag"
        cancel.touch()
        job.update(
            operation="reference-export",
            project=str(project_path),
            target=str(target),
            options=PostSettings(targets=("production",)).to_dict(),
            cancel_file=str(cancel),
            demo=True,
        )
    path = tmp_path / "job.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    # A windowed EXE can inherit redirected handles whose encoding is CP1252.
    # The error result must remain observable, and printing it must not raise
    # another exception that opens the Windows bootloader's modal error dialog.
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    try:
        with monkeypatch.context() as context:
            context.setattr(sys, "stdout", stream)
            context.setattr(sys, "argv", ["StitchPress", "--internal-worker", str(path)])
            assert main() == 2
            stream.flush()
        response = json.loads(output.read_bytes())
        assert response["status"] == status
        assert response["error"] in raw.getvalue().decode("utf-8")
        assert not target.exists()
        assert not list(tmp_path.glob(".stitchpress-post-*"))
    finally:
        stream.close()

import json
import sys

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

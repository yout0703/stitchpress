"""Export a captured settings bundle without validating or compressing on the UI thread."""

import copy
import json
import shutil
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QProcess

from stitchpress.core.identity import canonical_bytes
from stitchpress.platform.files import atomic_write
from stitchpress.platform.launch import worker_arguments, worker_program
from stitchpress.platform.settings import settings_dir

from .background_save import write_snapshot


def export_settings(owner, target):
    if getattr(owner, "_settings_process", None) is not None:
        owner.statusBar().showMessage("作品设置仍在导出，请完成后再导出下一份")
        return False
    folder = settings_dir() / "settings-work" / uuid4().hex
    folder.mkdir(parents=True)
    try:
        snapshot = write_snapshot(copy.deepcopy(owner.draft), folder)
        output = folder / "result.json"
        job = folder / "job.json"
        atomic_write(
            job,
            canonical_bytes(
                dict(
                    operation="settings-export",
                    snapshot=str(snapshot),
                    target=str(Path(target).resolve()),
                    output=str(output),
                )
            ),
        )
        process = QProcess(owner)
        owner._settings_process = process
        process.setProgram(worker_program())
        process.setArguments(worker_arguments("worker", [str(job)]))

        def finished(code, *_):
            if owner._settings_process is not process:
                return
            owner._settings_process = None
            try:
                report = json.loads(output.read_bytes())
                if code or report.get("status") != "completed":
                    raise ValueError(report.get("error", "作品设置未导出，原文件保持不变"))
                owner.statusBar().showMessage(
                    "设置 JSON 和相对资源目录已保存，可一起移动重开：" + str(target)
                )
            except Exception as exc:
                owner.error(exc)
            finally:
                shutil.rmtree(folder, ignore_errors=True)
                process.deleteLater()

        process.finished.connect(finished)
        process.errorOccurred.connect(
            lambda error: finished(2) if error == QProcess.ProcessError.FailedToStart else None
        )
        process.start()
        owner.statusBar().showMessage("正在后台导出作品设置，可以继续切页和编辑…")
        return True
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise

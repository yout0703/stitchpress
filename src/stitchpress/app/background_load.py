"""Cancelable process loading; a failed/obsolete result never replaces the active draft."""

import json
import shutil
import struct
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

from PySide6.QtCore import QProcess, Qt
from PySide6.QtWidgets import QProgressDialog

from stitchpress.core.errors import ConflictError, StitchPressError
from stitchpress.core.identity import canonical_bytes
from stitchpress.platform.launch import worker_arguments, worker_program
from stitchpress.platform.settings import settings_dir

from .background_save import read_snapshot, write_snapshot
from .store import Coordinator


def needs_background(path, kind):
    path = Path(path)
    if path.stat().st_size > 2 * 1024**2:
        return True
    try:
        if kind == "load-project":
            with ZipFile(path) as archive:
                return archive.getinfo("manifest.json").file_size > 1024**2
        if path.suffix.lower() == ".xsd":
            with path.open("rb") as stream:
                header = stream.read(745)
            return (
                len(header) == 745
                and struct.unpack_from("<H", header)[0] == 0x0510
                and (
                    struct.unpack_from("<H", header, 741)[0]
                    * struct.unpack_from("<H", header, 743)[0]
                    > 250_000
                )
            )
    except (OSError, ValueError, KeyError):
        pass
    return False


def cancel_load(owner):
    process = getattr(owner, "_load_process", None)
    if process is not None:
        process.load_cancelled = True
        process.kill()


def start_load(owner, path, kind):
    if getattr(owner, "_load_process", None) is not None:
        raise StitchPressError("正在后台打开文件，请完成或取消后重试")
    folder = settings_dir() / "load-work" / uuid4().hex
    folder.mkdir(parents=True)
    source_id = owner.draft.project_id
    output = folder / "result.json"
    job = dict(operation=kind, file=str(Path(path).resolve()), output=str(output))
    if kind == "import-pattern":
        job["snapshot"] = str(write_snapshot(owner.draft, folder))
    job_path = folder / "job.json"
    job_path.write_bytes(canonical_bytes(job))
    progress = QProgressDialog("正在后台解析和校验大文件，当前作品保持不变…", "取消", 0, 0, owner)
    progress.setWindowTitle("打开大图纸" if kind == "import-pattern" else "打开大作品")
    progress.setWindowModality(Qt.WindowModality.WindowModal)
    progress.setMinimumDuration(0)
    progress.setAutoClose(False)
    process = QProcess(owner)
    owner._load_process = process
    process.load_cancelled = False
    process.setProgram(worker_program())
    process.setArguments(worker_arguments("worker", [str(job_path)]))
    process.readyReadStandardOutput.connect(process.readAllStandardOutput)
    process.readyReadStandardError.connect(process.readAllStandardError)
    progress.canceled.connect(lambda: cancel_load(owner))
    finished = False

    def complete(code, *_):
        nonlocal finished
        if finished:
            return
        finished = True
        owner._load_process = None
        progress.close()
        writer = None
        try:
            if process.load_cancelled:
                owner.statusBar().showMessage("已取消打开；原作品和草稿保持不变")
                return
            if code != 0 or not output.is_file():
                reply = json.loads(output.read_bytes()) if output.is_file() else {}
                raise StitchPressError(reply.get("error", "后台打开失败，当前作品保持不变"))
            reply = json.loads(output.read_bytes())
            if reply.get("status") != "completed" or owner.draft.project_id != source_id:
                raise ConflictError("打开结果已过期，当前作品保持不变")
            from stitchpress.platform.licensing import require_license

            require_license()
            result = reply["result"]
            project = read_snapshot(Path(result["snapshot"]))
            project.validate_contents()
            if kind == "load-project":
                project.source_container_sha = result["source_sha"]
                project.source_project_id = project.project_id
                writer = Coordinator.from_loaded(Path(path), project)
                owner.set_project(project, Path(path), writer)
            else:
                owner.set_project(project)
                owner.dirty = True
                owner._reference_path = Path(path)
                owner.update_document_state()
            owner.statusBar().showMessage("大文件已打开；请核对作品信息、实际格距及预览")
        except Exception as exc:
            if writer is not None and writer is not owner.coordinator:
                writer.close()
            owner.error(exc)
        finally:
            process.deleteLater()
            progress.deleteLater()
            shutil.rmtree(folder, ignore_errors=True)

    process.finished.connect(complete)
    process.errorOccurred.connect(
        lambda error: complete(2) if error == QProcess.ProcessError.FailedToStart else None
    )
    progress.show()
    process.start()

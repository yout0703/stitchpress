"""Compress a captured save in a process; only the coordinator publishes it."""

import copy
import json
import shutil
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_STORED

from PySide6.QtCore import QProcess

from stitchpress.core.identity import canonical_bytes, digest
from stitchpress.platform.files import atomic_write
from stitchpress.platform.launch import worker_arguments, worker_program
from stitchpress.platform.settings import settings_dir

from .store import PROJECT_SCHEMA


def request_save(owner):
    if owner._save_process is not None:
        owner.statusBar().showMessage("正在保存上一份快照；当前新修改会保留，完成后可再次保存")
        return False
    if (
        owner.coordinator is None
        or owner.coordinator.project.source_schema < PROJECT_SCHEMA
        or sum(map(len, owner.draft.assets.values())) < 8 * 1024**2
    ):
        return owner.save()
    if owner.reference_pending():
        owner.error("有尚未应用的页面参数，请先应用对应页面")
        return False
    if not owner.apply_fields():
        return False
    folder = None
    try:
        captured = copy.deepcopy(owner.draft)
        baseline = owner.baseline
        coordinator = owner.coordinator
        changes = {k: v for k, v in captured.entities.items() if v != baseline.entities.get(k)}
        for key in baseline.entities.keys() - captured.entities.keys():
            changes[key] = None
        if not changes and captured.assets == baseline.assets:
            owner.dirty = False
            owner.update_document_state()
            owner.statusBar().showMessage("作品已经保存，没有新的修改")
            return True
        reads = {k: baseline.revision(k) for k in changes}
        prepared = copy.deepcopy(captured)
        prepared.commit_seq = baseline.commit_seq + 1
        folder = settings_dir() / "save-work" / uuid4().hex
        folder.mkdir(parents=True)
        snapshot = folder / "input.stitchpress"
        atomic_write(snapshot, prepared.encode(compression=ZIP_STORED))
        output = folder / "result.json"
        job = folder / "job.json"
        atomic_write(
            job,
            canonical_bytes(
                dict(operation="encode-project", project=str(snapshot), output=str(output))
            ),
        )
        process = QProcess(owner)
        owner._save_process = process
        process.setProgram(worker_program())
        process.setArguments(worker_arguments("worker", [str(job)]))

        def finished(code, *_):
            active = owner._save_process is process
            try:
                if not active or owner.coordinator is not coordinator:
                    return
                owner._save_process = None
                report = json.loads(output.read_bytes())
                if code or report.get("status") != "completed":
                    raise ValueError(report.get("error", "后台保存未完成，草稿已保留"))
                result = coordinator.commit(
                    expected_seq=baseline.commit_seq,
                    reads=reads,
                    changes=changes,
                    assets=captured.assets,
                    prepared_payload=Path(report["result"]["candidate"]).read_bytes(),
                )
                newer = (
                    digest(owner.draft.entities) != digest(captured.entities)
                    or owner.draft.assets != captured.assets
                )
                owner.baseline = copy.deepcopy(result)
                if newer:
                    owner.draft.commit_seq = result.commit_seq
                    owner.draft.events = list(result.events)
                else:
                    owner.draft = copy.deepcopy(result)
                owner.dirty = newer
                owner.stop_recovery_worker()
                owner.recovery.clear()
                owner._last_recovery_sha = None
                owner.statusBar().showMessage(
                    f"已保存 · 提交 {result.commit_seq}"
                    + ("；保存期间的新修改仍待保存" if newer else "")
                )
            except Exception as exc:
                owner.error(exc)
            finally:
                if active:
                    owner.update_document_state()
                shutil.rmtree(folder, ignore_errors=True)
                process.deleteLater()

        process.finished.connect(finished)
        process.errorOccurred.connect(
            lambda error: finished(2) if error == QProcess.ProcessError.FailedToStart else None
        )
        owner.update_document_state()
        owner.statusBar().showMessage("正在后台保存，可以继续切页和编辑…")
        process.start()
        return True
    except Exception as exc:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        owner.error(exc)
        return False


def stop_save(owner):
    process = getattr(owner, "_save_process", None)
    if process is not None:
        owner._save_process = None
        process.kill()

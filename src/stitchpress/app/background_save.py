"""Compress a captured save in a process; only the coordinator publishes it."""

import copy
import json
import shutil
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QProcess

from stitchpress.core.identity import canonical_bytes, digest
from stitchpress.platform.files import atomic_write
from stitchpress.platform.launch import worker_arguments, worker_program
from stitchpress.platform.settings import settings_dir

from .store import PROJECT_SCHEMA, Coordinator


def write_snapshot(project, folder):
    """Stage immutable bytes without image validation or compression on the UI thread."""
    resources = folder / "resources"
    resources.mkdir()
    names = []
    for index, (name, data) in enumerate(project.assets.items()):
        filename = f"{index}.bin"
        (resources / filename).write_bytes(data)
        names.append((name, filename))
    snapshot = folder / "snapshot.json"
    snapshot.write_bytes(
        canonical_bytes(
            dict(
                project_id=project.project_id,
                commit_seq=project.commit_seq,
                entities=project.entities,
                events=project.events,
                source_schema=project.source_schema,
                asset_files=names,
            )
        )
    )
    return snapshot


def read_snapshot(path):
    from .store import Project

    document = json.loads(path.read_bytes())
    assets = {}
    for index, (name, filename) in enumerate(document.pop("asset_files")):
        if filename != f"{index}.bin" or name in assets:
            raise ValueError("后台保存资源清单无效")
        assets[name] = (path.parent / "resources" / filename).read_bytes()
    return Project(**document, assets=assets)


def needs_background_save(project):
    design = project.entities.get("design", {})
    return (
        sum(map(len, project.assets.values())) >= 8 * 1024**2
        or design.get("width", 0) * design.get("height", 0) > 250_000
    )


def request_save(owner, *, copy_target=None):
    if owner._save_process is not None:
        owner.statusBar().showMessage("正在保存上一份快照；当前新修改会保留，完成后可再次保存")
        return False
    if copy_target is None and (
        owner.coordinator is None
        or owner.coordinator.project.source_schema < PROJECT_SCHEMA
        or not needs_background_save(owner.draft)
    ):
        return owner.save()
    if not owner.check_save_forms():
        return False
    folder = None
    copy_coordinator = None
    try:
        original_coordinator = owner.coordinator
        original_id = owner.draft.project_id
        original_snapshot = copy.deepcopy(owner.draft)
        captured = original_snapshot
        if copy_target is not None:
            captured = captured.upgraded_copy()
            captured.project_id = uuid4().hex
            copy_coordinator = Coordinator(copy_target, captured)
        baseline = captured if copy_target is not None else owner.baseline
        coordinator = copy_coordinator or owner.coordinator
        changes = {
            k: v
            for k, v in captured.entities.items()
            if k not in baseline.entities or v != baseline.entities[k]
        }
        for key in baseline.entities.keys() - captured.entities.keys():
            changes[key] = None
        if copy_target is None and not changes and captured.assets == baseline.assets:
            owner.dirty = False
            owner.update_document_state()
            owner.statusBar().showMessage("作品已经保存，没有新的修改")
            return True
        reads = {k: baseline.revision(k) for k in changes}
        prepared = copy.deepcopy(captured)
        prepared.commit_seq = baseline.commit_seq + 1
        # A None change deletes the entity at commit. Match that exact snapshot,
        # including drafts created by older versions that retained tombstones.
        for key, value in changes.items():
            if value is None:
                prepared.entities.pop(key, None)
        folder = settings_dir() / "save-work" / uuid4().hex
        folder.mkdir(parents=True)
        snapshot = write_snapshot(prepared, folder)
        output = folder / "result.json"
        job = folder / "job.json"
        atomic_write(
            job,
            canonical_bytes(
                dict(operation="encode-project", snapshot=str(snapshot), output=str(output))
            ),
        )
        process = QProcess(owner)
        owner._save_process = process
        owner._save_cleanup = copy_coordinator.close if copy_coordinator else None
        process.setProgram(worker_program())
        process.setArguments(worker_arguments("worker", [str(job)]))

        def finished(code, *_):
            active = owner._save_process is process
            try:
                if not active or owner.coordinator is not original_coordinator:
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
                    digest(owner.draft.entities) != digest(original_snapshot.entities)
                    or owner.draft.assets != original_snapshot.assets
                    or owner.has_pending_form()
                    or owner.reference_pending()
                )
                if copy_target is not None:
                    if original_coordinator:
                        original_coordinator.close()
                    owner.coordinator, owner.project_path = coordinator, copy_target
                    owner._save_cleanup = None  # The active document now owns this lock.
                    for panel in getattr(owner, "reference_panels", ()):
                        if panel.project_id == original_id:
                            panel.project_id = result.project_id
                    if owner.reference_project == original_id:
                        owner.reference_project = result.project_id
                    owner.draft.project_id = result.project_id
                    owner.draft.source_schema = result.source_schema
                    for key in captured.entities.keys() - original_snapshot.entities.keys():
                        owner.draft.entities.setdefault(key, copy.deepcopy(captured.entities[key]))
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
                if active and owner._save_cleanup:
                    owner._save_cleanup()
                    owner._save_cleanup = None
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
        if copy_coordinator:
            copy_coordinator.close()
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        owner.error(exc)
        return False


def stop_save(owner):
    process = getattr(owner, "_save_process", None)
    if process is not None:
        owner._save_process = None
        process.kill()
        cleanup = getattr(owner, "_save_cleanup", None)
        if cleanup:
            cleanup()
            owner._save_cleanup = None

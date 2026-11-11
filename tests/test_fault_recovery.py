"""Inject real boundary failures: no successful-looking partial delivery or lost saved master."""

import json
from collections import namedtuple

import pytest

from stitchpress.app import export, store
from stitchpress.app.fixtures import demo_project
from stitchpress.app.jobs import find_receipt, verify_delivery
from stitchpress.core.errors import StitchPressError
from stitchpress.core.layout import Fabric
from stitchpress.core.resources import assign_symbols, demo_library


def parameters(tmp_path, design):
    library = demo_library(2)
    return dict(
        design=design,
        library=library,
        mapping=assign_symbols(design, library),
        fabric=Fabric(),
        code="AK0026",
        title="Fault fixture",
        review=None,
        demo=True,
        destination=tmp_path / "package",
        work=tmp_path / "work",
    )


def test_full_disk_preflight_does_not_publish_or_create_journal(tmp_path, minimal, monkeypatch):
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(export.shutil, "disk_usage", lambda _: Usage(100, 100, 0))
    with pytest.raises(StitchPressError, match="空间不足"):
        export.export_package(**parameters(tmp_path, minimal))
    assert not (tmp_path / "package").exists()
    assert not list((tmp_path / "work").glob("job-*.json"))


def test_pdf_failure_after_other_artifacts_never_publishes_partial_package(
    tmp_path, minimal, monkeypatch
):
    def fail(*args, **kwargs):
        raise OSError("injected PDF write failure")

    monkeypatch.setattr(export, "write_pdf", fail)
    with pytest.raises(OSError, match="injected"):
        export.export_package(**parameters(tmp_path, minimal))
    assert not (tmp_path / "package").exists()
    assert not list(tmp_path.glob(".stitchpress-*"))
    journal = json.loads(next((tmp_path / "work").glob("job-*.json")).read_bytes())
    assert journal["status"] == "failed" and journal["artifacts"]


def test_published_files_recover_after_final_journal_write_failure(tmp_path, minimal, monkeypatch):
    original_write = export.atomic_write
    failed = False

    def fail_final_once(path, payload):
        nonlocal failed
        if (
            path.name.startswith("job-")
            and json.loads(payload).get("status") == "completed"
            and not failed
        ):
            failed = True
            raise OSError("injected final journal failure")
        return original_write(path, payload)

    monkeypatch.setattr(export, "atomic_write", fail_final_once)
    with pytest.raises(OSError, match="journal"):
        export.export_package(**parameters(tmp_path, minimal))
    assert failed and (tmp_path / "package").is_dir()
    recovered = find_receipt(tmp_path / "work", tmp_path / "package")
    assert recovered and verify_delivery(tmp_path / "package", recovered)["files"] == 7
    # Recovery uses actual bytes; a later change must prevent accepting that old receipt.
    next((tmp_path / "package").glob("*.xlsx")).write_bytes(b"corrupted after publication")
    assert find_receipt(tmp_path / "work", tmp_path / "package") is None


def test_failed_atomic_project_write_keeps_original_and_writer_revision(tmp_path, monkeypatch):
    path = tmp_path / "作品.stitchpress"
    writer = store.Coordinator(path, demo_project())
    try:
        writer.commit(expected_seq=0, reads={}, changes={})
        before = path.read_bytes()
        original_write = store.atomic_write

        def fail_active(target, payload):
            if target == path:
                raise OSError("injected disk full before replace")
            return original_write(target, payload)

        monkeypatch.setattr(store, "atomic_write", fail_active)
        with pytest.raises(OSError, match="disk full"):
            writer.commit(
                expected_seq=1,
                reads={"metadata": writer.project.revision("metadata")},
                changes={"metadata": {"title": "未保存修改", "code": "AK0027"}},
            )
        assert path.read_bytes() == before and writer.project.commit_seq == 1
        assert writer.project.entities["metadata"]["code"] != "AK0027"
    finally:
        writer.close()


def test_publish_collision_preserves_the_other_task_directory(tmp_path, minimal):
    params = parameters(tmp_path, minimal)

    def race(event):
        if event["stage"] == "publishing":
            params["destination"].mkdir()
            (params["destination"] / "keep.txt").write_text("another task")

    with pytest.raises(StitchPressError, match="未覆盖"):
        export.export_package(**params, progress=race)
    assert (params["destination"] / "keep.txt").read_text() == "another task"
    assert list(params["destination"].iterdir()) == [params["destination"] / "keep.txt"]
    assert not list(tmp_path.glob(".stitchpress-*"))

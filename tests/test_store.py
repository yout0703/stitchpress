import io
import json
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from stitchpress.app.store import Coordinator, Project
from stitchpress.core.errors import ConflictError, StitchPressError
from stitchpress.core.identity import digest


def test_portable_assets_and_hash_verification(tmp_path, minimal):
    project = Project.create("中文测试")
    project.entities["design"] = minimal.to_dict()
    project.assets["sources/原图.bin"] = b"original bytes"
    assert Project.decode(project.encode()).assets == project.assets
    output = io.BytesIO()
    with ZipFile(io.BytesIO(project.encode())) as src, ZipFile(output, "w") as dst:
        for name in src.namelist():
            dst.writestr(name, b"tamper" if name.startswith("sources") else src.read(name))
    with pytest.raises(StitchPressError, match="校验"):
        Project.decode(output.getvalue())


@pytest.mark.parametrize(
    "names",
    [("../escape",), ("/absolute",), ("C:/escape",), ("A", "a"), ("é", "e\u0301"), ("foo ",)],
)
def test_s08_archive_path_checks(names):
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as arc:
        for name in names:
            arc.writestr(name, b"x")
    with pytest.raises(StitchPressError):
        Project.decode(output.getvalue())


def test_s18_unrelated_event_does_not_block_title_and_duplicates_dedup(tmp_path):
    path = tmp_path / "作品.stitchpress"
    coordinator = Coordinator(path, Project.create())
    try:
        metadata_base = coordinator.project.revision("metadata")
        coordinator.commit(
            expected_seq=0,
            reads={"job:1": digest(None)},
            changes={"job:1": {"status": "completed"}},
            event_id="event:1",
        )
        result = coordinator.commit(
            expected_seq=0,
            reads={"metadata": metadata_base},
            changes={"metadata": {"title": "用户编辑", "code": "AK0026"}},
        )
        assert result.commit_seq == 2
        assert result.entities["job:1"]["status"] == "completed"
        coordinator.commit(
            expected_seq=0,
            reads={"job:1": digest(None)},
            changes={"job:1": {"status": "completed"}},
            event_id="event:1",
        )
        assert coordinator.project.commit_seq == 2
        with pytest.raises(ConflictError):
            coordinator.commit(
                expected_seq=0,
                reads={"metadata": metadata_base},
                changes={"metadata": {"title": "过期编辑"}},
            )
        assert Project.decode(path.read_bytes()).entities["metadata"]["title"] == "用户编辑"
    finally:
        coordinator.close()


def test_s18_lock_and_external_modification(tmp_path):
    path = tmp_path / "project.stitchpress"
    coordinator = Coordinator(path, Project.create())
    try:
        with pytest.raises(ConflictError):
            Coordinator(path, Project.create())
        path.write_bytes(Project.create("external").encode())
        with pytest.raises(ConflictError, match="外部修改"):
            coordinator.commit(expected_seq=0, reads={}, changes={})
    finally:
        coordinator.close()


def test_future_schema_is_not_written(tmp_path):
    project = Project.create()
    encoded = project.encode()
    result = io.BytesIO()
    with ZipFile(io.BytesIO(encoded)) as src, ZipFile(result, "w") as dst:
        manifest = json.loads(src.read("manifest.json"))
        manifest["schema_version"] = 999
        dst.writestr("manifest.json", json.dumps(manifest))
    with pytest.raises(StitchPressError, match="版本"):
        Project.decode(result.getvalue())


def test_symlink_alias_cannot_acquire_a_second_writer(tmp_path):
    import os

    if os.name == "nt":
        pytest.skip("Windows symlink creation requires a separate privilege check")
    path = tmp_path / "original.stitchpress"
    path.write_bytes(Project.create().encode())
    alias = tmp_path / "alias.stitchpress"
    alias.symlink_to(path)
    coordinator = Coordinator(path)
    try:
        with pytest.raises(ConflictError):
            Coordinator(alias)
    finally:
        coordinator.close()

import io
import json
from zipfile import ZipFile

import pytest

from stitchpress.app.fixtures import demo_project
from stitchpress.app.store import PROJECT_SCHEMA, Coordinator, Project
from stitchpress.core.errors import ConflictError, StitchPressError
from stitchpress.core.identity import sha256


def legacy_bytes():
    current = demo_project().encode()
    result = io.BytesIO()
    with ZipFile(io.BytesIO(current)) as source, ZipFile(result, "w") as target:
        for name in source.namelist():
            data = source.read(name)
            if name == "manifest.json":
                manifest = json.loads(data)
                manifest["schema_version"] = 1
                data = json.dumps(manifest).encode()
            target.writestr(name, data)
    return result.getvalue()


def test_v1_migration_is_an_independent_copy_preserving_source_bytes_and_resources(tmp_path):
    raw = legacy_bytes()
    source = tmp_path / "legacy.stitchpress"
    source.write_bytes(raw)
    coordinator = Coordinator(source)
    assert coordinator.project.source_schema == 1
    try:
        with pytest.raises(ConflictError, match="另存"):
            coordinator.commit(expected_seq=0, reads={}, changes={})
        target = tmp_path / "upgraded.stitchpress"
        copy = Coordinator(target, coordinator.project)
        try:
            copy.commit(expected_seq=0, reads={}, changes={})
        finally:
            copy.close()
        restored = Project.decode(target.read_bytes())
        assert restored.source_schema == PROJECT_SCHEMA
        assert restored.assets == coordinator.project.assets
        records = [v for k, v in restored.entities.items() if k.startswith("migration:")]
        assert len(records) == 1 and records[0]["original_container_sha"] == sha256(raw)
        assert source.read_bytes() == raw
        assert not any(k.startswith("migration:") for k in coordinator.project.entities)
    finally:
        coordinator.close()


def test_boolean_manifest_version_is_not_an_integer_schema():
    raw = legacy_bytes()
    result = io.BytesIO()
    with ZipFile(io.BytesIO(raw)) as source, ZipFile(result, "w") as target:
        for name in source.namelist():
            data = source.read(name)
            if name == "manifest.json":
                manifest = json.loads(data)
                manifest["schema_version"] = True
                data = json.dumps(manifest).encode()
            target.writestr(name, data)
    with pytest.raises(StitchPressError, match="版本"):
        Project.decode(result.getvalue())


def test_migration_cli_new_file_report_and_same_path_rejection(tmp_path, capsys):
    from stitchpress.cli import main

    source, target = tmp_path / "旧作品.stitchpress", tmp_path / "升级副本.stitchpress"
    raw = legacy_bytes()
    source.write_bytes(raw)
    assert main(["migrate", str(source), str(source)]) == 2
    assert source.read_bytes() == raw
    capsys.readouterr()
    assert main(["migrate", str(source), str(target)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["assets_preserved"] and report["original_preserved"]
    assert report["to_schema"] == 2 and report["original_sha"] == sha256(raw)
    first = target.read_bytes()
    assert main(["migrate", str(source), str(target)]) == 2
    assert target.read_bytes() == first and source.read_bytes() == raw

import copy
from pathlib import Path

import pytest

from stitchpress.app.desktop import Workbench
from stitchpress.app.fixtures import demo_project, new_project, sample_design
from stitchpress.app.reference_actions import import_pattern
from stitchpress.app.store import Coordinator, Project
from stitchpress.core.errors import ConflictError, StitchPressError
from stitchpress.core.models import Design
from stitchpress.core.resources import assign_symbols, builtin_library


def test_builtin_validation_reuse_requires_exact_record_and_immutable_byte_identity(monkeypatch):
    from stitchpress.core import resources

    library = builtin_library()
    project = new_project()
    parser = resources.parse_symbol
    parsed = []

    def counted(*args):
        parsed.append(1)
        return parser(*args)

    monkeypatch.setattr(resources, "parse_symbol", counted)
    assert resources.library_from_assets(project.entities["library"], project.assets) is library
    assert not parsed
    # Fresh bytes must be checked even when content matches an installed resource.
    changed = dict(project.assets)
    name = project.entities["library"]["entries"][0]["asset"]
    changed[name] = bytes(bytearray(changed[name]))
    assert resources.library_from_assets(project.entities["library"], changed) == library
    assert len(parsed) == 152
    changed[name] = b"broken tile"
    with pytest.raises(StitchPressError):
        resources.library_from_assets(project.entities["library"], changed)
    record = copy.deepcopy(project.entities["library"])
    record["entries"][-1]["external_id"] = 152.0
    with pytest.raises(ValueError, match="浮点"):
        resources.library_from_assets(record, project.assets)


def test_xsd_import_validates_without_serializing_all_symbol_assets(monkeypatch):
    project = new_project()
    before = copy.deepcopy(project.entities)

    def forbidden(*args, **kwargs):
        raise AssertionError("Import must not build a temporary project container")

    monkeypatch.setattr(Project, "encode", forbidden)
    updated = import_pattern(project, "fixtures/s01-independent.xsd")
    assert Design.from_dict(updated.entities["design"]).counts == (3, 3)
    assert len(updated.entities["mapping"]) == 2
    assert project.entities == before
    broken = copy.deepcopy(project)
    broken.assets["../escape"] = b"not portable"
    with pytest.raises(StitchPressError, match="路径"):
        import_pattern(broken, "fixtures/s01-independent.xsd")


def test_many_color_gallery_does_not_rebuild_master_per_palette_row(qtbot, monkeypatch):
    window = Workbench()
    qtbot.addWidget(window)
    design = sample_design(220, 220, 77)
    project = new_project()
    project.entities["design"] = design.to_dict()
    project.entities["mapping"] = assign_symbols(design, builtin_library())
    window.set_project(project)
    window._symbol_page_sha = None
    from_dict = Design.from_dict
    rebuilds = []

    def counted(cls, data):
        rebuilds.append(1)
        return from_dict(data)

    monkeypatch.setattr(Design, "from_dict", classmethod(counted))
    window.refresh_reference_symbols()
    assert window.ref_mapping.rowCount() > 20
    assert len(rebuilds) <= 3  # Bound work independently of the number of palette rows.
    window.dirty = False
    window.close()


def test_open_uses_the_decoded_snapshot_hash_to_detect_concurrent_external_edit(
    tmp_path, monkeypatch
):
    path = tmp_path / "snapshot.stitchpress"
    original = demo_project()
    path.write_bytes(original.encode())
    external = demo_project()
    external.entities["metadata"]["title"] = "Externally replaced during open"
    external_payload = external.encode()
    read_bytes = Path.read_bytes
    replaced = []

    def read_then_replace(candidate):
        data = read_bytes(candidate)
        if candidate == path and not replaced:
            replaced.append(True)
            path.write_bytes(external_payload)
        return data

    monkeypatch.setattr(Path, "read_bytes", read_then_replace)
    writer = Coordinator(path)
    try:
        assert writer.project.project_id == original.project_id
        with pytest.raises(ConflictError, match="外部修改"):
            writer.commit(
                expected_seq=0,
                reads={"metadata": writer.project.revision("metadata")},
                changes={"metadata": {"title": "Must not overwrite", "code": ""}},
            )
        assert path.read_bytes() == external_payload
    finally:
        writer.close()

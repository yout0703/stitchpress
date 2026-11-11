import io
import json
from dataclasses import asdict, replace
from zipfile import ZipFile

import pytest

from stitchpress.app.export import export_package
from stitchpress.app.fixtures import demo_project
from stitchpress.app.recovery import PresetStore, RecoveryStore
from stitchpress.app.store import Coordinator, Project
from stitchpress.core.configuration import Preset, ReferenceSettings, resize_fabric
from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import canonical_bytes, digest
from stitchpress.core.layout import Fabric, page_plan
from stitchpress.core.models import ReviewRecord
from stitchpress.core.quality import inspect_design
from stitchpress.core.resources import assign_symbols, demo_library


def test_saved_versions_and_real_entity_deletion(tmp_path):
    path = tmp_path / "作品.stitchpress"
    with_writer = Coordinator(path, Project.create("原名"))
    try:
        first = with_writer.commit(expected_seq=0, reads={}, changes={})
        with_writer.commit(
            expected_seq=1, reads={"note": digest(None)}, changes={"note": {"text": "temp"}}
        )
        with_writer.commit(
            expected_seq=2,
            reads={"note": with_writer.project.revision("note")},
            changes={"note": None},
        )
        assert "note" not in Project.decode(path.read_bytes()).entities
        versions = with_writer.versions()
        assert len(versions) == 2
        assert any(Project.decode(p.read_bytes()).commit_seq == first.commit_seq for p in versions)
    finally:
        with_writer.close()


def test_recovery_survives_a_new_session_and_detects_tampering(tmp_path):
    store = RecoveryStore(tmp_path / "drafts")
    project = demo_project()
    project.entities["metadata"]["title"] = "未保存中文草稿"
    store.checkpoint(project, tmp_path / "original.stitchpress")
    other = RecoveryStore(store.directory)
    record = other.candidates()[0]
    restored = other.restore(record)
    assert canonical_bytes(restored.entities) == canonical_bytes(project.entities)
    assert restored.assets == project.assets
    record["path"].write_bytes(b"tampered")
    with pytest.raises(StitchPressError, match="SHA"):
        other.restore(record)


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.entities["metadata"].update(code=26),
        lambda p: p.entities.update(source={"asset": "not-in-project.png"}),
        lambda p: p.entities.update(config={"api_key": "must-not-be-saved"}),
        lambda p: p.entities.update(mapping={"missing": "int:0"}),
        lambda p: p.events.extend(["event", "event"]),
    ],
)
def test_invalid_entities_never_reach_disk(change):
    project = demo_project()
    change(project)
    with pytest.raises(StitchPressError):
        project.encode()


def test_unknown_manifest_fields_cannot_be_discarded_on_old_writer():
    original = Project.create().encode()
    payload = io.BytesIO()
    with ZipFile(io.BytesIO(original)) as src, ZipFile(payload, "w") as dst:
        manifest = json.loads(src.read("manifest.json"))
        manifest["future_required"] = {"do_not_drop": True}
        dst.writestr("manifest.json", json.dumps(manifest))
    with pytest.raises(StitchPressError, match="未知字段"):
        Project.decode(payload.getvalue())


def test_fake_truthy_review_is_rejected(minimal):
    review = ReviewRecord(
        minimal.content_sha, "human", "false", ((0, 0, 1, 1), (1, 0, 2, 1)), True, True, ""
    )
    with pytest.raises(StitchPressError, match="布尔"):
        review.validate(minimal)


def test_presets_are_immutable_and_target_width_preserves_pitch_ratio(tmp_path):
    source = Fabric(sx="0.181", sy="0.182", left="1.1", top="2.2", right="3.3", bottom="4.4")
    resized = resize_fabric(source, 200, target_width_cm="72.4")
    assert (resized.sx, resized.sy) == ("0.362", "0.364")
    assert (resized.left, resized.top, resized.right, resized.bottom) == (
        "1.1",
        "2.2",
        "3.3",
        "4.4",
    )
    store = PresetStore(tmp_path)
    preset = Preset(
        "自定义A3",
        asdict(resized),
        asdict(ReferenceSettings(paper="A3", landscape=True, scale="2", overlap=5)),
    )
    first = store.save(preset)
    changed = replace(preset, name="新版本")
    second = store.save(changed)
    assert first != second
    assert len(store.list()[0]) == 2
    restored = store.list()[0][0]
    assert restored.fabric["sy"] == "0.364"
    with pytest.raises(StitchPressError):
        ReferenceSettings(scale="0.8")


def test_diagnostics_do_not_remove_white_subject_or_auto_approve(minimal):
    before = minimal.content_sha
    report = inspect_design(minimal)
    assert report["stitches"] == 5
    assert report["blank_cells"] == 1
    assert report["light_stitched_colors"] == [1]
    assert report["human_review"] == "required"
    assert report["tiny_components"]
    assert minimal.content_sha == before


def test_reference_only_change_reuses_production_counts_and_showcase(tmp_path, minimal):
    library = demo_library(2)
    params = dict(
        design=minimal,
        library=library,
        mapping=assign_symbols(minimal, library),
        fabric=Fabric(),
        code="AK0026",
        title="test",
        work=tmp_path / "jobs",
        cache_root=tmp_path / "cache",
        review=None,
        demo=True,
    )
    first = export_package(**params, destination=tmp_path / "first")
    second = export_package(
        **params,
        destination=tmp_path / "second",
        reference=ReferenceSettings(scale="2", style="pale"),
    )
    assert second["reused"] == 3
    by_name = {r["name"]: r for r in first["artifacts"]}
    for item in second["artifacts"]:
        if item["reused"]:
            assert item["sha256"] == by_name[item["name"]]["sha256"]
    assert first["input_fingerprint"] != second["input_fingerprint"]
    # A corrupt cache file is regenerated rather than copied into a new delivery.
    artifact = next(x for x in first["artifacts"] if x["name"].endswith(".xlsx"))
    (tmp_path / "cache" / artifact["input_fingerprint"] / "artifact.bin").write_bytes(b"corrupt")
    third = export_package(**params, destination=tmp_path / "third")
    workbook = next((tmp_path / "third").glob("*.xlsx"))
    from openpyxl import load_workbook

    assert load_workbook(workbook, data_only=True).active["E5"].value == sum(minimal.counts)
    assert third["reused"] == 4


def test_reading_overlap_cores_cover_exactly_once(minimal):
    reference = ReferenceSettings(paper="A3", landscape=True, scale="2", overlap=3)
    plan = page_plan(
        240,
        320,
        2,
        Fabric(),
        paper_cm=reference.paper_cm,
        scale=reference.scale,
        overlap=reference.overlap,
    )
    coverage = [[0] * 240 for _ in range(320)]
    for page in plan.pages:
        x0, y0, x1, y1 = page.core
        for y in range(y0, y1):
            for x in range(x0, x1):
                coverage[y][x] += 1
        assert page.visible[0] <= x0 and page.visible[1] <= y0
    assert all(v == 1 for row in coverage for v in row)

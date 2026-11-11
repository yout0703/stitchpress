from dataclasses import replace

import pytest

from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import safe_filename, symbol_key
from stitchpress.core.layout import Fabric, page_coordinates, page_plan, physical_layout
from stitchpress.core.models import (
    PatchCell,
    ReviewRecord,
    apply_patch,
    validate_catalog_membership,
)
from stitchpress.core.resources import assign_symbols, demo_library


def test_s01_integer_counts_and_blank(minimal):
    assert minimal.counts == (3, 2)
    assert minimal.width * minimal.height - sum(minimal.counts) == 1
    with pytest.raises(StitchPressError, match="索引"):
        replace(minimal, labels=((0, -2, 1), (1, 0, 0)))
    with pytest.raises(StitchPressError, match="全针"):
        replace(minimal, stitch_type="backstitch")


def test_unknown_thread_is_not_resolved_by_matching_rgb(minimal):
    validate_catalog_membership(minimal, minimal.palette)
    catalog = (minimal.palette[0], replace(minimal.palette[1], code="different-same-RGB"))
    with pytest.raises(StitchPressError, match="尚未"):
        validate_catalog_membership(minimal, catalog)
    assert minimal.palette[1].code == "3865"


def test_s02_customer_size_and_global_phase():
    fabric = Fabric()
    physical = physical_layout(240, 320, fabric)
    assert physical.canvas_px == (6413, 8124)
    assert physical.width_cm == "43.440"
    assert physical.height_cm == "57.920"
    assert physical.scale100_cm == ("18.100", "18.100")
    assert set(b - a for a, b in zip(physical.xs, physical.xs[1:], strict=False)) == {21, 22}
    plan = page_plan(240, 320, 30, fabric)
    assert plan.capacity == (100, 110)
    assert len(plan.pages) == 9
    visited = set()
    for page in plan.pages:
        x0, y0, x1, y1 = page.core
        current = {(x, y) for y in range(y0, y1) for x in range(x0, x1)}
        assert not visited & current
        visited |= current
        xs, ys = page_coordinates(physical, page, plan)
        assert tuple(b - a for a, b in zip(xs, xs[1:], strict=False)) == tuple(
            b - a for a, b in zip(physical.xs[x0:x1], physical.xs[x0 + 1 : x1 + 1], strict=True)
        )
    assert len(visited) == 240 * 320
    assert physical_layout(160, 160, fabric).canvas_cm == ("39.820", "39.820")
    assert len(page_plan(160, 160, 30, fabric).pages) == 4
    assert Fabric.theoretical("14").sx != fabric.sx


def test_s04_more_legend_rows_reduce_grid_capacity():
    assert page_plan(240, 60, 66, Fabric()).capacity[1] < 110


def test_overlap_does_not_exceed_available_chart_area_or_double_count():
    plan = page_plan(240, 320, 30, Fabric(), overlap=5)
    layout = physical_layout(240, 320, Fabric())
    assert plan.capacity == (95, 105)
    assert sum((p.core[2] - p.core[0]) * (p.core[3] - p.core[1]) for p in plan.pages) == 240 * 320
    for page in plan.pages:
        xs, ys = page_coordinates(layout, page, plan)
        assert xs[-1] < plan.paper_px[0] - 100
        assert ys[-1] < plan.legend_top - 50


@pytest.mark.parametrize(
    "field,value",
    [("sx", "0"), ("sy", "NaN"), ("left", "-1"), ("dpi", 0), ("nominal_ct", "Infinity")],
)
def test_reject_invalid_physical_values(field, value):
    with pytest.raises(StitchPressError):
        Fabric(**{field: value})


def test_s05_identity_mapping_and_capacity(minimal):
    assert symbol_key(24) == "int:24"
    assert symbol_key("0.1") == "str:0.1"
    assert symbol_key("24") != symbol_key(24)
    with pytest.raises(ValueError):
        symbol_key(0.1)
    same_rgb = replace(minimal.palette[1], rgb=minimal.palette[0].rgb)
    design = replace(minimal, palette=(minimal.palette[0], same_rgb))
    assert len(design.palette) == 2
    library = demo_library(2)
    existing = {design.palette[1].key: library.symbols[0].key}
    mapping = assign_symbols(design, library, existing)
    assert mapping[design.palette[1].key] == library.symbols[0].key
    assert len(set(mapping.values())) == 2
    with pytest.raises(StitchPressError, match="容量"):
        assign_symbols(design, demo_library(1))


def test_s08_portable_filenames():
    for name in ("CON", "aux.txt", "AK0026", "中文 / 图片.", "name ", "ＮＡＭＥ"):
        result = safe_filename(name)
        assert not any(c in result for c in '<>:"/\\|?*')
        assert result == result.rstrip(" .")
    assert safe_filename("A") != safe_filename("a")
    assert safe_filename("é") != safe_filename("e\u0301")


def test_s14_patch_outside_unchanged_and_undo_parent(minimal):
    result = apply_patch(
        minimal,
        base_sha=minimal.content_sha,
        cells=(PatchCell(1, 0, -1, 1),),
        allowed=frozenset({(1, 0)}),
    )
    assert minimal.labels[0][1] == -1
    assert result.labels[0][1] == 1
    assert result.parent_revision_id == minimal.revision_id
    assert result.origin == "patched"
    for y, row in enumerate(minimal.labels):
        for x, val in enumerate(row):
            if (x, y) != (1, 0):
                assert result.labels[y][x] == val
                assert result.palette == minimal.palette


@pytest.mark.parametrize(
    "cell,allowed,protected",
    [
        (PatchCell(0, 0, 1, 1), {(0, 0)}, set()),
        (PatchCell(0, 0, 0, 9), {(0, 0)}, set()),
        (PatchCell(0, 0, 0, 1), {(1, 0)}, set()),
        (PatchCell(0, 0, 0, 1), {(0, 0)}, {(0, 0)}),
    ],
)
def test_s15_invalid_patch_rejected(minimal, cell, allowed, protected):
    with pytest.raises(StitchPressError):
        apply_patch(
            minimal,
            base_sha=minimal.content_sha,
            cells=(cell,),
            allowed=frozenset(allowed),
            protected=frozenset(protected),
        )


def test_review_does_not_transfer_to_changed_master(minimal):
    review = ReviewRecord(
        minimal.content_sha,
        "test",
        True,
        ((0, 0, 1, 1), (1, 0, 3, 2)),
        True,
        True,
        "synthetic test only",
    )
    review.validate(minimal)
    changed = replace(minimal, labels=((0, 0, 1), (1, 0, 0)))
    with pytest.raises(StitchPressError):
        review.validate(changed)

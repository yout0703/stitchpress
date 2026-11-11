from dataclasses import replace

import pytest

from stitchpress.core.errors import StitchPressError
from stitchpress.core.models import PatchCell, apply_patch
from stitchpress.core.proposals import apply_migration, compare_designs, propose_migration


def patched(design):
    return apply_patch(
        design,
        base_sha=design.content_sha,
        cells=(PatchCell(0, 0, design.labels[0][0], 1),),
        allowed=frozenset({(0, 0)}),
    )


def test_reordered_palette_migration_uses_identity_and_preserves_outside(minimal):
    old = replace(minimal, width=2, labels=((0, 0), (0, 1)))
    changed = patched(old)
    target = replace(old, palette=tuple(reversed(old.palette)), labels=((1, 1), (1, 0)))
    proposal = propose_migration(old, changed, target)
    assert proposal["entries"][0]["status"] == "ready"
    result, record = apply_migration(target, proposal, [(0, 0)])
    assert result.labels[0][0] == 0
    assert compare_designs(target, result)["changed_cells"] == 1
    assert record["external_semantics"] == "unchanged"
    assert target.labels == ((1, 1), (1, 0))


def test_s19_conflicts_original_value_grid_catalog_and_protection(minimal):
    old = replace(minimal, width=2, labels=((0, 0), (0, 1)))
    changed = patched(old)
    for target, protected in (
        (replace(old, labels=((-1, 0), (0, 1))), frozenset()),
        (replace(old, width=1, labels=((0,), (1,))), frozenset()),
        (old, frozenset({(0, 0)})),
        (
            replace(
                old, palette=(old.palette[0], replace(old.palette[1], catalog_revision="different"))
            ),
            frozenset(),
        ),
    ):
        proposal = propose_migration(old, changed, target, protected=protected)
        assert proposal["entries"][0]["status"] == "conflict"
        with pytest.raises(StitchPressError):
            apply_migration(target, proposal, [(0, 0)], protected=protected)


def test_migration_keeps_already_applied_and_stale_or_tampered_proposals_are_rejected(minimal):
    old = replace(minimal, width=2, labels=((0, 0), (0, 1)))
    changed = patched(old)
    assert propose_migration(old, changed, changed)["entries"][0]["status"] == "already"
    proposal = propose_migration(old, changed, old)
    with pytest.raises(StitchPressError, match="目标"):
        apply_migration(changed, proposal, [(0, 0)])
    proposal["entries"][0]["after"] = None
    with pytest.raises(StitchPressError, match="已改变"):
        apply_migration(old, proposal, [(0, 0)])


def test_local_requantization_appends_colors_and_protects_outside(minimal):
    import io

    from PIL import Image

    from stitchpress.core.conversion import ConversionOptions, convert_region

    base = replace(minimal, width=2, labels=((0, 0), (0, 1)))
    extra = replace(base.palette[1], code="002", rgb=(200, 30, 50))
    image = Image.new("RGB", (2, 2), extra.rgb)
    raw = io.BytesIO()
    image.save(raw, "PNG")
    result, record = convert_region(
        raw.getvalue(),
        base.palette + (extra,),
        ConversionOptions(2, 2, 3),
        base,
        (0, 0, 1, 1),
        capacity=3,
    )
    assert result.palette[:2] == base.palette
    assert result.labels == ((2, 0), (0, 1))
    assert record["external_semantics"] == "unchanged"
    with pytest.raises(StitchPressError, match="保护区"):
        convert_region(
            raw.getvalue(),
            base.palette + (extra,),
            ConversionOptions(2, 2, 3),
            base,
            (0, 0, 1, 1),
            capacity=3,
            protected=frozenset({(0, 0)}),
        )
    with pytest.raises(StitchPressError, match="容量"):
        convert_region(
            raw.getvalue(),
            base.palette + (extra,),
            ConversionOptions(2, 2, 3),
            base,
            (0, 0, 1, 1),
            capacity=2,
        )


def test_image_edit_candidate_changes_outside_but_composition_preserves_it_exactly():
    import io

    from PIL import Image

    from stitchpress.core.conversion import compose_local_image

    def png(color, size=(4, 4)):
        buffer = io.BytesIO()
        Image.new("RGB", size, color).save(buffer, "PNG")
        return buffer.getvalue()

    merged, record = compose_local_image(png("blue"), png("red"), (1, 1, 2, 2), (4, 4))
    with Image.open(io.BytesIO(merged)) as image:
        assert image.getpixel((1, 1))[:3] == (255, 0, 0)
        assert image.getpixel((0, 0))[:3] == (0, 0, 255)
        assert image.getpixel((2, 1))[:3] == (0, 0, 255)
    assert record["outside_pixels"] == "exact-normalized-original"
    with pytest.raises(StitchPressError, match="不一致"):
        compose_local_image(png("blue"), png("red", (5, 4)), (1, 1, 2, 2), (4, 4))


def test_local_conversion_subject_mask_never_turns_transparency_into_blank(minimal):
    import io

    import numpy as np
    from PIL import Image

    from stitchpress.core.conversion import ConversionOptions, convert_region

    raw = io.BytesIO()
    Image.new("RGBA", (3, 2), (0, 0, 0, 0)).save(raw, "PNG")
    subject = np.zeros((2, 3), dtype=bool)
    subject[0, 0] = True
    with pytest.raises(StitchPressError, match="主体蒙版与透明"):
        convert_region(
            raw.getvalue(),
            minimal.palette,
            ConversionOptions(3, 2, 2),
            minimal,
            (0, 0, 1, 1),
            subject_mask=subject,
        )

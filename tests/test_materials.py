from dataclasses import asdict, replace
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from stitchpress.core.documents import write_counts
from stitchpress.core.errors import StitchPressError
from stitchpress.core.materials import MaterialRule, material_rows


def rule():
    return MaterialRule(
        name="Self-authored formula test, not customer rule",
        nominal_ct="14",
        strands=2,
        stitches_per_work_metre="100",
        loss_percent="10",
        pack_work_metres="0.5",
        source_note="Unit-test only",
        confirmed=True,
    )


def test_each_color_rounds_up_before_summing_and_strands_are_not_applied_twice(minimal):
    rows = material_rows(minimal, rule())
    assert all(row["packages"] == 1 for row in rows)
    assert sum(Decimal(row["supplied_work_metres"]) for row in rows) == Decimal("1")
    assert all(
        Decimal(row["raw_single_strand_metres"]) == Decimal(row["supplied_work_metres"]) * 2
        for row in rows
    )
    assert Decimal(rows[0]["base_work_metres"]) == Decimal(rows[0]["stitches"]) / 100
    with pytest.raises(StitchPressError, match="未经人工确认"):
        replace(rule(), confirmed=False)
    with pytest.raises(StitchPressError):
        replace(rule(), stitches_per_work_metre="0")


def test_multict_material_workbook_retains_text_codes_counts_rules_and_decimal_results(
    tmp_path, minimal
):
    target = tmp_path / "materials.xlsx"
    rules = [asdict(rule()), asdict(replace(rule(), nominal_ct="11", stitches_per_work_metre="75"))]
    receipt = write_counts(minimal, "AK0026", target, rules)
    assert len(receipt["material_rules"]) == 2
    workbook = load_workbook(target, data_only=True)
    assert workbook.sheetnames == ["Stitch Counts", "Material 01", "Material 02"]
    sheet = workbook["Material 01"]
    assert sheet["B6"].data_type == "s"
    assert sheet["F6"].data_type == "n" and sheet["F6"].value == 1
    assert sheet["H6"].value == "1.0"

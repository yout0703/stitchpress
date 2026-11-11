"""Semantic chart differences and reviewable patch migration, independent of palette order."""

from dataclasses import asdict

from .errors import StitchPressError
from .identity import digest
from .models import Design, PatchCell, ThreadColor, apply_patch


def cell_value(design, x, y):
    index = design.labels[y][x]
    return asdict(design.palette[index]) if index >= 0 else None


def semantic(value):
    if value is None:
        return None
    color = ThreadColor.from_dict(value)
    return color.key, color.rgb


def changed_positions(before: Design, after: Design):
    if (before.width, before.height) != (after.width, after.height):
        raise StitchPressError("网格尺寸不同，不能缩放坐标伪装为局部修订")
    old = [semantic(asdict(c)) for c in before.palette] + [None]
    new = [semantic(asdict(c)) for c in after.palette] + [None]
    for y in range(before.height):
        for x in range(before.width):
            if old[before.labels[y][x]] != new[after.labels[y][x]]:
                yield x, y


def design_changes(before, after):
    result = []
    for index, (x, y) in enumerate(changed_positions(before, after)):
        if index >= 100_000:
            raise StitchPressError("单个局部提议最多10万处变化，请拆分明确选区；不截断补丁")
        result.append(
            {"x": x, "y": y, "before": cell_value(before, x, y), "after": cell_value(after, x, y)}
        )
    return result


def compare_designs(before, after):
    same_grid = (before.width, before.height) == (after.width, after.height)
    changed = 0
    region = None
    if same_grid:
        for x, y in changed_positions(before, after):
            changed += 1
            region = (
                [x, y, x + 1, y + 1]
                if region is None
                else [
                    min(region[0], x),
                    min(region[1], y),
                    max(region[2], x + 1),
                    max(region[3], y + 1),
                ]
            )
    result = {
        "before_sha": before.content_sha,
        "after_sha": after.content_sha,
        "same_grid": same_grid,
        "changed_cells": changed if same_grid else None,
        "region": region,
        "stitch_delta": sum(after.counts) - sum(before.counts),
        "colors_before": sum(n > 0 for n in before.counts),
        "colors_after": sum(n > 0 for n in after.counts),
        "grid_before": [before.width, before.height],
        "grid_after": [after.width, after.height],
    }
    return result


def propose_migration(old_base, old_patched, new_base, *, protected=frozenset(), catalog=None):
    changes = design_changes(old_base, old_patched)
    known = {semantic(asdict(c)) for c in (catalog if catalog is not None else new_base.palette)}
    same_grid = (old_base.width, old_base.height) == (new_base.width, new_base.height)
    entries = []
    for change in changes:
        x, y = change["x"], change["y"]
        status, reason = "ready", "原值与线材身份匹配"
        current = None
        if not same_grid:
            status, reason = "conflict", "针格宽高改变；不自动缩放旧修订坐标"
        else:
            current = cell_value(new_base, x, y)
            if (x, y) in protected:
                status, reason = "conflict", "触及当前保护区或负空间"
            elif semantic(current) == semantic(change["after"]):
                status, reason = "already", "新候选已包含此结果，无需重复应用"
            elif semantic(current) != semantic(change["before"]):
                status, reason = "conflict", "新候选原值不匹配，不能覆盖新的细节"
            elif change["after"] is not None and semantic(change["after"]) not in known:
                status, reason = "conflict", "目标线色不属于当前目录修订/颜色语义"
        entries.append({**change, "candidate_before": current, "status": status, "reason": reason})
    record = {
        "schema_version": 1,
        "kind": "semantic-patch-migration",
        "old_base_sha": old_base.content_sha,
        "old_patched_sha": old_patched.content_sha,
        "target_sha": new_base.content_sha,
        "grid": [new_base.width, new_base.height],
        "same_grid": same_grid,
        "entries": entries,
        "review_required": True,
    }
    record["proposal_sha"] = digest(record)
    return record


def apply_migration(design, proposal, selected, *, capacity=66, protected=frozenset()):
    if proposal.get("proposal_sha") != digest(
        {k: v for k, v in proposal.items() if k != "proposal_sha"}
    ):
        raise StitchPressError("迁移提议内容已改变")
    if proposal.get("target_sha") != design.content_sha or not proposal.get("same_grid"):
        raise StitchPressError("迁移目标已改变或网格不同；不能直接重放")
    if not selected or len(set(selected)) != len(selected):
        raise StitchPressError("请明确选择不重复的可迁移针格")
    entries = {(c["x"], c["y"]): c for c in proposal["entries"]}
    appended = []
    palette = list(design.palette)
    indices = {c.key: i for i, c in enumerate(palette)}
    cells = []
    for xy in selected:
        entry = entries.get(tuple(xy))
        if not entry or entry["status"] != "ready" or tuple(xy) in protected:
            raise StitchPressError("所选修订有冲突或触及保护区，不能采用")
        x, y = xy
        if semantic(cell_value(design, x, y)) != semantic(entry["before"]):
            raise StitchPressError("迁移原值已改变")
        value = entry["after"]
        after = -1
        if value is not None:
            color = ThreadColor.from_dict(value)
            if color.key not in indices:
                indices[color.key] = len(palette)
                palette.append(color)
                appended.append(color)
            after = indices[color.key]
            if palette[after].rgb != color.rgb:
                raise StitchPressError("相同目录身份的颜色不一致")
        cells.append(PatchCell(x, y, design.labels[y][x], after))
    result = apply_patch(
        design,
        base_sha=design.content_sha,
        cells=tuple(cells),
        allowed=frozenset(selected),
        protected=protected,
        append_colors=tuple(appended),
        capacity=capacity,
    )
    # This comparison includes directory identity and RGB, not just numeric labels.
    changed = {(c["x"], c["y"]) for c in design_changes(design, result)}
    if changed != set(selected):
        raise StitchPressError("迁移变更范围与确认清单不同")
    return result, {
        "base_sha": design.content_sha,
        "result_sha": result.content_sha,
        "cells": [asdict(c) for c in cells],
        "proposal_sha": proposal["proposal_sha"],
        "selected": [list(xy) for xy in selected],
        "omitted": len(proposal["entries"]) - len(selected),
        "external_semantics": "unchanged",
    }

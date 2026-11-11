"""Resolve durable chart history and compare candidates without writing an active project."""

from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import sha256
from stitchpress.core.masks import annotation_masks
from stitchpress.core.models import Design
from stitchpress.core.proposals import compare_designs, propose_migration
from stitchpress.core.resources import load_catalog


def design_index(project):
    records = {}
    for key, value in project.entities.items():
        if key == "design":
            design = Design.from_dict(value)
        elif key.startswith(("revision:", "candidate:")) and isinstance(value.get("design"), dict):
            design = Design.from_dict(value["design"])
        else:
            continue
        records[design.revision_id] = design
    return records


def patch_root(project, current):
    index = design_index(project)
    root = current
    chain = []
    seen = set()
    while root.origin == "patched":
        if root.revision_id in seen:
            raise StitchPressError("修订来源链出现循环")
        seen.add(root.revision_id)
        parent = index.get(root.parent_revision_id)
        if parent is None:
            raise StitchPressError("局部修订缺少原基准母版，不能猜测迁移")
        chain.append(root.revision_id)
        root = parent
    return root, list(reversed(chain))


def protected_cells(project, design):
    source = project.entities.get("source")
    source_sha = sha256(project.assets[source["asset"]]) if source else None
    record = project.entities.get("annotations")
    if record and (
        record["grid"] != [design.width, design.height] or record.get("source_sha") != source_sha
    ):
        raise StitchPressError("保护区/负空间的输入语境已改变，请核对后清除或重新标注")
    masks = annotation_masks(record, design.width, design.height, source_sha)
    return frozenset(
        (int(x), int(y))
        for y, x in zip(*(masks["protected"] | masks["blank"]).nonzero(), strict=True)
    )


def compare_candidate(project, key):
    if not key.startswith("candidate:") or key not in project.entities:
        raise StitchPressError("针格候选不存在")
    current = Design.from_dict(project.entities["design"])
    candidate = Design.from_dict(project.entities[key]["design"])
    result = {
        "candidate_key": key,
        "comparison": compare_designs(current, candidate),
        "migration": None,
        "patch_chain": [],
    }
    if current.origin == "patched" and candidate.origin == "converted":
        root, chain = patch_root(project, current)
        catalog_record = project.entities.get("catalog")
        catalog = (
            load_catalog(
                project.assets[catalog_record["asset"]], catalog_record.get("brand", "DMC")
            )
            if catalog_record
            else candidate.palette
        )
        result["migration"] = propose_migration(
            root,
            current,
            candidate,
            protected=protected_cells(project, candidate)
            if (current.width, current.height) == (candidate.width, candidate.height)
            else frozenset(),
            catalog=catalog,
        )
        result["patch_chain"] = chain
    return result

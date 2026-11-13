"""Transactional reference-workflow actions, shared by native UI and local checks."""

import copy
import json
import re
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_STORED

from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import digest, sha256
from stitchpress.core.importers import import_chart
from stitchpress.core.resources import (
    SymbolLibrary,
    assign_symbols,
    library_assets,
    library_from_assets,
    normalize_symbol_image,
    parse_symbol,
)


def import_pattern(project, path):
    path = Path(path)
    raw = path.read_bytes()
    format = path.suffix.lower().removeprefix(".")
    design = import_chart(raw, format)
    library = library_from_assets(project.entities["library"], project.assets)
    used = sum(count > 0 for count in design.counts)
    if used > len(library.symbols):
        raise StitchPressError(
            f"图纸有 {used} 种线色，当前符号库只有 {len(library.symbols)} 个符号。"
            f"请先到“符号与配线 → 选择符号库”选择至少 {used} 个符号的库，再导入此图纸。"
        )
    mapping = assign_symbols(design, library, strategy="color-nearest-v1")
    updated = copy.deepcopy(project)
    for key in (
        "design",
        "mapping",
        "manual_mapping",
        "source",
        "annotations",
        "input_profile",
        "pattern_input",
        "freeze",
        "post_review",
        "active_conversion",
        "active_candidate",
    ):
        if old := updated.entities.pop(key, None):
            updated.entities[f"{key}:{uuid4().hex}"] = old
    asset = f"sources/{sha256(raw)}.{format}"
    updated.assets[asset] = raw
    updated.entities.update(
        design=design.to_dict(),
        mapping=mapping,
        pattern_input=dict(asset=asset, format=format, sha256=sha256(raw), filename=path.name),
        mapping_policy=dict(algorithm="color-nearest-v1", manual_first=True),
    )
    if project.entities.get("metadata", {}).get("code") == "DEMO001":
        report = json.loads(dict(design.provenance)["import_report"])
        updated.entities["metadata"] = {
            **updated.entities.get("metadata", {}),
            "title": report.get("title", "").strip() or path.stem,
            "code": path.stem,
        }
    updated.encode(compression=ZIP_STORED)
    return updated


def bind_symbol(project, color_key, symbol_key):
    library = library_from_assets(project.entities["library"], project.assets)
    from stitchpress.core.models import Design

    design = Design.from_dict(project.entities["design"])
    before = assign_symbols(design, library, project.entities.get("mapping"))
    if color_key not in before or symbol_key not in library.by_key:
        raise StitchPressError("所选线色或符号不属于当前作品")
    mapping = dict(before)
    other = next((key for key, value in mapping.items() if value == symbol_key), None)
    if other:
        mapping[other] = mapping[color_key]
    mapping[color_key] = symbol_key
    assign_symbols(design, library, mapping)
    manual = dict(project.entities.get("manual_mapping", {}).get("bindings", {}))
    for key in {color_key, other} - {None}:
        manual[key] = mapping[key]
    project.entities[f"mapping:{uuid4().hex}"] = dict(
        before=before,
        after=mapping,
        manual=True,
        library_revision=library.revision,
        design_sha=design.content_sha,
    )
    project.entities["mapping"] = mapping
    project.entities["manual_mapping"] = dict(bindings=manual)
    return mapping


def import_symbol_folder(project, folder):
    current = library_from_assets(project.entities["library"], project.assets)

    def natural(path):
        return tuple(
            (0, int(part)) if part.isdigit() else (1, part.casefold())
            for part in re.split(r"(\d+)", path.name)
        )

    paths = sorted(
        (
            p
            for p in Path(folder).iterdir()
            if p.is_file() and p.suffix.lower() in {".tif", ".tiff", ".png", ".bmp"}
        ),
        key=natural,
    )
    if not paths or len(paths) > 512:
        raise StitchPressError("请选择含1–512个TIF/PNG/BMP图块的文件夹")
    symbols, originals, receipts = [], {}, []
    for path in paths:
        try:
            raw = path.read_bytes()
            normalized, checks = normalize_symbol_image(raw, current.icc)
            identity = int(path.stem) if re.fullmatch(r"0|[1-9]\d*", path.stem) else path.stem
            symbol = parse_symbol(normalized, identity, path.name)
        except (OSError, StitchPressError) as exc:
            raise StitchPressError(f"符号文件“{path.name}”导入失败：{exc}") from exc
        symbols.append(symbol)
        asset = f"resources/symbol-originals/{sha256(raw)}{path.suffix.lower()}"
        originals[asset] = raw
        receipts.append(dict(asset=asset, filename=path.name, **checks))
    library = SymbolLibrary(
        digest([(s.key, s.filename, s.pixel_sha) for s in symbols]),
        tuple(symbols),
        current.icc,
        "customer-imported" if current.icc else "self-authored",
    )
    from stitchpress.core.models import Design

    mapping = assign_symbols(
        Design.from_dict(project.entities["design"]), library, strategy="color-nearest-v1"
    )
    record, assets = library_assets(library)
    return dict(record=record, assets={**assets, **originals}, mapping=mapping, receipts=receipts)

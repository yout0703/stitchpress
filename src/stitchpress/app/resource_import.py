"""Explicit local import of customer's existing resources, never installation defaults."""

import json
from pathlib import Path

from PIL import ImageCms

from stitchpress.core.errors import ResourceError
from stitchpress.core.identity import sha256
from stitchpress.core.resources import SymbolLibrary, library_assets, load_catalog, parse_symbol
from stitchpress.platform.files import atomic_write

from .store import Project


def import_factory(
    profile_path: Path, manifest_path: Path, catalog_path: Path, target: Path
) -> dict:
    profile_data, manifest_data, catalog_data = (
        p.read_bytes() for p in (profile_path, manifest_path, catalog_path)
    )
    manifest = json.loads(manifest_data)
    colors = load_catalog(catalog_data)

    def checked(record):
        path = Path(record["path"])
        if not path.is_absolute():
            path = manifest_path.parent / path
        data = path.read_bytes()
        if sha256(data) != record["sha256"]:
            raise ResourceError(f"导入资源SHA不符：{path.name}")
        return data

    icc = checked(manifest["shared_icc"])
    import io

    profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
    if profile.profile.xcolor_space.strip() != "CMYK":
        raise ResourceError("生产ICC必须为CMYK设备配置")
    symbols = tuple(
        parse_symbol(checked(e["normalized"]), e["id"], Path(e["normalized"]["path"]).name)
        for e in manifest["entries"]
    )
    library = SymbolLibrary(sha256(manifest_data), symbols, icc, "customer-imported")
    record, assets = library_assets(library)
    project = Project.create("本地客户资源库")
    project.entities["library"] = record
    project.entities["catalog"] = {
        "asset": "resources/catalog.json",
        "sha256": sha256(catalog_data),
    }
    project.entities["source_profile"] = {
        "asset": "resources/profile.json",
        "sha256": sha256(profile_data),
    }
    # Profile JSON has historical absolute paths. Keep private provenance as an asset,
    # but all runtime symbol/catalog/ICC dependencies above are content-addressed members.
    project.assets.update(assets)
    project.assets["resources/catalog.json"] = catalog_data
    project.assets["resources/profile.json"] = profile_data
    atomic_write(target, project.encode())
    return {
        "profile_sha": sha256(profile_data),
        "symbol_manifest_sha": sha256(manifest_data),
        "catalog_sha": sha256(catalog_data),
        "icc_sha": sha256(icc),
        "symbols": len(symbols),
        "colors": len(colors),
        "target": str(target),
    }

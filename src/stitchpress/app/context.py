"""Resolve an exact, self-contained export snapshot without editing its records."""

from stitchpress.core.configuration import ReferenceSettings, ShowcaseSettings
from stitchpress.core.identity import digest, sha256
from stitchpress.core.layout import Fabric
from stitchpress.core.models import Design, ReviewRecord
from stitchpress.core.resources import assign_symbols, library_from_assets, load_catalog


def review_context(project):
    source = project.entities.get("source")
    profile = project.entities.get("input_profile")
    return {
        "source_sha": sha256(project.assets[source["asset"]]) if source else None,
        "brief_sha": digest(project.entities["brief"]) if project.entities.get("brief") else None,
        "annotations_sha": digest(project.entities["annotations"])
        if project.entities.get("annotations")
        else None,
        "input_icc_sha": sha256(project.assets[profile["asset"]]) if profile else None,
    }


def export_context(project):
    design = Design.from_dict(project.entities["design"])
    library = library_from_assets(project.entities["library"], project.assets)
    record = project.entities.get("freeze")
    review = (
        ReviewRecord(
            **{**record, "detail_regions": tuple(tuple(r) for r in record["detail_regions"])}
        )
        if record
        else None
    )
    catalog = project.entities.get("catalog")
    font = project.entities.get("output_font")
    return {
        "design": design,
        "library": library,
        "mapping": project.entities.get("mapping") or assign_symbols(design, library),
        "fabric": Fabric(**project.entities["fabric"]),
        "reference": ReferenceSettings(**project.entities.get("reference", {})),
        "showcase_options": ShowcaseSettings(**project.entities.get("showcase", {})),
        "code": project.entities["metadata"]["code"],
        "title": project.entities["metadata"]["title"],
        "review": review,
        "review_context": review_context(project),
        "catalog": load_catalog(project.assets[catalog["asset"]], catalog.get("brand", "DMC"))
        if catalog
        else (),
        "font_data": project.assets[font["asset"]] if font else None,
        "text_layers": project.entities.get("text_layers", {}),
        "material_rules": project.entities.get("material_rules", {}).get("rules", []),
    }

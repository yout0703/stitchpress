"""Reproducible local alpha evidence, with explicit pending external acceptance."""

import argparse
import json
import platform
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from stitchpress.app.export import export_package, zip_package
from stitchpress.app.fixtures import sample_design
from stitchpress.app.store import Project
from stitchpress.core.identity import sha256
from stitchpress.core.layout import Fabric
from stitchpress.core.render import screen_rgb
from stitchpress.core.resources import (
    assign_symbols,
    bundled_font,
    library_from_assets,
    load_catalog,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("resources", type=Path)
    parser.add_argument("--output", type=Path, default=Path("reports/local/240x320-v01"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    project = Project.decode(args.resources.read_bytes())
    library = library_from_assets(project.entities["library"], project.assets)
    catalog = load_catalog(project.assets[project.entities["catalog"]["asset"]])
    # Self-authored geometry uses actual customer catalog IDs, not customer art.
    design = replace(sample_design(240, 320, 30), palette=tuple(catalog[:30]))
    if (args.output / "package").exists():
        journals = list((args.output / "work").glob("job-*.json"))
        if len(journals) != 1:
            raise RuntimeError("Ambiguous existing run; choose a new output version")
        report = json.loads(journals[0].read_bytes())
        if (
            report["status"] != "completed"
            or report["snapshot"]["design_sha"] != design.content_sha
        ):
            raise RuntimeError("Existing outputs do not match the requested input")
        for item in report["artifacts"]:
            if sha256((args.output / "package" / item["name"]).read_bytes()) != item["sha256"]:
                raise RuntimeError("Existing output hash changed")
    else:
        report = export_package(
            design=design,
            library=library,
            mapping=assign_symbols(design, library),
            fabric=Fabric(),
            code="TEST240",
            title="Self-authored validation",
            destination=args.output / "package",
            work=args.output / "work",
            review=None,
            demo=True,
        )
    pdf = next((args.output / "package").glob("*.pdf"))
    document = pdfium.PdfDocument(pdf)
    contacts = Image.new("RGB", (900, 3 * 470), "#e6e9e3")
    for i in range(len(document)):
        page = document[i]
        bitmap = page.render(scale=900 / page.get_height(), rev_byteorder=True)
        image = bitmap.to_pil().copy()
        image.save(args.output / f"PDF实际渲染-{i + 1:02}.png")
        image.thumbnail((290, 440))
        contacts.paste(image, ((i % 3) * 300, (i // 3) * 470))
        bitmap.close()
        page.close()
    report["pdf_actual_render"] = {
        "tool": "PDFium",
        "version": version("pypdfium2"),
        "images": len(document),
        "exit_code": 0,
    }
    document.close()
    contacts.save(args.output / "PDF九页接触表.png")
    production = next((args.output / "package" / "Production").glob("*.tif"))
    with Image.open(production) as im:
        rgb = screen_rgb(im, library.icc)
        rgb.thumbnail((1000, 1400))
        rgb.save(args.output / "生产图预览.png")
    zip_path = args.output / "TEST240-demo.zip"
    report["zip"] = (
        {"sha256": sha256(zip_path.read_bytes())}
        if zip_path.exists()
        else zip_package(args.output / "package", zip_path)
    )
    report["environment"] = {
        "os": platform.platform(),
        "python": platform.python_version(),
        "versions": {
            name: version(name) for name in ("PySide6", "Pillow", "numpy", "pypdf", "openpyxl")
        },
        "font_sha": sha256(bundled_font().read_bytes()),
    }
    report["acceptance"] = {
        "numeric": "passed-local-only",
        "visual": "pending",
        "windows": "pending-no-machine",
        "factory": "pending",
        "spreadsheet_application_render": "pending",
    }
    (args.output / "validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "files": report["files"],
                "elapsed_seconds": report["elapsed_seconds"],
                "report": str(args.output / "validation.json"),
                "pdf_render": report.get("pdf_actual_render"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

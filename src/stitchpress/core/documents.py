import io
import re
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from .errors import StitchPressError
from .materials import MaterialRule, material_rows
from .models import Design
from .resources import natural_color_key


def write_counts(design: Design, code: str, path: Path, material_rules=()) -> dict:
    wb = Workbook()
    ws = wb.active
    ws.title = "Stitch Counts"
    ws.append(["Customer Code", code])
    ws["B1"].data_type = "s"
    ws.append(["No.", "Brand", "DMC Code", "Color Name", "Stitches"])
    entries = sorted(
        [(color, n) for color, n in zip(design.palette, design.counts, strict=True) if n],
        key=lambda entry: natural_color_key(entry[0]),
    )
    for i, (color, n) in enumerate(entries, 1):
        ws.append([i, color.brand, color.code, color.name, n])
        ws.cell(i + 2, 3).data_type = "s"  # Also prevents formula injection via thread code.
        ws.cell(i + 2, 3).number_format = "@"
        for col in (2, 4):
            ws.cell(i + 2, col).data_type = "s"
    total_row = len(entries) + 3
    ws.cell(total_row, 4, "TOTAL")
    ws.cell(total_row, 5, f"=SUM(E3:E{total_row - 1})")
    for row in ws:
        for cell in row:
            cell.font = Font(name="Noto Sans", size=14, bold=cell.row in {1, 2, total_row})
            cell.alignment = Alignment(vertical="center")
        ws.row_dimensions[row[0].row].height = 26
    for cell in ws[2]:
        cell.fill = PatternFill("solid", fgColor="DCECE6")
    for col, width in zip("ABCDE", (10, 18, 20, 38, 18), strict=True):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "D3"
    ws.auto_filter.ref = f"A2:E{total_row - 1}"
    ws.print_area = f"A1:E{total_row}"
    ws.print_title_rows = "1:2"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    buffer = io.BytesIO()
    material_checks = []
    if len(material_rules) > 12:
        raise StitchPressError("单次配线表最多12套客户规则")
    for index, item in enumerate(material_rules, 1):
        rule = MaterialRule(**item)
        rows = material_rows(design, rule)
        sheet = wb.create_sheet(f"Material {index:02}")
        sheet.append(["Rule", rule.name, "CT", rule.nominal_ct, "Strands", rule.strands])
        sheet.append(
            [
                "Stitches / work metre",
                rule.stitches_per_work_metre,
                "Loss %",
                rule.loss_percent,
                "Work metres / pack",
                rule.pack_work_metres,
            ]
        )
        sheet.append(["Source", rule.source_note, "Revision", rule.revision])
        sheet.append(["Working metres refer to combined thread; raw metres count single strands."])
        sheet.append(
            [
                "Brand",
                "Code",
                "Stitches",
                "Base work m",
                "With loss work m",
                "Packages",
                "Supplied work m",
                "Raw single-strand m",
            ]
        )
        for row in rows:
            sheet.append(
                [
                    row["color"].brand,
                    row["color"].code,
                    row["stitches"],
                    row["base_work_metres"],
                    row["with_loss_work_metres"],
                    row["packages"],
                    row["supplied_work_metres"],
                    row["raw_single_strand_metres"],
                ]
            )
        for row in sheet:
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
                cell.font = Font(name="Calibri", size=14)
                cell.alignment = Alignment(vertical="center", wrap_text=True)
            sheet.row_dimensions[row[0].row].height = 32
        for column in "ABCDEFGH":
            sheet.column_dimensions[column].width = 24
        sheet.freeze_panes = "A6"
        material_checks.append(
            {
                "sheet": sheet.title,
                "rule_sha": rule.revision,
                "rows": len(rows),
                "packages": sum(row["packages"] for row in rows),
            }
        )
    wb.save(buffer)
    # openpyxl does not calculate. Our evaluator supports ONLY the SUM we own,
    # computes from serialized integer cells, and writes its OOXML cached value.
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(io.BytesIO(buffer.getvalue())) as source:
        xml = ET.fromstring(source.read("xl/worksheets/sheet1.xml"))
        values = {c.attrib["r"]: c for c in xml.findall(".//s:c", ns)}
        formula = values[f"E{total_row}"].find("s:f", ns)
        if formula is None or not re.fullmatch(r"SUM\(E3:E\d+\)", formula.text or ""):
            raise StitchPressError("不支持的配线公式，不能伪造缓存值")
        numbers = [int(values[f"E{i}"].find("s:v", ns).text) for i in range(3, total_row)]
        computed = sum(numbers)
        values[f"E{total_row}"].find("s:v", ns).text = str(computed)
        with ZipFile(path, "w", ZIP_DEFLATED) as target:
            for info in source.infolist():
                target.writestr(
                    info,
                    ET.tostring(xml, encoding="utf-8", xml_declaration=True)
                    if info.filename == "xl/worksheets/sheet1.xml"
                    else source.read(info.filename),
                )
    formulas, results = load_workbook(path), load_workbook(path, data_only=True)
    actual = results.active
    if (
        actual.cell(total_row, 5).value != sum(design.counts)
        or formulas.active.cell(total_row, 5).value != f"=SUM(E3:E{total_row - 1})"
    ):
        raise StitchPressError("Excel总计公式/缓存核对失败")
    for row, (color, count) in enumerate(entries, 3):
        if (
            actual.cell(row, 3).value != color.code
            or actual.cell(row, 3).data_type != "s"
            or actual.cell(row, 5).value != count
            or actual.cell(row, 5).data_type != "n"
        ):
            raise StitchPressError("Excel逐色文本/整数针数核对失败")
    for check, item in zip(material_checks, material_rules, strict=True):
        sheet = results[check["sheet"]]
        for index, row in enumerate(material_rows(design, MaterialRule(**item)), 6):
            expected = [
                row["color"].brand,
                row["color"].code,
                row["stitches"],
                row["base_work_metres"],
                row["with_loss_work_metres"],
                row["packages"],
                row["supplied_work_metres"],
                row["raw_single_strand_metres"],
            ]
            if [sheet.cell(index, col).value for col in range(1, 9)] != expected or sheet.cell(
                index, 2
            ).data_type != "s":
                raise StitchPressError("Excel配线规则逐色读回不符")
    return {
        "colors": len(entries),
        "total": computed,
        "formula": f"SUM(E3:E{total_row - 1})",
        "cached_by": "stitchpress-owned-integer-SUM-only",
        "external_spreadsheet_render": "pending",
        "material_rules": material_checks,
    }


def write_pdf(
    page_paths: list[Path], path: Path, dpi: int, paper_cm: tuple[str, str], icc: bytes | None
) -> dict:
    from PIL import Image

    writer = PdfWriter()
    profile_ref = None
    if icc:
        profile = DecodedStreamObject()
        profile.set_data(icc)
        profile[NameObject("/N")] = NumberObject(4)
        profile[NameObject("/Alternate")] = NameObject("/DeviceCMYK")
        profile_ref = writer._add_object(profile.flate_encode())
        intent = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/OutputIntent"),
                NameObject("/S"): NameObject("/GTS_PDFX"),
                NameObject("/OutputConditionIdentifier"): TextStringObject(
                    "Imported production ICC; not PDF/X certification"
                ),
                NameObject("/DestOutputProfile"): profile_ref,
            }
        )
        writer._root_object[NameObject("/OutputIntents")] = ArrayObject(
            [writer._add_object(intent)]
        )
    pw, ph = (float(cm) / 2.54 * 72 for cm in paper_cm)
    for page_path in page_paths:
        with Image.open(page_path) as image:
            image.load()
            if image.mode != "CMYK":
                raise StitchPressError("PDF只接受当前已核验的CMYK查图页")
            stream = DecodedStreamObject()
            stream.set_data(image.tobytes())
            stream.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(image.width),
                    NameObject("/Height"): NumberObject(image.height),
                    NameObject("/BitsPerComponent"): NumberObject(8),
                    NameObject("/ColorSpace"): ArrayObject([NameObject("/ICCBased"), profile_ref])
                    if profile_ref
                    else NameObject("/DeviceCMYK"),
                }
            )
            ref = writer._add_object(stream.flate_encode())
            page = writer.add_blank_page(width=pw, height=ph)
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/XObject"): DictionaryObject({NameObject("/Chart"): ref})}
            )
            w, h = image.width / dpi * 72, image.height / dpi * 72
            content = DecodedStreamObject()
            content.set_data(
                (
                    f"q {w:.8f} 0 0 {h:.8f} {(pw - w) / 2:.8f} {(ph - h) / 2:.8f} cm /Chart Do Q"
                ).encode()
            )
            page[NameObject("/Contents")] = writer._add_object(content)
    writer.create_viewer_preferences()
    writer.viewer_preferences.print_scaling = NameObject("/None")
    writer.write(path)
    reader = PdfReader(path)
    for n, page in enumerate(reader.pages):
        obj = page["/Resources"]["/XObject"]["/Chart"].get_object()
        with Image.open(page_paths[n]) as expected:
            if obj.get_data() != expected.tobytes():
                raise StitchPressError("PDF内嵌CMYK像素不等于查图页")
        if (
            abs(float(page.mediabox.width) - pw) > 0.001
            or abs(float(page.mediabox.height) - ph) > 0.001
        ):
            raise StitchPressError("PDF物理纸张大小错误")
    return {
        "pages": len(reader.pages),
        "embedded_cmyk_pixels": "exact",
        "actual_renderer": "pending",
        "print_scaling": "None",
    }

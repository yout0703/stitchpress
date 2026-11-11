"""Reference-post settings and real image/workbook encoding, independent of Qt."""

import io
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as SheetImage
from openpyxl.styles import Alignment, Font, PatternFill
from PIL import Image, ImageCms

from .commerce import TARGETS
from .errors import StitchPressError
from .identity import digest
from .layout import decimal, physical_layout
from .render import screen_rgb
from .resources import natural_color_key

POST_TARGETS = (
    "original",
    "source",
    "production",
    "whole",
    "symbols",
    "card",
    "threads",
    "effect",
    "pages",
    "commerce",
    "settings",
)


@dataclass(frozen=True)
class ThreadPlan:
    ct: str
    stitches_per_metre: str
    source_note: str
    confirmed: bool = False

    def __post_init__(self):
        if not isinstance(self.ct, str) or not 1 <= decimal(self.ct) <= 100:
            raise StitchPressError("配线CT须1–100")
        if (
            not isinstance(self.stitches_per_metre, str)
            or not 0 < decimal(self.stitches_per_metre) <= 1_000_000
        ):
            raise StitchPressError("每米针数须为正值且不超过100万")
        if (
            not isinstance(self.source_note, str)
            or not self.source_note.strip()
            or len(self.source_note) > 2000
        ):
            raise StitchPressError("请填写配线系数来源，不能猜测客户口径")
        if type(self.confirmed) is not bool:
            raise StitchPressError("配线确认须为布尔值")

    @property
    def revision(self):
        return digest(asdict(self))


@dataclass(frozen=True)
class PostSettings:
    targets: tuple[str, ...] = tuple(t for t in POST_TARGETS if t not in {"threads", "commerce"})
    commerce_targets: tuple[str, ...] = TARGETS
    jpeg_quality: int = 95
    tiff_container: str = "classic"
    bigtiff_confirmed: bool = False
    effect_cell: int = 18
    thread_plans: tuple[dict, ...] = ()

    def __post_init__(self):
        if (
            not self.targets
            or len(set(self.targets)) != len(self.targets)
            or not set(self.targets) <= set(POST_TARGETS)
        ):
            raise StitchPressError("参考输出目标为空/重复或无效")
        if (
            not self.commerce_targets
            or len(set(self.commerce_targets)) != len(self.commerce_targets)
            or not set(self.commerce_targets) <= set(TARGETS)
        ):
            raise StitchPressError("电商目标为空/重复或无效")
        if type(self.jpeg_quality) is not int or not 80 <= self.jpeg_quality <= 100:
            raise StitchPressError("JPEG质量须80–100整数")
        if (
            self.tiff_container not in {"classic", "bigtiff"}
            or type(self.bigtiff_confirmed) is not bool
        ):
            raise StitchPressError("TIFF容器或确认无效")
        if self.tiff_container == "bigtiff" and not self.bigtiff_confirmed:
            raise StitchPressError("BigTIFF须先确认目标读取软件支持")
        if type(self.effect_cell) is not int or not 6 <= self.effect_cell <= 36:
            raise StitchPressError("点图每格像素须6–36整数")
        if len(self.thread_plans) > 12:
            raise StitchPressError("单次配线最多12种CT")
        plans = [ThreadPlan(**p) for p in self.thread_plans]
        if len({decimal(p.ct) for p in plans}) != len(plans):
            raise StitchPressError("同一CT不可重复配置配线系数")

    def to_dict(self):
        return asdict(self)


def save_rgb(image, path, *, dpi=300, quality=95, source_icc=None):
    """JPEG is explicitly lossy; record decoded error without claiming visual approval."""
    rgb = screen_rgb(image, source_icc)
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    suffix = Path(path).suffix.lower()
    options = dict(dpi=(dpi, dpi))
    if suffix in {".jpg", ".jpeg", ".png"}:
        options["icc_profile"] = icc
    if suffix in {".jpg", ".jpeg"}:
        options.update(quality=quality, subsampling=0)
    rgb.save(path, **options)
    with Image.open(path) as actual:
        actual.load()
        expected_format = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".bmp": "BMP"}[suffix]
        if actual.format != expected_format or actual.size != rgb.size or actual.mode != "RGB":
            raise StitchPressError("图像实际格式/尺寸/模式反读不符")
        if suffix in {".jpg", ".jpeg", ".png"} and actual.info.get("icc_profile") != icc:
            raise StitchPressError("图像缺少正确sRGB配置")
        import numpy as np

        diff = np.abs(np.asarray(actual).astype("int16") - np.asarray(rgb).astype("int16"))
        lossy = suffix in {".jpg", ".jpeg"}
        if not lossy and np.any(diff):
            raise StitchPressError("无损图像像素反读不符")
        report = dict(
            format=actual.format,
            size=list(actual.size),
            dpi=dpi,
            lossy=lossy,
            jpeg_quality=quality if lossy else None,
            mean_absolute_error=float(diff.mean()),
            max_absolute_error=int(diff.max()),
            visual_review="pending",
            source_pixels_checked=True,
        )
    rgb.close()
    return report


def write_thread_workbook(design, library, mapping, fabric, *, title, code, plan, path):
    plan = plan if isinstance(plan, ThreadPlan) else ThreadPlan(**plan)
    if not plan.confirmed:
        raise StitchPressError("配线系数尚未人工确认")
    wb = Workbook()
    sheet = wb.active
    sheet.title = "Thread List"
    divisor = decimal(plan.stitches_per_metre)
    sheet.append(["Customer Code", code, "CT", plan.ct, "Stitches / metre", float(divisor)])
    sheet.append(["No.", "DMC", "Symbol", "Color", "Swatch", "Stitches", "Metres"])
    entries = sorted(
        [(c, n) for c, n in zip(design.palette, design.counts, strict=True) if n],
        key=lambda p: natural_color_key(p[0]),
    )
    expected = {}
    expected_formula = {}
    for number, (color, count) in enumerate(entries, 1):
        row = number + 2
        metres = int((Decimal(count) / divisor).to_integral_value(rounding=ROUND_CEILING))
        formula = f"ROUNDUP(F{row}/$F$1,0)"
        sheet.append([number, color.code, "", color.name, "", count, "=" + formula])
        expected[f"G{row}"] = metres
        expected_formula[f"G{row}"] = formula
        sheet.cell(row, 5).fill = PatternFill(
            "solid", fgColor="".join(f"{v:02X}" for v in color.rgb)
        )
        tile = library.by_key[mapping[color.key]].image()
        rgb = screen_rgb(tile, library.icc)
        buffer = io.BytesIO()
        rgb.save(buffer, "PNG")
        buffer.seek(0)
        stamp = SheetImage(buffer)
        stamp.width = stamp.height = 30
        sheet.add_image(stamp, f"C{row}")
        rgb.close()
        tile.close()
    end = len(entries) + 3
    sheet.cell(end, 5, "TOTAL")
    for col in ("F", "G"):
        formula = f"SUM({col}3:{col}{end - 1})"
        sheet[f"{col}{end}"] = "=" + formula
        expected_formula[f"{col}{end}"] = formula
    expected[f"F{end}"] = sum(design.counts)
    expected[f"G{end}"] = sum(expected[f"G{r}"] for r in range(3, end))
    for row in sheet:
        for cell in row:
            if isinstance(cell.value, str) and cell.coordinate not in expected_formula:
                cell.data_type = "s"
            cell.font = Font(name="Calibri", size=14, bold=cell.row <= 2 or cell.row == end)
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        sheet.row_dimensions[row[0].row].height = 32
    for col, width in zip("ABCDEFG", (9, 18, 12, 32, 12, 18, 18), strict=True):
        sheet.column_dimensions[col].width = width
    sheet.freeze_panes = "C3"
    sheet.print_title_rows = "1:2"
    sheet.auto_filter.ref = f"A2:G{end - 1}"
    info = wb.create_sheet("Artwork Info")
    layout = physical_layout(design.width, design.height, fabric)
    for key, value in [
        ("Title", title),
        ("Code", code),
        ("Grid", f"{design.width} x {design.height}"),
        ("Thread scenario CT", plan.ct),
        ("Full crosses", sum(design.counts)),
        ("Active colors", len(entries)),
        ("Stitches per working metre", plan.stitches_per_metre),
        ("Coefficient source", plan.source_note),
        ("Rule revision", plan.revision),
        ("Current production CT", fabric.nominal_ct),
        ("Current production width cm", layout.width_cm),
        ("Current production height cm", layout.height_cm),
        (
            "Calculation",
            "Each colour ROUNDUP(stitches / configured coefficient); no added loss/strands",
        ),
    ]:
        info.append([key, value])
    for row in info:
        for cell in row:
            if isinstance(cell.value, str):
                cell.data_type = "s"
            cell.font = Font(size=14)
            cell.alignment = Alignment(wrap_text=True)
        info.row_dimensions[row[0].row].height = 30
    info.column_dimensions["A"].width = 36
    info.column_dimensions["B"].width = 80
    output = io.BytesIO()
    wb.save(output)
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(io.BytesIO(output.getvalue())) as source:
        xml = ET.fromstring(source.read("xl/worksheets/sheet1.xml"))
        cells = {c.attrib["r"]: c for c in xml.findall(".//s:c", ns)}
        for coordinate, value in expected.items():
            formula = cells[coordinate].find("s:f", ns)
            if formula is None or formula.text != expected_formula[coordinate]:
                raise StitchPressError("未知配线公式，不能填写缓存")
            cells[coordinate].find("s:v", ns).text = str(value)
        with ZipFile(path, "w", ZIP_DEFLATED) as target:
            for entry in source.infolist():
                target.writestr(
                    entry,
                    ET.tostring(xml, encoding="utf-8", xml_declaration=True)
                    if entry.filename == "xl/worksheets/sheet1.xml"
                    else source.read(entry.filename),
                )
    actual = load_workbook(path, data_only=True)
    formulas = load_workbook(path)
    if len(actual.active._images) != len(entries) or len(actual.sheetnames) != 2:
        raise StitchPressError("配线表符号图/作品信息页缺失")
    for coordinate, value in expected.items():
        if (
            actual.active[coordinate].value != value
            or formulas.active[coordinate].value != "=" + expected_formula[coordinate]
        ):
            raise StitchPressError("配线实际公式/缓存核对失败")
    for number, (color, count) in enumerate(entries, 3):
        if (
            actual.active.cell(number, 2).value != color.code
            or actual.active.cell(number, 2).data_type != "s"
            or actual.active.cell(number, 6).value != count
        ):
            raise StitchPressError("配线线号/针数反读不符")
    return dict(
        ct=plan.ct,
        colors=len(entries),
        stitches=sum(design.counts),
        metres=expected[f"G{end}"],
        symbols=len(entries),
        rule_sha=plan.revision,
        external_excel_display="pending",
    )

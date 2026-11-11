from dataclasses import asdict, dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from math import ceil

from .errors import StitchPressError


def decimal(value: str) -> Decimal:
    try:
        result = Decimal(value)
        if not result.is_finite():
            raise ValueError
        return result
    except (ValueError, ArithmeticError) as exc:
        raise StitchPressError(f"无效十进制数：{value}") from exc


def px(cm: Decimal, dpi: int = 300) -> int:
    return int((cm * dpi / Decimal("2.54")).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class Fabric:
    sx: str = "0.181"
    sy: str = "0.181"
    left: str = "5.430"
    top: str = "5.430"
    right: str = "5.430"
    bottom: str = "5.430"
    nominal_ct: str = "14"
    source: str = "factory-v10-actual-pitch"
    dpi: int = 300

    def __post_init__(self):
        for name in ("sx", "sy", "left", "top", "right", "bottom", "nominal_ct"):
            val = decimal(getattr(self, name))
            if val < 0 or (name in {"sx", "sy", "nominal_ct"} and val == 0):
                raise StitchPressError(f"{name}必须为合法正格距/布号或非负余量")
        if type(self.dpi) is not int or not 72 <= self.dpi <= 1200:
            raise StitchPressError("试验版DPI支持72–1200整数")

    @classmethod
    def theoretical(cls, ct: str) -> "Fabric":
        pitch = Decimal("2.54") / decimal(ct) if decimal(ct) > 0 else Decimal(0)
        return cls(sx=str(pitch), sy=str(pitch), nominal_ct=ct, source="theoretical-ct")


@dataclass(frozen=True)
class PhysicalLayout:
    width_cm: str
    height_cm: str
    canvas_cm: tuple[str, str]
    canvas_px: tuple[int, int]
    xs: tuple[int, ...]
    ys: tuple[int, ...]
    scale100_cm: tuple[str, str]


def physical_layout(width: int, height: int, fabric: Fabric) -> PhysicalLayout:
    sx, sy = decimal(fabric.sx), decimal(fabric.sy)
    left, top = decimal(fabric.left), decimal(fabric.top)
    w, h = sx * width, sy * height
    cw, ch = left + w + decimal(fabric.right), top + h + decimal(fabric.bottom)
    return PhysicalLayout(
        str(w),
        str(h),
        (str(cw), str(ch)),
        (px(cw, fabric.dpi), px(ch, fabric.dpi)),
        tuple(px(left + sx * i, fabric.dpi) for i in range(width + 1)),
        tuple(px(top + sy * i, fabric.dpi) for i in range(height + 1)),
        (str(100 * sx), str(100 * sy)),
    )


@dataclass(frozen=True)
class Page:
    number: int
    row: int
    column: int
    core: tuple[int, int, int, int]
    visible: tuple[int, int, int, int]
    neighbors: tuple[int | None, int | None, int | None, int | None]


@dataclass(frozen=True)
class PagePlan:
    paper_cm: tuple[str, str]
    paper_px: tuple[int, int]
    origin: tuple[int, int]
    legend_top: int
    legend_columns: int
    legend_rows: int
    row_height: int
    capacity: tuple[int, int]
    pages: tuple[Page, ...]
    scale: str = "1"

    def to_dict(self):
        return asdict(self)


def page_plan(
    width: int,
    height: int,
    colors: int,
    fabric: Fabric,
    paper_cm: tuple[str, str] = ("21", "29.7"),
    overlap: int = 0,
    scale: str = "1",
    legend_columns: int = 6,
    legend_entry_width: int = 345,
) -> PagePlan:
    if colors < 1 or colors > 512 or overlap < 0:
        raise StitchPressError("图例色数或重叠格数无效")
    mag = decimal(scale)
    if mag < 1 or mag > 4:
        raise StitchPressError("阅读倍率支持1–4，不能缩小冒充原倍率")
    pw, ph = (px(decimal(s), fabric.dpi) for s in paper_cm)
    factor = fabric.dpi / 300
    ox, oy = ceil(150 * factor), ceil(285 * factor)
    row_height = ceil(62 * factor)
    # Font-measured entry width supplied by renderer; never shrink font to gain capacity.
    cols = min(legend_columns, (pw - 2 * ox) // ceil(legend_entry_width * factor))
    if cols < 1:
        raise StitchPressError("纸张宽度无法容纳规定字号的图例")
    rows = ceil(colors / cols)
    legend_top = ph - ceil(330 * factor) - rows * row_height
    avail_w = pw - 2 * ox
    avail_h = legend_top - oy - ceil(80 * factor)
    pitch_x = decimal(fabric.sx) * fabric.dpi / Decimal("2.54") * mag
    pitch_y = decimal(fabric.sy) * fabric.dpi / Decimal("2.54") * mag
    cap_x = int((Decimal(avail_w) / pitch_x).to_integral_value(rounding=ROUND_FLOOR))
    cap_y = int((Decimal(avail_h) / pitch_y).to_integral_value(rounding=ROUND_FLOOR))
    cap_x, cap_y = (n // 10 * 10 if n >= 10 else n for n in (cap_x, cap_y))
    cap_x -= overlap
    cap_y -= overlap
    if cap_x < 1 or cap_y < 1 or overlap >= min(cap_x, cap_y):
        raise StitchPressError("图例/余量/重叠占满纸张，无法容纳针格")
    nx, ny = ceil(width / cap_x), ceil(height / cap_y)
    pages = []
    for row in range(ny):
        for col in range(nx):
            x0, y0 = col * cap_x, row * cap_y
            x1, y1 = min(width, x0 + cap_x), min(height, y0 + cap_y)
            visible = (max(0, x0 - overlap), max(0, y0 - overlap), x1, y1)
            pages.append(
                Page(
                    row * nx + col + 1,
                    row,
                    col,
                    (x0, y0, x1, y1),
                    visible,
                    (
                        row * nx + col if col else None,
                        row * nx + col + 2 if col + 1 < nx else None,
                        (row - 1) * nx + col + 1 if row else None,
                        (row + 1) * nx + col + 1 if row + 1 < ny else None,
                    ),
                )
            )
    return PagePlan(
        paper_cm,
        (pw, ph),
        (ox, oy),
        legend_top,
        cols,
        rows,
        row_height,
        (cap_x, cap_y),
        tuple(pages),
        scale,
    )


def page_coordinates(layout: PhysicalLayout, page: Page, plan: PagePlan):
    x0, y0, x1, y1 = page.visible
    ox, oy = plan.origin
    mag = decimal(plan.scale)
    # Offset globally rounded coordinates: keeps the production phase, never accumulates.
    xs = tuple(
        ox + int((Decimal(v - layout.xs[x0]) * mag).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        for v in layout.xs[x0 : x1 + 1]
    )
    ys = tuple(
        oy + int((Decimal(v - layout.ys[y0]) * mag).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        for v in layout.ys[y0 : y1 + 1]
    )
    return xs, ys

"""Explicit customer-supplied working-thread rules, never guessed consumption."""

from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, Decimal

from .errors import StitchPressError
from .identity import digest
from .layout import decimal
from .resources import natural_color_key


@dataclass(frozen=True)
class MaterialRule:
    name: str
    nominal_ct: str
    strands: int
    stitches_per_work_metre: str
    loss_percent: str
    pack_work_metres: str
    source_note: str
    confirmed: bool = False

    def __post_init__(self):
        if any(
            not isinstance(v, str) or not v.strip() or len(v) > 2000
            for v in (self.name, self.source_note)
        ):
            raise StitchPressError("配线规则须填写名称与厂家/口径来源")
        if type(self.strands) is not int or not 1 <= self.strands <= 12:
            raise StitchPressError("合股工作线须1–12股")
        if any(
            not isinstance(v, str)
            for v in (
                self.nominal_ct,
                self.stitches_per_work_metre,
                self.loss_percent,
                self.pack_work_metres,
            )
        ):
            raise StitchPressError("材料规则须保存为十进制文本")
        if (
            any(
                decimal(v) <= 0
                for v in (self.nominal_ct, self.stitches_per_work_metre, self.pack_work_metres)
            )
            or not 0 <= decimal(self.loss_percent) <= 200
        ):
            raise StitchPressError("CT/每米全针数/包装工作米须为正数，损耗须0–200%")
        if type(self.confirmed) is not bool or not self.confirmed:
            raise StitchPressError("材料口径未经人工确认，不能输出估算米数")

    @property
    def revision(self):
        return digest(asdict(self))


def material_rows(design, rule):
    result = []
    for color, count in sorted(
        zip(design.palette, design.counts, strict=True), key=lambda item: natural_color_key(item[0])
    ):
        if not count:
            continue
        exact = Decimal(count) / decimal(rule.stitches_per_work_metre)
        with_loss = exact * (1 + decimal(rule.loss_percent) / 100)
        pieces = int(
            (with_loss / decimal(rule.pack_work_metres)).to_integral_value(rounding=ROUND_CEILING)
        )
        work = Decimal(pieces) * decimal(rule.pack_work_metres)
        result.append(
            {
                "color": color,
                "stitches": count,
                "base_work_metres": format(exact, "f"),
                "with_loss_work_metres": format(with_loss, "f"),
                "packages": pieces,
                "supplied_work_metres": format(work, "f"),
                "raw_single_strand_metres": format(work * rule.strands, "f"),
            }
        )
    return result

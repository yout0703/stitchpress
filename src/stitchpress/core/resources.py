import io
import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageCms, ImageDraw, ImageFont

from .errors import ResourceError, StitchPressError
from .identity import digest, sha256, symbol_key
from .models import Design, ThreadColor


def bundled_font() -> Path:
    return Path(__file__).parents[1] / "resources" / "fonts" / "NotoSans-Regular.ttf"


def load_font(size: int, path: Path | None = None):
    path = path or bundled_font()
    if not path.is_file():
        raise ResourceError("缺少版式所绑定的字体文件，请重新导入")
    return ImageFont.truetype(str(path), size=size)


@dataclass(frozen=True)
class Symbol:
    key: str
    external_id: int | str
    filename: str
    tile: bytes
    pixel_sha: str

    def image(self) -> Image.Image:
        with Image.open(io.BytesIO(self.tile)) as im:
            im.load()
            return im.copy()


@dataclass(frozen=True)
class SymbolLibrary:
    revision: str
    symbols: tuple[Symbol, ...]
    icc: bytes | None
    usage: str

    def __post_init__(self):
        if self.icc:
            try:
                profile = ImageCms.ImageCmsProfile(io.BytesIO(self.icc))
                if profile.profile.xcolor_space.strip() != "CMYK":
                    raise ValueError("not CMYK")
            except Exception as exc:
                raise ResourceError("符号库共享ICC损坏或不是CMYK配置") from exc
        if not self.symbols or len({s.key for s in self.symbols}) != len(self.symbols):
            raise ResourceError("符号库为空或ID重复")
        if len({s.pixel_sha for s in self.symbols}) != len(self.symbols):
            raise ResourceError("符号库含像素完全重复的图块")

    @property
    def by_key(self):
        return {s.key: s for s in self.symbols}


def parse_symbol(data: bytes, external_id: int | str, filename: str) -> Symbol:
    if len(data) > 8 * 1024 * 1024:
        raise ResourceError("符号原件超过8MiB试验限制")
    with Image.open(io.BytesIO(data)) as source:
        if getattr(source, "n_frames", 1) != 1 or source.width * source.height > 1_000_000:
            raise ResourceError("符号必须为单页，且不能超过100万像素")
        source.load()
        if source.mode != "CMYK":
            raise ResourceError("当前印花小样仅接收已规范化的CMYK符号；不能直接改模式冒充ICC转换")
        pixels = source.tobytes()
        if not any(pixels):
            raise ResourceError("符号图块为空白")
        if source.info.get("icc_profile"):
            try:
                profile = ImageCms.ImageCmsProfile(io.BytesIO(source.info["icc_profile"]))
                if profile.profile.xcolor_space.strip() != "CMYK":
                    raise ValueError("CMYK tile with non-CMYK ICC")
            except Exception as exc:
                raise ResourceError("符号ICC损坏") from exc
        return Symbol(
            symbol_key(external_id),
            external_id,
            filename,
            data,
            sha256(source.mode.encode() + str(source.size).encode() + pixels),
        )


def demo_library(count: int = 66) -> SymbolLibrary:
    """Self-made test glyphs, explicitly unsuitable for customer production acceptance."""
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz@#%&"
    symbols = []
    for i, char in enumerate(chars[:count]):
        im = Image.new("CMYK", (20, 20), (0, 0, 0, 0))
        ImageDraw.Draw(im).text((2, -2), char, font=load_font(18), fill=(255,) * 4)
        output = io.BytesIO()
        im.save(output, "TIFF", compression="tiff_lzw")
        symbols.append(parse_symbol(output.getvalue(), i, f"demo-{i}.tif"))
    return SymbolLibrary(
        digest([s.pixel_sha for s in symbols]),
        tuple(symbols),
        None,
        "self-authored-demo-not-factory-approved",
    )


def load_catalog(data: bytes, brand: str = "DMC") -> tuple[ThreadColor, ...]:
    values = json.loads(data)
    if not isinstance(values, list) or not 1 <= len(values) <= 4096:
        raise ResourceError("线色目录必须为1–4096条原始记录")
    revision = sha256(data)
    colors = tuple(
        ThreadColor(brand, c["code"], c["name"], tuple(c["rgb"]), revision) for c in values
    )
    if len({c.key for c in colors}) != len(colors):
        raise ResourceError("目录包含重复线号身份")
    return colors


def assign_symbols(
    design: Design, library: SymbolLibrary, existing: dict[str, str] | None = None
) -> dict[str, str]:
    colors = [c for c, n in zip(design.palette, design.counts, strict=True) if n]
    if len(colors) > len(library.symbols):
        raise StitchPressError(f"有效色数{len(colors)}超过符号容量{len(library.symbols)}")
    keys = {c.key for c in colors}
    result = {k: v for k, v in (existing or {}).items() if k in keys}
    if any(v not in library.by_key for v in result.values()) or len(set(result.values())) != len(
        result
    ):
        raise StitchPressError("既有/手动绑定引用未知或重复符号")
    available = [s.key for s in library.symbols if s.key not in result.values()]
    # Source palette order is stable. Assignment is persisted, not rerun at export.
    for color in colors:
        if color.key not in result:
            result[color.key] = available.pop(0)
    return result


def library_assets(library: SymbolLibrary) -> tuple[dict, dict[str, bytes]]:
    assets = {}
    entries = []
    for sym in library.symbols:
        path = f"resources/symbols/{sha256(sym.tile)}.tif"
        assets[path] = sym.tile
        entries.append({"external_id": sym.external_id, "filename": sym.filename, "asset": path})
    icc_path = None
    if library.icc:
        icc_path = f"resources/icc/{sha256(library.icc)}.icc"
        assets[icc_path] = library.icc
    return {
        "revision": library.revision,
        "usage": library.usage,
        "icc_asset": icc_path,
        "entries": entries,
    }, assets


def library_from_assets(record: dict, assets: dict[str, bytes]) -> SymbolLibrary:
    return SymbolLibrary(
        record["revision"],
        tuple(
            parse_symbol(assets[e["asset"]], e["external_id"], e["filename"])
            for e in record["entries"]
        ),
        assets[record["icc_asset"]] if record["icc_asset"] else None,
        record["usage"],
    )

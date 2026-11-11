"""Bounded-band classic CMYK/LZW TIFF writer and decoded strip/cell verification."""

from hashlib import sha256

import imagecodecs
import numpy as np
import tifffile

from .errors import Cancelled, ResourceError, StitchPressError

MAX_STREAM_PIXELS = 250_000_000
BAND_BYTES = 8 * 1024**2


def save_production_stream(
    renderer, path, *, cancel=lambda: False, progress=lambda event: None, bigtiff=False
):
    width, height = renderer.layout.canvas_px
    if width * height > MAX_STREAM_PIXELS or width > 60_000:
        raise ResourceError(
            "流式生产TIFF暂限2.5亿像素、宽60000px；不自动缩图；BigTIFF目标软件须另验"
        )
    rows = max(1, min(128, BAND_BYTES // (width * 4)))
    expected = []

    def checkpoint():
        if cancel():
            raise Cancelled("流式生产已取消，未发布完整交付")

    def bands():
        for top in range(0, height, rows):
            checkpoint()
            surface, _ = renderer.production(band=(top, min(rows, height - top)))
            raw = np.asarray(surface.image)
            expected.append(sha256(raw.tobytes()).hexdigest())
            progress({"stage": "streaming-production", "row": top, "rows": height})
            # Byte iterators are pre-encoded strips; ndarray iterators denote whole pages.
            yield imagecodecs.lzw_encode(raw.tobytes())
            surface.image.close()

    with tifffile.TiffWriter(path, bigtiff=bigtiff) as writer:
        writer.write(
            bands(),
            shape=(height, width, 4),
            dtype=np.uint8,
            photometric="separated",
            planarconfig="contig",
            compression="lzw",
            rowsperstrip=rows,
            resolution=(renderer.fabric.dpi,) * 2,
            resolutionunit="INCH",
            iccprofile=renderer.library.icc,
            metadata=None,
            software="StitchPress",
            maxworkers=1,
            buffersize=BAND_BYTES,
        )
    counts = [0] * len(renderer.design.palette)
    xs, ys = renderer.layout.xs, renderer.layout.ys
    checked = 0
    with tifffile.TiffFile(path) as actual:
        if len(actual.pages) != 1 or actual.is_bigtiff != bigtiff:
            raise StitchPressError("流式TIFF页数或格式不符")
        page = actual.pages[0]
        icc = page.tags.get(34675)
        if (
            page.shape != (height, width, 4)
            or int(page.photometric) != 5
            or int(page.compression) != 5
            or page.samplesperpixel != 4
            or int(page.planarconfig) != 1
            or int(page.tags[296].value) != 2
            or any(
                page.tags[key].value[0] / page.tags[key].value[1] != renderer.fabric.dpi
                for key in (282, 283)
            )
            or (icc.value if icc else None) != renderer.library.icc
        ):
            raise StitchPressError("流式TIFF实际标签、DPI或ICC不符")
        for i, (decoded, index, _shape) in enumerate(page.segments(maxworkers=1, sort=True)):
            checkpoint()
            if decoded is None:
                raise StitchPressError("流式TIFF缺少图像分条")
            strip = decoded.reshape(-1, width, 4)
            top = index[2]
            if (
                top != i * rows
                or strip.shape[0] != min(rows, height - top)
                or sha256(strip.tobytes()).hexdigest() != expected[i]
            ):
                raise StitchPressError("流式TIFF分条顺序/像素读回不符")
            bottom = top + strip.shape[0]
            for gy in range(renderer.design.height):
                if ys[gy + 1] <= top or ys[gy] >= bottom:
                    continue
                sy0, sy1 = max(ys[gy], top), min(ys[gy + 1], bottom)
                for gx in range(renderer.design.width):
                    color = renderer.design.labels[gy][gx]
                    actual_cell = strip[sy0 - top : sy1 - top, xs[gx] : xs[gx + 1]]
                    if color >= 0:
                        tile = renderer.tile(color, xs[gx + 1] - xs[gx], ys[gy + 1] - ys[gy])
                        expected_cell = np.asarray(tile)[sy0 - ys[gy] : sy1 - ys[gy]]
                        correct = np.array_equal(actual_cell, expected_cell)
                    else:
                        correct = not np.any(actual_cell)
                    if not correct:
                        raise StitchPressError(f"流式TIFF针格反读不符：({gx},{gy})")
                    if sy0 == ys[gy] and color >= 0:
                        counts[color] += 1
            checked += 1
    if checked != len(expected) or tuple(counts) != renderer.design.counts:
        raise StitchPressError("流式TIFF分条/针数汇总不符")
    return {
        "window": (0, 0, renderer.design.width, renderer.design.height),
        "stitches": sum(counts),
        "counts": counts,
        "blank": renderer.design.width * renderer.design.height - sum(counts),
        "strips": checked,
        "rows_per_strip": rows,
        "maximum_band_bytes": width * rows * 4,
        "pixel_sha_verified": True,
        "classic_tiff": not bigtiff,
        "bigtiff": bigtiff,
    }


def production_overview(renderer, *, max_edge=1600, cancel=lambda: False):
    """Sample the actual full raster at global coordinates using bounded source bands."""
    from PIL import Image

    from .render import screen_rgb

    width, height = renderer.layout.canvas_px
    if width * height > MAX_STREAM_PIXELS or width > 60_000:
        raise ResourceError("完整概览暂限2.5亿像素、宽60000px；不会改动生产尺寸")
    if type(max_edge) is not int or not 16 <= max_edge <= 4096:
        raise ResourceError("概览长边须为16–4096像素")
    ratio = min(1, max_edge / max(width, height))
    ow, oh = max(1, round(width * ratio)), max(1, round(height * ratio))
    # Pixel-centre nearest sampling, independent of strip boundaries.
    xs = np.minimum(width - 1, ((np.arange(ow) + 0.5) * width / ow).astype(int))
    ys = np.minimum(height - 1, ((np.arange(oh) + 0.5) * height / oh).astype(int))
    result = np.empty((oh, ow, 3), dtype=np.uint8)
    rows = max(1, min(128, BAND_BYTES // (width * 4)))
    peak = 0
    for top in range(0, height, rows):
        if cancel():
            raise Cancelled("完整概览已取消")
        end = min(height, top + rows)
        selected = np.flatnonzero((ys >= top) & (ys < end))
        if not len(selected):
            continue
        surface, _ = renderer.production(band=(top, end - top))
        peak = max(peak, width * (end - top) * 4)
        rgb = screen_rgb(surface.image, renderer.library.icc)
        surface.image.close()
        result[selected] = np.asarray(rgb)[(ys[selected] - top)[:, None], xs[None, :]]
        rgb.close()
    return Image.fromarray(result), dict(
        overview=True,
        partial=False,
        full_size=(width, height),
        preview_size=(ow, oh),
        sampling="global-pixel-centre-nearest-v1",
        max_source_band_bytes=peak,
    )

"""Self-authored deterministic PM-v4 stress input, unrelated to customer artwork."""

import struct
import sys
from array import array
from pathlib import Path

from stitchpress.core import xsd


def make_large_xsd(path, width=1200, height=900, *, appendix_bytes=17 * 1024**2):
    original = Path(__file__).parents[1] / "fixtures/s01-independent.xsd"
    data = original.read_bytes()
    original_grid, offsets = xsd._grid, []

    def record(reader, count):
        offsets.append(reader.pos)
        return original_grid(reader, count)

    xsd._grid = record
    try:
        xsd.import_xsd(data)
    finally:
        xsd._grid = original_grid
    prefix = bytearray(data[: offsets[0]])
    struct.pack_into("<HH", prefix, 741, width, height)
    words = array("I")
    for y in range(height):
        words.extend(
            (15 << 24) if (x // 40 + y // 40) % 3 == 0 else (((x // 40 + y // 40) % 3 - 1) << 16)
            for x in range(width)
        )
    if sys.byteorder != "little":
        words.byteswap()
    with Path(path).open("wb") as stream:
        stream.write(prefix)
        stream.write(struct.pack("<4I", 0, 0, 0, 0))
        stream.write(struct.pack("<I", width * height))
        stream.write(words.tobytes())
        stream.write(b"\0\0\0\0")
        if appendix_bytes:
            stream.write(struct.pack("<II", 1, 6))
            chunk = b"self-authored non-stitch QA appendix\n" * 1024
            while appendix_bytes:
                part = chunk[:appendix_bytes]
                stream.write(part)
                appendix_bytes -= len(part)

import struct
from pathlib import Path

import pytest

from stitchpress.core.errors import StitchPressError
from stitchpress.core.xsd import import_xsd


def test_independent_nonzero_cipher_xsd():
    design = import_xsd(Path("fixtures/s01-independent.xsd").read_bytes())
    assert (design.width, design.height) == (4, 3)
    assert design.labels == ((0, -1, 1, -1), (-1, 0, 1, -1), (1, -1, -1, 0))
    assert [c.code for c in design.palette] == ["310", "B5200"]
    assert design.counts == (3, 3)


@pytest.mark.parametrize(
    "mutation", ["truncated", "small_stitch", "special", "unknown_tail", "version"]
)
def test_xsd_unsafe_or_unsupported_never_silently_drops(mutation):
    data = bytearray(Path("fixtures/s01-independent.xsd").read_bytes())
    if mutation == "truncated":
        data = data[:1000]
    elif mutation == "small_stitch":
        struct.pack_into("<I", data, 745, 1)
    elif mutation == "special":
        struct.pack_into("<H", data, len(data) - 2, 1)
    elif mutation == "unknown_tail":
        data.extend(b"unknown record")
    else:
        data[0] = 0
    with pytest.raises(StitchPressError):
        import_xsd(bytes(data))


def test_native_extension_preserves_full_cross_data_and_rejects_other_envelopes():
    import json

    raw = Path("fixtures/s01-independent.xsd").read_bytes()
    expected = import_xsd(raw)
    extension = struct.pack("<II", 1, 6) + b"native non-stitch appendix"
    restored = import_xsd(raw + extension)
    assert restored.labels == expected.labels and restored.counts == expected.counts
    report = json.loads(dict(restored.provenance)["import_report"])
    assert report["native_extension"]["bytes"] == len(extension)
    assert report["native_extension"]["sha256"]
    for invalid in (b"unknown", struct.pack("<II", 1, 7), struct.pack("<I", 1)):
        with pytest.raises(StitchPressError, match="附加区"):
            import_xsd(raw + invalid)
    complex_data = bytearray(raw + extension)
    struct.pack_into("<H", complex_data, 749, 2)
    with pytest.raises(StitchPressError, match="非全针"):
        import_xsd(bytes(complex_data))

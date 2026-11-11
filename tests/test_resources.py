import io

import pytest
from PIL import Image, ImageCms

from stitchpress.core.errors import ResourceError
from stitchpress.core.resources import SymbolLibrary, demo_library, parse_symbol


def test_broken_or_rgb_shared_icc_cannot_be_used_for_cmyk_printing():
    symbols = demo_library(2).symbols
    for data in (b"broken", ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()):
        with pytest.raises(ResourceError, match="ICC"):
            SymbolLibrary("test", symbols, data, "customer-imported")


def test_symbol_with_mismatched_embedded_icc_is_rejected():
    image = Image.new("CMYK", (20, 20), (255, 255, 255, 255))
    output = io.BytesIO()
    image.save(
        output,
        "TIFF",
        icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes(),
    )
    with pytest.raises(ResourceError, match="ICC"):
        parse_symbol(output.getvalue(), "0.1", "test.tif")

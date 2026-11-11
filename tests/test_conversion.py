import io

import numpy as np
import pytest
from PIL import Image, ImageCms

from stitchpress.core.conversion import ConversionOptions, convert_image, normalize_image
from stitchpress.core.errors import StitchPressError


def image_bytes(image, **kwargs):
    output = io.BytesIO()
    image.save(output, "TIFF" if image.mode == "CMYK" else "PNG", **kwargs)
    return output.getvalue()


def test_s20_white_is_stitched_transparent_hidden_rgb_excluded(minimal):
    im = Image.new("RGBA", (3, 2))
    im.putdata(
        [
            (0, 0, 0, 255),
            (254, 0, 129, 0),
            (255, 255, 255, 255),
            (255, 255, 255, 255),
            (0, 0, 0, 255),
            (0, 0, 0, 255),
        ]
    )
    opts = ConversionOptions(3, 2, 2)
    first, run = convert_image(image_bytes(im), minimal.palette, opts)
    assert first.labels == minimal.labels
    assert first.counts == (3, 2)
    assert run["assumption"] is not None
    im.putpixel((1, 0), (1, 255, 0, 0))
    second, _ = convert_image(image_bytes(im), minimal.palette, opts)
    assert second.content_sha == first.content_sha


def test_s20_icc_validation_and_actual_transform():
    rgb = Image.new("RGB", (3, 2), (120, 80, 230))
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    im, run = normalize_image(image_bytes(rgb, icc_profile=profile))
    assert im.getpixel((0, 0)) == (120, 80, 230, 255)
    assert run["source_icc_sha"]
    assert run["assumption"] is None
    with pytest.raises(StitchPressError, match="ICC"):
        normalize_image(image_bytes(rgb, icc_profile=b"broken-profile"))
    with pytest.raises(StitchPressError, match="ICC"):
        normalize_image(image_bytes(Image.new("CMYK", (2, 2))))


def test_s20_masks_and_subject_alpha_conflicts(minimal):
    im = Image.new("RGBA", (3, 2), (255, 255, 255, 255))
    im.putpixel((0, 0), (0, 0, 0, 0))
    with pytest.raises(StitchPressError, match="透明"):
        convert_image(
            image_bytes(im),
            minimal.palette,
            ConversionOptions(3, 2, 2),
            subject_mask=np.ones((2, 3), dtype=bool),
        )
    with pytest.raises(StitchPressError, match="布尔"):
        convert_image(
            image_bytes(im),
            minimal.palette,
            ConversionOptions(3, 2, 2),
            blank_mask=np.zeros((3, 2), dtype=bool),
        )


def test_deterministic_conversion_no_color_budget_expansion(minimal):
    im = Image.new("RGB", (12, 8))
    im.putdata([(i * 15 % 256, i * 31 % 256, i * 61 % 256) for i in range(96)])
    data = image_bytes(im)
    a, _ = convert_image(data, minimal.palette, ConversionOptions(6, 4, 1))
    b, _ = convert_image(data, minimal.palette, ConversionOptions(6, 4, 1))
    assert a.content_sha == b.content_sha
    assert len(a.palette) == 1

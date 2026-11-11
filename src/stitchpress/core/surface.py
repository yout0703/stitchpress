"""A global-coordinate Pillow drawing surface backed by one bounded horizontal band."""

from PIL import Image, ImageDraw


class BandSurface:
    def __init__(self, size, top, rows):
        self.width, self.height = size
        self.band_top = top
        self.image = Image.new("CMYK", (self.width, rows), (0, 0, 0, 0))

    def paste(self, tile, xy):
        self.image.paste(tile, (xy[0], xy[1] - self.band_top))


class BandDraw:
    def __init__(self, surface):
        self.surface = surface
        self.draw = ImageDraw.Draw(surface.image)

    def shifted(self, xy):
        if len(xy) == 4 and isinstance(xy[0], (int, float)):
            return xy[0], xy[1] - self.surface.band_top, xy[2], xy[3] - self.surface.band_top
        return [(x, y - self.surface.band_top) for x, y in xy]

    def rectangle(self, xy, **kwargs):
        return self.draw.rectangle(self.shifted(xy), **kwargs)

    def line(self, xy, **kwargs):
        return self.draw.line(self.shifted(xy), **kwargs)

    def textbbox(self, xy, *args, **kwargs):
        # Measurement remains in global coordinates; only painting shifts the origin.
        return self.draw.textbbox(xy, *args, **kwargs)

    def text(self, xy, *args, **kwargs):
        return self.draw.text((xy[0], xy[1] - self.surface.band_top), *args, **kwargs)


def draw_on(image):
    return BandDraw(image) if isinstance(image, BandSurface) else ImageDraw.Draw(image)

"""Portable brand assets; never depend on an external image-generation directory."""

from pathlib import Path

from PySide6.QtGui import QIcon


def brand_asset(name):
    return Path(__file__).parents[1] / "resources" / "brand" / name


def application_icon():
    return QIcon(str(brand_asset("stitchpress-mark.png")))

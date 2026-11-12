import json
from pathlib import Path
from xml.etree import ElementTree as XML

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest

from stitchpress.app.appearance import THEMES
from stitchpress.app.desktop import Workbench
from stitchpress.app.fixtures import demo_project
from stitchpress.app.jobs import verify_delivery
from stitchpress.app.post_settings import load_settings, save_settings
from stitchpress.app.reference_actions import import_pattern
from stitchpress.app.reference_package import export_reference, preflight
from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import digest, sha256
from stitchpress.core.importers import export_oxs, import_oxs
from stitchpress.core.models import Design
from stitchpress.core.post import PostSettings
from stitchpress.core.xsd import import_xsd


def chart(tmp_path):
    root = XML.fromstring(export_oxs(import_xsd(Path("fixtures/s01-independent.xsd").read_bytes())))
    root.find("properties").set("charttitle", "白针与留空 · OXS用例")
    path = tmp_path / "00123.OXS"
    path.write_bytes(XML.tostring(root, encoding="utf-8", xml_declaration=True))
    return path


def test_oxs_import_keeps_title_sku_white_blank_and_portable_original(tmp_path):
    path = chart(tmp_path)
    source = demo_project()
    before = digest(source.entities)
    imported = import_pattern(source, path)
    record = imported.entities["pattern_input"]
    assert record["format"] == "oxs" and record["asset"].endswith(".oxs")
    assert record["sha256"] == sha256(path.read_bytes())
    assert imported.assets[record["asset"]] == path.read_bytes()
    assert imported.entities["metadata"] == {"code": "00123", "title": "白针与留空 · OXS用例"}
    design = Design.from_dict(imported.entities["design"])
    assert design.labels[0][1] == -1 and design.labels[0][2] >= 0
    assert design.palette[design.labels[0][2]].code == "B5200"
    assert design.counts == (3, 3)
    assert before == digest(source.entities)
    save_settings(imported, tmp_path / "设置.json")
    reopened = load_settings(tmp_path / "设置.json")
    assert reopened.entities["pattern_input"] == record
    assert reopened.assets[record["asset"]] == path.read_bytes()


def test_oxs_original_and_print_outputs_share_one_master(tmp_path):
    path = chart(tmp_path)
    project = import_pattern(demo_project(), path)
    options = PostSettings(targets=("original", "production", "pages", "settings"))
    project.entities["post"] = options.to_dict()
    receipt = export_reference(project, tmp_path / "outputs", tmp_path / "work", demo=True)
    assert verify_delivery(tmp_path / "outputs", receipt)["files"] == receipt["files"]
    originals = list((tmp_path / "outputs").glob("*.oxs"))
    assert len(originals) == 1 and originals[0].read_bytes() == path.read_bytes()
    assert not list((tmp_path / "outputs").glob("*.xsd"))
    assert list((tmp_path / "outputs").glob("*画布文件.tif"))
    assert list((tmp_path / "outputs/分页图纸").glob("*.jpg"))
    project.entities["design"] = json.loads(json.dumps(project.entities["design"]))
    project.entities["design"]["labels"][0][0] = 1
    with pytest.raises(StitchPressError, match="原OXS"):
        preflight(project, demo=True)


def test_oxs_with_backstitches_does_not_silently_drop_them(tmp_path):
    path = chart(tmp_path)
    root = XML.fromstring(path.read_bytes())
    XML.SubElement(
        root.find("backstitches"), "backstitch", x1="0", y1="0", x2="1", y2="1", palindex="1"
    )
    path.write_bytes(XML.tostring(root, encoding="utf-8"))
    project = demo_project()
    before = digest(project.entities)
    with pytest.raises(StitchPressError, match="非全针"):
        import_pattern(project, path)
    assert digest(project.entities) == before


def test_oxs_main_import_entry_preserves_input_for_later_export(qtbot, tmp_path, monkeypatch):
    path = chart(tmp_path)
    w = Workbench()
    qtbot.addWidget(w)

    def choose(*args):
        assert "*.oxs" in args[-1] and "*.xsd" in args[-1]
        return str(path), ""

    monkeypatch.setattr("stitchpress.app.reference_ui.QFileDialog.getOpenFileName", choose)
    w.import_reference_pattern()
    assert w.draft.entities["pattern_input"]["format"] == "oxs"
    assert w.design.counts == import_oxs(path.read_bytes()).counts
    assert "00123.OXS" in w.ref_input_report.text()
    assert "OXS" in w.statusBar().currentMessage()
    w.dirty = False
    w.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_tabs_hover_has_solid_background_and_no_native_black_baseline(qtbot, theme):
    w = Workbench()
    qtbot.addWidget(w)
    w.set_theme(theme, persist=False)
    w.show()
    w.show_page(4)
    bar = w.print_tabs
    bar.setCurrentIndex(3)
    assert not bar.drawBase()
    geometry = [bar.tabRect(i) for i in range(bar.count())]
    for _ in range(3):
        for i in (0, 2, 1, 3):
            QTest.mouseMove(bar, bar.tabRect(i).center())
            bar.repaint()
            assert bar.currentIndex() == 3
            assert [bar.tabRect(j) for j in range(bar.count())] == geometry
            image = bar.grab().toImage()
            scale = image.devicePixelRatio()

            def pixel(x, y, image=image, scale=scale):
                return image.pixelColor(round(x * scale), round(y * scale))

            rect = bar.tabRect(i)
            assert pixel(rect.left() + 4, rect.top() + 3) == QColor(THEMES[theme]["bg"])
            assert pixel(bar.width() - 3, bar.height() - 1) == QColor(THEMES[theme]["bg"])
    QTest.mouseMove(bar, QPoint(bar.width() - 4, 2))
    w.close()

import colorsys
from dataclasses import asdict
from pathlib import Path

from stitchpress.core.identity import canonical_bytes
from stitchpress.core.layout import Fabric
from stitchpress.core.models import ThreadColor, new_design
from stitchpress.core.resources import demo_library, library_assets

from .store import Project


def sample_design(width: int = 48, height: int = 64, colors: int = 8):
    palette = tuple(
        ThreadColor(
            "TEST",
            f"{i:03}",
            f"Self-authored color {i}",
            tuple(round(v * 255) for v in colorsys.hsv_to_rgb(i / colors, 0.65, 0.8)),
            "self-authored-v1",
        )
        for i in range(colors)
    )
    labels = []
    for y in range(height):
        row = []
        for x in range(width):
            u, v = (x + 0.5) / width - 0.5, (y + 0.5) / height - 0.5
            if u * u + v * v > 0.22 or abs(u) < 0.035 and abs(v) < 0.12:
                row.append(-1)
            else:
                row.append((x // 4 + y // 5) % colors)
        labels.append(row)
    return new_design(
        width,
        height,
        palette,
        labels,
        provenance=(("source", "self-authored-geometric-test-grid"),),
    )


def _sample_project(design, symbols):
    project = Project.create("几何花园 · 自有测试图")
    project.entities["metadata"]["code"] = "DEMO001"
    project.entities["design"] = design.to_dict()
    library, assets = library_assets(symbols)
    project.entities["library"] = library
    project.entities["fabric"] = asdict(Fabric())
    project.assets.update(assets)
    return project


def demo_project(width=48, height=64, colors=8):
    return _sample_project(sample_design(width, height, colors), demo_library())


def new_project():
    """First launch and File/New share the shipped color library; fixtures stay independent."""
    from stitchpress.core.resources import assign_symbols, builtin_library

    library = builtin_library()
    design = sample_design()
    project = _sample_project(design, library)
    project.entities["mapping"] = assign_symbols(design, library, strategy="color-nearest-v1")
    return project


def save_demo(path: Path, width=48, height=64, colors=8):
    from stitchpress.platform.files import atomic_write

    atomic_write(path, demo_project(width, height, colors).encode())


def benchmark_import(path: Path):
    """Measure the real packaged importer and widgets without changing customer files."""
    import platform
    from time import perf_counter

    from PySide6.QtWidgets import QApplication

    from stitchpress import __version__
    from stitchpress.core.identity import sha256_file

    from .desktop import Workbench
    from .reference_actions import import_pattern

    application = QApplication.instance() or QApplication([])
    window = Workbench()
    try:
        window.show()
        application.processEvents()
        original_sha = sha256_file(path)
        start = perf_counter()
        project = import_pattern(window.draft, path)
        parsed = perf_counter()
        window.set_project(project)
        window.dirty = True
        window.update_document_state()
        application.processEvents()
        adopted = perf_counter()
        design = window.design
        # A many-color original grid catches accidental per-row full-grid reconstruction.
        many = new_project()
        many_design = sample_design(220, 220, 77)
        from stitchpress.core.resources import assign_symbols, builtin_library

        many.entities["design"] = many_design.to_dict()
        many.entities["mapping"] = assign_symbols(many_design, builtin_library())
        large_start = perf_counter()
        window.set_project(many)
        application.processEvents()
        large_elapsed = perf_counter() - large_start
        return dict(
            app=__version__,
            os=platform.system(),
            pattern_sha256=original_sha,
            grid=[design.width, design.height],
            stitches=sum(design.counts),
            import_seconds=parsed - start,
            adopt_seconds=adopted - parsed,
            total_seconds=adopted - start,
            many_color_adopt_seconds=large_elapsed,
            many_color_grid=[220, 220],
            many_color_stitches=sum(many_design.counts),
            builtin_symbols=len(window.symbol_library().symbols),
            source_unchanged=sha256_file(path) == original_sha,
        )
    finally:
        window.dirty = False
        window.close()


def verify_large_ui(pattern, project_path, output):
    """Drive the real worker dispatcher and Qt event loop; only write an independent QA copy."""
    import time

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from stitchpress.core.identity import sha256_file
    from stitchpress.platform.metrics import peak_rss_bytes

    from .background_load import cancel_load, start_load
    from .background_save import request_save
    from .desktop import Workbench

    application = QApplication.instance() or QApplication([])
    window = Workbench()
    window.recovery_timer.stop()
    errors, ticks, phases = [], [], {}
    window.error = lambda error: errors.append(str(error))
    timer = QTimer(window)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(10)
    source_sha = sha256_file(project_path)
    window.show()

    def wait_for(attribute, started, before):
        while getattr(window, attribute, None) is not None:
            application.processEvents()
            if time.monotonic() - started > 240:
                cancel_load(window)
                raise RuntimeError("大图界面诊断超时")
            time.sleep(0.005)
        application.processEvents()
        if errors:
            raise RuntimeError(errors[-1])
        observed = [started, *ticks[before:], time.monotonic()]
        return dict(
            seconds=observed[-1] - started,
            event_ticks=len(ticks) - before,
            maximum_event_gap_seconds=max(
                b - a for a, b in zip(observed, observed[1:], strict=False)
            ),
        )

    try:
        started, before = time.monotonic(), len(ticks)
        start_load(window, project_path, "load-project")
        phases["open"] = wait_for("_load_process", started, before)
        item = window.canvas._grid_item
        assert max(item.overview.width(), item.overview.height()) <= 2048
        window.show_page(0)
        application.processEvents()
        window.canvas.actual_pixels()
        window.canvas.centerOn(item.dx + item.design.width * 9, item.dy + item.design.height * 9)
        window.canvas.viewport().repaint()
        application.processEvents()
        assert 0 < item.last_visible_cells < 20000
        visible = item.last_visible_cells
        saved_copy = Path(output)
        started, before = time.monotonic(), len(ticks)
        assert request_save(window, copy_target=saved_copy)
        phases["save_copy"] = wait_for("_save_process", started, before)
        started, before = time.monotonic(), len(ticks)
        start_load(window, pattern, "import-pattern")
        phases["import"] = wait_for("_load_process", started, before)
        design = window.design
        start_load(window, pattern, "import-pattern")
        cancel_load(window)
        wait_for("_load_process", time.monotonic(), len(ticks))
        assert window.design.content_sha == design.content_sha
        assert sha256_file(project_path) == source_sha
        assert all(p["event_ticks"] > 2 for p in phases.values())
        return dict(
            grid=[design.width, design.height],
            counts=design.counts,
            phases=phases,
            viewport_cells=visible,
            source_unchanged=True,
            cancel_preserved_draft=True,
            saved_copy=str(saved_copy),
            peak_rss_bytes=peak_rss_bytes(),
        )
    finally:
        timer.stop()
        window.dirty = False
        window.close()


def create_fixture_manifest(path: Path):
    descriptions = [
        "最小矩阵",
        "客户尺寸样本",
        "160格样本",
        "55/56/66/67色复杂图例",
        "特殊ID和同RGB线号",
        "浅色表面与负空间",
        "不支持针法",
        "路径与迁移",
        "故障注入",
        "阶梯大图",
        "逐色材料取整",
        "商品模板",
        "AI来图与文字",
        "AI局部漂移",
        "AI疑点与错误",
        "费用与网络",
        "AI跨端",
        "并发保存",
        "补丁后重转",
        "ICC与透明度",
    ]
    data = {
        "schema_version": 1,
        "license": "self-authored-test-data",
        "fixtures": [
            {
                "id": f"S{i:02}",
                "description": text,
                "status": "specified",
                "source": "self-authored",
            }
            for i, text in enumerate(descriptions, 1)
        ],
    }
    path.write_bytes(canonical_bytes(data) + b"\n")

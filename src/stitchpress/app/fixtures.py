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

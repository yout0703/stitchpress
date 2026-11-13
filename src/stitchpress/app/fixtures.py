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


def demo_project(width=48, height=64, colors=8):
    project = Project.create("几何花园 · 自有测试图")
    project.entities["metadata"]["code"] = "DEMO001"
    project.entities["design"] = sample_design(width, height, colors).to_dict()
    library, assets = library_assets(demo_library())
    project.entities["library"] = library
    project.entities["fabric"] = asdict(Fabric())
    project.assets.update(assets)
    return project


def new_project():
    """First launch and File/New share the shipped color library; fixtures stay independent."""
    from stitchpress.core.resources import assign_symbols, builtin_library

    project = demo_project()
    library = builtin_library()
    record, assets = library_assets(library)
    project.entities["library"] = record
    project.assets = assets
    project.entities["mapping"] = assign_symbols(
        sample_design(), library, strategy="color-nearest-v1"
    )
    return project


def save_demo(path: Path, width=48, height=64, colors=8):
    from stitchpress.platform.files import atomic_write

    atomic_write(path, demo_project(width, height, colors).encode())


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

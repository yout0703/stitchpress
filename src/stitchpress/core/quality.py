"""Deterministic chart diagnostics. Warnings are never an automatic human approval."""

from collections import deque

from .identity import digest
from .models import Design


def inspect_design(design: Design, *, tiny_limit: int = 3) -> dict:
    counts = design.counts
    visited = bytearray(design.width * design.height)
    histogram = {}
    examples = []
    components = 0
    for y, row in enumerate(design.labels):
        for x, color in enumerate(row):
            position = y * design.width + x
            if color < 0 or visited[position]:
                continue
            visited[position] = 1
            queue = deque([(x, y)])
            size = 0
            x0 = x1 = x
            y0 = y1 = y
            while queue:
                gx, gy = queue.popleft()
                size += 1
                x0, x1 = min(x0, gx), max(x1, gx)
                y0, y1 = min(y0, gy), max(y1, gy)
                for nx, ny in ((gx - 1, gy), (gx + 1, gy), (gx, gy - 1), (gx, gy + 1)):
                    if 0 <= nx < design.width and 0 <= ny < design.height:
                        index = ny * design.width + nx
                        if not visited[index] and design.labels[ny][nx] == color:
                            visited[index] = 1
                            queue.append((nx, ny))
            components += 1
            if size <= tiny_limit:
                histogram[str(size)] = histogram.get(str(size), 0) + 1
                if len(examples) < 200:
                    examples.append(
                        {"palette_index": color, "cells": size, "region": [x0, y0, x1 + 1, y1 + 1]}
                    )
    stitches = sum(counts)
    white = [
        i
        for i, (c, n) in enumerate(zip(design.palette, counts, strict=True))
        if n and min(c.rgb) >= 235
    ]
    report = {
        "schema_version": 1,
        "design_sha": design.content_sha,
        "grid": [design.width, design.height],
        "stitches": stitches,
        "blank_cells": design.width * design.height - stitches,
        "colors": sum(n > 0 for n in counts),
        "counts": list(counts),
        "connectivity": "same-color-four-neighbor",
        "components": components,
        "tiny_limit": tiny_limit,
        "tiny_components": histogram,
        "examples": examples,
        "examples_truncated": sum(histogram.values()) > len(examples),
        "light_stitched_colors": white,
        "human_review": "required",
        "notes": [
            "小色块可能是必要细节，不自动删除或合并。",
            "浅色有针与无针留空分别计数，不自动抠除白色。",
            "数字检查不判断对象、构图或来源是否正确。",
        ],
    }
    report["report_sha"] = digest(report)
    return report

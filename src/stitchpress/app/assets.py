"""Merge imported content without overwriting immutable asset paths in an existing work."""

import copy
from pathlib import PurePosixPath

from stitchpress.core.identity import sha256


def merge_assets(current, incoming):
    result = dict(current)
    aliases = {}
    for name, content in incoming.items():
        target = name
        if name in result and result[name] != content:
            suffix = PurePosixPath(name).suffix
            target = f"resources/imported/{sha256(content)}{suffix}"
        result[target] = content
        aliases[name] = target
    return result, aliases


def remap_assets(record, aliases):
    if isinstance(record, dict):
        return {
            k: aliases.get(v, v)
            if k in {"asset", "icc_asset"} and isinstance(v, str)
            else remap_assets(v, aliases)
            for k, v in record.items()
        }
    if isinstance(record, list):
        return [remap_assets(v, aliases) for v in record]
    return copy.deepcopy(record)

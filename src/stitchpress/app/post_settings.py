"""Versioned JSON settings with immutable relative sidecars; no credentials or base64."""

import copy
import json
import shutil
import tempfile
from pathlib import Path

from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import canonical_bytes, digest, safe_filename, sha256
from stitchpress.platform.files import atomic_write, exclusive_lock

from .store import MAX_PROJECT_BYTES, Project, _safe_member

FORMAT = "StitchPress.Reference.Settings.v1"
KEYS = {
    "metadata",
    "design",
    "fabric",
    "reference",
    "library",
    "mapping",
    "manual_mapping",
    "mapping_policy",
    "source",
    "input_profile",
    "output_font",
    "text_layers",
    "commerce",
    "post",
    "pattern_input",
}


class MissingSettingsAssets(StitchPressError):
    def __init__(self, missing):
        self.missing = missing
        super().__init__("设置资源缺失，可重新选择匹配SHA的原文件：" + ", ".join(missing))


def settings_document(project, resource_folder):
    entities = {k: copy.deepcopy(v) for k, v in project.entities.items() if k in KEYS}
    used = set()

    def walk(value):
        if isinstance(value, str) and value in project.assets:
            used.add(value)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)

    walk(entities)
    assets = {name: project.assets[name] for name in used}
    # Reuse full container validation before writing portable settings.
    check = Project.create()
    check.entities = entities
    check.assets = assets
    check.encode()
    record = dict(
        format=FORMAT,
        schema_version=1,
        origin_project_id=project.project_id,
        entities=entities,
        assets={
            name: dict(file=f"{resource_folder}/{sha256(data)}.bin", sha256=sha256(data))
            for name, data in sorted(assets.items())
        },
    )
    return record, assets


def save_settings(project, path):
    path = Path(path)
    if path.suffix.lower() != ".json":
        raise StitchPressError("作品设置须为JSON")
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(path.with_suffix(".json.lock")):
        if path.exists():
            raise StitchPressError("设置已存在，请另存新版本")
        preliminary, _ = settings_document(project, "resources")
        folder = safe_filename(path.stem) + ".resources-" + digest(preliminary)[:12]
        document, assets = settings_document(project, folder)
        target = path.parent / folder
        if target.exists():
            raise StitchPressError("设置资源目录已存在，请选择新的设置文件名")
        staging = Path(tempfile.mkdtemp(prefix=".post-settings-", dir=path.parent))
        try:
            for data in assets.values():
                atomic_write(staging / (sha256(data) + ".bin"), data)
            staging.rename(target)
            atomic_write(path, canonical_bytes(document))
        except BaseException:
            if target.exists() and not path.exists():
                shutil.rmtree(target)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return dict(saved=str(path), resources=len(assets), sha256=sha256(path.read_bytes()))


def load_settings(path, *, replacements=None):
    path = Path(path)
    raw = path.read_bytes()
    if len(raw) > MAX_PROJECT_BYTES:
        raise StitchPressError("设置文件超过资源上限")
    record = json.loads(raw)
    if (
        not isinstance(record, dict)
        or set(record) != {"format", "schema_version", "origin_project_id", "entities", "assets"}
        or record.get("format") != FORMAT
        or type(record.get("schema_version")) is not int
        or record["schema_version"] != 1
    ):
        raise StitchPressError("不是支持的本软件作品设置JSON；不会猜测导入参考私有协议")
    entries = record["assets"]
    if not isinstance(entries, dict) or len(entries) > 2048:
        raise StitchPressError("设置资源清单无效")
    assets = {}
    missing = []
    total = len(raw)
    for name, entry in entries.items():
        _safe_member(name)
        if not isinstance(entry, dict) or set(entry) != {"file", "sha256"}:
            raise StitchPressError("设置资源索引无效")
        _safe_member(entry["file"])
        relative = Path(entry["file"])
        resource = path.parent / relative
        if any(
            (path.parent / Path(*relative.parts[:n])).is_symlink()
            for n in range(1, len(relative.parts) + 1)
        ):
            raise StitchPressError("设置资源不能使用符号链接")
        if name in (replacements or {}):
            resource = Path(replacements[name])
        if not resource.is_file():
            missing.append(name)
            continue
        total += resource.stat().st_size
        if total > MAX_PROJECT_BYTES:
            raise StitchPressError("设置及资源总量超过上限")
        data = resource.read_bytes()
        if sha256(data) != entry["sha256"]:
            raise StitchPressError("重新选择的资源或设置侧件SHA不匹配：" + name)
        assets[name] = data
    if missing:
        raise MissingSettingsAssets(missing)
    project = Project.create()
    project.entities = record["entities"]
    project.assets = assets
    project.encode()
    return project


def companion_candidates(path):
    path = Path(path)
    return sorted(
        (
            p
            for p in path.parent.iterdir()
            if p.is_file()
            and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
            and p.stem.casefold() == path.stem.casefold()
        ),
        key=lambda p: p.name.casefold(),
    )

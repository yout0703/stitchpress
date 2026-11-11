"""Independent crash drafts and immutable parameter presets, separate from live projects."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from stitchpress.core.configuration import Preset
from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import canonical_bytes, sha256
from stitchpress.platform.files import atomic_write

from .store import Project


class RecoveryStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.session = uuid4().hex

    @property
    def path(self):
        return self.directory / f"{self.session}.stitchpress"

    def checkpoint(self, project: Project, original_path: Path | None):
        payload = project.encode()
        atomic_write(self.path, payload)
        receipt = {
            "schema_version": 1,
            "project_id": project.project_id,
            "title": project.entities["metadata"]["title"],
            "original_path": str(original_path) if original_path else None,
            "created_at": datetime.now(UTC).isoformat(),
            "sha256": sha256(payload),
        }
        atomic_write(self.path.with_suffix(".json"), canonical_bytes(receipt))

    def clear(self):
        self.path.unlink(missing_ok=True)
        self.path.with_suffix(".json").unlink(missing_ok=True)

    def candidates(self):
        results = []
        for path in self.directory.glob("*.json"):
            try:
                record = json.loads(path.read_bytes())
                candidate = path.with_suffix(".stitchpress")
                if candidate != self.path and candidate.is_file():
                    results.append({**record, "path": candidate})
            except (OSError, ValueError, TypeError):
                continue
        return sorted(results, key=lambda r: r.get("created_at", ""), reverse=True)

    @staticmethod
    def restore(record):
        payload = record["path"].read_bytes()
        if sha256(payload) != record["sha256"]:
            raise StitchPressError("恢复草稿的SHA不符；未替换当前作品")
        project = Project.decode(payload)
        if project.project_id != record["project_id"]:
            raise StitchPressError("恢复草稿身份不符")
        return project


class PresetStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def save(self, preset: Preset):
        path = self.directory / (preset.revision + ".json")
        payload = canonical_bytes(preset.to_dict())
        if path.exists() and path.read_bytes() != payload:
            raise StitchPressError("预设版本发生冲突，不能覆盖")
        atomic_write(path, payload)
        return path

    def list(self):
        presets = []
        errors = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                preset = Preset(**json.loads(path.read_bytes()))
                if preset.revision != path.stem:
                    raise StitchPressError("预设指纹不符")
                presets.append(preset)
            except (OSError, ValueError, TypeError, StitchPressError) as exc:
                errors.append(f"{path.name}: {exc}")
        return presets, errors

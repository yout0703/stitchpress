import copy
import io
import json
import re
import stat
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from stitchpress.core.errors import ConflictError, StitchPressError
from stitchpress.core.identity import canonical_bytes, digest, sha256
from stitchpress.platform.files import atomic_write, exclusive_lock

MAX_PROJECT_BYTES = 256 * 1024 * 1024
MAX_PROJECT_FILES = 2048
PROJECT_SCHEMA = 2


def _safe_member(name: str) -> None:
    parts = PurePosixPath(name).parts
    if (
        not parts
        or name.startswith(("/", "\\"))
        or "\\" in name
        or PurePosixPath(name).as_posix() != name
        or any(
            p in {".", ".."}
            or re.search(r'[<>:"|?*\x00-\x1f]', p)
            or p.rstrip(" .") != p
            or p.split(".")[0].upper()
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10)),
            }
            for p in parts
        )
    ):
        raise StitchPressError("作品容器含不安全相对路径")


@dataclass
class Project:
    project_id: str
    commit_seq: int
    entities: dict[str, dict]
    assets: dict[str, bytes]
    events: list[str]
    source_schema: int = PROJECT_SCHEMA
    source_container_sha: str | None = None
    source_project_id: str | None = None

    @classmethod
    def create(cls, title: str = "未命名作品") -> "Project":
        return cls(uuid4().hex, 0, {"metadata": {"title": title, "code": ""}}, {}, [])

    def revision(self, key: str) -> str:
        return digest(self.entities.get(key))

    def upgraded_copy(self):
        updated = copy.deepcopy(self)
        if self.source_schema < PROJECT_SCHEMA:
            report = {
                "from_schema": self.source_schema,
                "to_schema": PROJECT_SCHEMA,
                "original_container_sha": self.source_container_sha,
                "original_project_id": self.source_project_id or self.project_id,
                "method": "copy-preserve-resources-v1",
                "legacy_reviews": "recheck-source-context-before-production",
            }
            updated.entities["migration:" + digest(report)] = report
            updated.source_schema = PROJECT_SCHEMA
        return updated

    def encode(self, *, compression=ZIP_DEFLATED) -> bytes:
        if self.source_schema < PROJECT_SCHEMA:
            return self.upgraded_copy().encode(compression=compression)
        from .validation import validate_entities

        validate_entities(self.entities, self.assets)
        for name in self.assets:
            _safe_member(name)
            if name == "manifest.json":
                raise StitchPressError("资源路径与作品清单冲突")
        manifest = {
            "schema_version": PROJECT_SCHEMA,
            "project_id": self.project_id,
            "commit_seq": self.commit_seq,
            "entities": self.entities,
            "events": self.events,
            "assets": {n: sha256(b) for n, b in self.assets.items()},
        }
        output = io.BytesIO()
        with ZipFile(output, "w", compression) as archive:
            archive.writestr("manifest.json", canonical_bytes(manifest))
            for name, data in sorted(self.assets.items()):
                archive.writestr(name, data)
        payload = output.getvalue()
        # Apply the same portability validation before save, not just on reopen.
        self.decode(payload)
        return payload

    @classmethod
    def decode(cls, data: bytes) -> "Project":
        if len(data) > MAX_PROJECT_BYTES:
            raise StitchPressError("作品容器超过试验版256MiB上限")
        try:
            with ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                if (
                    len(infos) > MAX_PROJECT_FILES
                    or sum(i.file_size for i in infos) > MAX_PROJECT_BYTES
                ):
                    raise StitchPressError("作品文件数量/解压大小超限")
                seen = set()
                for info in infos:
                    _safe_member(info.filename)
                    key = unicodedata.normalize("NFC", info.filename).casefold()
                    if key in seen or stat.S_ISLNK(info.external_attr >> 16) or info.is_dir():
                        raise StitchPressError("作品含重复/大小写冲突/链接/目录记录")
                    seen.add(key)
                manifest = json.loads(archive.read("manifest.json"))
                required = {
                    "schema_version",
                    "project_id",
                    "commit_seq",
                    "entities",
                    "events",
                    "assets",
                }
                if not isinstance(manifest, dict) or set(manifest) != required:
                    raise StitchPressError("作品清单含缺失或未知字段，不能不完整写回")
                if type(manifest["schema_version"]) is not int or manifest[
                    "schema_version"
                ] not in {1, PROJECT_SCHEMA}:
                    raise StitchPressError("未来或不支持的作品版本；不能写回旧格式")
                if (
                    not isinstance(manifest["project_id"], str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", manifest["project_id"])
                    or not isinstance(manifest["assets"], dict)
                    or not all(
                        isinstance(n, str)
                        and isinstance(s, str)
                        and re.fullmatch(r"[a-f0-9]{64}", s)
                        for n, s in manifest["assets"].items()
                    )
                    or not isinstance(manifest["events"], list)
                    or not all(isinstance(e, str) and e for e in manifest["events"])
                    or len(set(manifest["events"])) != len(manifest["events"])
                ):
                    raise StitchPressError("作品身份、资源索引或事件记录无效")
                assets = {name: archive.read(name) for name in manifest["assets"]}
                if {i.filename for i in infos} != {"manifest.json", *assets}:
                    raise StitchPressError("作品包含未登记文件")
                for name, content in assets.items():
                    if sha256(content) != manifest["assets"][name]:
                        raise StitchPressError(f"作品资源校验失败：{name}")
                if type(manifest["commit_seq"]) is not int or manifest["commit_seq"] < 0:
                    raise StitchPressError("作品提交序列无效")
                from .validation import validate_entities

                validate_entities(manifest["entities"], assets)
                return cls(
                    manifest["project_id"],
                    manifest["commit_seq"],
                    manifest["entities"],
                    assets,
                    manifest["events"],
                    manifest["schema_version"],
                    sha256(data),
                    manifest["project_id"],
                )
        except StitchPressError:
            raise
        except (
            KeyError,
            TypeError,
            ValueError,
            BadZipFile,
            UnicodeDecodeError,
            RecursionError,
        ) as exc:
            raise StitchPressError("作品容器字段损坏或不完整") from exc


def migrate_project(source: Path, target: Path):
    """Upgrade a legacy file through an independent writer, never overwrite either path."""
    source, target = source.resolve(), target.resolve()
    if source == target or target.exists():
        raise StitchPressError("升级须使用尚不存在的新路径，不能覆盖原作品或其他文件")
    raw = source.read_bytes()
    project = Project.decode(raw)
    if project.source_schema >= PROJECT_SCHEMA:
        raise StitchPressError("作品已是当前格式，无需升级")
    original_id = project.project_id
    project.project_id = uuid4().hex
    writer = Coordinator(target, project)
    try:
        if target.exists():
            raise StitchPressError("升级目标已由其他进程建立，未覆盖")
        writer.commit(expected_seq=project.commit_seq, reads={}, changes={})
        restored = Project.decode(target.read_bytes())
        return {
            "saved": str(target),
            "from_schema": project.source_schema,
            "to_schema": restored.source_schema,
            "original_project_id": original_id,
            "original_sha": sha256(raw),
            "saved_sha": sha256(target.read_bytes()),
            "assets_preserved": restored.assets == project.assets,
            "original_preserved": sha256(source.read_bytes()) == sha256(raw),
            "review": "recheck-source-context-before-production",
        }
    finally:
        writer.close()


class Coordinator:
    """UI and worker results submit commands here, never replace an old project snapshot."""

    def __init__(self, path: Path, project: Project | None = None):
        path = path.resolve()
        self.path = path
        self._mutex = threading.RLock()
        self._lock = exclusive_lock(path.with_suffix(path.suffix + ".lock"))
        self._lock.__enter__()
        try:
            self.project = (
                Project.decode(path.read_bytes()) if path.exists() else copy.deepcopy(project)
            )
            if self.project is None:
                raise StitchPressError("新作品缺少初始数据")
            self._disk_sha = sha256(path.read_bytes()) if path.exists() else None
        except BaseException:
            self._lock.__exit__(None, None, None)
            raise

    def close(self):
        self._lock.__exit__(None, None, None)

    @property
    def history_directory(self):
        return self.path.parent / ".stitchpress-history" / self.project.project_id

    def versions(self):
        return sorted(self.history_directory.glob("*.stitchpress"), reverse=True)

    def commit(
        self,
        *,
        expected_seq: int,
        reads: dict[str, str],
        changes: dict[str, dict],
        assets: dict[str, bytes] | None = None,
        event_id: str | None = None,
        prepared_payload: bytes | None = None,
    ) -> Project:
        with self._mutex:
            if self.project.source_schema < PROJECT_SCHEMA and self._disk_sha is not None:
                raise ConflictError("旧格式作品只读保留；请另存新格式副本，不能原地覆盖迁移")
            if type(expected_seq) is not int or expected_seq < 0:
                raise ConflictError("提交序列须为非负整数")
            disk_sha = sha256(self.path.read_bytes()) if self.path.exists() else None
            if disk_sha != self._disk_sha:
                raise ConflictError("作品被外部修改，已停止写回；请另存当前草稿")
            if event_id and event_id in self.project.events:
                return copy.deepcopy(self.project)
            if expected_seq > self.project.commit_seq:
                raise ConflictError("提交序列来自未来版本")
            if not set(changes) <= set(reads):
                raise StitchPressError("每个拟修改实体必须携带原修订，不能盲写")
            for key, revision in reads.items():
                if revision != self.project.revision(key):
                    raise ConflictError(f"实体或读取依赖已改变：{key}；草稿未丢弃")
            updated = copy.deepcopy(self.project)
            for name, content in (assets or {}).items():
                if name in updated.assets and updated.assets[name] != content:
                    raise ConflictError("不可原地覆盖已保存资源，请创建新内容地址")
                updated.assets[name] = content
            for key, value in copy.deepcopy(changes).items():
                if value is None:
                    updated.entities.pop(key, None)
                else:
                    updated.entities[key] = value
            updated.commit_seq += 1
            if event_id:
                updated.events.append(event_id)
            updated = updated.upgraded_copy()
            if prepared_payload is None:
                payload = updated.encode()
            else:
                prepared = Project.decode(prepared_payload)
                if (
                    prepared.project_id != updated.project_id
                    or prepared.source_schema != PROJECT_SCHEMA
                    or prepared.commit_seq != updated.commit_seq
                    or prepared.events != updated.events
                    or digest(prepared.entities) != digest(updated.entities)
                    or prepared.assets != updated.assets
                ):
                    raise ConflictError("后台保存快照与本次提交不符；草稿未丢弃")
                payload = prepared_payload
            previous = self.path.read_bytes() if self.path.exists() else None
            if (sha256(previous) if previous is not None else None) != self._disk_sha:
                raise ConflictError("校验期间作品被外部修改，已停止保存")
            if previous is not None:
                backup = (
                    self.history_directory
                    / f"{self.project.commit_seq:08d}-{self._disk_sha}.stitchpress"
                )
                if not backup.exists():
                    atomic_write(backup, previous)
            atomic_write(self.path, payload)
            self._disk_sha = sha256(payload)
            self.project = updated
            return copy.deepcopy(updated)

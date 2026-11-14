import copy
import io
import json
import re
import stat
import threading
import unicodedata
from dataclasses import dataclass
from hashlib import sha256 as new_sha256
from pathlib import Path, PurePosixPath
from uuid import uuid4
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from stitchpress.core.errors import ConflictError, StitchPressError
from stitchpress.core.identity import canonical_bytes, digest, sha256, sha256_file
from stitchpress.core.limits import MAX_MANIFEST_BYTES, MAX_PROJECT_BYTES
from stitchpress.platform.files import atomic_copy, atomic_write, exclusive_lock

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

    def validate_contents(self):
        """Check an in-memory draft without manufacturing a throwaway ZIP container."""
        from .validation import validate_entities

        if (
            not isinstance(self.project_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.project_id)
            or type(self.commit_seq) is not int
            or self.commit_seq < 0
            or not isinstance(self.events, list)
            or not all(isinstance(event, str) and event for event in self.events)
            or len(set(self.events)) != len(self.events)
        ):
            raise StitchPressError("作品身份、提交序列或事件记录无效")
        validate_entities(self.entities, self.assets)
        if len(self.assets) + 1 > MAX_PROJECT_FILES:
            raise StitchPressError("作品文件数量/解压大小超限")
        seen, total = {"manifest.json"}, 0
        for name, data in self.assets.items():
            _safe_member(name)
            key = unicodedata.normalize("NFC", name).casefold()
            if key in seen:
                raise StitchPressError("资源路径重复或与作品清单冲突")
            seen.add(key)
            if not isinstance(data, bytes):
                raise StitchPressError("作品资源必须为不可变字节")
            total += len(data)
        if total > MAX_PROJECT_BYTES:
            raise StitchPressError("作品文件数量/解压大小超限")

    def encode(self, *, compression=ZIP_DEFLATED) -> bytes:
        if self.source_schema < PROJECT_SCHEMA:
            return self.upgraded_copy().encode(compression=compression)
        self.validate_contents()
        manifest = self.manifest()
        manifest_bytes = canonical_bytes(manifest)
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise StitchPressError("作品清单超过128MiB上限")
        output = io.BytesIO()
        with ZipFile(output, "w", compression) as archive:
            archive.writestr("manifest.json", manifest_bytes)
            for name, data in sorted(self.assets.items()):
                archive.writestr(name, data)
        payload = output.getvalue()
        self.verify_encoded(payload)
        return payload

    def manifest(self):
        return {
            "schema_version": PROJECT_SCHEMA,
            "project_id": self.project_id,
            "commit_seq": self.commit_seq,
            "entities": self.entities,
            "events": self.events,
            "assets": {n: sha256(b) for n, b in self.assets.items()},
        }

    def verify_encoded(self, payload):
        """Verify exact metadata, every asset SHA and CRC without a duplicate Project."""
        if len(payload) > MAX_PROJECT_BYTES:
            raise StitchPressError("作品压缩容器超过1GiB上限")
        expected = self.manifest()
        with ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            if (
                len(infos) > MAX_PROJECT_FILES
                or sum(i.file_size for i in infos) > MAX_PROJECT_BYTES
            ):
                raise StitchPressError("作品解压内容超过1GiB上限")
            if len({i.filename for i in infos}) != len(infos) or {i.filename for i in infos} != {
                "manifest.json",
                *self.assets,
            }:
                raise ConflictError("后台作品资源清单与保存快照不符")
            for info in infos:
                _safe_member(info.filename)
                if stat.S_ISLNK(info.external_attr >> 16) or info.is_dir():
                    raise StitchPressError("作品含链接或目录记录")
            if archive.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES or (
                archive.read("manifest.json") != canonical_bytes(expected)
            ):
                raise ConflictError("后台作品清单与保存快照不符")
            for name, checksum in expected["assets"].items():
                actual = new_sha256()
                with archive.open(name) as stream:
                    for chunk in iter(lambda: stream.read(1024**2), b""):
                        actual.update(chunk)
                if actual.hexdigest() != checksum:
                    raise ConflictError(f"后台保存资源SHA不符：{name}")

    @classmethod
    def decode(cls, data: bytes) -> "Project":
        if len(data) > MAX_PROJECT_BYTES:
            raise StitchPressError(f"作品容器{len(data) / 1024**2:.1f}MiB，超过1GiB上限")
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
                if archive.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES:
                    raise StitchPressError("作品清单超过128MiB上限")
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
    @classmethod
    def from_loaded(cls, path, project):
        """Adopt a validated worker snapshot only while its source still matches."""
        if not project.source_container_sha:
            raise ConflictError("打开快照缺少原文件校验值")
        owner = cls.__new__(cls)
        owner.path = Path(path).resolve()
        owner._mutex = threading.RLock()
        owner._lock = exclusive_lock(owner.path.with_suffix(owner.path.suffix + ".lock"))
        owner._lock.__enter__()
        try:
            if sha256_file(owner.path) != project.source_container_sha:
                raise ConflictError("作品在后台打开期间被修改，请重新打开；当前草稿未替换")
            owner.project, owner._disk_sha = project, project.source_container_sha
            return owner
        except BaseException:
            owner._lock.__exit__(None, None, None)
            raise

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
            self._disk_sha = self.project.source_container_sha if path.exists() else None
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
            disk_sha = sha256_file(self.path) if self.path.exists() else None
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
                updated.validate_contents()
                updated.verify_encoded(prepared_payload)
                payload = prepared_payload
            if (sha256_file(self.path) if self.path.exists() else None) != self._disk_sha:
                raise ConflictError("校验期间作品被外部修改，已停止保存")
            if self._disk_sha is not None:
                backup = (
                    self.history_directory
                    / f"{self.project.commit_seq:08d}-{self._disk_sha}.stitchpress"
                )
                if not backup.exists():
                    atomic_copy(self.path, backup, self._disk_sha)
            atomic_write(self.path, payload)
            self._disk_sha = sha256(payload)
            self.project = updated
            return copy.deepcopy(updated)

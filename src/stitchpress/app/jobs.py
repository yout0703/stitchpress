"""Frozen local batch queue and independent artifact receipt verification."""

import json
from datetime import UTC, datetime
from pathlib import Path

from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import canonical_bytes, digest, sha256, sha256_file
from stitchpress.platform.files import atomic_write, exclusive_lock

from .context import export_context
from .export import PIPELINE_REVISION, Cancelled, export_package
from .store import Project


def verify_delivery(directory: Path, receipt: dict):
    if not directory.is_dir() or directory.is_symlink():
        raise StitchPressError("交付目录不存在或为链接")
    expected = receipt.get("artifacts", [])
    if not expected or len({item["name"] for item in expected}) != len(expected):
        raise StitchPressError("交付核验清单缺失或重复")
    if len(expected) != receipt.get("expected_files", receipt.get("files")):
        raise StitchPressError("交付回执未包含完整目标清单，不能接受为已完成")
    all_paths = list(directory.rglob("*"))
    if any(p.is_symlink() for p in all_paths):
        raise StitchPressError("交付不能包含符号链接")
    files = {p.relative_to(directory).as_posix(): p for p in all_paths if p.is_file()}
    if set(files) != {item["name"] for item in expected}:
        raise StitchPressError("实际交付文件与核验清单不一致")
    for item in expected:
        path = files[item["name"]]
        if path.stat().st_size != item["bytes"] or sha256_file(path) != item["sha256"]:
            raise StitchPressError(f"交付已改变：{item['name']}；旧审核不能沿用")
    return {"files": len(files), "digital": "passed", "visual": "pending", "factory": "pending"}


def find_receipt(work: Path, destination: Path, request_id=None):
    for path in sorted(
        (p for pattern in ("job-*.json", "post-*.json") for p in work.glob(pattern)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        try:
            record = json.loads(path.read_bytes())
            if Path(record.get("destination", "")) == destination and (
                request_id is None or record.get("request_id") == request_id
            ):
                verify_delivery(destination, record)
                if record.get("status") != "completed":
                    record = {
                        **record,
                        "status": "completed",
                        "files": len(record["artifacts"]),
                        "recovered_publish": True,
                        "original_status": record.get("status"),
                    }
                return record
        except (OSError, ValueError, KeyError, TypeError, StitchPressError):
            continue
    return None


def run_batch(
    manifest_path: Path,
    work: Path,
    *,
    cache: Path | None = None,
    resume=False,
    cancel=lambda: False,
    progress=lambda event: None,
):
    work.mkdir(parents=True, exist_ok=True)
    state_path = work / "queue.json"
    with exclusive_lock(work / "queue.lock"):
        manifest_data = manifest_path.read_bytes()
        if state_path.exists():
            if not resume:
                raise StitchPressError("队列已有记录；请显式恢复，或使用新工作目录")
            state = json.loads(state_path.read_bytes())
            if state.get("manifest_sha") != sha256(manifest_data):
                raise StitchPressError("恢复清单已改变；不能把新输入套在原任务上")
            if state.get("pipeline", PIPELINE_REVISION) != PIPELINE_REVISION and any(
                j["status"] != "completed" for j in state["jobs"]
            ):
                raise StitchPressError(
                    "队列引擎版本已改变；请保留冻结输入并创建新任务，不自动重做旧队列"
                )
        else:
            manifest = json.loads(manifest_data)
            items = manifest.get("items")
            if (
                manifest.get("schema_version") != 1
                or not isinstance(items, list)
                or not 1 <= len(items) <= 100
            ):
                raise StitchPressError("批次须为schema_version=1、1–100个items")
            jobs = []
            destinations = set()
            for index, item in enumerate(items):
                if (
                    set(item) - {"project", "destination", "demo"}
                    or type(item.get("demo", False)) is not bool
                ):
                    raise StitchPressError("批次字段或演示模式无效")
                source = (manifest_path.parent / item["project"]).resolve()
                destination = (manifest_path.parent / item["destination"]).resolve()
                if str(destination).casefold() in destinations or destination.exists():
                    raise StitchPressError("批次目标重复或已存在，请使用新版本目录")
                destinations.add(str(destination).casefold())
                payload = source.read_bytes()
                project = Project.decode(payload)
                export_context(project)
                snapshot = work / "inputs" / f"{index:03}.stitchpress"
                atomic_write(snapshot, payload)
                task_id = digest(
                    {
                        "input_sha": sha256(payload),
                        "destination": str(destination),
                        "demo": item.get("demo", False),
                    }
                )
                jobs.append(
                    {
                        "task_id": task_id,
                        "project_id": project.project_id,
                        "snapshot": str(snapshot.resolve()),
                        "snapshot_sha": sha256(payload),
                        "destination": str(destination),
                        "demo": item.get("demo", False),
                        "status": "queued",
                        "attempts": [],
                    }
                )
            state = {
                "schema_version": 1,
                "pipeline": PIPELINE_REVISION,
                "manifest_sha": sha256(manifest_data),
                "created_at": datetime.now(UTC).isoformat(),
                "status": "queued",
                "jobs": jobs,
            }
            atomic_write(state_path, canonical_bytes(state))
        state["status"] = "running"
        for index, job in enumerate(state["jobs"]):
            if cancel():
                state["status"] = "cancelled"
                break
            destination = Path(job["destination"])
            job_work = work / "jobs" / f"{index:03}"
            if job["status"] == "completed":
                try:
                    verify_delivery(destination, job["receipt"])
                except StitchPressError:
                    job["status"] = "externally_modified"
                    state["status"] = "blocked"
                    atomic_write(state_path, canonical_bytes(state))
                    raise
                continue
            if destination.exists():
                recovered = find_receipt(job_work, destination, job["task_id"])
                if recovered:
                    job.update(status="completed", receipt=recovered, recovered_publish=True)
                    atomic_write(state_path, canonical_bytes(state))
                    continue
                raise StitchPressError("队列目标存在但缺少匹配的完整回执；已停止，未覆盖")
            payload = Path(job["snapshot"]).read_bytes()
            if sha256(payload) != job["snapshot_sha"]:
                raise StitchPressError("队列冻结输入被改写，已停止恢复")
            job["attempts"].append({"started_at": datetime.now(UTC).isoformat()})
            job["status"] = "running"
            atomic_write(state_path, canonical_bytes(state))
            try:
                context = export_context(Project.decode(payload))
                receipt = export_package(
                    **context,
                    destination=destination,
                    work=job_work,
                    demo=job["demo"],
                    cache_root=cache,
                    request_id=job["task_id"],
                    cancel=cancel,
                    progress=lambda event, number=index + 1: progress(
                        {**event, "item": number, "total": len(state["jobs"])}
                    ),
                )
                verify_delivery(destination, receipt)
                job.update(status="completed", receipt=receipt)
                job.pop("error", None)
            except Exception as exc:
                job.update(
                    status="cancelled" if isinstance(exc, Cancelled) else "failed", error=str(exc)
                )
                if isinstance(exc, Cancelled):
                    state["status"] = "cancelled"
            atomic_write(state_path, canonical_bytes(state))
            if state["status"] == "cancelled":
                break
        if state["status"] != "cancelled":
            state["status"] = (
                "completed" if all(j["status"] == "completed" for j in state["jobs"]) else "partial"
            )
        atomic_write(state_path, canonical_bytes(state))
        return state

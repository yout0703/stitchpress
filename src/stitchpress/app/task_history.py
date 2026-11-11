"""Durable process inbox. Inputs are frozen; interrupted remote jobs never auto-submit."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import canonical_bytes, digest
from stitchpress.platform.files import atomic_write, exclusive_lock


@dataclass(frozen=True)
class AIPolicy:
    max_attempts: int = 3
    currency: str = "CNY"
    limit: str | None = None
    reservation: str | None = None

    def __post_init__(self):
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 100:
            raise StitchPressError("每个作品的调用次数上限须1–100")
        if self.currency not in {"CNY", "USD"}:
            raise StitchPressError("计划额度单位仅支持CNY/USD")
        if (self.limit is None) != (self.reservation is None):
            raise StitchPressError("计划总额与每次预留须同时填写或同时留空")
        for value in (self.limit, self.reservation):
            if value is not None:
                try:
                    number = Decimal(value)
                    if not isinstance(value, str) or not number.is_finite() or number <= 0:
                        raise ValueError
                except (ValueError, ArithmeticError) as exc:
                    raise StitchPressError("金额须为正的十进制文本") from exc


def context_fingerprint(window, operation, include_image=True):
    entities = window.draft.entities
    keys = {"design"}
    if operation in {"plan", "preview", "symbol-card"}:
        keys |= {
            "library",
            "mapping",
            "fabric",
            "reference",
            "output_font",
            "metadata",
            "text_layers",
        }
    if operation == "showcase-preview":
        keys.add("showcase")
    if operation == "commerce-preview":
        keys |= {
            "commerce",
            "fabric",
            "metadata",
            "showcase",
            "library",
            "mapping",
            "output_font",
            "input_profile",
        }
    if operation in {"convert", "convert-region"}:
        keys |= {"catalog", "annotations", "input_profile", "conversion_options", "brief"}
    if operation in {"compare-candidate", "migrate-candidate"}:
        keys |= {"catalog", "library", "mapping", "annotations", "source"}
        keys |= {k for k in entities if k.startswith(("candidate:", "revision:", "patch:"))}
    if operation in {"blueprint-import", "blueprint-composite"}:
        keys |= {"brief", "source", "input_profile", "annotations"}
    if operation == "analyze":
        keys |= {"brief", "creative_prompt"}
        if include_image:
            keys.add("input_profile")
    values = {k: entities.get(k) for k in sorted(keys)}
    if include_image:
        from stitchpress.core.identity import sha256

        values["source_sha"] = sha256(window.source_image) if window.source_image else None
    return digest(values)


class TaskHistory:
    def __init__(self, root: Path):
        self.root = root

    def records(self, project_id):
        result = []
        for path in self.root.glob("*/task.json"):
            try:
                item = json.loads(path.read_bytes())
                if item["project_id"] == project_id:
                    output = path.parent / "result.json"
                    if output.exists():
                        response = json.loads(output.read_bytes())
                        item["status"] = response.get("status", "unknown")
                        if response.get("result") and item["operation"] == "analyze":
                            item["usage"] = response["result"].get("usage", {})
                            item["request_id"] = response["result"].get("request_id")
                    elif item["status"] == "running":
                        item["status"] = (
                            "remote_unknown" if item["operation"] == "analyze" else "interrupted"
                        )
                    result.append((path, item))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(result, key=lambda pair: pair[1]["created_at"])

    def begin(self, folder, record, *, policy=None, imported=()):
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(self.root / "history.lock"):
            if record["operation"] == "analyze":
                policy = policy or AIPolicy()
                existing = {
                    item["task_id"]: item
                    for _, item in self.records(record["project_id"])
                    if item["operation"] == "analyze"
                }
                for item in imported:
                    if item["task_id"] not in existing:
                        existing[item["task_id"]] = item
                    elif any(
                        existing[item["task_id"]].get(k) != item.get(k)
                        for k in ("owner", "currency", "reserved", "context_sha")
                    ):
                        raise StitchPressError("导入AI任务与本机记录冲突，不能重置次数或费用")
                if len(existing) >= policy.max_attempts:
                    raise StitchPressError("本作品已达到AI调用次数上限，请在调用计划中明确调整")
                if policy.limit:
                    if any(
                        item.get("currency") != policy.currency or item.get("reserved") is None
                        for item in existing.values()
                    ):
                        raise StitchPressError("历史费用单位或预留未知，须核对账单后再设置金额计划")
                    committed = sum(
                        (
                            Decimal(
                                item["actual"]
                                if item.get("actual") is not None
                                else item["reserved"]
                            )
                            for item in existing.values()
                        ),
                        Decimal(0),
                    )
                    if committed + Decimal(policy.reservation) > Decimal(policy.limit):
                        raise StitchPressError("计划总额（含在途/未知费用预留）不足；未发送请求")
                record.update(
                    currency=policy.currency,
                    reserved=policy.reservation,
                    actual=None,
                    cost="unknown",
                )
            record.update(
                status="running", created_at=datetime.now(UTC).isoformat(), schema_version=1
            )
            atomic_write(folder / "task.json", canonical_bytes(record))
        return record

    @staticmethod
    def finish(folder, status):
        path = folder / "task.json"
        if path.exists():
            record = json.loads(path.read_bytes())
            record.update(status=status, finished_at=datetime.now(UTC).isoformat())
            atomic_write(path, canonical_bytes(record))

    def reconcile(self, path, *, owner, actual, note):
        try:
            amount = Decimal(actual)
            if not amount.is_finite() or amount < 0 or not note.strip():
                raise ValueError
        except (ValueError, ArithmeticError) as exc:
            raise StitchPressError("实际金额须为非负数，并填写账单核对依据") from exc
        with exclusive_lock(self.root / "history.lock"):
            record = json.loads(path.read_bytes())
            if record["owner"] != owner:
                raise StitchPressError("其他设备的未决费用不能由本机自动释放或接管")
            if record["operation"] != "analyze":
                raise StitchPressError("本地任务无需核对模型费用")
            record.setdefault("reconciliations", []).append(
                {
                    "actual": actual,
                    "note": note,
                    "at": datetime.now(UTC).isoformat(),
                    "previous": record.get("actual"),
                }
            )
            record.update(actual=str(amount), cost="human-reconciled")
            atomic_write(path, canonical_bytes(record))
        return record


def validate_usage(record):
    if not isinstance(record, dict) or not isinstance(record.get("tasks"), dict):
        raise StitchPressError("AI用量记录格式无效")
    for task_id, task in record["tasks"].items():
        if task.get("task_id") != task_id or task.get("operation") != "analyze":
            raise StitchPressError("AI任务身份无效")
        for key in ("owner", "project_id", "context_sha", "input_sha", "created_at", "status"):
            if not isinstance(task.get(key), str) or not task[key]:
                raise StitchPressError("AI任务记录缺少归属或输入版本")
        if task.get("currency") not in {"CNY", "USD"}:
            raise StitchPressError("AI历史费用单位无效")
        for key in ("actual", "reserved"):
            value = task.get(key)
            if value is not None:
                try:
                    number = Decimal(value)
                    if not isinstance(value, str) or not number.is_finite() or number < 0:
                        raise ValueError
                except (ValueError, ArithmeticError) as exc:
                    raise StitchPressError("AI历史金额无效，不能释放未决费用") from exc

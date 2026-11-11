"""Explicit local stage sessions. Pausing/resuming never submits a remote request."""

import copy
from datetime import UTC, datetime
from uuid import uuid4

from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import digest, sha256
from stitchpress.core.models import Design, ReviewRecord

from .context import export_context, review_context
from .export import export_snapshot
from .revisions import patch_root

ROUTES = {
    "existing": "沿用当前针格工程",
    "image": "客户来图/干净蓝图",
    "text": "文字创作（可导入外部蓝图）",
}


def begin_session(project, owner, route, *, parent=None):
    if route not in ROUTES or not owner:
        raise StitchPressError("流程入口或执行设备无效")
    return {
        "schema_version": 1,
        "run_id": uuid4().hex,
        "owner": owner,
        "route": route,
        "paused": False,
        "created_at": datetime.now(UTC).isoformat(),
        "parent_run": parent,
        "initial_design_sha": Design.from_dict(project.entities["design"]).content_sha,
        "events": [],
    }


def session_event(session, owner, event, *, input_sha=None):
    if session["owner"] != owner:
        raise StitchPressError("其他设备流程不能直接接管；请创建本机接续流程，保留原任务与费用")
    if event not in {"pause", "resume", "continue", "observe"}:
        raise StitchPressError("未知流程事件")
    updated = copy.deepcopy(session)
    if event == "pause":
        updated["paused"] = True
    if event == "resume":
        updated["paused"] = False
    if event == "continue" and updated["paused"]:
        raise StitchPressError("流程已暂停，请先恢复")
    updated["events"].append(
        {
            "event": event,
            "input_sha": input_sha,
            "at": datetime.now(UTC).isoformat(),
            "owner": owner,
        }
    )
    return updated


def workflow_status(project, owner):
    session = project.entities.get("workflow")
    if not session:
        return {
            "stages": [],
            "next": "begin",
            "paused": False,
            "foreign": False,
            "completed": False,
        }
    if (
        session.get("schema_version") != 1
        or session.get("route") not in ROUTES
        or type(session.get("paused")) is not bool
    ):
        raise StitchPressError("流程记录格式无效")
    design = Design.from_dict(project.entities["design"])
    source = project.entities.get("source")
    source_sha = sha256(project.assets[source["asset"]]) if source else None
    brief = project.entities.get("brief", {})
    stages = []

    def stage(key, label, passed, detail):
        stages.append(
            {
                "key": key,
                "label": label,
                "state": "completed" if passed else "waiting",
                "detail": detail,
            }
        )

    existing = session["route"] == "existing"
    catalog = project.entities.get("catalog")
    catalog_sha = sha256(project.assets[catalog["asset"]]) if catalog else None
    brief_ok = bool(
        brief.get("confirmed")
        and brief.get("source_sha") == source_sha
        and brief.get("catalog_revision") == catalog_sha
    )
    stage(
        "brief", "设计要求/输入路径", existing or brief_ok, "当前入口：" + ROUTES[session["route"]]
    )
    adopted = bool(source and source.get("role") == "human-adopted-clean-blueprint") or bool(
        brief_ok and brief.get("route") == "direct_blueprint"
    )
    stage(
        "blueprint",
        "采用真实蓝图",
        existing or adopted,
        "图像服务待接入；支持直接复用或导入外部蓝图，采用需人工确认",
    )
    root, _ = patch_root(project, design)
    conversion = project.entities.get("active_conversion", {})
    converted = (
        conversion.get("result_sha") == root.content_sha
        and conversion.get("source_sha") == source_sha
    )
    if not converted and design.origin == "patched":
        patch = project.entities.get("patch:" + design.revision_id, {})
        converted = (
            patch.get("result_sha") == design.content_sha
            and patch.get("source", {}).get("source_sha") == source_sha
        )
    stage(
        "convert",
        "本地转换/局部修订",
        existing or converted,
        "候选比较后采用，拒绝不改变当前母版；外区语义独立核对",
    )
    quality = any(
        key.startswith("quality:") and value.get("design_sha") == design.content_sha
        for key, value in project.entities.items()
    )
    stage(
        "quality", "当前母版数字检查", quality, "只认当前SHA的检查；疑点不自动删除，不替代人工审核"
    )
    freeze = project.entities.get("freeze")
    reviewed = False
    if freeze:
        try:
            record = ReviewRecord(
                **{**freeze, "detail_regions": tuple(tuple(r) for r in freeze["detail_regions"])}
            )
            record.validate(design)
            record.validate_context(review_context(project))
            reviewed = True
        except (StitchPressError, TypeError, KeyError):
            pass
    stage("review", "人工审核冻结", reviewed, "整体、两处局部、负空间、来源，记录具名审核")
    current_snapshot = digest(export_snapshot(export_context(project)))
    exports = [
        value
        for key, value in project.entities.items()
        if key.startswith("export:")
        and value.get("status") == "completed"
        and value.get("snapshot", {}).get("design_sha") == design.content_sha
        and not value.get("snapshot", {}).get("demo", True)
        and digest(value.get("snapshot", {})) == current_snapshot
    ]
    full = [
        record
        for record in exports
        if set(record.get("snapshot", {}).get("targets", []))
        == {"production", "reference", "pdf", "counts", "showcase", "notes"}
    ]
    stage(
        "export",
        "完整厂家包",
        bool(full),
        "正式整包生成仍需人工外观和目标软件/工厂验收，演示或补件不算整包完成",
    )
    next_step = next((s["key"] for s in stages if s["state"] != "completed"), None)
    result = {
        "run_id": session["run_id"],
        "route": session["route"],
        "stages": stages,
        "next": next_step,
        "paused": session["paused"],
        "foreign": session["owner"] != owner,
        "completed": next_step is None,
        "design_sha": design.content_sha,
        "state_sha": digest(stages),
        "r1_release": "not-established",
    }
    return result

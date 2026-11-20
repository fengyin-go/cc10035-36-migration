"""语义槽位迁移接口：批次控制、交换数据核对、交接清单与语义入库回执。

这些接口只做参数校验和响应组装，迁移规则集中在
``app.services.slot_migration``，离线脚本复用同一个服务。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services import slot_codec
from app.services.slot_migration import (
    ACTION_ALIASES,
    MigrationError,
    pipeline,
)

router = APIRouter(prefix="/api/nlu-slot-migration", tags=["语义槽位迁移"])


class BatchPayload(BaseModel):
    batch_id: str | None = None
    name: str | None = None


class MigratePayload(BaseModel):
    limit: int | None = Field(default=None, ge=1)


class IngestPayload(BaseModel):
    run_id: str | None = None


class ActionRequest(BaseModel):
    action: str


def _run(operation):
    try:
        return operation()
    except MigrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except slot_codec.SlotCodecError as exc:
        raise HTTPException(status_code=422, detail=exc.as_dict()) from exc


@router.get("/state")
def migration_state() -> dict[str, Any]:
    """当前批次状态、槽位状态分布与待补正摘要。"""
    return pipeline.state()


@router.get("/exchanges")
def list_exchanges(
    status: str | None = Query(default=None),
    source_sentiment_id: str | None = Query(default=None),
    semantic_id: str | None = Query(default=None),
    version: str | None = Query(default=None),
) -> dict[str, Any]:
    """按来源/版本/状态核对带版本的槽位交换数据。"""
    items = pipeline.list_exchanges(
        status=status,
        source_sentiment_id=source_sentiment_id,
        semantic_id=semantic_id,
        version=version,
    )
    return {"items": items, "total": len(items)}


@router.get("/exchanges/{exchange_id}")
def get_exchange(exchange_id: str) -> dict[str, Any]:
    doc = pipeline.find_exchange(exchange_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"交换记录 {exchange_id} 不存在")
    ok, message = slot_codec.verify_exchange(doc)
    return {"exchange": doc, "integrity_ok": ok, "integrity_message": message}


@router.get("/exchanges/{exchange_id}/legacy")
def read_exchange_as_legacy(exchange_id: str) -> dict[str, Any]:
    """按旧版本读取已核对的原值，用于切换前验证与回滚预演。"""
    payload = pipeline.exchange_to_legacy(exchange_id)
    if not payload.get("ok"):
        raise HTTPException(status_code=404, detail=payload.get("reason", "交换记录不存在"))
    return payload


@router.get("/legacy/nlu/{entry_id}")
def read_nlu_entry_as_legacy(entry_id: int) -> dict[str, Any]:
    """旧读取端入口：按语义记录ID读取已核对的旧版槽位原值。"""
    from app.services.nlu_result import NluResultService

    payload = NluResultService().read_legacy_slots(entry_id)
    if not payload.get("ok"):
        raise HTTPException(status_code=404, detail=payload.get("reason", "语义记录不存在"))
    return payload


@router.get("/manifest")
def handover_manifest(status: str | None = Query(default=None)) -> dict[str, Any]:
    """标注交接清单：按情感来源定位已转换槽位与仍要补正的记录。"""
    items = pipeline.manifest(status=status)
    return {
        "items": items,
        "total": len(items),
        "pending_correction": [item for item in items if item["needs_correction"]],
        "converted": [item for item in items if not item["needs_correction"]],
    }


@router.post("/batch", response_model=dict)
def start_batch(payload: BatchPayload) -> dict[str, Any]:
    batch = _run(lambda: pipeline.begin(payload.batch_id, payload.name or ""))
    return {"ok": True, "batch": batch}


@router.post("/migrate", response_model=dict)
def migrate(payload: MigratePayload) -> dict[str, Any]:
    result = _run(lambda: pipeline.migrate(limit=payload.limit))
    return {"ok": True, **result}


@router.post("/action", response_model=ActionResult)
def run_batch_action(payload: ActionRequest) -> ActionResult:
    action = ACTION_ALIASES.get(payload.action.strip())
    if action is None:
        return ActionResult(ok=False, message=f"动作「{payload.action}」不属于迁移流水线")
    if action == "start":
        batch = _run(lambda: pipeline.begin())
        return ActionResult(ok=True, message="迁移批次已开始", entry=batch)
    if action == "migrate":
        result = _run(lambda: pipeline.migrate())
        return ActionResult(
            ok=True,
            message=f"已迁移 {len(result['migrated'])} 条，剩余 {result['remaining']} 条",
            entry=result,
        )
    if action == "pause":
        batch = _run(pipeline.pause)
        return ActionResult(ok=True, message="迁移已暂停", entry=batch)
    if action == "resume":
        batch = _run(pipeline.resume)
        return ActionResult(ok=True, message="迁移已恢复", entry=batch)
    if action == "rollback":
        batch = _run(pipeline.rollback)
        return ActionResult(ok=True, message="迁移已回滚，原值已还原", entry=batch)
    if action == "reset":
        batch = _run(pipeline.reset)
        return ActionResult(ok=True, message="迁移状态已重置", entry=batch)
    if action == "rehearse":
        summary = _run(pipeline.rehearsal_summary)
        return ActionResult(ok=True, message="演练报告已生成", entry=summary)
    return ActionResult(ok=False, message="未实现的动作")


@router.post("/ingest")
def ingest(payload: IngestPayload) -> dict[str, Any]:
    run = pipeline.ingest(run_id=payload.run_id)
    return {"ok": True, **run}


@router.get("/ingest/runs")
def ingest_runs() -> dict[str, Any]:
    """语义入库实际接收了哪些槽位、用了哪版输入、哪些须回情感标注补正。"""
    report = pipeline.acceptance_report()
    return {
        "runs": report["runs"],
        "active_acceptances": report["active_acceptances"],
        "revoked_acceptances": report["revoked_acceptances"],
        "active_by_input_version": report["active_by_input_version"],
    }

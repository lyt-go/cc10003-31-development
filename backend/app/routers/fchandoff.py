"""预测交接接口：部署固化口径、分析消费已部署版本、审计查询与交接包文件流转。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.fchandoff import STALE_BADGE, handoff_service

router = APIRouter(prefix="/api/fchandoff", tags=["预测交接"])

service = handoff_service


@router.get("/envelopes", response_model=PageResult[dict])
def list_envelopes(
    status: str | None = Query(default=None, description="deployed、superseded"),
    target_entity: str | None = Query(default=None, description="按预测目标过滤"),
) -> PageResult[dict]:
    """交接信封列表：每一封都是部署时固化的预测口径（窗口、特征、模型版本、置信水平）。"""
    items = service.list_envelopes(status=status, target_entity=target_entity)
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.get("/envelopes/{envelope_id}", response_model=dict)
def get_envelope(envelope_id: str) -> dict:
    """单个信封明细：分析输出按 envelope_id 回溯模型版本、窗口与置信水平。"""
    envelope = service.get_envelope(envelope_id)
    if envelope is None:
        raise HTTPException(status_code=404, detail=f"交接信封 {envelope_id} 不存在")
    return envelope


@router.post("/deploy", response_model=ActionResult)
def deploy(payload: EntryPayload) -> ActionResult:
    """部署固化口径：同一预测目标+窗口当前部署唯一；重复部署同一版本只生效一次。"""
    envelope, message, code = service.deploy(payload.values)
    if envelope is None:
        return ActionResult(ok=False, message=message, entry={"code": code, "badge": STALE_BADGE})
    return ActionResult(ok=True, message=message, entry=envelope)


@router.post("/retrain", response_model=ActionResult)
def retrain(payload: EntryPayload) -> ActionResult:
    """重训：成功则部署新版本（旧版转历史）；训练失败或窗口不完整只标记受影响输出。"""
    values = payload.values
    target = str(values.get("target_entity") or "").strip()
    window = values.get("forecast_window_days")
    new_version = str(values.get("new_model_version") or "").strip()
    if not target or window in (None, "") or not new_version:
        return ActionResult(ok=False, message="重训需要 target_entity、forecast_window_days、new_model_version")
    envelope, message, code = service.retrain(
        target_entity=target,
        forecast_window_days=int(window),
        new_model_version=new_version,
        training_ok=bool(values.get("training_ok", True)),
        window_complete=bool(values.get("window_complete", True)),
        overrides=values.get("overrides") if isinstance(values.get("overrides"), dict) else None,
    )
    if envelope is None:
        return ActionResult(ok=False, message=message, entry={"code": code})
    return ActionResult(ok=True, message=message, entry=envelope)


@router.get("/outputs", response_model=PageResult[dict])
def list_outputs(
    analysis_plan: str | None = Query(default=None, description="按分析方案编号过滤"),
    affected: bool | None = Query(default=None, description="只看被训练失败/窗口不完整标记的输出"),
) -> PageResult[dict]:
    """分析输出列表：每条都带预测结果、置信水平、对比基线与模型版本。"""
    items = service.list_outputs(analysis_plan=analysis_plan, affected=affected)
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.post("/outputs", response_model=ActionResult)
def consume(payload: EntryPayload) -> ActionResult:
    """分析消费：只接受已部署信封；同一分析方案+信封+预测日期只生效一次。"""
    values = payload.values
    output, message, code = service.consume(
        analysis_plan=str(values.get("analysis_plan") or "").strip(),
        envelope_id=str(values.get("envelope_id") or "").strip(),
        forecast_date=str(values.get("forecast_date") or "").strip(),
        forecast_value=values.get("forecast_value"),
        baseline_value=values.get("baseline_value"),
        output_kind=str(values.get("output_kind") or "backtest"),
    )
    if output is None:
        return ActionResult(ok=False, message=message, entry={"code": code, "badge": STALE_BADGE})
    return ActionResult(ok=True, message=message, entry=output)


@router.get("/audit", response_model=PageResult[dict])
def list_audit() -> PageResult[dict]:
    """审计事件：deploy、supersede、reject 三类，换版全程留痕。"""
    items = service.list_audit()
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.get("/export")
def export_package() -> dict[str, Any]:
    """导出交接包：信封、分析输出（含预测窗口与置信水平）、审计一起打包下载。"""
    return service.export_package()


@router.post("/reload", response_model=ActionResult)
def reload_package(payload: EntryPayload) -> ActionResult:
    """重新载入交接包：模型版本与样本范围原样保留，已存在的记录跳过不覆盖。"""
    package = payload.values.get("package") if isinstance(payload.values.get("package"), dict) else payload.values
    summary, message = service.reload_package(package)
    return ActionResult(ok=True, message=message, entry=summary)

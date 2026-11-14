"""预测模型业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.fchandoff import handoff_service
from app.store import store

MODULE = "forecast"
REQUIRED_FIELDS = ["方案编号", "预测目标", "算法名称"]
OPTIONAL_FIELDS = ["特征字段", "训练周期", "预测窗口天", "置信水平", "模型版本"]
STATUS_ORDER = ["草稿", "模型评估中", "已部署", "已下线"]
ACTION_RULES = {"提交模型评估": "模型评估中", "部署模型": "已部署", "下线模型": "已下线"}
NEGATIVE_ACTIONS = ["下线模型"]


class ForecastService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("方案编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry.update({field: values.get(field) for field in OPTIONAL_FIELDS if values.get(field) is not None})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"预测方案 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于预测模型可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        handoff_message = ""
        if action == "部署模型":
            # 部署即固化口径：预测窗口、特征字段、模型版本、置信水平写入交接信封
            envelope, handoff_message, _code = handoff_service.deploy({
                "forecast_plan": entry.get("方案编号"),
                "target_entity": entry.get("预测目标"),
                "algorithm_label": entry.get("算法名称"),
                "feature_set": entry.get("特征字段"),
                "train_cycle_days": entry.get("训练周期"),
                "forecast_window_days": entry.get("预测窗口天"),
                "confidence_level": entry.get("置信水平"),
                "model_version": entry.get("模型版本"),
            })
            if envelope is None:
                return None, handoff_message
            entry["交接信封"] = envelope["envelope_id"]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        suffix = f"；{handoff_message}" if handoff_message else ""
        return entry, f"预测方案已{action}{suffix}"

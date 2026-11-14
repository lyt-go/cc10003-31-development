"""预测到分析的版本链服务：部署固化口径、分析只读已部署版本、换版写审计。

版本边界（依据预测到分析的现有链路）：
- 预测方案「部署模型」时，把预测窗口、特征字段、模型版本、置信水平固化成交接信封
  （handoff_envelope），信封是下游唯一的口径来源；
- 分析方案「启用方案」/生成输出时只能引用 status=deployed 的信封（deployed_revision），
  输出里记录预测结果、置信水平与对比基线；
- 重训产生新版本信封，旧信封转 superseded 保留，旧预测结果不丢；
- 训练失败或窗口不完整时不建新版，只标记受影响输出，不覆盖上一版；
- 同一预测目标+预测窗口的当前部署唯一，重复部署/重复启用只生效一次；
- deploy / supersede / reject 三类事件全部写审计。
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone
from typing import Any

from app.store import store

ENVELOPE_TABLE = "fc_envelope"
OUTPUT_TABLE = "fc_output"
AUDIT_TABLE = "fc_audit"

REJECT_UNDEPLOYED = "UNDEPLOYED_MODEL_31"  # 引用未部署模型的拒绝码
STALE_BADGE = "NOT_ALLOWED_31"  # 未部署引用对应的徽标

# 部署时必须固化的口径字段（缺一则拒绝建版），键对应信封字段，值是可读名称
CALIBER_FIELDS = {
    "target_entity": "预测目标",
    "algorithm_label": "算法名称",
    "feature_set": "特征字段",
    "forecast_window_days": "预测窗口天",
    "confidence_level": "置信水平",
    "model_version": "模型版本",
}

_deploy_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _short_sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def deployed_badge(model_version: Any) -> str:
    """model-v8 -> DEPLOYED_V8，信封与分析输出共用同一套徽标口径。"""
    suffix = str(model_version or "").strip().upper().replace("MODEL-", "")
    return f"DEPLOYED_{suffix}"


def confidence_interval_label(confidence_level: Any) -> str:
    """0.95 -> interval_95，置信水平的区间标签。"""
    try:
        pct = int(round(float(confidence_level) * 100))
    except (TypeError, ValueError):
        pct = 0
    return f"interval_{pct}"


class HandoffService:
    """预测交接版本链的全部业务规则；路由层不做业务判断。"""

    # ---------- 查询 ----------
    def list_envelopes(
        self,
        *,
        status: str | None = None,
        target_entity: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = store.rows(ENVELOPE_TABLE)
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if target_entity:
            rows = [row for row in rows if row.get("target_entity") == target_entity]
        return list(rows)

    def list_outputs(
        self,
        *,
        analysis_plan: str | None = None,
        affected: bool | None = None,
    ) -> list[dict[str, Any]]:
        rows = store.rows(OUTPUT_TABLE)
        if analysis_plan:
            rows = [row for row in rows if row.get("analysis_plan") == analysis_plan]
        if affected is not None:
            rows = [row for row in rows if bool(row.get("affected")) is affected]
        return list(rows)

    def list_audit(self) -> list[dict[str, Any]]:
        return list(store.rows(AUDIT_TABLE))

    def get_envelope(self, envelope_id: str) -> dict[str, Any] | None:
        for row in store.rows(ENVELOPE_TABLE):
            if row.get("envelope_id") == envelope_id:
                return row
        return None

    def current_envelope(self, target_entity: str, forecast_window_days: int) -> dict[str, Any] | None:
        """同一预测目标+窗口的当前部署（deployed_revision）唯一。"""
        for row in store.rows(ENVELOPE_TABLE):
            if (
                row.get("target_entity") == target_entity
                and int(row.get("forecast_window_days", 0)) == int(forecast_window_days)
                and row.get("status") == "deployed"
            ):
                return row
        return None

    def deployed_for_plan(self, forecast_plan: str) -> dict[str, Any] | None:
        for row in store.rows(ENVELOPE_TABLE):
            if row.get("forecast_plan") == forecast_plan and row.get("status") == "deployed":
                return row
        return None

    # ---------- 部署：固化预测口径 ----------
    def deploy(
        self,
        caliber: dict[str, Any],
        *,
        actor: str = "forecast-ops",
    ) -> tuple[dict[str, Any] | None, str, str | None]:
        normalized, error = self._normalize_caliber(caliber)
        if normalized is None:
            self._audit("reject", None, caliber.get("model_version"), actor, f"口径不完整，拒绝部署：{error}")
            return None, f"预测口径不完整，拒绝部署：{error}", "CALIBER_INCOMPLETE"

        target = normalized["target_entity"]
        window = normalized["forecast_window_days"]
        version = normalized["model_version"]

        with _deploy_lock:
            current = self.current_envelope(target, window)
            if current and current.get("model_version") == version:
                # 同一目标+窗口的当前模型唯一；重复部署同一版本只生效一次
                return current, f"{target}/{window}天窗口当前已部署 {version}，重复部署不重复建版", None
            if current:
                current["status"] = "superseded"
                current["superseded_at"] = _now()
                self._audit(
                    "supersede",
                    current,
                    current.get("model_version"),
                    actor,
                    f"{version} 部署，{current.get('model_version')} 转为历史版本，旧预测结果保留",
                )
            envelope = self._build_envelope(normalized, actor)
            store.rows(ENVELOPE_TABLE).append(envelope)
            self._audit(
                "deploy",
                envelope,
                version,
                actor,
                f"部署 {version}：固化窗口 {window} 天、特征 {envelope['feature_set']}、置信水平 {envelope['confidence_level']}",
            )
        return envelope, f"已部署 {version}，口径固化到 {envelope['envelope_id']}", None

    # ---------- 重训：成功则换版，失败只标记受影响输出 ----------
    def retrain(
        self,
        *,
        target_entity: str,
        forecast_window_days: int,
        new_model_version: str,
        training_ok: bool = True,
        window_complete: bool = True,
        overrides: dict[str, Any] | None = None,
        actor: str = "forecast-ops",
    ) -> tuple[dict[str, Any] | None, str, str | None]:
        current = self.current_envelope(target_entity, int(forecast_window_days))
        if not training_ok or not window_complete:
            reason = "训练失败" if not training_ok else "训练窗口不完整"
            code = "TRAINING_FAILED" if not training_ok else "WINDOW_INCOMPLETE"
            affected = self.mark_affected(current, reason) if current else 0
            self._audit(
                "reject",
                current,
                new_model_version,
                actor,
                f"{reason}：{new_model_version} 未建版，标记 {affected} 条受影响输出，上一版继续生效",
            )
            keep = current.get("model_version") if current else "无"
            return None, f"{reason}，已标记 {affected} 条受影响输出，上一版 {keep} 继续生效", code

        if current is None and not overrides:
            return None, f"{target_entity}/{forecast_window_days}天窗口没有基线口径，重训需要完整口径", "CALIBER_INCOMPLETE"
        caliber = dict(current or {})
        caliber.update(overrides or {})
        caliber["target_entity"] = target_entity
        caliber["forecast_window_days"] = int(forecast_window_days)
        caliber["model_version"] = new_model_version
        for transient in ("id", "envelope_id", "case_id", "status", "badge", "revision", "deployed_at", "superseded_at"):
            caliber.pop(transient, None)
        return self.deploy(caliber, actor=actor)

    def mark_affected(self, envelope: dict[str, Any] | None, reason: str) -> int:
        """训练失败/窗口不完整时只打标记，不改写上一版的任何输出数值。"""
        if envelope is None:
            return 0
        count = 0
        for row in store.rows(OUTPUT_TABLE):
            if row.get("envelope_id") == envelope.get("envelope_id") and not row.get("affected"):
                row["affected"] = True
                row["affected_reason"] = reason
                row["affected_at"] = _now()
                count += 1
        return count

    # ---------- 分析消费：只读已部署版本 ----------
    def consume(
        self,
        *,
        analysis_plan: str,
        envelope_id: str,
        forecast_date: str,
        forecast_value: Any,
        baseline_value: Any,
        output_kind: str = "backtest",
        actor: str = "analytics",
    ) -> tuple[dict[str, Any] | None, str, str | None]:
        envelope = self.get_envelope(envelope_id)
        if envelope is None or envelope.get("status") != "deployed":
            self._audit(
                "reject",
                envelope,
                (envelope or {}).get("model_version"),
                actor,
                f"分析方案 {analysis_plan} 引用未部署模型 {envelope_id}，徽标 {STALE_BADGE}",
            )
            return None, f"模型 {envelope_id} 不是已部署版本，分析方案只能引用已部署模型（{REJECT_UNDEPLOYED}）", REJECT_UNDEPLOYED

        for label, value in (("预测日期", forecast_date), ("预测结果", forecast_value), ("对比基线", baseline_value)):
            if value is None or str(value).strip() == "":
                return None, f"分析输出缺少{label}", "OUTPUT_INCOMPLETE"

        rows = store.rows(OUTPUT_TABLE)
        for row in rows:
            if (
                row.get("analysis_plan") == analysis_plan
                and row.get("envelope_id") == envelope_id
                and row.get("forecast_date") == forecast_date
            ):
                return row, "重复启用只生效一次，返回已存在的分析输出", None

        output = {
            "id": _next_id(rows),
            "output_id": f"OUT-{envelope['target_entity']}-{str(forecast_date).replace('-', '')}-{_next_id(rows)}",
            "analysis_plan": analysis_plan,
            "envelope_id": envelope["envelope_id"],
            "model_version": envelope["model_version"],
            "revision": envelope["revision"],
            "output_kind": output_kind,
            "forecast_date": forecast_date,
            "forecast_value": forecast_value,
            "confidence": envelope["confidence_level"],
            "confidence_interval": envelope["confidence_interval"],
            "baseline_value": baseline_value,
            "forecast_window_days": envelope["forecast_window_days"],
            "feature_snapshot_sha": envelope["feature_snapshot_sha"],
            "sample_range": dict(envelope.get("sample_range") or {}),
            "badge": deployed_badge(envelope["model_version"]),
            "affected": False,
            "affected_reason": None,
            "created_at": _now(),
        }
        rows.append(output)
        return output, "分析输出已生成，记录预测结果、置信水平与对比基线", None

    # ---------- 文件流转：打包下载与重新载入 ----------
    def export_package(self) -> dict[str, Any]:
        """交接包：预测窗口、置信水平随输出一起打包，可直接下载流转。"""
        return {
            "module": "fchandoff",
            "package_kind": "forecast_analysis_handoff",
            "exported_at": _now(),
            "envelopes": [dict(row) for row in store.rows(ENVELOPE_TABLE)],
            "outputs": [dict(row) for row in store.rows(OUTPUT_TABLE)],
            "audit": [dict(row) for row in store.rows(AUDIT_TABLE)],
        }

    def reload_package(self, package: dict[str, Any]) -> tuple[dict[str, int], str]:
        """重新载入：模型版本与样本范围原样保留；已存在的记录跳过，不覆盖现状。"""
        envelopes = store.rows(ENVELOPE_TABLE)
        outputs = store.rows(OUTPUT_TABLE)
        known_envelopes = {row.get("envelope_id") for row in envelopes}
        known_outputs = {row.get("output_id") for row in outputs}
        summary = {"envelopes_loaded": 0, "envelopes_skipped": 0, "outputs_loaded": 0, "outputs_skipped": 0}

        for item in package.get("envelopes", []):
            if item.get("envelope_id") in known_envelopes:
                summary["envelopes_skipped"] += 1
                continue
            row = dict(item)
            row["id"] = _next_id(envelopes)
            envelopes.append(row)
            known_envelopes.add(row.get("envelope_id"))
            summary["envelopes_loaded"] += 1

        for item in package.get("outputs", []):
            if item.get("output_id") in known_outputs:
                summary["outputs_skipped"] += 1
                continue
            row = dict(item)  # model_version / sample_range / 置信水平原样保留
            row["id"] = _next_id(outputs)
            outputs.append(row)
            known_outputs.add(row.get("output_id"))
            summary["outputs_loaded"] += 1

        message = (
            f"交接包重新载入完成：新增信封 {summary['envelopes_loaded']} 条、输出 {summary['outputs_loaded']} 条，"
            f"跳过已存在 {summary['envelopes_skipped'] + summary['outputs_skipped']} 条，模型版本与样本范围原样保留"
        )
        return summary, message

    # ---------- 内部 ----------
    def _normalize_caliber(self, caliber: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        missing = [label for key, label in CALIBER_FIELDS.items() if not str(caliber.get(key) or "").strip()]
        if missing:
            return None, f"缺少 {'、'.join(missing)}"
        normalized = dict(caliber)
        try:
            window = int(str(caliber.get("forecast_window_days")).strip())
            if window <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return None, "预测窗口天必须是正整数"
        try:
            confidence = float(str(caliber.get("confidence_level")).strip())
            if not 0 < confidence <= 1:
                raise ValueError
        except (TypeError, ValueError):
            return None, "置信水平必须是 0~1 之间的小数"
        normalized["forecast_window_days"] = window
        normalized["confidence_level"] = confidence
        normalized["target_entity"] = str(caliber.get("target_entity")).strip()
        normalized["model_version"] = str(caliber.get("model_version")).strip()
        normalized["feature_set"] = str(caliber.get("feature_set")).strip()
        normalized["algorithm_label"] = str(caliber.get("algorithm_label")).strip()
        return normalized, None

    def _build_envelope(self, caliber: dict[str, Any], actor: str) -> dict[str, Any]:
        rows = store.rows(ENVELOPE_TABLE)
        target = caliber["target_entity"]
        window = caliber["forecast_window_days"]
        version = caliber["model_version"]
        revision = max(
            (int(row.get("revision", 0)) for row in rows if row.get("target_entity") == target and int(row.get("forecast_window_days", 0)) == window),
            default=0,
        ) + 1
        version_suffix = version.lower().replace("model-", "")
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        artifact = str(caliber.get("source_artifact_uri") or f"forecast_{version_suffix}_{today}")
        return {
            "id": _next_id(rows),
            "envelope_id": str(caliber.get("envelope_id") or f"analysis_handoff_{version_suffix}"),
            "case_id": str(caliber.get("case_id") or f"FC-HANDOFF-{today}-{_short_sha(artifact + version)[:4].upper()}"),
            "forecast_plan": caliber.get("forecast_plan"),
            "target_entity": target,
            "algorithm_label": caliber["algorithm_label"],
            "feature_set": caliber["feature_set"],
            "feature_snapshot_sha": str(caliber.get("feature_snapshot_sha") or _short_sha(caliber["feature_set"])),
            "forecast_window_days": window,
            "train_cycle_days": caliber.get("train_cycle_days"),
            "confidence_level": caliber["confidence_level"],
            "confidence_interval": confidence_interval_label(caliber["confidence_level"]),
            "model_version": version,
            "trained_at": str(caliber.get("trained_at") or _now()),
            "deploy_marker": str(caliber.get("deploy_marker") or f"release_{today}"),
            "source_artifact_uri": artifact,
            "artifact_sha256": str(caliber.get("artifact_sha256") or hashlib.sha256(artifact.encode("utf-8")).hexdigest()[:12]),
            "checksum": str(caliber.get("checksum") or f"artifact_{_short_sha(artifact)}"),
            "source_stream": caliber.get("source_stream"),
            "consumer_scope": caliber.get("consumer_scope"),
            "sample_range": dict(caliber.get("sample_range") or {}),
            "revision": revision,
            "status": "deployed",
            "badge": deployed_badge(version),
            "deployed_at": _now(),
            "deployed_by": actor,
            "superseded_at": None,
        }

    def _audit(
        self,
        event: str,
        envelope: dict[str, Any] | None,
        model_version: Any,
        actor: str,
        detail: str,
    ) -> None:
        rows = store.rows(AUDIT_TABLE)
        rows.append({
            "id": _next_id(rows),
            "event": event,  # deploy / supersede / reject
            "envelope_id": (envelope or {}).get("envelope_id"),
            "model_version": model_version,
            "actor": actor,
            "at": _now(),
            "detail": detail,
        })


handoff_service = HandoffService()

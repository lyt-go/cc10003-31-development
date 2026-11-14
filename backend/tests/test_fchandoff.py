"""预测到分析版本链的验收测试。

覆盖口径：部署固化、唯一当前部署、幂等启用、未部署拒绝、重训换版、
训练失败/窗口不完整只标记、文件流转保留模型版本与样本范围、审计留痕。

运行：cd backend && .venv/bin/python -m unittest tests.test_fchandoff -v
"""
from __future__ import annotations

import copy
import unittest

from app.seed import SEED_ROWS
from app.services.analysis import AnalysisService
from app.services.fchandoff import REJECT_UNDEPLOYED, STALE_BADGE, HandoffService
from app.services.forecast import ForecastService
from app.store import store

V8 = "analysis_handoff_v8"
V7 = "analysis_handoff_v7"


def reset_store() -> None:
    store._tables = {name: [copy.deepcopy(row) for row in rows] for name, rows in SEED_ROWS.items()}


def v9_caliber() -> dict:
    return {
        "forecast_plan": "FORE-GMV_DAILY",
        "target_entity": "GMV_DAILY",
        "algorithm_label": "Prophet",
        "feature_set": "channel,discount,campaign",
        "forecast_window_days": 14,
        "train_cycle_days": 7,
        "confidence_level": 0.95,
        "model_version": "model-v9",
    }


class DeployCaliberCase(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.svc = HandoffService()

    def test_seed_v8_is_current_deployed(self) -> None:
        current = self.svc.current_envelope("GMV_DAILY", 14)
        self.assertIsNotNone(current)
        self.assertEqual(current["envelope_id"], V8)
        self.assertEqual(current["model_version"], "model-v8")
        self.assertEqual(current["badge"], "DEPLOYED_V8")
        self.assertEqual(current["confidence_interval"], "interval_95")
        self.assertEqual(current["feature_snapshot_sha"], "8d3a551c")

    def test_deploy_freezes_caliber_and_supersedes(self) -> None:
        envelope, message, code = self.svc.deploy(v9_caliber())
        self.assertIsNotNone(envelope, message)
        self.assertIsNone(code)
        # 口径固化：窗口、特征、模型版本、置信水平都落在信封上
        self.assertEqual(envelope["forecast_window_days"], 14)
        self.assertEqual(envelope["feature_set"], "channel,discount,campaign")
        self.assertEqual(envelope["model_version"], "model-v9")
        self.assertEqual(envelope["confidence_level"], 0.95)
        self.assertEqual(envelope["confidence_interval"], "interval_95")
        self.assertEqual(envelope["status"], "deployed")
        self.assertEqual(envelope["badge"], "DEPLOYED_V9")
        self.assertEqual(envelope["revision"], 9)
        # 旧版本转历史，不删除
        old = self.svc.get_envelope(V8)
        self.assertEqual(old["status"], "superseded")
        self.assertIsNotNone(old["superseded_at"])
        # 审计同时记录 supersede 与 deploy
        events = [(row["event"], row["model_version"]) for row in self.svc.list_audit()]
        self.assertIn(("supersede", "model-v8"), events)
        self.assertIn(("deploy", "model-v9"), events)

    def test_deploy_same_version_is_idempotent(self) -> None:
        before = len(self.svc.list_envelopes())
        caliber = v9_caliber()
        caliber["model_version"] = "model-v8"
        envelope, message, _ = self.svc.deploy(caliber)
        self.assertEqual(envelope["envelope_id"], V8)
        self.assertIn("重复部署", message)
        self.assertEqual(len(self.svc.list_envelopes()), before)

    def test_single_current_per_target_and_window(self) -> None:
        self.svc.deploy(v9_caliber())
        deployed = [row for row in self.svc.list_envelopes(target_entity="GMV_DAILY") if row["status"] == "deployed" and row["forecast_window_days"] == 14]
        self.assertEqual(len(deployed), 1)
        self.assertEqual(deployed[0]["model_version"], "model-v9")

    def test_deploy_rejects_incomplete_caliber(self) -> None:
        caliber = v9_caliber()
        del caliber["confidence_level"]
        envelope, message, code = self.svc.deploy(caliber)
        self.assertIsNone(envelope)
        self.assertEqual(code, "CALIBER_INCOMPLETE")
        self.assertIn("置信水平", message)
        self.assertEqual(self.svc.list_audit()[-1]["event"], "reject")


class ConsumeCase(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.svc = HandoffService()

    def test_consume_records_result_confidence_baseline(self) -> None:
        output, message, code = self.svc.consume(
            analysis_plan="ANAL-GMV_TREND",
            envelope_id=V8,
            forecast_date="2026-10-16",
            forecast_value=1291000,
            baseline_value=1203000,
        )
        self.assertIsNotNone(output, message)
        self.assertEqual(output["forecast_value"], 1291000)
        self.assertEqual(output["confidence"], 0.95)
        self.assertEqual(output["baseline_value"], 1203000)
        self.assertEqual(output["model_version"], "model-v8")
        self.assertEqual(output["badge"], "DEPLOYED_V8")

    def test_output_traces_back_to_envelope(self) -> None:
        # 验收口径：从分析输出能调到模型版本、窗口和置信水平
        output = self.svc.list_outputs(analysis_plan="ANAL-GMV_TREND")[1]
        envelope = self.svc.get_envelope(output["envelope_id"])
        self.assertEqual(envelope["model_version"], output["model_version"])
        self.assertEqual(envelope["forecast_window_days"], output["forecast_window_days"])
        self.assertEqual(envelope["confidence_level"], output["confidence"])
        self.assertEqual(envelope["feature_snapshot_sha"], output["feature_snapshot_sha"])
        self.assertEqual(output["forecast_value"], 1284000)
        self.assertEqual(output["baseline_value"], 1198000)

    def test_consume_rejects_undeployed_model(self) -> None:
        for envelope_id in (V7, "analysis_handoff_v31"):
            output, message, code = self.svc.consume(
                analysis_plan="ANAL-GMV_TREND",
                envelope_id=envelope_id,
                forecast_date="2026-10-17",
                forecast_value=1,
                baseline_value=1,
            )
            self.assertIsNone(output)
            self.assertEqual(code, REJECT_UNDEPLOYED)
            self.assertIn(REJECT_UNDEPLOYED, message)
        audit = self.svc.list_audit()
        self.assertEqual(audit[-1]["event"], "reject")
        self.assertIn(STALE_BADGE, audit[-1]["detail"])

    def test_consume_is_idempotent(self) -> None:
        kwargs = dict(
            analysis_plan="ANAL-GMV_TREND",
            envelope_id=V8,
            forecast_date="2026-10-18",
            forecast_value=1300000,
            baseline_value=1210000,
        )
        first, _, _ = self.svc.consume(**kwargs)
        before = len(self.svc.list_outputs())
        second, message, _ = self.svc.consume(**kwargs)
        self.assertEqual(first["output_id"], second["output_id"])
        self.assertIn("只生效一次", message)
        self.assertEqual(len(self.svc.list_outputs()), before)


class RetrainCase(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.svc = HandoffService()

    def test_retrain_success_deploys_new_version_and_keeps_old_outputs(self) -> None:
        old_outputs = [dict(row) for row in self.svc.list_outputs()]
        envelope, message, _ = self.svc.retrain(
            target_entity="GMV_DAILY",
            forecast_window_days=14,
            new_model_version="model-v9",
        )
        self.assertIsNotNone(envelope, message)
        self.assertEqual(envelope["model_version"], "model-v9")
        self.assertEqual(self.svc.get_envelope(V8)["status"], "superseded")
        # 旧预测结果保留且数值未被改写
        now_outputs = self.svc.list_outputs()
        self.assertEqual(len(now_outputs), len(old_outputs))
        for before, after in zip(old_outputs, now_outputs):
            self.assertEqual(before["forecast_value"], after["forecast_value"])
            self.assertEqual(before["model_version"], after["model_version"])

    def test_retrain_failure_marks_only_affected(self) -> None:
        envelope, message, code = self.svc.retrain(
            target_entity="GMV_DAILY",
            forecast_window_days=14,
            new_model_version="model-v9",
            training_ok=False,
        )
        self.assertIsNone(envelope)
        self.assertEqual(code, "TRAINING_FAILED")
        # 不建新版，v8 仍是当前部署
        self.assertEqual(self.svc.current_envelope("GMV_DAILY", 14)["model_version"], "model-v8")
        self.assertIsNone(self.svc.get_envelope("analysis_handoff_v9"))
        outputs = {row["envelope_id"]: row for row in self.svc.list_outputs()}
        # 只标记当前版输出，上一版数值不覆盖
        self.assertTrue(outputs[V8]["affected"])
        self.assertIn("训练失败", outputs[V8]["affected_reason"])
        self.assertEqual(outputs[V8]["forecast_value"], 1284000)
        # 历史版输出不受影响
        self.assertFalse(outputs[V7]["affected"])
        self.assertEqual(self.svc.list_audit()[-1]["event"], "reject")

    def test_retrain_incomplete_window_marks_only_affected(self) -> None:
        envelope, message, code = self.svc.retrain(
            target_entity="GMV_DAILY",
            forecast_window_days=14,
            new_model_version="model-v9",
            window_complete=False,
        )
        self.assertIsNone(envelope)
        self.assertEqual(code, "WINDOW_INCOMPLETE")
        outputs = {row["envelope_id"]: row for row in self.svc.list_outputs()}
        self.assertTrue(outputs[V8]["affected"])
        self.assertIn("窗口不完整", outputs[V8]["affected_reason"])
        self.assertFalse(outputs[V7]["affected"])
        self.assertEqual(self.svc.current_envelope("GMV_DAILY", 14)["model_version"], "model-v8")


class PackageCase(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.svc = HandoffService()

    def test_export_contains_window_and_confidence(self) -> None:
        package = self.svc.export_package()
        self.assertEqual(package["package_kind"], "forecast_analysis_handoff")
        output = next(row for row in package["outputs"] if row["envelope_id"] == V8)
        self.assertEqual(output["forecast_window_days"], 14)
        self.assertEqual(output["confidence"], 0.95)
        self.assertEqual(output["sample_range"], {"start": "2026-11-06", "end": "2026-11-12"})

    def test_reload_preserves_model_version_and_sample_range(self) -> None:
        package = self.svc.export_package()
        reset_store()  # 模拟换环境重新载入
        store._tables["fc_envelope"] = []
        store._tables["fc_output"] = []
        svc = HandoffService()
        summary, message = svc.reload_package(package)
        self.assertEqual(summary["outputs_loaded"], 2)
        output = next(row for row in svc.list_outputs() if row["envelope_id"] == V8)
        self.assertEqual(output["model_version"], "model-v8")
        self.assertEqual(output["sample_range"], {"start": "2026-11-06", "end": "2026-11-12"})
        self.assertEqual(output["forecast_window_days"], 14)
        self.assertEqual(output["confidence"], 0.95)
        # 重复载入幂等：已存在的记录跳过，不翻倍
        summary2, _ = svc.reload_package(package)
        self.assertEqual(summary2["outputs_loaded"], 0)
        self.assertEqual(summary2["outputs_skipped"], 2)


class ExistingChainHookCase(unittest.TestCase):
    """现有链路：forecast.部署模型 固化口径，analysis.启用方案 幂等且只绑已部署模型。"""

    def setUp(self) -> None:
        reset_store()
        self.forecast = ForecastService()
        self.analysis = AnalysisService()

    def test_forecast_deploy_action_freezes_caliber(self) -> None:
        entry, missing = self.forecast.create_entry({
            "方案编号": "FORE-GMV_HOURLY",
            "预测目标": "GMV_HOURLY",
            "算法名称": "LightGBM",
            "特征字段": "channel,discount",
            "训练周期": "7",
            "预测窗口天": "3",
            "置信水平": "0.9",
            "模型版本": "model-v1",
        })
        self.assertFalse(missing)
        entry, message = self.forecast.run_action(entry["id"], "部署模型")
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["交接信封"], "analysis_handoff_v1")
        envelope = HandoffService().get_envelope("analysis_handoff_v1")
        self.assertEqual(envelope["forecast_window_days"], 3)
        self.assertEqual(envelope["confidence_interval"], "interval_90")

    def test_forecast_deploy_action_rejects_incomplete_caliber(self) -> None:
        entry, _ = self.forecast.create_entry({"方案编号": "FORE-X", "预测目标": "X", "算法名称": "Y"})
        result, message = self.forecast.run_action(entry["id"], "部署模型")
        self.assertIsNone(result)
        self.assertIn("口径不完整", message)
        self.assertEqual(entry["status"], "草稿")  # 状态不被推进

    def test_analysis_enable_is_idempotent(self) -> None:
        entry, message = self.analysis.run_action(4, "启用方案")
        self.assertIsNotNone(entry)
        self.assertIn("只生效一次", message)  # 种子里 ANAL-GMV_TREND 已是已启用
        self.assertEqual(entry["绑定模型版本"], "model-v8")

    def test_analysis_enable_binds_deployed_model(self) -> None:
        entry, _ = self.analysis.create_entry({
            "方案编号": "ANAL-GMV_CHECK",
            "分析主题": "GMV 复核",
            "数据集范围": "order_fact_daily",
            "引用预测方案": "FORE-GMV_DAILY",
        })
        entry, message = self.analysis.run_action(entry["id"], "启用方案")
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["绑定交接信封"], V8)
        self.assertEqual(entry["绑定模型版本"], "model-v8")

    def test_analysis_enable_rejects_undeployed_reference(self) -> None:
        entry, _ = self.analysis.create_entry({
            "方案编号": "ANAL-GHOST",
            "分析主题": "引用未部署模型",
            "数据集范围": "order_fact_daily",
            "引用预测方案": "FORE-GHOST",
        })
        result, message = self.analysis.run_action(entry["id"], "启用方案")
        self.assertIsNone(result)
        self.assertIn(REJECT_UNDEPLOYED, message)
        self.assertEqual(entry["status"], "草稿")


if __name__ == "__main__":
    unittest.main()

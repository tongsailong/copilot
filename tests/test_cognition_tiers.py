#!/usr/bin/env python3
"""v9.12 认知分级（T0–T3）与统一调度口径回归。

覆盖：

1. T0 只由“显式简单类型 + 无认知价值信号 + 无高影响范围”成立；默认风险值与
   文件类型不能单独制造 T0。
2. 原始意图是分级第一输入：docs/mechanical 任务在原始意图涉及规则/架构/协作
   时进入 T3；意图缺失且任务性质无法可靠分类时至少 T1。
3. T1 只开启动窗口（A）；T2/T3 开 A/B/E；E 仍然只在最终工程验证完成后打开。
4. 自动执行是真实请求：start 自身消费 A 义务，记录带 dispatch=auto-obligation、
   tier 与 cognition_family；建议/命令文本不算 invoked。
5. 失败、退避与统计口径：失败只记录不阻断；退避保留义务；expected/triggered/
   invoked 与 auto/manual 分开计数。
6. 材料按阶段组织，并强制写明“用户原始任务意图”。

所有认知通道都用替身；本文件不发起任何真实模型请求。
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import v6_support as F

G = F.G
import review_policy as RP  # noqa: E402
import cognition_call as CC  # noqa: E402
import cognition_schedule as SCH  # noqa: E402
import copilot_dispatch as CD  # noqa: E402
import copilot_auto as CA  # noqa: E402

CONTRACT = "## 完成条件\n- 产出可复核的结果文件\n"
GOVERNANCE_INTENT = (
    "把 Copilot 的认知调度规则改成按 T0–T3 分级：治理、架构与协作规则的任务"
    "必须在启动、调查后和收口前各判断一次，并保证规则文档之间不矛盾。")
GOVERNANCE_CONTRACT = """## 完成条件
- 复核全局规则与角色边界，确认认知调度规则是否仍然成立
- 给出规则优化后的验证结论
"""


def think_result(mode: str = "CONSULT") -> dict:
    return {
        "schema": CC.THINK_SCHEMA, "mode": mode,
        "model": CC.MODEL, "model_resolved": CC.MODEL,
        "reasoning_effort": "high", "reasoning_mode": "standard",
        "reasoning_resolved": {"effort": "high"}, "reasoning_verified": True,
        "zero_tool": True, "attempts": 1, "request_id": "req-1",
        "trace_id": "trace-1", "response_id": "resp-1",
        "materials_sha256": "e" * 64, "materials_chars": 10,
        "request_sha256": "r" * 64, "reply_text": "judgment: ok",
        "reply_sha256": "a" * 64, "reply_chars": 10,
        "usage": {"total_tokens": 42}, "usage_available": True,
        "latency_ms": 1, "judgment": "先补测试", "essence": "缺少验证",
        "strategy": "先测后改", "tradeoffs": [], "risks": [], "priority": [],
        "verify": [], "reconsult": [], "unverified": [],
        "completed_at": G.now_iso(),
    }


class TierBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        self.contract_path = os.path.join(self.dir, "contract.md")
        self.plan_path = os.path.join(self.dir, "plan.json")

    # ── 状态构造 ─────────────────────────────────────────────────────────
    def start_state(self, *, goal: str = "交付可复核结果",
                    contract: str = CONTRACT, risk: str = "medium",
                    kind: str = "standard", intent: str = "",
                    triggers: list[str] | None = None) -> dict:
        return G.start_state(
            self.state_path, goal=goal, contract=contract, risk=risk,
            task_id="tier-task", workspace=self.dir, task_kind=kind,
            triggers=triggers or [], intent=intent,
            test_policy={"required": False} if risk == "high" else None)

    def write_contract(self, text: str = CONTRACT) -> None:
        with open(self.contract_path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def write_plan(self, actions: int = 1) -> None:
        items = [{"id": f"A{index}", "title": f"工作{index}",
                  "goal": f"目标{index}", "expected_evidence": "证据"}
                 for index in range(1, actions + 1)]
        with open(self.plan_path, "w", encoding="utf-8") as fh:
            json.dump({"summary": "计划", "actions": items}, fh)

    def state(self) -> dict:
        return G.load_state(self.state_path)

    def plan(self, actions: int = 1) -> None:
        self.write_plan(actions)
        G.plan_state(self.state_path, json.load(open(self.plan_path,
                                                     encoding="utf-8")))

    def evidence(self, *, name: str = "e1.txt") -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("evidence\n")
        state = self.state()
        eid = G.next_evidence_id(state)
        G.register_evidence(state, eid, kind="file", file_path=path,
                            note="证据")
        G.save_state(self.state_path, state)
        return eid

    def obligations(self, family: str = "") -> list[dict]:
        rows = SCH.obligations(self.state())
        return [item for item in rows
                if not family or str(item.get("family")) == family]

    def calls(self) -> list[dict]:
        return [item for item in self.state().get("cognition_calls") or []
                if isinstance(item, dict)]

    def sync(self, *, policy: str = "advise", auto: bool = False,
             origin: str = "", advice: dict | None = None,
             **advice_kwargs) -> dict:
        state = self.state()
        synced = G.sync_cognition_schedule(
            state, advice=advice if advice is not None
            else G.cognition_advice_summary(state, **advice_kwargs),
            policy=policy, auto_execution=auto, origin=origin)
        if synced["changes"]:
            G.save_state(self.state_path, state)
        return synced

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = CD.main(argv)
        return code, out.getvalue(), err.getvalue()

    @contextlib.contextmanager
    def channel(self, *, auto: str = "on", error: Exception | None = None):
        calls = {"count": 0, "modes": []}

        def fake(mode: str):
            def run(*_args, **_kwargs):
                calls["count"] += 1
                calls["modes"].append(mode)
                if error is not None:
                    raise error
                return think_result(mode)
            return run

        with mock.patch.dict(os.environ, {"COPILOT_COGNITION_AUTO": auto}), \
                mock.patch.object(CC, "run_consult", side_effect=fake("CONSULT")), \
                mock.patch.object(CC, "run_plan", side_effect=fake("PLAN")):
            yield calls


# ── T0–T3 分级 ────────────────────────────────────────────────────────────
class TierClassificationTests(TierBase):
    def test_t0_requires_explicit_simple_kind_and_no_signal(self):
        self.start_state(risk="low", kind="docs")
        complexity = RP.task_complexity(self.state())
        self.assertEqual(complexity["tier"], "T0")
        self.assertEqual(complexity["windows"], [])
        self.assertEqual(G.cognition_advice_summary(self.state())["action"],
                         "none")
        self.sync()
        self.assertEqual(SCH.obligations(self.state()), [])
        stats = SCH.task_stats(self.state())
        self.assertFalse(stats["eligible"])
        self.assertEqual(stats["metrics"]["expected"], 0)

    def test_governance_intent_on_docs_task_is_t3(self):
        """文件类型是 docs 不能掩盖原始意图里的治理/规则范围。"""
        self.start_state(goal="整理一份说明", contract=GOVERNANCE_CONTRACT,
                         risk="medium", kind="docs",
                         intent=GOVERNANCE_INTENT)
        complexity = RP.task_complexity(self.state())
        self.assertEqual(complexity["tier"], "T3")
        self.assertEqual(complexity["windows"], ["A", "B", "E"])
        self.assertIn("high-impact-scope", complexity["signal_codes"])
        sync = self.sync(origin="start")
        self.assertTrue(sync["changes"])
        self.assertEqual([row["family"] for row in self.obligations()], ["A"])
        self.assertEqual(self.obligations("A")[0]["tier"], "T3")

    def test_missing_intent_on_unclassified_task_is_at_least_t1(self):
        self.start_state(risk="medium", kind="standard")
        complexity = RP.task_complexity(self.state())
        self.assertEqual(complexity["level"], "low")
        self.assertEqual(complexity["tier"], "T1")
        self.assertEqual(complexity["windows"], ["A"])
        self.assertFalse(complexity["intent"]["available"])

    def test_t1_never_opens_plan_or_closure_window(self):
        self.start_state(risk="medium", kind="standard")
        self.sync(origin="start")
        self.plan(2)
        self.evidence()
        self.sync(origin="plan")
        advice = G.cognition_advice_summary(self.state())
        self.assertNotEqual(advice.get("family"), "B")
        self.assertFalse(RP.plan_admission(self.state())["required"])
        self.assertEqual(self.obligations("B"), [])
        self.assertEqual(
            SCH.task_stats(self.state())["coverage"]["plan"]["denominator"], 0)

    def test_t2_declares_all_three_windows(self):
        # risk=high 是一个真实认知价值信号（T2），不是默认值。
        self.start_state(risk="high", kind="feature")
        complexity = RP.task_complexity(self.state())
        self.assertEqual(complexity["tier"], "T2")
        self.assertEqual(complexity["windows"], ["A", "B", "E"])
        # PLAN 仍要求调查出事实：启动时没有计划/证据，准入暂不成立。
        self.assertFalse(RP.plan_admission(self.state())["required"])
        self.plan(1)
        self.evidence()
        self.assertTrue(RP.plan_admission(self.state())["required"])

    def test_t3_from_architecture_kind(self):
        self.start_state(risk="low", kind="architecture")
        self.assertEqual(RP.task_complexity(self.state())["tier"], "T3")

    def test_t3_from_stakes_trigger(self):
        self.start_state(risk="medium", kind="feature",
                         triggers=["architecture"])
        complexity = RP.task_complexity(self.state())
        self.assertEqual(complexity["tier"], "T3")
        self.assertIn("declared-trigger", complexity["signal_codes"])

    def test_plain_low_docs_task_stays_t0_even_with_contract_text(self):
        """高影响范围必须真的命中，而不是“合同里有一句人话”就升级。"""
        self.start_state(risk="low", kind="docs",
                         contract="## 完成条件\n- 修正错别字\n")
        self.assertEqual(RP.task_complexity(self.state())["tier"], "T0")


# ── 原始意图登记 ──────────────────────────────────────────────────────────
class IntentRegistrationTests(TierBase):
    def test_start_intent_is_stored_and_used_for_tier(self):
        self.write_contract(GOVERNANCE_CONTRACT)
        code, out, _err = self.run_cli([
            "start", "--state", self.state_path, "--goal", "整理说明",
            "--contract-file", self.contract_path, "--task-kind", "docs",
            "--workspace", self.dir, "--intent", GOVERNANCE_INTENT,
            "--cognition", "off"])
        self.assertEqual(code, 0, out)
        state = self.state()
        self.assertEqual(state["intent"], GOVERNANCE_INTENT)
        self.assertEqual(state["intent_source"], "explicit")
        self.assertEqual(len(state["intent_sha256"]), 64)
        self.assertIn("认知等级：T3", out)
        self.assertIn("原始意图：已登记", out)

    def test_start_intent_file(self):
        self.write_contract()
        intent_path = os.path.join(self.dir, "intent.txt")
        with open(intent_path, "w", encoding="utf-8") as fh:
            fh.write(GOVERNANCE_INTENT)
        code, _out, _err = self.run_cli([
            "start", "--state", self.state_path, "--goal", "整理说明",
            "--contract-file", self.contract_path, "--task-kind", "docs",
            "--workspace", self.dir, "--intent-file", intent_path,
            "--cognition", "off"])
        self.assertEqual(code, 0)
        self.assertEqual(self.state()["intent"], GOVERNANCE_INTENT)

    def test_intent_cannot_be_overwritten_without_replace(self):
        self.start_state(kind="standard")
        G.record_intent(self.state_path, "第一版原始意图")
        with self.assertRaises(G.GovernanceError):
            G.record_intent(self.state_path, "第二版原始意图")
        self.assertEqual(self.state()["intent"], "第一版原始意图")
        G.record_intent(self.state_path, "第二版原始意图", replace=True)
        self.assertEqual(self.state()["intent"], "第二版原始意图")
        events = [row["event"] for row in self.state().get("history") or []]
        self.assertIn("task_intent_recorded", events)

    def test_intent_cli_fills_gap_and_reports_tier(self):
        self.start_state(kind="docs")
        code, out, _err = self.run_cli([
            "intent", "--state", self.state_path, "--text", GOVERNANCE_INTENT])
        self.assertEqual(code, 0, out)
        self.assertIn("认知等级：T3", out)
        self.assertEqual(self.state()["intent"], GOVERNANCE_INTENT)
        code, _out, _err = self.run_cli([
            "intent", "--state", self.state_path, "--text", "覆盖尝试"])
        self.assertNotEqual(code, 0)


# ── 自动执行与真实调用记录 ────────────────────────────────────────────────
class AutoExecutionTests(TierBase):
    def test_start_consumes_startup_obligation_with_real_request(self):
        self.write_contract()
        with self.channel() as calls:
            code, out, err = self.run_cli([
                "start", "--state", self.state_path, "--goal", "交付可复核结果",
                "--contract-file", self.contract_path, "--task-kind", "feature",
                "--trigger", "core-logic", "--workspace", self.dir])
        self.assertEqual(code, 0, err)
        self.assertEqual(calls["count"], 1)
        self.assertIn("已执行义务", out)
        completed = [item for item in self.calls()
                     if item.get("status") == "completed"]
        self.assertEqual(len(completed), 1)
        record = completed[0]
        self.assertEqual(record["dispatch"], "auto-obligation")
        self.assertEqual(record["cognition_family"], "A")
        self.assertEqual(record["tier"], "T2")
        self.assertTrue(record["obligation_id"])
        obligation = self.obligations("A")[0]
        self.assertEqual(obligation["status"], "satisfied")
        self.assertEqual(obligation["call_id"], record["call_id"])

    def test_advise_policy_does_not_count_recommendation_as_invocation(self):
        self.start_state(kind="architecture")
        step = CD._cognition_schedule_step(
            self.state_path, argparse.Namespace(
                state="", cognition="advise", command="start"))
        self.assertEqual(step["policy"], "advise")
        self.assertTrue(step["command"])
        self.assertEqual(self.calls(), [])
        stats = SCH.task_stats(self.state())
        self.assertEqual(stats["metrics"]["triggered"], 1)
        self.assertEqual(stats["metrics"]["invoked"], 0)
        self.assertEqual(stats["metrics"]["succeeded"], 0)
        self.assertEqual(stats["metrics"]["missed"], 0)

    def test_failed_startup_call_keeps_engineering_running(self):
        self.start_state(kind="architecture")
        with self.channel(error=CC.CognitionQuotaError("cognition_quota",
                                                       "429")):
            result = CD._cognition_schedule_step(
                self.state_path, argparse.Namespace(
                    state="", cognition="auto", command="start"))
        self.assertEqual(result["exit"], 0)
        failed = [item for item in self.calls()
                  if item.get("status") == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(self.obligations("A")[0]["status"], "failed")
        self.assertEqual(self.state().get("blocks") or [], [])
        self.assertIn(G.evaluate(self.state())["phase"], ("RUNNING", "PARTIAL"))
        stats = SCH.task_stats(self.state())
        self.assertEqual(stats["metrics"]["failed"], 1)
        # 失败请求已经进入通道并留下真实调用记录：invoked 计 1，但 succeeded 为 0。
        self.assertEqual(stats["metrics"]["invoked"], 1)
        self.assertEqual(stats["metrics"]["succeeded"], 0)

    def test_backoff_defers_without_losing_obligation(self):
        self.start_state(kind="architecture")
        state = self.state()
        until = (datetime.datetime.now(datetime.timezone.utc)
                 + datetime.timedelta(hours=1)).isoformat(timespec="seconds")
        G.store_model_backoff(state, {
            "model_key": CC.model_key(), "provider": CC.PROVIDER,
            "model": CC.MODEL, "until": until, "error_class": "overloaded",
            "reason": "server_is_overloaded"})
        G.save_state(self.state_path, state)
        with self.channel() as calls:
            result = CD._cognition_schedule_step(
                self.state_path, argparse.Namespace(
                    state="", cognition="auto", command="start"))
        self.assertEqual(calls["count"], 0)
        self.assertEqual(result["deferred"]["obligation"],
                         self.obligations("A")[0]["obligation_id"])
        row = self.obligations("A")[0]
        self.assertEqual(row["status"], "due")
        self.assertTrue(row.get("deferrals"))
        stats = SCH.task_stats(self.state())
        self.assertEqual(stats["metrics"]["deferred"], 1)
        self.assertEqual(stats["metrics"]["failed"], 0)

    def test_stats_separate_auto_and_manual_calls(self):
        self.start_state(kind="architecture")
        with self.channel():
            CD._cognition_schedule_step(
                self.state_path, argparse.Namespace(
                    state="", cognition="auto", command="start"))
        G.record_cognition_call(self.state_path, {
            "call_id": "SCmanual", "mode": "CONSULT", "status": "completed",
            "dispatch": "manual", "stage": "development",
            "completed_at": G.now_iso(), "summary": "手动咨询",
        }, mode="CONSULT", materials_sha256="m" * 64)
        metrics = SCH.task_stats(self.state())["metrics"]
        self.assertEqual(metrics["auto_invoked"], 1)
        self.assertEqual(metrics["manual_invoked"], 1)
        self.assertEqual(metrics["invoked"], 1)
        aggregated = SCH.stats([self.state()])["metrics"]
        self.assertEqual(aggregated["auto_invoked"], 1)
        self.assertEqual(aggregated["manual_invoked"], 1)

    def test_manual_obligation_call_is_counted_as_manual(self):
        """手动执行某条义务不等于调度自动执行：统计必须分开。"""
        self.start_state(kind="architecture")
        self.sync()
        obligation = self.obligations("A")[0]
        with self.channel() as calls:
            code, _out, err = self.run_cli([
                "cognition-consult", "--state", self.state_path,
                "--obligation", obligation["obligation_id"]])
        self.assertEqual(code, 0, err)
        self.assertEqual(calls["count"], 1)
        record = [item for item in self.calls()
                  if item.get("status") == "completed"][0]
        self.assertEqual(record["dispatch"], "manual-obligation")
        metrics = SCH.task_stats(self.state())["metrics"]
        self.assertEqual(metrics["manual_invoked"], 1)
        self.assertEqual(metrics["auto_invoked"], 0)


# ── E 仍然只在最终验证后打开 ──────────────────────────────────────────────
class ClosureWindowTests(TierBase):
    def start_strict(self) -> None:
        G.start_state(
            self.state_path, goal="交付可复核结果", contract=CONTRACT,
            risk="medium", task_id="tier-closure", workspace=self.dir,
            task_kind="feature", triggers=["architecture"],
            test_policy={"required": True,
                         "commands": ["python3 -m unittest"]})

    def covered(self) -> None:
        self.start_strict()
        self.record_call("CONSULT")
        self.plan(2)
        self.evidence()
        self.record_call("PLAN")

    def record_call(self, mode: str) -> dict:
        state = self.state()
        return G.record_cognition_call(self.state_path, {
            "call_id": f"SC{len(state.get('cognition_calls') or [])}{mode}",
            "mode": mode, "status": "completed", "stage": "",
            "question": "本次需要判断的事项",
            "contract_sha256": state.get("contract_sha256") or "",
            "completed_at": G.now_iso(),
        }, mode=mode, materials_sha256="m" * 64)

    def actions_done(self) -> None:
        for action in self.state().get("actions") or []:
            G.record_action(self.state_path, action["id"], status="done",
                            result="完成", evidence_ids=["E1"])

    def criteria_met(self) -> None:
        for criterion in self.state()["contract"]["criteria"]:
            G.record_criterion(self.state_path, criterion["criterion_id"],
                               "met", evidence_ids=["E1"])

    def register_tests_evidence(self) -> str:
        path = os.path.join(self.dir, "tests.log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("Ran 7 tests\nOK\n")
        state = self.state()
        eid = G.next_evidence_id(state)
        G.register_evidence(state, eid, kind="capture", file_path=path,
                            note="测试输出",
                            capture_cmd="python3 -m unittest discover -s tests")
        G.save_state(self.state_path, state)
        return eid

    def verified(self) -> None:
        self.covered()
        self.actions_done()
        self.criteria_met()
        self.register_tests_evidence()

    def test_e_is_not_opened_before_final_verification(self):
        self.covered()
        self.actions_done()
        self.criteria_met()
        readiness = G.closure_readiness(self.state())
        self.assertFalse(readiness["ready"])
        self.assertNotEqual(G.cognition_advice_summary(self.state()).get("family"),
                            "E")
        self.sync(origin="closure", gates_passed=True)
        self.assertEqual(self.obligations("E"), [])

    def test_e_opens_and_executes_after_final_verification(self):
        self.verified()
        digest = G.closure_readiness(self.state())["digest"]
        advice = G.cognition_advice_summary(self.state(), gates_passed=True)
        self.assertEqual((advice["family"], advice["event"]), ("E", "closure"))
        self.assertEqual(advice["instance"], digest)
        self.sync(origin="closure", gates_passed=True)
        rows = self.obligations("E")
        self.assertEqual(len(rows), 1)
        with self.channel() as calls:
            result = CD._cognition_schedule_step(
                self.state_path, argparse.Namespace(
                    state="", cognition="auto", command="closure"),
                gates_passed=True, prefer_family="E")
        self.assertEqual(calls["count"], 1)
        self.assertEqual(result["executed"], rows[0]["obligation_id"])
        record = [item for item in self.calls()
                  if item.get("stage") == "closure"][0]
        self.assertEqual(record["final_verification_digest"], digest)
        self.assertEqual(record["cognition_family"], "E")


# ── 材料按阶段组织 ────────────────────────────────────────────────────────
class MaterialsTests(TierBase):
    def _obligation(self, family: str, stage: str, mode: str = "CONSULT",
                    event: str = "") -> dict:
        return {
            "schema": SCH.OBLIGATION_SCHEMA, "obligation_id": f"O-{family}",
            "family": family, "mode": mode, "stage": stage,
            "event": event or {"A": "startup", "B": "plan-needed",
                               "C": "new-risk", "E": "closure"}[family],
            "question": "这次需要判断什么？", "reason": "触发事实",
            "instance": "i", "level": "recommended", "tier": "T3",
        }

    def test_all_stage_materials_carry_raw_intent(self):
        self.start_state(kind="architecture", intent=GOVERNANCE_INTENT)
        for family, stage in (("A", "understanding"), ("B", "planning"),
                              ("C", "development"), ("E", "closure")):
            with self.subTest(family=family):
                text = SCH.build_materials(
                    self.state(), self._obligation(family, stage),
                    readiness={"ready": True, "digest": "d" * 64})
                self.assertIn("## 用户原始任务意图", text)
                self.assertIn("T0–T3 分级", text)
                self.assertIn(SCH.MATERIALS_SCHEMA, text)

    def test_stage_specific_blocks(self):
        self.start_state(kind="architecture", intent=GOVERNANCE_INTENT)
        a = SCH.build_materials(self.state(),
                                self._obligation("A", "understanding"))
        self.assertIn("授权边界与已知约束", a)
        b = SCH.build_materials(self.state(), self._obligation("B", "planning"))
        self.assertIn("## 调查事实与候选方案", b)
        c = SCH.build_materials(self.state(), self._obligation("C", "development"))
        self.assertIn("## 本次决策问题与新增事实", c)
        e = SCH.build_materials(self.state(), self._obligation("E", "closure"),
                                readiness={"ready": True, "digest": "d" * 64})
        self.assertIn("## 最终成果与完成情况", e)

    def test_missing_intent_is_stated_not_disguised_as_goal(self):
        self.start_state(kind="architecture", goal="整理说明")
        text = SCH.build_materials(self.state(),
                                   self._obligation("A", "understanding"))
        self.assertIn("原始意图：未登记", text)
        self.assertNotIn("原始意图（explicit", text)


# ── CLI 输出与统计命令 ────────────────────────────────────────────────────
class StatsOutputTests(TierBase):
    def test_review_admission_shows_tier_windows_and_intent(self):
        self.start_state(kind="architecture", intent=GOVERNANCE_INTENT)
        code, out, _err = self.run_cli(
            ["review-admission", "--state", self.state_path])
        self.assertEqual(code, 0)
        self.assertIn("认知等级：T3", out)
        self.assertIn("窗口：A/B/E", out)
        self.assertIn("原始意图：已登记", out)

    def test_cognition_stats_exposes_metrics_and_by_window(self):
        self.start_state(kind="architecture", intent=GOVERNANCE_INTENT)
        self.sync(origin="start")
        code, out, _err = self.run_cli(
            ["cognition-stats", "--state", self.state_path])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        stats = payload["tasks"][0]
        self.assertEqual(stats["tier"], "T3")
        self.assertEqual(stats["intent"]["available"], True)
        for key in ("expected", "triggered", "invoked", "succeeded", "failed",
                    "reused", "deferred", "skipped", "missed",
                    "auto_invoked", "manual_invoked"):
            self.assertIn(key, stats["metrics"])
        self.assertIn("A", stats["by_window"])
        self.assertIn("effect", stats)
        self.assertEqual(payload["totals"]["tier_tasks"]["T3"], 1)
        self.assertEqual(payload["totals"]["intent_registered_tasks"], 1)

    def test_cognition_stats_exposes_mode_and_task_kind_dimensions(self):
        """统计维度必须区分 CONSULT/PLAN/REVIEW 与任务类型，且只认真实调用。"""
        self.write_contract()
        with self.channel() as channel:
            code, _out, err = self.run_cli([
                "start", "--state", self.state_path, "--goal", "交付可复核结果",
                "--contract-file", self.contract_path,
                "--task-kind", "architecture", "--intent", GOVERNANCE_INTENT,
                "--workspace", self.dir])
        self.assertEqual(code, 0, err)
        self.assertEqual(channel["count"], 1)
        code, out, _err = self.run_cli(
            ["cognition-stats", "--state", self.state_path])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        task = payload["tasks"][0]
        for mode in ("CONSULT", "PLAN", "REVIEW"):
            self.assertIn(mode, task["by_mode"])
            self.assertIn(mode, payload["by_mode"])
        recorded = [item for item in self.calls()
                    if item.get("status") == "completed"]
        self.assertEqual(len(recorded), 1)
        expected = {}
        for item in recorded:
            mode = str(item.get("mode") or "").upper()
            expected[mode] = expected.get(mode, 0) + 1
        for mode in ("CONSULT", "PLAN", "REVIEW"):
            self.assertEqual(task["by_mode"][mode]["calls"],
                             expected.get(mode, 0), mode)
        self.assertEqual(task["by_mode"]["CONSULT"]["succeeded"], 1)
        self.assertEqual(
            sum(row["calls"] for row in task["by_mode"].values()),
            len(recorded))
        self.assertEqual(payload["totals"]["task_kind_tasks"]
                         ["architecture"], 1)
        self.assertEqual(sum(payload["totals"]["task_kind_tasks"].values()), 1)
        self.assertEqual(payload["by_mode"]["CONSULT"]["calls"], 1)
        self.assertEqual(payload["by_mode"]["PLAN"]["calls"], 0)
        self.assertEqual(task["metrics"]["success_rate"], 1.0)
        self.assertEqual(task["metrics"]["missed_rate"], 0.0)
        self.assertEqual(payload["metrics"]["success_rate"], 1.0)

    def test_rates_are_null_instead_of_zero_without_denominator(self):
        """分母为 0 时记 null：没有请求不能声称 0% 成功率，T0 也没有漏调率。"""
        self.start_state(kind="docs", goal="修正一处错别字")
        stats = SCH.task_stats(self.state())
        self.assertEqual(stats["tier"], "T0")
        self.assertIsNone(stats["metrics"]["success_rate"])
        self.assertIsNone(stats["metrics"]["missed_rate"])

    def test_contract_amendment_with_new_scope_reopens_cognitive_event(self):
        self.start_state(kind="feature", intent="实现一个局部功能",
                         triggers=["core-logic"])
        self.sync(origin="start")
        with self.channel():
            CD._cognition_schedule_step(
                self.state_path, argparse.Namespace(
                    state="", cognition="auto", command="start"))
        G.amend_contract(self.state_path,
                         contract=GOVERNANCE_CONTRACT,
                         authorized_by="user",
                         authorization_note="现场证据要求扩大范围")
        advice = G.cognition_advice_summary(self.state())
        self.assertEqual(advice.get("family"), "C")
        self.assertEqual(RP.task_complexity(self.state())["tier"], "T3")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""v9.0.0 SOL-first 默认认知引擎回归：生命周期、路由、交接与主代理边界。

覆盖 v9 的核心契约：

1. 简单/机械任务 0 调用；未声明或非简单任务默认进入 SOL 认知流程。
2. 生命周期：启动 CONSULT → 调查后 PLAN → 执行期新信号动态 CONSULT →
   高复杂任务阶段复盘 → 收口综合判断。
3. 同一合同内无新信息不重复；计划/合同实质变化或用户明确要求才追加。
4. 输出交回 ZCode：建议进入 evaluate/next_commands，不派生工作项、不改门禁；
   采纳结论由主代理用 sol-accept 显式登记。
5. 失败只降级：真实失败被记录，任务继续推进，不伪造完成。
6. PARTIAL 恢复复用既有结论；项级阻塞不冻结其余工作；正式 REVIEW 仅用户授权。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import v6_support as F

G = F.G
import review_policy as RP  # noqa: E402  v6_support 已把 scripts/ 加入 sys.path
import sol_review as SR  # noqa: E402
import copilot_dispatch as CD  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""


class SolFirstBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        self.materials = os.path.join(self.dir, "materials.md")
        with open(self.materials, "w", encoding="utf-8") as fh:
            fh.write("## 现场事实\n- 已核实入口与现状\n")

    def start(self, *, risk: str = "medium", kind: str = "standard",
              triggers: list[str] | None = None) -> str:
        G.start_state(
            self.state_path, goal="交付可复核结果", contract=CONTRACT,
            risk=risk, task_id="sol-first", workspace=self.dir,
            task_kind=kind, triggers=triggers,
            test_policy={"required": False} if risk == "high" else None)
        return self.state_path

    def plan(self, actions: int, *, deps: bool = False) -> None:
        items = []
        for index in range(1, actions + 1):
            item = {"id": f"A{index}", "title": f"工作{index}",
                    "goal": f"目标{index}", "expected_evidence": "证据"}
            if deps and index > 1:
                item["depends_on"] = [f"A{index - 1}"]
            items.append(item)
        G.plan_state(self.state_path, {"summary": "计划", "actions": items})

    def evidence(self, modules: int = 1, count: int | None = None) -> list[str]:
        total = count or max(modules, 1)
        state = G.load_state(self.state_path)
        ids = []
        for index in range(total):
            folder = os.path.join(self.dir, f"mod{index % max(modules, 1) + 1}")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"e{index}.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"证据{index}\n")
            eid = G.next_evidence_id(state)
            G.register_evidence(state, eid, file_path=path, note="测试证据")
            ids.append(eid)
        G.save_state(self.state_path, state)
        return ids

    def done(self, count: int) -> None:
        ids = self.evidence()
        state = G.load_state(self.state_path)
        for action in (state.get("actions") or [])[:count]:
            G.record_action(self.state_path, action["id"], status="done",
                            result="完成", evidence_ids=[ids[0]])

    def close_all(self) -> None:
        ids = self.evidence()
        state = G.load_state(self.state_path)
        for action in state.get("actions") or []:
            G.record_action(self.state_path, action["id"], status="done",
                            result="完成", evidence_ids=[ids[0]])
        state = G.load_state(self.state_path)
        for criterion in state["contract"]["criteria"]:
            G.record_criterion(self.state_path, criterion["criterion_id"],
                               "met", evidence_ids=[ids[0]])

    def record(self, mode: str, *, signals: list[str] | None = None,
               stage: str = "", progress_digest: str = "",
               trigger: str = "", contract: str | None = None,
               usage: dict | None = None, latency_ms: int | None = None,
               status: str = "completed") -> dict:
        state = G.load_state(self.state_path)
        call = {
            "call_id": f"SC{mode.lower()}{len(state.get('sol_calls') or [])}",
            "mode": mode,
            "status": status,
            "stage": stage,
            "progress_digest": progress_digest,
            "trigger": trigger,
            "question": "本次需要判断的事项",
            "contract_sha256": contract
            if contract is not None else state.get("contract_sha256") or "",
            "materials_sha256": "m" * 64,
            "summary": "建议",
            "verdict": "pass" if mode == "REVIEW" else "",
            "findings": [],
            "findings_count": {},
            "completed_at": G.now_iso(),
        }
        if signals is not None:
            call["complexity"] = "medium"
            call["complexity_signals"] = list(signals)
        if usage is not None:
            call["usage"] = usage
        if latency_ms is not None:
            call["latency_ms"] = latency_ms
        return G.record_sol_call(self.state_path, call, mode=mode,
                                 materials_sha256="m" * 64, trigger=trigger)

    def advice(self, **kwargs) -> dict:
        return G.sol_advice_summary(G.load_state(self.state_path), **kwargs)

    def inject_replan(self) -> None:
        """模拟主代理登记重规划：AUTO-REPLAN 是升级型信号，但不属于计划项。"""
        state = G.load_state(self.state_path)
        state.setdefault("actions", []).append({
            "id": "AUTO-REPLAN-1", "title": "重规划", "goal": "重规划",
            "status": "pending", "origin": "replan"})
        G.save_state(self.state_path, state)

    def run_consult(self, *, result=None, error=None, json_output: bool = True):
        """在本进程内执行 sol-consult，可注入成功结果或真实失败。"""
        args = argparse.Namespace(
            state=self.state_path, materials=self.materials,
            question="该方案是否可行？", stage="", reason="", trigger="",
            force=False, dry_run=False, list=False, json=json_output,
            timeout=5, max_output_tokens=100, audit_log="", output_dir="",
            knowledge_file="")
        out, err = io.StringIO(), io.StringIO()
        patch = (mock.patch("sol_review.run_consult", return_value=dict(result))
                 if result is not None else
                 mock.patch("sol_review.run_consult", side_effect=error))
        with patch, contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = CD.cmd_sol_consult(args)
        return code, out.getvalue(), err.getvalue()


class RoutingTests(SolFirstBase):
    def test_simple_mechanical_task_gets_zero_calls(self):
        self.start(risk="low", kind="trivial")
        self.plan(1)
        self.evidence()
        advice = self.advice()
        self.assertEqual(advice["action"], "none")
        self.assertEqual(advice["recommended_action"], "not_needed")
        self.assertFalse([cmd for cmd in
                          G.evaluate(G.load_state(self.state_path)).get(
                              "next_commands") or [] if "sol-" in cmd])

    def test_undeclared_task_defaults_to_non_simple_consult(self):
        self.start()  # 未声明 task_kind → standard（非简单）
        advice = self.advice()
        self.assertEqual(advice["task_kind"], "standard")
        self.assertEqual(advice["action"], "consult")
        self.assertEqual(advice["mode"], "CONSULT")
        self.assertEqual(advice["stage"], "understanding")
        self.assertFalse(advice["second_round"])
        self.assertIn("sol-consult", advice["command"])

    def test_plan_is_suggested_after_investigation(self):
        self.start(kind="feature")
        self.record("CONSULT")
        self.plan(2)
        self.evidence()
        advice = self.advice()
        self.assertEqual(advice["action"], "plan")
        self.assertEqual(advice["mode"], "PLAN")
        self.assertEqual(advice["stage"], "planning")
        self.assertIn("sol-plan", advice["command"])
        self.assertIn("--question", advice["command"])

    def test_product_design_gets_default_consult(self):
        self.start(kind="product-design")
        advice = self.advice()
        self.assertEqual(advice["action"], "consult")
        self.assertEqual(advice["stage"], "understanding")
        self.assertIn("非简单任务从启动阶段默认", advice["reason"])

    def test_architecture_task_gets_consult_then_plan(self):
        self.start(kind="architecture")
        first = self.advice()
        self.assertEqual(first["action"], "consult")
        self.record("CONSULT")
        self.plan(1)  # 架构类任务即使只有一个计划项也属于需要规划的类型
        self.evidence()
        second = self.advice()
        self.assertEqual(second["action"], "plan")
        self.assertEqual(second["mode"], "PLAN")

    def test_declared_trigger_is_a_signal_but_not_a_gate(self):
        self.start(risk="low", kind="feature", triggers=["architecture"])
        profile = self.advice()["complexity"]
        self.assertIn("declared-trigger", profile["signal_codes"])
        self.assertEqual(profile["level"], "medium")

    def test_new_escalation_signal_allows_dynamic_consult(self):
        self.start(kind="feature")
        self.record("CONSULT", signals=["multi-module"])
        self.plan(2)
        self.evidence(modules=2, count=2)
        self.record("PLAN", signals=["multi-module"])
        self.inject_replan()
        advice = self.advice()
        self.assertEqual(advice["action"], "consult")
        self.assertEqual(advice["phase"], "development")
        self.assertIn("replan-history", advice["new_signals"])
        self.assertIn("--force", advice["command"])

    def test_ordinary_execution_steps_do_not_trigger(self):
        self.start(kind="feature")
        self.record("CONSULT", signals=[])
        self.plan(3)
        self.evidence()
        self.record("PLAN", signals=[])
        self.done(1)
        self.assertEqual(self.advice()["action"], "reuse")

    def test_no_repeat_without_new_information(self):
        self.start(kind="feature")
        self.record("CONSULT", signals=[])
        self.plan(3)
        self.evidence()
        self.record("PLAN", signals=[])
        self.done(2)
        advice = self.advice()
        self.assertEqual(advice["action"], "reuse")
        self.assertEqual(advice["command"], "")

    def test_contract_change_allows_new_plan(self):
        self.start(kind="feature")
        self.record("CONSULT")
        self.plan(2)
        self.evidence()
        self.assertEqual(self.advice()["action"], "plan")
        self.record("PLAN")
        self.assertEqual(self.advice()["action"], "reuse")
        # 计划/合同实质变化后，旧记录不再覆盖当前合同：先重新 CONSULT 理解，
        # 再允许新的 PLAN；不能用旧结论冒充已覆盖。
        G.amend_contract(self.state_path,
                         contract=CONTRACT + "- 新增：交付迁移说明\n")
        after = self.advice()
        self.assertEqual(after["action"], "consult")
        self.assertTrue(after["second_round"])
        self.record("CONSULT", signals=[])
        replanned = self.advice()
        self.assertEqual(replanned["action"], "plan")
        self.assertTrue(replanned["second_round"])
        self.assertIn("--force", replanned["command"])

    def test_new_decision_is_reconsulted_on_user_request(self):
        self.start(kind="feature")
        self.record("CONSULT", signals=[])
        self.plan(2)
        self.evidence()
        self.record("PLAN", signals=[])
        advice = self.advice(user_request=True)
        self.assertEqual(advice["action"], "consult")
        self.assertEqual(advice["mode"], "CONSULT")
        self.assertIn("--force", advice["command"])
        self.assertIn("user-request", advice["trigger"])


class HandoffTests(SolFirstBase):
    def test_advice_is_handed_back_to_zcode_without_creating_work(self):
        self.start(kind="standard")
        state = G.load_state(self.state_path)
        evaluation = G.evaluate(state)
        self.assertEqual(evaluation["sol"]["action"], "consult")
        self.assertTrue(any("sol-consult" in cmd
                            for cmd in evaluation.get("next_commands") or []))
        # 建议不派生工作项：derive_work 只处理真实缺口。
        G.derive_work(state)
        state = G.load_state(self.state_path)
        self.assertFalse([item for item in state.get("actions") or []
                          if "SOL" in str(item.get("id") or "")])

    def test_acceptance_is_explicit_and_does_not_change_state(self):
        self.start(kind="feature")
        first = self.record("CONSULT")
        second = self.record("PLAN")
        third = self.record("CONSULT")
        G.record_sol_acceptance(self.state_path, G.sol_call_id(first),
                                status="accepted", note="采纳目标理解")
        G.record_sol_acceptance(self.state_path, G.sol_call_id(second),
                                status="modified", note="只采纳优先级",
                                reason="原方案范围过大")
        G.record_sol_acceptance(self.state_path, G.sol_call_id(third),
                                status="rejected", reason="与现场证据冲突")
        state = G.load_state(self.state_path)
        self.assertEqual([item["acceptance"]["status"]
                          for item in state["sol_calls"]],
                         ["accepted", "modified", "rejected"])
        self.assertEqual(G.evaluate(state)["phase"], "PARTIAL")
        metrics = G.review_metrics(state)["sol_cognitive"]
        self.assertEqual(metrics["acceptance"],
                         {"accepted": 1, "modified": 1, "rejected": 1,
                          "pending": 0})
        self.assertEqual(metrics["adopted"], 2)

    def test_acceptance_requires_completed_call(self):
        self.start(kind="feature")
        failed = G.record_sol_call_failure(
            self.state_path, mode="CONSULT", error_code="sol_timeout")
        with self.assertRaises(G.GovernanceError):
            G.record_sol_acceptance(self.state_path, failed["call_id"],
                                    status="accepted")

    def test_zero_tool_for_every_mode(self):
        for mode in SR.SOL_MODES:
            with self.subTest(mode=mode):
                payload = SR.build_payload("材料", mode=mode)
                self.assertNotIn("tools", payload)
                self.assertNotIn("functions", payload)
                self.assertEqual(payload["tool_choice"], "none")
                self.assertEqual(payload["reasoning"], {"effort": "high"})
                SR.assert_zero_tool(payload)

    def test_consult_reply_contains_cognitive_superset(self):
        parsed = SR.parse_think_output(
            json.dumps({"schema": SR.THINK_SCHEMA, "judgment": "先验证",
                        "goal_understanding": "交付可靠结果",
                        "essence": "缺少端到端验证", "strategy": "先补测试",
                        "alternatives": ["直接合并"],
                        "tradeoffs": ["速度换验证"],
                        "risks": ["遗漏集成路径"],
                        "priority": ["P1 测试"],
                        "investigation": ["检查调用方"],
                        "phase_goals": ["阶段一完成验证"],
                        "assumptions": ["现有接口稳定"],
                        "omissions": ["未覆盖回滚"],
                        "decisions": ["是否扩大范围"],
                        "verify": ["定向测试通过"],
                        "reconsult": ["出现并发写入"],
                        "unverified": []}, ensure_ascii=False))
        self.assertEqual(parsed["judgment"], "先验证")
        self.assertEqual(parsed["alternatives"], ["直接合并"])


class FailureTests(SolFirstBase):
    def test_failed_consult_is_recorded_and_task_continues(self):
        self.start(kind="feature")
        code, _out, err = self.run_consult(
            error=SR.SolTimeoutError("sol_timeout", "上游超时"))
        self.assertEqual(code, 0)  # CONSULT 失败只降级
        self.assertIn("sol_timeout", err)
        state = G.load_state(self.state_path)
        self.assertEqual(state["sol_calls"][-1]["status"], "failed")
        self.assertEqual(state["sol_calls"][-1]["mode"], "CONSULT")
        self.assertNotEqual(G.evaluate(state)["phase"], "BLOCKED")

    def test_provider_unavailable_is_not_faked_as_completed(self):
        self.start(kind="feature")
        self.run_consult(error=SR.SolUpstreamError("sol_network",
                                                   "无法连接 Provider"))
        state = G.load_state(self.state_path)
        self.assertFalse([item for item in state["sol_calls"]
                          if item.get("status") == "completed"])
        metrics = G.review_metrics(state)["sol_cognitive"]
        self.assertEqual(metrics["failed"], 1)
        self.assertEqual(metrics["consult_completed"], 0)
        self.assertFalse(metrics["default_covered"])

    def test_partial_recovery_reuses_existing_conclusions(self):
        self.start(kind="feature")
        self.record("CONSULT", signals=[])
        self.plan(2)
        self.evidence()
        self.record("PLAN", signals=[])
        state = G.load_state(self.state_path)
        self.assertIn(G.evaluate(state)["phase"], ("RUNNING", "PARTIAL"))
        # 上下文恢复后重新加载状态：既有 CONSULT/PLAN 被复用，不重新付费。
        advice = self.advice()
        self.assertEqual(advice["action"], "reuse")
        self.assertEqual(advice["mode"], "CONSULT")

    def test_legacy_think_record_counts_as_consult(self):
        self.start(kind="feature")
        self.record("THINK", signals=[])
        advice = self.advice()
        self.assertTrue(advice["covered"]["CONSULT"])
        self.assertNotEqual(advice["action"], "consult")

    def test_lightweight_metrics_mark_unavailable_latency(self):
        self.start(kind="feature")
        self.record("CONSULT", signals=[], usage={"input_tokens": 10,
                                                  "output_tokens": 20},
                    latency_ms=1200)
        metrics = G.review_metrics(G.load_state(self.state_path))
        self.assertEqual(metrics["schema"], "copilot-review-metrics/v2")
        cognitive = metrics["sol_cognitive"]
        self.assertTrue(cognitive["non_simple"])
        self.assertTrue(cognitive["default_covered"])
        self.assertEqual(cognitive["usage"],
                         {"input_tokens": 10, "output_tokens": 20})
        self.assertFalse(cognitive["latency_unavailable"])
        self.assertEqual(cognitive["latency_ms_avg"], 1200.0)

    def test_latency_is_unavailable_without_samples(self):
        self.start(kind="feature")
        self.record("CONSULT", signals=[])
        cognitive = G.review_metrics(
            G.load_state(self.state_path))["sol_cognitive"]
        self.assertTrue(cognitive["latency_unavailable"])
        self.assertIsNone(cognitive["latency_ms_avg"])


class BoundaryTests(SolFirstBase):
    def test_work_item_block_does_not_freeze_other_work(self):
        self.start(kind="feature")
        self.record("CONSULT")
        self.plan(2)
        self.evidence()
        G.add_block(self.state_path, "缺少发布授权", "用户授权",
                    work_items=["A2"])
        advice = self.advice()
        self.assertEqual(advice["action"], "plan")
        self.assertNotIn("BLOCKED", advice["reason"])

    def test_task_level_block_stops_new_sol_calls(self):
        self.start(kind="feature")
        G.add_block(self.state_path, "缺少用户决策", "用户确认范围")
        advice = self.advice()
        self.assertEqual(advice["action"], "none")
        self.assertIn("BLOCKED", advice["reason"])

    def test_closure_consult_is_deduplicated_by_progress(self):
        self.start(risk="high", kind="feature")
        self.record("CONSULT", signals=["risk-high", "multi-action"])
        self.plan(4)
        self.evidence(modules=2, count=2)
        self.done(2)
        self.record("PLAN", signals=["risk-high", "multi-action"])
        first = self.advice()
        self.assertEqual(first["stage"], "stage")
        self.assertEqual(first["recommended_action"], "optional")
        digest = RP.sol_progress_key(G.load_state(self.state_path))
        self.record("CONSULT", signals=["risk-high", "multi-action"],
                    stage="stage", progress_digest=digest)
        second = self.advice()
        self.assertEqual(second["action"], "reuse")

    def test_closure_consult_precedes_review(self):
        self.start(risk="high", kind="feature")
        self.plan(1)
        self.close_all()
        advice = self.advice(gates_passed=True)
        self.assertEqual(advice["action"], "consult")
        self.assertEqual(advice["stage"], "closure")
        self.assertIn("sol-consult", advice["command"])
        self.assertNotIn("sol-review", advice["command"])

    def test_formal_review_requires_user_request(self):
        self.start(kind="feature")
        self.record("CONSULT", signals=[])
        self.plan(2)
        self.evidence()
        self.record("PLAN", signals=[])
        self.assertNotEqual(self.advice()["action"], "review")
        requested = self.advice(review_requested=True)
        self.assertEqual(requested["action"], "review")
        self.assertEqual(requested["mode"], "REVIEW")
        self.assertIn("sol-review", requested["command"])
        self.assertIn("用户明确要求", requested["reason"])

    def test_g1_g3_are_unaffected_by_cognitive_calls(self):
        self.start(risk="medium", kind="standard")
        self.plan(2)
        self.close_all()
        state = G.load_state(self.state_path)
        before = G.closure_check(state)
        self.assertTrue(before["passed"])
        self.assertEqual(G.evaluate(state)["phase"], "PASS")
        self.record("CONSULT", signals=[], stage="closure")
        self.record("PLAN", signals=[])
        state = G.load_state(self.state_path)
        self.assertTrue(G.closure_check(state)["passed"])
        self.assertEqual(G.evaluate(state)["phase"], "PASS")

    def test_consult_requires_explicit_question(self):
        self.start(kind="feature")
        proc = F.run_cli("copilot_dispatch.py", "sol-consult",
                         "--state", self.state_path,
                         "--materials", self.materials,
                         "--dry-run", "--json")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--question", proc.stderr)

    def test_plan_defaults_to_planning_stage_and_consult_to_development(self):
        self.assertEqual(SR.normalize_stage("", mode="PLAN"), "planning")
        self.assertEqual(SR.normalize_stage("", mode="CONSULT"), "development")
        with self.assertRaises(SR.SolUsageError):
            SR.normalize_stage("invented", mode="CONSULT")

    def test_dry_run_is_mode_aware_and_zero_tool(self):
        self.start(kind="feature")
        cases = {
            "sol-consult": ("copilot-sol-consult-dry-run/v1", "CONSULT",
                            "development"),
            "sol-plan": ("copilot-sol-plan-dry-run/v1", "PLAN", "planning"),
        }
        before = open(self.state_path, "rb").read()
        for command, (schema, mode, stage) in cases.items():
            with self.subTest(command=command):
                proc = F.run_cli("copilot_dispatch.py", command,
                                 "--state", self.state_path,
                                 "--materials", self.materials,
                                 "--question", "本次需要判断的事项",
                                 "--dry-run", "--json")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                payload = json.loads(proc.stdout)
                self.assertEqual(payload["schema"], schema)
                self.assertEqual(payload["mode"], mode)
                self.assertEqual(payload["stage"], stage)
                self.assertTrue(payload["zero_tool"])
                self.assertEqual(payload["tool_keys_present"], [])
                self.assertEqual(payload["model"], "gpt-6.2-sol")
        self.assertEqual(open(self.state_path, "rb").read(), before)

    def test_review_is_not_auto_derived_from_complexity(self):
        self.start(risk="high", kind="architecture")
        self.plan(5, deps=True)
        self.evidence(modules=2, count=2)
        profile = self.advice()["complexity"]
        self.assertEqual(profile["level"], "high")
        self.assertNotEqual(self.advice()["action"], "review")


if __name__ == "__main__":
    unittest.main()

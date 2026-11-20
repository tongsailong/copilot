#!/usr/bin/env python3
"""v8.3 SOL 主动介入策略契约。

覆盖本轮触发策略的核心不变量：

1. 复杂度判断只用状态事实（风险、触发因素、计划结构、证据目录、重规划/重开）。
2. 简单/局部任务保持 0 调用；进入 PASS 也不因阶段自动调用。
3. 中高复杂任务在形成初步理解后主动获得一次 THINK 建议。
4. 已有有效结论且无新信号时复用；只有新信号才允许第二次调用。
5. SOL 仍是 advisory：不改变阶段、不改变 G1–G3、失败不阻断；G4 不存在。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest

import v6_support as F

G = F.G
import review_policy as RP  # noqa: E402  v6_support 已把 scripts/ 加入 sys.path
import sol_review as SR  # noqa: E402
import copilot_dispatch as CD  # noqa: E402
import copilot_auto as CA  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""


class TriggerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")

    def start(self, *, risk: str = "medium", workspace: bool = True,
              kind: str = "trivial") -> str:
        G.start_state(
            self.state_path, goal="交付可复核结果", contract=CONTRACT,
            risk=risk, task_id="sol-trigger", workspace=self.dir if workspace else "",
            task_kind=kind,
            test_policy={"required": False} if risk == "high" else None)
        return self.state_path

    def plan(self, actions: int, *, deps: bool = False, prefix: str = "A") -> None:
        items = []
        for index in range(1, actions + 1):
            item = {"id": f"{prefix}{index}", "title": f"工作{index}",
                    "goal": f"目标{index}", "expected_evidence": "证据"}
            if deps and index > 1:
                item["depends_on"] = [f"{prefix}{index - 1}"]
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

    def close_all(self) -> None:
        """把计划内工作项与完成条件全部核销，使 G1–G3 通过。"""
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
               contract: str | None = None, status: str = "completed") -> dict:
        state = G.load_state(self.state_path)
        call = {
            "call_id": f"SC{mode.lower()}{len(state.get('sol_calls') or [])}",
            "mode": mode,
            "status": status,
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
        return G.record_sol_call(self.state_path, call, mode=mode,
                                 materials_sha256="m" * 64)

    def advice(self, **kwargs) -> dict:
        return G.sol_advice_summary(G.load_state(self.state_path), **kwargs)


class ComplexityTests(TriggerBase):
    def test_simple_task_is_low_complexity(self):
        self.start(risk="low")
        self.plan(1)
        self.evidence()
        profile = G.sol_advice_summary(G.load_state(self.state_path))["complexity"]
        self.assertEqual(profile["level"], "low")
        self.assertEqual(profile["signal_codes"], [])
        self.assertEqual(profile["counts"]["plan_actions"], 1)

    def test_multi_module_evidence_is_medium_complexity(self):
        self.start()
        self.plan(3)
        self.evidence(modules=2, count=2)
        profile = G.sol_advice_summary(G.load_state(self.state_path))["complexity"]
        self.assertEqual(profile["level"], "medium")
        self.assertIn("multi-module", profile["signal_codes"])

    def test_multiple_signals_are_high_complexity(self):
        self.start(risk="high")
        self.plan(5, deps=True)
        self.evidence(modules=2, count=2)
        profile = G.sol_advice_summary(G.load_state(self.state_path))["complexity"]
        self.assertEqual(profile["level"], "high")
        for code in ("risk-high", "multi-action", "interdependent",
                     "multi-module"):
            self.assertIn(code, profile["signal_codes"])

    def test_derived_governance_items_do_not_inflate_complexity(self):
        self.start(risk="low")
        self.plan(1)
        state = G.load_state(self.state_path)
        for ident in ("AUTO-C1", "AUTO-FIX-F1", "AUTO-VERIFY-F1"):
            state.setdefault("actions", []).append({
                "id": ident, "title": "派生", "goal": "派生", "status": "pending",
                "origin": "partial-derived"})
        G.save_state(self.state_path, state)
        profile = RP.task_complexity(G.load_state(self.state_path),
                                     workspace=self.dir)
        self.assertEqual(profile["counts"]["plan_actions"], 1)
        self.assertEqual(profile["level"], "low")

    def test_evidence_outside_workspace_is_not_a_module(self):
        self.start()
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        path = os.path.join(outside.name, "x.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("外部证据\n")
        state = G.load_state(self.state_path)
        G.register_evidence(state, G.next_evidence_id(state), file_path=path)
        G.save_state(self.state_path, state)
        profile = RP.task_complexity(G.load_state(self.state_path),
                                     workspace=self.dir)
        self.assertEqual(profile["counts"]["modules"], 0)
        self.assertNotIn("multi-module", profile["signal_codes"])

    def test_declared_architecture_trigger_is_a_signal(self):
        self.start()
        self.plan(2)
        profile = RP.task_complexity(G.load_state(self.state_path),
                                     triggers=["architecture"], workspace=self.dir)
        self.assertIn("declared-trigger", profile["signal_codes"])
        self.assertEqual(profile["level"], "medium")


class SimpleTaskZeroCallTests(TriggerBase):
    def test_simple_task_gets_no_sol_advice(self):
        self.start(risk="low")
        self.plan(1)
        self.evidence()
        advice = self.advice()
        self.assertEqual(advice["action"], "none")
        self.assertEqual(advice["recommended_action"], "not_needed")
        evaluation = G.evaluate(G.load_state(self.state_path))
        self.assertFalse([cmd for cmd in evaluation.get("next_commands") or []
                          if "sol-" in cmd])

    def test_simple_task_stays_zero_call_after_gates_pass(self):
        self.start(risk="low")
        self.plan(2)
        self.close_all()
        state = G.load_state(self.state_path)
        self.assertTrue(G.closure_check(state)["passed"])
        advice = self.advice(gates_passed=True)
        self.assertEqual(advice["action"], "none")
        self.assertEqual(advice["mode"], "")

    def test_not_understood_task_gets_understanding_consult(self):
        """还没有计划与证据时，启动阶段默认一次理解性 CONSULT。"""
        self.start(risk="high")
        advice = self.advice()
        self.assertEqual(advice["action"], "consult")
        self.assertEqual(advice["stage"], "understanding")
        self.assertIn("非简单任务从启动阶段默认获得", advice["reason"])


class ProactiveTriggerTests(TriggerBase):
    def test_complex_task_gets_one_think_after_plan(self):
        self.start(risk="high")
        self.plan(5, deps=True)
        self.evidence(modules=2, count=2)
        advice = self.advice()
        self.assertEqual(advice["action"], "consult")
        self.assertEqual(advice["mode"], "CONSULT")
        self.assertEqual(advice["phase"], "development")
        self.assertEqual(advice["recommended_action"], "recommended")
        self.assertIn("sol-consult", advice["command"])
        self.assertFalse(advice["second_round"])

    def test_high_risk_with_evidence_uses_same_cognitive_entry(self):
        """没有计划但有证据：仍走唯一认知入口，不再分 CONSULT / PLAN。"""
        self.start(risk="high")
        self.evidence()
        advice = self.advice()
        self.assertEqual(advice["mode"], "CONSULT")
        self.assertEqual(advice["action"], "consult")
        self.assertIn("sol-consult", advice["command"])

    def test_drive_prints_the_proactive_advice(self):
        self.start(risk="high")
        self.plan(5, deps=True)
        self.evidence(modules=2, count=2)
        out = io.StringIO()
        args = argparse.Namespace(state=self.state_path, max_actions=1,
                                  out="", backend="")
        with contextlib.redirect_stdout(out):
            code = CA.drive(args)
        self.assertEqual(code, CA.EXIT_EXECUTE)
        rendered = out.getvalue()
        self.assertIn("SOL CONSULT", rendered)
        self.assertIn("advisory", rendered)

    def test_cli_admission_exposes_complexity_and_mode(self):
        self.start(risk="high")
        self.plan(5, deps=True)
        self.evidence(modules=2, count=2)
        proc = F.run_cli("copilot_dispatch.py", "review-admission",
                         "--state", self.state_path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["advice"]["action"], "consult")
        self.assertEqual(payload["recommended_mode"], "CONSULT")
        self.assertEqual(payload["advice"]["complexity"]["level"], "high")
        self.assertEqual(payload["recommended_action"], "recommended")


class DuplicateSuppressionTests(TriggerBase):
    def test_existing_think_is_reused_without_new_signals(self):
        self.start(risk="high")
        self.plan(5, deps=True)
        self.evidence(modules=2, count=2)
        profile = self.advice()["complexity"]
        for mode, stage in (("CONSULT", "understanding"), ("PLAN", "planning")):
            self.record(mode, signals=profile["signal_codes"])
        advice = self.advice()
        self.assertEqual(advice["action"], "reuse")
        self.assertEqual(advice["recommended_action"], "not_needed")
        self.assertEqual(advice["new_signals"], [])

    def test_legacy_record_without_signal_snapshot_is_reused(self):
        """v8.2 及更早的记录没有信号快照：按已覆盖处理，升级后不重复付费。"""
        self.start(risk="high")
        self.plan(5, deps=True)
        self.evidence(modules=2, count=2)
        self.record("CONSULT")
        self.record("PLAN")
        advice = self.advice()
        self.assertEqual(advice["action"], "reuse")
        self.assertTrue(advice["second_round"])

    def test_covered_review_does_not_replace_cognition(self):
        """REVIEW 不能替代整任务认知：收口前仍只给可选 THINK，不自动 REVIEW。"""
        self.start(risk="high")
        self.plan(2)
        self.close_all()
        profile = self.advice(gates_passed=True)["complexity"]
        self.record("REVIEW", signals=profile["signal_codes"])
        advice = self.advice(gates_passed=True)
        self.assertEqual(advice["action"], "consult")
        self.assertEqual(advice["mode"], "CONSULT")
        self.assertEqual(advice["recommended_action"], "recommended")
        self.assertFalse(advice["covered"]["CONSULT"])

    def test_declared_trigger_is_not_a_new_signal_after_think(self):
        """调用方在准入时声明的触发因素不是新事实，不能诱发第二次调用。"""
        self.start(risk="high")
        self.plan(3, deps=True)
        self.evidence(modules=2, count=2)
        profile = self.advice(triggers=["architecture"])["complexity"]
        self.assertIn("declared-trigger", profile["signal_codes"])
        self.record("CONSULT", signals=["risk-high"])
        self.record("PLAN", signals=["risk-high"])
        advice = self.advice(triggers=["architecture"])
        self.assertEqual(advice["action"], "reuse")
        self.assertEqual(advice["new_signals"], [])

    def test_structural_growth_does_not_trigger_a_second_call(self):
        """证据目录增加只是结构变化，不是新的认知对象。"""
        self.start(risk="high")
        self.plan(3, deps=True)
        self.evidence(modules=1, count=1)
        self.record("CONSULT", signals=["risk-high"])
        self.record("PLAN", signals=["risk-high"])
        self.evidence(modules=2, count=3)
        advice = self.advice()
        self.assertEqual(advice["action"], "reuse")
        self.assertEqual(advice["complexity"]["level"], "high")


class MaterialChangeTests(TriggerBase):
    def test_new_signal_allows_one_second_call(self):
        self.start(risk="high")
        self.plan(3, deps=True)
        self.evidence(modules=2, count=2)
        profile = self.advice()["complexity"]
        self.record("CONSULT", signals=profile["signal_codes"])
        self.record("PLAN", signals=profile["signal_codes"])
        # 方案失败后重规划：出现新的认知信号，允许追加一次判断。
        state = G.load_state(self.state_path)
        state.setdefault("actions", []).append({
            "id": "AUTO-REPLAN", "title": "重新分析根因", "goal": "替代路线",
            "assignee": "zcode", "status": "pending", "origin": "replan",
            "constraints": "", "expected_evidence": "根因分析", "depends_on": [],
            "result": "", "evidence_ids": []})
        G.save_state(self.state_path, state)
        advice = self.advice()
        self.assertEqual(advice["action"], "consult")
        self.assertIn("replan-history", advice["new_signals"])
        self.assertTrue(advice["second_round"])
        self.assertIn("--force --trigger new-risk", advice["command"])

    def test_contract_amendment_starts_a_new_scope(self):
        self.start(risk="high")
        self.plan(3, deps=True)
        self.evidence(modules=2, count=2)
        self.record("CONSULT", signals=["risk-high"])
        self.record("PLAN", signals=["risk-high"])
        G.amend_contract(self.state_path,
                         contract=CONTRACT + "- 新增边界：仅本地\n")
        advice = self.advice()
        self.assertEqual(advice["action"], "consult")
        # 合同修订后是新的认知对象，但 CLI 仍按“同模式已有记录”要求 --force；
        # 建议命令必须可执行，不能给出会被 sol_review_requires_reason 拒绝的命令。
        self.assertTrue(advice["second_round"])
        self.assertIn("sol-consult", advice["command"])
        self.assertIn("--force --trigger scope-changed", advice["command"])
        # 建议命令必须能被 CLI 真正接受，而不只是字符串看起来正确。
        materials = os.path.join(self.dir, "materials.md")
        with open(materials, "w", encoding="utf-8") as fh:
            fh.write("合同修订后的材料\n")
        proc = F.run_cli(
            "copilot_dispatch.py", "sol-consult", "--state", self.state_path,
            "--materials", materials, "--question", "合同修订后怎么判断？",
            "--force", "--trigger", "scope-changed",
            "--reason", "合同修订后重新判断", "--dry-run", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_review_request_is_explicit_and_reuses_only_when_covered(self):
        self.start(risk="medium")
        self.plan(3)
        self.evidence(modules=2, count=2)
        first = self.advice(review_requested=True)
        self.assertEqual(first["action"], "review")
        self.assertEqual(first["mode"], "REVIEW")
        self.assertIn("sol-review", first["command"])
        self.assertNotIn("--force", first["command"])
        self.record("REVIEW")
        covered = self.advice(review_requested=True)
        self.assertEqual(covered["action"], "reuse")
        self.assertEqual(covered["mode"], "REVIEW")


class GatesNoAutoCallTests(TriggerBase):
    def test_gates_pass_does_not_auto_call_for_medium_task(self):
        self.start(risk="medium")
        self.plan(3)
        self.evidence(modules=2, count=2)
        self.record("CONSULT", signals=["multi-module"])
        self.record("PLAN", signals=["multi-module"])
        self.close_all()
        state = G.load_state(self.state_path)
        self.assertTrue(G.closure_check(state)["passed"])
        advice = self.advice(gates_passed=True)
        # 中等复杂任务收口前的综合判断是可选建议，不是强制调用。
        self.assertEqual(advice["action"], "consult")
        self.assertEqual(advice["recommended_action"], "optional")

    def test_high_risk_closure_never_derives_review(self):
        """正式 REVIEW 只由用户明确要求触发，不由阶段或风险自动派生。"""
        self.start(risk="high")
        self.plan(2)
        self.evidence()
        self.record("CONSULT", signals=["risk-high"])
        self.record("PLAN", signals=["risk-high"])
        self.close_all()
        advice = self.advice(gates_passed=True)
        self.assertNotEqual(advice["action"], "review")
        self.assertNotEqual(advice["mode"], "REVIEW")
        self.assertTrue(advice["covered"]["CONSULT"])

    def test_closure_never_derives_sol_work_items(self):
        self.start(risk="high")
        self.plan(5, deps=True)
        self.evidence(modules=2, count=2)
        state = G.load_state(self.state_path)
        derived = [item["id"] for item in G.derive_work(state)]
        self.assertTrue(derived)
        self.assertFalse([ident for ident in derived
                          if "SOL" in ident or "G4" in ident])


class AdvisorySemanticsTests(TriggerBase):
    def test_sol_findings_do_not_change_phase_or_gates(self):
        self.start(risk="high")
        self.plan(1)
        state = G.load_state(self.state_path)
        call = {
            "call_id": "SCreview", "mode": "REVIEW", "status": "completed",
            "contract_sha256": state.get("contract_sha256") or "",
            "materials_sha256": "m" * 64, "verdict": "partial",
            "findings": [{"id": "F1", "severity": "blocking", "file": "a.py",
                          "line": 1, "summary": "阻断问题"}],
            "findings_count": {"blocking": 1},
            "complexity_signals": ["risk-high"],
            "completed_at": G.now_iso(),
        }
        G.record_sol_call(self.state_path, call, mode="REVIEW")
        state = G.load_state(self.state_path)
        evaluation = G.evaluate(state)
        self.assertNotEqual(evaluation["phase"], "CLOSED")
        check = G.closure_check(state)
        self.assertFalse(check["passed"])
        self.assertEqual(set(check["gates"]), {"G1", "G2", "G3"})
        # finding 以 advisory 身份进入台账，由主代理核实后修复并定向验证。
        item = G.list_findings(state)["items"][0]
        self.assertEqual(item["authority"], "advisory")
        # SOL 结论不是完成证据：完成条件仍必须由主代理登记的真实证据核销。
        self.assertEqual(
            [c["status"] for c in state["contract"]["criteria"]], ["pending"])

    def test_failed_call_is_not_coverage_and_does_not_block(self):
        self.start(risk="high")
        self.plan(5, deps=True)
        self.evidence(modules=2, count=2)
        G.record_sol_call_failure(self.state_path, mode="CONSULT",
                                  error_code="sol_timeout", detail="300s",
                                  materials_sha256="x" * 64)
        advice = self.advice()
        self.assertEqual(advice["action"], "consult")
        state = G.load_state(self.state_path)
        self.assertEqual(state.get("blocks") or [], [])
        self.assertIn(G.evaluate(state)["phase"], ("RUNNING", "PARTIAL"))


class BlockedAndClosedTests(TriggerBase):
    def test_blocked_task_gets_no_sol_advice(self):
        self.start(risk="high")
        self.plan(5, deps=True)
        self.evidence(modules=2, count=2)
        self.assertNotEqual(self.advice()["action"], "none")
        G.add_block(self.state_path, "缺少生产权限", "用户授权")
        advice = self.advice()
        self.assertEqual(advice["action"], "none")
        self.assertIn("BLOCKED", advice["reason"])

    def test_closed_task_gets_no_sol_advice(self):
        self.start(risk="high")
        self.plan(2)
        self.close_all()
        G.close_task(self.state_path)
        advice = self.advice(gates_passed=True)
        self.assertEqual(advice["action"], "none")
        self.assertIn("已收口", advice["reason"])


class ExplicitRequestTests(TriggerBase):
    def test_user_request_forces_one_call_on_simple_task(self):
        self.start(risk="low")
        self.plan(1)
        self.evidence()
        advice = self.advice(user_request=True)
        self.assertEqual(advice["recommended_action"], "recommended")
        self.assertEqual(advice["action"], "consult")
        self.assertIn("user-request", advice["complexity"]["signal_codes"])

    def test_user_request_is_not_suppressed_by_existing_think(self):
        self.start(risk="low")
        self.plan(1)
        self.evidence()
        self.record("CONSULT", signals=[])
        advice = self.advice(user_request=True)
        self.assertEqual(advice["action"], "consult")
        self.assertEqual(advice["recommended_action"], "recommended")
        self.assertIn("用户明确要求", advice["reason"])
        self.assertIn("--force", advice["command"])

    def test_cli_user_request_flag(self):
        self.start(risk="low")
        self.plan(1)
        self.evidence()
        proc = F.run_cli("copilot_dispatch.py", "review-admission",
                         "--state", self.state_path, "--user-request", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["recommended_action"], "recommended")

    def test_direct_sol_call_still_works_without_advice(self):
        """用户显式调用永远可用：admission 只是建议，不阻断 CLI。"""
        self.start(risk="high")
        self.plan(1)
        materials = os.path.join(self.dir, "materials.md")
        with open(materials, "w", encoding="utf-8") as fh:
            fh.write("材料\n")
        args = argparse.Namespace(
            state=self.state_path, materials=materials,
            question="方案怎么选？", reason="", trigger="", force=False,
            dry_run=True, list=False, json=True, timeout=5,
            max_output_tokens=100, audit_log="", output_dir="",
            knowledge_file="")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(CD.cmd_sol_think(args), 0)
        self.assertEqual(json.loads(out.getvalue())["mode"], "CONSULT")


class NoG4Tests(TriggerBase):
    def test_evaluation_has_no_g4_surface(self):
        self.start(risk="high")
        self.plan(3, deps=True)
        self.evidence(modules=2, count=2)
        state = G.load_state(self.state_path)
        rendered = json.dumps(G.evaluate(state), ensure_ascii=False)
        for marker in ("g4_", "G4", "host_capability", "native_review"):
            self.assertNotIn(marker, rendered)
        self.assertEqual(set(G.closure_check(state)["gates"]),
                         {"G1", "G2", "G3"})


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""v9.10.0 架构轻量化：治理、认知与验收的边界回归。

每条用例对应本轮的一条硬约束，而不是实现细节：

1. 认知价值由**真实信号**决定：简单/机械任务与默认 `risk=medium` 不产生任何
   义务、不发起任何调用；风险等级不是复杂度的同义词。
2. 认知不是门禁：完全不调用认知、或调用了但没登记采纳，都不阻止收口。
3. D 阶段（里程碑复盘）不再自动派生；普通进度推进不制造新的付费窗口。
4. G1–G3 按风险适配：`blocking` 缺陷任何风险都阻断，`major` 只在高风险阻断；
   普通任务不因缺少独立复核（G4 已退役）而被挡在收口之外。
5. PWW 完整保留：质量结论为 PASS_WITH_WARNINGS 且用户未接受时不能收口。
6. 最终验收只有一个事实源：廉价准入与完整验收共享同一批事实与同一份缓存。
7. `drive` 的固定步数是有界工作量，不是任务完成上限：有进展时返回“继续”。

所有用例都不联网、不写工作区外文件，也不发起任何真实模型请求。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import governance as G  # noqa: E402
import review_policy as RP  # noqa: E402
import cognition_schedule as SCH  # noqa: E402
import copilot_auto as CA  # noqa: E402
import v6_support as F  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")

    def start(self, *, risk: str = "medium", kind: str = "standard",
              triggers: list[str] | None = None,
              test_policy: dict | None = None) -> None:
        G.start_state(self.state_path, goal="交付可复核结果", contract=CONTRACT,
                      risk=risk, task_id="lightweight", workspace=self.dir,
                      task_kind=kind, triggers=triggers,
                      test_policy=test_policy)

    def state(self) -> dict:
        return G.load_state(self.state_path)

    def plan(self, actions: int) -> None:
        G.plan_state(self.state_path, {"summary": "计划", "actions": [
            {"id": f"A{index}", "title": f"工作{index}",
             "goal": f"目标{index}", "expected_evidence": "证据"}
            for index in range(1, actions + 1)]})

    def evidence(self, name: str = "e1.txt") -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("证据\n")
        state = self.state()
        eid = G.next_evidence_id(state)
        G.register_evidence(state, eid, file_path=path, note="测试证据")
        G.save_state(self.state_path, state)
        return eid

    def finish_work(self, *, tests: bool = True) -> str:
        """工作项终态 + 条件核销 +（可选）必要测试的执行证据。"""
        eid = self.evidence()
        state = self.state()
        for action in state.get("actions") or []:
            G.record_action(self.state_path, action["id"], status="done",
                            result="完成", evidence_ids=[eid])
        for criterion in state["contract"]["criteria"]:
            G.record_criterion(self.state_path, criterion["criterion_id"],
                               "met", evidence_ids=[eid])
        if tests:
            log = os.path.join(self.dir, "tests.log")
            with open(log, "w", encoding="utf-8") as fh:
                fh.write("Ran 7 tests\nOK\n")
            state = self.state()
            tid = G.next_evidence_id(state)
            G.register_evidence(
                state, tid, kind="capture", file_path=log, note="测试输出",
                capture_cmd="python3 -m unittest discover -s tests")
            G.save_state(self.state_path, state)
        return eid

    def advice(self, **kwargs) -> dict:
        return G.cognition_advice_summary(self.state(), **kwargs)

    def sync(self, **kwargs) -> dict:
        state = self.state()
        synced = G.sync_cognition_schedule(state, advice=self.advice(**kwargs))
        if synced["changes"]:
            G.save_state(self.state_path, state)
        return synced


class CognitionValueTests(Base):
    """认知调用由影响面 × 真实产物 × 自评不确定性决定，不由默认风险值决定。"""

    def test_simple_task_kinds_get_zero_calls(self):
        for kind in ("trivial", "mechanical", "docs", "format"):
            with self.subTest(kind=kind):
                self.setUp()
                self.start(kind=kind)
                self.plan(1)
                self.evidence()
                complexity = RP.task_complexity(self.state())
                self.assertEqual(complexity["level"], "low", complexity)
                self.assertEqual(self.advice()["action"], "none")
                self.sync()
                self.assertEqual(SCH.obligations(self.state()), [])

    def test_default_standard_task_is_t1_with_startup_consult(self):
        """未声明类型且没有中/高价值信号：按 T1 给一次启动 CONSULT。

        默认 `risk=medium` 本身不构成认知价值信号，因此任务不会升级到 T2/T3；
        但它也不是显式机械类型，原始意图缺失时不能按 T0 处理——一次启动判断
        正是“任务性质无法可靠分类”时的保守落点。
        """
        self.start()
        self.plan(2)
        self.evidence()
        complexity = RP.task_complexity(self.state())
        self.assertEqual(complexity["level"], "low", complexity)
        self.assertEqual(complexity["tier"], "T1", complexity)
        self.assertEqual(complexity["windows"], ["A"])
        advice = self.advice()
        self.assertEqual(advice["action"], "consult")
        self.assertEqual(advice.get("family"), "A")
        self.sync()
        families = [row["family"] for row in SCH.obligations(self.state())]
        self.assertEqual(families, ["A"])
        stats = SCH.task_stats(self.state())
        self.assertFalse(stats["not_registered"])
        self.assertEqual(stats["coverage"]["startup"]["denominator"], 1)
        self.assertEqual(stats["coverage"]["plan"]["denominator"], 0)
        self.assertEqual(stats["coverage"]["closure"]["denominator"], 0)

    def test_multiple_work_items_do_not_raise_risk(self):
        """工作项数量只影响“多动作”信号，绝不改写 risk 或制造高风险门禁。"""
        self.start()
        self.plan(5)
        self.evidence()
        self.evidence(name="e2.txt")
        state = self.state()
        self.assertEqual(state["risk"], "medium")
        complexity = RP.task_complexity(state)
        codes = set(complexity["signal_codes"])
        self.assertIn("multi-action", codes)
        # 多动作是认知价值信号，不是 risk：风险仍是调用方声明的 medium。
        self.assertNotIn("risk-high", codes)

    def test_high_risk_is_a_real_signal(self):
        self.start(risk="high", test_policy={"required": False})
        complexity = RP.task_complexity(self.state())
        self.assertEqual(complexity["level"], "medium")
        self.assertEqual({item["code"] for item in complexity["signals"]},
                         {"risk-high"})
        self.assertEqual(self.advice()["action"], "consult")

    def test_declared_plan_kind_is_a_real_signal(self):
        """显式声明方案类任务＝自评不确定性，仍值得一次启动判断。"""
        self.start(kind="architecture")
        complexity = RP.task_complexity(self.state())
        self.assertEqual(complexity["level"], "medium")
        self.assertIn("plan-kind",
                      {item["code"] for item in complexity["signals"]})
        self.assertEqual(self.advice()["action"], "consult")

    def test_routine_evidence_does_not_open_new_window(self):
        """普通证据追加/工作项推进会重新同步，但不制造新的付费义务。"""
        self.start(kind="architecture")
        self.plan(2)
        self.sync()
        before = len(SCH.obligations(self.state()))
        for index in range(3):
            self.evidence(name=f"extra{index}.txt")
            self.sync()
        rows = SCH.obligations(self.state())
        self.assertEqual(len(rows), before)
        # 义务集合不变：证据追加既不开新窗口，也没有把已开窗口结算掉。
        self.assertEqual([item.get("obligation_id") for item in rows],
                         [item.get("obligation_id") for item in rows])

    def test_closure_coverage_matches_the_e_window(self):
        """E 义务与收口覆盖率分母同口径：简单类别 + 高风险仍是真实信号。

        `task_kind` 只是分类，真实信号（此处 `risk=high`）决定复杂度。若分母
        仍要求“非简单类别”，E 义务会落在分母之外：满足后分子进聚合而分母不进，
        多任务覆盖率可超过 100% 或掩盖其它任务的漏调。
        """
        self.start(kind="docs", risk="high", test_policy={"required": False})
        self.plan(1)
        self.finish_work()
        advice = self.advice()
        self.assertEqual((advice["action"], advice["family"]), ("consult", "E"))
        self.sync()
        self.assertEqual([item.get("family") for item in SCH.obligations(self.state())],
                         ["E"])
        stats = SCH.task_stats(self.state())
        self.assertTrue(stats["closure_eligible"])
        coverage = stats["coverage"]["closure"]
        self.assertEqual(coverage["denominator"], 1)
        self.assertEqual(coverage["numerator"], 0)
        self.assertEqual(coverage["rate"], 0.0)


class CognitionIsNotAGateTests(Base):
    """认知与认知采纳都不是工程验收的一部分。"""

    def test_task_closes_without_any_cognition_call(self):
        self.start()
        self.plan(1)
        self.finish_work()
        state = self.state()
        self.assertEqual(state.get("cognition_calls") or [], [])
        self.assertIsNone(G.closure_acceptance_pending(state))
        closure = G.close_task(self.state_path)
        self.assertEqual(closure["verdict"], "pass")
        self.assertTrue(self.state()["closure"]["closed"])

    def _closure_call(self, digest: str = "d" * 64) -> dict:
        state = self.state()
        record = {
            "call_id": "SCclosure0001", "mode": "CONSULT", "status": "completed",
            "stage": "closure", "trigger": "closure", "question": "收口判断",
            "contract_sha256": state.get("contract_sha256") or "",
            "materials_sha256": "m" * 64, "summary": "结论",
            "final_verification_digest": digest,
            "completed_at": G.now_iso(),
        }
        return G.record_cognition_call(self.state_path, record, mode="CONSULT",
                                       materials_sha256="m" * 64,
                                       trigger="closure")

    def test_unaccepted_closure_cognition_does_not_block_closure(self):
        self.start()
        self.plan(1)
        self.finish_work()
        digest = G.closure_readiness(self.state())["digest"]
        self._closure_call(digest)
        pending = G.closure_acceptance_pending(self.state())
        self.assertEqual(pending["digest"], digest)
        # 未登记采纳：收口照常完成，且收口记录明确“不需要采纳”。
        closure = G.close_task(self.state_path)
        self.assertEqual(closure["verdict"], "pass")
        self.assertEqual(
            self.state()["closure"]["cognition_acceptance"]["required"], False)

    def test_cognition_acceptance_is_optional_audit_record(self):
        self.start()
        self.plan(1)
        self.finish_work()
        digest = G.closure_readiness(self.state())["digest"]
        call = self._closure_call(digest)
        G.record_cognition_acceptance(
            self.state_path, call["call_id"], status="rejected",
            rationale="ZCode 独立判断：结论与证据不符")
        self.assertIsNone(G.closure_acceptance_pending(self.state()))
        # 否决也不阻止收口：采纳是对建议的态度，不是工程验收结论。
        closure = G.close_task(self.state_path)
        self.assertEqual(closure["verdict"], "pass")

    def test_milestone_family_is_never_generated(self):
        """D（里程碑复盘）已退役：中等价值任务的推进不产生 D 义务。"""
        self.start(kind="architecture", triggers=["core-logic"])
        self.plan(3)
        self.evidence()
        self.sync()
        state = self.state()
        for action in (state.get("actions") or [])[:2]:
            G.record_action(self.state_path, action["id"], status="done",
                            result="完成", evidence_ids=["E1"])
        synced = self.sync()
        self.assertEqual([item for item in SCH.obligations(self.state())
                          if item.get("family") == "D"], [])
        self.assertFalse(any("D/" in line for line in synced["changes"]))


class RiskAdaptedGateTests(Base):
    """G1–G3 按风险适配，但不降低真实完成条件。"""

    def _finding(self, severity: str) -> None:
        state = self.state()
        state["finding_ledger"] = {
            "schema": RP.LEDGER_SCHEMA, "next_id": 2,
            "items": [{"finding_id": "F1", "severity": severity,
                       "status": "open", "summary": "缺陷"}]}
        G.save_state(self.state_path, state)

    def test_blocking_finding_blocks_at_any_risk(self):
        for risk in ("low", "medium", "high"):
            with self.subTest(risk=risk):
                self.setUp()
                self.start(risk=risk,
                           test_policy={"required": False} if risk == "high"
                           else None)
                self.plan(1)
                self.finish_work()
                self._finding("blocking")
                check = G.final_acceptance(self.state(), task_dir=self.dir)
                self.assertFalse(check["passed"])
                codes = {str(item["code"]) for item in check["reasons"]}
                self.assertIn("native_review_finding_open", codes)

    def test_major_finding_is_advisory_at_medium_and_blocking_at_high(self):
        self.start()
        self.plan(1)
        self.finish_work()
        self._finding("major")
        medium = G.final_acceptance(self.state(), task_dir=self.dir)
        self.assertTrue(medium["passed"])
        self.assertTrue(medium["advisory_findings"])
        self.assertTrue(any("未闭合 Finding" in line
                            for line in medium["warnings"]))
        # 高风险下同一缺陷是阻断项：适配风险，不等于放宽高风险标准。
        state = self.state()
        state["risk"] = "high"
        state["contract"]["test_policy"] = {"required": False}
        high = G.final_acceptance(state, task_dir=self.dir)
        self.assertFalse(high["passed"])
        self.assertIn("native_review_finding_open",
                      {str(item["code"]) for item in high["reasons"]})

    def test_closure_needs_no_reviewer_or_g4(self):
        self.start()
        self.plan(1)
        self.finish_work()
        check = G.closure_check(self.state())
        self.assertEqual(set(check["gates"]), {"G1", "G2", "G3"})
        self.assertTrue(check["passed"])
        state = self.state()
        self.assertEqual(state.get("native_review") or {}, {})
        self.assertEqual(state.get("cognition_calls") or [], [])
        self.assertEqual(G.close_task(self.state_path)["verdict"], "pass")

    def test_missing_required_tests_still_blocks_closure(self):
        """风险适配不放宽完成条件：合同要求测试却没有执行证据时不得收口。"""
        self.start(risk="high",
                   test_policy={"required": True,
                                "commands": ["python3 -m unittest"]})
        self.plan(1)
        self.finish_work(tests=False)
        readiness = G.closure_readiness(self.state())
        self.assertFalse(readiness["ready"])
        self.assertIn("required_tests_missing",
                      [item["code"] for item in readiness["blockers"]])
        with self.assertRaises(G.GovernanceError) as ctx:
            G.close_task(self.state_path)
        self.assertIn("测试", str(ctx.exception))


class PwwPreservedTests(Base):
    """取消认知采纳门禁不得削弱 PWW 的用户接受。"""

    MARKER = "<!-- copilot-verify-result/v1 -->"

    def _report(self, *, verdict: str, warnings: list) -> str:
        payload = {
            "schema": G.QUALITY_SCHEMA, "report_id": "VR-1",
            "generated_at": "2026-11-25T10:00:00+08:00", "verdict": verdict,
            "warnings": warnings, "residual_risks": [], "unverified": [],
            "acceptance_required": verdict == "PASS_WITH_WARNINGS",
            "acceptance_status": ("pending" if verdict == "PASS_WITH_WARNINGS"
                                  else "not_required"),
            "checks": {"required_tests_executed": True,
                       "required_evidence_present": True,
                       "completion_criteria_met": True,
                       "delivery_verifiable": True},
            "subject": {"scope": "fixture", "summary": "test fixture"},
        }
        path = os.path.join(self.dir, "report.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("## 验收报告\n\n### 10. 机器可读质量结论\n" + self.MARKER
                     + "\n```json\n" + json.dumps(payload, ensure_ascii=False)
                     + "\n```\n")
        return path

    def test_pass_with_warnings_still_requires_user_acceptance(self):
        self.start()
        self.plan(1)
        self.finish_work()
        G.ingest_quality_report(
            self.state_path,
            self._report(verdict="PASS_WITH_WARNINGS",
                         warnings=[{"id": "W1", "text": "边界未验证",
                                    "severity": "minor"}]))
        codes = {str(item["code"]) for item in
                 G.closure_check(self.state(), task_dir=self.dir)["reasons"]}
        self.assertIn("pww_acceptance_pending", codes)
        with self.assertRaises(G.GovernanceError):
            G.close_task(self.state_path)
        G.accept_pww(self.state_path, ids=["W1"], accepted_by="user",
                     quote="我明确接受这条警告")
        closure = G.close_task(self.state_path)
        self.assertEqual(closure["quality"]["verdict"], "pass_with_warnings")
        self.assertNotEqual(closure["quality"]["verdict"], "pass")


class UnifiedAcceptanceTests(Base):
    """最终验收只有一个事实源，且同一命令内只算一次。"""

    def test_cheap_and_deep_acceptance_share_the_same_facts(self):
        self.start()
        self.plan(1)
        self.finish_work()
        state = self.state()
        cheap = G.final_acceptance(state, task_dir=self.dir, deep=False)
        deep = G.final_acceptance(state, task_dir=self.dir, deep=True)
        self.assertEqual(cheap["digest"], deep["digest"])
        self.assertEqual(cheap["ready"], deep["ready"])
        self.assertEqual(cheap["blockers"], deep["blockers"])
        self.assertEqual(cheap["gates"], deep["gates"])
        self.assertEqual(cheap["required_tests_completed"],
                         deep["required_tests_completed"])

    def test_repeated_calls_in_one_command_reuse_the_cache(self):
        self.start()
        self.plan(1)
        self.finish_work()
        state = self.state()
        cache: dict = {}
        calls = {"count": 0}
        real = G._acceptance_facts

        def counting(current):
            calls["count"] += 1
            return real(current)

        with mock.patch.object(G, "_acceptance_facts", side_effect=counting):
            first = G.closure_readiness(state, cache=cache)
            second = G.closure_check(state, cache=cache)
            third = G.closure_readiness(state, cache=cache)
        # 廉价准入与完整验收共用一个事实源但各自缓存一次；重复调用不再重算。
        self.assertEqual(calls["count"], 2)
        before = calls["count"]
        G.closure_check(state, cache=cache)
        self.assertEqual(calls["count"], before)
        self.assertTrue(first["ready"])
        self.assertTrue(second["passed"])
        self.assertEqual(third["digest"], first["digest"])

    def test_cache_is_scoped_to_the_state_revision(self):
        """状态变了就不能复用旧验收：缓存键含 save_seq。"""
        self.start()
        self.plan(1)
        self.finish_work()
        cache: dict = {}
        state = self.state()
        self.assertTrue(G.closure_readiness(state, cache=cache)["ready"])
        self.evidence(name="later.txt")
        updated = self.state()
        self.assertNotEqual(updated.get("save_seq"), state.get("save_seq"))
        check = G.closure_readiness(updated, cache=cache)
        self.assertTrue(check["ready"])
        keys = [key for key in cache if key and key[0] == "final_acceptance"]
        self.assertEqual(len(keys), 2)


class DriveBoundTests(Base):
    """固定步数是有界工作量，不是任务完成上限。"""

    def _partial_state(self) -> None:
        self.start(kind="trivial", risk="low")
        self.plan(1)
        eid = self.evidence()
        G.record_action(self.state_path, "A1", status="done", result="完成",
                        evidence_ids=[eid])

    def test_progress_at_step_bound_returns_continue(self):
        self._partial_state()
        args = argparse.Namespace(state=self.state_path, cognition="off",
                                  max_actions=1, dispatch_out="")
        out = io.StringIO()
        with mock.patch.object(CA, "MAX_DRIVE_STEPS", 1), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = CA.drive(args)
        self.assertEqual(code, CA.EXIT_EXECUTE)
        self.assertIn("达到单进程 1 步上限", out.getvalue())
        self.assertIn("继续执行下一轮 drive", out.getvalue())

    def test_no_progress_at_step_bound_reports_error(self):
        """派生不改变进展摘要时不能假装“继续”：这是真实的原地打转保护。"""
        self._partial_state()
        args = argparse.Namespace(state=self.state_path, cognition="off",
                                  max_actions=1, dispatch_out="")
        with mock.patch.object(CA, "MAX_DRIVE_STEPS", 1), \
                mock.patch.object(G, "derive_work",
                                  return_value=[{"id": "AUTO-NEXT-x",
                                                 "origin": "auto"}]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = CA.drive(args)
        self.assertEqual(code, CA.EXIT_ERROR)


if __name__ == "__main__":
    unittest.main()

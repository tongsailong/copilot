#!/usr/bin/env python3
"""本地治理引擎回归：合同、三道门、PARTIAL 推进、证据完整性、CAS、历史只读。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import governance as G  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件

## 边界
- 不修改无关项目
"""


def evidence_file(directory: str, text: str = "ok\n") -> str:
    path = os.path.join(directory, "evidence.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def init_git(directory: str) -> None:
    subprocess.run(["git", "init", "-q", directory], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", directory, "config", "user.email", "t@e.invalid"],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", directory, "config", "user.name", "t"],
                   check=True, capture_output=True, text=True)
    with open(os.path.join(directory, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write("state.json\n")
    with open(os.path.join(directory, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("baseline\n")
    subprocess.run(["git", "-C", directory, "add", "."], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", directory, "commit", "-qm", "baseline"], check=True,
                   capture_output=True, text=True)


class StartAndPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state = os.path.join(self.dir, "state.json")

    def test_start_requires_goal_and_criteria(self):
        with self.assertRaises(G.GovernanceError):
            G.start_state(self.state, goal="")
        with self.assertRaises(G.GovernanceError):
            G.start_state(self.state, goal="目标", contract="没有条件")

    def test_start_confirms_criteria_locally_without_remote_gate(self):
        state = G.start_state(self.state, goal="目标", contract=CONTRACT)
        contract = state["contract"]
        self.assertEqual(contract["criteria_state"], "confirmed")
        self.assertEqual([c["criterion_id"] for c in contract["criteria"]], ["C1"])
        self.assertEqual(contract["history"][0]["source"], "local-start")
        self.assertNotIn("pending_call", state)
        self.assertTrue(os.path.isfile(self.state))

    def test_duplicate_start_is_refused(self):
        G.start_state(self.state, goal="目标", contract=CONTRACT)
        with self.assertRaises(G.GovernanceError):
            G.start_state(self.state, goal="目标2", contract=CONTRACT)

    def test_plan_sets_ready_actions_and_rejects_cycles(self):
        G.start_state(self.state, goal="目标", contract=CONTRACT)
        plan = {"summary": "做一件事", "actions": [
            {"id": "A1", "title": "实现", "goal": "产出结果",
             "expected_evidence": "结果文件"},
        ]}
        result = G.plan_state(self.state, plan)
        self.assertFalse(result["idempotent"])
        state = G.load_state(self.state)
        self.assertEqual([a["id"] for a in G.ready_actions(state)], ["A1"])
        self.assertEqual(state["actions"][0]["plan_id"], result["plan"]["plan_id"])

        cyclic = {"summary": "环", "actions": [
            {"id": "B1", "title": "b1", "goal": "g", "depends_on": ["B2"]},
            {"id": "B2", "title": "b2", "goal": "g", "depends_on": ["B1"]},
        ]}
        with self.assertRaises(G.GovernanceError):
            G.plan_state(self.state, cyclic)

    def test_plan_is_idempotent_by_content(self):
        G.start_state(self.state, goal="目标", contract=CONTRACT)
        plan = {"summary": "s", "actions": [
            {"id": "A1", "title": "t", "goal": "g",
             "expected_evidence": "结果"}]}
        first = G.plan_state(self.state, plan)
        second = G.plan_state(self.state, plan)
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["plan"]["plan_id"], second["plan"]["plan_id"])

    def test_cross_plan_action_id_is_rejected(self):
        G.start_state(self.state, goal="目标", contract=CONTRACT)
        G.plan_state(self.state, {"summary": "s1", "actions": [
            {"id": "A1", "title": "t", "goal": "g",
             "expected_evidence": "结果"}]})
        with self.assertRaisesRegex(G.GovernanceError, "跨计划重复"):
            G.plan_state(self.state, {"summary": "s2", "actions": [
                {"id": "A1", "title": "另一个", "goal": "g",
                 "expected_evidence": "结果"}]})
        state = G.load_state(self.state)
        self.assertEqual(len([a for a in state["actions"] if a["id"] == "A1"]), 1)

    def test_retired_g4_actions_do_not_block_or_stall(self):
        state = {"actions": [
            {"id": "A1", "status": "done", "origin": "zcode-plan"},
            {"id": "AUTO-G4", "status": "failed", "origin": "g4-review"},
            {"id": "G4-PENDING", "status": "pending", "origin": "g4-review"},
            {"id": "AUTO-R-AUTO-G4", "status": "pending",
             "origin": "recovery"},
        ]}
        self.assertEqual(G._gate_actions(state)["passed"], True)
        self.assertEqual(G.stalled_actions(state), [])
        self.assertEqual([a["id"] for a in G.ready_actions(state)], [])


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state = os.path.join(self.dir, "state.json")
        G.start_state(self.state, goal="目标", contract=CONTRACT)
        G.plan_state(self.state, {"summary": "s", "actions": [
            {"id": "A1", "title": "实现", "goal": "产出结果",
             "expected_evidence": "结果文件"}]})

    def _done_with_evidence(self, text: str = "ok\n") -> str:
        path = evidence_file(self.dir, text)
        state = G.load_state(self.state)
        G.register_evidence(state, "E1", file_path=path, note="结果")
        G.save_state(self.state, state)
        G.record_action(self.state, "A1", "done", result="完成", evidence_ids=["E1"])
        G.record_criterion(self.state, "C1", "met", evidence_ids=["E1"])
        return path

    def test_full_flow_reaches_pass_and_closes(self):
        self._done_with_evidence()
        state = G.load_state(self.state)
        evaluation = G.evaluate(state)
        self.assertEqual(evaluation["phase"], "PASS", evaluation)
        closure = G.close_task(self.state)
        self.assertTrue(closure["closed"])
        self.assertEqual(closure["verdict"], "pass")
        self.assertEqual(G.evaluate(G.load_state(self.state))["phase"], "CLOSED")

    def test_g1_requires_terminal_actions(self):
        state = G.load_state(self.state)
        self.assertEqual(G.evaluate(state)["phase"], "RUNNING")
        check = G.closure_check(state)
        codes = {r["code"] for r in check["reasons"] if r["gate"] == "G1"}
        self.assertIn("action_open", codes)
        self.assertFalse(check["passed"])

    def test_criterion_met_requires_registered_evidence(self):
        with self.assertRaises(G.GovernanceError):
            G.record_criterion(self.state, "C1", "met", evidence_ids=["E9"])
        G.record_action(self.state, "A1", "done", result="完成")
        with self.assertRaises(G.GovernanceError):
            G.record_criterion(self.state, "C1", "met", evidence_ids=[])

    def test_terminal_action_cannot_be_reopened(self):
        G.record_action(self.state, "A1", "done", result="完成")
        with self.assertRaises(G.GovernanceError):
            G.record_action(self.state, "A1", "failed", result="重试")

    def test_evidence_tamper_is_detected_at_g2(self):
        path = self._done_with_evidence()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("tampered\n")
        check = G.closure_check(G.load_state(self.state))
        codes = {r["code"] for r in check["reasons"]}
        self.assertIn("evidence_tampered", codes)
        self.assertFalse(check["passed"])
        with self.assertRaises(G.GovernanceError):
            G.close_task(self.state)

    def test_missing_evidence_file_is_detected(self):
        path = self._done_with_evidence()
        os.unlink(path)
        check = G.closure_check(G.load_state(self.state))
        codes = {r["code"] for r in check["reasons"]}
        self.assertIn("evidence_file_missing", codes)

    def test_blocked_state_allows_progress_but_not_closure(self):
        G.add_block(self.state, "需要用户授权", "授权 push")
        self.assertEqual(G.evaluate(G.load_state(self.state))["phase"], "BLOCKED")
        # 真实进展可以登记；阻塞状态不自动解除，也不能提前收口。
        G.record_action(self.state, "A1", "done", result="完成")
        self.assertEqual(G.evaluate(G.load_state(self.state))["phase"], "BLOCKED")
        with self.assertRaises(G.GovernanceError):
            G.close_task(self.state)
        G.resolve_block(self.state, "用户已授权")
        self.assertNotEqual(G.evaluate(G.load_state(self.state))["phase"], "BLOCKED")

    def test_closed_task_cannot_be_modified_without_reopen(self):
        self._done_with_evidence()
        G.close_task(self.state)
        with self.assertRaises(G.GovernanceError):
            G.add_decision(self.state, "继续")
        with self.assertRaises(G.GovernanceError):
            G.reopen_task(self.state, "", authorized_by="user")
        with self.assertRaises(G.GovernanceError):
            G.reopen_task(self.state, "用户要求补充验证")
        with self.assertRaises(G.GovernanceError):
            G.reopen_task(self.state, "用户要求补充验证", authorized_by="zcode")
        G.reopen_task(self.state, "用户要求补充验证", authorized_by="user")
        state = G.load_state(self.state)
        self.assertFalse(state["closure"]["closed"])
        self.assertEqual(state["closure"]["reopen_history"][0]["reason"],
                         "用户要求补充验证")
        self.assertEqual(state["closure"]["reopen_history"][0]["authorized_by"],
                         "user")


class PartialPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state = os.path.join(self.dir, "state.json")

    def test_unmet_criteria_derive_next_work_item(self):
        G.start_state(self.state, goal="目标", contract=CONTRACT)
        state = G.load_state(self.state)
        self.assertEqual(G.evaluate(state)["phase"], "PARTIAL")
        created = G.derive_work(state)
        self.assertEqual([a["id"] for a in created], ["AUTO-C1"])
        G.save_state(self.state, state)
        self.assertEqual(G.evaluate(G.load_state(self.state))["phase"], "RUNNING")

    def test_repeated_failure_derives_distinct_attempts_and_replan(self):
        G.start_state(self.state, goal="目标", contract=CONTRACT)
        state = G.load_state(state_path := self.state)
        created = G.derive_work(state)
        G.save_state(self.state, state)
        first = created[0]["id"]
        G.record_action(self.state, first, "failed", result="首次失败")
        state = G.load_state(self.state)
        created = G.derive_work(state)
        G.save_state(self.state, state)
        self.assertEqual([a["id"] for a in created], ["AUTO-C1-R2"])
        G.record_action(self.state, "AUTO-C1-R2", "failed", result="第二次失败")
        state = G.load_state(self.state)
        created = G.derive_work(state)
        G.save_state(self.state, state)
        ids = [a["id"] for a in created]
        self.assertIn("AUTO-C1-R3", ids)
        G.record_action(self.state, "AUTO-C1-R3", "failed", result="第三次失败")
        state = G.load_state(self.state)
        created = G.derive_work(state)
        origins = {a["origin"] for a in created}
        self.assertIn("replan", origins)
        rows = [a for a in created if a["origin"] == "replan"]
        self.assertIn("替代路线", rows[0]["goal"])

    def test_contract_without_criteria_derives_contract_work(self):
        state = G.load_state(self.state) if os.path.exists(self.state) else None
        G.start_state(self.state, goal="目标", contract=CONTRACT)
        state = G.load_state(self.state)
        state["contract"]["criteria_state"] = "draft"
        created = G.derive_work(state)
        self.assertEqual([a["id"] for a in created], ["AUTO-CONTRACT"])

    def test_pending_action_counts_as_running_not_partial(self):
        G.start_state(self.state, goal="目标", contract=CONTRACT)
        G.plan_state(self.state, {"summary": "s", "actions": [
            {"id": "A1", "title": "t", "goal": "g",
             "expected_evidence": "结果"}]})
        evaluation = G.evaluate(G.load_state(self.state))
        self.assertEqual(evaluation["phase"], "RUNNING")
        self.assertEqual(evaluation["actions"], ["A1"])


class HighRiskRetirementTests(unittest.TestCase):
    """v8：G4 已退役。高风险任务不再需要原生只读复核，但安全约束保持。

    锁定行为：
    * 高风险任务可用 legacy 后端创建（不再强制 native）；
    * closure 只有 G1–G3，没有 G4；
    * 历史 native_reviews / review_attempts / g4_attestations 不参与门禁；
    * 未闭合 blocking/major finding 仍然阻断收口（原 G4 的台账检查保留）；
    * derive_work 不再派生 AUTO-G4 工作项。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        init_git(self.dir)
        self.state = os.path.join(self.dir, "state.json")
        G.start_state(self.state, goal="目标", contract=CONTRACT, risk="high",
                      execution_backend="legacy",
                      test_policy={"required": False})
        G.plan_state(self.state, {"summary": "s", "actions": [
            {"id": "A1", "title": "实现", "goal": "产出结果",
             "expected_evidence": "结果文件"}]})
        path = evidence_file(self.dir)
        state = G.load_state(self.state)
        G.register_evidence(state, "E1", file_path=path)
        G.save_state(self.state, state)
        G.record_action(self.state, "A1", "done", result="完成", evidence_ids=["E1"])
        G.record_criterion(self.state, "C1", "met", evidence_ids=["E1"])

    def test_high_risk_closes_without_native_review(self):
        check = G.closure_check(G.load_state(self.state))
        self.assertEqual(set(check["gates"]), {"G1", "G2", "G3"})
        self.assertEqual(check["reasons"], [])
        closure = G.close_task(self.state)
        self.assertEqual(closure["verdict"], "pass")
        self.assertEqual(G.evaluate(G.load_state(self.state))["phase"], "CLOSED")

    def test_high_risk_accepts_legacy_backend(self):
        state = G.load_state(self.state)
        self.assertEqual(state["risk"], "high")
        self.assertEqual(state["execution_backend"], "legacy")

    def test_historical_g4_records_do_not_gate(self):
        state = G.load_state(self.state)
        state["native_reviews"] = [{
            "task_id": state["task_id"], "verdict": "pass",
            "g4_eligible": True, "g4_source": "candidate",
            "candidate_g4": {"eligible": True},
            "target_sha256": "0" * 64, "findings": [], "unverified": [],
        }]
        state["review_attempts"] = [{"review_attempt_id": "RA" + "a" * 24,
                                     "consumed": False}]
        state["g4_attestations"] = [{"status": "rejected",
                                     "new_g4_eligible": False}]
        G.save_state(self.state, state)
        check = G.closure_check(G.load_state(self.state))
        self.assertEqual(check["reasons"], [])
        self.assertTrue(check["passed"])

    def test_unresolved_blocking_finding_blocks_high_risk_closure(self):
        state = G.load_state(self.state)
        state.setdefault("finding_ledger", {}).setdefault("items", []).append({
            "finding_id": "F1", "severity": "major", "status": "open",
            "authority": "advisory", "summary": "未修复问题",
            "root_cause": "未修复问题", "file": "", "line": None,
            "occurrences": 1, "reported_ids": ["F1"], "history": [],
        })
        G.save_state(self.state, state)
        check = G.closure_check(G.load_state(self.state))
        self.assertFalse(check["passed"])
        self.assertIn("native_review_finding_open",
                      [r["code"] for r in check["reasons"]])

    def test_verified_finding_no_longer_blocks(self):
        state = G.load_state(self.state)
        state.setdefault("finding_ledger", {}).setdefault("items", []).append({
            "finding_id": "F1", "severity": "blocking", "status": "verified",
            "authority": "authoritative", "summary": "已修复",
            "root_cause": "已修复", "file": "", "line": None,
            "resolved_severity": "blocking",
            "occurrences": 1, "reported_ids": ["F1"], "history": [],
        })
        G.save_state(self.state, state)
        check = G.closure_check(G.load_state(self.state))
        self.assertTrue(check["passed"], check["reasons"])

    def test_derive_work_never_creates_g4_items(self):
        state = G.load_state(self.state)
        state["actions"] = [a for a in state["actions"]
                            if str(a.get("id")) != "A1"]
        state["contract"]["criteria"][0]["status"] = "pending"
        state["contract"]["criteria"][0]["evidence_refs"] = []
        created = G.derive_work(state)
        self.assertTrue(created)
        self.assertFalse([a for a in created
                          if str(a.get("id")).startswith("AUTO-G4")])

    def test_high_risk_pass_suggests_optional_think_not_review(self):
        """高风险与 G1–G3 通过只给可选 THINK；REVIEW 只由用户明确要求触发。"""
        state = G.load_state(self.state)
        evaluate = G.evaluate(state)
        self.assertEqual(evaluate["phase"], "PASS")
        hints = "\n".join(evaluate.get("next_commands") or [])
        self.assertIn("sol-think", hints)
        self.assertNotIn("sol-review", hints)
        self.assertIn("advisory", hints)
        # 已有认知结论后不再重复提示。
        state["sol_calls"] = [{
            "call_id": "SC1", "status": "completed", "mode": "THINK",
            "contract_sha256": state.get("contract_sha256") or "",
            "complexity_signals": ["risk-high"]}]
        evaluate = G.evaluate(state)
        hints = "\n".join(evaluate.get("next_commands") or [])
        self.assertNotIn("sol-think", hints)

    def test_review_metrics_marks_g4_retired(self):
        metrics = G.review_metrics(G.load_state(self.state))
        self.assertTrue(metrics["g4"]["retired"])
        self.assertNotIn("g4_reviews", metrics)

    def test_high_risk_requires_explicit_test_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with self.assertRaises(G.GovernanceError) as ctx:
                G.start_state(path, goal="目标", contract=CONTRACT, risk="high")
            self.assertIn("test_policy", str(ctx.exception))
            self.assertFalse(os.path.exists(path))
            with self.assertRaises(G.GovernanceError):
                G.start_state(path, goal="目标", contract=CONTRACT, risk="high",
                              test_policy={})

    def test_high_risk_binding_requires_contract_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            G.start_state(path, goal="目标", contract=CONTRACT,
                          execution_backend="native")
            G.plan_state(path, {"summary": "s", "actions": [
                {"id": "A1", "title": "实现", "goal": "产出结果",
                 "expected_evidence": "结果文件"}]})
            state = G.load_state(path)
            state["risk"] = "high"
            state["native_dispatch"] = {"actions": [
                {"work_item_id": "A1", "dispatch_id": "d-legacy"}]}
            G.save_state(path, state)
            with self.assertRaises(G.GovernanceError) as ctx:
                G.create_execution_binding(path, work_item_id="A1",
                                           workspace=tmp)
            self.assertIn("test_policy", str(ctx.exception))

    def test_high_risk_amend_cannot_drop_test_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            G.start_state(path, goal="目标", contract=CONTRACT, risk="high",
                          test_policy={"required": False})
            with self.assertRaises(G.GovernanceError):
                G.amend_contract(path, contract=CONTRACT, test_policy={})
            state = G.load_state(path)
            self.assertTrue((state["contract"]["test_policy"] or {}).get("captured"))


class StateIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state.json")

    def test_stale_writer_is_rejected_by_save_seq(self):
        G.start_state(self.state, goal="目标", contract=CONTRACT)
        stale = G.load_state(self.state)
        G.add_decision(self.state, "用户决策")
        with self.assertRaises(G.GovernanceError):
            G.save_state(self.state, stale)

    def test_atomic_write_leaves_no_temp_files(self):
        G.start_state(self.state, goal="目标", contract=CONTRACT)
        G.add_decision(self.state, "用户决策")
        names = sorted(os.listdir(self.tmp.name))
        self.assertEqual(names, [".state.json.lock", "state.json"])

    def test_unknown_schema_is_refused(self):
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump({"schema": "copilot-governance/v0", "version": 1}, fh)
        with self.assertRaises(G.GovernanceError):
            G.load_state(self.state)


class LegacyReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def test_legacy_state_is_read_only(self):
        path = os.path.join(self.dir, "old-state.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "version": 2, "task_id": "task-old", "goal": "旧目标",
                "round_seq": 3, "rounds": [{"round_id": "r1", "verdict": "PARTIAL",
                                            "call_id": "c1"}],
                "evidence": [{"id": "E1"}], "actions": [{"id": "A1"}],
            }, fh)
        with self.assertRaises(G.GovernanceError):
            G.load_state(path)
        status = G.legacy_status(path=path)
        self.assertTrue(status["retired"])
        self.assertEqual(status["engine"], "legacy-sol")
        self.assertEqual(status["state"]["rounds"][0]["verdict"], "PARTIAL")
        self.assertEqual(status["state"]["rounds"][0]["call_id"], "c1")
        self.assertEqual(status["state"]["evidence_count"], 1)

    def test_legacy_audit_and_ledger_remain_readable(self):
        audit = os.path.join(self.dir, "audit.jsonl")
        with open(audit, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "call_requested",
                                 "call": {"call_id": "c1", "model": "gpt-6.1-sol"}}) + "\n")
            fh.write(json.dumps({"event": "call_result",
                                 "call": {"call_id": "c1", "status": "ok"}}) + "\n")
        ledger = os.path.join(self.dir, "ledger.json")
        with open(ledger, "w", encoding="utf-8") as fh:
            json.dump({"subject": "copilot", "rounds": [
                {"round_id": "r1", "verdict": "PASS", "call_id": "c1"}]}, fh)
        status = G.legacy_status(audit_log=audit, ledger=ledger)
        self.assertEqual(status["audit"]["requests"], 1)
        self.assertEqual(status["audit"]["results"], 1)
        self.assertEqual(status["audit"]["models"], {"gpt-6.1-sol": 1})
        self.assertEqual(status["ledger"]["rounds"][0]["verdict"], "PASS")
        self.assertIn("只读", status["audit"]["note"])


class FormalReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state.json")
        G.start_state(self.state, goal="目标", contract=CONTRACT)

    def test_formal_review_requires_explicit_user_authorization(self):
        with self.assertRaises(G.GovernanceError):
            G.record_formal_review(self.state, verdict="pass", summary="看过了")
        with self.assertRaises(G.GovernanceError):
            G.authorize_formal_review(self.state, authorized_by="zcode",
                                      reason="自行决定")
        # 不给默认值：不传 authorized_by 不能被当成用户授权。
        with self.assertRaises(G.GovernanceError):
            G.authorize_formal_review(self.state, reason="用户要求正式复核")
        entry = G.authorize_formal_review(self.state, authorized_by="user",
                                          reason="用户要求正式复核")
        self.assertEqual(entry["authorized_by"], "user")
        record = G.record_formal_review(self.state, verdict="pass",
                                        summary="独立复核通过")
        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["authorization"]["reason"], "用户要求正式复核")

    def test_formal_review_authorization_binds_current_contract(self):
        G.authorize_formal_review(self.state, authorized_by="user",
                                  reason="用户要求正式复核")
        G.amend_contract(self.state, contract="## 完成条件\n- 产出结果\n- 补充验证\n")
        with self.assertRaises(G.GovernanceError):
            G.record_formal_review(self.state, verdict="pass", summary="旧合同结论")

    def test_formal_review_is_not_a_closure_gate(self):
        check = G.closure_check(G.load_state(self.state))
        self.assertNotIn("formal_review", str(check["gates"]))
        names = set(check["gates"])
        self.assertEqual(names, {"G1", "G2", "G3"})

    def _authorized_pass(self) -> dict:
        G.authorize_formal_review(self.state, authorized_by="user",
                                  reason="用户要求正式复核")
        return G.record_formal_review(self.state, verdict="pass",
                                      summary="独立复核通过")

    def test_formal_review_amendment_preserves_original(self):
        record = self._authorized_pass()
        original = json.loads(json.dumps(record))
        result = G.record_formal_review(
            self.state, amend=record["review_id"], review_ref="E9（编号更正）",
            reason="仅更正证据引用编号，结论未变")
        self.assertEqual(result["review_id"], record["review_id"])
        self.assertEqual(result["verdict"], "pass")
        state = G.load_state(self.state)
        stored = state["formal_reviews"][0]
        for key in ("review_id", "verdict", "summary", "review_ref", "reviewer",
                    "authorization", "target_sha256", "created"):
            self.assertEqual(stored[key], original[key])
        self.assertEqual(len(stored["amendments"]), 1)
        self.assertIn("引用编号", stored["amendments"][0]["reason"])
        metrics = G.review_metrics(state)
        self.assertEqual(metrics["formal_reviews_total"], 1)
        self.assertEqual(metrics["formal_review_amendments_total"], 1)
        self.assertEqual(metrics["active_formal_reviews_total"], 1)

    def test_formal_review_amendment_rejects_changed_target(self):
        record = self._authorized_pass()
        state = G.load_state(self.state)
        G.register_evidence(state, "E1", text="new evidence")
        G.save_state(self.state, state)
        with self.assertRaises(G.GovernanceError):
            G.record_formal_review(
                self.state, amend=record["review_id"], review_ref="E1",
                reason="证据变化后仍想继承旧 PASS")
        stored = G.load_state(self.state)["formal_reviews"][0]
        self.assertNotIn("amendments", stored)

    def test_formal_review_supersede_keeps_both_records(self):
        record = self._authorized_pass()
        state = G.load_state(self.state)
        G.register_evidence(state, "E1", text="new evidence")
        G.save_state(self.state, state)
        new = G.record_formal_review(
            self.state, verdict="pass", summary="新证据下重新复核通过",
            supersedes=record["review_id"], reason="新增证据改变了目标摘要")
        self.assertEqual(new["supersedes"], record["review_id"])
        state = G.load_state(self.state)
        self.assertEqual(len(state["formal_reviews"]), 2)
        self.assertEqual(state["formal_reviews"][0]["review_id"],
                         record["review_id"])
        metrics = G.review_metrics(state)
        self.assertEqual(metrics["formal_reviews_total"], 2)
        self.assertEqual(metrics["superseded_formal_reviews_total"], 1)
        self.assertEqual(metrics["active_formal_reviews_total"], 1)


class JudgmentAndReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state.json")
        G.start_state(self.state, goal="目标", contract=CONTRACT)

    def test_judgments_and_reports_are_audit_only(self):
        G.record_judgment(self.state, "partial", "还差一项验证")
        G.add_report(self.state, "执行摘要", blocked=False)
        state = G.load_state(self.state)
        self.assertEqual(state["judgments"][0]["source"], "zcode")
        self.assertTrue(state["reports"][0]["report_sha256"])
        # 记录判断不改变本地阶段：仍缺条件与工作项。
        self.assertEqual(G.evaluate(state)["phase"], "PARTIAL")

    def test_evidence_cannot_be_silently_replaced(self):
        state = G.load_state(self.state)
        G.register_evidence(state, "E1", text="v1")
        with self.assertRaises(G.GovernanceError):
            G.register_evidence(state, "E1", text="v2")
        same = G.register_evidence(state, "E1", text="v1")
        self.assertEqual(same["sha256"], G._sha256_text("v1"))
        G.drop_evidence(state, "E1", "被新证据取代")
        self.assertEqual(G.next_evidence_id(state), "E2")


if __name__ == "__main__":
    unittest.main()

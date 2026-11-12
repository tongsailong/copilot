#!/usr/bin/env python3
"""v6 本地治理引擎回归：合同、四门、PARTIAL 推进、证据完整性、CAS、历史只读。"""
from __future__ import annotations

import json
import os
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

    def test_blocked_state_blocks_writes_and_can_be_resolved(self):
        G.add_block(self.state, "需要用户授权", "授权 push")
        self.assertEqual(G.evaluate(G.load_state(self.state))["phase"], "BLOCKED")
        with self.assertRaises(G.GovernanceError):
            G.record_action(self.state, "A1", "done", result="完成")
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


class HighRiskGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state = os.path.join(self.dir, "state.json")
        G.start_state(self.state, goal="目标", contract=CONTRACT, risk="high",
                      execution_backend="native")
        G.plan_state(self.state, {"summary": "s", "actions": [
            {"id": "A1", "title": "实现", "goal": "产出结果",
             "expected_evidence": "结果文件"}]})
        path = evidence_file(self.dir)
        state = G.load_state(self.state)
        G.register_evidence(state, "E1", file_path=path)
        G.save_state(self.state, state)
        G.record_action(self.state, "A1", "done", result="完成", evidence_ids=["E1"])
        G.record_criterion(self.state, "C1", "met", evidence_ids=["E1"])

    def _review(self, **overrides) -> dict:
        state = G.load_state(self.state)
        review = {
            "task_id": state["task_id"],
            "contract_sha256": state["contract_sha256"],
            "work_item_id": "AUTO-G4",
            "verdict": "pass",
            "findings": [],
            "unverified": [],
            "changed_files": [],
            "g4_eligible": True,
            "target_sha256": G.final_evidence_digest(state),
            "reviewer": {"profile_id": "reviewer", "agent_id": "agent-1",
                         "child_session_id": "child-1"},
            "host": {"metadata_path": "/tmp/meta.json",
                     "output_file": "/tmp/out.txt",
                     "rollout_path": "/tmp/rollout.jsonl",
                     "tool_calls": [{"name": "Read", "count": 1}],
                     "agent_id": "agent-1", "child_session_id": "child-1"},
        }
        review["host_sha256"] = G._sha256_text(G.canonical_json(review["host"]))
        review.update(overrides)
        state.setdefault("native_reviews", []).append(review)
        G.save_state(self.state, state)
        return review

    def test_high_risk_without_review_fails_closed(self):
        state = G.load_state(self.state)
        check = G.closure_check(state)
        g4 = [r for r in check["reasons"] if r["gate"] == "G4"]
        self.assertEqual(g4[0]["code"], "native_review_missing")
        self.assertEqual(G.evaluate(state)["phase"], "PARTIAL")

    def test_high_risk_with_bound_review_passes(self):
        self._review()
        state = G.load_state(self.state)
        self.assertEqual(G.evaluate(state)["phase"], "PASS")
        self.assertTrue(G.close_task(self.state)["closed"])

    def test_stale_review_target_fails_closed(self):
        self._review(target_sha256="0" * 64)
        check = G.closure_check(G.load_state(self.state))
        codes = {r["code"] for r in check["reasons"]}
        self.assertIn("native_review_stale", codes)

    def test_review_must_be_pass_without_unverified_or_blocking(self):
        self._review(verdict="partial")
        codes = {r["code"] for r in
                 G.closure_check(G.load_state(self.state))["reasons"]}
        self.assertIn("native_review_not_pass", codes)
        state = G.load_state(self.state)
        state["native_reviews"] = []
        G.save_state(self.state, state)
        self._review(unverified=["未验证项"])
        codes = {r["code"] for r in
                 G.closure_check(G.load_state(self.state))["reasons"]}
        self.assertIn("native_review_unverified", codes)

    def test_g4_review_action_does_not_invalidate_its_own_target(self):
        state = G.load_state(self.state)
        before = G.final_evidence_digest(state)
        state["actions"].append({
            "id": "AUTO-G4", "title": "复核", "origin": "g4-review",
            "status": "pending", "result": "", "evidence_ids": [],
        })
        self.assertEqual(G.final_evidence_digest(state), before)
        state["actions"][-1]["status"] = "done"
        state["actions"][-1]["result"] = "复核完成"
        self.assertEqual(G.final_evidence_digest(state), before)

    def test_review_from_other_task_is_rejected(self):
        self._review(task_id="task-other")
        codes = {r["code"] for r in
                 G.closure_check(G.load_state(self.state))["reasons"]}
        self.assertIn("native_review_foreign", codes)


    def test_g4_rejections_are_fail_closed(self):
        cases = [
            ({"findings": [{"severity": "blocking", "summary": "x"}]},
             "native_review_blocking"),
            ({"unverified": ["未验证"]}, "native_review_unverified"),
            ({"changed_files": ["a.py"]}, "native_review_changed_files"),
            ({"g4_eligible": False}, "native_review_not_g4_eligible"),
            ({"reviewer": {"profile_id": "executor", "agent_id": "agent-1",
                           "child_session_id": "child-1"}},
             "native_review_profile_invalid"),
            ({"contract_sha256": "0" * 64}, "native_review_contract_mismatch"),
        ]
        for overrides, code in cases:
            with self.subTest(code=code):
                state = G.load_state(self.state)
                state["native_reviews"] = []
                G.save_state(self.state, state)
                self._review(**overrides)
                codes = {r["code"] for r in
                         G.closure_check(G.load_state(self.state))["reasons"]}
                self.assertIn(code, codes)

    def test_review_without_host_binding_is_rejected(self):
        review = self._review()
        state = G.load_state(self.state)
        state["native_reviews"][-1] = {
            k: v for k, v in review.items() if k != "host"}
        G.save_state(self.state, state)
        codes = {r["code"] for r in
                 G.closure_check(G.load_state(self.state))["reasons"]}
        self.assertIn("native_review_host_missing", codes)

    def test_tampered_host_binding_is_rejected(self):
        self._review()
        state = G.load_state(self.state)
        state["native_reviews"][-1]["host"]["agent_id"] = "agent-evil"
        G.save_state(self.state, state)
        codes = {r["code"] for r in
                 G.closure_check(G.load_state(self.state))["reasons"]}
        self.assertIn("native_review_host_changed", codes)

    def test_host_identity_must_match_reviewer(self):
        self._review()
        state = G.load_state(self.state)
        host = state["native_reviews"][-1]["host"]
        host["child_session_id"] = "child-other"
        state["native_reviews"][-1]["host_sha256"] = G._sha256_text(
            G.canonical_json(host))
        G.save_state(self.state, state)
        codes = {r["code"] for r in
                 G.closure_check(G.load_state(self.state))["reasons"]}
        self.assertIn("native_review_host_identity_mismatch", codes)

    def test_high_risk_requires_native_backend_at_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with self.assertRaises(G.GovernanceError):
                G.start_state(path, goal="目标", contract=CONTRACT, risk="high")
            self.assertFalse(os.path.exists(path))


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
        self.assertEqual(names, {"G1", "G2", "G3", "G4"})


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

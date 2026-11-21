#!/usr/bin/env python3
"""v6.8.0 复核效率专项：准入、停止、Finding 台账与定向验证。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import v6_support as F

import governance as G
import native_bridge as NB
import review_policy as RP
import copilot_auto as AC


def review(findings, *, sha="a" * 64, verdict="pass", scope="full",
           target="t" * 64, based_on="", manifest=None, work_item="A1",
           attempt=""):
    return {
        "task_id": "task-1",
        "contract_sha256": "c" * 64,
        "work_item_id": work_item,
        "verdict": verdict,
        "review_scope": scope,
        "based_on_review_sha256": based_on,
        "evidence_manifest": list(manifest or []),
        "target_sha256": target,
        "review_attempt_id": attempt,
        "result_sha256": sha,
        "findings": findings,
        "unverified": [],
        "changed_files": [],
        "g4_eligible": True,
        "received": G.now_iso(),
    }


class AdmissionTests(unittest.TestCase):
    def test_plain_normal_task_is_optional_not_forced(self):
        state = {"risk": "normal", "sol_reviews": []}
        decision = G.review_admission(state)
        self.assertEqual(decision["decision"], "optional")
        self.assertEqual(decision["scope"], "normal")
        self.assertEqual(decision["tier"], "medium")
        self.assertTrue(decision["single_round"])
        self.assertNotIn("g4_separate", decision)

    def test_low_risk_task_does_not_need_sol(self):
        decision = G.review_admission({"risk": "low", "sol_reviews": []})
        self.assertEqual(decision["decision"], "not_needed")
        self.assertEqual(decision["scope"], "none")
        self.assertEqual(decision["tier"], "low")
        self.assertIn("低风险", decision["detail"])

    def test_medium_risk_task_is_optional(self):
        decision = G.review_admission({"risk": "medium", "sol_reviews": []})
        self.assertEqual(decision["decision"], "optional")
        self.assertEqual(decision["tier"], "medium")

    def test_low_risk_explicit_trigger_recommends_once(self):
        decision = G.review_admission(
            {"risk": "low", "sol_reviews": []},
            triggers=["security"], reason="意外涉及凭据读取")
        self.assertEqual(decision["decision"], "recommended")
        self.assertEqual(decision["scope"], "triggered")

    def test_unknown_risk_fails_closed(self):
        import sol_review as SR
        with self.assertRaises(SR.SolUsageError):
            G.review_admission({"risk": "whatever", "sol_reviews": []})
        with self.assertRaises(SR.SolUsageError):
            G.review_admission({"risk": "", "sol_reviews": []})

    def test_high_risk_recommends_sol_advisory(self):
        decision = G.review_admission({"risk": "high", "sol_reviews": []})
        self.assertEqual(decision["decision"], "recommended")
        self.assertEqual(decision["scope"], "high-risk")
        self.assertNotIn("g4_separate", decision)
        self.assertIn("不构成收口门禁", decision["detail"])

    def test_declared_trigger_recommends_sol(self):
        decision = G.review_admission(
            {"risk": "normal", "sol_reviews": []},
            triggers=["security"], reason="凭据处理")
        self.assertEqual(decision["decision"], "recommended")
        self.assertEqual(decision["scope"], "triggered")
        self.assertEqual(decision["triggers"], ["security"])

    def test_unknown_trigger_is_rejected(self):
        import sol_review as SR
        with self.assertRaises(SR.SolUsageError):
            G.review_admission({"risk": "normal", "sol_reviews": []},
                               triggers=["looks-complex"])

    def test_baseline_sol_review_is_visible(self):
        state = {"risk": "medium", "sol_reviews": [{
            "status": "completed", "review_id": "SR1", "verdict": "partial",
            "materials_sha256": "a" * 64, "created_at": "now"}]}
        decision = G.review_admission(state)
        self.assertEqual(decision["sol_reviews_total"], 1)
        self.assertEqual(decision["last_sol_review"]["verdict"], "partial")


class FindingLedgerTests(unittest.TestCase):
    def test_same_root_cause_keeps_stable_id_across_paraphrases(self):
        state = {}
        first = RP.merge_review(state, review([{
            "id": "F1", "severity": "major", "file": "src/a.py", "line": 9,
            "summary": "请求在超时后仍写入缓存", "root_cause": "timeout-write",
            "evidence": "src/a.py:9"}], sha="1" * 64))
        second = RP.merge_review(state, review([{
            "id": "F7", "severity": "major", "file": "src/a.py", "line": 12,
            "summary": "超时路径没有取消缓存写入（换一种说法）",
            "root_cause": "Timeout Write", "evidence": "src/a.py:12"}],
            sha="2" * 64))
        self.assertEqual(first["created"], ["F1"])
        self.assertEqual(second["created"], [])
        self.assertEqual(len(state["finding_ledger"]["items"]), 1)
        item = state["finding_ledger"]["items"][0]
        self.assertEqual(item["finding_id"], "F1")
        self.assertEqual(item["occurrences"], 2)
        self.assertIn("F7", item["reported_ids"])

    def test_different_root_cause_gets_new_id(self):
        state = {}
        RP.merge_review(state, review([{
            "severity": "major", "file": "src/a.py", "line": 9,
            "summary": "超时后写入缓存", "root_cause": "timeout-write",
            "evidence": "e"}], sha="1" * 64))
        merged = RP.merge_review(state, review([{
            "severity": "major", "file": "src/a.py", "line": 30,
            "summary": "空指针导致崩溃", "root_cause": "null-deref",
            "evidence": "e"}], sha="2" * 64))
        self.assertEqual(merged["created"], ["F2"])

    def test_verified_finding_is_not_reopened_without_new_evidence(self):
        state = {}
        RP.merge_review(state, review([{
            "severity": "major", "file": "src/a.py", "line": 1,
            "summary": "问题", "root_cause": "rc", "evidence": "old"}],
            sha="1" * 64))
        RP.merge_review(state, review([{
            "severity": "major", "file": "src/a.py", "line": 1,
            "summary": "问题", "root_cause": "rc", "evidence": "old",
            "status": "resolved"}], sha="2" * 64))
        self.assertEqual(RP.get_item(state, "F1")["status"], "verified")
        merged = RP.merge_review(state, review([{
            "severity": "major", "file": "src/a.py", "line": 1,
            "summary": "问题", "root_cause": "rc"}], sha="3" * 64))
        self.assertEqual(RP.get_item(state, "F1")["status"], "verified")
        self.assertEqual(merged["rereported"], ["F1"])

    def test_verified_finding_regresses_with_new_evidence(self):
        state = {}
        RP.merge_review(state, review([{
            "severity": "blocking", "file": "src/a.py", "line": 1,
            "summary": "问题", "root_cause": "rc", "evidence": "old",
            "status": "resolved"}], sha="1" * 64))
        merged = RP.merge_review(state, review([{
            "severity": "blocking", "file": "src/a.py", "line": 1,
            "summary": "问题", "root_cause": "rc", "evidence": "new",
            "status": "regressed"}], sha="2" * 64))
        self.assertEqual(RP.get_item(state, "F1")["status"], "regressed")
        self.assertEqual(merged["regressed"], ["F1"])

    def test_merge_is_idempotent_for_same_review(self):
        state = {}
        entry = review([{
            "severity": "minor", "file": "a", "summary": "小问题"}],
            sha="1" * 64)
        RP.merge_review(state, entry)
        RP.merge_review(state, entry)
        self.assertEqual(state["finding_ledger"]["items"][0]["occurrences"], 1)

    def test_lifecycle_requires_evidence_and_order(self):
        state = {}
        RP.merge_review(state, review([{
            "severity": "major", "file": "a", "summary": "问题",
            "root_cause": "rc", "evidence": "e"}], sha="1" * 64))
        with self.assertRaises(RP.ReviewPolicyError):
            RP.transition(state, "F1", "fixed", evidence_ids=[])
        RP.transition(state, "F1", "fixed", evidence_ids=["E1"])
        with self.assertRaises(RP.ReviewPolicyError):
            RP.transition(state, "F1", "closed", evidence_ids=["E1"])
        with self.assertRaises(RP.ReviewPolicyError):
            RP.transition(state, "F1", "verified", evidence_ids=[])
        RP.transition(state, "F1", "verified", evidence_ids=["E2"])
        RP.transition(state, "F1", "closed", evidence_ids=["E2"])
        self.assertEqual(RP.get_item(state, "F1")["status"], "closed")
        self.assertEqual(RP.blocking_findings(state), [])

    def test_resolved_minor_escalated_to_major_blocks(self):
        state = {}
        RP.merge_review(state, review([{
            "severity": "minor", "file": "a", "summary": "小问题",
            "root_cause": "rc", "evidence": "same"}], sha="1" * 64))
        RP.transition(state, "F1", "dropped", note="非缺陷")
        self.assertEqual(RP.blocking_findings(state), [])
        RP.merge_review(state, review([{
            "severity": "major", "file": "a", "summary": "小问题",
            "root_cause": "rc", "evidence": "same"}], sha="2" * 64))
        self.assertEqual(RP.get_item(state, "F1")["status"], "regressed")
        self.assertEqual([item["finding_id"]
                          for item in RP.blocking_findings(state)], ["F1"])

    def test_duplicate_merge_does_not_rollback_fixed_status(self):
        state = {}
        entry = review([{
            "severity": "major", "file": "a", "summary": "问题",
            "root_cause": "rc", "evidence": "e"}], sha="2" * 64)
        RP.merge_review(state, entry)
        RP.transition(state, "F1", "fixed", evidence_ids=["E1"])
        RP.merge_review(state, entry)
        item = RP.get_item(state, "F1")
        self.assertEqual(item["status"], "fixed")
        self.assertEqual(item["occurrences"], 1)

    def test_repeated_reference_escalation_reuses_entry(self):
        state = {}
        RP.merge_review(state, review([{
            "severity": "minor", "file": "a", "summary": "小问题",
            "root_cause": "rc"}], sha="1" * 64))
        RP.transition(state, "F1", "fixed", evidence_ids=["E1"])
        RP.transition(state, "F1", "verified", evidence_ids=["E2"])
        for sha in ("2" * 64, "3" * 64):
            RP.merge_review(state, review([{
                "severity": "major", "file": "a", "summary": "小问题",
                "root_cause": "rc"}], sha=sha), authoritative=False)
        escalations = [item for item in RP.ledger_summary(state)["items"]
                       if item.get("escalation_of") == "F1"]
        self.assertEqual(len(escalations), 1)
        self.assertEqual(escalations[0]["occurrences"], 2)
        self.assertEqual(RP.get_item(state, "F1")["status"], "verified")

    def test_reference_review_escalation_still_blocks(self):
        state = {}
        RP.merge_review(state, review([{
            "severity": "minor", "file": "a", "summary": "小问题",
            "root_cause": "rc", "evidence": "same"}], sha="1" * 64))
        RP.transition(state, "F1", "dropped", note="非缺陷")
        RP.merge_review(state, review([{
            "severity": "major", "file": "a", "summary": "小问题",
            "root_cause": "rc", "evidence": "same"}], sha="2" * 64),
            authoritative=False)
        # advisory 不能把已解决的 authoritative 记录单方面提级为不可驳回阻断：
        # 原记录保持 dropped，提级单独登记为可驳回的 advisory 条目并阻断收口。
        self.assertEqual(RP.get_item(state, "F1")["status"], "dropped")
        escalation = RP.get_item(state, "F2")
        self.assertIsNotNone(escalation)
        self.assertEqual(escalation["authority"], "advisory")
        self.assertEqual(escalation["severity"], "major")
        self.assertEqual(escalation["escalation_of"], "F1")
        self.assertEqual([item["finding_id"]
                          for item in RP.blocking_findings(state)], ["F2"])
        RP.transition(state, "F2", "dropped", note="误报：原记录已验证")
        self.assertEqual(RP.blocking_findings(state), [])
        self.assertEqual(RP.get_item(state, "F1")["status"], "dropped")

    def test_reference_review_cannot_verify_existing_finding(self):
        state = {}
        RP.merge_review(state, review([{
            "severity": "major", "file": "a", "summary": "问题",
            "root_cause": "rc", "evidence": "e"}], sha="1" * 64))
        RP.merge_review(state, review([{
            "severity": "major", "file": "a", "summary": "问题",
            "root_cause": "rc", "evidence": "新说法",
            "status": "resolved"}], sha="2" * 64), authoritative=False)
        item = RP.get_item(state, "F1")
        self.assertEqual(item["status"], "open")
        self.assertEqual([x["finding_id"] for x in RP.blocking_findings(state)],
                         ["F1"])

    def test_blocking_major_cannot_be_dropped_by_executor(self):
        state = {}
        RP.merge_review(state, review([{
            "severity": "major", "file": "a", "summary": "问题",
            "root_cause": "rc", "evidence": "e"}], sha="1" * 64))
        with self.assertRaises(RP.ReviewPolicyError):
            RP.transition(state, "F1", "dropped", note="误报")
        self.assertEqual(RP.get_item(state, "F1")["status"], "open")

    def test_verified_requires_fixed_first(self):
        state = {}
        RP.merge_review(state, review([{
            "severity": "minor", "file": "a", "summary": "小问题",
            "root_cause": "rc", "evidence": "e"}], sha="1" * 64))
        with self.assertRaises(RP.ReviewPolicyError):
            RP.transition(state, "F1", "verified", evidence_ids=["E1"])

    def test_minor_and_info_do_not_block(self):
        state = {}
        RP.merge_review(state, review([
            {"severity": "minor", "file": "a", "summary": "小"},
            {"severity": "info", "file": "b", "summary": "观察"}],
            sha="1" * 64))
        self.assertEqual(RP.blocking_findings(state), [])

    def test_fixed_but_unverified_still_blocks(self):
        state = {}
        RP.merge_review(state, review([{
            "severity": "major", "file": "a", "summary": "问题",
            "root_cause": "rc", "evidence": "e"}], sha="1" * 64))
        RP.transition(state, "F1", "fixed", evidence_ids=["E1"])
        blocked = RP.blocking_findings(state)
        self.assertEqual([item["finding_id"] for item in blocked], ["F1"])
        self.assertEqual([item["finding_id"]
                          for item in RP.pending_verification(state)], ["F1"])


class DeriveWorkEfficiencyTests(unittest.TestCase):
    def _state(self):
        path = tempfile.mkdtemp(prefix="eff-")
        state_path = os.path.join(path, "state.json")
        G.start_state(state_path, goal="目标", contract=F.CONTRACT,
                      execution_backend="native", risk="high",
                      test_policy={"required": False})
        return state_path, G.load_state(state_path)

    def test_open_finding_derives_fix_without_new_review(self):
        _, state = self._state()
        RP.merge_review(state, review([{
            "severity": "major", "file": "src/a.py", "line": 2,
            "summary": "实质缺陷", "root_cause": "rc", "evidence": "e"}],
            sha="1" * 64))
        actions = G.derive_work(state)
        ids = [item["id"] for item in actions]
        self.assertIn("AUTO-FIX-F1", ids)
        self.assertFalse(any(item.startswith("AUTO-G4") for item in ids))

    def test_fixed_finding_derives_targeted_verification(self):
        _, state = self._state()
        RP.merge_review(state, review([{
            "severity": "major", "file": "src/a.py", "line": 2,
            "summary": "实质缺陷", "root_cause": "rc", "evidence": "e"}],
            sha="1" * 64))
        RP.transition(state, "F1", "fixed", evidence_ids=["E1"])
        actions = G.derive_work(state)
        ids = [item["id"] for item in actions]
        self.assertIn("AUTO-VERIFY-F1", ids)
        self.assertFalse(any(item.startswith("AUTO-G4") for item in ids))

    def test_active_family_is_not_duplicated(self):
        _, state = self._state()
        state["actions"].append({
            "id": "AUTO-C1", "title": "t", "status": "pending",
            "origin": "partial-derived", "result": "", "evidence_ids": [],
        })
        actions = G.derive_work(state)
        self.assertFalse(any(item["id"].startswith("AUTO-C1")
                             for item in actions))

    def test_verified_finding_no_longer_derives_work(self):
        _, state = self._state()
        RP.merge_review(state, review([{
            "severity": "major", "file": "src/a.py", "line": 2,
            "summary": "实质缺陷", "root_cause": "rc", "evidence": "e"}],
            sha="1" * 64))
        RP.transition(state, "F1", "fixed", evidence_ids=["E1"])
        RP.transition(state, "F1", "verified", evidence_ids=["E2"])
        actions = G.derive_work(state)
        self.assertFalse(any("F1" in item["id"] for item in actions))




class IngestLedgerTests(unittest.TestCase):
    def _state(self):
        tmp = tempfile.mkdtemp(prefix="sol-ledger-")
        path = os.path.join(tmp, "state.json")
        G.start_state(path, goal="目标", contract=F.CONTRACT)
        return path

    def _record(self, findings, sha="s" * 64):
        return {
            "review_id": "SR" + sha[:10], "task_id": "task-1",
            "verdict": "pass", "findings": findings,
            "findings_count": {"blocking": 0, "major": len(findings),
                               "minor": 0, "info": 0},
            "unverified": [], "reply_sha256": sha,
            "completed_at": G.now_iso(),
        }

    def test_sol_ingest_populates_ledger_as_advisory(self):
        path = self._state()
        entry = self._record([{
            "id": "F1", "severity": "major", "file": "src/a.py", "line": 1,
            "summary": "问题", "root_cause": "rc", "evidence": "证据"}])
        G.record_sol_review(path, entry, materials_sha256="m" * 64)
        state = G.load_state(path)
        self.assertEqual(len(state["finding_ledger"]["items"]), 1)
        item = state["finding_ledger"]["items"][0]
        self.assertEqual(item["authority"], "advisory")
        self.assertEqual(item["finding_id"], "F1")
        # SOL 是 advisory：不产生停止标记。
        self.assertEqual(state.get("review_stops") or [], [])

    def test_duplicate_sol_record_does_not_double_count(self):
        path = self._state()
        entry = self._record([{
            "severity": "major", "file": "src/a.py", "line": 1,
            "summary": "问题", "root_cause": "rc", "evidence": "证据"}])
        G.record_sol_review(path, entry, materials_sha256="m" * 64)
        G.record_sol_review(path, entry, materials_sha256="m" * 64)
        state = G.load_state(path)
        self.assertEqual(state["finding_ledger"]["items"][0]["occurrences"], 1)

    def test_second_sol_review_reuses_finding_id(self):
        path = self._state()
        first = self._record([{
            "severity": "major", "file": "src/a.py", "line": 1,
            "summary": "问题 A", "root_cause": "rc", "evidence": "证据"}],
            sha="1" * 64)
        second = self._record([{
            "id": "F9", "severity": "major", "file": "src/a.py", "line": 1,
            "summary": "问题 A（换表述）", "root_cause": "rc",
            "evidence": "新证据"}], sha="2" * 64)
        G.record_sol_review(path, first, materials_sha256="m" * 64)
        G.record_sol_review(path, second, materials_sha256="n" * 64)
        state = G.load_state(path)
        self.assertEqual(len(state["finding_ledger"]["items"]), 1)
        self.assertEqual(state["finding_ledger"]["items"][0]["finding_id"], "F1")


class CliEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state.json")
        G.start_state(self.state, goal="目标", contract=F.CONTRACT)

    def test_admission_cli_reports_not_required_for_plain_task(self):
        proc = F.run_cli("copilot_dispatch.py", "review-admission",
                         "--state", self.state, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["decision"], "optional")
        self.assertEqual(payload["tier"], "medium")

    def test_admission_cli_reports_low_tier_without_review(self):
        path = os.path.join(self.tmp.name, "low.json")
        G.start_state(path, goal="改文案", contract=F.CONTRACT, risk="low")
        proc = F.run_cli("copilot_dispatch.py", "review-admission",
                         "--state", path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["tier"], "low")
        self.assertEqual(payload["decision"], "not_needed")

    def test_legacy_normal_risk_is_stored_as_medium(self):
        path = os.path.join(self.tmp.name, "normal.json")
        G.start_state(path, goal="目标", contract=F.CONTRACT, risk="normal")
        self.assertEqual(G.load_state(path)["risk"], "medium")

    def test_start_accepts_low_and_medium_risks(self):
        for risk in ("low", "medium"):
            path = os.path.join(self.tmp.name, f"{risk}.json")
            G.start_state(path, goal="目标", contract=F.CONTRACT, risk=risk)
            self.assertEqual(G.load_state(path)["risk"], risk)

    def test_admission_cli_recommends_sol_for_security_trigger(self):
        proc = F.run_cli("copilot_dispatch.py", "review-admission",
                         "--state", self.state, "--trigger", "security",
                         "--reason", "凭据处理", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["decision"], "recommended")
        self.assertEqual(payload["scope"], "triggered")
        self.assertNotIn("g4_separate", payload)

    def test_admission_cli_rejects_retired_workspace_flag(self):
        proc = F.run_cli("copilot_dispatch.py", "review-admission",
                         "--state", self.state, "--workspace", self.tmp.name)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unrecognized arguments", proc.stderr)

    def test_finding_file_evidence_is_rechecked(self):
        repo = tempfile.mkdtemp(prefix="finding-ev-")
        self.addCleanup(__import__("shutil").rmtree, repo, True)
        evidence = os.path.join(repo, "fix.log")
        with open(evidence, "w", encoding="utf-8") as fh:
            fh.write("pass\n")
        state = G.load_state(self.state)
        G.register_evidence(state, "E1", file_path=evidence)
        state["finding_ledger"] = {
            "schema": RP.LEDGER_SCHEMA, "next_id": 2,
            "items": [{
                "finding_id": "F1", "severity": "major", "status": "fixed",
                "file": "src/a.py", "summary": "问题", "occurrences": 1,
            }],
        }
        G.save_state(self.state, state)
        with open(evidence, "w", encoding="utf-8") as fh:
            fh.write("tampered\n")
        proc = F.run_cli("copilot_dispatch.py", "finding", "--state",
                         self.state, "--id", "F1", "--status", "verified",
                         "--evidence", "E1")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("摘要", proc.stderr)

    def test_finding_cli_lifecycle(self):
        state = G.load_state(self.state)
        state["finding_ledger"] = {
            "schema": RP.LEDGER_SCHEMA, "next_id": 2,
            "items": [{
                "finding_id": "F1", "severity": "major", "status": "open",
                "file": "src/a.py", "summary": "问题", "occurrences": 1,
            }],
        }
        G.save_state(self.state, state)
        proc = F.run_cli("copilot_dispatch.py", "evidence", "--state", self.state,
                         "--add", "--id", "E1", "--text", "修复证据")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # finding 回填后任务仍要求续跑：用 EXIT_CONTINUE 提示继续，不冒充终点。
        proc = F.run_cli("copilot_dispatch.py", "finding", "--state", self.state,
                         "--id", "F1", "--status", "fixed", "--evidence", "E1")
        self.assertEqual(proc.returncode, 10, proc.stderr)
        proc = F.run_cli("copilot_dispatch.py", "finding", "--state", self.state,
                         "--id", "F1", "--status", "verified")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("需要定向验证证据", proc.stderr)
        proc = F.run_cli("copilot_dispatch.py", "finding", "--state", self.state,
                         "--id", "F1", "--status", "verified",
                         "--evidence", "E1")
        self.assertEqual(proc.returncode, 10, proc.stderr)
        proc = F.run_cli("copilot_dispatch.py", "finding", "--state", self.state,
                         "--list", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["items"][0]["status"], "verified")


class NativeReviewRetiredTests(unittest.TestCase):
    def test_native_review_envelope_is_retired(self):
        envelope = {"schema": NB.REVIEW_SCHEMA, "verdict": "pass"}
        with self.assertRaises(NB.BridgeError) as ctx:
            NB.classify_native_result(envelope)
        self.assertEqual(ctx.exception.code, "native_review_retired")

    def test_legacy_review_envelope_is_retired(self):
        envelope = {"schema": "copilot-native-review/v1"}
        with self.assertRaises(NB.BridgeError) as ctx:
            NB.classify_native_result(envelope)
        self.assertEqual(ctx.exception.code, "schema_retired")


class MetricsEfficiencyTests(unittest.TestCase):
    def test_metrics_include_ledger_admissions_and_stop_counts(self):
        state = {
            "task_id": "task-1", "risk": "high", "created": G.now_iso(),
            "closure": {},
            "native_reviews": [{"work_item_id": "A1", "g4_eligible": True}],
            "sol_reviews": [{"status": "completed", "verdict": "partial",
                             "findings_count": {"major": 1}}],
            "review_admissions": [{"triggers": ["major-fix"], "tier": "high"}],
            "review_stops": [{"stop_id": "RST1"}],
            "finding_ledger": {
                "schema": RP.LEDGER_SCHEMA, "next_id": 2,
                "items": [{
                    "finding_id": "F1", "severity": "major",
                    "status": "open", "occurrences": 2,
                }],
            },
        }
        metrics = G.review_metrics(state)
        self.assertEqual(metrics["review_admissions_total"], 1)
        self.assertEqual(metrics["review_admission_tiers"], {"high": 1})
        self.assertEqual(metrics["review_stops_total"], 1)
        self.assertEqual(metrics["rereported_findings"], 1)
        self.assertEqual(metrics["sol"]["completed"], 1)
        self.assertEqual(metrics["sol"]["verdicts"]["partial"], 1)
        self.assertTrue(metrics["g4"]["retired"])
        self.assertEqual(metrics["g4"]["historical_native_reviews"], 1)
        self.assertNotIn("mean_review_seconds", metrics)




if __name__ == "__main__":
    unittest.main()

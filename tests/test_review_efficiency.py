#!/usr/bin/env python3
"""v6.8.0 复核效率专项：准入、停止、Finding 台账与定向验证。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

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
    def test_plain_normal_task_does_not_require_review(self):
        state = {"risk": "normal", "review_stops": []}
        decision = G.review_admission(state)
        self.assertEqual(decision["decision"], "not_required")
        self.assertEqual(decision["scope"], "none")
        self.assertIn("确定性测试", decision["detail"])

    def test_high_risk_always_requires_g4(self):
        decision = G.review_admission({"risk": "high", "review_stops": []})
        self.assertEqual(decision["decision"], "required")
        self.assertEqual(decision["scope"], "g4")

    def test_declared_trigger_requires_advisory_review(self):
        decision = G.review_admission(
            {"risk": "normal", "review_stops": []},
            triggers=["security"], reason="凭据处理")
        self.assertEqual(decision["decision"], "required")
        self.assertEqual(decision["scope"], "advisory")
        self.assertEqual(decision["triggers"], ["security"])

    def test_unknown_trigger_is_rejected(self):
        with self.assertRaises(RP.ReviewPolicyError):
            RP.admission(risk="normal", triggers=["looks-complex"])


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
        self.assertEqual(RP.get_item(state, "F1")["status"], "regressed")
        self.assertEqual([item["finding_id"]
                          for item in RP.blocking_findings(state)], ["F1"])

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

    def test_admission_accepts_reentry_trigger_vocabulary(self):
        decision = RP.admission(risk="normal", triggers=["major-fix"])
        self.assertEqual(decision["decision"], "required")

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


class ReentryStopTests(unittest.TestCase):
    def _state(self, *, files, target="t" * 64, code="c" * 64,
               g4=True, attempt_files=None):
        state = {
            "risk": "high",
            "review_stops": [{
                "schema": RP.STOP_SCHEMA, "stop_id": "RST1",
                "review_sha256": "r" * 64,
                "review_attempt_id": "RA" + "a" * 24,
                "target_sha256": target, "code_scope_sha256": code,
                "verdict": "pass", "g4_eligible": g4,
                "findings": {"blocking": 0, "major": 0, "minor": 1, "info": 0},
                "created_at": G.now_iso(),
            }],
            "review_attempts": [{
                "review_attempt_id": "RA" + "a" * 24,
                "code_snapshot": {"files": attempt_files if attempt_files
                                  is not None else files},
            }],
        }
        return state

    def test_same_snapshot_is_not_reviewed_again(self):
        files = [{"path": "src/a.py", "sha256": "1", "status": " M"}]
        state = self._state(files=files, attempt_files=files)
        decision = RP.reentry_decision(
            state, target="t" * 64, code_scope_sha256="c" * 64, files=files)
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["code"], "review_not_needed")

    def test_docs_only_change_does_not_require_review(self):
        before = [{"path": "src/a.py", "sha256": "1", "status": " M"},
                  {"path": "README.md", "sha256": "old", "status": " M"}]
        after = [{"path": "src/a.py", "sha256": "1", "status": " M"},
                 {"path": "README.md", "sha256": "new", "status": " M"}]
        state = self._state(files=after, attempt_files=before, g4=False)
        state["risk"] = "normal"
        decision = RP.reentry_decision(
            state, target="t" * 64, code_scope_sha256="d" * 64, files=after)
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["code"], "review_not_needed_docs_only")

    def test_g4_docs_change_requires_new_review(self):
        before = [{"path": "src/a.py", "sha256": "1", "status": " M"},
                  {"path": "README.md", "sha256": "old", "status": " M"}]
        after = [{"path": "src/a.py", "sha256": "1", "status": " M"},
                 {"path": "README.md", "sha256": "new", "status": " M"}]
        state = self._state(files=after, attempt_files=before, g4=True)
        decision = RP.reentry_decision(
            state, target="t" * 64, code_scope_sha256="d" * 64, files=after)
        self.assertTrue(decision["allowed"])
        self.assertIn("G4", decision["detail"])

    def test_code_change_requires_declared_trigger(self):
        before = [{"path": "src/a.py", "sha256": "1", "status": " M"}]
        after = [{"path": "src/a.py", "sha256": "2", "status": " M"}]
        state = self._state(files=after, attempt_files=before)
        denied = RP.reentry_decision(
            state, target="t" * 64, code_scope_sha256="d" * 64, files=after)
        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["code"], "review_reentry_requires_trigger")
        allowed = RP.reentry_decision(
            state, target="t" * 64, code_scope_sha256="d" * 64, files=after,
            triggers=["code-changed"], reason="修复后代码变化")
        self.assertTrue(allowed["allowed"])

    def test_incremental_reentry_is_allowed(self):
        files = [{"path": "src/a.py", "sha256": "1", "status": " M"}]
        state = self._state(files=files, attempt_files=files)
        decision = RP.reentry_decision(
            state, target="u" * 64, code_scope_sha256="c" * 64,
            scope="incremental", files=files)
        self.assertTrue(decision["allowed"])

    def test_target_change_keeps_stop_as_reentry_signal(self):
        files = [{"path": "src/a.py", "sha256": "1", "status": " M"}]
        state = self._state(files=files, attempt_files=files)
        state["native_reviews"] = [{
            "result_sha256": "r" * 64, "verdict": "pass",
            "target_sha256": "t" * 64, "findings": [], "unverified": [],
            "g4_eligible": True, "candidate_g4": {"eligible": True},
        }]
        reentry_state, stop, valid = G._effective_reentry_state(
            state, "u" * 64)
        self.assertIsNotNone(stop)
        self.assertFalse(valid)
        decision = RP.reentry_decision(
            reentry_state, target="u" * 64, code_scope_sha256="c" * 64,
            files=files)
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["code"], "review_reentry_requires_trigger")

    def test_csv_is_not_treated_as_docs(self):
        before = [{"path": "data.csv", "sha256": "old", "status": " M"}]
        after = [{"path": "data.csv", "sha256": "new", "status": " M"}]
        state = self._state(files=after, attempt_files=before, g4=False)
        state["risk"] = "normal"
        decision = RP.reentry_decision(
            state, target="t" * 64, code_scope_sha256="d" * 64, files=after)
        self.assertEqual(decision["code"], "review_reentry_requires_trigger")

    def test_record_stop_is_idempotent_and_skips_non_pass(self):
        state = {}
        entry = review([], sha="1" * 64, target="t" * 64)
        first = RP.record_stop(state, entry, code_scope_sha256="c" * 64)
        second = RP.record_stop(state, entry, code_scope_sha256="c" * 64)
        self.assertEqual(first["stop_id"], second["stop_id"])
        self.assertEqual(len(state["review_stops"]), 1)
        self.assertIsNone(RP.record_stop(state, review([], verdict="partial",
                                                       sha="2" * 64)))


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


class AttemptReuseTests(F.BridgeBase):
    def test_same_snapshot_attempt_is_reused(self):
        execution = self.execution()
        AC._ingest_execution(self.state_path, execution)
        first = self.create_attempt(execution)
        second = self.create_attempt(execution)
        self.assertEqual(first["review_attempt_id"],
                         second["review_attempt_id"])
        state = G.load_state(self.state_path)
        self.assertEqual(len(state["review_attempts"]), 1)
        self.assertEqual(len(state["review_admissions"]), 1)

    def test_stop_marker_blocks_reentry_without_trigger(self):
        execution = self.execution()
        AC._ingest_execution(self.state_path, execution)
        attempt = self.create_attempt(execution)
        state = G.load_state(self.state_path)
        entry = review([], sha="r" * 64,
                       target=str(attempt["target_sha256"]),
                       attempt=attempt["review_attempt_id"])
        RP.record_stop(state, entry,
                       code_scope_sha256=str(attempt["code_scope_sha256"]))
        state.setdefault("native_reviews", []).append(entry)
        # PASS 之后该 attempt 已被消费；再次创建必须命中停止标记而不是复用旧快照。
        state["review_attempts"][0]["consumed"] = True
        G.save_state(self.state_path, state)
        with self.assertRaises(G.GovernanceError) as ctx:
            self.create_attempt(execution)
        self.assertIn("review_not_needed", str(ctx.exception))


class IngestLedgerTests(F.BridgeBase):
    def test_ingest_populates_ledger_and_is_idempotent(self):
        execution = self.execution()
        AC._ingest_execution(self.state_path, execution)
        env = self.review(
            execution, g4_eligible=False, target_sha256="",
            verdict="pass",
            findings=[{
                "id": "F1", "severity": "major", "file": self.evidence,
                "line": 1, "summary": "问题", "root_cause": "rc",
                "evidence": "证据"}])
        AC._ingest_review(self.state_path, env)
        state = G.load_state(self.state_path)
        self.assertEqual(len(state["finding_ledger"]["items"]), 1)
        self.assertEqual(state["native_reviews"][-1]["finding_refs"][0]
                         ["finding_id"], "F1")
        # 高风险任务的 g4_eligible=false 复核只是参考，不产生停止标记。
        self.assertEqual(state.get("review_stops") or [], [])
        AC._ingest_review(self.state_path, env)
        state = G.load_state(self.state_path)
        self.assertEqual(state["finding_ledger"]["items"][0]["occurrences"], 1)
        self.assertEqual(state.get("review_stops") or [], [])

    def test_second_review_reuses_finding_id(self):
        execution = self.execution()
        AC._ingest_execution(self.state_path, execution)
        first = self.review(
            execution, g4_eligible=False, target_sha256="",
            findings=[{"severity": "major", "file": self.evidence,
                       "line": 1, "summary": "问题 A", "root_cause": "rc",
                       "evidence": "证据"}])
        AC._ingest_review(self.state_path, first)
        second = self.review(
            execution, g4_eligible=False, target_sha256="",
            findings=[{"id": "F9", "severity": "major", "file": self.evidence,
                       "line": 1, "summary": "问题 A（换表述）",
                       "root_cause": "rc", "evidence": "新证据"}])
        AC._ingest_review(self.state_path, second)
        state = G.load_state(self.state_path)
        self.assertEqual(len(state["finding_ledger"]["items"]), 1)
        self.assertEqual(state["native_reviews"][-1]["finding_refs"][0]
                         ["finding_id"], "F1")


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
        self.assertEqual(payload["decision"], "not_required")

    def test_admission_cli_reports_required_for_security_trigger(self):
        proc = F.run_cli("copilot_dispatch.py", "review-admission",
                         "--state", self.state, "--trigger", "security",
                         "--reason", "凭据处理", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["decision"], "required")
        self.assertEqual(payload["scope"], "advisory")

    def test_admission_workspace_uses_same_evidence_exclusions(self):
        repo = tempfile.mkdtemp(prefix="adm-ws-")
        self.addCleanup(__import__("shutil").rmtree, repo, True)
        F.init_git_workspace(repo)
        evidence = os.path.join(repo, "evidence.txt")
        with open(evidence, "w", encoding="utf-8") as fh:
            fh.write("untracked evidence\n")
        state_path = os.path.join(repo, "state.json")
        G.start_state(state_path, goal="目标", contract=F.CONTRACT,
                      workspace=repo)
        state = G.load_state(state_path)
        G.register_evidence(state, "E1", file_path=evidence)
        G.save_state(state_path, state)
        state = G.load_state(state_path)
        target = G.final_evidence_digest(state)
        excludes = G.governance_paths(state_path) + \
            G.CS.safe_evidence_excludes(state, repo)
        snapshot = G.capture_code_snapshot(repo, exclude_paths=excludes)
        state["review_stops"] = [{
            "schema": RP.STOP_SCHEMA, "stop_id": "RST1",
            "review_sha256": "r" * 64, "target_sha256": target,
            "code_scope_sha256": snapshot["digest"], "verdict": "pass",
            "g4_eligible": False, "findings": {},
            "created_at": G.now_iso(),
        }]
        state["native_reviews"] = [{
            "result_sha256": "r" * 64, "verdict": "pass",
            "target_sha256": target, "findings": [], "unverified": [],
            "g4_eligible": False, "candidate_g4": {},
        }]
        G.save_state(state_path, state)
        proc = F.run_cli("copilot_dispatch.py", "review-admission",
                         "--state", state_path, "--workspace", repo, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["code_scope_checked"])
        self.assertEqual(payload["reentry"]["code"], "review_not_needed")

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
        proc = F.run_cli("copilot_dispatch.py", "finding", "--state", self.state,
                         "--id", "F1", "--status", "fixed", "--evidence", "E1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = F.run_cli("copilot_dispatch.py", "finding", "--state", self.state,
                         "--id", "F1", "--status", "verified")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("需要定向验证证据", proc.stderr)
        proc = F.run_cli("copilot_dispatch.py", "finding", "--state", self.state,
                         "--id", "F1", "--status", "verified",
                         "--evidence", "E1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = F.run_cli("copilot_dispatch.py", "finding", "--state", self.state,
                         "--list", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["items"][0]["status"], "verified")


class NormalRiskStopTests(F.BridgeBase):
    def test_pass_records_advisory_stop(self):
        state = G.load_state(self.state_path)
        state["risk"] = "normal"
        G.save_state(self.state_path, state)
        execution = self.execution()
        AC._ingest_execution(self.state_path, execution)
        env = self.review(execution, g4_eligible=False, target_sha256="")
        AC._ingest_review(self.state_path, env)
        state = G.load_state(self.state_path)
        self.assertEqual(len(state["review_stops"]), 1)
        self.assertIs(state["review_stops"][0]["g4_eligible"], False)
        self.assertIs(state["review_stops"][0]["authoritative"], False)


class MetricsEfficiencyTests(unittest.TestCase):
    def test_metrics_include_ledger_admissions_and_stop_counts(self):
        state = {
            "task_id": "task-1", "risk": "high", "created": G.now_iso(),
            "closure": {},
            "review_attempts": [{
                "review_attempt_id": "RA" + "a" * 24,
                "created_at": "2026-01-01T00:00:00+00:00",
            }],
            "native_reviews": [{
                "work_item_id": "A1", "verdict": "pass",
                "review_scope": "incremental", "g4_eligible": True,
                "review_attempt_id": "RA" + "a" * 24,
                "received": "2026-01-01T00:00:10+00:00",
                "findings": [{"severity": "major", "summary": "新问题"}],
                "based_on_review_sha256": "b" * 64,
                "evidence_manifest": ["E1"],
            }],
            "review_admissions": [{"triggers": ["major-fix"]}],
            "review_stops": [{"g4_eligible": True}],
            "finding_ledger": {
                "schema": RP.LEDGER_SCHEMA, "next_id": 2,
                "items": [{
                    "finding_id": "F1", "severity": "major",
                    "status": "open", "occurrences": 2,
                }],
            },
        }
        metrics = G.review_metrics(state)
        self.assertEqual(metrics["full_reviews"], 0)
        self.assertEqual(metrics["incremental_reviews"], 1)
        self.assertEqual(metrics["review_admissions_total"], 1)
        self.assertEqual(metrics["review_stops_total"], 1)
        self.assertEqual(metrics["rereported_findings"], 1)
        self.assertEqual(metrics["incremental_new_major"], 1)
        self.assertEqual(metrics["avg_first_full_review_seconds"], None)
        self.assertEqual(metrics["avg_review_seconds"], 10.0)


if __name__ == "__main__":
    unittest.main()

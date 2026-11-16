#!/usr/bin/env python3
"""v6.7.1 质量报告替代安全与代码范围绑定测试。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import code_scope as CS  # noqa: E402
import governance as G  # noqa: E402
import v6_support as F  # noqa: E402

CONTRACT = "## 完成条件\n- 产出可复核的结果文件\n"
CHECKS = {"required_tests_executed": True, "required_evidence_present": True,
          "completion_criteria_met": True, "delivery_verifiable": True}
W = [{"id": "W1", "text": "边界输入未验证", "severity": "minor"},
     {"id": "W2", "text": "错误路径未覆盖", "severity": "minor"}]
R = [{"id": "R1", "text": "性能未压测", "severity": "minor"}]


class BaseFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.task_dir = os.path.join(self.root, "task")
        os.makedirs(self.task_dir)
        self.state_path = os.path.join(self.task_dir, "state.json")
        G.start_state(self.state_path, goal="质量报告绑定", contract=CONTRACT)
        G.plan_state(self.state_path, {"summary": "实现", "actions": [
            {"id": "A1", "title": "实现", "goal": "产出结果",
             "expected_evidence": "结果文件"}]})
        self.evidence = os.path.join(self.task_dir, "result.txt")
        with open(self.evidence, "w", encoding="utf-8") as fh:
            fh.write("result\n")
        state = G.load_state(self.state_path)
        G.register_evidence(state, "E1", file_path=self.evidence)
        G.save_state(self.state_path, state)
        G.record_action(self.state_path, "A1", "done", result="完成",
                        evidence_ids=["E1"])
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=["E1"])
        self.seq = 0

    def write_report(self, block: dict) -> str:
        self.seq += 1
        path = os.path.join(self.task_dir, f"report-{self.seq}.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("<!-- copilot-verify-result/v1 -->\n```json\n"
                     + json.dumps(block, ensure_ascii=False) + "\n```\n")
        return path

    def ingest(self, block: dict, **kwargs) -> dict:
        return G.ingest_quality_report(
            self.state_path, self.write_report(block), **kwargs)

    def codes(self) -> set[str]:
        state = G.load_state(self.state_path)
        return {reason["code"] for reason in G.closure_check(state)["reasons"]}

    def block(self, *, report_id, verdict="PASS_WITH_WARNINGS",
              warnings=None, risks=None, supersedes=None, resolved_w=(),
              resolved_r=(), subject=None, gen=None):
        payload = {
            "schema": G.QUALITY_SCHEMA,
            "report_id": report_id,
            "generated_at": gen or f"2026-11-16T12:00:{self.seq:02d}+08:00",
            "verdict": verdict,
            "warnings": W if warnings is None else warnings,
            "residual_risks": R if risks is None else risks,
            "unverified": [],
            "acceptance_required": verdict == "PASS_WITH_WARNINGS",
            "acceptance_status": ("pending" if verdict == "PASS_WITH_WARNINGS"
                                  else "not_required"),
            "checks": CHECKS,
            "subject": subject or {"scope": "fixture", "summary": "fixture"},
        }
        if supersedes is not None:
            payload["supersedes_report_id"] = supersedes
        if resolved_w:
            payload["resolved_warning_ids"] = list(resolved_w)
        if resolved_r:
            payload["resolved_residual_risk_ids"] = list(resolved_r)
        return payload

    def accept(self, ids, **kwargs) -> dict:
        return G.accept_pww(self.state_path, ids=ids, accepted_by="user",
                            quote=kwargs.pop("quote", "用户明确接受这些风险"),
                            **kwargs)


class SupersedeTests(BaseFixture):
    def test_first_report_does_not_need_supersedes(self):
        result = self.ingest(self.block(report_id="VR-1"))
        self.assertFalse(result["idempotent"])

    def test_missing_supersedes_is_rejected(self):
        self.ingest(self.block(report_id="VR-1"))
        with self.assertRaisesRegex(G.GovernanceError, "pww_supersede_missing"):
            self.ingest(self.block(report_id="VR-2", verdict="PASS",
                                   warnings=[], risks=[],
                                   resolved_w=("W1", "W2"),
                                   resolved_r=("R1",)))

    def test_invalid_supersede_target_is_rejected(self):
        self.ingest(self.block(report_id="VR-1"))
        with self.assertRaisesRegex(G.GovernanceError,
                                    "pww_supersede_invalid_target"):
            self.ingest(self.block(report_id="VR-2", verdict="PASS",
                                   warnings=[], risks=[],
                                   supersedes="VR-OTHER",
                                   resolved_w=("W1", "W2"),
                                   resolved_r=("R1",)))

    def test_pass_partial_resolution_is_rejected(self):
        self.ingest(self.block(report_id="VR-1"))
        with self.assertRaisesRegex(G.GovernanceError,
                                    "pww_supersede_coverage_incomplete"):
            self.ingest(self.block(report_id="VR-2", verdict="PASS",
                                   warnings=[], risks=[],
                                   supersedes="VR-1",
                                   resolved_w=("W1",)))

    def test_pass_full_resolution_allows_close(self):
        self.ingest(self.block(report_id="VR-1"))
        self.ingest(self.block(report_id="VR-2", verdict="PASS",
                               warnings=[], risks=[], supersedes="VR-1",
                               resolved_w=("W1", "W2"),
                               resolved_r=("R1",)))
        self.assertEqual(self.codes(), set())
        closure = G.close_task(self.state_path)
        self.assertEqual(closure["verdict"], "pass")
        state = G.load_state(self.state_path)
        old = next(r for r in state["quality_reports"]
                   if r["report_id"] == "VR-1")
        self.assertEqual(old["acceptance_status"], "superseded")
        self.assertIsNone(G._acceptance_criterion(state))

    def test_pww_partial_coverage_is_rejected(self):
        self.ingest(self.block(report_id="VR-1"))
        with self.assertRaisesRegex(G.GovernanceError,
                                    "pww_supersede_coverage_incomplete"):
            self.ingest(self.block(
                report_id="VR-2", supersedes="VR-1",
                warnings=[{"id": "W2", "text": "只覆盖 W2", "severity": "minor"}],
                risks=[]))

    def test_pww_full_carry_creates_new_acceptance(self):
        self.ingest(self.block(report_id="VR-1"))
        self.ingest(self.block(report_id="VR-2", supersedes="VR-1"))
        state = G.load_state(self.state_path)
        criterion = G._acceptance_criterion(state)
        self.assertEqual(criterion["status"], "pending")
        self.assertEqual(set(criterion["acceptance"]["required_ids"]),
                         {"W1", "W2", "R1"})
        self.assertIn("pww_acceptance_pending", self.codes())
        self.accept(["W1", "W2", "R1"])
        self.assertEqual(self.codes(), set())
        self.assertEqual(G.close_task(self.state_path)["verdict"],
                         "pass_with_warnings")

    def test_subject_mismatch_rejects_resolution(self):
        self.ingest(self.block(report_id="VR-1"))
        with self.assertRaisesRegex(G.GovernanceError,
                                    "pww_supersede_subject_mismatch"):
            self.ingest(self.block(
                report_id="VR-2", verdict="PASS", warnings=[], risks=[],
                supersedes="VR-1", resolved_w=("W1", "W2"),
                resolved_r=("R1",),
                subject={"scope": "other", "summary": "other"}))

    def test_subject_mismatch_allows_pww_carry(self):
        self.ingest(self.block(report_id="VR-1"))
        self.ingest(self.block(
            report_id="VR-2", supersedes="VR-1",
            subject={"scope": "other", "summary": "other"}))
        state = G.load_state(self.state_path)
        self.assertEqual(G.quality_summary(state)["unresolved_ids"],
                         ["W1", "W2", "R1"])
        self.accept(["W1", "W2", "R1"])
        self.assertEqual(G.close_task(self.state_path)["verdict"],
                         "pass_with_warnings")

    def test_unknown_resolved_id_is_rejected(self):
        self.ingest(self.block(report_id="VR-1"))
        with self.assertRaisesRegex(G.GovernanceError,
                                    "pww_supersede_coverage_incomplete"):
            self.ingest(self.block(
                report_id="VR-2", verdict="PASS", warnings=[], risks=[],
                supersedes="VR-1", resolved_w=("W1", "W2", "W9"),
                resolved_r=("R1",)))

    def test_accepted_old_pww_can_be_superseded_by_new_pass(self):
        self.ingest(self.block(report_id="VR-1"))
        self.accept(["W1", "W2", "R1"])
        self.ingest(self.block(
            report_id="VR-2", verdict="PASS", warnings=[], risks=[],
            supersedes="VR-1",
            subject={"scope": "other", "summary": "other"}))
        self.assertEqual(G.close_task(self.state_path)["verdict"], "pass")
        state = G.load_state(self.state_path)
        old = next(r for r in state["quality_reports"]
                   if r["report_id"] == "VR-1")
        self.assertEqual(old["acceptance"]["accepted_ids"],
                         ["W1", "W2", "R1"])

    def test_chain_cannot_launder_unresolved_through_fail(self):
        self.ingest(self.block(report_id="VR-1"))
        self.ingest(self.block(report_id="VR-2", verdict="FAIL",
                               warnings=[], risks=[], supersedes="VR-1",
                               subject={"scope": "other", "summary": "other"}))
        state = G.load_state(self.state_path)
        self.assertEqual(G.quality_summary(state)["unresolved_ids"],
                         ["W1", "W2", "R1"])
        with self.assertRaisesRegex(G.GovernanceError,
                                    "pww_supersede_subject_mismatch"):
            self.ingest(self.block(
                report_id="VR-3", verdict="PASS", warnings=[], risks=[],
                supersedes="VR-2", resolved_w=("W1", "W2"),
                resolved_r=("R1",),
                subject={"scope": "other2", "summary": "other2"}))

    def test_new_warnings_are_not_inherited_from_old_acceptance(self):
        self.ingest(self.block(report_id="VR-1"))
        self.accept(["W1", "W2", "R1"])
        self.ingest(self.block(
            report_id="VR-2", supersedes="VR-1",
            warnings=[{"id": "W3", "text": "新警告", "severity": "minor"}],
            risks=[]))
        state = G.load_state(self.state_path)
        criterion = G._acceptance_criterion(state)
        self.assertEqual(criterion["acceptance"]["required_ids"], ["W3"])
        self.assertEqual(criterion["status"], "pending")
        self.assertIn("pww_acceptance_pending", self.codes())

    def test_duplicate_ingest_is_idempotent(self):
        block = self.block(report_id="VR-1")
        self.ingest(block)
        second = self.ingest(block)
        self.assertTrue(second["idempotent"])
        self.assertEqual(len(G.load_state(self.state_path)["quality_reports"]), 1)

    def test_superseded_report_replay_is_rejected(self):
        first = self.block(report_id="VR-1")
        self.ingest(first)
        self.ingest(self.block(report_id="VR-2", supersedes="VR-1"))
        with self.assertRaisesRegex(G.GovernanceError, "verify_report_superseded"):
            self.ingest(first)

    def test_g2_detects_tampered_unresolved(self):
        self.ingest(self.block(report_id="VR-1"))
        state = G.load_state(self.state_path)
        state["quality_reports"][0]["unresolved"] = [
            {"kind": "warning", "id": "W1",
             "origin_report_id": "VR-1", "origin_subject_digest": "x"}]
        G.save_state(self.state_path, state)
        self.assertTrue({"pww_supersede_inconsistent",
                         "pww_supersede_coverage_incomplete"} &
                        self.codes())

    def test_g2_detects_tampered_superseded_target(self):
        self.ingest(self.block(report_id="VR-1"))
        self.ingest(self.block(report_id="VR-2", supersedes="VR-1"))
        state = G.load_state(self.state_path)
        old = next(r for r in state["quality_reports"]
                   if r["report_id"] == "VR-1")
        old["warnings"] = []
        G.save_state(self.state_path, state)
        self.assertIn("pww_supersede_target_tampered", self.codes())

    def test_structurally_invalid_state_fails_closed(self):
        self.ingest(self.block(report_id="VR-1"))
        state = G.load_state(self.state_path)
        state["quality_reports"][0]["warnings"] = ["W1"]
        G.save_state(self.state_path, state)
        codes = {reason["code"] for reason in G.closure_check(state)["reasons"]}
        self.assertIn("quality_report_state_invalid", codes)

    def test_warning_unknown_nested_field_is_rejected(self):
        with self.assertRaisesRegex(G.GovernanceError, "verify_report_invalid"):
            self.ingest(self.block(
                report_id="VR-1",
                warnings=[{"id": "W1", "text": "x", "file": "src/app.py"}],
                risks=[]))

    def test_noop_contract_amend_preserves_acceptance(self):
        self.ingest(self.block(report_id="VR-1"))
        self.accept(["W1", "W2", "R1"])
        G.amend_contract(self.state_path)
        self.assertEqual(self.codes(), set())
        result = self.accept(["W1", "W2", "R1"])
        self.assertTrue(result.get("idempotent"))
        self.assertEqual(G.close_task(self.state_path)["verdict"],
                         "pass_with_warnings")

    def test_non_pww_report_does_not_create_acceptance(self):
        self.ingest(self.block(report_id="VR-1"))
        self.ingest(self.block(report_id="VR-2", verdict="FAIL",
                               warnings=W, risks=R, supersedes="VR-1"))
        state = G.load_state(self.state_path)
        self.assertIsNone(G._acceptance_criterion(state))
        actions = G.derive_work(state, reason="partial")
        self.assertFalse(any(str(item.get("id") or "").startswith("AUTO-ACCEPT")
                             for item in actions))
        self.assertTrue(any(str(item.get("id") or "").startswith("AUTO-QUALITY")
                            for item in actions))

    def test_legacy_binding_accepts_subject_digest_variants(self):
        self.ingest(self.block(report_id="VR-1"))
        state = G.load_state(self.state_path)
        report = state["quality_reports"][0]
        report.pop("ingest_version", None)
        old = G._sha256_text(G.canonical_json(G._quality_binding_payload(
            state, report, include_subject=False)))
        transitional = G._sha256_text(G.canonical_json(
            G._quality_binding_payload(
                state, report, include_subject=True, include_code_scope=False)))
        for value in (old, transitional):
            report["binding_sha256"] = value
            self.assertTrue(G._quality_binding_current(state, report))

    def test_legacy_payload_omits_empty_generated_at(self):
        payload = G._quality_payload({
            "schema": G.QUALITY_SCHEMA, "verdict": "pass",
            "warnings": [], "residual_risks": [], "unverified": [],
            "acceptance_required": False, "checks": {}, "subject": {},
        }, legacy=True)
        self.assertNotIn("generated_at", payload)
        versioned = G._quality_payload({
            "schema": G.QUALITY_SCHEMA, "verdict": "pass",
            "warnings": [], "residual_risks": [], "unverified": [],
            "acceptance_required": False, "checks": {}, "subject": {},
        })
        self.assertEqual(versioned["generated_at"], "")

    def test_legacy_acceptance_without_history_is_tolerated(self):
        self.ingest(self.block(report_id="VR-1"))
        self.accept(["W1", "W2", "R1"])
        state = G.load_state(self.state_path)
        report = state["quality_reports"][0]
        report.pop("ingest_version", None)
        report.pop("acceptance_history", None)
        report.pop("supersedes_report_id", None)
        report.pop("resolved_warning_ids", None)
        report.pop("resolved_residual_risk_ids", None)
        report.pop("unresolved", None)
        report["report_sha256"] = G._sha256_text(G.canonical_json(
            G._quality_payload(report, legacy=True)))
        report["binding_sha256"] = G._sha256_text(G.canonical_json(
            G._quality_binding_payload(state, report, include_subject=False)))
        criterion = G._acceptance_criterion(state)
        criterion["acceptance"]["binding_sha256"] = report["binding_sha256"]
        G.save_state(self.state_path, state)
        self.assertTrue(G._quality_binding_current(state, report))
        self.assertNotIn("pww_acceptance_record_inconsistent", self.codes())
        self.assertTrue(G.closure_check(state)["passed"])

    def test_pass_binding_change_blocks_closure(self):
        self.ingest(self.block(report_id="VR-PASS", verdict="PASS",
                               warnings=[], risks=[]))
        self.assertEqual(self.codes(), set())
        G.amend_contract(self.state_path,
                         criteria=["产出可复核结果文件", "新增条件"])
        self.assertIn("quality_binding_stale", self.codes())

    def test_reopen_does_not_derive_auto_accept(self):
        self.ingest(self.block(report_id="VR-1"))
        self.accept(["W1", "W2", "R1"])
        G.close_task(self.state_path)
        G.reopen_task(self.state_path, "补验证", authorized_by="user")
        state = G.load_state(self.state_path)
        criterion = G._acceptance_criterion(state)
        self.assertEqual(criterion["acceptance"]["status"], "pending")
        actions = G.derive_work(state, reason="partial")
        self.assertFalse(any(str(item.get("id") or "").startswith("AUTO-ACCEPT")
                             for item in actions))
        self.assertTrue(any(str(item.get("id") or "").startswith("AUTO-QUALITY")
                            for item in actions))

    def test_repeat_partial_accept_is_idempotent(self):
        self.ingest(self.block(report_id="VR-1"))
        first = self.accept(["W1"])
        self.assertEqual(first["status"], "partial")
        state = G.load_state(self.state_path)
        before = len(state["quality_reports"][0].get("acceptance_history") or [])
        second = self.accept(["W1"])
        self.assertTrue(second.get("idempotent"))
        self.assertEqual(second["status"], "partial")
        state = G.load_state(self.state_path)
        after = len(state["quality_reports"][0].get("acceptance_history") or [])
        self.assertEqual(before, after)

    def test_pending_pww_does_not_derive_auto_quality(self):
        self.ingest(self.block(report_id="VR-1"))
        state = G.load_state(self.state_path)
        actions = G.derive_work(state, reason="partial")
        self.assertTrue(any(str(item.get("id") or "").startswith("AUTO-ACCEPT")
                            for item in actions))
        self.assertFalse(any(str(item.get("id") or "").startswith("AUTO-QUALITY")
                             for item in actions))

    def test_invalid_severity_is_rejected(self):
        with self.assertRaisesRegex(G.GovernanceError, "severity"):
            self.ingest(self.block(
                report_id="VR-1",
                warnings=[{"id": "W1", "text": "x", "severity": "critical"}],
                risks=[]))

    def test_falsy_non_list_warnings_are_rejected(self):
        with self.assertRaisesRegex(G.GovernanceError, "必须是数组"):
            self.ingest(self.block(report_id="VR-1", warnings={}, risks=[]))

    def test_structurally_invalid_subject_fails_closed(self):
        self.ingest(self.block(report_id="VR-1"))
        state = G.load_state(self.state_path)
        state["quality_reports"][0]["subject"] = ["not-an-object"]
        G.save_state(self.state_path, state)
        self.assertIn("quality_report_state_invalid", self.codes())

    def test_workspace_with_doc_subject_is_rejected(self):
        with self.assertRaisesRegex(G.GovernanceError,
                                    "verify_code_scope_missing"):
            self.ingest(self.block(report_id="VR-DOC-1",
                                   subject={"scope": "doc"}),
                        workspace=self.root)

    def test_legacy_transition_binding_accept_is_idempotent(self):
        self.ingest(self.block(report_id="VR-1"))
        self.accept(["W1", "W2", "R1"])
        state = G.load_state(self.state_path)
        report = state["quality_reports"][0]
        report.pop("ingest_version", None)
        report["report_sha256"] = G._sha256_text(G.canonical_json(
            G._quality_payload(report, legacy=True)))
        transitional = G._sha256_text(G.canonical_json(
            G._quality_binding_payload(
                state, report, include_subject=True, include_code_scope=False)))
        report["binding_sha256"] = transitional
        report["acceptance"]["binding_sha256"] = transitional
        criterion = G._acceptance_criterion(state)
        criterion["acceptance"]["binding_sha256"] = transitional
        G.save_state(self.state_path, state)
        result = self.accept(["W1", "W2", "R1"])
        self.assertTrue(result.get("idempotent"))
        state = G.load_state(self.state_path)
        self.assertEqual(state["quality_reports"][0]["acceptance"]["accepted_ids"],
                         ["W1", "W2", "R1"])

    def test_supersede_save_failure_leaves_state_unchanged(self):
        self.ingest(self.block(report_id="VR-1"))
        import hashlib

        def digest(path):
            with open(path, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()

        before = digest(self.state_path)
        with mock.patch.object(G, "save_state",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.ingest(self.block(report_id="VR-2", supersedes="VR-1"))
        self.assertEqual(digest(self.state_path), before)


class CodeScopeTests(BaseFixture):
    def setUp(self):
        super().setUp()
        self.workspace = os.path.join(self.root, "workspace")
        os.makedirs(os.path.join(self.workspace, "src"))
        subprocess.run(["git", "init", "-q", self.workspace], check=True)
        subprocess.run(["git", "-C", self.workspace, "config", "user.email",
                        "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", self.workspace, "config", "user.name",
                        "test"], check=True)
        self.app = os.path.join(self.workspace, "src", "app.py")
        with open(self.app, "w", encoding="utf-8") as fh:
            fh.write("value = 1\n")
        subprocess.run(["git", "-C", self.workspace, "add", "."], check=True)
        subprocess.run(["git", "-C", self.workspace, "commit", "-qm", "base"],
                       check=True)
        G.set_task_workspace(self.state_path, self.workspace, source="test")

    def snapshot(self) -> dict:
        excludes = CS.governance_paths(self.state_path)
        return CS.capture_code_snapshot(self.workspace, exclude_paths=excludes)

    def code_subject(self, snapshot: dict | None = None,
                     files: list[str] | None = None) -> dict:
        snap = snapshot or self.snapshot()
        changed = sorted({entry["path"] for entry in snap["files"]})
        return {
            "scope": "src/app.py",
            "commit": snap["git_head"],
            "dirty": bool(snap["files"]),
            "files": files if files is not None else (changed or ["src/app.py"]),
            "code_scope_sha256": snap["digest"],
        }

    def code_block(self, *, report_id, verdict="PASS_WITH_WARNINGS",
                   warnings=None, risks=None, supersedes=None,
                   resolved_w=(), resolved_r=(), subject=None,
                   files=None) -> dict:
        snap = self.snapshot() if subject is None else None
        return self.block(
            report_id=report_id, verdict=verdict, warnings=warnings,
            risks=risks, supersedes=supersedes, resolved_w=resolved_w,
            resolved_r=resolved_r,
            subject=subject or self.code_subject(snap, files=files))

    def test_code_report_binds_snapshot_and_closes(self):
        with open(self.app, "w", encoding="utf-8") as fh:
            fh.write("value = 2\n")
        result = self.ingest(self.code_block(report_id="VR-CODE-1"),
                             workspace=self.workspace)
        report = result["report"]
        self.assertTrue(report["code_scope_sha256"])
        self.assertEqual(report["code_scope"]["git_head"],
                         report["subject"]["commit"])
        self.accept(["W1", "W2", "R1"])
        self.assertEqual(self.codes(), set())
        self.assertEqual(G.close_task(self.state_path)["verdict"],
                         "pass_with_warnings")

    def test_clean_workspace_code_report_without_files(self):
        snap = self.snapshot()
        subject = {"scope": "src/app.py", "commit": snap["git_head"],
                   "dirty": False, "code_scope_sha256": snap["digest"]}
        self.ingest(self.block(report_id="VR-CODE-1", subject=subject),
                    workspace=self.workspace)
        self.accept(["W1", "W2", "R1"])
        self.assertEqual(G.close_task(self.state_path)["verdict"],
                         "pass_with_warnings")

    def test_code_report_requires_workspace(self):
        with self.assertRaisesRegex(G.GovernanceError,
                                    "verify_code_scope_workspace_required"):
            self.ingest(self.code_block(report_id="VR-CODE-1"))

    def test_workspace_mismatch_is_rejected(self):
        other = os.path.join(self.root, "other")
        os.makedirs(other)
        subprocess.run(["git", "init", "-q", other], check=True)
        subprocess.run(["git", "-C", other, "config", "user.email",
                        "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", other, "config", "user.name", "test"],
                       check=True)
        with open(os.path.join(other, "clean.py"), "w", encoding="utf-8") as fh:
            fh.write("clean = 1\n")
        subprocess.run(["git", "-C", other, "add", "."], check=True)
        subprocess.run(["git", "-C", other, "commit", "-qm", "base"], check=True)
        with self.assertRaisesRegex(G.GovernanceError,
                                    "quality_code_scope_workspace_mismatch"):
            self.ingest(self.code_block(report_id="VR-CODE-1"),
                        workspace=other)

    def test_unbound_task_fails_closed(self):
        state = G.load_state(self.state_path)
        state["workspace"] = ""
        state["workspace_source"] = ""
        G.save_state(self.state_path, state)
        with self.assertRaisesRegex(G.GovernanceError,
                                    "quality_code_scope_workspace_unbound"):
            self.ingest(self.code_block(report_id="VR-CODE-1"),
                        workspace=self.workspace)

    def test_workspace_derives_from_evidence(self):
        with open(self.app, "w", encoding="utf-8") as fh:
            fh.write("value = 2\n")
        state = G.load_state(self.state_path)
        state["workspace"] = ""
        state["workspace_source"] = ""
        G.register_evidence(state, "E2", file_path=self.app)
        G.save_state(self.state_path, state)
        self.assertEqual(G.task_workspace(G.load_state(self.state_path),
                                          self.state_path),
                         os.path.realpath(self.workspace))
        result = self.ingest(self.code_block(
            report_id="VR-CODE-1", files=["src/app.py"]),
            workspace=self.workspace)
        self.assertEqual(result["report"]["workspace"],
                         os.path.realpath(self.workspace))
        self.assertEqual(G.load_state(self.state_path)["workspace_source"],
                         "derived")

    def test_workspace_symlink_resolves_to_bound_repo(self):
        link = os.path.join(self.root, "workspace-link")
        os.symlink(self.workspace, link)
        result = self.ingest(self.code_block(report_id="VR-CODE-1"),
                             workspace=link)
        self.assertEqual(result["report"]["workspace"],
                         os.path.realpath(self.workspace))

    def test_code_scope_metadata_binds_report_and_task(self):
        result = self.ingest(self.code_block(report_id="VR-CODE-1"),
                             workspace=self.workspace)
        snapshot = result["report"]["code_scope"]
        self.assertEqual(snapshot["report_id"], "VR-CODE-1")
        self.assertEqual(snapshot["report_sha256"],
                         result["report"]["report_sha256"])
        self.assertEqual(snapshot["task_id"],
                         G.load_state(self.state_path)["task_id"])
        self.assertEqual(snapshot["capture_source"],
                         "copilot-ingest-recompute")
        self.assertTrue(snapshot["captured_at"])
        self.assertFalse(G._code_scope_status(result["report"],
                                              G.load_state(self.state_path))["reasons"])

    def test_g2_rejects_workspace_rebinding(self):
        self.ingest(self.code_block(report_id="VR-CODE-1"),
                    workspace=self.workspace)
        other = os.path.join(self.root, "other2")
        os.makedirs(other)
        subprocess.run(["git", "init", "-q", other], check=True)
        state = G.load_state(self.state_path)
        state["workspace"] = os.path.realpath(other)
        G.save_state(self.state_path, state)
        self.assertIn("quality_code_scope_workspace_mismatch", self.codes())

    def test_workspace_change_invalidates_acceptance(self):
        self.ingest(self.code_block(report_id="VR-CODE-1"),
                    workspace=self.workspace)
        self.accept(["W1", "W2", "R1"])
        other = os.path.join(self.root, "other3")
        os.makedirs(other)
        subprocess.run(["git", "init", "-q", other], check=True)
        state = G.load_state(self.state_path)
        state["workspace"] = os.path.realpath(other)
        G.save_state(self.state_path, state)
        with self.assertRaisesRegex(
                G.GovernanceError,
                "pww_acceptance_stale|quality_code_scope_workspace_mismatch"):
            self.accept(["W1", "W2", "R1"])

    def test_code_report_requires_declared_digest(self):
        subject = self.code_subject()
        subject.pop("code_scope_sha256")
        with self.assertRaisesRegex(G.GovernanceError,
                                    "verify_code_scope_missing"):
            self.ingest(self.block(report_id="VR-CODE-1", subject=subject),
                        workspace=self.workspace)

    def test_code_change_between_report_generation_and_ingest_is_rejected(self):
        subject = self.code_subject()
        with open(self.app, "w", encoding="utf-8") as fh:
            fh.write("value = 2\n")
        with self.assertRaisesRegex(G.GovernanceError,
                                    "quality_code_scope_mismatch"):
            self.ingest(self.block(report_id="VR-CODE-1", subject=subject),
                        workspace=self.workspace)

    def test_commit_mismatch_is_rejected(self):
        subject = self.code_subject()
        subject["commit"] = "f" * 40
        with self.assertRaisesRegex(G.GovernanceError,
                                    "quality_code_scope_commit_mismatch"):
            self.ingest(self.block(report_id="VR-CODE-1", subject=subject),
                        workspace=self.workspace)

    def test_snapshot_mismatch_is_rejected(self):
        subject = self.code_subject()
        subject["code_scope_sha256"] = "0" * 64
        with self.assertRaisesRegex(G.GovernanceError,
                                    "quality_code_scope_mismatch"):
            self.ingest(self.block(report_id="VR-CODE-1", subject=subject),
                        workspace=self.workspace)

    def test_undeclared_change_is_rejected(self):
        with open(self.app, "w", encoding="utf-8") as fh:
            fh.write("value = 2\n")
        other = os.path.join(self.workspace, "src", "other.py")
        with open(other, "w", encoding="utf-8") as fh:
            fh.write("other = 1\n")
        with self.assertRaisesRegex(G.GovernanceError,
                                    "quality_code_scope_files_incomplete"):
            self.ingest(self.code_block(report_id="VR-CODE-1",
                                        files=["src/app.py"]),
                        workspace=self.workspace)

    def test_dirty_flag_mismatch_is_rejected(self):
        with open(self.app, "w", encoding="utf-8") as fh:
            fh.write("value = 2\n")
        subject = self.code_subject()
        subject["dirty"] = False
        with self.assertRaisesRegex(G.GovernanceError,
                                    "quality_code_scope_dirty_mismatch"):
            self.ingest(self.block(report_id="VR-CODE-1", subject=subject),
                        workspace=self.workspace)

    def test_code_drift_after_acceptance_blocks_closure(self):
        self.ingest(self.code_block(report_id="VR-CODE-1"),
                    workspace=self.workspace)
        self.accept(["W1", "W2", "R1"])
        self.assertEqual(self.codes(), set())
        with open(self.app, "w", encoding="utf-8") as fh:
            fh.write("value = 3\n")
        self.assertIn("quality_code_scope_drift", self.codes())
        with self.assertRaisesRegex(G.GovernanceError, "quality_code_scope_drift"):
            self.accept(["W1", "W2", "R1"])
        with self.assertRaises(G.GovernanceError):
            G.close_task(self.state_path)

    def test_new_code_report_rebinds_after_drift(self):
        self.ingest(self.code_block(report_id="VR-CODE-1"),
                    workspace=self.workspace)
        self.accept(["W1", "W2", "R1"])
        with open(self.app, "w", encoding="utf-8") as fh:
            fh.write("value = 2\n")
        self.assertIn("quality_code_scope_drift", self.codes())
        # 旧报告已被接受，未解决集合为空；新报告可换 subject 并重新绑定。
        supersedes = "VR-CODE-1"
        block = self.code_block(report_id="VR-CODE-2", supersedes=supersedes)
        self.ingest(block, workspace=self.workspace)
        self.assertNotIn("quality_code_scope_drift", self.codes())
        self.accept(["W1", "W2", "R1"])
        self.assertEqual(G.close_task(self.state_path)["verdict"],
                         "pass_with_warnings")

    def test_committed_head_change_drift(self):
        self.ingest(self.code_block(report_id="VR-CODE-1"),
                    workspace=self.workspace)
        self.accept(["W1", "W2", "R1"])
        with open(self.app, "w", encoding="utf-8") as fh:
            fh.write("value = 2\n")
        subprocess.run(["git", "-C", self.workspace, "add", "."], check=True)
        subprocess.run(["git", "-C", self.workspace, "commit", "-qm", "change"],
                       check=True)
        self.assertIn("quality_code_scope_drift", self.codes())

    def test_rename_drift(self):
        self.ingest(self.code_block(report_id="VR-CODE-1"),
                    workspace=self.workspace)
        self.accept(["W1", "W2", "R1"])
        subprocess.run(["git", "-C", self.workspace, "mv", "src/app.py",
                        "src/main.py"], check=True)
        self.assertIn("quality_code_scope_drift", self.codes())

    def test_added_and_deleted_files_drift(self):
        with open(self.app, "w", encoding="utf-8") as fh:
            fh.write("value = 2\n")
        self.ingest(self.code_block(report_id="VR-CODE-1"),
                    workspace=self.workspace)
        self.accept(["W1", "W2", "R1"])
        other = os.path.join(self.workspace, "src", "other.py")
        with open(other, "w", encoding="utf-8") as fh:
            fh.write("other = 1\n")
        self.assertIn("quality_code_scope_drift", self.codes())
        os.remove(other)
        os.remove(self.app)
        self.assertIn("quality_code_scope_drift", self.codes())

    def test_evidence_change_cannot_be_hidden_from_snapshot(self):
        with open(self.app, "w", encoding="utf-8") as fh:
            fh.write("value = 2\n")
        state = G.load_state(self.state_path)
        G.register_evidence(state, "E2", file_path=self.app)
        G.save_state(self.state_path, state)
        subject = self.code_subject()
        self.assertIn("src/app.py", [entry["path"] for entry in
                                     self.snapshot()["files"]])
        proc = F.run_cli("code_scope_capture.py", "--workspace", self.workspace,
                         "--state", self.state_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("src/app.py", json.loads(proc.stdout)["files_paths"])
        with self.assertRaisesRegex(G.GovernanceError,
                                    "quality_code_scope_files_incomplete"):
            self.ingest(self.block(
                report_id="VR-CODE-1",
                subject={**subject, "files": ["src/other.py"]}),
                workspace=self.workspace)

    def test_newly_registered_evidence_does_not_false_drift(self):
        with open(self.app, "w", encoding="utf-8") as fh:
            fh.write("value = 2\n")
        evidence = os.path.join(self.workspace, "evidence.txt")
        with open(evidence, "w", encoding="utf-8") as fh:
            fh.write("result\n")
        self.ingest(self.code_block(
            report_id="VR-CODE-1", files=["src/app.py", "evidence.txt"]),
            workspace=self.workspace)
        state = G.load_state(self.state_path)
        G.register_evidence(state, "E2", file_path=evidence)
        G.save_state(self.state_path, state)
        self.assertNotIn("quality_code_scope_drift", self.codes())

    def test_rename_files_paths_include_old_and_new(self):
        with open(self.app, "w", encoding="utf-8") as fh:
            fh.write("value = 2\n")
        subprocess.run(["git", "-C", self.workspace, "mv", "src/app.py",
                        "src/new.py"], check=True)
        proc = F.run_cli("code_scope_capture.py", "--workspace", self.workspace,
                         "--state", self.state_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("src/app.py", payload["files_paths"])
        self.assertIn("src/new.py", payload["files_paths"])
        subject = {"scope": "src/new.py", "commit": payload["commit"],
                   "dirty": payload["dirty"], "files": payload["files_paths"],
                   "code_scope_sha256": payload["code_scope_sha256"]}
        self.ingest(self.block(report_id="VR-CODE-1", subject=subject),
                    workspace=self.workspace)
        self.accept(["W1", "W2", "R1"])
        self.assertEqual(G.close_task(self.state_path)["verdict"],
                         "pass_with_warnings")

    def test_subject_files_must_be_string_paths(self):
        subject = self.code_subject()
        subject["files"] = [{"path": "src/app.py"}]
        with self.assertRaisesRegex(G.GovernanceError,
                                    "subject.files 的每一项"):
            self.ingest(self.block(report_id="VR-CODE-1", subject=subject),
                        workspace=self.workspace)

    def test_clean_workspace_accepts_empty_files(self):
        snap = self.snapshot()
        subject = {"scope": "src/app.py", "commit": snap["git_head"],
                   "dirty": False, "files": [],
                   "code_scope_sha256": snap["digest"]}
        self.ingest(self.block(report_id="VR-CODE-1", subject=subject),
                    workspace=self.workspace)
        self.accept(["W1", "W2", "R1"])
        self.assertEqual(G.close_task(self.state_path)["verdict"],
                         "pass_with_warnings")

    def test_report_file_inside_workspace_is_rejected(self):
        path = os.path.join(self.workspace, "verify-report.md")
        block = self.block(report_id="VR-CODE-1", subject=self.code_subject())
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("<!-- copilot-verify-result/v1 -->\n```json\n"
                     + json.dumps(block, ensure_ascii=False) + "\n```\n")
        with self.assertRaisesRegex(
                G.GovernanceError, "verify_code_scope_report_in_workspace"):
            G.ingest_quality_report(
                self.state_path, path,
                workspace=self.workspace)

    def test_code_drift_does_not_derive_auto_accept(self):
        self.ingest(self.code_block(report_id="VR-CODE-1"),
                    workspace=self.workspace)
        with open(self.app, "w", encoding="utf-8") as fh:
            fh.write("value = 2\n")
        state = G.load_state(self.state_path)
        actions = G.derive_work(state, reason="partial")
        self.assertFalse(any(str(item.get("id") or "").startswith("AUTO-ACCEPT")
                             for item in actions))
        self.assertTrue(any(str(item.get("id") or "").startswith("AUTO-QUALITY")
                            for item in actions))

    def test_g4_evidence_excludes_only_untracked_artifacts(self):
        with open(self.app, "w", encoding="utf-8") as fh:
            fh.write("value = 2\n")
        artifact = os.path.join(self.workspace, "run.log")
        with open(artifact, "w", encoding="utf-8") as fh:
            fh.write("log\n")
        state = G.load_state(self.state_path)
        G.register_evidence(state, "E2", file_path=self.app)
        G.register_evidence(state, "E3", file_path=artifact)
        excludes = CS.safe_evidence_excludes(state, self.workspace)
        self.assertNotIn(os.path.realpath(self.app), excludes)
        self.assertIn(os.path.realpath(artifact), excludes)
        direct = CS.safe_path_excludes([self.app, artifact], self.workspace)
        self.assertNotIn(os.path.realpath(self.app), direct)
        self.assertIn(os.path.realpath(artifact), direct)

    def test_review_snapshot_ignores_tracked_path_in_envelope_excludes(self):
        with open(self.app, "w", encoding="utf-8") as fh:
            fh.write("value = 2\n")
        snap = self.snapshot()
        state = G.load_state(self.state_path)
        result = G._review_code_snapshot_for_execution(
            self.workspace, state, self.state_path, "",
            snap["digest"], [self.app, self.state_path])
        self.assertIn("src/app.py",
                      [entry["path"] for entry in result["files"]])

    def test_tracked_detection_handles_glob_and_head_only(self):
        special = os.path.join(self.workspace, "src", "app[1].py")
        with open(special, "w", encoding="utf-8") as fh:
            fh.write("special = 1\n")
        subprocess.run(["git", "-C", self.workspace, "add", "src/app[1].py"],
                       check=True)
        subprocess.run(["git", "-C", self.workspace, "commit", "-qm", "special"],
                       check=True)
        self.assertNotIn(os.path.realpath(special),
                         CS.safe_path_excludes([special], self.workspace))
        cached = os.path.join(self.workspace, "src", "cached.py")
        with open(cached, "w", encoding="utf-8") as fh:
            fh.write("cached = 1\n")
        subprocess.run(["git", "-C", self.workspace, "add", "src/cached.py"],
                       check=True)
        subprocess.run(["git", "-C", self.workspace, "commit", "-qm", "cached"],
                       check=True)
        subprocess.run(["git", "-C", self.workspace, "rm", "--cached",
                        "src/cached.py"], check=True)
        self.assertNotIn(os.path.realpath(cached),
                         CS.safe_path_excludes([cached], self.workspace))

    def test_doc_report_does_not_require_code_scope(self):
        self.ingest(self.block(report_id="VR-DOC-1"))
        self.accept(["W1", "W2", "R1"])
        self.assertEqual(G.close_task(self.state_path)["verdict"],
                         "pass_with_warnings")

    def test_capture_helper_matches_copilot_snapshot(self):
        with open(self.app, "w", encoding="utf-8") as fh:
            fh.write("value = 2\n")
        proc = F.run_cli("code_scope_capture.py", "--workspace", self.workspace,
                         "--state", self.state_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        snap = self.snapshot()
        self.assertEqual(payload["commit"], snap["git_head"])
        self.assertEqual(payload["code_scope_sha256"], snap["digest"])
        self.assertEqual(payload["dirty"], True)
        subject = {
            "scope": "src/app.py", "commit": payload["commit"],
            "dirty": payload["dirty"],
            "files": list(payload["files_paths"]),
            "code_scope_sha256": payload["code_scope_sha256"],
        }
        self.ingest(self.block(report_id="VR-CODE-1", subject=subject),
                    workspace=self.workspace)
        self.accept(["W1", "W2", "R1"])
        self.assertEqual(G.close_task(self.state_path)["verdict"],
                         "pass_with_warnings")


if __name__ == "__main__":
    unittest.main()

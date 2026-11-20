#!/usr/bin/env python3
"""v6.7 PWW 自动治理：识别、自动登记、用户接受、G2 与原始结论保留。"""
from __future__ import annotations

import hashlib
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
import v6_support as F  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""

MARKER = "<!-- copilot-verify-result/v1 -->"
CHECKS = {"required_tests_executed": True, "required_evidence_present": True,
          "completion_criteria_met": True, "delivery_verifiable": True}


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quality_block(*, report_id="VR-1", verdict="PASS_WITH_WARNINGS",
                  warnings=None, risks=None, unverified=None,
                  acceptance_required=True, acceptance_status=None,
                  checks=None, subject=None, schema=G.QUALITY_SCHEMA,
                  supersedes_report_id=None, resolved_warning_ids=None,
                  resolved_residual_risk_ids=None) -> dict:
    if warnings is None:
        warnings = [{"id": "W1", "text": "边界输入未验证", "severity": "minor"}]
    if risks is None:
        risks = [{"id": "R1", "text": "性能未压测", "severity": "minor"}]
    if acceptance_status is None:
        acceptance_status = "pending" if acceptance_required else "not_required"
    payload = {
        "schema": schema,
        "report_id": report_id,
        "generated_at": "2026-11-15T22:00:00+08:00",
        "verdict": verdict,
        "warnings": warnings,
        "residual_risks": risks,
        "unverified": unverified or [],
        "acceptance_required": acceptance_required,
        "acceptance_status": acceptance_status,
        "checks": CHECKS if checks is None else checks,
        "subject": subject or {"scope": "fixture", "summary": "test fixture"},
    }
    if supersedes_report_id is not None:
        payload["supersedes_report_id"] = supersedes_report_id
    if resolved_warning_ids is not None:
        payload["resolved_warning_ids"] = resolved_warning_ids
    if resolved_residual_risk_ids is not None:
        payload["resolved_residual_risk_ids"] = resolved_residual_risk_ids
    return payload


def report_text(block: dict | None = None, *, marker: bool = True, **kwargs) -> str:
    payload = block if block is not None else quality_block(**kwargs)
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    head = "## 验收报告\n\n### 9. 结论\n原始 verdict：见下\n\n"
    if not marker:
        return head + body + "\n"
    return head + "### 10. 机器可读质量结论\n" + MARKER + "\n```json\n" + body + "\n```\n"


class PwwFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state_path = os.path.join(self.dir, "state.json")
        self.contract_path = os.path.join(self.dir, "contract.md")
        with open(self.contract_path, "w", encoding="utf-8") as fh:
            fh.write(CONTRACT)
        G.start_state(self.state_path, goal="实现计算器", contract=CONTRACT)
        G.plan_state(self.state_path, {"summary": "实现", "actions": [
            {"id": "A1", "title": "实现", "goal": "产出结果",
             "expected_evidence": "结果文件"},
        ]})
        self.evidence_path = os.path.join(self.dir, "result.txt")
        with open(self.evidence_path, "w", encoding="utf-8") as fh:
            fh.write("result\n")
        state = G.load_state(self.state_path)
        G.register_evidence(state, "E1", file_path=self.evidence_path)
        G.save_state(self.state_path, state)
        G.record_action(self.state_path, "A1", "done", result="完成",
                        evidence_ids=["E1"])
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=["E1"])
        self.report_index = 0

    def write_report(self, block: dict | None = None, **kwargs) -> str:
        self.report_index += 1
        path = os.path.join(self.dir, f"report-{self.report_index}.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(report_text(block, **kwargs))
        return path

    def ingest(self, path: str) -> dict:
        return G.ingest_quality_report(self.state_path, path)

    def ingest_cli(self, path: str) -> F.subprocess.CompletedProcess:
        return F.run_cli("copilot_dispatch.py", "report", "--state",
                         self.state_path, "--verify-report", path)

    def accept(self, ids, *, by="user", quote="我明确接受这些风险",
               report_id="") -> dict:
        return G.accept_pww(self.state_path, report_id=report_id, ids=ids,
                            accepted_by=by, quote=quote)

    def gate_codes(self) -> set[str]:
        state = G.load_state(self.state_path)
        return {str(reason.get("code")) for reason in
                G.closure_check(state, task_dir=self.dir)["reasons"]}

    def closure_error(self) -> str:
        with self.assertRaises(G.GovernanceError) as ctx:
            G.close_task(self.state_path)
        return str(ctx.exception)


class PwwIngestTests(PwwFixture):
    def test_plain_pass_needs_no_acceptance_and_closes(self):
        path = self.write_report(verdict="PASS", warnings=[], risks=[],
                                 acceptance_required=False)
        result = self.ingest(path)
        self.assertFalse(result["idempotent"])
        state = G.load_state(self.state_path)
        self.assertIsNone(G._acceptance_criterion(state))
        self.assertEqual(len(state["contract"]["criteria"]), 1)
        self.assertTrue(G.closure_check(state)["passed"])
        closure = G.close_task(self.state_path)
        self.assertEqual(closure["verdict"], "pass")
        self.assertEqual(closure["quality"]["verdict"], "pass")

    def test_pww_ingest_auto_registers_pending_acceptance_condition(self):
        path = self.write_report()
        result = self.ingest(path)
        report = result["report"]
        criterion = result["criterion"]
        self.assertEqual(report["acceptance_status"], "pending")
        self.assertEqual(report["criterion_id"], criterion["criterion_id"])
        self.assertEqual(criterion["status"], "pending")
        self.assertEqual(criterion["acceptance"]["required_ids"], ["W1", "R1"])
        state = G.load_state(self.state_path)
        self.assertEqual(len(state["contract"]["criteria"]), 2)
        self.assertEqual(G._acceptance_criterion(state)["criterion_id"],
                         criterion["criterion_id"])
        self.assertIn("pww_acceptance_pending", self.gate_codes())
        self.assertIn("criterion_pending", self.gate_codes())

    def test_repeat_ingest_is_idempotent(self):
        path = self.write_report()
        first = self.ingest(path)
        second = self.ingest(path)
        third = self.ingest(path)
        self.assertTrue(second["idempotent"])
        self.assertTrue(third["idempotent"])
        self.assertEqual(first["report"]["report_id"], second["report"]["report_id"])
        state = G.load_state(self.state_path)
        self.assertEqual(len(state["quality_reports"]), 1)
        self.assertEqual(len(state["reports"]), 1)
        self.assertEqual(len([c for c in state["contract"]["criteria"]
                              if c.get("acceptance")]), 1)

    def test_same_content_different_file_is_idempotent(self):
        first = self.write_report()
        second = self.write_report()
        self.ingest(first)
        result = self.ingest(second)
        self.assertTrue(result["idempotent"])
        self.assertEqual(len(G.load_state(self.state_path)["quality_reports"]), 1)

    def test_report_id_reuse_for_different_content_is_rejected(self):
        self.ingest(self.write_report(report_id="VR-SAME"))
        changed = self.write_report(report_id="VR-SAME", warnings=[
            {"id": "W1", "text": "另一条警告", "severity": "minor"}])
        with self.assertRaisesRegex(G.GovernanceError, "verify_report_id_reused"):
            self.ingest(changed)

    def test_plain_pass_requires_all_checks_true(self):
        checks = dict(CHECKS)
        checks["completion_criteria_met"] = False
        path = self.write_report(verdict="PASS", warnings=[], risks=[],
                                 acceptance_required=False, checks=checks)
        with self.assertRaisesRegex(G.GovernanceError,
                                    "verify_pww_precondition_failed"):
            self.ingest(path)
        block = quality_block(verdict="PASS", warnings=[], risks=[],
                              acceptance_required=False)
        block.pop("checks")
        with self.assertRaisesRegex(G.GovernanceError, "verify_report_invalid"):
            G.parse_quality_report(report_text(block))

    def test_markerless_pure_json_is_rejected(self):
        path = os.path.join(self.dir, "pure.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(quality_block(), fh, ensure_ascii=False)
        with self.assertRaisesRegex(G.GovernanceError, "verify_report_marker_missing"):
            self.ingest(path)

    def test_generated_at_is_required(self):
        block = quality_block()
        block.pop("generated_at")
        with self.assertRaisesRegex(G.GovernanceError, "generated_at"):
            G.parse_quality_report(report_text(block))

    def test_missing_marker_is_rejected(self):
        path = os.path.join(self.dir, "plain.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("## 结论\nPASS_WITH_WARNINGS\n")
        with self.assertRaisesRegex(G.GovernanceError, "verify_report_marker_missing"):
            self.ingest(path)

    def test_pww_with_unverified_is_rejected(self):
        path = self.write_report(unverified=["真机未验"])
        with self.assertRaisesRegex(G.GovernanceError, "verify_pww_has_unverified"):
            self.ingest(path)
        self.assertEqual(G.load_state(self.state_path)["quality_reports"], [])

    def test_pww_with_blocking_warning_is_rejected(self):
        path = self.write_report(warnings=[
            {"id": "W1", "text": "关键链路失败", "severity": "blocking"}])
        with self.assertRaisesRegex(G.GovernanceError, "verify_pww_blocking_finding"):
            self.ingest(path)

    def test_pww_requires_all_checks_true(self):
        for key in ("required_tests_executed", "required_evidence_present",
                    "completion_criteria_met", "delivery_verifiable"):
            with self.subTest(check=key):
                checks = dict(CHECKS)
                checks[key] = False
                path = self.write_report(report_id=f"VR-{key}", checks=checks)
                with self.assertRaisesRegex(G.GovernanceError,
                                            "verify_pww_precondition_failed"):
                    self.ingest(path)
        self.assertEqual(G.load_state(self.state_path)["quality_reports"], [])

    def test_pww_rejected_when_contract_criteria_unmet(self):
        # 新任务：条件尚未核销时 verify 即使写 PWW 也不能接入。
        state_path = os.path.join(self.dir, "unmet-state.json")
        G.start_state(state_path, goal="未完成", contract=CONTRACT)
        path = self.write_report()
        with self.assertRaisesRegex(G.GovernanceError, "verify_pww_criteria_unmet"):
            G.ingest_quality_report(state_path, path)

    def test_verify_cannot_assert_acceptance(self):
        block = quality_block(acceptance_status="accepted")
        with self.assertRaisesRegex(G.GovernanceError,
                                    "verify_acceptance_forbidden"):
            G.parse_quality_report(report_text(block))

    def test_pass_with_warnings_field_is_rejected(self):
        block = quality_block(verdict="PASS", acceptance_required=False,
                              acceptance_status="not_required")
        with self.assertRaisesRegex(G.GovernanceError, "verify_report_invalid"):
            G.parse_quality_report(report_text(block))

    def test_non_pww_cannot_require_acceptance(self):
        block = quality_block(verdict="FAIL", acceptance_required=True,
                              acceptance_status="pending", warnings=[], risks=[])
        with self.assertRaisesRegex(G.GovernanceError, "verify_report_invalid"):
            G.parse_quality_report(report_text(block))

    def test_report_id_and_subject_are_required(self):
        block = quality_block(report_id="")
        with self.assertRaisesRegex(G.GovernanceError, "verify_report_invalid"):
            G.parse_quality_report(report_text(block))
        block = quality_block()
        block["subject"] = {}
        with self.assertRaisesRegex(G.GovernanceError, "verify_report_invalid"):
            G.parse_quality_report(report_text(block))


class PwwGateTests(PwwFixture):
    def test_accept_requires_quote(self):
        self.ingest(self.write_report())
        with self.assertRaisesRegex(G.GovernanceError, "acceptance_quote_required"):
            self.accept(["W1", "R1"], quote=" ")

    def test_short_quote_is_accepted(self):
        self.ingest(self.write_report())
        result = self.accept(["W1", "R1"], quote="接受")
        self.assertEqual(result["status"], "accepted")

    def test_reopen_invalidates_acceptance_until_reverify(self):
        self.ingest(self.write_report(report_id="VR-1"))
        self.accept(["W1", "R1"])
        first = G.close_task(self.state_path)
        self.assertEqual(first["verdict"], "pass_with_warnings")
        G.reopen_task(self.state_path, "需要补一项验证", authorized_by="user")
        state = G.load_state(self.state_path)
        self.assertFalse(state["closure"]["closed"])
        self.assertEqual(G.quality_summary(state)["acceptance_status"], "stale")
        self.assertIn("pww_reverify_required", self.gate_codes())
        criterion = G._acceptance_criterion(state)
        self.assertEqual(criterion["acceptance"]["status"], "pending")
        with self.assertRaisesRegex(G.GovernanceError, "pww_reverify_required"):
            self.accept(["W1", "R1"])
        with self.assertRaises(G.GovernanceError):
            G.close_task(self.state_path)
        block = quality_block(report_id="VR-2",
                              supersedes_report_id="VR-1")
        block["generated_at"] = "2026-11-15T23:00:00+08:00"
        self.ingest(self.write_report(block))
        self.accept(["W1", "R1"], quote="复验后我再次接受")
        second = G.close_task(self.state_path)
        self.assertEqual(second["verdict"], "pass_with_warnings")
        self.assertEqual(second["quality"]["report_id"], "VR-2")

    def test_reopen_invalidates_plain_pass_binding(self):
        self.ingest(self.write_report(
            quality_block(verdict="PASS", warnings=[], risks=[],
                          acceptance_required=False)))
        first = G.close_task(self.state_path)
        self.assertEqual(first["verdict"], "pass")
        G.reopen_task(self.state_path, "需要补充验证", authorized_by="user")
        state = G.load_state(self.state_path)
        self.assertFalse(G.quality_summary(state)["binding_current"])
        self.assertIn("quality_binding_stale", self.gate_codes())
        with self.assertRaises(G.GovernanceError):
            G.close_task(self.state_path)

    def test_g2_blocks_until_user_accepts_all(self):
        self.ingest(self.write_report())
        self.assertIn("pww_acceptance_pending", self.gate_codes())
        error = self.closure_error()
        self.assertIn("pww_acceptance_pending", error)

    def test_agent_or_verify_cannot_accept(self):
        self.ingest(self.write_report())
        with self.assertRaisesRegex(G.GovernanceError, "acceptance_requires_user"):
            self.accept(["W1", "R1"], by="zcode")
        with self.assertRaisesRegex(G.GovernanceError, "acceptance_requires_user"):
            self.accept(["W1", "R1"], by="verify")
        state = G.load_state(self.state_path)
        criterion = G._acceptance_criterion(state)
        with self.assertRaisesRegex(G.GovernanceError, "PWW 用户接受条件"):
            G.record_criterion(self.state_path, criterion["criterion_id"], "met",
                               evidence_ids=["E1"])
        G.add_decision(self.state_path, "我接受 PWW", source="user")
        self.assertIn("pww_acceptance_pending", self.gate_codes())
        self.assertIn("criterion_pending", self.gate_codes())

    def test_partial_acceptance_keeps_task_incomplete(self):
        self.ingest(self.write_report())
        result = self.accept(["W1"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["pending_ids"], ["R1"])
        self.assertIn("pww_acceptance_partial", self.gate_codes())
        self.assertIn("criterion_pending", self.gate_codes())

    def test_full_acceptance_allows_close_and_preserves_raw_verdict(self):
        path = self.write_report()
        self.ingest(path)
        self.accept(["W1"])
        result = self.accept(["R1"], quote="R1 也接受")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.gate_codes(), set())
        closure = G.close_task(self.state_path)
        self.assertEqual(closure["verdict"], "pass_with_warnings")
        self.assertEqual(closure["quality"]["verdict"], "pass_with_warnings")
        self.assertEqual(closure["quality"]["raw_verdict"], "PASS_WITH_WARNINGS")
        self.assertEqual(closure["quality"]["accepted_ids"], ["W1", "R1"])
        state = G.load_state(self.state_path)
        events = state["quality_reports"][0]["acceptance_history"]
        self.assertEqual([event["status"] for event in events], ["partial", "accepted"])
        self.assertEqual([event["quote"] for event in events],
                         ["我明确接受这些风险", "R1 也接受"])
        criterion = G._acceptance_criterion(state)
        self.assertEqual(criterion["status"], "met")
        self.assertEqual(criterion["evidence_refs"][0]["id"],
                         state["quality_reports"][0]["acceptance_evidence_id"])

    def test_repeat_full_accept_is_idempotent(self):
        self.ingest(self.write_report())
        self.accept(["W1", "R1"])
        state = G.load_state(self.state_path)
        evidence_count = len(state["evidence"])
        result = self.accept(["W1"], quote="再次确认")
        self.assertTrue(result.get("idempotent"))
        state = G.load_state(self.state_path)
        self.assertEqual(len(state["evidence"]), evidence_count)
        self.assertEqual(len(state["quality_reports"][0]["acceptance_history"]), 1)

    def test_warning_change_invalidates_previous_acceptance(self):
        self.ingest(self.write_report(report_id="VR-1"))
        self.accept(["W1", "R1"])
        self.assertEqual(self.gate_codes(), set())
        changed = self.write_report(
            report_id="VR-2", supersedes_report_id="VR-1",
            warnings=[{"id": "W1", "text": "边界输入仍未验证，另加限制",
                       "severity": "minor"}],
            risks=[{"id": "R1", "text": "性能未压测", "severity": "minor"}])
        result = self.ingest(changed)
        self.assertEqual(result["report"]["acceptance_status"], "pending")
        state = G.load_state(self.state_path)
        old = next(r for r in state["quality_reports"] if r["report_id"] == "VR-1")
        self.assertEqual(old["superseded_by"], "VR-2")
        self.assertEqual(old["acceptance_status"], "superseded")
        criterion = G._acceptance_criterion(state)
        self.assertEqual(criterion["status"], "pending")
        self.assertEqual(criterion["acceptance"]["report_id"], "VR-2")
        codes = self.gate_codes()
        self.assertIn("pww_acceptance_pending", codes)
        self.assertIn("criterion_pending", codes)

    def test_target_change_after_acceptance_makes_binding_stale(self):
        self.ingest(self.write_report())
        self.accept(["W1", "R1"])
        G.amend_contract(self.state_path,
                         contract="## 完成条件\n- 产出可复核的结果文件\n- 兼容旧接口\n")
        state = G.load_state(self.state_path)
        self.assertEqual(G.quality_summary(state)["acceptance_status"], "stale")
        self.assertIn("pww_acceptance_stale", self.gate_codes())
        self.assertIn("pww_acceptance_stale", self.closure_error())

    def test_accepting_after_contract_change_is_rejected(self):
        self.ingest(self.write_report())
        G.amend_contract(self.state_path,
                         contract="## 完成条件\n- 产出可复核的结果文件\n- 兼容旧接口\n")
        with self.assertRaisesRegex(G.GovernanceError, "pww_acceptance_stale"):
            self.accept(["W1", "R1"])

    def test_reverify_after_contract_change_refreshes_acceptance(self):
        self.ingest(self.write_report(report_id="VR-1"))
        self.accept(["W1", "R1"])
        G.amend_contract(self.state_path,
                         contract="## 完成条件\n- 产出可复核的结果文件\n- 兼容旧接口\n")
        state = G.load_state(self.state_path)
        acceptance_criterion = G._acceptance_criterion(state)
        self.assertEqual(acceptance_criterion["status"], "pending")
        G.record_criterion(self.state_path, "C2", "met", evidence_ids=["E1"])
        self.assertIn("pww_acceptance_stale", self.gate_codes())

        block = quality_block(report_id="VR-RUN-2",
                              supersedes_report_id="VR-1")
        block["generated_at"] = "2026-11-15T22:30:00+08:00"
        result = self.ingest(self.write_report(block))
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["report"]["report_id"], "VR-RUN-2")
        self.assertEqual(result["report"]["acceptance_status"], "pending")
        self.assertNotIn("pww_acceptance_stale", self.gate_codes())
        self.accept(["W1", "R1"], quote="复验后我仍接受这些风险")
        self.assertEqual(self.gate_codes(), set())
        closure = G.close_task(self.state_path)
        self.assertEqual(closure["verdict"], "pass_with_warnings")
        self.assertEqual(closure["quality"]["report_id"], "VR-RUN-2")

    def test_superseded_report_cannot_be_reingested(self):
        first = self.write_report(report_id="VR-1")
        self.ingest(first)
        later = self.write_report(report_id="VR-2", supersedes_report_id="VR-1",
                                  warnings=[
            {"id": "W1", "text": "已更新的警告", "severity": "minor"}],
            risks=[], resolved_residual_risk_ids=["R1"])
        self.ingest(later)
        with self.assertRaisesRegex(G.GovernanceError, "verify_report_superseded"):
            self.ingest(first)

    def test_new_pass_report_supersedes_pww_and_allows_close(self):
        self.ingest(self.write_report(report_id="VR-PWW"))
        later = self.write_report(report_id="VR-PASS", verdict="PASS",
                                  supersedes_report_id="VR-PWW",
                                  warnings=[], risks=[],
                                  acceptance_required=False,
                                  resolved_warning_ids=["W1"],
                                  resolved_residual_risk_ids=["R1"])
        self.ingest(later)
        state = G.load_state(self.state_path)
        self.assertIsNone(G._acceptance_criterion(state))
        self.assertEqual(len(state["contract"]["criteria"]), 1)
        self.assertEqual(G.quality_summary(state)["verdict"], "pass")
        closure = G.close_task(self.state_path)
        self.assertEqual(closure["verdict"], "pass")
        self.assertEqual(closure["quality"]["report_id"], "VR-PASS")

    def test_fail_report_blocks_closure_and_preserves_fail(self):
        path = self.write_report(verdict="FAIL", warnings=[], risks=[],
                                 acceptance_required=False)
        self.ingest(path)
        state = G.load_state(self.state_path)
        self.assertEqual(G.quality_summary(state)["verdict"], "fail")
        self.assertIn("quality_verdict_not_pass", self.gate_codes())
        error = self.closure_error()
        self.assertIn("quality_verdict_not_pass", error)
        self.assertFalse((G.load_state(self.state_path)["closure"])["closed"])

    def test_gate_is_independent_of_acceptance_criterion(self):
        self.ingest(self.write_report())
        state = G.load_state(self.state_path)
        state["contract"]["criteria"] = [
            c for c in state["contract"]["criteria"]
            if not isinstance(c.get("acceptance"), dict)]
        state["contract"]["criteria_sha256"] = G.criteria_digest(
            state["contract"]["criteria"])
        state["contract_sha256"] = G._contract_fingerprint(
            state["goal"], state["contract"]["done_criteria_text"],
            state["contract"]["criteria"], state["contract"]["test_policy"])
        G.save_state(self.state_path, state)
        self.assertIn("pww_acceptance_pending", self.gate_codes())

    def test_tampered_report_state_is_rejected(self):
        self.ingest(self.write_report())
        state = G.load_state(self.state_path)
        report = state["quality_reports"][0]
        report["warnings"][0]["text"] = "被手工改写的警告"
        G.save_state(self.state_path, state)
        self.assertIn("quality_report_tampered", self.gate_codes())
        with self.assertRaisesRegex(G.GovernanceError, "quality_report_tampered"):
            self.accept(["W1", "R1"])

    def test_deleting_quality_reports_is_detected(self):
        self.ingest(self.write_report())
        state = G.load_state(self.state_path)
        state["quality_reports"] = []
        G.save_state(self.state_path, state)
        self.assertIn("quality_report_state_inconsistent", self.gate_codes())
        self.assertIn("quality_report_state_inconsistent", self.closure_error())

    def test_forged_acceptance_without_evidence_is_rejected(self):
        self.ingest(self.write_report())
        state = G.load_state(self.state_path)
        report = state["quality_reports"][0]
        report["acceptance_status"] = "accepted"
        report["acceptance"] = {
            "status": "accepted", "accepted_ids": ["W1", "R1"],
            "accepted_by": "user", "accepted_at": G.now_iso(),
            "quote": "伪造", "report_id": report["report_id"],
            "binding_sha256": report["binding_sha256"],
        }
        criterion = G._acceptance_criterion(state)
        criterion["status"] = "met"
        criterion["evidence_refs"] = []
        G.save_state(self.state_path, state)
        self.assertIn("pww_acceptance_record_inconsistent", self.gate_codes())
        self.assertIn("pww_acceptance_record_inconsistent", self.closure_error())

    def test_accept_stale_does_not_write_state(self):
        self.ingest(self.write_report())
        G.amend_contract(self.state_path,
                         contract="## 完成条件\n- 产出可复核的结果文件\n- 兼容旧接口\n")
        before = sha256_file(self.state_path)
        with self.assertRaisesRegex(G.GovernanceError, "pww_acceptance_stale"):
            self.accept(["W1", "R1"])
        self.assertEqual(sha256_file(self.state_path), before)

    def test_historical_or_missing_acceptance_fields_never_read_as_accepted(self):
        self.ingest(self.write_report())
        state = G.load_state(self.state_path)
        report = state["quality_reports"][0]
        report["acceptance"] = {}
        report["acceptance_status"] = ""
        report.pop("acceptance_history", None)
        G.save_state(self.state_path, state)
        codes = self.gate_codes()
        self.assertIn("pww_acceptance_pending", codes)
        self.assertIn("criterion_pending", codes)
        summary = G.quality_summary(G.load_state(self.state_path))
        self.assertNotEqual(summary["acceptance_status"], "accepted")

    def test_historical_closed_state_reports_unrecorded_quality(self):
        path = os.path.join(self.dir, "legacy-state.json")
        G.start_state(path, goal="旧任务", contract=CONTRACT)
        G.plan_state(path, {"summary": "实现", "actions": [
            {"id": "A1", "title": "实现", "goal": "产出结果",
             "expected_evidence": "结果文件"}]})
        G.record_action(path, "A1", "done", result="完成")
        state = G.load_state(path)
        G.register_evidence(state, "E1", text="historical")
        G.save_state(path, state)
        G.record_criterion(path, "C1", "met", evidence_ids=["E1"])
        closure = G.close_task(path)
        self.assertEqual(closure["verdict"], "pass")
        self.assertEqual(closure["quality"]["verdict"], "unrecorded")
        summary = G.quality_summary(G.load_state(path))
        self.assertEqual(summary["verdict"], "unrecorded")
        self.assertNotEqual(summary["acceptance_status"], "accepted")


class PwwCliTests(PwwFixture):
    def test_cli_end_to_end_pww_accept_close(self):
        report = self.write_report()
        proc = self.ingest_cli(report)
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
        self.assertIn("已自动登记用户接受条件", proc.stdout)
        self.assertIn("PASS_WITH_WARNINGS", proc.stdout)

        check = F.run_cli("copilot_dispatch.py", "closure-check", "--state",
                          self.state_path)
        self.assertEqual(check.returncode, 1)
        self.assertIn("pww_acceptance_pending", check.stdout)

        close = F.run_cli("copilot_dispatch.py", "closure", "--state",
                          self.state_path, "--close")
        self.assertEqual(close.returncode, 2)
        self.assertIn("pww_acceptance_pending", close.stderr)

        fake = F.run_cli("copilot_dispatch.py", "accept", "--state", self.state_path,
                         "--accept", "W1,R1", "--by", "zcode",
                         "--quote", "代用户接受")
        self.assertEqual(fake.returncode, 2)
        self.assertIn("acceptance_requires_user", fake.stderr)

        accepted = F.run_cli("copilot_dispatch.py", "accept", "--state",
                             self.state_path, "--accept", "W1,R1",
                             "--by", "user", "--quote", "我接受 W1 和 R1")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn("→ accepted", accepted.stdout)

        quality = F.run_cli("copilot_dispatch.py", "quality", "--state",
                            self.state_path, "--json")
        self.assertEqual(quality.returncode, 0)
        payload = json.loads(quality.stdout)
        self.assertEqual(payload["verdict"], "pass_with_warnings")
        self.assertEqual(payload["acceptance_status"], "accepted")

        close = F.run_cli("copilot_dispatch.py", "closure", "--state",
                          self.state_path, "--close")
        self.assertEqual(close.returncode, 0, close.stderr)
        self.assertIn("verdict=pass_with_warnings", close.stdout)
        self.assertIn("PASS_WITH_WARNINGS", close.stdout)
        state = G.load_state(self.state_path)
        self.assertTrue(state["closure"]["closed"])
        self.assertEqual(state["closure"]["verdict"], "pass_with_warnings")
        self.assertEqual(state["closure"]["quality"]["raw_verdict"],
                         "PASS_WITH_WARNINGS")

    def test_cli_report_idempotent_message(self):
        report = self.write_report()
        first = self.ingest_cli(report)
        second = self.ingest_cli(report)
        # 报告登记不结束任务：仍需按续跑判定继续/收口，因此返回 EXIT_CONTINUE。
        self.assertEqual(first.returncode, 10, first.stdout + first.stderr)
        self.assertEqual(second.returncode, 10, second.stdout + second.stderr)
        self.assertIn("幂等", second.stdout)
        state = G.load_state(self.state_path)
        self.assertEqual(len(state["quality_reports"]), 1)

    def test_cli_malformed_report_does_not_change_state(self):
        bad = os.path.join(self.dir, "bad.md")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write(MARKER + "\n```json\n{\"schema\":\"x\"}\n```\n")
        before = sha256_file(self.state_path)
        proc = self.ingest_cli(bad)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("verify_report_invalid", proc.stderr)
        self.assertEqual(sha256_file(self.state_path), before)


class PwwAtomicityTests(PwwFixture):
    def test_ingest_save_failure_leaves_state_unchanged(self):
        report = self.write_report()
        before = sha256_file(self.state_path)
        with mock.patch.object(G, "save_state", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                G.ingest_quality_report(self.state_path, report)
        self.assertEqual(sha256_file(self.state_path), before)
        self.assertEqual(G.load_state(self.state_path)["quality_reports"], [])

    def test_accept_save_failure_leaves_state_unchanged(self):
        self.ingest(self.write_report())
        before = sha256_file(self.state_path)
        with mock.patch.object(G, "save_state", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.accept(["W1", "R1"])
        self.assertEqual(sha256_file(self.state_path), before)
        state = G.load_state(self.state_path)
        self.assertEqual(state["quality_reports"][0]["acceptance_status"], "pending")
        self.assertEqual(G._acceptance_criterion(state)["status"], "pending")


if __name__ == "__main__":
    unittest.main()

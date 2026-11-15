#!/usr/bin/env python3
"""G4 影子取证与持久化测试。"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import g4_attest as A  # noqa: E402
import g4_shadow as SH  # noqa: E402
import native_bridge as NB  # noqa: E402


REVIEW_SHA = "1" * 64
TARGET = "2" * 64
EXEC_SHA = "3" * 64
ATTEMPT_ID = "RA" + "a" * 24


def review(**over) -> dict:
    data = {
        "result_sha256": REVIEW_SHA,
        "review_attempt_id": ATTEMPT_ID,
        "target_sha256": TARGET,
        "review_scope": "full",
        "verdict": "pass",
        "execution_result_sha256": EXEC_SHA,
        "g4_eligible": True,
        "g4_reasons": [],
        "host": {"metadata_path": "/tmp/meta.json",
                 "workspace_root": "/tmp/ws"},
    }
    data.update(over)
    return data


def attempt() -> dict:
    return {"code_snapshot": {"workspace": "/tmp/ws"},
            "review_attempt_id": ATTEMPT_ID,
            "code_scope_sha256": "5" * 64}


def state() -> dict:
    return {"task_id": "T1", "native_results": [{
        "result_sha256": EXEC_SHA,
        "executor": {"agent_id": "agent-exec", "child_session_id": "child-exec"},
    }]}


def incremental_state() -> dict:
    data = state()
    data["native_reviews"] = [{
        "result_sha256": "8" * 64, "g4_eligible": True, "verdict": "pass",
        "task_id": "T1", "contract_sha256": "",
        "review_attempt_id": "RA" + "b" * 24,
        "target_sha256": TARGET,
        "reviewer": {"agent_id": "agent-rev",
                     "child_session_id": "child-rev"},
        "coverage": {"evidence": {}, "actions": {}, "criteria": {}},
    }]
    data["review_attempts"] = [{
        "review_attempt_id": "RA" + "b" * 24, "consumed": True,
        "consumed_by_result_sha256": "8" * 64,
        "code_scope_sha256": "5" * 64,
    }]
    data["g4_attestations"] = [{
        "review_result_sha256": "8" * 64,
        "attestation": {
            "schema_version": A.SCHEMA, "attestation_id": "a" * 64,
            "attempt_id": "RA" + "b" * 24, "target_sha256": TARGET,
            "target_source": "state",
            "code_scope_sha256": "5" * 64, "verdict": "pass",
            "rejections": [], "checks": [{"passed": True}],
            "agent_id": "agent-rev", "child_session_id": "child-rev",
            "parent_session_id": "parent-rev", "evidence_sha256": [],
        },
    }]
    return data


def attestation(eligible=True, codes=()) -> dict:
    return {
        "schema": A.SCHEMA,
        "eligible": eligible,
        "attestation_id": "f" * 64,
        "review_attempt_id": ATTEMPT_ID,
        "target_sha256": TARGET,
        "agent_id": "agent-rev",
        "child_session_id": "child-rev",
        "parent_session_id": "parent-rev",
        "trace_id": "trace",
        "turn_id": "turn",
        "profile": {"tools": ["Read", "Grep", "Glob"]},
        "model_selection": {"modelId": "m"},
        "total_tool_use_count": 2,
        "tool_call_ids": ["c1", "c2"],
        "evidence": [{"id": "E1", "path": "/tmp/ws/e.txt", "sha256": "4" * 64}],
        "code_scope_sha256": "5" * 64,
        "review_verdict": "pass",
        "checks": [{"code": "x", "passed": eligible, "detail": ""}],
        "rejections": [{"code": code, "detail": ""} for code in codes],
        "residual": ["r"],
        "limitations": ["l"],
        "checked_at": "2026-11-15T10:00:00+08:00",
    }


class ShadowBuildTests(unittest.TestCase):
    def setUp(self):
        # 默认假设测试执行证据已校验；专门的负向用例会覆盖该 patch。
        self.te = mock.patch.object(
            SH.TE, "build",
            return_value={"status": "verified", "problems": [],
                          "binding_id": "t" * 64})
        self.te.start()
        self.addCleanup(self.te.stop)

    def test_consistent_pass(self):
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review(), state(), attempt(), state_path="/tmp/s.json")
        self.assertEqual(record["status"], "attested")
        self.assertEqual(record["agreement"], "consistent")
        self.assertTrue(record["new_g4_eligible"])
        self.assertEqual(record["attestation"]["attempt_id"], ATTEMPT_ID)
        self.assertEqual(record["attestation"]["tool_call_count"], 2)
        self.assertEqual(record["attestation"]["residuals"], ["r"])

    def test_new_stricter_records_rejections(self):
        with mock.patch.object(SH.A, "attest",
                               return_value=attestation(False, ["profile_tools"])):
            record = SH.build(review(), state(), attempt(), state_path="/tmp/s.json")
        self.assertEqual(record["agreement"], "new_stricter")
        self.assertEqual(record["difference_category"], "new_stricter")
        self.assertEqual(record["old_verdict"], "pass")
        self.assertEqual(record["new_verdict"], "pass")
        self.assertIn("profile_tools", record["new_reason"])
        self.assertFalse(record["new_g4_eligible"])
        self.assertIn("new_rejection:profile_tools", record["differences"])

    def test_build_passes_current_contract_to_test_execution(self):
        data = state()
        data["contract_sha256"] = "c" * 64
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            SH.build(review(), data, attempt(), state_path="/tmp/s.json")
        kwargs = SH.TE.build.call_args.kwargs
        self.assertEqual(kwargs["expected_contract_sha256"], "c" * 64)

    def test_build_rejects_not_applicable_without_current_policy(self):
        data = state()
        data["contract_sha256"] = "c" * 64
        data["contract"] = {"test_policy": {}}
        data["execution_bindings"] = [{
            "binding_id": "EB" + "b" * 24, "contract_sha256": "c" * 64,
            "requirement_source": "contract", "test_commands": [],
            "contract_report": "", "contract_artifacts": [],
            "contract_test_policy": {"required": False}}]
        review_data = review()
        review_data["execution_binding"] = {"binding_id": "EB" + "b" * 24}
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            with mock.patch.object(SH.TE, "build",
                                   return_value={"status": "not_applicable",
                                                 "problems": [],
                                                 "binding_id": "t" * 64}):
                record = SH.build(review_data, data, attempt(),
                                  state_path="/tmp/s.json")
        self.assertFalse(record["new_g4_eligible"])
        self.assertIn("test_execution:not_applicable_invalid",
                      record["differences"])

    def test_new_looser_records_old_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = os.path.join(tmp, "metadata.json")
            with open(meta, "w", encoding="utf-8") as fh:
                fh.write("{}")
            with mock.patch.object(SH.A, "attest", return_value=attestation()):
                record = SH.build(review(g4_eligible=False,
                                         g4_reasons=["old_reason"],
                                         host={"metadata_path": meta,
                                               "workspace_root": "/tmp/ws"}),
                                  state(), attempt(), state_path="/tmp/s.json")
        self.assertEqual(record["agreement"], "new_looser")
        self.assertEqual(record["difference_category"], "new_looser")
        self.assertEqual(record["old_reason"], ["old_reason"])
        self.assertEqual(record["old_reason_source"], "review.g4_reasons")
        self.assertTrue(record["old_host_reproducible"])
        self.assertIn("old_reason:old_reason", record["differences"])

    def test_new_looser_without_old_host_evidence_is_incomparable(self):
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review(g4_eligible=False,
                                     g4_reasons=["old_reason"],
                                     host={"metadata_path": "/tmp/no-such-meta",
                                           "workspace_root": "/tmp/ws"}),
                              state(), attempt(), state_path="/tmp/s.json")
        self.assertEqual(record["agreement"], "new_looser")
        self.assertEqual(record["difference_category"], "incomparable")
        self.assertFalse(record["old_host_reproducible"])

    def test_execution_binding_is_resolved_and_required(self):
        data = state()
        data["risk"] = "high"
        data["contract"] = {"test_policy": {
            "captured": "2026-11-15T00:00:00+00:00", "required": True,
            "commands": ["npm test"], "report": "", "artifacts": [],
            "require_test_result": True, "require_artifact_integrity": False}}
        bind_id = "EB" + "c" * 24
        data["execution_bindings"] = [{
            "binding_id": bind_id, "nonce": "d" * 32,
            "task_id": "T1", "work_item_id": "A1",
            "workspace": "/tmp/ws", "test_commands": ["npm test"],
            "created_at": "2026-11-15T00:00:00+00:00",
            "consumed": True, "consumed_by_result_sha256": EXEC_SHA}]
        data["native_results"][0]["execution_binding"] = {
            "binding_id": bind_id, "nonce": "d" * 32}
        captured = {}

        def fake(execution, attempt, **kwargs):
            captured.update(kwargs)
            return {"status": "verified", "problems": [],
                    "binding_id": "t" * 64}

        with mock.patch.object(SH.TE, "build", side_effect=fake), \
                mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review(), data, attempt(), state_path="/tmp/s.json")
        self.assertEqual(captured["binding"]["binding_id"], bind_id)
        self.assertTrue(captured["require_binding"])
        self.assertTrue(record["new_g4_eligible"])

    def test_legacy_high_risk_without_contract_policy_is_not_eligible(self):
        data = state()
        data["risk"] = "high"
        bind_id = "EB" + "c" * 24
        data["execution_bindings"] = [{
            "binding_id": bind_id, "nonce": "d" * 32,
            "task_id": "T1", "work_item_id": "A1",
            "workspace": "/tmp/ws", "test_commands": ["npm test"],
            "created_at": "2026-11-15T00:00:00+00:00",
            "consumed": True, "consumed_by_result_sha256": EXEC_SHA}]
        data["native_results"][0]["execution_binding"] = {
            "binding_id": bind_id, "nonce": "d" * 32}
        with mock.patch.object(SH.TE, "build", return_value={
                "status": "verified", "problems": [],
                "binding_id": "t" * 64}), \
                mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review(), data, attempt(), state_path="/tmp/s.json")
        self.assertFalse(record["new_g4_eligible"])
        self.assertIn("test_execution:contract_test_policy_missing",
                      record["differences"])

    def test_attest_error_is_recorded_not_raised(self):
        error = A.AttestError("metadata_field_missing", "parentSessionId")
        with mock.patch.object(SH.A, "attest", side_effect=error):
            record = SH.build(review(), state(), attempt(), state_path="/tmp/s.json")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["agreement"], "unavailable")
        self.assertIsNone(record["new_g4_eligible"])
        self.assertEqual(record["error"]["code"], "metadata_field_missing")

    def test_unexpected_error_is_recorded(self):
        with mock.patch.object(SH.A, "attest", side_effect=TypeError("boom")):
            record = SH.build(review(), state(), attempt(), state_path="/tmp/s.json")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"]["code"], "shadow_internal_error")

    def test_attribute_error_is_contained(self):
        with mock.patch.object(SH.A, "attest", side_effect=AttributeError("bad")):
            record = SH.build(review(), state(), attempt(), state_path="/tmp/s.json")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"]["code"], "shadow_internal_error")

    def test_host_metadata_identity_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta_path = os.path.join(tmp, "metadata.json")
            with open(meta_path, "w", encoding="utf-8") as fh:
                json.dump({"agentId": "agent-other",
                           "childSessionId": "child-other"}, fh)
            review_data = review(host={"metadata_path": meta_path,
                                       "workspace_root": "/tmp/ws"})
            review_data["reviewer"] = {"agent_id": "agent-expected",
                                       "child_session_id": "child-expected"}
            record = SH.build(review_data, state(), attempt(),
                              state_path="/tmp/s.json")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"]["code"], "shadow_metadata_missing")

    def test_unverified_test_execution_blocks_new_g4(self):
        self.te.stop()
        with mock.patch.object(SH.A, "attest",
                               return_value=attestation()), \
                mock.patch.object(SH.TE, "build", return_value={
                    "status": "unverified",
                    "problems": ["test_commands_not_declared"]}):
            record = SH.build(review(), state(), attempt(), state_path="/tmp/s.json")
        self.assertFalse(record["new_g4_eligible"])
        self.assertIn("test_execution:test_commands_not_declared",
                      record["differences"])
        self.assertEqual(record["test_execution"]["status"], "unverified")

    def test_expected_target_is_passed_through(self):
        with mock.patch.object(SH.A, "attest",
                               return_value=attestation()) as patched:
            SH.build(review(), state(), attempt(), state_path="/tmp/s.json",
                     expected_target="9" * 64)
        self.assertEqual(patched.call_args.kwargs["expected_target"], "9" * 64)

    def test_missing_attempt_is_skipped(self):
        record = SH.build(review(review_attempt_id=""), state(), None,
                          state_path="/tmp/s.json")
        self.assertEqual(record["status"], "skipped")
        self.assertEqual(record["agreement"], "not_applicable")
        self.assertIsNone(record["attestation"])

    def test_foreign_attempt_is_failed(self):
        record = SH.build(review(), state(), None, state_path="/tmp/s.json")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"]["code"], "shadow_attempt_missing")

    def test_attest_receives_attempt_dict_and_no_marker(self):
        with mock.patch.object(SH.A, "attest",
                               return_value=attestation()) as patched:
            SH.build(review(), state(), attempt(), state_path="/tmp/s.json")
        kwargs = patched.call_args.kwargs
        self.assertIsInstance(kwargs["attempt"], dict)
        self.assertEqual(kwargs["consume_marker"], "")
        self.assertEqual(kwargs["executor_agent"], "agent-exec")
        self.assertEqual(kwargs["executor_session"], "child-exec")

    def test_incremental_missing_baseline_is_rejected(self):
        data = state()
        review_data = review(review_scope="incremental",
                             based_on_review_sha256="8" * 64,
                             evidence_manifest=["E2"])
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review_data, data, attempt(), state_path="/tmp/s.json")
        self.assertFalse(record["new_g4_eligible"])
        self.assertEqual(record["agreement"], "new_stricter")
        self.assertIn("incremental:baseline_missing", record["differences"])
        self.assertEqual(record["incremental"]["problems"], ["baseline_missing"])

    def test_incremental_valid_baseline_keeps_eligibility(self):
        data = incremental_state()
        data["evidence"] = [{"id": "E2", "sha256": "a" * 64}]
        review_data = review(review_scope="incremental",
                             based_on_review_sha256="8" * 64,
                             evidence_manifest=["E2"])
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review_data, data, attempt(), state_path="/tmp/s.json")
        self.assertTrue(record["new_g4_eligible"], record["incremental"])
        self.assertEqual(record["incremental"]["problems"], [])

    def test_incremental_unconsumed_shadow_baseline_is_rejected(self):
        # 影子 attestation 不能替代一次性消费记录：未消费基线一律要求完整复核。
        data = incremental_state()
        data["native_reviews"][0]["g4_eligible"] = False
        data["review_attempts"][0]["consumed"] = False
        data["g4_attestations"][0].update({"status": "attested",
                                           "new_g4_eligible": True})
        review_data = review(review_scope="incremental",
                             based_on_review_sha256="8" * 64,
                             evidence_manifest=[])
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review_data, data, attempt(), state_path="/tmp/s.json")
        self.assertFalse(record["new_g4_eligible"])
        self.assertIn("baseline_attempt_not_consumed",
                      record["incremental"]["problems"])
        self.assertIn("incremental:baseline_attempt_not_consumed",
                      record["differences"])

    def test_incremental_current_work_item_action_change_is_allowed(self):
        data = incremental_state()
        action = {"id": "A1", "status": "done", "result": "done",
                  "evidence_ids": ["E1"]}
        data["actions"] = [action]
        data["native_reviews"][0]["coverage"]["actions"] = {"A1": "stale"}
        review_data = review(review_scope="incremental",
                             based_on_review_sha256="8" * 64,
                             work_item_id="A1", evidence_manifest=[])
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review_data, data, attempt(), state_path="/tmp/s.json")
        self.assertEqual(record["incremental"]["problems"], [],
                         record["incremental"])
        self.assertTrue(record["new_g4_eligible"])

    def test_incremental_other_work_item_change_is_rejected(self):
        data = incremental_state()
        data["actions"] = [{"id": "A2", "status": "done", "result": "x",
                            "evidence_ids": []}]
        data["native_reviews"][0]["coverage"]["actions"] = {"A2": "stale"}
        review_data = review(review_scope="incremental",
                             based_on_review_sha256="8" * 64,
                             work_item_id="A1", evidence_manifest=[])
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review_data, data, attempt(), state_path="/tmp/s.json")
        self.assertFalse(record["new_g4_eligible"])
        self.assertIn("incremental_actions_changed",
                      record["incremental"]["problems"])

    def test_incremental_baseline_attestation_missing_is_rejected(self):
        data = incremental_state()
        data["g4_attestations"] = []
        review_data = review(review_scope="incremental",
                             based_on_review_sha256="8" * 64)
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review_data, data, attempt(), state_path="/tmp/s.json")
        self.assertFalse(record["new_g4_eligible"])
        self.assertIn("baseline_attestation_missing",
                      record["incremental"]["problems"])

    def test_built_record_can_become_incremental_baseline(self):
        # 真实链路：SH.build 生成的摘要必须能重新作为增量基线（B1 回归）。
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            built = SH.build(review(result_sha256="8" * 64), state(), attempt(),
                             state_path="/tmp/s.json", expected_target=TARGET)
        self.assertTrue(built["new_g4_eligible"], built.get("differences"))
        self.assertEqual(built["attestation"]["target_source"], "state")
        data = incremental_state()
        data["native_reviews"][0]["review_attempt_id"] = ATTEMPT_ID
        data["review_attempts"][0]["review_attempt_id"] = ATTEMPT_ID
        data["g4_attestations"][0]["attestation"] = built["attestation"]
        data["evidence"] = [{"id": "E2", "sha256": "a" * 64}]
        review_data = review(review_scope="incremental",
                             based_on_review_sha256="8" * 64,
                             evidence_manifest=["E2"])
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review_data, data, attempt(), state_path="/tmp/s.json",
                              expected_target=TARGET)
        self.assertEqual(record["incremental"]["problems"], [],
                         record["incremental"])
        self.assertTrue(record["new_g4_eligible"])

    def test_incremental_baseline_target_self_reported_is_rejected(self):
        data = incremental_state()
        data["g4_attestations"][0]["attestation"]["target_source"] = "envelope"
        data["evidence"] = [{"id": "E2", "sha256": "a" * 64}]
        review_data = review(review_scope="incremental",
                             based_on_review_sha256="8" * 64,
                             evidence_manifest=["E2"])
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review_data, data, attempt(), state_path="/tmp/s.json")
        self.assertFalse(record["new_g4_eligible"])
        self.assertIn("baseline_attestation_target_self_reported",
                      record["incremental"]["problems"])

    def test_incremental_baseline_attestation_tamper_is_rejected(self):
        data = incremental_state()
        data["g4_attestations"][0]["attestation"]["target_sha256"] = "9" * 64
        review_data = review(review_scope="incremental",
                             based_on_review_sha256="8" * 64)
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review_data, data, attempt(), state_path="/tmp/s.json")
        self.assertFalse(record["new_g4_eligible"])
        self.assertIn("baseline_attestation_target_mismatch",
                      record["incremental"]["problems"])

    def test_incremental_baseline_not_consumed_is_rejected(self):
        data = incremental_state()
        data["review_attempts"][0]["consumed"] = False
        review_data = review(review_scope="incremental",
                             based_on_review_sha256="8" * 64)
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review_data, data, attempt(), state_path="/tmp/s.json")
        self.assertFalse(record["new_g4_eligible"])
        self.assertIn("baseline_attempt_not_consumed",
                      record["incremental"]["problems"])

    def test_incremental_code_change_is_rejected(self):
        data = incremental_state()
        review_data = review(review_scope="incremental",
                             based_on_review_sha256="8" * 64)
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review_data, data,
                              attempt() | {"code_scope_sha256": "6" * 64},
                              state_path="/tmp/s.json")
        self.assertFalse(record["new_g4_eligible"])
        self.assertIn("incremental_code_changed",
                      record["incremental"]["problems"])

    def test_incremental_manifest_unknown_id_is_rejected(self):
        data = incremental_state()
        review_data = review(review_scope="incremental",
                             based_on_review_sha256="8" * 64,
                             evidence_manifest=["E2"])
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review_data, data, attempt(), state_path="/tmp/s.json")
        self.assertFalse(record["new_g4_eligible"])
        self.assertIn("incremental_manifest_unknown",
                      record["incremental"]["problems"])

    def test_incremental_missing_changed_evidence_is_rejected(self):
        data = incremental_state()
        data["evidence"] = [{"id": "E2", "sha256": "a" * 64}]
        review_data = review(review_scope="incremental",
                             based_on_review_sha256="8" * 64,
                             evidence_manifest=[])
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review_data, data, attempt(), state_path="/tmp/s.json")
        self.assertFalse(record["new_g4_eligible"])
        self.assertIn("incremental_manifest_incomplete",
                      record["incremental"]["problems"])

    def test_incremental_manifest_superset_of_changes_is_allowed(self):
        data = incremental_state()
        data["evidence"] = [{"id": "E1", "sha256": "a" * 64},
                            {"id": "E2", "sha256": "b" * 64}]
        data["native_reviews"][0]["coverage"]["evidence"] = {
            "E1": "a" * 64 + ":live"}
        review_data = review(review_scope="incremental",
                             based_on_review_sha256="8" * 64,
                             evidence_manifest=["E1", "E2"])
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review_data, data, attempt(), state_path="/tmp/s.json")
        self.assertEqual(record["incremental"]["problems"], [],
                         record["incremental"])
        self.assertTrue(record["new_g4_eligible"])

    def test_incremental_duplicate_attestation_is_rejected(self):
        data = incremental_state()
        data["g4_attestations"].append(data["g4_attestations"][0])
        review_data = review(review_scope="incremental",
                             based_on_review_sha256="8" * 64)
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review_data, data, attempt(), state_path="/tmp/s.json")
        self.assertFalse(record["new_g4_eligible"])
        self.assertIn("baseline_attestation_duplicate",
                      record["incremental"]["problems"])

    def test_missing_target_does_not_self_certify(self):
        with mock.patch.object(SH.A, "attest",
                               return_value=attestation()) as patched:
            SH.build(review(target_sha256=""), state(),
                     attempt() | {"target_sha256": TARGET},
                     state_path="/tmp/s.json")
        self.assertEqual(patched.call_args.kwargs["expected_target"], "")

    def test_malformed_g4_reasons_does_not_raise(self):
        record = SH.build(review(g4_reasons=5), state(), attempt(),
                          state_path="/tmp/s.json")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["old_g4_reasons"], [])

    def test_rollout_missing_difference_is_incomparable(self):
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review(g4_eligible=False,
                                     g4_reasons=["review_rollout_missing"]),
                              state(), attempt(), state_path="/tmp/s.json")
        self.assertEqual(record["agreement"], "new_looser")
        self.assertEqual(record["difference_category"], "incomparable")
        self.assertEqual(record["evidence_summary"]["old"]["reasons"],
                         ["review_rollout_missing"])

    def test_test_binding_records_commands_and_evidence(self):
        data = state()
        data["native_results"][0]["evidence"] = [
            {"id": "E1", "file": "/tmp/ws/e.txt", "sha256": "4" * 64}]
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            record = SH.build(review(commands_run=["pytest -q"]), data,
                              attempt(), state_path="/tmp/s.json")
        binding = record["test_binding"]
        self.assertEqual(binding["commands_run"], ["pytest -q"])
        self.assertEqual(binding["execution_evidence"][0]["id"], "E1")
        self.assertTrue(binding["exit_code_bound"])
        self.assertEqual(binding["limitations"], [])

    def test_shadow_id_is_deterministic(self):
        with mock.patch.object(SH.A, "attest", return_value=attestation()):
            first = SH.build(review(), state(), attempt(), state_path="/tmp/s.json")
            second = SH.build(review(), state(), attempt(), state_path="/tmp/s.json")
        self.assertEqual(first["shadow_id"], second["shadow_id"])
        self.assertNotEqual(first["shadow_id"],
                            SH.build(review(result_sha256="9" * 64), state(),
                                     attempt(), state_path="/tmp/s.json")["shadow_id"])


class ShadowAppendTests(unittest.TestCase):
    def test_append_is_idempotent(self):
        data = {"g4_attestations": []}
        record = {"shadow_id": "s1", "status": "attested"}
        SH.append_attestation(data, record)
        SH.append_attestation(data, record)
        self.assertEqual(len(data["g4_attestations"]), 1)

    def test_append_keeps_distinct_records(self):
        data = {"g4_attestations": []}
        SH.append_attestation(data, {"shadow_id": "s1"})
        SH.append_attestation(data, {"shadow_id": "s2"})
        self.assertEqual(len(data["g4_attestations"]), 2)




# ── 与真实 ingest 的集成（使用现有假宿主夹具） ────────────────────────────
import governance as G  # noqa: E402
import native_bridge as NB  # noqa: E402
import copilot_auto as AC  # noqa: E402
import v6_support as F  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""


class ShadowIngestIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="目标", contract=CONTRACT,
                      execution_backend="native", risk="high",
                      test_policy={"required": False})
        G.plan_state(self.state_path, {"summary": "实现", "actions": [
            {"id": "A1", "title": "实现", "goal": "产出结果",
             "expected_evidence": "结果文件"}]})
        state = G.load_state(self.state_path)
        self.dispatch = NB.build_dispatch(state, G.ready_actions(state))
        state["native_dispatch"] = self.dispatch
        G.save_state(self.state_path, state)
        self.evidence = os.path.join(self.dir, "result.txt")
        with open(self.evidence, "w", encoding="utf-8") as fh:
            fh.write("result\n")
        item = self.dispatch["actions"][0]
        self.execution = {
            "schema": NB.RESULT_SCHEMA,
            "task_id": state["task_id"],
            "contract_sha256": state["contract_sha256"],
            "plan_id": self.dispatch["plan_id"],
            "plan_sha256": self.dispatch["plan_sha256"],
            "iteration_id": self.dispatch["iteration_id"],
            "dispatch_id": item["dispatch_id"],
            "idempotency_key": item["idempotency_key"],
            "work_item_id": "A1",
            "status": "done",
            "summary": "完成",
            "result": "实际结果",
            "evidence": [{"id": "E1", "file": self.evidence,
                          "sha256": NB.sha256_file(self.evidence),
                          "kind": "file"}],
            "executor": {"kind": "subagent", "agent_id": "agent-exec",
                         "child_session_id": "child-exec",
                         "profile_id": "executor", "model_reported": "m"},
            "timing": {"duration_ms": 10},
            "usage": {"total_tokens": 10},
        }
        self.execution["result_sha256"] = NB.envelope_sha256(self.execution)
        AC._ingest_execution(self.state_path, self.execution)
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=["E1"])
        self.state = G.load_state(self.state_path)

    def _attempt(self) -> dict:
        return G.create_review_attempt(
            self.state_path, work_item_id="A1",
            execution_result_sha256=str(self.execution["result_sha256"]),
            workspace=self.dir)

    def _envelope(self, attempt: dict, **overrides) -> dict:
        state = G.load_state(self.state_path)
        host = F.make_fake_review_host(
            self.dir, execution=self.execution, dispatch=self.dispatch,
            evidence=self.evidence, task_id=state["task_id"],
            attempt_id=attempt["review_attempt_id"],
            session_created_at=attempt["created_at"],
            prompt=f"复核 {attempt['review_attempt_id']}")
        env = {
            "schema": NB.REVIEW_SCHEMA,
            "task_id": state["task_id"],
            "contract_sha256": state["contract_sha256"],
            "plan_id": self.dispatch["plan_id"],
            "plan_sha256": self.dispatch["plan_sha256"],
            "dispatch_id": self.execution["dispatch_id"],
            "work_item_id": "A1",
            "execution_result_sha256": self.execution["result_sha256"],
            "target_sha256": attempt["target_sha256"],
            "review_attempt_id": attempt["review_attempt_id"],
            "review_attempt_created_at": attempt["created_at"],
            "review_scope": attempt.get("review_scope") or "full",
            "based_on_review_sha256": attempt.get("based_on_review_sha256") or "",
            "evidence_manifest": list(attempt.get("evidence_manifest") or []),
            "reviewer": {"kind": "subagent", "agent_id": "agent-review",
                         "child_session_id": "child-review",
                         "profile_id": "reviewer", "model_reported": "m"},
            "host": host,
            "verdict": "pass",
            "findings": [],
            "files_inspected": [self.evidence],
            "commands_run": [],
            "changed_files": [],
            "unverified": [],
            "residual_risks": [],
            "g4_eligible": True,
        }
        env.update(overrides)
        env["result_sha256"] = NB.envelope_sha256(env)
        return env

    def test_ingest_without_executor_host_is_not_formal_g4(self):
        attempt = self._attempt()
        env = self._envelope(attempt)
        AC._ingest_review(self.state_path, env)
        state = G.load_state(self.state_path)
        self.assertEqual(len(state["native_reviews"]), 1)
        # 夹具没有 Executor 宿主记录/绑定：新链不产生正式 G4。
        self.assertFalse(state["native_reviews"][0]["g4_eligible"])
        self.assertEqual(state["native_reviews"][0].get("g4_source"), "")
        self.assertEqual(len(state["g4_attestations"]), 1)
        shadow = state["g4_attestations"][0]
        self.assertEqual(shadow["review_result_sha256"], env["result_sha256"])
        self.assertTrue(shadow["old_g4_eligible"])
        self.assertFalse(shadow["new_g4_eligible"])
        self.assertEqual(shadow["status"], "attested")
        self.assertEqual(shadow["agreement"], "new_stricter")
        self.assertIsNone(shadow["error"])
        self.assertTrue(shadow["differences"])
        consumed = G.find_review_attempt(state, attempt["review_attempt_id"])
        self.assertFalse(consumed["consumed"])

    def test_ingest_passes_host_db_to_shadow(self):
        attempt = self._attempt()
        env = self._envelope(attempt)
        env["host"]["host_db"] = "/tmp/custom-host.sqlite"
        env["result_sha256"] = NB.envelope_sha256(env)
        with mock.patch.object(AC.SH, "build", wraps=SH.build) as patched:
            AC._ingest_review(self.state_path, env)
        self.assertEqual(patched.call_args.kwargs["db"], "/tmp/custom-host.sqlite")

    def test_ingest_passes_current_final_target(self):
        attempt = self._attempt()
        env = self._envelope(attempt)
        before = G.final_evidence_digest(G.load_state(self.state_path))
        with mock.patch.object(AC.SH, "build", wraps=SH.build) as patched:
            AC._ingest_review(self.state_path, env)
        self.assertEqual(patched.call_args.kwargs["expected_target"], before)

    def test_reingest_is_idempotent_for_shadow(self):
        attempt = self._attempt()
        env = self._envelope(attempt)
        AC._ingest_review(self.state_path, env)
        AC._ingest_review(self.state_path, env)
        state = G.load_state(self.state_path)
        self.assertEqual(len(state["g4_attestations"]), 1)

    def test_malformed_attestation_field_does_not_block_ingest(self):
        attempt = self._attempt()
        env = self._envelope(attempt)
        state = G.load_state(self.state_path)
        state["g4_attestations"] = None
        G.save_state(self.state_path, state)
        AC._ingest_review(self.state_path, env)
        state = G.load_state(self.state_path)
        self.assertEqual(len(state["native_reviews"]), 1)
        self.assertEqual(len(state["g4_attestations"]), 1)
        self.assertTrue(any(item.get("event") == "g4_shadow_field_repaired"
                            for item in state.get("history") or []))

    def test_save_failure_leaves_no_partial_consumption(self):
        attempt = self._attempt()
        env = self._envelope(attempt)
        before = G.load_state(self.state_path)
        with mock.patch.object(AC.G, "save_state",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                AC._ingest_review(self.state_path, env)
        after = G.load_state(self.state_path)
        self.assertEqual(len(after["native_reviews"]), 0)
        self.assertFalse(G.find_review_attempt(
            after, attempt["review_attempt_id"])["consumed"])
        self.assertEqual(after["save_seq"], before["save_seq"])

    def test_ineligible_review_does_not_consume_attempt(self):
        attempt = self._attempt()
        env = self._envelope(attempt, g4_eligible=False,
                             g4_reasons=["risk_not_high"])
        AC._ingest_review(self.state_path, env)
        state = G.load_state(self.state_path)
        self.assertFalse(G.find_review_attempt(
            state, attempt["review_attempt_id"])["consumed"])
        self.assertEqual(len(state["g4_attestations"]), 1)
        self.assertEqual(state["g4_attestations"][0]["agreement"],
                         "consistent")



class ExecutionAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="目标", contract=CONTRACT,
                      execution_backend="native", risk="high",
                      test_policy={"required": False})
        G.plan_state(self.state_path, {"summary": "实现", "actions": [
            {"id": "A1", "title": "实现", "goal": "产出结果",
             "expected_evidence": "结果文件"}]})
        state = G.load_state(self.state_path)
        self.dispatch = NB.build_dispatch(state, G.ready_actions(state))
        state["native_dispatch"] = self.dispatch
        G.save_state(self.state_path, state)
        self.evidence = os.path.join(self.dir, "result.txt")
        with open(self.evidence, "w", encoding="utf-8") as fh:
            fh.write("result\n")
        item = self.dispatch["actions"][0]
        self.execution = {
            "schema": NB.RESULT_SCHEMA,
            "task_id": state["task_id"],
            "contract_sha256": state["contract_sha256"],
            "plan_id": self.dispatch["plan_id"],
            "plan_sha256": self.dispatch["plan_sha256"],
            "iteration_id": self.dispatch["iteration_id"],
            "dispatch_id": item["dispatch_id"],
            "idempotency_key": item["idempotency_key"],
            "work_item_id": "A1",
            "status": "done",
            "summary": "完成",
            "result": "实际结果",
            "evidence": [{"id": "E1", "file": self.evidence,
                          "sha256": NB.sha256_file(self.evidence),
                          "kind": "file"}],
            "executor": {"kind": "subagent", "agent_id": "agent-exec",
                         "child_session_id": "child-exec",
                         "profile_id": "executor", "model_reported": "m"},
            "timing": {"duration_ms": 10},
            "usage": {"total_tokens": 10},
        }
        self.execution["result_sha256"] = NB.envelope_sha256(self.execution)

    def test_execution_save_failure_leaves_no_partial_state(self):
        before = G.load_state(self.state_path)
        with mock.patch.object(AC.G, "save_state",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                AC._ingest_execution(self.state_path, self.execution)
        after = G.load_state(self.state_path)
        self.assertEqual(after["save_seq"], before["save_seq"])
        self.assertEqual([a["status"] for a in after["actions"]], ["pending"])
        self.assertEqual(after.get("evidence") or [], [])
        self.assertEqual(after.get("native_results") or [], [])


class RequirementPolicyTests(unittest.TestCase):
    def test_contract_policy_requires_binding_even_for_normal_risk(self):
        state = {"risk": "normal",
                 "contract": {"test_policy": {"captured": "now"}}}
        self.assertTrue(SH._require_binding(state))

    def test_old_task_without_policy_keeps_binding_optional(self):
        self.assertFalse(SH._require_binding({"risk": "normal",
                                              "contract": {}}))
        self.assertTrue(SH._require_binding({"risk": "high", "contract": {}}))


class TestExecutionAcceptanceTests(unittest.TestCase):
    def test_contract_no_test_not_applicable_is_accepted(self):
        policy = {"required": False, "commands": [], "report": "",
                  "artifacts": [], "require_test_result": False,
                  "require_artifact_integrity": False}
        state = {"contract_sha256": "c" * 64,
                 "contract": {"test_policy": {**policy, "captured": "now"}}}
        binding = {"requirement_source": "contract", "test_commands": [],
                   "contract_sha256": "c" * 64,
                   "contract_report": "", "contract_artifacts": [],
                   "contract_test_policy": policy}
        self.assertTrue(SH._test_execution_acceptable(
            {"status": "not_applicable"}, binding, state))

    def test_contract_source_without_current_policy_is_rejected(self):
        binding = {"requirement_source": "contract", "test_commands": [],
                   "contract_sha256": "c" * 64,
                   "contract_report": "", "contract_artifacts": [],
                   "contract_test_policy": {"required": False}}
        self.assertFalse(SH._test_execution_acceptable(
            {"status": "not_applicable"}, binding,
            {"contract_sha256": "c" * 64, "contract": {"test_policy": {}}}))

    def test_not_applicable_without_contract_policy_is_rejected(self):
        self.assertFalse(SH._test_execution_acceptable(
            {"status": "not_applicable"}, {"test_commands": []}))

    def test_verified_is_always_accepted(self):
        self.assertTrue(SH._test_execution_acceptable(
            {"status": "verified"}, None))


class CandidateFinalizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state_path = os.path.join(self.dir, "state.json")
        self.output = os.path.join(self.dir, "output.txt")
        self.raw = (b'{"schema":"copilot-native-review/v2","verdict":"pass",'
                    b'"findings":[],"unverified":[],"changed_files":[]}')
        with open(self.output, "wb") as fh:
            fh.write(self.raw)
        self.sha = __import__("hashlib").sha256(self.raw).hexdigest()
        self.state = {
            "task_id": "T1", "contract_sha256": "c" * 64, "risk": "high",
            "execution_bindings": [{
                "binding_id": "EB" + "b" * 24, "nonce": "a" * 32,
                "task_id": "T1", "work_item_id": "A1",
                "contract_sha256": "c" * 64,
                "workspace": self.dir, "consumed": True,
                "consumed_by_result_sha256": "e" * 64}],
            "review_attempts": [{
                "review_attempt_id": "RA" + "a" * 24, "consumed": False,
                "consumed_by_result_sha256": ""}],
        }
        self.normalized = {
            "review_attempt_id": "RA" + "a" * 24,
            "verdict": "pass", "findings": [], "unverified": [],
            "changed_files": [],
            "host": {"output_file": self.output, "output_sha256": self.sha},
            "reviewer": {"child_session_id": "child"},
        }
        self.envelope = {"verdict": "pass", "findings": [],
                         "unverified": [], "changed_files": []}
        self.execution = {"result_sha256": "e" * 64}
        self.shadow = {
            "status": "attested", "new_g4_eligible": True,
            "attestation": {"output_sha256": self.sha},
            "test_execution": {"status": "verified",
                               "execution_binding_id": "EB" + "b" * 24},
        }

    def test_candidate_consumes_attempt_and_persists_raw_output(self):
        candidate = AC._finalize_candidate(
            self.state_path, self.state, self.normalized, self.envelope,
            self.shadow, self.execution, "1" * 64)
        self.assertTrue(candidate["eligible"], candidate["reasons"])
        attempt = self.state["review_attempts"][0]
        self.assertEqual(attempt["candidate_consumed_by_result_sha256"],
                         "1" * 64)
        raw = self.shadow["attestation"]["raw_output"]
        self.assertEqual(raw["sha256"], self.sha)
        with open(raw["path"], "rb") as fh:
            self.assertEqual(fh.read(), self.raw)

    def test_envelope_host_verdict_mismatch_blocks_candidate(self):
        # 宿主原文是 pass，信封却写成 partial：候选必须拒绝。
        self.envelope["verdict"] = "partial"
        candidate = AC._finalize_candidate(
            self.state_path, self.state, self.normalized, self.envelope,
            self.shadow, self.execution, "1" * 64)
        self.assertFalse(candidate["eligible"])
        self.assertIn("envelope_host_field_mismatch:verdict",
                      candidate["reasons"])
        self.assertNotIn("candidate_consumed_by_result_sha256",
                         self.state["review_attempts"][0])

    def test_binding_contract_changed_blocks_candidate(self):
        self.state["contract_sha256"] = "d" * 64
        candidate = AC._finalize_candidate(
            self.state_path, self.state, self.normalized, self.envelope,
            self.shadow, self.execution, "1" * 64)
        self.assertFalse(candidate["eligible"])
        self.assertIn("execution_binding_contract_changed",
                      candidate["reasons"])

    def test_failed_candidate_does_not_persist_raw_output(self):
        self.state["execution_bindings"][0][
            "consumed_by_result_sha256"] = "3" * 64
        candidate = AC._finalize_candidate(
            self.state_path, self.state, self.normalized, self.envelope,
            self.shadow, self.execution, "1" * 64)
        self.assertFalse(candidate["eligible"])
        self.assertNotIn("raw_output", self.shadow["attestation"])
        self.assertFalse(os.path.isdir(
            os.path.join(self.dir, "review-outputs")))

    def _set_output(self, payload: dict) -> None:
        self.raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with open(self.output, "wb") as fh:
            fh.write(self.raw)
        self.sha = hashlib.sha256(self.raw).hexdigest()
        self.normalized["host"]["output_sha256"] = self.sha
        self.shadow["attestation"]["output_sha256"] = self.sha

    def test_normalized_findings_do_not_block_candidate(self):
        # 真实链路里 findings 会被 NB 补齐 id/root_cause/... 并小写 severity；
        # 候选只能与信封原始字段比对，不能与规范化结构比对。
        finding = {"severity": "minor", "summary": "小问题",
                   "file": "src/a.py", "evidence": "第 3 行"}
        self._set_output({"schema": "copilot-native-review/v2",
                          "verdict": "PASS", "findings": [finding],
                          "unverified": [], "changed_files": []})
        self.envelope = {"verdict": "PASS", "findings": [finding],
                         "unverified": [], "changed_files": []}
        self.normalized["verdict"] = "pass"
        # 用真实 bridge 归一化（补齐元数据并小写 severity），而不是手造结构。
        self.normalized["findings"] = NB._validate_findings(
            [finding], check_files=False)
        candidate = AC._finalize_candidate(
            self.state_path, self.state, self.normalized, self.envelope,
            self.shadow, self.execution, "1" * 64)
        self.assertTrue(candidate["eligible"], candidate["reasons"])

    def test_candidate_does_not_touch_formal_fields(self):
        candidate = AC._finalize_candidate(
            self.state_path, self.state, self.normalized, self.envelope,
            self.shadow, self.execution, "1" * 64)
        self.assertTrue(candidate["eligible"], candidate["reasons"])
        attempt = self.state["review_attempts"][0]
        self.assertFalse(attempt.get("consumed"))
        self.assertEqual(attempt.get("consumed_by_result_sha256"), "")
        self.assertNotIn("native_reviews", self.state)
        self.assertNotIn("g4_eligible", candidate)

    def test_contract_no_test_not_applicable_is_eligible(self):
        policy = {"captured": "2026-11-15T00:00:00+08:00",
                  "source": "contract", "required": False, "commands": [],
                  "report": "", "artifacts": [],
                  "require_test_result": False,
                  "require_artifact_integrity": False}
        self.state["contract"] = {"test_policy": policy}
        self.state["execution_bindings"][0].update({
            "requirement_source": "contract",
            "contract_test_policy": {key: policy.get(key) for key in
                                     ("required", "commands", "report",
                                      "artifacts", "require_test_result",
                                      "require_artifact_integrity")},
            "contract_report": "", "contract_artifacts": [],
            "test_commands": []})
        self.shadow["test_execution"]["status"] = "not_applicable"
        candidate = AC._finalize_candidate(
            self.state_path, self.state, self.normalized, self.envelope,
            self.shadow, self.execution, "1" * 64)
        self.assertTrue(candidate["eligible"], candidate["reasons"])

    def test_not_applicable_with_contract_artifacts_is_rejected(self):
        self.state["execution_bindings"][0].update({
            "requirement_source": "contract",
            "contract_test_policy": {"required": False},
            "contract_artifacts": ["reports/junit.xml"],
            "contract_report": "", "test_commands": []})
        self.shadow["test_execution"]["status"] = "not_applicable"
        candidate = AC._finalize_candidate(
            self.state_path, self.state, self.normalized, self.envelope,
            self.shadow, self.execution, "1" * 64)
        self.assertFalse(candidate["eligible"])
        self.assertIn("test_execution_not_applicable_invalid",
                      candidate["reasons"])

    def test_not_applicable_without_contract_policy_is_rejected(self):
        self.shadow["test_execution"]["status"] = "not_applicable"
        candidate = AC._finalize_candidate(
            self.state_path, self.state, self.normalized, self.envelope,
            self.shadow, self.execution, "1" * 64)
        self.assertFalse(candidate["eligible"])
        self.assertIn("test_execution_not_applicable_invalid",
                      candidate["reasons"])

    def test_missing_raw_output_blocks_candidate(self):
        os.remove(self.output)
        self.normalized["host"]["output_file"] = ""
        self.shadow["attestation"]["output_sha256"] = ""
        with mock.patch.object(AC.HR, "assistant_output", return_value=None):
            candidate = AC._finalize_candidate(
                self.state_path, self.state, self.normalized, self.envelope,
                self.shadow, self.execution, "1" * 64)
        self.assertFalse(candidate["eligible"])
        self.assertIn("raw_output_missing", candidate["reasons"])
        self.assertNotIn("candidate_consumed_by_result_sha256",
                         self.state["review_attempts"][0])

    def test_raw_output_sha_mismatch_blocks_candidate(self):
        self.normalized["host"]["output_sha256"] = "f" * 64
        candidate = AC._finalize_candidate(
            self.state_path, self.state, self.normalized, self.envelope,
            self.shadow, self.execution, "1" * 64)
        self.assertFalse(candidate["eligible"])
        self.assertIn("raw_output_sha_mismatch", candidate["reasons"])

    def test_attempt_consumed_by_other_result_blocks_candidate(self):
        self.state["review_attempts"][0].update({
            "consumed": True, "consumed_by_result_sha256": "2" * 64})
        candidate = AC._finalize_candidate(
            self.state_path, self.state, self.normalized, self.envelope,
            self.shadow, self.execution, "1" * 64)
        self.assertFalse(candidate["eligible"])
        self.assertIn("attempt_consumed_by_other", candidate["reasons"])

    def test_binding_not_bound_to_execution_blocks_candidate(self):
        self.state["execution_bindings"][0][
            "consumed_by_result_sha256"] = "3" * 64
        candidate = AC._finalize_candidate(
            self.state_path, self.state, self.normalized, self.envelope,
            self.shadow, self.execution, "1" * 64)
        self.assertFalse(candidate["eligible"])
        self.assertIn("execution_binding_not_bound_to_execution",
                      candidate["reasons"])

    def test_persist_failure_blocks_candidate_without_marker(self):
        with mock.patch.object(AC.G, "store_review_output",
                               side_effect=OSError("disk full")):
            candidate = AC._finalize_candidate(
                self.state_path, self.state, self.normalized, self.envelope,
                self.shadow, self.execution, "1" * 64)
        self.assertFalse(candidate["eligible"])
        self.assertTrue(any("raw_output_persist_failed" in item
                            for item in candidate["reasons"]))
        self.assertNotIn("candidate_consumed_by_result_sha256",
                         self.state["review_attempts"][0])

if __name__ == "__main__":
    unittest.main()

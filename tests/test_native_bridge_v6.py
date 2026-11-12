#!/usr/bin/env python3
"""v6 原生桥接回归：派发/结果/复核信封绑定、只读边界、G4 fail-closed。"""
from __future__ import annotations

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import native_bridge as NB  # noqa: E402
import v6_support as F  # noqa: E402

G = F.G
BridgeBase = F.BridgeBase
CONTRACT = F.CONTRACT


class DispatchTests(F.BridgeBase):
    def test_dispatch_binds_plan_and_has_stable_ids(self):
        again = NB.build_dispatch(self.state, G.ready_actions(self.state))
        self.assertEqual(self.dispatch["schema"], "copilot-native-dispatch/v2")
        self.assertEqual(self.dispatch["plan_id"], again["plan_id"])
        self.assertEqual(self.dispatch["dispatch_id"], again["dispatch_id"])
        self.assertEqual([i["dispatch_id"] for i in self.dispatch["actions"]],
                         [i["dispatch_id"] for i in again["actions"]])
        self.assertTrue(all(i["idempotency_key"].startswith("ik-")
                            for i in self.dispatch["actions"]))

    def test_dispatch_requires_native_backend(self):
        legacy_state = os.path.join(self.dir, "legacy.json")
        G.start_state(legacy_state, goal="目标", contract=CONTRACT)
        G.plan_state(legacy_state, {"summary": "s", "actions": [
            {"id": "A1", "title": "t", "goal": "g", "expected_evidence": "e"}]})
        state = G.load_state(legacy_state)
        self.assert_code("backend_not_native", NB.build_dispatch,
                         state, G.ready_actions(state))

    def test_validate_dispatch_rejects_v1_and_drift(self):
        self.assert_code("schema_retired", NB.validate_dispatch,
                         {**self.dispatch, "schema": "copilot-native-dispatch/v1"},
                         self.state)
        self.assert_code("schema_invalid", NB.validate_dispatch,
                         {**self.dispatch, "schema": "other"}, self.state)
        drifted = G.load_state(self.state_path)
        for action in drifted["actions"]:
            if action["id"] == "A1":
                action["goal"] = "被改写"
        self.assert_code("plan_drift", NB.validate_dispatch, self.dispatch, drifted)

    def test_validate_dispatch_rejects_foreign_or_duplicate_items(self):
        foreign = json.loads(json.dumps(self.dispatch))
        foreign["actions"][0]["work_item_id"] = "ZZ"
        self.assert_code("work_item_unknown", NB.validate_dispatch,
                         foreign, self.state)
        duplicated = json.loads(json.dumps(self.dispatch))
        duplicated["actions"][1] = dict(duplicated["actions"][0])
        self.assert_code("dispatch_duplicate", NB.validate_dispatch,
                         duplicated, self.state)

    def test_dispatch_ids_cannot_drift_for_existing_items(self):
        existing = json.loads(json.dumps(self.dispatch))
        existing["actions"][0]["dispatch_id"] = "d-forged"
        self.assert_code("dispatch_id_drift", NB.build_dispatch, self.state,
                         G.ready_actions(self.state), existing=existing)


class ExecutionTests(F.BridgeBase):
    def test_valid_execution_is_accepted(self):
        normalized = NB.validate_execution_result(self.execution(), self.dispatch,
                                                  self.state)
        self.assertEqual(normalized["work_item_id"], "A1")
        self.assertEqual(normalized["evidence"][0]["id"], "E1")

    def test_v1_execution_is_retired(self):
        self.assert_code("schema_retired", NB.validate_execution_result,
                         {**self.execution(), "schema": "copilot-native-result/v1"},
                         self.dispatch, self.state)

    def test_plan_and_idempotency_bindings_are_enforced(self):
        self.assert_code("plan_mismatch", NB.validate_execution_result,
                         self.execution(plan_sha256="0" * 64),
                         self.dispatch, self.state)
        self.assert_code("idempotency_mismatch", NB.validate_execution_result,
                         self.execution(idempotency_key="ik-forged"),
                         self.dispatch, self.state)
        self.assert_code("dispatch_mismatch", NB.validate_execution_result,
                         self.execution(dispatch_id="d-forged"),
                         self.dispatch, self.state)

    def test_tampered_result_sha_is_rejected(self):
        env = self.execution()
        env["result"] = "被替换的结论"
        self.assert_code("result_sha_mismatch", NB.validate_execution_result,
                         env, self.dispatch, self.state)

    def test_evidence_file_tamper_is_rejected(self):
        env = self.execution()
        with open(self.evidence, "w", encoding="utf-8") as fh:
            fh.write("tampered\n")
        self.assert_code("evidence_sha_mismatch", NB.validate_execution_result,
                         env, self.dispatch, self.state)

    def test_executor_identity_is_required(self):
        self.assert_code("executor_identity_missing", NB.validate_execution_result,
                         self.execution(executor={"kind": "subagent"}),
                         self.dispatch, self.state)
        self.assert_code("reviewer_as_executor", NB.validate_execution_result,
                         self.execution(executor={
                             "kind": "subagent", "agent_id": "a",
                             "child_session_id": "c", "profile_id": "reviewer"}),
                         self.dispatch, self.state)

    def test_evidence_is_mandatory(self):
        self.assert_code("evidence_missing", NB.validate_execution_result,
                         self.execution(evidence=[]), self.dispatch, self.state)


class ReviewTests(F.BridgeBase):
    def test_g4_review_with_host_record_is_accepted(self):
        execution = self.execution()
        normalized = NB.validate_review_result(self.review(execution),
                                               self.dispatch, execution, self.state)
        self.assertTrue(normalized["g4_eligible"])
        self.assertEqual(normalized["target_sha256"],
                         G.final_evidence_digest(self.state))

    def test_g4_requires_pass_and_host_record(self):
        execution = self.execution()
        self.assert_code("g4_requires_pass", NB.validate_review_result,
                         self.review(execution, verdict="partial"),
                         self.dispatch, execution, self.state)
        env = self.review(execution)
        env.pop("host")
        env["result_sha256"] = NB.envelope_sha256(env)
        self.assert_code("g4_host_record_missing", NB.validate_review_result,
                         env, self.dispatch, execution, self.state)

    def test_g4_host_record_must_be_readonly_and_real(self):
        execution = self.execution()
        self.assert_code("g4_tool_not_readonly", NB.validate_review_result,
                         self.review(execution, host={
                             **self.host_files(),
                             "tool_calls": [{"name": "Write", "count": 1}]}),
                         self.dispatch, execution, self.state)
        missing = {**self.host_files(), "tool_calls": [{"name": "Read", "count": 1}]}
        missing["metadata_path"] = os.path.join(self.dir, "nope.json")
        self.assert_code("g4_host_record_invalid", NB.validate_review_result,
                         self.review(execution, host=missing),
                         self.dispatch, execution, self.state)

    def test_reviewer_must_be_independent_and_readonly(self):
        execution = self.execution()
        self.assert_code("executor_reviewer_same", NB.validate_review_result,
                         self.review(execution, reviewer={
                             "kind": "subagent", "agent_id": "agent-exec",
                             "child_session_id": "child-exec",
                             "profile_id": "reviewer",
                             "model_reported": "m"}),
                         self.dispatch, execution, self.state)
        self.assert_code("review_contaminated", NB.validate_review_result,
                         self.review(execution, changed_files=["src/x.py"]),
                         self.dispatch, execution, self.state)

    def test_review_must_bind_registered_execution(self):
        execution = self.execution()
        self.assert_code("review_binding_mismatch", NB.validate_review_result,
                         self.review(execution, execution_result_sha256="0" * 64),
                         self.dispatch, execution, self.state)

    def test_pass_with_unverified_or_blocking_is_rejected(self):
        execution = self.execution()
        self.assert_code("g4_with_unverified", NB.validate_review_result,
                         self.review(execution, unverified=["没有验证"]),
                         self.dispatch, execution, self.state)
        self.assert_code("g4_with_blocking", NB.validate_review_result,
                         self.review(execution, findings=[{
                             "severity": "blocking", "summary": "存在缺口",
                             "file": self.evidence, "evidence": "证据显示缺口"}]),
                         self.dispatch, execution, self.state)
        # 非 G4 复核同样不得用 PASS 掩盖未验证项或 blocking finding。
        self.assert_code("pass_with_unverified", NB.validate_review_result,
                         self.review(execution, g4_eligible=False,
                                     unverified=["没有验证"]),
                         self.dispatch, execution, self.state)
        self.assert_code("pass_with_blocking_finding", NB.validate_review_result,
                         self.review(execution, g4_eligible=False, findings=[{
                             "severity": "blocking", "summary": "存在缺口",
                             "file": self.evidence, "evidence": "证据显示缺口"}]),
                         self.dispatch, execution, self.state)
        passing = NB.validate_review_result(
            self.review(execution, g4_eligible=False),
            self.dispatch, execution, self.state)
        self.assertFalse(passing["g4_eligible"])
        self.assertEqual(passing["target_sha256"], "")

    def test_write_commands_are_rejected(self):
        execution = self.execution()
        for command in ("rm -rf /tmp/x", "git push", "sed -i s/a/b/ f",
                        "cat f > out.txt", "find . -delete",
                        "python3 -c 'open(\"x\",\"w\")'", "sort -o out in"):
            with self.subTest(command=command):
                self.assert_code("review_command_not_readonly",
                                 NB.validate_review_result,
                                 self.review(execution, commands_run=[command]),
                                 self.dispatch, execution, self.state)

    def test_readonly_commands_are_accepted(self):
        for command in ("git status --short", "git -C /tmp status",
                        "find . -name '*.py' | sort; stat README.md",
                        "grep -n TODO src/a.py 2>/dev/null",
                        "git diff --stat HEAD~1", "wc -l a b"):
            with self.subTest(command=command):
                self.assertEqual(NB.validate_review_commands([command]), [command])


class HostRecordTests(unittest.TestCase):
    def test_g4_requires_absolute_existing_host_files(self):
        with self.assertRaises(NB.BridgeError) as ctx:
            NB._validate_host_record({"metadata_path": "relative.json"}, required=True)
        self.assertEqual(ctx.exception.code, "g4_host_record_invalid")

    def test_non_g4_review_can_omit_host(self):
        self.assertEqual(NB._validate_host_record(None, required=False), {})

    def test_classify_rejects_unknown_and_retired(self):
        self.assertEqual(NB.classify_native_result({"schema": NB.RESULT_SCHEMA}),
                         "execution")
        self.assertEqual(NB.classify_native_result({"schema": NB.REVIEW_SCHEMA}),
                         "review")
        with self.assertRaises(NB.BridgeError) as ctx:
            NB.classify_native_result({"schema": "copilot-native-result/v1"})
        self.assertEqual(ctx.exception.code, "schema_retired")
        with self.assertRaises(NB.BridgeError):
            NB.classify_native_result({"schema": "unknown/v9"})


if __name__ == "__main__":
    unittest.main()

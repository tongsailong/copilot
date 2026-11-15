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
        # 只读 Profile 不得承担执行：判定按宿主有效策略，与 Profile 名称无关。
        self.assert_code("executor_profile_readonly",
                         NB.validate_execution_result,
                         self.execution(executor={
                             "kind": "subagent", "agent_id": "a",
                             "child_session_id": "c",
                             "profile_id": "any-name",
                             "profile": F.readonly_profile_policy(
                                 name="any-name")}),
                         self.dispatch, self.state)
        self.assert_code("executor_profile_readonly",
                         NB.validate_execution_result,
                         self.execution(executor={
                             "kind": "subagent", "agent_id": "a",
                             "child_session_id": "c",
                             "profile_id": "another-name",
                             "profile": F.readonly_profile_policy(
                                 name="another-name")}),
                         self.dispatch, self.state)

    def test_executor_policy_is_required(self):
        # 没有有效策略就不能证明执行方不是只读 Profile：失败关闭。
        self.assert_code("executor_profile_invalid",
                         NB.validate_execution_result,
                         self.execution(executor={
                             "kind": "subagent", "agent_id": "a",
                             "child_session_id": "c", "profile_id": "any"}),
                         self.dispatch, self.state)

    def test_evidence_is_mandatory(self):
        self.assert_code("evidence_missing", NB.validate_execution_result,
                         self.execution(evidence=[]), self.dispatch, self.state)


class ReviewProfilePolicyTests(F.BridgeBase):
    """复核方只读边界只认宿主有效策略：名称/来源/路径改变不影响合法复核。"""

    def _check(self, **overrides) -> dict:
        execution = self.execution()
        env = self.review(execution=execution, **overrides)
        return NB.validate_review_result(env, self.dispatch, execution, self.state)

    def test_profile_name_is_not_gating(self):
        env = self._check(reviewer={
            "kind": "subagent", "agent_id": "agent-review",
            "child_session_id": "child-review",
            "profile_id": "generic-readonly",
            "profile": F.readonly_profile_policy(name="generic-readonly",
                                                 source="user",
                                                 path="/tmp/any/readonly.md"),
            "model_reported": "some-model",
        })
        self.assertEqual(env["reviewer"]["profile_id"], "generic-readonly")
        self.assertEqual(env["reviewer"]["profile"]["tools"],
                         ["Read", "Grep", "Glob"])

    def test_profile_with_bash_is_rejected(self):
        self.assert_code("reviewer_profile_not_readonly", self._check, reviewer={
            "kind": "subagent", "agent_id": "agent-review",
            "child_session_id": "child-review", "profile_id": "reviewer",
            "profile": F.readonly_profile_policy(
                tools=["Read", "Grep", "Glob", "Bash"]),
            "model_reported": "some-model",
        })

    def test_profile_with_mcp_is_rejected(self):
        self.assert_code("reviewer_profile_not_readonly", self._check, reviewer={
            "kind": "subagent", "agent_id": "agent-review",
            "child_session_id": "child-review", "profile_id": "reviewer",
            "profile": F.readonly_profile_policy(mcpServers=["playwright"]),
            "model_reported": "some-model",
        })

    def test_profile_skills_wrong_type_is_rejected(self):
        # 非空但类型错误的 skills 不能被归一化成“没有启用”。
        self.assert_code("reviewer_profile_not_readonly", self._check, reviewer={
            "kind": "subagent", "agent_id": "agent-review",
            "child_session_id": "child-review", "profile_id": "reviewer",
            "profile": F.readonly_profile_policy(skills="app-testing"),
            "model_reported": "some-model",
        })

    def test_profile_mcp_wrong_type_is_rejected(self):
        self.assert_code("reviewer_profile_not_readonly", self._check, reviewer={
            "kind": "subagent", "agent_id": "agent-review",
            "child_session_id": "child-review", "profile_id": "reviewer",
            "profile": F.readonly_profile_policy(mcpServers={"x": 1}),
            "model_reported": "some-model",
        })

    def test_profile_missing_is_rejected(self):
        # 只有 Profile 名称、没有有效权限策略：不能作为只读证据。
        self.assert_code("reviewer_profile_invalid", self._check, reviewer={
            "kind": "subagent", "agent_id": "agent-review",
            "child_session_id": "child-review", "profile_id": "reviewer",
            "model_reported": "some-model",
        })


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

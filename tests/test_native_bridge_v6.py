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
    def test_executor_policy_is_required(self):
        # 执行方身份必须带 profile 快照；缺失失败关闭（G4 退役不等于放弃身份记录）。
        self.assert_code("executor_profile_invalid",
                         NB.validate_execution_result,
                         self.execution(executor={
                             "kind": "subagent", "agent_id": "a",
                             "child_session_id": "c", "profile_id": "any"}),
                         self.dispatch, self.state)

    def test_evidence_is_mandatory(self):
        self.assert_code("evidence_missing", NB.validate_execution_result,
                         self.execution(evidence=[]), self.dispatch, self.state)






if __name__ == "__main__":
    unittest.main()

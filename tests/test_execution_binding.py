#!/usr/bin/env python3
"""Execution binding：创建、消费与执行信封校验。"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import governance as G  # noqa: E402
import native_agent as NA  # noqa: E402
import native_bridge as NB  # noqa: E402
import v6_support as F  # noqa: E402

COMMAND = "node --test tests/calc.test.js"
CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""


def iso(minutes: int) -> str:
    moment = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ms(minutes: int) -> int:
    return int((datetime.now(timezone.utc) + timedelta(minutes=minutes))
               .timestamp() * 1000)


def write_json(path: str, payload) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


class ExecutionBindingFlowTests(F.BridgeBase):
    def setUp(self):
        super().setUp()
        state = G.load_state(self.state_path)
        state["native_dispatch"] = self.dispatch
        G.save_state(self.state_path, state)
        self.out = tempfile.mkdtemp(prefix="binding-out-")
        self.addCleanup(shutil.rmtree, self.out, True)

    def _binding(self, **over):
        binding = G.create_execution_binding(
            self.state_path, work_item_id="A1",
            test_commands=[COMMAND], workspace=self.dir)
        binding["created_at"] = iso(-10)
        state = G.load_state(self.state_path)
        for item in state["execution_bindings"]:
            if item["binding_id"] == binding["binding_id"]:
                item["created_at"] = iso(-10)
        G.save_state(self.state_path, state)
        return binding

    def _metadata(self, nonce: str = "", *, created_at: str = "",
                  prompt: str = "") -> tuple[str, str]:
        meta = os.path.join(self.out, "metadata.json")
        write_json(meta, {
            "agentId": "agent-exec", "childSessionId": "child-exec",
            "parentSessionId": "sess-parent", "profileId": "executor",
            "profileSnapshot": {"name": "executor"},
            "metadataFile": meta, "cwd": self.dir, "workspaceRoot": self.dir,
            "status": "completed", "totalDurationMs": 300000,
            "usage": {"totalTokens": 10},
            "createdAt": created_at or iso(-9), "completedAt": iso(-5),
            "prompt": prompt or f"执行任务 nonce={nonce}",
        })
        rollout = os.path.join(self.out, "rollout.jsonl")
        with open(rollout, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"sessionId": "child-exec",
                                 "model": {"providerId": "p",
                                           "modelId": "m"}}) + "\n")
        return meta, rollout

    def test_binding_flows_from_creation_to_consumption(self):
        binding = self._binding()
        self.assertEqual(len(binding["nonce"]), 32)
        self.assertTrue(binding["binding_id"].startswith("EB"))
        meta, rollout = self._metadata(binding["nonce"])
        args = SimpleNamespace(
            state=self.state_path,
            dispatch=write_json(os.path.join(self.out, "dispatch.json"),
                                self.dispatch),
            work_item="A1", metadata=meta, model_rollout=rollout,
            result_file="", result_text="实际结果", summary="完成",
            status="done", evidence=[f"E1={self.evidence}"],
            test_command=[COMMAND], test_artifact=["E1"],
            workspace=self.dir, base="", output="",
            binding=binding["binding_id"])
        env = NA.build_execution(args)
        self.assertEqual(env["execution_binding"]["binding_id"],
                         binding["binding_id"])
        path = write_json(os.path.join(self.out, "execution.json"), env)
        proc = F.run_cli("copilot_auto.py", "ingest", "--state",
                         self.state_path, "--result-file", path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = G.load_state(self.state_path)
        stored = G.find_execution_binding(state, binding["binding_id"])
        self.assertTrue(stored["consumed"])
        self.assertEqual(stored["consumed_by_result_sha256"],
                         env["result_sha256"])
        self.assertTrue(state["native_results"])

    def _execution_args(self, binding: dict, meta: str, rollout: str):
        return SimpleNamespace(
            state=self.state_path,
            dispatch=write_json(os.path.join(self.out, "dispatch.json"),
                                self.dispatch),
            work_item="A1", metadata=meta, model_rollout=rollout,
            result_file="", result_text="实际结果", summary="完成",
            status="done", evidence=[f"E1={self.evidence}"],
            test_command=[COMMAND], test_artifact=["E1"],
            workspace=self.dir, base="", output="",
            binding=binding["binding_id"])

    def test_binding_requires_nonce_in_executor_prompt(self):
        binding = self._binding()
        meta, rollout = self._metadata("not-the-binding-nonce")
        with self.assertRaisesRegex(NA.AgentRecordError,
                                    "execution_binding_prompt_missing"):
            NA.build_execution(self._execution_args(binding, meta, rollout))

    def test_binding_rejects_session_created_before_binding(self):
        binding = self._binding()
        meta, rollout = self._metadata(binding["nonce"], created_at=iso(-20))
        with self.assertRaisesRegex(NA.AgentRecordError,
                                    "execution_binding_session_too_old"):
            NA.build_execution(self._execution_args(binding, meta, rollout))

    def test_output_inside_workspace_is_rejected(self):
        binding = self._binding()
        meta, rollout = self._metadata(binding["nonce"])
        args = SimpleNamespace(
            state=self.state_path,
            dispatch=write_json(os.path.join(self.out, "dispatch.json"),
                                self.dispatch),
            work_item="A1", metadata=meta, model_rollout=rollout,
            result_file="", result_text="实际结果", summary="完成",
            status="done", evidence=[f"E1={self.evidence}"],
            test_command=[COMMAND], test_artifact=["E1"],
            workspace=self.dir, base="",
            output=os.path.join(self.dir, "execution.json"),
            binding=binding["binding_id"])
        with self.assertRaisesRegex(NA.AgentRecordError, "output"):
            NA.build_execution(args)

    def test_consumed_binding_cannot_be_reused(self):
        binding = self._binding()
        state = G.load_state(self.state_path)
        stored = G.find_execution_binding(state, binding["binding_id"])
        stored["consumed"] = True
        G.save_state(self.state_path, state)
        problems = G.execution_binding_problems(
            G.load_state(self.state_path), binding["binding_id"],
            work_item_id="A1", test_commands=[COMMAND],
            workspace=self.dir)
        self.assertIn("execution_binding_consumed", problems)

    def test_command_or_workspace_change_is_rejected(self):
        binding = self._binding()
        state = G.load_state(self.state_path)
        problems = G.execution_binding_problems(
            state, binding["binding_id"], work_item_id="A1",
            test_commands=["npm test"], workspace=self.dir)
        self.assertIn("execution_binding_command_mismatch", problems)
        problems = G.execution_binding_problems(
            state, binding["binding_id"], work_item_id="A1",
            test_commands=[COMMAND], workspace="/tmp/other")
        self.assertIn("execution_binding_workspace_mismatch", problems)

    def test_bridge_rejects_contract_changed_binding(self):
        binding = self._binding()
        meta, rollout = self._metadata(binding["nonce"])
        args = SimpleNamespace(
            state=self.state_path,
            dispatch=write_json(os.path.join(self.out, "dispatch.json"),
                                self.dispatch),
            work_item="A1", metadata=meta, model_rollout=rollout,
            result_file="", result_text="实际结果", summary="完成",
            status="done", evidence=[f"E1={self.evidence}"],
            test_command=[COMMAND], test_artifact=["E1"],
            workspace=self.dir, base="", output="",
            binding=binding["binding_id"])
        env = NA.build_execution(args)
        state = G.load_state(self.state_path)
        state["contract_sha256"] = "f" * 64
        with self.assertRaises(NB.BridgeError) as ctx:
            NB._validate_execution_binding(
                env["execution_binding"], state, work_item_id="A1",
                task_id=state["task_id"], test_commands=[COMMAND])
        self.assertEqual(ctx.exception.code,
                         "execution_binding_contract_changed")

    def test_contract_change_invalidates_binding(self):
        binding = self._binding()
        state = G.load_state(self.state_path)
        state["contract_sha256"] = "f" * 64
        problems = G.execution_binding_problems(
            state, binding["binding_id"], work_item_id="A1",
            test_commands=[COMMAND], workspace=self.dir)
        self.assertIn("execution_binding_contract_changed", problems)

    def test_cross_task_binding_is_rejected(self):
        binding = self._binding()
        state = G.load_state(self.state_path)
        state["task_id"] = "task-other"
        problems = G.execution_binding_problems(
            state, binding["binding_id"], work_item_id="A1",
            test_commands=[COMMAND], workspace=self.dir)
        self.assertIn("execution_binding_cross_task", problems)


class ContractPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(
            self.state_path, goal="目标", contract=CONTRACT,
            execution_backend="native", risk="high",
            test_policy={"required": True, "commands": [COMMAND],
                         "report": "reports/junit.xml",
                         "artifacts": ["reports/junit.xml"]})
        G.plan_state(self.state_path, {"summary": "实现", "actions": [
            {"id": "A1", "title": "实现", "goal": "产出结果",
             "expected_evidence": "结果文件"}]})
        state = G.load_state(self.state_path)
        self.dispatch = NB.build_dispatch(state, G.ready_actions(state))
        state["native_dispatch"] = self.dispatch
        G.save_state(self.state_path, state)

    def test_contract_commands_and_layers_cannot_be_lowered(self):
        binding = G.create_execution_binding(
            self.state_path, work_item_id="A1", workspace=self.dir)
        self.assertEqual(binding["test_commands"], [COMMAND])
        self.assertTrue(binding["require_test_result"])
        self.assertTrue(binding["require_artifact_integrity"])
        self.assertEqual(binding["requirement_source"], "contract")
        self.assertEqual(binding["contract_report"], "reports/junit.xml")
        with self.assertRaises(G.GovernanceError):
            G.create_execution_binding(
                self.state_path, work_item_id="A1", workspace=self.dir,
                test_commands=["npm test"])

    def _metadata(self, binding: dict) -> tuple[str, str]:
        meta = os.path.join(self.dir, "metadata.json")
        write_json(meta, {
            "agentId": "agent-exec", "childSessionId": "child-exec",
            "parentSessionId": "sess-parent", "profileId": "executor",
            "profileSnapshot": {"name": "executor"},
            "metadataFile": meta, "cwd": self.dir, "workspaceRoot": self.dir,
            "status": "completed", "totalDurationMs": 300000,
            "usage": {"totalTokens": 10},
            "createdAt": iso(1), "completedAt": iso(2),
            "prompt": f"执行任务 nonce={binding['nonce']}",
        })
        rollout = os.path.join(self.dir, "rollout.jsonl")
        with open(rollout, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"sessionId": "child-exec",
                                 "model": {"providerId": "p",
                                           "modelId": "m"}}) + "\n")
        return meta, rollout

    def _args(self, binding: dict, evidence: str):
        dispatch = write_json(os.path.join(self.dir, "dispatch.json"),
                              self.dispatch)
        meta, rollout = self._metadata(binding)
        return SimpleNamespace(
            state=self.state_path, dispatch=dispatch, work_item="A1",
            metadata=meta, model_rollout=rollout, result_file="",
            result_text="实际结果", summary="完成", status="done",
            evidence=[f"E1={evidence}"], test_command=[COMMAND],
            test_artifact=["E1"], workspace=self.dir, base="", output="",
            binding=binding["binding_id"])

    def test_contract_report_must_be_registered_as_evidence(self):
        binding = G.create_execution_binding(
            self.state_path, work_item_id="A1", workspace=self.dir)
        evidence = os.path.join(self.dir, "result.txt")
        with open(evidence, "w", encoding="utf-8") as fh:
            fh.write("result\n")
        with self.assertRaisesRegex(NA.AgentRecordError,
                                    "contract_artifact_missing"):
            NA.build_execution(self._args(binding, evidence))

    def test_contract_report_created_before_binding_is_rejected(self):
        report = os.path.join(self.dir, "reports", "junit.xml")
        os.makedirs(os.path.dirname(report), exist_ok=True)
        with open(report, "w", encoding="utf-8") as fh:
            fh.write("<testsuite/>\n")
        old = time.time() - 60
        os.utime(report, (old, old))
        binding = G.create_execution_binding(
            self.state_path, work_item_id="A1", workspace=self.dir)
        with self.assertRaisesRegex(NA.AgentRecordError,
                                    "test_artifact_stale|contract_artifact_stale"):
            NA.build_execution(self._args(binding, report))

    def test_test_commands_without_artifact_are_rejected(self):
        binding = G.create_execution_binding(
            self.state_path, work_item_id="A1", workspace=self.dir)
        evidence = os.path.join(self.dir, "result.txt")
        with open(evidence, "w", encoding="utf-8") as fh:
            fh.write("result\n")
        args = self._args(binding, evidence)
        args.test_artifact = []
        with self.assertRaisesRegex(NA.AgentRecordError,
                                    "execution_test_artifact_missing"):
            NA.build_execution(args)

    def test_contract_report_as_evidence_is_accepted(self):
        binding = G.create_execution_binding(
            self.state_path, work_item_id="A1", workspace=self.dir)
        report = os.path.join(self.dir, "reports", "junit.xml")
        os.makedirs(os.path.dirname(report), exist_ok=True)
        with open(report, "w", encoding="utf-8") as fh:
            fh.write("<testsuite/>\n")
        env = NA.build_execution(self._args(binding, report))
        self.assertEqual(env["test_artifacts"], ["E1"])
        self.assertEqual(env["test_commands"], [COMMAND])

    def test_gate_criteria_accepts_contract_test_policy(self):
        state = G.load_state(self.state_path)
        codes = [item["code"] for item in G._gate_criteria(state)["reasons"]]
        self.assertNotIn("contract_fingerprint_mismatch", codes)

    def test_tampered_binding_policy_is_rejected(self):
        binding = G.create_execution_binding(
            self.state_path, work_item_id="A1", workspace=self.dir)
        state = G.load_state(self.state_path)
        stored = G.find_execution_binding(state, binding["binding_id"])
        stored["require_test_result"] = False
        stored["require_artifact_integrity"] = False
        G.save_state(self.state_path, state)
        with self.assertRaises(NB.BridgeError) as ctx:
            NB._validate_execution_binding(
                {"binding_id": binding["binding_id"],
                 "nonce": binding["nonce"], "workspace": binding["workspace"]},
                G.load_state(self.state_path), work_item_id="A1",
                task_id=binding["task_id"], test_commands=[COMMAND])
        self.assertEqual(ctx.exception.code,
                         "execution_binding_policy_weakened")

    def test_tampered_contract_report_artifacts_are_rejected(self):
        binding = G.create_execution_binding(
            self.state_path, work_item_id="A1", workspace=self.dir)
        state = G.load_state(self.state_path)
        stored = G.find_execution_binding(state, binding["binding_id"])
        stored["contract_artifacts"] = []
        G.save_state(self.state_path, state)
        with self.assertRaises(NB.BridgeError) as ctx:
            NB._validate_execution_binding(
                {"binding_id": binding["binding_id"],
                 "nonce": binding["nonce"], "workspace": binding["workspace"]},
                G.load_state(self.state_path), work_item_id="A1",
                task_id=binding["task_id"], test_commands=[COMMAND])
        self.assertEqual(ctx.exception.code,
                         "execution_binding_policy_weakened")

    def test_tampered_contract_report_is_rejected(self):
        binding = G.create_execution_binding(
            self.state_path, work_item_id="A1", workspace=self.dir)
        state = G.load_state(self.state_path)
        stored = G.find_execution_binding(state, binding["binding_id"])
        stored["contract_report"] = ""
        G.save_state(self.state_path, state)
        with self.assertRaises(NB.BridgeError) as ctx:
            NB._validate_execution_binding(
                {"binding_id": binding["binding_id"],
                 "nonce": binding["nonce"], "workspace": binding["workspace"]},
                G.load_state(self.state_path), work_item_id="A1",
                task_id=binding["task_id"], test_commands=[COMMAND])
        self.assertEqual(ctx.exception.code,
                         "execution_binding_policy_weakened")

    def test_tampered_contract_policy_is_rejected(self):
        binding = G.create_execution_binding(
            self.state_path, work_item_id="A1", workspace=self.dir)
        state = G.load_state(self.state_path)
        stored = G.find_execution_binding(state, binding["binding_id"])
        stored["contract_test_policy"]["required"] = False
        G.save_state(self.state_path, state)
        with self.assertRaises(NB.BridgeError) as ctx:
            NB._validate_execution_binding(
                {"binding_id": binding["binding_id"],
                 "nonce": binding["nonce"], "workspace": binding["workspace"]},
                G.load_state(self.state_path), work_item_id="A1",
                task_id=binding["task_id"], test_commands=[COMMAND])
        self.assertEqual(ctx.exception.code,
                         "execution_binding_policy_weakened")

    def test_contract_without_tests_is_not_forced(self):
        other = os.path.join(self.dir, "plain.json")
        G.start_state(other, goal="无测试任务", contract=CONTRACT,
                      execution_backend="native", risk="normal",
                      test_policy={"required": False})
        G.plan_state(other, {"summary": "实现", "actions": [
            {"id": "A1", "title": "实现", "goal": "产出结果",
             "expected_evidence": "结果文件"}]})
        state = G.load_state(other)
        dispatch = NB.build_dispatch(state, G.ready_actions(state))
        state["native_dispatch"] = dispatch
        G.save_state(other, state)
        binding = G.create_execution_binding(other, work_item_id="A1",
                                             workspace=self.dir)
        self.assertEqual(binding["test_commands"], [])
        self.assertFalse(binding["require_test_result"])
        self.assertFalse(binding["require_artifact_integrity"])
        self.assertEqual(binding["requirement_source"], "contract")


class TestPolicyCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state = os.path.join(self.dir, "state.json")
        self.contract = os.path.join(self.dir, "contract.md")
        with open(self.contract, "w", encoding="utf-8") as fh:
            fh.write(CONTRACT)

    def test_start_and_amend_accept_test_policy(self):
        policy = {"required": True, "commands": [COMMAND],
                  "report": "reports/junit.xml",
                  "artifacts": ["reports/junit.xml"]}
        policy_file = os.path.join(self.dir, "policy.json")
        with open(policy_file, "w", encoding="utf-8") as fh:
            json.dump(policy, fh)
        proc = F.run_cli(
            "copilot_dispatch.py", "start", "--state", self.state,
            "--goal", "目标", "--contract-file", self.contract,
            "--risk", "high", "--backend", "native",
            "--test-policy-file", policy_file)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = G.load_state(self.state)
        stored = state["contract"]["test_policy"]
        self.assertTrue(stored["captured"])
        self.assertEqual(stored["commands"], [COMMAND])
        self.assertTrue(stored["require_test_result"])
        self.assertEqual(stored["report"], "reports/junit.xml")
        # 放松 test_policy 属破坏性修订：没有用户明确授权时 fail-closed。
        proc = F.run_cli(
            "copilot_dispatch.py", "contract", "--state", self.state,
            "--amend", "--contract-file", self.contract,
            "--test-policy", json.dumps({"required": False}))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("authorized-by", proc.stderr)
        proc = F.run_cli(
            "copilot_dispatch.py", "contract", "--state", self.state,
            "--amend", "--contract-file", self.contract,
            "--test-policy", json.dumps({"required": False}),
            "--authorized-by", "user")
        # 只声明 user 不足以留痕：必须写明用户要求，否则事后无法核对授权范围。
        self.assertEqual(proc.returncode, 2)
        self.assertIn("authorization-note", proc.stderr)
        proc = F.run_cli(
            "copilot_dispatch.py", "contract", "--state", self.state,
            "--amend", "--contract-file", self.contract,
            "--test-policy", json.dumps({"required": False}),
            "--authorized-by", "user",
            "--authorization-note", "用户同意本次不强制测试")
        self.assertEqual(proc.returncode, 10, proc.stderr)
        stored = G.load_state(self.state)["contract"]["test_policy"]
        self.assertFalse(stored["required"])
        self.assertEqual(stored["commands"], [])


if __name__ == "__main__":
    unittest.main()

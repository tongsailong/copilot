#!/usr/bin/env python3
"""正式 G4 切换后的快照绑定与放行判别测试。

阶段九起 `native_reviews[].g4_eligible` 只在 shadow `attested` 且新链 eligible 时
产生，收口门禁还要求 `g4_source="candidate"` 与 `candidate_g4.eligible=true`；旧
Reviewer 信封的 `g4_eligible` 自述、Bash 命令、rollout 记录都不再构成正式资格。本文件锁定：
- 正式放行需要 attested + 新链 eligible，且风险为 high；
- 旧信封自报 eligible 不放行，Bash 命令直接拒绝；
- attempt 固化的代码/证据快照漂移在 ingest 时失败关闭；
- review-context 创建 attempt 的绑定与 pending criteria 保护；
- attempt 工作区与证据仓库一致。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
import sys  # noqa: E402

sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import governance as G  # noqa: E402
import native_bridge as NB  # noqa: E402
import native_agent as NA  # noqa: E402
import copilot_auto as AC  # noqa: E402
import v6_support as F  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""


class SnapshotBindingTests(unittest.TestCase):
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
        self.binding = G.create_execution_binding(
            self.state_path, work_item_id="A1", workspace=self.dir)
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
            "execution_binding": {
                "binding_id": self.binding["binding_id"],
                "nonce": self.binding["nonce"],
                "created_at": self.binding["created_at"],
                "workspace": self.binding["workspace"],
                "test_commands": self.binding["test_commands"],
            },
        }
        self.execution["result_sha256"] = NB.envelope_sha256(self.execution)
        AC._ingest_execution(self.state_path, self.execution)
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=["E1"])
        self.state = G.load_state(self.state_path)
        # Reviewer 宿主产物放在工作区外，避免取证件进入执行代码快照。
        self.host_dir = tempfile.mkdtemp(prefix="review-host-")
        self.addCleanup(shutil.rmtree, self.host_dir, True)

    # ── 夹具 ──────────────────────────────────────────────────────────────
    def attempt(self, execution: dict | None = None) -> dict:
        execution = execution or self.execution
        return G.create_review_attempt(
            self.state_path, work_item_id=str(execution["work_item_id"]),
            execution_result_sha256=str(execution["result_sha256"]),
            workspace=self.dir)

    def envelope(self, attempt: dict, *, prompt: str = "",
                 **overrides) -> dict:
        state = G.load_state(self.state_path)
        host = F.make_fake_review_host(
            self.host_dir, execution=self.execution, dispatch=self.dispatch,
            evidence=self.evidence, task_id=state["task_id"],
            attempt_id=attempt["review_attempt_id"],
            session_created_at=attempt["created_at"],
            prompt=prompt, workspace=self.dir)
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
            # 旧 Reviewer 自述；阶段九起不参与正式放行。
            "g4_eligible": True,
        }
        env.update(overrides)
        env["result_sha256"] = NB.envelope_sha256(env)
        return env

    def shadow(self, *, status: str = "attested", eligible: bool = True,
               reasons: list[str] | None = None) -> dict:
        return {
            "shadow_id": "GS-TEST",
            "status": status,
            "new_g4_eligible": eligible,
            "new_reason": list(reasons or []),
            "test_execution": {
                "status": "not_applicable", "problems": [],
                "execution_binding_id": self.binding["binding_id"],
            },
            "attestation": {"output_sha256": ""},
        }

    def ingest_with(self, env: dict, shadow: dict) -> None:
        with mock.patch.object(AC.SH, "build", return_value=shadow):
            AC._ingest_review(self.state_path, env)

    def write_source(self, name: str, text: str, *, commit: bool = False) -> str:
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        if commit:
            subprocess.run(["git", "-C", self.dir, "add", name], check=True,
                           capture_output=True, text=True)
            subprocess.run(["git", "-C", self.dir, "commit", "-qm", name],
                           check=True, capture_output=True, text=True)
        return path

    # ── 正式 G4 放行 ──────────────────────────────────────────────────────
    def test_formal_g4_requires_attested_candidate(self):
        attempt = self.attempt()
        env = self.envelope(attempt)
        self.ingest_with(env, self.shadow())
        state = G.load_state(self.state_path)
        review = state["native_reviews"][0]
        self.assertTrue(review["g4_eligible"])
        self.assertEqual(review["g4_source"], "candidate")
        self.assertEqual(review["g4_reasons"], [])
        self.assertTrue(review["candidate_g4"]["eligible"],
                        review["candidate_g4"]["reasons"])
        self.assertTrue(review["candidate_g4"]["raw_output_sha256"])
        consumed = G.find_review_attempt(state, attempt["review_attempt_id"])
        self.assertTrue(consumed["consumed"])
        self.assertEqual(consumed["consumed_by_result_sha256"],
                         env["result_sha256"])

    def test_attested_but_ineligible_is_not_formal(self):
        attempt = self.attempt()
        self.ingest_with(self.envelope(attempt),
                         self.shadow(eligible=False,
                                     reasons=["test_execution_not_verified"]))
        state = G.load_state(self.state_path)
        review = state["native_reviews"][0]
        self.assertFalse(review["g4_eligible"])
        self.assertEqual(review["g4_source"], "")
        self.assertIn("test_execution_not_verified", review["g4_reasons"])
        self.assertFalse(G.find_review_attempt(
            state, attempt["review_attempt_id"])["consumed"])

    def test_failed_shadow_is_not_formal(self):
        attempt = self.attempt()
        self.ingest_with(self.envelope(attempt),
                         self.shadow(status="failed",
                                     eligible=False,
                                     reasons=["host_session_missing"]))
        state = G.load_state(self.state_path)
        self.assertFalse(state["native_reviews"][0]["g4_eligible"])
        self.assertFalse(G.find_review_attempt(
            state, attempt["review_attempt_id"])["consumed"])

    def test_normal_risk_is_never_formal(self):
        state = G.load_state(self.state_path)
        state["risk"] = "normal"
        G.save_state(self.state_path, state)
        self.ingest_with(self.envelope(self.attempt()), self.shadow())
        state = G.load_state(self.state_path)
        self.assertFalse(state["native_reviews"][0]["g4_eligible"])
        self.assertEqual(state["native_reviews"][0]["g4_source"], "")

    def test_legacy_g4_source_is_rejected_by_gate(self):
        attempt = self.attempt()
        env = self.envelope(attempt)
        state = G.load_state(self.state_path)
        record = dict(env)
        record["g4_eligible"] = True
        record["g4_source"] = ""
        record["target_sha256"] = G.final_evidence_digest(state)
        record["coverage"] = NB.review_coverage(state)
        F.attach_review(state, record, attempt)
        G.save_state(self.state_path, state)
        ok, code = G._eligible_review(record, G.load_state(self.state_path))
        self.assertFalse(ok)
        self.assertEqual(code, "native_review_legacy_g4_source")

    def test_bash_review_is_rejected(self):
        env = self.envelope(self.attempt(),
                            commands_run=["git status --short"])
        with self.assertRaises(AC.AutoError) as ctx:
            self.ingest_with(env, self.shadow())
        self.assertIn("review_command_unexpected", str(ctx.exception))

    def test_code_change_after_attempt_blocks_formal_g4(self):
        attempt = self.attempt()
        self.write_source("src/feature.py", "VALUE = 2\n")
        with self.assertRaises(AC.AutoError) as ctx:
            self.ingest_with(self.envelope(attempt), self.shadow())
        self.assertIn("native_review_code_scope_drift", str(ctx.exception))
        state = G.load_state(self.state_path)
        self.assertFalse(G.find_review_attempt(
            state, attempt["review_attempt_id"])["consumed"])

    def test_evidence_drift_after_attempt_blocks_formal_g4(self):
        attempt = self.attempt()
        with open(self.evidence, "w", encoding="utf-8") as fh:
            fh.write("drift\n")
        with self.assertRaises(AC.AutoError) as ctx:
            self.ingest_with(self.envelope(attempt), self.shadow())
        self.assertIn("native_review_attempt_workspace_drift",
                      str(ctx.exception))

    # ── review-context 绑定 ──────────────────────────────────────────────
    def write_json(self, name: str, payload: dict) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        return path

    def context_args(self, **overrides) -> argparse.Namespace:
        args = {
            "state": self.state_path,
            "dispatch": self.write_json("dispatch.json", self.dispatch),
            "execution": self.write_json("execution.json", self.execution),
            "work_item": "A1", "previous_review": "", "create_attempt": False,
            "workspace": self.dir,
        }
        args.update(overrides)
        return argparse.Namespace(**args)

    def test_review_context_create_attempt_binds_current_snapshot(self):
        self.write_source("src/feature.py", "VALUE = 1\n")
        payload = NA.build_review_context(self.context_args(create_attempt=True))
        self.assertTrue(payload["review_attempt_id"].startswith("RA"))
        self.assertIn("src/feature.py", [f["path"] for f in
                                         payload["code_snapshot"]["files"]])
        self.assertEqual(payload["target_sha256"],
                         G.final_evidence_digest(G.load_state(self.state_path)))
        state = G.load_state(self.state_path)
        attempt = G.find_review_attempt(state, payload["review_attempt_id"])
        self.assertIsNotNone(attempt)
        self.assertFalse(attempt["consumed"])
        self.assertEqual(attempt["created_at"],
                         payload["review_attempt_created_at"])
        self.assertEqual(attempt["target_sha256"], payload["target_sha256"])

    def test_review_context_create_attempt_refuses_pending_criteria(self):
        sub = os.path.join(self.dir, "pending")
        os.makedirs(sub)
        state_path = os.path.join(sub, "state.json")
        G.start_state(state_path, goal="目标", contract=CONTRACT,
                      execution_backend="native", risk="high",
                      test_policy={"required": False})
        G.plan_state(state_path, {"summary": "实现", "actions": [
            {"id": "A1", "title": "实现", "goal": "产出结果",
             "expected_evidence": "结果文件"}]})
        state = G.load_state(state_path)
        dispatch = NB.build_dispatch(state, G.ready_actions(state))
        state["native_dispatch"] = dispatch
        G.save_state(state_path, state)
        item = dispatch["actions"][0]
        evidence = os.path.join(sub, "result.txt")
        with open(evidence, "w", encoding="utf-8") as fh:
            fh.write("result\n")
        execution = {
            "schema": NB.RESULT_SCHEMA, "task_id": state["task_id"],
            "contract_sha256": state["contract_sha256"],
            "plan_id": dispatch["plan_id"], "plan_sha256": dispatch["plan_sha256"],
            "iteration_id": dispatch["iteration_id"],
            "dispatch_id": item["dispatch_id"],
            "idempotency_key": item["idempotency_key"], "work_item_id": "A1",
            "status": "done", "summary": "完成", "result": "实际结果",
            "evidence": [{"id": "E1", "file": evidence,
                          "sha256": NB.sha256_file(evidence), "kind": "file"}],
            "executor": {"kind": "subagent", "agent_id": "agent-exec",
                         "child_session_id": "child-exec",
                         "profile_id": "executor", "model_reported": "m"},
            "timing": {"duration_ms": 1}, "usage": {"total_tokens": 1},
        }
        execution["result_sha256"] = NB.envelope_sha256(execution)
        AC._ingest_execution(state_path, execution)
        args = argparse.Namespace(
            state=state_path,
            dispatch=self.write_json("pending-dispatch.json", dispatch),
            execution=self.write_json("pending-execution.json", execution),
            work_item="A1", previous_review="", create_attempt=True)
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review_context(args)
        self.assertIn("review_attempt_unstable", str(ctx.exception))
        self.assertEqual(G.load_state(state_path).get("review_attempts") or [], [])

    # ── 快照捕获 ──────────────────────────────────────────────────────────
    def test_committed_task_change_is_included_with_base(self):
        base = subprocess.run(["git", "-C", self.dir, "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
        self.write_source("src/committed.py", "VALUE = 1\n", commit=True)
        attempt = G.create_review_attempt(
            self.state_path, work_item_id="A1",
            execution_result_sha256=self.execution["result_sha256"],
            workspace=self.dir, base=base)
        self.assertIn("src/committed.py", [
            f["path"] for f in attempt["code_snapshot"]["files"]])

    def test_ignored_build_output_does_not_invalidate(self):
        with open(os.path.join(self.dir, ".gitignore"), "a",
                  encoding="utf-8") as fh:
            fh.write("build/\n")
        subprocess.run(["git", "-C", self.dir, "add", ".gitignore"], check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", self.dir, "commit", "-qm", "ignore build"],
                       check=True, capture_output=True, text=True)
        attempt = self.attempt()
        build = self.write_source("build/out.bin", "artifact\n")
        state = G.load_state(self.state_path)
        self.assertEqual(G.code_scope_problems(attempt, state), [])
        with open(build, "w", encoding="utf-8") as fh:
            fh.write("changed\n")
        state = G.load_state(self.state_path)
        self.assertEqual(G.code_scope_problems(attempt, state), [])

    def test_attempt_workspace_must_match_evidence_repository(self):
        other = tempfile.mkdtemp(prefix="other-ws-")
        self.addCleanup(shutil.rmtree, other, True)
        F.init_git_workspace(other)
        with self.assertRaises(G.GovernanceError):
            G.create_review_attempt(
                self.state_path, work_item_id="A1",
                execution_result_sha256=self.execution["result_sha256"],
                workspace=other)


if __name__ == "__main__":
    unittest.main()

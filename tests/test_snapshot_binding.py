#!/usr/bin/env python3
"""G4 快照绑定判别测试：旧 Reviewer 输出不能变成新快照的有效结论。

覆盖场景：
- S0 正常 PASS；
- S0→S1 代码 / 证据 / 合同变化后旧复核过期；
- Reviewer 会话 / attempt 复用 / 旧输出重新封装；
- 复核期间证据文件漂移；
- 新快照新建 attempt 可复核；
- 缺失 attempt、失败后重试、重复 ingest。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import types
import unittest

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
                      execution_backend="native", risk="high")
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

    # ── 夹具 ──────────────────────────────────────────────────────────────
    def attempt(self, execution: dict | None = None) -> dict:
        execution = execution or self.execution
        return G.create_review_attempt(
            self.state_path, work_item_id=str(execution["work_item_id"]),
            execution_result_sha256=str(execution["result_sha256"]),
            workspace=self.dir)

    def envelope(self, attempt: dict, *, prompt: str = "",
                 session_created_at: str = "", **overrides) -> dict:
        state = G.load_state(self.state_path)
        host = F.make_fake_review_host(
            self.dir, execution=self.execution, dispatch=self.dispatch,
            evidence=self.evidence, task_id=state["task_id"],
            attempt_id=attempt["review_attempt_id"],
            session_created_at=session_created_at or attempt["created_at"],
            prompt=prompt)
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
            "commands_run": ["git status --short"],
            "changed_files": [],
            "unverified": [],
            "residual_risks": [],
            "g4_eligible": True,
        }
        env.update(overrides)
        env["result_sha256"] = NB.envelope_sha256(env)
        return env

    def ingest(self, env: dict) -> str:
        AC._ingest_review(self.state_path, env)
        return str(env["result_sha256"])

    def add_code_change(self) -> None:
        G.rerun_action(self.state_path, "A1", reason="代码变化")
        G.record_action(self.state_path, "A1", "done", result="变化后的代码",
                        evidence_ids=["E1"])

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

    def code_codes(self) -> dict:
        check = G.closure_check(G.load_state(self.state_path))
        return {gate: {r["code"] for r in check["gates"][gate]["reasons"]}
                for gate in ("G1", "G2", "G3", "G4")}

    def test_unregistered_source_change_breaks_g2_and_g4(self):
        self.write_source("src/feature.py", "VALUE = 1\n")
        self.ingest(self.envelope(self.attempt()))
        self.write_source("src/feature.py", "VALUE = 2\n")
        codes = self.code_codes()
        self.assertIn("native_review_code_scope_drift", codes["G2"])
        self.assertIn("native_review_code_scope_drift", codes["G4"])

    def test_new_source_file_after_attempt_is_rejected_at_ingest(self):
        self.write_source("src/feature.py", "VALUE = 1\n")
        attempt = self.attempt()
        env = self.envelope(attempt)
        self.write_source("src/new_file.py", "NEW = True\n")
        with self.assertRaises(AC.AutoError) as ctx:
            self.ingest(env)
        self.assertIn("native_review_code_scope_drift", str(ctx.exception))
        state = G.load_state(self.state_path)
        self.assertFalse(G.find_review_attempt(
            state, attempt["review_attempt_id"])["consumed"])

    def test_deleted_source_file_is_detected(self):
        path = self.write_source("src/feature.py", "VALUE = 1\n")
        self.ingest(self.envelope(self.attempt()))
        os.remove(path)
        self.assertIn("native_review_code_scope_drift", self.code_codes()["G2"])

    def test_renamed_source_file_is_detected(self):
        self.write_source("src/old.py", "VALUE = 1\n", commit=True)
        self.ingest(self.envelope(self.attempt()))
        subprocess.run(["git", "-C", self.dir, "mv", "src/old.py", "src/new.py"],
                       check=True, capture_output=True, text=True)
        self.assertIn("native_review_code_scope_drift", self.code_codes()["G2"])
        # 新 attempt 必须把 rename 作为 R + old_path 纳入快照。
        fresh = self.attempt()
        state = G.load_state(self.state_path)
        entry = next(item for item in G.find_review_attempt(
            state, fresh["review_attempt_id"])["code_snapshot"]["files"]
            if item["path"] == "src/new.py")
        self.assertEqual(entry["status"], "R ")
        self.assertEqual(entry["old_path"], "src/old.py")

    def test_ignored_build_output_does_not_invalidate(self):
        with open(os.path.join(self.dir, ".gitignore"), "a", encoding="utf-8") as fh:
            fh.write("build/\n")
        subprocess.run(["git", "-C", self.dir, "add", ".gitignore"], check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", self.dir, "commit", "-qm", "ignore build"],
                       check=True, capture_output=True, text=True)
        build = self.write_source("build/out.bin", "a\n")
        self.ingest(self.envelope(self.attempt()))
        with open(build, "w", encoding="utf-8") as fh:
            fh.write("changed build output\n")
        self.assertNotIn("native_review_code_scope_drift", self.code_codes()["G2"])

    def test_new_attempt_after_source_change_is_allowed(self):
        self.write_source("src/feature.py", "VALUE = 1\n")
        self.ingest(self.envelope(self.attempt()))
        self.write_source("src/feature.py", "VALUE = 2\n")
        fresh = self.attempt()
        self.ingest(self.envelope(fresh))
        self.assertNotIn("native_review_code_scope_drift", self.code_codes()["G2"])
        self.assertNotIn("native_review_code_scope_drift", self.code_codes()["G4"])

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
        self.ingest(self.envelope(attempt))
        self.assertNotIn("native_review_code_scope_drift", self.code_codes()["G2"])

    def test_legacy_attempt_without_code_scope_fails_g2(self):
        attempt = self.attempt()
        self.ingest(self.envelope(attempt))
        state = G.load_state(self.state_path)
        bound = G.find_review_attempt(state, attempt["review_attempt_id"])
        bound.pop("code_snapshot", None)
        bound.pop("code_scope_sha256", None)
        G.save_state(self.state_path, state)
        self.assertIn("native_review_code_scope_missing", self.code_codes()["G2"])

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
                      execution_backend="native", risk="high")
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

    def _ingested_base_record(self) -> dict:
        self.ingest(self.envelope(self.attempt()))
        state = G.load_state(self.state_path)
        return dict(state["native_reviews"][0])

    def test_legacy_pass_without_attempt_is_not_reusable(self):
        record = self._ingested_base_record()
        record.pop("review_attempt_id", None)
        record.pop("review_attempt_created_at", None)
        state = G.load_state(self.state_path)
        state["native_reviews"] = [record]
        state["review_attempts"] = []
        G.save_state(self.state_path, state)
        path = self.write_json("legacy-base.json", record)
        payload = NA.build_review_context(
            self.context_args(previous_review=path))
        self.assertEqual(payload["review_scope"], "full")
        self.assertIn("信任检查", payload["scope_reason"])
        self.assertIn("native_review_attempt_missing",
                      payload["previous_review"]["trust_problems"])
        # 不可信基线不阻止重新派发：允许创建新 attempt 做完整复核。
        fresh = NA.build_review_context(
            self.context_args(previous_review=path, create_attempt=True))
        self.assertTrue(fresh["review_attempt_id"].startswith("RA"))
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA._incremental_base(G.load_state(self.state_path), path)
        self.assertIn("incremental_base_ineligible", str(ctx.exception))

    def test_review_context_refuses_reuse_of_identical_trusted_pass(self):
        record = self._ingested_base_record()
        path = self.write_json("base.json", record)
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review_context(
                self.context_args(previous_review=path, create_attempt=True))
        self.assertIn("review_attempt_not_needed", str(ctx.exception))

    def test_baseline_evidence_drift_is_not_reusable(self):
        record = self._ingested_base_record()
        path = self.write_json("base.json", record)
        with open(self.evidence, "w", encoding="utf-8") as fh:
            fh.write("drift after pass\n")
        payload = NA.build_review_context(
            self.context_args(previous_review=path))
        self.assertEqual(payload["review_scope"], "full")
        self.assertIn("native_review_attempt_workspace_drift",
                      payload["previous_review"]["trust_problems"])
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA._incremental_base(G.load_state(self.state_path), path)
        self.assertIn("incremental_base_ineligible", str(ctx.exception))

    def test_build_review_rejects_old_session_for_new_attempt(self):
        attempt = self.attempt()
        old = "2000-01-01T00:00:00+00:00"
        host = F.make_fake_review_host(
            self.dir, execution=self.execution, dispatch=self.dispatch,
            evidence=self.evidence, task_id=self.state["task_id"],
            attempt_id=attempt["review_attempt_id"],
            session_created_at=old)
        args = types.SimpleNamespace(
            state=self.state_path,
            dispatch=self.write_json("dispatch.json", self.dispatch),
            execution=self.write_json("execution.json", self.execution),
            work_item="A1", metadata=host["metadata_path"],
            model_rollout=host["rollout_path"], input="", output="",
            attempt=attempt["review_attempt_id"], based_on="")
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review(args)
        self.assertIn("review_attempt_session_invalid", str(ctx.exception))

    # ── 基本 PASS ─────────────────────────────────────────────────────────
    def test_s0_normal_pass_is_accepted(self):
        attempt = self.attempt()
        env = self.envelope(attempt)
        self.ingest(env)
        state = G.load_state(self.state_path)
        gate = G._gate_review(state)
        self.assertTrue(gate["passed"], gate.get("reasons"))
        bound = G.find_review_attempt(state, attempt["review_attempt_id"])
        self.assertTrue(bound["consumed"])
        self.assertEqual(bound["consumed_by_result_sha256"], env["result_sha256"])

    # ── S0→S1：旧结论过期 ────────────────────────────────────────────────
    def test_code_change_after_review_is_stale(self):
        self.ingest(self.envelope(self.attempt()))
        self.add_code_change()
        codes = {r["code"] for r in G._gate_review(
            G.load_state(self.state_path))["reasons"]}
        self.assertIn("native_review_stale", codes)

    def test_evidence_change_after_review_is_stale(self):
        self.ingest(self.envelope(self.attempt()))
        state = G.load_state(self.state_path)
        G.register_evidence(state, "E2", text="新增证据")
        G.save_state(self.state_path, state)
        codes = {r["code"] for r in G._gate_review(
            G.load_state(self.state_path))["reasons"]}
        self.assertIn("native_review_stale", codes)

    def test_contract_change_after_review_is_stale(self):
        self.ingest(self.envelope(self.attempt()))
        G.amend_contract(self.state_path,
                         contract=CONTRACT + "\n- 新增边界：不修改无关项目\n")
        codes = {r["code"] for r in G._gate_review(
            G.load_state(self.state_path))["reasons"]}
        self.assertTrue({"native_review_contract_mismatch",
                         "native_review_stale"} & codes, codes)

    # ── 会话 / attempt 绑定 ───────────────────────────────────────────────
    def test_session_mismatch_is_rejected_at_ingest(self):
        attempt = self.attempt()
        state = G.load_state(self.state_path)
        bound = G.find_review_attempt(state, attempt["review_attempt_id"])
        bound["reviewer"] = {"agent_id": "agent-review",
                             "child_session_id": "child-other"}
        G.save_state(self.state_path, state)
        with self.assertRaises(AC.AutoError) as ctx:
            self.ingest(self.envelope(attempt))
        self.assertIn("native_review_attempt_session_mismatch", str(ctx.exception))
        state = G.load_state(self.state_path)
        self.assertFalse(G.find_review_attempt(
            state, attempt["review_attempt_id"])["consumed"])

    def test_old_session_cannot_claim_new_attempt(self):
        attempt = self.attempt()
        old = "2000-01-01T00:00:00+00:00"
        with self.assertRaises(AC.AutoError) as ctx:
            self.ingest(self.envelope(attempt, session_created_at=old))
        self.assertIn("review_attempt_session_precedes_attempt", str(ctx.exception))

    def test_attempt_replay_is_rejected(self):
        attempt = self.attempt()
        first = self.envelope(attempt)
        self.ingest(first)
        replay = self.envelope(attempt, residual_risks=["重放的第二份输出"])
        with self.assertRaises(AC.AutoError) as ctx:
            self.ingest(replay)
        self.assertIn("native_review_attempt_consumed", str(ctx.exception))
        state = G.load_state(self.state_path)
        self.assertEqual(len(state.get("native_reviews") or []), 1)

    def test_old_output_re_enveloped_with_new_target_is_rejected(self):
        """核心判别：宿主重算当前 SHA 也不能把旧输出变成新快照结论。"""
        attempt = self.attempt()
        old = self.envelope(attempt)
        self.add_code_change()
        current = G.final_evidence_digest(G.load_state(self.state_path))
        forged = json.loads(json.dumps(old))
        forged["target_sha256"] = current
        forged["result_sha256"] = NB.envelope_sha256(
            {k: v for k, v in forged.items() if k != "result_sha256"})
        with self.assertRaises(AC.AutoError) as ctx:
            self.ingest(forged)
        self.assertIn("native_review_stale", str(ctx.exception))
        state = G.load_state(self.state_path)
        self.assertFalse(G.find_review_attempt(
            state, attempt["review_attempt_id"])["consumed"])

    def test_missing_attempt_is_rejected(self):
        env = self.envelope(self.attempt())
        env.pop("review_attempt_id")
        env.pop("review_attempt_created_at")
        env["result_sha256"] = NB.envelope_sha256(
            {k: v for k, v in env.items() if k != "result_sha256"})
        with self.assertRaises(NB.BridgeError) as ctx:
            NB.validate_review_result(env, self.dispatch, self.execution,
                                      G.load_state(self.state_path))
        self.assertEqual(ctx.exception.code, "review_attempt_missing")

    # ── 复核期间工作区漂移 ────────────────────────────────────────────────
    def test_evidence_file_drift_during_review_is_rejected(self):
        attempt = self.attempt()
        env = self.envelope(attempt)
        with open(self.evidence, "w", encoding="utf-8") as fh:
            fh.write("changed while reviewing\n")
        with self.assertRaises(AC.AutoError) as ctx:
            self.ingest(env)
        self.assertIn("native_review_attempt_workspace_drift", str(ctx.exception))

    # ── 新快照新建 attempt ────────────────────────────────────────────────
    def test_new_snapshot_new_attempt_is_allowed(self):
        self.attempt()  # 旧 attempt 保持未消费，模拟旧派发被放弃
        self.write_source("src/feature.py", "VALUE = 2\n")
        fresh = self.attempt()
        self.ingest(self.envelope(fresh))
        gate = G._gate_review(G.load_state(self.state_path))
        self.assertTrue(gate["passed"], gate.get("reasons"))

    def test_attempt_cannot_bind_superseded_execution(self):
        AC._ingest_execution(self.state_path, self.execution)
        G.rerun_action(self.state_path, "A1", reason="重跑")
        second = json.loads(json.dumps(self.execution))
        second["result"] = "重跑后的结果"
        second.pop("result_sha256", None)
        second["result_sha256"] = NB.envelope_sha256(second)
        AC._ingest_execution(self.state_path, second)
        with self.assertRaises(G.GovernanceError) as ctx:
            G.create_review_attempt(
                self.state_path, work_item_id="A1",
                execution_result_sha256=self.execution["result_sha256"],
                workspace=self.dir)
        self.assertIn("旧结果", str(ctx.exception))

    def test_build_review_rejects_wrong_workspace(self):
        attempt = self.attempt()
        other = os.path.join(self.dir, "other-workspace")
        os.makedirs(other)
        host = F.make_fake_review_host(
            self.dir, execution=self.execution, dispatch=self.dispatch,
            evidence=self.evidence, task_id=self.state["task_id"],
            attempt_id=attempt["review_attempt_id"],
            session_created_at=attempt["created_at"], workspace=other)
        args = types.SimpleNamespace(
            state=self.state_path,
            dispatch=self.write_json("dispatch.json", self.dispatch),
            execution=self.write_json("execution.json", self.execution),
            work_item="A1", metadata=host["metadata_path"],
            model_rollout=host["rollout_path"], input="", output="",
            attempt=attempt["review_attempt_id"], based_on="")
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review(args)
        self.assertIn("review_attempt_workspace_mismatch", str(ctx.exception))

    def test_attempt_workspace_must_match_evidence_repository(self):
        other = os.path.join(self.dir, "other-repo")
        F.init_git_workspace(other)
        with self.assertRaises(G.GovernanceError) as ctx:
            G.create_review_attempt(
                self.state_path, work_item_id="A1",
                execution_result_sha256=self.execution["result_sha256"],
                workspace=other)
        self.assertIn("证据所属仓库不一致", str(ctx.exception))

    def test_failed_ingest_does_not_consume_attempt_and_can_retry(self):
        attempt = self.attempt()
        with self.assertRaises(AC.AutoError) as ctx:
            self.ingest(self.envelope(attempt, prompt="旧会话 prompt"))
        self.assertIn("review_attempt_prompt_unbound", str(ctx.exception))
        state = G.load_state(self.state_path)
        self.assertFalse(G.find_review_attempt(
            state, attempt["review_attempt_id"])["consumed"])
        # 修正宿主 prompt 后重新生成信封即可回填，不需要新建 attempt。
        self.ingest(self.envelope(attempt))
        state = G.load_state(self.state_path)
        self.assertTrue(G.find_review_attempt(
            state, attempt["review_attempt_id"])["consumed"])

    def test_duplicate_ingest_is_idempotent(self):
        attempt = self.attempt()
        env = self.envelope(attempt)
        self.ingest(env)
        self.ingest(env)
        state = G.load_state(self.state_path)
        self.assertEqual(len(state.get("native_reviews") or []), 1)


if __name__ == "__main__":
    unittest.main()

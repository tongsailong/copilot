#!/usr/bin/env python3
"""v6 CLI 回归：本地治理命令、自动驱动退出码、原生回填与 G4 宿主核对。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import governance as G  # noqa: E402
import native_bridge as NB  # noqa: E402
import v6_support as F  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""

PLAN = {"summary": "实现并验证", "actions": [
    {"id": "A1", "title": "实现", "goal": "产出结果",
     "expected_evidence": "结果文件"},
]}


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str, payload) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


class LegacyCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state = os.path.join(self.dir, "state.json")
        self.contract = os.path.join(self.dir, "contract.md")
        with open(self.contract, "w", encoding="utf-8") as fh:
            fh.write(CONTRACT)

    def start(self, *extra: str):
        return F.run_cli("copilot_dispatch.py", "start", "--state", self.state,
                         "--goal", "目标", "--contract-file", self.contract, *extra)

    def test_legacy_flow_reaches_closed_via_drive(self):
        proc = self.start()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plan = write_json(os.path.join(self.dir, "plan.json"), PLAN)
        self.assertEqual(F.run_cli("copilot_dispatch.py", "plan", "--state", self.state,
                                   "--plan", plan).returncode, 0)

        proc = F.run_cli("copilot_auto.py", "drive", "--state", self.state)
        self.assertEqual(proc.returncode, 10, proc.stderr)
        self.assertIn("工作项 A1", proc.stdout)
        self.assertIn("授权提醒", proc.stdout)

        evidence = os.path.join(self.dir, "result.txt")
        with open(evidence, "w", encoding="utf-8") as fh:
            fh.write("done\n")
        self.assertEqual(F.run_cli("copilot_dispatch.py", "evidence", "--state", self.state,
                                   "--add", "--id", "E1", "--file", evidence).returncode, 0)
        self.assertEqual(F.run_cli("copilot_dispatch.py", "record", "--state", self.state,
                                   "--id", "A1", "--status", "done",
                                   "--result", "完成", "--evidence", "E1").returncode, 0)
        self.assertEqual(F.run_cli("copilot_dispatch.py", "criterion", "--state", self.state,
                                   "--id", "C1", "--status", "met",
                                   "--evidence", "E1").returncode, 0)

        proc = F.run_cli("copilot_dispatch.py", "closure-check", "--state", self.state,
                         "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["passed"])

        proc = F.run_cli("copilot_auto.py", "drive", "--state", self.state)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("四门通过", proc.stdout)
        data = read_json(self.state)
        self.assertTrue(data["closure"]["closed"])

        proc = F.run_cli("copilot_auto.py", "drive", "--state", self.state)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("CLOSED", proc.stdout)

    def test_drive_derives_partial_work_without_plan(self):
        self.assertEqual(self.start().returncode, 0)
        proc = F.run_cli("copilot_auto.py", "drive", "--state", self.state)
        self.assertEqual(proc.returncode, 10, proc.stderr)
        self.assertIn("AUTO-C1", proc.stdout)
        data = read_json(self.state)
        self.assertEqual(data["actions"][0]["origin"], "partial-derived")

    def test_capture_cmd_registers_redacted_evidence_file(self):
        self.assertEqual(self.start().returncode, 0)
        proc = F.run_cli("copilot_dispatch.py", "evidence", "--state", self.state,
                         "--add", "--id", "E1", "--capture-cmd", "printf 'hello\\n'",
                         "--note", "命令输出")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        captured = os.path.join(self.dir, "evidence", "E1.md")
        self.assertTrue(os.path.isfile(captured))
        data = read_json(self.state)
        entry = data["evidence"][0]
        self.assertEqual(entry["kind"], "capture")
        self.assertEqual(entry["path_hint"], captured)
        with open(captured, "r", encoding="utf-8") as fh:
            self.assertIn("hello", fh.read())

    def test_cli_errors_exit_2_and_do_not_corrupt_state(self):
        self.assertEqual(self.start().returncode, 0)
        before = read_json(self.state)["save_seq"]
        proc = self.start()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("已存在", proc.stderr)
        proc = F.run_cli("copilot_dispatch.py", "record", "--state", self.state,
                         "--id", "A9", "--status", "done", "--result", "x")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(read_json(self.state)["save_seq"], before)

    def test_block_and_unblock_cli(self):
        self.assertEqual(self.start().returncode, 0)
        self.assertEqual(F.run_cli("copilot_dispatch.py", "block", "--state", self.state,
                                   "--reason", "缺授权", "--needs", "用户授权").returncode, 0)
        self.assertEqual(F.run_cli("copilot_auto.py", "drive",
                                   "--state", self.state).returncode, 11)
        self.assertEqual(F.run_cli("copilot_dispatch.py", "unblock", "--state", self.state,
                                   "--reason", "用户已授权").returncode, 0)

    def test_status_alias_reports_phase(self):
        self.assertEqual(self.start().returncode, 0)
        for name in ("status", "next", "brief", "render"):
            proc = F.run_cli("copilot_dispatch.py", name, "--state", self.state,
                             "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["phase"], "PARTIAL")

    def test_high_risk_with_legacy_backend_is_rejected_at_start(self):
        proc = self.start("--risk", "high")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--backend native", proc.stderr)
        self.assertFalse(os.path.exists(self.state))

    def test_review_authorization_requires_explicit_user_declaration(self):
        self.assertEqual(self.start().returncode, 0)
        proc = F.run_cli("copilot_dispatch.py", "authorize-review",
                         "--state", self.state, "--reason", "用户要求复核")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("authorized-by", proc.stderr)
        proc = F.run_cli("copilot_dispatch.py", "authorize-review",
                         "--state", self.state, "--authorized-by", "zcode",
                         "--reason", "自行决定")
        self.assertEqual(proc.returncode, 2)

    def test_unblock_requires_block_id_when_multiple(self):
        self.assertEqual(self.start().returncode, 0)
        self.assertEqual(F.run_cli(
            "copilot_dispatch.py", "block", "--state", self.state,
            "--reason", "缺授权", "--needs", "用户授权").returncode, 0)
        first = read_json(self.state)["blocks"][0]["block_id"]
        self.assertEqual(F.run_cli(
            "copilot_dispatch.py", "block", "--state", self.state,
            "--reason", "缺资源", "--needs", "外部资源").returncode, 0)
        second = read_json(self.state)["blocks"][1]["block_id"]

        proc = F.run_cli("copilot_dispatch.py", "unblock",
                         "--state", self.state, "--reason", "一条理由")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--block-id", proc.stderr)
        self.assertEqual(F.run_cli(
            "copilot_dispatch.py", "unblock", "--state", self.state,
            "--reason", "授权已到", "--block-id", first).returncode, 0)
        active = [b for b in read_json(self.state)["blocks"]
                  if not b.get("resolved_at")]
        self.assertEqual([b["block_id"] for b in active], [second])
        self.assertEqual(F.run_cli(
            "copilot_dispatch.py", "unblock", "--state", self.state,
            "--reason", "资源已到", "--block-id", second).returncode, 0)

    def test_formal_review_amendment_cli(self):
        self.assertEqual(self.start().returncode, 0)
        self.assertEqual(F.run_cli(
            "copilot_dispatch.py", "authorize-review", "--state", self.state,
            "--authorized-by", "user", "--reason", "用户要求正式复核").returncode, 0)
        proc = F.run_cli("copilot_dispatch.py", "formal-review",
                         "--state", self.state, "--verdict", "pass",
                         "--summary", "复核通过")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        review_id = read_json(self.state)["formal_reviews"][0]["review_id"]

        proc = F.run_cli("copilot_dispatch.py", "formal-review",
                         "--state", self.state, "--amend", review_id,
                         "--review-ref", "E9（编号更正）",
                         "--reason", "仅更正引用，结论未变")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("amendment", proc.stdout)
        state = read_json(self.state)
        self.assertEqual(len(state["formal_reviews"]), 1)
        self.assertEqual(len(state["formal_reviews"][0]["amendments"]), 1)

        evidence = os.path.join(self.dir, "new.txt")
        with open(evidence, "w", encoding="utf-8") as fh:
            fh.write("new evidence\n")
        self.assertEqual(F.run_cli(
            "copilot_dispatch.py", "evidence", "--state", self.state,
            "--add", "--id", "E1", "--file", evidence).returncode, 0)
        proc = F.run_cli("copilot_dispatch.py", "formal-review",
                         "--state", self.state, "--amend", review_id,
                         "--review-ref", "E1", "--reason", "证据已变化")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("目标摘要已变化", proc.stderr)

    def test_legacy_state_is_read_only_through_cli(self):
        legacy = os.path.join(self.dir, "legacy.json")
        write_json(legacy, {"version": 2, "task_id": "task-old", "goal": "旧目标",
                            "rounds": [{"round_id": "r1", "verdict": "PARTIAL",
                                        "call_id": "c1"}]})
        proc = F.run_cli("copilot_dispatch.py", "legacy-status", "--state", legacy,
                         "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["retired"])
        self.assertEqual(payload["state"]["rounds"][0]["verdict"], "PARTIAL")
        proc = F.run_cli("copilot_dispatch.py", "next", "--state", legacy)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("legacy-status", proc.stderr)


class NativeCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state = os.path.join(self.dir, "state.json")
        self.contract = os.path.join(self.dir, "contract.md")
        with open(self.contract, "w", encoding="utf-8") as fh:
            fh.write(CONTRACT)
        proc = F.run_cli("copilot_dispatch.py", "start", "--state", self.state,
                         "--goal", "目标", "--contract-file", self.contract,
                         "--risk", "high", "--backend", "native")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plan = write_json(os.path.join(self.dir, "plan.json"), PLAN)
        self.assertEqual(F.run_cli("copilot_dispatch.py", "plan", "--state", self.state,
                                   "--plan", plan).returncode, 0)
        self.evidence = os.path.join(self.dir, "result.txt")
        with open(self.evidence, "w", encoding="utf-8") as fh:
            fh.write("done\n")
        self.dispatch_path = os.path.join(self.dir, "dispatch.json")

    def _execution(self, dispatch: dict) -> dict:
        item = dispatch["actions"][0]
        state = read_json(self.state)
        env = {
            "schema": NB.RESULT_SCHEMA,
            "task_id": state["task_id"],
            "contract_sha256": state["contract_sha256"],
            "plan_id": dispatch["plan_id"],
            "plan_sha256": dispatch["plan_sha256"],
            "iteration_id": dispatch["iteration_id"],
            "dispatch_id": item["dispatch_id"],
            "idempotency_key": item["idempotency_key"],
            "work_item_id": item["work_item_id"],
            "status": "done",
            "summary": "完成",
            "result": "实际结果：文件已生成",
            "evidence": [{"id": "E1", "file": self.evidence,
                          "sha256": NB.sha256_file(self.evidence), "kind": "file"}],
            "executor": {"kind": "subagent", "agent_id": "agent-exec",
                         "child_session_id": "child-exec",
                         "profile_id": "executor", "model_reported": "m"},
            "timing": {"duration_ms": 10},
            "usage": {"total_tokens": 42},
        }
        env["result_sha256"] = NB.envelope_sha256(env)
        return env

    def _review(self, dispatch: dict, execution: dict) -> dict:
        state = read_json(self.state)
        attempt = G.create_review_attempt(
            self.state, work_item_id=str(execution["work_item_id"]),
            execution_result_sha256=str(execution["result_sha256"]),
            workspace=self.dir)
        host = F.make_fake_review_host(self.dir, execution=execution,
                                       dispatch=dispatch, evidence=self.evidence,
                                       task_id=state["task_id"],
                                       attempt_id=attempt["review_attempt_id"],
                                       session_created_at=attempt["created_at"])
        env = {
            "schema": NB.REVIEW_SCHEMA,
            "task_id": state["task_id"],
            "contract_sha256": state["contract_sha256"],
            "plan_id": dispatch["plan_id"],
            "plan_sha256": dispatch["plan_sha256"],
            "dispatch_id": execution["dispatch_id"],
            "work_item_id": execution["work_item_id"],
            "execution_result_sha256": execution["result_sha256"],
            "target_sha256": G.final_evidence_digest(G.load_state(self.state)),
            "review_attempt_id": attempt["review_attempt_id"],
            "review_attempt_created_at": attempt["created_at"],
            "reviewer": {"kind": "subagent", "agent_id": host["agent_id"],
                         "child_session_id": host["child_session_id"],
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
        env["result_sha256"] = NB.envelope_sha256(env)
        return env

    def test_native_flow_ingests_execution_and_g4_review(self):
        proc = F.run_cli("copilot_auto.py", "drive", "--state", self.state,
                         "--dispatch-out", self.dispatch_path)
        self.assertEqual(proc.returncode, 10, proc.stderr)
        dispatch = read_json(self.dispatch_path)
        self.assertEqual(dispatch["schema"], "copilot-native-dispatch/v2")
        self.assertEqual(dispatch["plan_id"], read_json(self.state)["plans"][0]["plan_id"])

        execution = self._execution(dispatch)
        result_file = write_json(os.path.join(self.dir, "execution.json"), execution)
        proc = F.run_cli("copilot_auto.py", "ingest", "--state", self.state,
                         "--result-file", result_file)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("A1", proc.stdout)

        proc = F.run_cli("copilot_auto.py", "ingest", "--state", self.state,
                         "--result-file", result_file)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("幂等", proc.stdout)

        self.assertEqual(F.run_cli("copilot_dispatch.py", "criterion", "--state", self.state,
                                   "--id", "C1", "--status", "met",
                                   "--evidence", "E1").returncode, 0)

        proc = F.run_cli("copilot_auto.py", "drive", "--state", self.state,
                         "--dispatch-out", self.dispatch_path)
        self.assertEqual(proc.returncode, 10, proc.stderr)
        self.assertIn("AUTO-G4", proc.stdout)
        self.assertEqual(read_json(self.state)["native_dispatch"]["plan_id"],
                         dispatch["plan_id"])

        review = self._review(dispatch, execution)
        review_file = write_json(os.path.join(self.dir, "review.json"), review)
        proc = F.run_cli("copilot_auto.py", "ingest", "--state", self.state,
                         "--review-file", review_file)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("g4_eligible=True", proc.stdout)

        self.assertEqual(F.run_cli("copilot_dispatch.py", "record", "--state", self.state,
                                   "--id", "AUTO-G4", "--status", "done",
                                   "--result", "复核完成").returncode, 0)
        proc = F.run_cli("copilot_auto.py", "drive", "--state", self.state)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(read_json(self.state)["closure"]["closed"])

    def test_tampered_execution_envelope_is_rejected(self):
        F.run_cli("copilot_auto.py", "drive", "--state", self.state,
                  "--dispatch-out", self.dispatch_path)
        dispatch = read_json(self.dispatch_path)
        execution = self._execution(dispatch)
        execution["result"] = "被替换"
        result_file = write_json(os.path.join(self.dir, "bad.json"), execution)
        proc = F.run_cli("copilot_auto.py", "ingest", "--state", self.state,
                         "--result-file", result_file)
        self.assertEqual(proc.returncode, 12)
        self.assertIn("result_sha_mismatch", proc.stderr)
        self.assertEqual(read_json(self.state)["native_results"], [])

    def test_review_with_changed_host_profile_is_rejected(self):
        F.run_cli("copilot_auto.py", "drive", "--state", self.state,
                  "--dispatch-out", self.dispatch_path)
        dispatch = read_json(self.dispatch_path)
        execution = self._execution(dispatch)
        result_file = write_json(os.path.join(self.dir, "execution.json"), execution)
        self.assertEqual(F.run_cli("copilot_auto.py", "ingest", "--state", self.state,
                                   "--result-file", result_file).returncode, 0)
        self.assertEqual(F.run_cli("copilot_dispatch.py", "criterion", "--state", self.state,
                                   "--id", "C1", "--status", "met",
                                   "--evidence", "E1").returncode, 0)
        F.run_cli("copilot_auto.py", "drive", "--state", self.state,
                  "--dispatch-out", self.dispatch_path)
        review = self._review(dispatch, execution)
        meta_path = review["host"]["metadata_path"]
        metadata = read_json(meta_path)
        metadata["profileSnapshot"]["tools"] = ["Read", "Write"]
        write_json(meta_path, metadata)
        review_file = write_json(os.path.join(self.dir, "review.json"), review)
        proc = F.run_cli("copilot_auto.py", "ingest", "--state", self.state,
                         "--review-file", review_file)
        self.assertEqual(proc.returncode, 12)
        self.assertIn("G4", proc.stderr)
        self.assertEqual(read_json(self.state)["native_reviews"], [])


if __name__ == "__main__":
    unittest.main()

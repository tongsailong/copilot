#!/usr/bin/env python3
"""CLI 回归：本地治理命令、自动驱动退出码与原生执行信封回填。"""
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
        # plan 之后仍有可执行工作项：命令以 EXIT_CONTINUE 提示继续，不冒充终点。
        self.assertEqual(F.run_cli("copilot_dispatch.py", "plan", "--state", self.state,
                                   "--plan", plan).returncode, 10)

        proc = F.run_cli("copilot_auto.py", "drive", "--state", self.state)
        self.assertEqual(proc.returncode, 10, proc.stderr)
        self.assertIn("工作项 A1", proc.stdout)
        self.assertIn("授权提醒", proc.stdout)

        evidence = os.path.join(self.dir, "result.txt")
        with open(evidence, "w", encoding="utf-8") as fh:
            fh.write("done\n")
        self.assertEqual(F.run_cli("copilot_dispatch.py", "evidence", "--state", self.state,
                                   "--add", "--id", "E1", "--file", evidence).returncode, 0)
        # 回填后仍有续跑判定：record/criterion 用 EXIT_CONTINUE 提示继续，而不是
        # 让调用方把一次回填当成任务终点。
        proc = F.run_cli("copilot_dispatch.py", "record", "--state", self.state,
                         "--id", "A1", "--status", "done",
                         "--result", "完成", "--evidence", "E1")
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
        self.assertIn("续跑", proc.stdout)
        proc = F.run_cli("copilot_dispatch.py", "criterion", "--state", self.state,
                         "--id", "C1", "--status", "met", "--evidence", "E1")
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
        self.assertIn("close", proc.stdout)

        proc = F.run_cli("copilot_dispatch.py", "closure-check", "--state", self.state,
                         "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["passed"])

        proc = F.run_cli("copilot_auto.py", "drive", "--state", self.state)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("G1–G3 通过", proc.stdout)
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
        # 无工作项可指向时只能声明任务级阻塞；解阻后未闭合条件立即要求续跑。
        self.assertEqual(F.run_cli("copilot_dispatch.py", "block", "--state", self.state,
                                   "--reason", "缺授权", "--needs", "用户授权",
                                   "--scope", "task").returncode, 0)
        self.assertEqual(F.run_cli("copilot_auto.py", "drive",
                                   "--state", self.state).returncode, 11)
        proc = F.run_cli("copilot_dispatch.py", "unblock", "--state", self.state,
                         "--reason", "用户已授权")
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)

    def test_status_alias_reports_phase(self):
        self.assertEqual(self.start().returncode, 0)
        for name in ("status", "next"):
            proc = F.run_cli("copilot_dispatch.py", name, "--state", self.state,
                             "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["phase"], "PARTIAL")

    def test_high_risk_with_legacy_backend_starts(self):
        # v8：G4 退役后高风险任务不再强制 native 后端。
        proc = self.start("--risk", "high",
                          "--test-policy", '{"required": false}')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(read_json(self.state)["execution_backend"], "legacy")

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
            "--reason", "缺授权", "--needs", "用户授权",
            "--scope", "task").returncode, 0)
        first = read_json(self.state)["blocks"][0]["block_id"]
        self.assertEqual(F.run_cli(
            "copilot_dispatch.py", "block", "--state", self.state,
            "--reason", "缺资源", "--needs", "外部资源",
            "--scope", "task").returncode, 0)
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
        # 全部阻塞解除后仍未闭合条件：必须继续派生，而不是停在 0。
        proc = F.run_cli(
            "copilot_dispatch.py", "unblock", "--state", self.state,
            "--reason", "资源已到", "--block-id", second)
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
        self.assertIn("必须继续", proc.stdout)

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


class InlineEvidenceCliTests(unittest.TestCase):
    """`record`/`criterion` 内联登记证据：每条件一次 CLI 而不是两次。

    真实使用里每核销一条完成条件都要 `evidence --add` + `criterion --evidence`
    两次调用；内联形式把两步合成一步，同时保留失败关闭：目标不存在时不留下无主证据。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state = os.path.join(self.dir, "state.json")
        contract = os.path.join(self.dir, "contract.md")
        with open(contract, "w", encoding="utf-8") as fh:
            fh.write("## 完成条件\n- 结果可复核\n")
        proc = F.run_cli("copilot_dispatch.py", "start", "--state", self.state,
                         "--goal", "内联证据", "--contract-file", contract,
                         "--test-policy", '{"required": false}')
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def run_dispatch(self, *args: str):
        return F.run_cli("copilot_dispatch.py", *args)

    def state_data(self) -> dict:
        return read_json(self.state)

    def test_criterion_registers_text_evidence_in_one_call(self):
        proc = self.run_dispatch("criterion", "--state", self.state, "--id", "C1",
                                 "--status", "met", "--text", "单测 12 项通过")
        self.assertIn(proc.returncode, (0, 10), proc.stderr)
        self.assertIn("C1 → met", proc.stdout)
        data = self.state_data()
        self.assertEqual(len(data["evidence"]), 1)
        entry = data["evidence"][0]
        self.assertEqual(entry["kind"], "text")
        criterion = data["contract"]["criteria"][0]
        self.assertEqual(criterion["status"], "met")
        self.assertEqual([ref["id"] for ref in criterion["evidence_refs"]],
                         [entry["id"]])
        self.assertEqual(entry["id"], "E1")

    def test_record_registers_capture_evidence_in_one_call(self):
        plan = write_json(os.path.join(self.dir, "plan.json"), PLAN)
        self.assertIn(self.run_dispatch("plan", "--state", self.state,
                                        "--plan", plan).returncode, (0, 10))
        proc = self.run_dispatch("record", "--state", self.state, "--id", "A1",
                                 "--status", "done", "--result", "产出结果文件",
                                 "--capture-cmd", "printf 'hello\\n'")
        self.assertIn(proc.returncode, (0, 10), proc.stderr)
        data = self.state_data()
        entry = data["evidence"][0]
        self.assertEqual(entry["kind"], "capture")
        captured = os.path.join(self.dir, "evidence", entry["id"] + ".md")
        self.assertEqual(entry["path_hint"], captured)
        with open(captured, "r", encoding="utf-8") as fh:
            self.assertIn("hello", fh.read())
        self.assertEqual(data["actions"][0]["evidence_ids"], [entry["id"]])

    def test_inline_evidence_merges_with_existing_ids(self):
        first = self.run_dispatch("evidence", "--state", self.state, "--add",
                                  "--id", "E1", "--text", "第一次")
        self.assertEqual(first.returncode, 0, first.stderr)
        proc = self.run_dispatch("criterion", "--state", self.state, "--id", "C1",
                                 "--status", "met", "--evidence", "E1",
                                 "--text", "第二次", "--evidence-id", "E2")
        self.assertIn(proc.returncode, (0, 10), proc.stderr)
        criterion = self.state_data()["contract"]["criteria"][0]
        self.assertEqual([ref["id"] for ref in criterion["evidence_refs"]],
                         ["E1", "E2"])

    def test_failed_record_does_not_persist_inline_evidence(self):
        # 证据与回填在同一次保存里生效：回填被治理层拒绝时不能留下证据记录。
        plan = write_json(os.path.join(self.dir, "plan.json"), PLAN)
        self.assertIn(self.run_dispatch("plan", "--state", self.state,
                                        "--plan", plan).returncode, (0, 10))
        before = self.state_data()["save_seq"]
        proc = self.run_dispatch("record", "--state", self.state, "--id", "A1",
                                 "--status", "done", "--text", "不该留下")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("必须说明实际结果", proc.stderr)
        self.assertNotIn("已登记（内联）", proc.stdout)
        data = self.state_data()
        self.assertEqual(data["save_seq"], before)
        self.assertEqual(data.get("evidence") or [], [])
        self.assertEqual(data["actions"][0]["status"], "pending")

    def test_unknown_target_registers_no_evidence(self):
        proc = self.run_dispatch("criterion", "--state", self.state, "--id", "C9",
                                 "--status", "met", "--text", "x")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("不存在完成条件", proc.stderr)
        self.assertEqual(self.state_data().get("evidence") or [], [])

    def test_conflicting_sources_are_rejected(self):
        path = os.path.join(self.dir, "out.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("x")
        proc = self.run_dispatch("criterion", "--state", self.state, "--id", "C1",
                                 "--status", "met", "--text", "x", "--file", path)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("只能选一个", proc.stderr)
        self.assertEqual(self.state_data().get("evidence") or [], [])

    def test_capture_cannot_overwrite_registered_evidence(self):
        first = self.run_dispatch("criterion", "--state", self.state, "--id", "C1",
                                  "--status", "met", "--capture-cmd", "printf one")
        self.assertIn(first.returncode, (0, 10), first.stderr)
        second = self.run_dispatch("criterion", "--state", self.state, "--id", "C1",
                                   "--status", "met", "--capture-cmd", "printf two",
                                   "--evidence-id", "E1")
        self.assertEqual(second.returncode, 2)
        self.assertIn("会覆盖证据文件", second.stderr)
        with open(os.path.join(self.dir, "evidence", "E1.md"), "r",
                  encoding="utf-8") as fh:
            self.assertIn("one", fh.read())


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
                         "--risk", "high", "--backend", "native",
                         "--test-policy", '{"required": false}')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plan = write_json(os.path.join(self.dir, "plan.json"), PLAN)
        # plan 之后仍有可执行工作项：命令以 EXIT_CONTINUE 提示继续，不冒充终点。
        self.assertEqual(F.run_cli("copilot_dispatch.py", "plan", "--state", self.state,
                                   "--plan", plan).returncode, 10)
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
                         "profile_id": "executor",
                         "profile": {"tools": ["*"]},
                         "model_reported": "m"},
            "timing": {"duration_ms": 10},
            "usage": {"total_tokens": 42},
        }
        env["result_sha256"] = NB.envelope_sha256(env)
        return env

    def test_native_flow_ingests_execution_and_closes_without_review(self):
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

        proc = F.run_cli("copilot_dispatch.py", "criterion", "--state", self.state,
                         "--id", "C1", "--status", "met", "--evidence", "E1")
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
        self.assertIn("close", proc.stdout)

        # G4 退役：证据与条件齐备后直接收口，不再派生 AUTO-G4。
        proc = F.run_cli("copilot_auto.py", "drive", "--state", self.state,
                         "--dispatch-out", self.dispatch_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = read_json(self.state)
        self.assertTrue(state["closure"]["closed"])
        self.assertEqual(state["closure"]["verdict"], "pass")
        self.assertFalse([a for a in state["actions"]
                          if str(a.get("id", "")).startswith("AUTO-G4")])

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

    def test_review_file_flag_is_removed(self):
        result_file = write_json(os.path.join(self.dir, "execution.json"),
                                 self._execution(read_json(self.state) and {
                                     "actions": [], "plan_id": "",
                                     "plan_sha256": ""}) if False else {})
        proc = F.run_cli("copilot_auto.py", "ingest", "--state", self.state,
                         "--result-file", result_file,
                         "--review-file", result_file)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unrecognized", proc.stderr)


if __name__ == "__main__":
    unittest.main()

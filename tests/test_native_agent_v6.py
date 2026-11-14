#!/usr/bin/env python3
"""v6 原生复核生成与驱动保护：G4 资格必须由宿主记录计算，驱动上限不是任务终点。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import copilot_auto as auto  # noqa: E402
import governance as G  # noqa: E402
import native_agent as NA  # noqa: E402
import native_bridge as NB  # noqa: E402
import v6_support as F  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""

PLAN = {"summary": "实现", "actions": [
    {"id": "A1", "title": "实现", "goal": "产出结果",
     "expected_evidence": "结果文件"},
]}


def write_json(path: str, payload) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


class BuildReviewTests(unittest.TestCase):
    """`native_agent.py review` 的 G4 资格必须来自 metadata/rollout，而不是自报。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="目标", contract=CONTRACT,
                      execution_backend="native", risk="high")
        self.plan_path = write_json(os.path.join(self.dir, "plan.json"), PLAN)
        G.plan_state(self.state_path, PLAN)
        state = G.load_state(self.state_path)
        self.dispatch = NB.build_dispatch(state, G.ready_actions(state))
        state["native_dispatch"] = self.dispatch
        G.save_state(self.state_path, state)
        self.dispatch_path = write_json(
            os.path.join(self.dir, "dispatch.json"), self.dispatch)
        self.evidence = os.path.join(self.dir, "result.txt")
        with open(self.evidence, "w", encoding="utf-8") as fh:
            fh.write("done\n")
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
            "work_item_id": item["work_item_id"],
            "status": "done",
            "summary": "完成",
            "result": "实际结果",
            "evidence": [{"id": "E1", "file": self.evidence,
                          "sha256": NB.sha256_file(self.evidence), "kind": "file"}],
            "executor": {"kind": "subagent", "agent_id": "agent-exec",
                         "child_session_id": "child-exec",
                         "profile_id": "executor", "model_reported": "m"},
            "timing": {"duration_ms": 10},
            "usage": {"total_tokens": 10},
        }
        self.execution["result_sha256"] = NB.envelope_sha256(self.execution)
        self.execution_path = write_json(
            os.path.join(self.dir, "execution.json"), self.execution)
        proc = F.run_cli("copilot_auto.py", "ingest", "--state", self.state_path,
                         "--result-file", self.execution_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.state = G.load_state(self.state_path)
        self.attempt = G.create_review_attempt(
            self.state_path, work_item_id="A1",
            execution_result_sha256=self.execution["result_sha256"],
            workspace=self.dir)
        self.host = F.make_fake_review_host(
            self.dir, execution=self.execution, dispatch=self.dispatch,
            evidence=self.evidence, task_id=self.state["task_id"],
            attempt_id=self.attempt["review_attempt_id"],
            session_created_at=self.attempt["created_at"])

    def _args(self, **overrides) -> SimpleNamespace:
        args = SimpleNamespace(
            state=self.state_path, dispatch=self.dispatch_path,
            execution=self.execution_path, work_item="A1",
            metadata=self.host["metadata_path"],
            model_rollout=self.host["rollout_path"],
            input=self.host["output_file"], output="",
            attempt=self.attempt["review_attempt_id"])
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_g4_eligible_is_computed_from_host_record(self):
        env = NA.build_review(self._args())
        self.assertTrue(env["g4_eligible"], env["g4_reasons"])
        self.assertEqual(env["schema"], NB.REVIEW_SCHEMA)
        self.assertEqual(env["reviewer"]["agent_id"],
                         self.host["agent_id"])
        self.assertEqual(
            sorted(env["host"]["tool_calls"], key=lambda c: c["name"]),
            sorted(self.host["tool_calls"], key=lambda c: c["name"]))
        # 宿主记录参与了计算：换掉工具调用计数就不再具备 G4 资格。
        metadata = json.loads(open(self.host["metadata_path"], encoding="utf-8").read())
        metadata["profileSnapshot"]["disallowedTools"] = []
        write_json(self.host["metadata_path"], metadata)
        env = NA.build_review(self._args())
        self.assertFalse(env["g4_eligible"])
        self.assertTrue(any(reason.startswith("profile_disallowed_missing")
                            for reason in env["g4_reasons"]), env["g4_reasons"])

    def test_non_pass_review_is_not_g4_eligible(self):
        output = json.loads(open(self.host["output_file"], encoding="utf-8").read())
        output["verdict"] = "partial"
        write_json(self.host["output_file"], output)
        env = NA.build_review(self._args())
        self.assertFalse(env["g4_eligible"])
        self.assertIn("review_verdict_not_pass", env["g4_reasons"])

    def test_host_output_change_after_envelope_is_detected(self):
        env = NA.build_review(self._args())
        self.assertTrue(env["g4_eligible"], env["g4_reasons"])
        output = json.loads(open(self.host["output_file"], encoding="utf-8").read())
        output["verdict"] = "partial"
        write_json(self.host["output_file"], output)
        problems = NA.verify_review_host_record(env)
        self.assertIn("host_verdict_changed", problems)

    def test_host_unverified_truncation_is_detected(self):
        env = NA.build_review(self._args())
        self.assertTrue(env["g4_eligible"], env["g4_reasons"])
        output = json.loads(open(self.host["output_file"], encoding="utf-8").read())
        output["unverified"] = ["未验证项 A", "未验证项 B"]
        write_json(self.host["output_file"], output)
        problems = NA.verify_review_host_record(env)
        self.assertIn("host_unverified_changed", problems)


class DriveGuardTests(unittest.TestCase):
    def test_partial_returns_executable_work_order_instead_of_stopping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            G.start_state(path, goal="目标", contract=CONTRACT)
            # 没有任何工作项：任务只能是 PARTIAL，drive 必须本地派生并给出工作单。
            code = auto.drive(SimpleNamespace(state=path, max_actions=1,
                                              dispatch_out=""))
            self.assertEqual(code, auto.EXIT_EXECUTE)
            state = G.load_state(path)
            self.assertIn("AUTO-C1", [a.get("id") for a in state["actions"]])
            self.assertEqual(G.evaluate(state)["phase"], "RUNNING")

    def test_step_limit_is_an_internal_error_code_not_a_verdict(self):
        # 32 步上限只是防死循环的本地保护；达到上限时返回内部错误而不是 PASS/BLOCKED。
        # 当前派生规则下 PARTIAL 总会先产生可领取工作项并以 10 退出，所以这里核对
        # 常量与退出码映射，而不是构造不可达的 32 步场景。
        self.assertEqual(auto.MAX_DRIVE_STEPS, 32)
        self.assertEqual(auto.EXIT_ERROR, 12)
        self.assertNotIn(auto.EXIT_ERROR, (auto.EXIT_DONE, auto.EXIT_WAIT))


class RepeatedGapTests(unittest.TestCase):
    def test_repeated_g4_gap_derives_replan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            G.start_state(path, goal="目标", contract=CONTRACT,
                          execution_backend="native", risk="high")
            G.plan_state(path, PLAN)
            state = G.load_state(path)
            # 模拟连续三轮 G4 工作项被反复派生/标记完成但门禁仍缺。
            for index in range(3):
                action_id = "AUTO-G4" if index == 0 else f"AUTO-G4-R{index + 1}"
                state["actions"].append({
                    "id": action_id, "title": "G4 复核", "origin": "g4-review",
                    "status": "done", "result": "复核未通过", "evidence_ids": [],
                })
            G.save_state(path, state)
            state = G.load_state(path)
            created = G.derive_work(state)
            ids = [a["id"] for a in created]
            self.assertTrue(any(a["id"] == "AUTO-REPLAN" for a in created), ids)
            self.assertIn("AUTO-REPLAN", [a["id"] for a in state["actions"]])


if __name__ == "__main__":
    unittest.main()

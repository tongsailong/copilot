#!/usr/bin/env python3
"""原生执行信封 CLI 与驱动保护。

v8 起 G4 原生只读复核已退役：`native_agent.py` 只保留 `bind-execution` 与
`execution`；`review` / `review-context` / `capability` 子命令不再存在。
驱动上限仍不是任务终点；历史 G4 数据不参与派生。
"""
from __future__ import annotations

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
import v6_support as F  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""

PLAN = {"summary": "实现", "actions": [
    {"id": "A1", "title": "实现", "goal": "产出结果",
     "expected_evidence": "结果文件"},
]}


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
        self.assertEqual(auto.MAX_DRIVE_STEPS, 32)
        self.assertEqual(auto.EXIT_ERROR, 12)
        self.assertNotIn(auto.EXIT_ERROR, (auto.EXIT_DONE, auto.EXIT_WAIT))


class NativeAgentCLITests(unittest.TestCase):
    def test_review_subcommands_are_removed(self):
        for cmd in ("review", "review-context", "capability"):
            proc = F.run_cli("native_agent.py", cmd, "--state", "/tmp/x.json")
            self.assertNotEqual(proc.returncode, 0, cmd)
            self.assertIn("invalid choice", proc.stderr)

    def test_execution_subcommands_remain(self):
        proc = F.run_cli("native_agent.py", "--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("execution", proc.stdout)
        self.assertIn("bind-execution", proc.stdout)


class NoG4DerivationTests(unittest.TestCase):
    def _high_state(self, tmp: str) -> str:
        path = os.path.join(tmp, "state.json")
        G.start_state(path, goal="目标", contract=CONTRACT,
                      execution_backend="native", risk="high",
                      test_policy={"required": False})
        G.plan_state(path, PLAN)
        state = G.load_state(path)
        state.setdefault("actions", []).append({
            "id": "A1", "title": "实现", "origin": "zcode-plan",
            "status": "done", "result": "完成", "evidence_ids": [],
        })
        G.save_state(path, state)
        return path

    def test_historical_capability_gap_never_derives_g4_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._high_state(tmp)
            state = G.load_state(path)
            state["g4_attestations"] = [{
                "status": "attested",
                "new_reason": ["host_session_plan_mode"],
                "attestation": {"rejections": [
                    {"code": "host_session_plan_mode", "detail": "plan mode"}]},
            }]
            G.save_state(path, state)
            created = G.derive_work(G.load_state(path))
            ids = [a["id"] for a in created]
            self.assertFalse(any(item.startswith("AUTO-G4") for item in ids), ids)

    def test_repeated_historical_g4_actions_do_not_trigger_replan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._high_state(tmp)
            state = G.load_state(path)
            for index in range(3):
                action_id = "AUTO-G4" if index == 0 else f"AUTO-G4-R{index + 1}"
                state["actions"].append({
                    "id": action_id, "title": "G4 复核", "origin": "g4-review",
                    "status": "done", "result": "历史复核", "evidence_ids": [],
                })
            G.save_state(path, state)
            created = G.derive_work(G.load_state(path))
            self.assertFalse(any(a["id"] == "AUTO-REPLAN" for a in created))
            self.assertFalse(any(a["id"].startswith("AUTO-G4") for a in created))


if __name__ == "__main__":
    unittest.main()

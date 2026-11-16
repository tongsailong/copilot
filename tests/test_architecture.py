#!/usr/bin/env python3
"""v6 架构约束回归：活动路径不调用远程模型、不依赖 SOL、状态机收敛。"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import governance as G  # noqa: E402

ACTIVE = ("governance.py", "copilot_dispatch.py", "copilot_auto.py",
          "agent_policy.py", "native_bridge.py", "native_agent.py",
          "bridge_evidence.py", "lib_common.py")


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def source(name: str) -> str:
    with open(os.path.join(SCRIPTS, name), "r", encoding="utf-8") as fh:
        return fh.read()


class NoRemoteDecisionPathTests(unittest.TestCase):
    def test_active_scripts_do_not_import_sol_channel_modules(self):
        for name in ACTIVE:
            tree = ast.parse(source(name), filename=name)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            for banned in ("call_gpt", "review_ledger", "reply_protocol"):
                self.assertNotIn(banned, imported,
                                 f"{name} 仍导入已退役的 SOL 模块 {banned}")

    def test_active_scripts_have_no_network_clients(self):
        banned = ("urllib", "requests", "http", "socket", "aiohttp", "httpx")
        for name in ACTIVE:
            tree = ast.parse(source(name), filename=name)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            for module in banned:
                self.assertNotIn(module, imported,
                                 f"{name} 不应包含网络客户端 {module}")

    def test_no_sol_model_or_endpoint_in_active_scripts(self):
        # 活动 scripts/ 不得出现已退役的模型 ID、上游端点或凭据读取；
        # 历史实测事实只保留在 references/history.md 与 legacy/。
        for name in ACTIVE:
            text = source(name)
            self.assertNotIn("gpt-6.1-sol", text, name)
            self.assertNotIn("/v1/responses", text, name)
            self.assertNotIn("CPA_", text, name)

    def test_active_scripts_do_not_read_credentials(self):
        for name in ACTIVE:
            text = source(name)
            for needle in ("provider_config.json", "resolve_api_key",
                           "read_zcode_provider_key"):
                self.assertNotIn(needle, text, f"{name} 不应读取 Provider 凭据")
        self.assertTrue(os.path.isfile(
            os.path.join(ROOT, "legacy", "lib_common_legacy.py")),
            "v5 的凭据解析链应保留在 legacy/ 供历史回归使用")

    def test_retired_sol_implementations_are_archived(self):
        for name in ("call_gpt.py", "review_ledger.py", "reply_protocol.py"):
            self.assertFalse(os.path.exists(os.path.join(SCRIPTS, name)),
                             f"{name} 不应继续出现在活动 scripts/")
            self.assertTrue(os.path.isfile(os.path.join(ROOT, "legacy", name)),
                            f"{name} 的历史实现应保留在 legacy/")

    def test_governance_engine_is_pure_local(self):
        text = source("governance.py")
        for needle in ("subprocess", "urlopen", "request(", "socket"):
            self.assertNotIn(needle, text)


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state.json")
        G.start_state(self.state, goal="目标",
                      contract="## 完成条件\n- 产出结果\n")

    def test_phases_are_converged_set(self):
        self.assertEqual(tuple(G.PHASES),
                         ("CLOSED", "BLOCKED", "PASS", "RUNNING", "PARTIAL"))

    def test_v6_state_has_no_remote_or_budget_gate_fields(self):
        state = G.load_state(self.state)
        for key in ("pending_call", "quota", "round_seq", "budget",
                    "pass_pending", "next_plan"):
            self.assertNotIn(key, state)
        self.assertEqual(state["schema"], "copilot-governance/v1")
        self.assertEqual(state["version"], 1)

    def test_partial_is_not_terminal_and_drives_new_work(self):
        state = G.load_state(self.state)
        self.assertEqual(G.evaluate(state)["phase"], "PARTIAL")
        created = G.derive_work(state)
        self.assertTrue(created)
        self.assertNotIn("PARTIAL", [a["status"] for a in state["actions"]])

    def test_real_blocked_requires_reason_and_needs(self):
        with self.assertRaises(G.GovernanceError):
            G.add_block(self.state, " ", " ")
        block = G.add_block(self.state, "缺少用户授权", "用户确认")
        self.assertEqual(block["needs"], "用户确认")

    def test_no_artificial_round_or_token_stop(self):
        text = source("copilot_auto.py")
        self.assertNotIn("max_rounds", text)
        self.assertNotIn("token_limit", text)
        self.assertIn("MAX_DRIVE_STEPS", text)
        # 驱动步数只是防死循环的本地保护，不是任务终止条件：
        # 超限返回内部错误并要求检查状态，而不是标记 BLOCKED/PASS。
        self.assertIn("EXIT_ERROR", text)


class LegacyStateSafetyTests(unittest.TestCase):
    def test_legacy_status_never_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.json")
            payload = {"version": 2, "task_id": "t", "rounds": []}
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            before = _read_bytes(path)
            status = G.legacy_status(path=path)
            self.assertTrue(status["retired"])
            self.assertEqual(_read_bytes(path), before)
            self.assertEqual(sorted(os.listdir(tmp)), ["old.json"])


if __name__ == "__main__":
    unittest.main()

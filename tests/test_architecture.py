#!/usr/bin/env python3
"""v8.1 架构约束回归：SOL 是唯一远程通道，三模式均零工具、单轮、建议性。"""
from __future__ import annotations

import ast
import json
import os
import subprocess
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
          "bridge_evidence.py", "lib_common.py", "review_policy.py",
          "host_records.py", "code_scope.py", "code_scope_capture.py",
          "sol_review.py")
# v8.1 唯一允许访问网络与凭据的活动模块。其他模块只能通过它间接调用 SOL。
NETWORK_MODULE = "sol_review.py"
# v8 起 G4 活动机制已退役；这些模块不得重新出现在活动 scripts/。
RETIRED_MODULES = ("g4_attest.py", "g4_shadow.py", "test_execution.py",
                   "review_output.py")


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def source(name: str) -> str:
    with open(os.path.join(SCRIPTS, name), "r", encoding="utf-8") as fh:
        return fh.read()


def _imports(name: str) -> set[str]:
    """返回完整模块名：`urllib.parse` 与 `urllib.request` 必须区分。"""
    tree = ast.parse(source(name), filename=name)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


class RemoteReviewBoundaryTests(unittest.TestCase):
    def test_active_scripts_do_not_import_legacy_sol_modules(self):
        for name in ACTIVE:
            imported = _imports(name)
            for banned in ("call_gpt", "review_ledger", "reply_protocol"):
                self.assertNotIn(banned, imported,
                                 f"{name} 仍导入已退役的 SOL 模块 {banned}")

    def test_only_sol_review_has_network_clients(self):
        # urllib.parse 不是网络客户端；真正发起请求的是 urllib.request / socket。
        banned = ("urllib.request", "urllib.error", "http.client",
                  "requests", "aiohttp", "httpx", "socket")
        for name in ACTIVE:
            if name == NETWORK_MODULE:
                continue
            imported = _imports(name)
            for module in banned:
                self.assertFalse(
                    any(item == module or item.startswith(module + ".")
                        for item in imported),
                    f"{name} 不应包含网络客户端 {module}")

    def test_only_sol_review_has_model_endpoint_and_credentials(self):
        # 除 sol_review.py 外，活动模块不得出现模型 ID、上游端点或凭据读取；
        # 历史实测事实只保留在 references/history.md 与 legacy/。
        for name in ACTIVE:
            if name == NETWORK_MODULE:
                continue
            text = source(name)
            for needle in ("gpt-6.2-sol", "gpt-6.1-sol", "/v1/responses",
                           "CPA_", "provider_config.json", "resolve_api_key",
                           "read_zcode_provider_key"):
                self.assertNotIn(needle, text, f"{name} 不应包含 {needle}")

    def test_sol_is_advisory_only_with_three_modes(self):
        text = source(NETWORK_MODULE)
        self.assertIn("tool_choice", text)
        self.assertIn("gpt-6.2-sol", text)
        # 模型升级必须真正切换，不得保留旧模型名或静默回退。
        self.assertNotIn("gpt-6.1-sol", text)
        self.assertIn('SOL_MODES = ("CONSULT", "PLAN", "REVIEW")', text)
        # THINK 只作为历史别名读取，不再出现在活动模式集合里。
        self.assertIn('LEGACY_SOL_MODES = ("THINK",)', text)
        self.assertIn('COGNITIVE_MODES = ("CONSULT", "PLAN")', text)
        self.assertIn('REASONING_EFFORT = "high"', text)
        self.assertIn("sol_effort_not_pinned", text)
        # DECIDE 不恢复；SOL 不接管工程执行或决策（文案可提退役，但不得成为模式）。
        self.assertNotIn('"DECIDE"', text)
        self.assertNotIn("'DECIDE'", text)
        for banned in ("subprocess", "os.system", "os.exec", "eval(",
                       "exec("):
            self.assertNotIn(banned, text, f"sol_review.py 不应包含 {banned}")
        # 没有工具定义键被写入 payload：只允许出现在拒绝名单里。
        self.assertIn('TOOL_KEYS = ("tools", "functions"', text)

    def test_retired_sol_implementations_are_archived(self):
        for name in ("call_gpt.py", "review_ledger.py", "reply_protocol.py"):
            self.assertFalse(os.path.exists(os.path.join(SCRIPTS, name)),
                             f"{name} 不应继续出现在活动 scripts/")
            self.assertTrue(os.path.isfile(os.path.join(ROOT, "legacy", name)),
                            f"{name} 的历史实现应保留在 legacy/")

    def test_g4_modules_are_removed_from_active_scripts(self):
        for name in RETIRED_MODULES:
            self.assertFalse(os.path.exists(os.path.join(SCRIPTS, name)),
                             f"{name} 属已退役 G4 机制，不应继续出现在活动 scripts/")

    def test_governance_engine_is_pure_local(self):
        text = source("governance.py")
        for needle in ("subprocess", "urlopen", "request(", "socket"):
            self.assertNotIn(needle, text)

    def test_sol_review_import_stays_off_the_network_stack(self):
        """SOL 网络栈只在请求路径内导入：不调 SOL 的命令不付 urllib/ssl 成本。

        实测 `import urllib.request` 连带 `ssl`/`http.client`/`email` 约 40–45 ms，
        而生命周期命令与钩子每次都会启动进程。这里用独立解释器断言模块级导入
        不触发这些模块；`_NoRedirect` 仍可延迟取得（历史调用方直接构造它）。
        """
        code = (
            "import sys; sys.path.insert(0, %r); import sol_review;"
            "loaded = [m for m in ('http.client', 'ssl', 'urllib.request',"
            " 'urllib.error') if m in sys.modules];"
            "assert sol_review._NoRedirect.__name__ == '_NoRedirect';"
            "print(loaded)" % SCRIPTS)
        proc = subprocess.run([sys.executable, "-B", "-c", code],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "[]")


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

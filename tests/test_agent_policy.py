#!/usr/bin/env python3
"""Agent 调度建议与内置能力基线。

这些测试锁定三件事：

1. 调度建议默认主代理执行；Explore 只用于独立只读调查，
   general-purpose 只用于边界清晰的独立交付物，不设调用比例或预算。
2. 内置 `Explore` 含 Bash、`general-purpose` 为 `tools=["*"]` 的宿主基线
   仍然准确（升级 ZCode 后必须用真实记录重核）。
3. v8 起 G4 退役：普通任务不得因为 Agent/Profile 问题被阻塞或派生 G4 工作。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import agent_policy as AP  # noqa: E402
import governance as G  # noqa: E402
import v6_support as F  # noqa: E402


class BuiltinBaselineTests(unittest.TestCase):
    def test_explore_has_bash_so_it_is_investigation_only(self):
        tools = set(AP.BUILTIN_AGENTS["Explore"]["tools"])
        self.assertIn("Bash", tools)
        self.assertFalse(AP.BUILTIN_AGENTS["Explore"]["inject_agents_md"])

    def test_general_purpose_is_unrestricted(self):
        self.assertEqual(AP.BUILTIN_AGENTS["general-purpose"]["tools"], ("*",))
        self.assertTrue(AP.BUILTIN_AGENTS["general-purpose"]["inherits_mcp"])


class DispatchAdviceTests(unittest.TestCase):
    def test_default_is_main(self):
        result = AP.dispatch_advice()
        self.assertEqual(result["executor"], "main")
        self.assertIn("主代理直接执行", result["reason"])

    def test_investigation_uses_explore_with_context_requirements(self):
        result = AP.dispatch_advice(needs_independent_context=True)
        self.assertEqual(result["executor"], "Explore")
        joined = "\n".join(result["prompt_requirements"])
        self.assertIn("AGENTS.md", joined)
        self.assertIn("只调查不修改", joined)

    def test_independent_deliverable_uses_general_purpose(self):
        result = AP.dispatch_advice(independent_deliverable=True,
                                    parallelizable=True, saves_time=True)
        self.assertEqual(result["executor"], "general-purpose")
        joined = "\n".join(result["prompt_requirements"])
        self.assertIn("文件边界", joined)
        self.assertIn("主代理负责集成", joined)

    def test_write_conflict_keeps_main(self):
        result = AP.dispatch_advice(independent_deliverable=True,
                                    parallelizable=True, saves_time=True,
                                    write_conflict=True)
        self.assertEqual(result["executor"], "main")
        self.assertIn("冲突", result["reason"])

    def test_write_conflict_overrides_independent_context(self):
        # Explore 含 Bash，冲突范围同样不该交给子代理。
        result = AP.dispatch_advice(needs_independent_context=True,
                                    write_conflict=True)
        self.assertEqual(result["executor"], "main")

    def test_no_parallel_benefit_keeps_main(self):
        result = AP.dispatch_advice(independent_deliverable=True,
                                    parallelizable=False, saves_time=True)
        self.assertEqual(result["executor"], "main")

    def test_no_time_saving_keeps_main(self):
        result = AP.dispatch_advice(independent_deliverable=True,
                                    parallelizable=True, saves_time=False)
        self.assertEqual(result["executor"], "main")

    def test_research_only_uses_explore(self):
        result = AP.dispatch_advice(research_only=True)
        self.assertEqual(result["executor"], "Explore")

    def test_research_only_is_never_general_purpose(self):
        # 只读调查即使带交付物标志也不能落到全工具/MCP 代理。
        result = AP.dispatch_advice(research_only=True,
                                    independent_deliverable=True,
                                    parallelizable=True, saves_time=True)
        self.assertEqual(result["executor"], "Explore")

    def test_subagent_unavailable_falls_back_to_main(self):
        result = AP.dispatch_advice(research_only=True,
                                    subagent_unavailable=True)
        self.assertEqual(result["executor"], "main")
        self.assertIn("主代理继续", result["reason"])
        self.assertIn("不原样重派", result["reason"])

    def test_g4_flag_is_no_longer_accepted(self):
        # G4 退役后调度接口不再接受 g4_required / plan_mode。
        with self.assertRaises(TypeError):
            AP.dispatch_advice(g4_required=True)


class AgentPolicyCLITests(unittest.TestCase):
    def run_cli(self, *args: str):
        return F.run_cli("copilot_dispatch.py", "agent-policy", *args)

    def test_default_json_is_main(self):
        proc = self.run_cli("--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["executor"], "main")
        self.assertEqual(payload["schema"], "copilot-agent-dispatch-advice/v1")

    def test_research_json_is_explore(self):
        proc = self.run_cli("--needs-independent-context", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["executor"], "Explore")

    def test_deliverable_json_is_general_purpose(self):
        proc = self.run_cli("--independent-deliverable", "--parallel",
                            "--saves-time", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["executor"],
                         "general-purpose")

    def test_g4_required_flag_is_removed(self):
        proc = self.run_cli("--g4-required", "--json")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unrecognized", proc.stderr)


class NormalTaskProfileIndependenceTests(unittest.TestCase):
    """普通任务不得因为 Agent/Profile 问题被阻塞或派生 G4 工作。"""

    def test_normal_task_evaluation_has_no_profile_or_g4_dependency(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state_path = os.path.join(tmp.name, "state.json")
        G.start_state(
            state_path, goal="普通单模块修改",
            contract="## 完成条件\n- 产出可复核结果\n", risk="normal")
        state = G.load_state(state_path)
        rendered = json.dumps(G.evaluate(state), ensure_ascii=False)
        for marker in ("profile_tools", "g4_", "reviewer_profile",
                       "agent_profile", "UNKNOWN_AGENT_TYPE"):
            self.assertNotIn(marker, rendered)
        # 未核销条件会让任务停在 RUNNING/PARTIAL，但不会因为缺少 Profile 变成 BLOCKED。
        self.assertIn(G.evaluate(state)["phase"], ("RUNNING", "PARTIAL"))


if __name__ == "__main__":
    unittest.main()

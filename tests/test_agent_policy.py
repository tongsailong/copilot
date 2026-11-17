#!/usr/bin/env python3
"""Agent 调度策略与宿主权限能力判定。

这些测试锁定三件事：

1. G4 只认宿主有效工具策略：`Read/Grep/Glob` 精确集合 + 禁用 `Write/Edit/Bash`；
   Profile 名称/来源/路径不参与判定。
2. 内置 `Explore` 含 Bash、`general-purpose` 为 `tools=["*"]`，都不能作为 G4
   执行者；宿主没有按派发覆盖工具的参数。
3. 调度建议默认主代理执行；Explore 只用于独立只读调查，general-purpose 只用于
   边界清晰的独立交付物，不设调用比例或预算。
"""
from __future__ import annotations

import json
import os
import sqlite3
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


def readonly_snapshot(**overrides) -> dict:
    snapshot = {
        "name": "reviewer",
        "source": "built-in",
        "path": "/repo/.zcode/agents/independent-readonly.md",
        "tools": ["Read", "Grep", "Glob"],
        "disallowedTools": ["Write", "Edit", "Bash", "WebFetch", "WebSearch",
                            "Agent", "Task", "TodoWrite", "NotebookEdit"],
        "permissionMode": "",
        "systemPrompt": "read only",
    }
    snapshot.update(overrides)
    return snapshot


def explore_snapshot(**overrides) -> dict:
    snapshot = {
        "name": "Explore",
        "source": "built-in",
        "tools": list(AP.BUILTIN_AGENTS["Explore"]["tools"]),
        "injectAgentsMd": False,
        "systemPrompt": "explore",
    }
    snapshot.update(overrides)
    return snapshot


def general_snapshot(**overrides) -> dict:
    snapshot = {
        "name": "general-purpose",
        "source": "built-in",
        "tools": ["*"],
        "injectAgentsMd": True,
        "systemPrompt": "general",
    }
    snapshot.update(overrides)
    return snapshot


class ClassifyProfileTests(unittest.TestCase):
    def test_readonly_profile_is_g4_capable(self):
        result = AP.classify_profile(readonly_snapshot())
        self.assertTrue(result["g4_executor"])
        self.assertEqual(result["role"], "read_only")
        self.assertFalse(result["can_write"])
        self.assertEqual(result["codes"], ["g4_ready"])

    def test_name_and_path_are_not_gating_for_builtin(self):
        result = AP.classify_profile(readonly_snapshot(
            name="any-name", path="/outside/any.md"))
        self.assertTrue(result["g4_executor"])
        self.assertEqual(result["builtin"], "")
        self.assertEqual(result["carrier"], "built-in")
        self.assertTrue(result["architecture_compliant"])

    def test_source_must_be_builtin(self):
        # 原定架构只允许 ZCode 内置子代理；项目/用户 Profile 不是 G4 载体。
        for source in ("project", "user", "plugin"):
            with self.subTest(source=source):
                result = AP.classify_profile(readonly_snapshot(source=source))
                self.assertFalse(result["g4_executor"])
                self.assertFalse(result["architecture_compliant"])
                self.assertIn("profile_source_not_builtin", result["codes"])
                self.assertIn("BLOCKED", result["remedy"])

    def test_explore_has_bash_and_is_not_g4(self):
        result = AP.classify_profile(explore_snapshot())
        self.assertFalse(result["g4_executor"])
        self.assertTrue(result["can_write"])
        self.assertEqual(result["role"], "write_capable")
        self.assertIn("g4_tools_include_bash", result["codes"])
        self.assertIn("Bash", result["remedy"])

    def test_general_purpose_is_unrestricted_and_not_g4(self):
        result = AP.classify_profile(general_snapshot())
        self.assertFalse(result["g4_executor"])
        self.assertTrue(result["can_write"])
        self.assertEqual(result["role"], "unrestricted")
        self.assertIn("g4_tools_unrestricted", result["codes"])
        self.assertIn("general-purpose", result["remedy"])

    def test_mcp_tools_count_as_write_capable(self):
        result = AP.classify_profile(readonly_snapshot(
            tools=["Read", "Grep", "Glob", "mcp__demo__write"]))
        self.assertFalse(result["g4_executor"])
        self.assertTrue(result["can_write"])

    def test_mcp_servers_count_as_write_capable(self):
        result = AP.classify_profile(readonly_snapshot(mcpServers=["demo"]))
        self.assertFalse(result["g4_executor"])
        self.assertTrue(result["can_write"])

    def test_skills_count_as_write_capable(self):
        result = AP.classify_profile(readonly_snapshot(skills=["deploy"]))
        self.assertFalse(result["g4_executor"])
        self.assertTrue(result["can_write"])
        self.assertEqual(result["role"], "write_capable")

    def test_host_write_tools_are_not_read_only(self):
        # 宿主 3.18.2 的写工具包含 ApplyPatch；执行类 js* 同样能改文件。
        for tool in ("ApplyPatch", "js", "js_reset", "Bash"):
            with self.subTest(tool=tool):
                result = AP.classify_profile(readonly_snapshot(
                    tools=["Read", "Grep", "Glob", tool]))
                self.assertFalse(result["g4_executor"])
                self.assertTrue(result["can_write"])

    def test_unknown_tool_names_fail_closed_as_write_capable(self):
        # 未来新增工具没有先更新集合也不能被当成只读。
        result = AP.classify_profile(readonly_snapshot(
            tools=["Read", "Grep", "Glob", "SomeFutureTool"]))
        self.assertFalse(result["g4_executor"])
        self.assertTrue(result["can_write"])

    def test_missing_snapshot_fails_closed(self):
        for value in (None, {}):
            with self.subTest(value=value):
                result = AP.classify_profile(value)
                self.assertFalse(result["g4_executor"])
                self.assertEqual(result["role"], "unknown")
                self.assertIn("profile_snapshot_missing", result["codes"])

    def test_snapshot_without_tools_is_named_separately(self):
        result = AP.classify_profile({"name": "x"})
        self.assertFalse(result["g4_executor"])
        self.assertIn("profile_tools_missing", result["codes"])
        self.assertNotIn("profile_snapshot_missing", result["codes"])

    def test_empty_tools_fails_closed(self):
        result = AP.classify_profile(readonly_snapshot(tools=[]))
        self.assertFalse(result["g4_executor"])
        self.assertIn("profile_tools_empty", result["codes"])

    def test_missing_disallowed_fails_closed(self):
        result = AP.classify_profile(readonly_snapshot(disallowedTools=[]))
        self.assertFalse(result["g4_executor"])
        self.assertIn("profile_disallowed_missing", result["codes"])

    def test_builtin_capability_table_matches_host_evidence(self):
        explore = AP.BUILTIN_AGENTS["Explore"]
        self.assertIn("Bash", explore["tools"])
        self.assertFalse(explore["inject_agents_md"])
        self.assertFalse(explore["inherits_mcp"])
        general = AP.BUILTIN_AGENTS["general-purpose"]
        self.assertEqual(general["tools"], ("*",))
        self.assertTrue(general["inject_agents_md"])
        self.assertTrue(general["inherits_mcp"])


class CapabilityReportTests(unittest.TestCase):
    def test_report_marks_g4_capability_and_note(self):
        metadata = {"agentId": "a1", "childSessionId": "c1",
                    "profileId": "reviewer", "status": "completed",
                    "profileSnapshot": readonly_snapshot()}
        report = AP.capability_report(
            metadata, metadata_path="/tmp/m.json",
            execution_state={"found": True, "mode": "yolo",
                             "plan_enabled": False})
        self.assertEqual(report["schema"], "copilot-agent-capability/v1")
        self.assertTrue(report["g4"]["executor"])
        self.assertTrue(report["g4"]["precheck_only"])
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["session_execution"]["plan_enabled"], False)
        self.assertIn("不代表 G4 已通过", report["note"])
        self.assertIn("g4_attest", report["note"])

    def test_plan_mode_disqualifies_g4_executor(self):
        # plan 模式会给子代理注入 Bash：即使 Profile 策略精确只读也不能算 G4 载体。
        report = AP.capability_report(
            {"profileId": "reviewer", "status": "completed",
             "profileSnapshot": readonly_snapshot()},
            execution_state={"found": True, "mode": "build",
                             "plan_enabled": True})
        self.assertFalse(report["g4"]["executor"])
        self.assertIn("host_session_plan_mode", report["g4"]["codes"])
        self.assertIn("plan", report["g4"]["remedy"])
        self.assertIn("host_session_plan_mode", report["warnings"])

    def test_missing_session_state_fails_closed(self):
        report = AP.capability_report(
            {"profileId": "reviewer", "status": "completed",
             "profileSnapshot": readonly_snapshot()},
            execution_state={"found": False, "mode": "", "plan_enabled": None})
        self.assertFalse(report["g4"]["executor"])
        self.assertIn("host_session_state_missing", report["g4"]["codes"])

    def test_unknown_plan_flag_fails_closed(self):
        report = AP.capability_report(
            {"profileId": "reviewer", "status": "completed",
             "profileSnapshot": readonly_snapshot()},
            execution_state={"found": True, "mode": "yolo",
                             "plan_enabled": None})
        self.assertFalse(report["g4"]["executor"])
        self.assertIn("host_session_state_unknown", report["g4"]["codes"])

    def test_unchecked_session_state_is_reported(self):
        report = AP.capability_report({
            "profileId": "reviewer", "status": "completed",
            "profileSnapshot": readonly_snapshot()})
        self.assertIn("session_state_unchecked", report["warnings"])
        self.assertFalse(report["g4"]["executor"])
        self.assertIn("session_state_unchecked", report["g4"]["codes"])
        self.assertIsNone(report["session_execution"])

    def test_host_version_prefers_probed_value(self):
        report = AP.capability_report(
            {"profileId": "reviewer", "status": "completed",
             "profileSnapshot": readonly_snapshot()},
            execution_state={"found": True, "mode": "yolo",
                             "plan_enabled": False},
            host_info={"version": "9.9.9", "source": "test"})
        self.assertEqual(report["host_version"], "9.9.9")
        self.assertEqual(report["host_baseline"], AP.HOST_VERSION)
        self.assertEqual(report["host"]["source"], "test")

    def test_report_flags_profile_mismatch_and_incomplete(self):
        metadata = {"agentId": "a1", "childSessionId": "c1",
                    "profileId": "different", "status": "running",
                    "profileSnapshot": readonly_snapshot()}
        report = AP.capability_report(metadata)
        self.assertIn("profile_id_mismatch", report["warnings"])
        self.assertIn("agent_not_completed", report["warnings"])

    def test_report_flags_permission_mode_whitespace(self):
        report = AP.capability_report({
            "profileId": "reviewer", "status": "completed",
            "profileSnapshot": readonly_snapshot(permissionMode="default ")})
        self.assertIn("permission_mode_whitespace", report["warnings"])

    def test_report_without_snapshot_fails_closed(self):
        report = AP.capability_report({"profileId": "x"})
        self.assertFalse(report["g4"]["executor"])
        self.assertIn("profile_snapshot_missing", report["warnings"])

    def test_report_with_snapshot_but_no_policy_fails_closed(self):
        report = AP.capability_report({
            "profileId": "x", "profileSnapshot": {"name": "x"}})
        self.assertFalse(report["g4"]["executor"])
        self.assertIn("profile_tools_missing", report["warnings"])


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

    def test_g4_in_plan_mode_is_not_dispatchable(self):
        result = AP.dispatch_advice(g4_required=True, plan_mode=True)
        self.assertEqual(result["executor"], "unavailable")
        self.assertIn("plan", result["reason"])
        self.assertIn("Bash", result["reason"])

    def test_g4_outside_plan_mode_keeps_normal_advice(self):
        result = AP.dispatch_advice(g4_required=True, plan_mode=False)
        self.assertEqual(result["executor"], "main")


class ToolCallClassificationTests(unittest.TestCase):
    def test_host_write_and_execution_tools(self):
        for tool in ("Bash", "ApplyPatch", "Write", "js", "mcp__x__y"):
            with self.subTest(tool=tool):
                self.assertTrue(AP.tool_call_is_write(tool, 1)[0])

    def test_known_read_tools_are_not_write(self):
        for tool in ("Read", "Grep", "Glob", "WebFetch", "TodoWrite"):
            with self.subTest(tool=tool):
                is_write, unclassified = AP.tool_call_is_write(tool, 1)
                self.assertFalse(is_write)
                self.assertFalse(unclassified)

    def test_unknown_tool_with_read_only_flag_is_unclassified(self):
        is_write, unclassified = AP.tool_call_is_write("SomeFutureTool", -1)
        self.assertFalse(is_write)
        self.assertTrue(unclassified)
        # 宿主明确记录 read_only=0 时按写能力处理。
        self.assertTrue(AP.tool_call_is_write("SomeFutureTool", 0)[0])


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


class CapabilityCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write_metadata(self, snapshot) -> str:
        path = os.path.join(self.tmp.name, "metadata.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"agentId": "a1", "childSessionId": "c1",
                       "profileId": (snapshot or {}).get("name") or "x",
                       "status": "completed",
                       "profileSnapshot": snapshot}, fh,
                      ensure_ascii=False, indent=2)
        return path

    def make_host_db(self, calls, *, with_session=True) -> str:
        """最小宿主夹具：session + tool_usage + execution_state（与宿主 3.18.2 列名一致）。"""
        import sqlite3
        path = os.path.join(self.tmp.name, "host.sqlite")
        con = sqlite3.connect(path)
        con.execute("create table session (id text primary key, parent_id text)")
        con.execute("create table tool_usage (id integer primary key, "
                    "session_id text, tool_name text, read_only integer, "
                    "status text)")
        con.execute("create table session_entry (session_id text, type text, "
                    "data text, time_created integer)")
        if with_session:
            con.execute("insert into session (id) values (?)", ("c1",))
            con.execute(
                "insert into session_entry values (?,?,?,?)",
                ("c1", "runtime/execution_state",
                 json.dumps({"mode": "yolo", "planEnabled": False}), 1))
        for tool, read_only, status, count in calls:
            for _ in range(count):
                con.execute(
                    "insert into tool_usage (session_id, tool_name, read_only, "
                    "status) values (?,?,?,?)",
                    ("c1", tool, read_only, status))
        con.commit()
        con.close()
        return path

    def run_capability(self, snapshot, db):
        path = self.write_metadata(snapshot)
        proc = F.run_cli("native_agent.py", "capability", "--metadata", path,
                         "--db", db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_capability_cli_reports_explore_not_g4(self):
        db = self.make_host_db([("Bash", 0, "completed", 2),
                                ("Grep", 1, "completed", 3)])
        payload = self.run_capability(explore_snapshot(), db)
        self.assertEqual(payload["profile_id"], "Explore")
        self.assertFalse(payload["g4"]["executor"])
        self.assertIn("g4_tools_include_bash", payload["g4"]["codes"])

    def test_capability_cli_reports_readonly_g4(self):
        db = self.make_host_db([("Read", 1, "completed", 3),
                                ("Grep", 1, "completed", 2)])
        payload = self.run_capability(readonly_snapshot(), db)
        self.assertTrue(payload["g4"]["executor"])
        self.assertEqual(payload["g4"]["codes"], ["g4_ready"])
        evidence = payload["host_evidence"]
        self.assertTrue(evidence["available"])
        self.assertEqual(evidence["session_state"], "found")
        self.assertEqual(evidence["total_calls"], 5)
        self.assertEqual(evidence["write_capable_calls"], 0)
        self.assertEqual(payload["warnings"], [])

    def test_capability_cli_rejects_plan_mode(self):
        db = self.make_host_db([("Read", 1, "completed", 2)])
        con = sqlite3.connect(db)
        con.execute(
            "update session_entry set data=? "
            "where type='runtime/execution_state'",
            (json.dumps({"mode": "build", "planEnabled": True}),))
        con.commit()
        con.close()
        payload = self.run_capability(readonly_snapshot(), db)
        self.assertFalse(payload["g4"]["executor"])
        self.assertIn("host_session_plan_mode", payload["g4"]["codes"])
        self.assertEqual(payload["session_execution"]["plan_enabled"], True)
        if payload["host"].get("version"):
            self.assertEqual(payload["host_version"], payload["host"]["version"])

    def test_capability_cli_warns_on_observed_write_calls(self):
        # 快照是只读，但宿主记录出现 ApplyPatch 调用：必须告警。
        db = self.make_host_db([("Read", 1, "completed", 1),
                                ("ApplyPatch", 0, "completed", 1)])
        payload = self.run_capability(readonly_snapshot(), db)
        self.assertTrue(payload["g4"]["executor"])
        self.assertEqual(payload["host_evidence"]["write_capable_calls"], 1)
        self.assertIn("observed_write_capable_calls", payload["warnings"])

    def test_capability_cli_warns_on_unclassified_calls(self):
        db = self.make_host_db([("Read", 1, "completed", 1),
                                ("SomeFutureTool", None, "error", 1)])
        payload = self.run_capability(readonly_snapshot(), db)
        self.assertEqual(payload["host_evidence"]["unclassified_calls"], 1)
        self.assertIn("observed_unclassified_calls", payload["warnings"])

    def _add_tool_usage_rows(self, db: str, rows: list[tuple]) -> None:
        con = sqlite3.connect(db)
        columns = {row[1] for row in
                   con.execute("pragma table_info(tool_usage)")}
        for name in ("error_type", "duration_ms", "output_bytes"):
            if name not in columns:
                con.execute(f"alter table tool_usage add column {name} text")
        for tool, status, error_type, duration in rows:
            con.execute(
                "insert into tool_usage (session_id, tool_name, read_only,"
                " status, error_type, duration_ms, output_bytes)"
                " values (?,?,?,?,?,?,?)",
                ("c1", tool, 0, status, error_type, duration, 0))
        con.commit()
        con.close()

    def test_capability_cli_denied_bash_is_not_write_capability(self):
        db = self.make_host_db([("Read", 1, "completed", 1)])
        self._add_tool_usage_rows(db, [
            ("Bash", "error", "permission_denied", 0)])
        payload = self.run_capability(readonly_snapshot(), db)
        evidence = payload["host_evidence"]
        self.assertEqual(evidence["total_calls"], 2)
        self.assertEqual(evidence["executed_calls"], 1)
        self.assertEqual(evidence["denied_calls"], 1)
        self.assertEqual(evidence["denied_tools"], {"Bash": 1})
        self.assertEqual(evidence["write_capable_calls"], 0)
        self.assertNotIn("observed_write_capable_calls", payload["warnings"])

    def test_capability_cli_flags_repeated_denied_calls(self):
        db = self.make_host_db([("Read", 1, "completed", 1)])
        self._add_tool_usage_rows(db, [
            ("Bash", "error", "permission_denied", 0)] * 3)
        payload = self.run_capability(readonly_snapshot(), db)
        self.assertEqual(payload["host_evidence"]["denied_calls"], 3)
        self.assertEqual(payload["host_evidence"]["repeated_denied_tools"],
                         ["Bash"])
        self.assertIn("repeated_denied_calls", payload["warnings"])

    def test_capability_cli_executed_bash_still_warns(self):
        db = self.make_host_db([("Read", 1, "completed", 1)])
        self._add_tool_usage_rows(db, [
            ("Bash", "error", "tool_execution_failed", 12)])
        payload = self.run_capability(readonly_snapshot(), db)
        self.assertEqual(payload["host_evidence"]["write_capable_calls"], 1)
        self.assertIn("observed_write_capable_calls", payload["warnings"])

    def test_capability_cli_reports_missing_session(self):
        db = self.make_host_db([], with_session=False)
        payload = self.run_capability(readonly_snapshot(), db)
        evidence = payload["host_evidence"]
        self.assertFalse(evidence["available"])
        self.assertEqual(evidence["reason"], "session_not_found")
        self.assertEqual(evidence["session_state"], "missing")

    def test_capability_cli_tolerates_missing_host_db(self):
        payload = self.run_capability(
            explore_snapshot(), os.path.join(self.tmp.name, "nope.sqlite"))
        self.assertFalse(payload["host_evidence"]["available"])
        self.assertFalse(payload["g4"]["executor"])


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

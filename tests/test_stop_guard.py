#!/usr/bin/env python3
"""Stop 续跑守卫回归：PARTIAL 报告不能再结束回合。

守卫是 Copilot 与 ZCode 之间的机械衔接层：`EXIT_CONTINUE=10` 只是退出码，
模型可以忽略；Stop 钩子退出码 2 由宿主强制继续（宿主机单回合最多 3 次）。

两类相反风险都要覆盖：

* 该拦不拦：本会话负责、仍有可执行工作的任务，Stop 必须被拦下；
* 永久误拦：其它会话的旧任务、其它工作区、已收口/整体阻塞的任务必须放行，
  且放行不是靠“24 小时过期”这类人为时间上限。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import governance as G  # noqa: E402

GUARD = os.path.join(SCRIPTS, "copilot_stop_guard.py")
DISPATCH = os.path.join(SCRIPTS, "copilot_dispatch.py")
CONTRACT = """## 完成条件
- 代码改动完成
- 真机验证完成

## 边界
- 不修改无关项目
"""
PLAN = {"summary": "实现与验证", "actions": [
    {"id": "A1", "title": "实现", "goal": "完成代码改动",
     "expected_evidence": "diff 与测试输出"},
    {"id": "A2", "title": "真机验证", "goal": "在真机上验证", "expected_evidence": "截图"},
]}
SESSION = "sess_current_0001"
OTHER_SESSION = "sess_other_0002"


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


class StopGuardBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = self.tmp.name
        self.task_dir = os.path.join(self.workspace, ".copilot-tasks", "t1")
        os.makedirs(self.task_dir, exist_ok=True)
        self.state_path = os.path.join(self.task_dir, "state.json")
        G.start_state(self.state_path, goal="守卫回归", contract=CONTRACT,
                      test_policy={"required": False}, workspace=self.workspace)
        G.plan_state(self.state_path, PLAN)
        self.bind(SESSION)

    def bind(self, session: str, task_dir: str | None = None) -> None:
        """模拟 Copilot CLI 的会话绑定（与 G.touch_session 写入同格式）。"""
        target = task_dir or self.task_dir
        payload = {"sessions": [{"session_id": session, "at": "2026-11-20T00:00:00+00:00"}],
                   "updated": "2026-11-20T00:00:00+00:00"}
        with open(os.path.join(target, "session.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    def run_guard(self, *, payload: dict | None = None, args: list[str] | None = None,
                  env: dict | None = None, text: str | None = None,
                  session: str | None = SESSION):
        environment = dict(os.environ)
        for key in ("COPILOT_STOP_GUARD", "COPILOT_STOP_GUARD_LOG",
                    "ZCODE_SESSION_ID", "CLAUDE_SESSION_ID",
                    "CLAUDE_CODE_SESSION_ID"):
            environment.pop(key, None)
        if session is not None:
            environment["ZCODE_SESSION_ID"] = session
        if env:
            environment.update(env)
        body = text
        if body is None:
            data = dict(payload or {"cwd": self.workspace})
            if session is not None:
                data.setdefault("session_id", session)
            body = json.dumps(data)
        return subprocess.run(
            [sys.executable, "-B", GUARD, *(args or [])],
            input=body, capture_output=True, text=True, cwd=self.workspace,
            env=environment,
        )

    def state(self) -> dict:
        return G.load_state(self.state_path)

    def save(self, state: dict) -> None:
        G.save_state(self.state_path, state)


class ContinuationGateTests(StopGuardBase):
    def test_bound_partial_blocks_stop(self):
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("COPILOT 续跑守卫", proc.stderr)
        self.assertIn("t1", proc.stderr)
        self.assertIn("drive", proc.stderr)
        # 退出码 2 时 stdout 必须保持非 JSON 文本，否则宿主会按 JSON 解析。
        self.assertEqual(proc.stdout.strip(), "")

    def test_reason_is_actionable_and_within_host_limit(self):
        proc = self.run_guard()
        text = proc.stderr.strip()
        self.assertTrue(text, "退出码 2 必须带非空原因，否则宿主不会续跑")
        self.assertLessEqual(len(text), 4000, "宿主把 stderr 截断到 4000 字符")
        self.assertIn("领取工作项", text)
        self.assertIn("AUTO-REPLAN", text)
        self.assertIn("disable", text)

    def test_stop_hook_active_mentions_continuation_cap(self):
        self.run_guard()
        proc = self.run_guard(payload={"cwd": self.workspace,
                                       "session_id": SESSION,
                                       "stop_hook_active": True})
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("强制续跑", proc.stderr)
        self.assertIn("3 次", proc.stderr)

    def test_derive_only_task_blocks_stop(self):
        G.record_action(self.state_path, "A1", "done", result="完成")
        G.record_action(self.state_path, "A2", "done", result="完成")
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("AUTO-C", proc.stderr)

    def test_single_item_blocked_still_blocks(self):
        G.add_block(self.state_path, "A2 需要真机授权", "用户启用设备工具",
                    work_items=["A2"], criteria=["C2"])
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("A1", proc.stderr, "单项阻塞不能冻结其余可执行工作")

    def test_closed_task_allows_stop(self):
        state = self.state()
        state["closure"] = dict(state.get("closure") or {})
        state["closure"]["closed"] = True
        state["closure"]["closed_at"] = "2026-11-20T00:00:00+00:00"
        self.save(state)
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stderr.strip(), "")

    def test_fully_blocked_task_allows_stop(self):
        G.add_block(self.state_path, "缺少真机通道授权", "用户启用设备工具",
                    work_items=["A1", "A2"], criteria=["C1", "C2"])
        self.assertEqual(G.evaluate(self.state())["phase"], "BLOCKED")
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stderr.strip(), "")

    def test_other_session_task_does_not_block(self):
        # 旧任务/其它窗口的任务：不是靠时间过期，而是本会话没有负责它。
        self.bind(OTHER_SESSION)
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_unbound_legacy_task_does_not_block(self):
        os.unlink(os.path.join(self.task_dir, "session.json"))
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_stale_timestamp_alone_does_not_release(self):
        # 没有时间上限：只要本会话负责且仍有工作，多旧的更新时间都要继续。
        state = self.state()
        state["updated"] = "2020-01-01T00:00:00+00:00"
        self.save(state)
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_no_session_identity_enforces_newest_task_only(self):
        second = os.path.join(self.workspace, ".copilot-tasks", "t2")
        os.makedirs(second, exist_ok=True)
        state_path = os.path.join(second, "state.json")
        G.start_state(state_path, goal="第二个任务", contract=CONTRACT,
                      test_policy={"required": False}, workspace=self.workspace)
        G.plan_state(state_path, PLAN)
        self.bind(OTHER_SESSION, second)
        proc = self.run_guard(session=None, payload={"cwd": self.workspace})
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        # 只对最近更新的任务生效，避免把工作区里所有任务一次性当成当前任务。
        self.assertIn("1 个任务同时要求续跑", proc.stderr)

    def test_multi_workspace_isolation(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        os.makedirs(os.path.join(other.name, ".copilot-tasks"))
        proc = self.run_guard(payload={"cwd": other.name, "session_id": SESSION})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_env_kill_switch_allows_stop(self):
        proc = self.run_guard(env={"COPILOT_STOP_GUARD": "0"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_marker_kill_switch_allows_stop(self):
        with open(os.path.join(self.workspace, ".copilot-tasks",
                               ".stop-guard-off"), "w", encoding="utf-8") as fh:
            fh.write("off\n")
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_task_level_switch_allows_stop(self):
        state = self.state()
        state["stop_guard"] = False
        self.save(state)
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_task_marker_allows_stop(self):
        with open(os.path.join(self.task_dir, ".stop-guard-off"), "w",
                  encoding="utf-8") as fh:
            fh.write("off\n")
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_disable_and_enable_round_trip(self):
        proc = self.run_guard(args=["disable", "--state", self.state_path,
                                    "--reason", "用户明确要求今天先停"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        marker = os.path.join(self.task_dir, ".stop-guard-off")
        self.assertTrue(os.path.isfile(marker))
        with open(marker, encoding="utf-8") as fh:
            self.assertIn("用户明确要求今天先停", json.load(fh)["reason"])
        self.assertEqual(self.run_guard().returncode, 0)
        proc = self.run_guard(args=["enable", "--state", self.state_path])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.run_guard().returncode, 2)

    def test_disable_requires_reason(self):
        proc = self.run_guard(args=["disable", "--state", self.state_path])
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("--reason", proc.stderr)

    def test_corrupt_state_blocks_with_recovery(self):
        self.bind(SESSION)
        with open(self.state_path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("fail-closed", proc.stderr)
        self.assertIn("disable --state", proc.stderr)
        self.assertIn("不代表 PASS", proc.stderr)
        self.assertNotIn("任务已收口", proc.stderr)
        # 可恢复：disable 后放行（损坏的 state 也能显式退出）。
        self.run_guard(args=["disable", "--state", self.state_path,
                             "--reason", "任务已废弃"])
        self.assertEqual(self.run_guard().returncode, 0)

    def test_corrupt_binding_blocks_with_recovery(self):
        with open(os.path.join(self.task_dir, "session.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("[]")
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("session.json", proc.stderr)

    def test_invalid_payload_blocks_with_hook_fix(self):
        proc = self.run_guard(text="not json")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("钩子输入异常", proc.stderr)
        self.assertIn("COPILOT_STOP_GUARD=0", proc.stderr)

    def test_nested_cwd_finds_workspace_root(self):
        nested = os.path.join(self.workspace, "src", "feature")
        os.makedirs(nested, exist_ok=True)
        proc = self.run_guard(payload={"cwd": nested, "session_id": SESSION})
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_no_tasks_dir_allows_stop(self):
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        proc = self.run_guard(payload={"cwd": empty.name, "session_id": SESSION})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_status_reports_task_and_binding(self):
        proc = self.run_guard(args=["status", "--workspace", self.workspace,
                                    "--session", SESSION])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("t1", proc.stdout)
        self.assertIn("RUNNING", proc.stdout)
        self.assertIn("bound", proc.stdout)

    def test_optional_log_records_decision(self):
        log = os.path.join(self.workspace, "guard.log")
        proc = self.run_guard(env={"COPILOT_STOP_GUARD_LOG": log})
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        with open(log, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(rows[-1]["exit"], 2)
        self.assertEqual(rows[-1]["task_id"], self.state()["task_id"])
        self.assertEqual(rows[-1]["session"], SESSION)
        self.assertIn("RUNNING/execute", rows[-1]["note"])

    def test_guard_is_read_only(self):
        before = read_bytes(self.state_path)
        binding = os.path.join(self.task_dir, "session.json")
        binding_before = read_bytes(binding)
        listing = sorted(os.listdir(self.task_dir))
        self.run_guard()
        self.assertEqual(before, read_bytes(self.state_path))
        self.assertEqual(binding_before, read_bytes(binding))
        self.assertEqual(sorted(os.listdir(self.task_dir)), listing,
                         "守卫不得在任务目录里新建/删除任何文件")


class SessionBindingTests(StopGuardBase):
    def test_dispatch_cli_records_session_binding(self):
        os.unlink(os.path.join(self.task_dir, "session.json"))
        environment = dict(os.environ)
        environment["ZCODE_SESSION_ID"] = SESSION
        proc = subprocess.run(
            [sys.executable, "-B", DISPATCH, "record", "--state", self.state_path,
             "--id", "A1", "--status", "running", "--result", "开工"],
            capture_output=True, text=True, cwd=self.workspace, env=environment)
        # record 回填后判定仍需续跑，退出码 10；绑定必须已经写入。
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
        with open(os.path.join(self.task_dir, "session.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIn(SESSION, [row["session_id"] for row in data["sessions"]])
        # 绑定后 Stop 钩子就会认出这是本会话负责的任务。
        self.assertEqual(self.run_guard().returncode, 2)

    def test_read_only_command_does_not_write_binding(self):
        # 只读命令的契约是“只看不改”（handoff 回归覆盖写工作区），同一口径。
        os.unlink(os.path.join(self.task_dir, "session.json"))
        environment = dict(os.environ)
        environment["ZCODE_SESSION_ID"] = SESSION
        proc = subprocess.run(
            [sys.executable, "-B", DISPATCH, "status", "--state", self.state_path],
            capture_output=True, text=True, cwd=self.workspace, env=environment)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(
            os.path.join(self.task_dir, "session.json")))

    def test_failed_write_command_does_not_bind(self):
        # 命令失败（未写状态）时不建立会话绑定：否则会把一个没被本会话实际
        # 接手（也没改动）的任务变成“本会话负责”，Stop 时误拦。
        os.unlink(os.path.join(self.task_dir, "session.json"))
        environment = dict(os.environ)
        environment["ZCODE_SESSION_ID"] = SESSION
        proc = subprocess.run(
            [sys.executable, "-B", DISPATCH, "criterion", "--state",
             self.state_path, "--id", "C1", "--status", "met"],
            capture_output=True, text=True, cwd=self.workspace, env=environment)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(
            os.path.join(self.task_dir, "session.json")))

    def test_read_only_subcommands_do_not_bind(self):
        # quality / evidence --list 都是纯读取：查看旧任务不得变成接手它。
        cases = [
            ["quality", "--state", self.state_path, "--json"],
            ["evidence", "--state", self.state_path, "--list"],
            ["contract", "--state", self.state_path, "--show"],
        ]
        for argv in cases:
            with self.subTest(argv=argv[0]):
                binding = os.path.join(self.task_dir, "session.json")
                if os.path.exists(binding):
                    os.unlink(binding)
                environment = dict(os.environ)
                environment["ZCODE_SESSION_ID"] = SESSION
                proc = subprocess.run(
                    [sys.executable, "-B", DISPATCH, *argv],
                    capture_output=True, text=True, cwd=self.workspace,
                    env=environment)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertFalse(os.path.exists(binding), argv)

    def test_git_root_stops_ancestor_task_lookup(self):
        # 仓库内的会话不继承父目录（例如容器/家目录）里的 .copilot-tasks：
        # 越过 git 根向上找任务会把无关会话永久绑到别人的任务上。
        parent = tempfile.TemporaryDirectory()
        self.addCleanup(parent.cleanup)
        task_dir = os.path.join(parent.name, ".copilot-tasks", "t9")
        os.makedirs(task_dir)
        state_path = os.path.join(task_dir, "state.json")
        G.start_state(state_path, goal="父目录任务", contract=CONTRACT,
                      test_policy={"required": False}, workspace=parent.name)
        G.plan_state(state_path, PLAN)
        payload = {"sessions": [{"session_id": SESSION, "at": "2026-11-20T00:00:00+00:00"}],
                   "updated": "2026-11-20T00:00:00+00:00"}
        with open(os.path.join(task_dir, "session.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        repo = os.path.join(parent.name, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        proc = self.run_guard(payload={"cwd": repo, "session_id": SESSION})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_touch_session_keeps_multiple_sessions(self):
        G.touch_session(self.state_path, session=OTHER_SESSION)
        with open(os.path.join(self.task_dir, "session.json"), encoding="utf-8") as fh:
            ids = [row["session_id"] for row in json.load(fh)["sessions"]]
        self.assertEqual(ids, [SESSION, OTHER_SESSION])
        # 父会话与子代理会话都算“本任务负责方”。
        self.assertEqual(self.run_guard(session=OTHER_SESSION).returncode, 2)


class HookInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = self.tmp.name
        self.config_path = os.path.join(self.workspace, ".zcode", "config.json")

    def run_cli(self, *args: str, env: dict | None = None):
        environment = dict(os.environ)
        environment.pop("ZCODE_USER_CONFIG", None)
        if env:
            environment.update(env)
        return subprocess.run([sys.executable, "-B", GUARD, *args],
                              capture_output=True, text=True, cwd=self.workspace,
                              env=environment)

    def read_config(self) -> dict:
        with open(self.config_path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_install_preserves_existing_config_and_is_idempotent(self):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump({"mcp": {"servers": {"x": {"command": "y"}}}}, fh)
        proc = self.run_cli("install", "--workspace", self.workspace, "--apply")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        data = self.read_config()
        self.assertTrue(data["hooks"]["enabled"])
        self.assertEqual(data["mcp"]["servers"]["x"]["command"], "y")
        stop = data["hooks"]["events"]["Stop"]
        self.assertEqual(len(stop), 1)
        self.assertIn("copilot_stop_guard.py", json.dumps(stop[0], ensure_ascii=False))
        backups = [name for name in os.listdir(os.path.dirname(self.config_path))
                   if ".bak-" in name]
        self.assertEqual(len(backups), 1)

        before = read_bytes(self.config_path)
        again = self.run_cli("install", "--workspace", self.workspace, "--apply")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(len(self.read_config()["hooks"]["events"]["Stop"]), 1)
        self.assertEqual(before, read_bytes(self.config_path),
                         "重复安装不应改写配置或新增备份")

    def test_install_converges_duplicate_and_stale_entries(self):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        stale = {"hooks": [{"type": "command",
                            "command": "python3 /old/path/copilot_stop_guard.py"}]}
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"enabled": True, "events": {"Stop": [
                stale, dict(stale),
                {"hooks": [{"type": "command", "command": "echo other"}]},
            ]}}}, fh)
        proc = self.run_cli("install", "--workspace", self.workspace, "--apply")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        stop = self.read_config()["hooks"]["events"]["Stop"]
        self.assertEqual(len(stop), 2)
        self.assertIn("echo other", json.dumps(stop, ensure_ascii=False))
        guard_groups = [g for g in stop
                        if "copilot_stop_guard.py" in json.dumps(g, ensure_ascii=False)]
        self.assertEqual(len(guard_groups), 1)
        self.assertIn("$HOME/.zcode/skills/copilot/scripts",
                      json.dumps(guard_groups[0], ensure_ascii=False))

    def test_install_without_apply_changes_nothing(self):
        proc = self.run_cli("install", "--workspace", self.workspace)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(self.config_path))

    def test_uninstall_removes_only_guard_entry(self):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"enabled": True, "events": {"Stop": [
                {"hooks": [{"type": "command", "command": "echo other"}]},
            ]}}}, fh)
        self.run_cli("install", "--workspace", self.workspace, "--apply")
        proc = self.run_cli("uninstall", "--workspace", self.workspace, "--apply")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        stop = self.read_config()["hooks"]["events"]["Stop"]
        self.assertEqual(len(stop), 1)
        self.assertIn("echo other", json.dumps(stop[0], ensure_ascii=False))

    def test_config_command_prints_stop_hook(self):
        proc = self.run_cli("config")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["hooks"]["enabled"])
        self.assertIn("copilot_stop_guard.py",
                      json.dumps(payload["hooks"]["events"]["Stop"]))

    def test_install_writes_session_start_entry_and_is_idempotent(self):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        proc = self.run_cli("install", "--workspace", self.workspace, "--apply")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        events = self.read_config()["hooks"]["events"]
        self.assertEqual(len(events["SessionStart"]), 1)
        group = events["SessionStart"][0]
        self.assertEqual(group.get("matcher"), "resume|compact")
        self.assertIn("session-start", json.dumps(group, ensure_ascii=False))
        before = read_bytes(self.config_path)
        again = self.run_cli("install", "--workspace", self.workspace, "--apply")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(before, read_bytes(self.config_path),
                         "重复安装不应改写配置或新增备份")

    def test_uninstall_removes_only_our_session_start_entry(self):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        unrelated = {"matcher": "resume",
                     "hooks": [{"type": "command", "command": "echo other"}]}
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {"enabled": True, "events": {
                "SessionStart": [unrelated]}}}, fh)
        self.run_cli("install", "--workspace", self.workspace, "--apply")
        self.assertEqual(len(self.read_config()["hooks"]["events"]["SessionStart"]), 2)
        proc = self.run_cli("uninstall", "--workspace", self.workspace, "--apply")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        events = self.read_config()["hooks"]["events"]
        self.assertEqual(events["SessionStart"], [unrelated])
        self.assertEqual(events["Stop"], [])

    def test_install_requires_a_target(self):
        proc = self.run_cli("install", "--apply")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("--workspace", proc.stderr)
        self.assertFalse(os.path.exists(self.config_path))

    def test_user_scope_install_uses_user_config(self):
        # 工作区级 Stop 钩子需要 workspaceHookAdmission；用户级是可用兜底路径。
        user_config = os.path.join(self.workspace, "cli", "config.json")
        os.makedirs(os.path.dirname(user_config), exist_ok=True)
        with open(user_config, "w", encoding="utf-8") as fh:
            json.dump({"plugins": {"enabled": ["x"]}}, fh)
        env = {"ZCODE_USER_CONFIG": user_config}
        proc = self.run_cli("install", "--user", "--apply", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        with open(user_config, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertTrue(data["hooks"]["enabled"])
        self.assertEqual(data["plugins"]["enabled"], ["x"], "用户级其它配置必须保留")
        self.assertIn("copilot_stop_guard.py",
                      json.dumps(data["hooks"]["events"]["Stop"]))
        self.assertIn("用户级", proc.stdout)
        # 工作区级配置不应被顺带创建。
        self.assertFalse(os.path.exists(self.config_path))

    def test_user_scope_uninstall_removes_only_guard_entry(self):
        user_config = os.path.join(self.workspace, "cli", "config.json")
        env = {"ZCODE_USER_CONFIG": user_config}
        self.run_cli("install", "--user", "--apply", env=env)
        proc = self.run_cli("uninstall", "--user", "--apply", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        with open(user_config, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["hooks"]["events"]["Stop"], [])


class SessionStartRecoveryTests(StopGuardBase):
    """SessionStart 恢复注入：只补上下文，不绑定、不写状态、失败放行。

    ZCode 只在 `resume` / `compact` 真正丢失上下文时注入；`startup` 不推断旧任务，
    `clear` 尊重用户清空。未绑定当前会话的任务不披露任何任务内容。
    """

    def run_session_start(self, *, source: str = "resume",
                          payload: dict | None = None,
                          session: str | None = SESSION,
                          cwd: str | None = None, env: dict | None = None):
        data = dict(payload or {"cwd": cwd or self.workspace})
        data.setdefault("source", source)
        return self.run_guard(args=["session-start"], payload=data,
                              session=session, env=env)

    def injected(self, proc) -> str:
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(set(payload), {"hookSpecificOutput"})
        out = payload["hookSpecificOutput"]
        self.assertEqual(set(out), {"hookEventName", "additionalContext"})
        self.assertEqual(out["hookEventName"], "SessionStart")
        return out["additionalContext"]

    def test_resume_and_compact_inject_bound_task(self):
        for source in ("resume", "compact"):
            text = self.injected(self.run_session_start(source=source))
            self.assertIn("<copilot-session-recovery>", text)
            self.assertIn("</copilot-session-recovery>", text)
            self.assertIn(self.state()["task_id"], text)
            self.assertIn(self.state_path, text)
            self.assertIn("C1", text)
            self.assertIn("不是用户新指令", text)
            self.assertIn("不重做", text)
            self.assertLessEqual(len(text), 1600)

    def test_startup_and_clear_do_not_inject(self):
        for source in ("startup", "clear"):
            proc = self.run_session_start(source=source)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "")
            self.assertEqual(proc.stderr.strip(), "")

    def test_other_session_task_is_not_injected(self):
        # 任务绑定的是 SESSION；当前会话是 OTHER_SESSION，属于其它会话的任务。
        proc = self.run_session_start(session=OTHER_SESSION)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_unbound_task_is_not_injected(self):
        os.unlink(os.path.join(self.task_dir, "session.json"))
        proc = self.run_session_start()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertNotIn("task_id", proc.stdout)

    def test_missing_session_id_does_not_inject(self):
        proc = self.run_session_start(session=None)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_workspace_or_task_disable_skips_injection(self):
        marker = os.path.join(self.workspace, ".copilot-tasks", ".stop-guard-off")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("{}")
        self.assertEqual(self.run_session_start().stdout.strip(), "")
        os.unlink(marker)
        state = self.state()
        state["stop_guard"] = False
        self.save(state)
        self.assertEqual(self.run_session_start().stdout.strip(), "")

    def test_bound_corrupt_state_still_gets_repair_hint(self):
        # 本会话负责的任务状态损坏时不能静默跳过：否则会话会以为没有待续跑任务。
        bad = os.path.join(self.workspace, ".copilot-tasks", "bad")
        os.makedirs(bad, exist_ok=True)
        with open(os.path.join(bad, "state.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.bind(SESSION, task_dir=bad)
        text = self.injected(self.run_session_start())
        self.assertIn("任务状态读取异常", text)
        self.assertIn("不得伪造 PASS", text)

    def test_unbound_corrupt_state_is_not_injected(self):
        # 不属于本会话的损坏任务不进入本会话上下文；只有绑定本会话的任务才注入。
        bad = os.path.join(self.workspace, ".copilot-tasks", "bad")
        os.makedirs(bad, exist_ok=True)
        with open(os.path.join(bad, "state.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        os.unlink(os.path.join(self.task_dir, "session.json"))
        proc = self.run_session_start()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_closed_task_is_not_injected(self):
        state = self.state()
        state["closure"] = {"closed": True}
        self.save(state)
        self.assertEqual(self.run_session_start().stdout.strip(), "")

    def test_injection_is_read_only(self):
        tracked = [self.state_path, os.path.join(self.task_dir, "session.json")]
        before = {path: read_bytes(path) for path in tracked}
        self.run_session_start()
        self.assertEqual(before, {path: read_bytes(path) for path in tracked},
                         "恢复注入不得写状态或绑定文件")

    def test_other_workspace_task_is_not_injected(self):
        other_ws = tempfile.TemporaryDirectory()
        self.addCleanup(other_ws.cleanup)
        other_task = os.path.join(other_ws.name, ".copilot-tasks", "t9")
        os.makedirs(other_task, exist_ok=True)
        other_state = os.path.join(other_task, "state.json")
        G.start_state(other_state, goal="另一个工作区", contract=CONTRACT,
                      test_policy={"required": False}, workspace=other_ws.name)
        self.bind(SESSION, task_dir=other_task)
        text = self.injected(self.run_session_start())
        self.assertNotIn(other_ws.name, text)
        self.assertIn(self.workspace, text)


if __name__ == "__main__":
    unittest.main()

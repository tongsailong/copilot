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
import context_engine as CE  # noqa: E402

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
        environment.setdefault("COPILOT_SOL_AUTO", "off")
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

    def recall_fixture(self) -> dict:
        """临时知识根 + 临时召回状态，避免测试读写真实 Memory/Vault。"""
        subprocess.run(["git", "init", "-q"], cwd=self.workspace, check=True,
                       capture_output=True)
        base = os.path.join(self.workspace, "recall-fixture")
        memory_root = os.path.join(base, "memories")
        vault_root = os.path.join(base, "vault")
        state_root = os.path.join(base, "state")
        _, hash16 = CE.project_slug(os.path.realpath(self.workspace))
        memory_dir = os.path.join(memory_root, "demo-" + hash16, "memory")
        os.makedirs(memory_dir, exist_ok=True)
        with open(os.path.join(memory_dir, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write("- [Hook 续跑](hook-note.md) — stop hook 续跑与 session 绑定\n")
        with open(os.path.join(memory_dir, "hook-note.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: hook-note\nmetadata:\n  type: project\n---\n"
                     "- stop hook 续跑必须绑定 session；已验证 exit 2 生效。\n")
        vault_dir = os.path.join(vault_root, "15-Engineering-Memory", "demo")
        os.makedirs(vault_dir, exist_ok=True)
        with open(os.path.join(vault_dir, "hook-session.md"), "w", encoding="utf-8") as fh:
            fh.write("---\ntype: engineering-memory\nstatus: active\n"
                     "last_verified: 2026-11-20\n---\n# stop hook 与 session 绑定\n"
                     "stop hook 续跑：session 绑定后 exit 2 生效，已验证。\n")
        return {"COPILOT_RECALL_MEMORY_ROOT": memory_root,
                "COPILOT_RECALL_VAULT_ROOT": vault_root,
                "COPILOT_RECALL_STATE_DIR": state_root}


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

    def capture_ack(self, *, task_dir: str | None = None, status: str = "done"):
        return self.run_guard(args=["capture-ack", "--task-dir", task_dir or self.task_dir,
                                    "--status", status, "--reason", "test"])

    def test_closed_task_requires_one_capture_then_allows(self):
        state = self.state()
        state["closure"] = dict(state.get("closure") or {})
        state["closure"]["closed"] = True
        state["closure"]["closed_at"] = "2026-11-20T00:00:00+00:00"
        self.save(state)
        before = read_bytes(self.state_path)
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("copilot-capture-required", proc.stderr)
        self.assertIn("Capture", proc.stderr)
        self.assertEqual(read_bytes(self.state_path), before, "Capture 提示不得改写任务状态")
        ack = self.capture_ack()
        self.assertEqual(ack.returncode, 0, ack.stdout + ack.stderr)
        self.assertIn("copilot-capture-ack/v1", ack.stdout)
        self.assertEqual(read_bytes(self.state_path), before)
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stderr.strip(), "")
        # 重复回执幂等：同一终止版本不会再次要求 Capture。
        self.capture_ack()
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_fully_blocked_task_requires_capture_then_allows(self):
        G.add_block(self.state_path, "缺少真机通道授权", "用户启用设备工具",
                    work_items=["A1", "A2"], criteria=["C1", "C2"])
        self.assertEqual(G.evaluate(self.state())["phase"], "BLOCKED")
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("真实阻塞", proc.stderr)
        self.assertIn("可复用教训", proc.stderr)
        ack = self.capture_ack(status="skipped")
        self.assertEqual(ack.returncode, 0, ack.stdout + ack.stderr)
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stderr.strip(), "")

    def test_capture_gate_ignores_closure_before_binding(self):
        state = self.state()
        state["closure"] = dict(state.get("closure") or {})
        state["closure"]["closed"] = True
        state["closure"]["closed_at"] = "2026-11-19T00:00:00+00:00"
        self.save(state)
        # 本会话在收口之后才绑定该任务：不是本会话的成果，不要求补 Capture。
        payload = {"sessions": [{"session_id": SESSION, "at": "2026-11-20T00:00:00+00:00"}],
                   "updated": "2026-11-20T00:00:00+00:00"}
        with open(os.path.join(self.task_dir, "session.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

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

    def test_no_session_identity_with_multiple_tasks_does_not_pick_one(self):
        # 无会话标识且存在多个候选任务：不得按“最近更新”替某个会话做续跑决定，
        # 否则会把别人的任务强制续跑到当前会话（跨会话错误绑定）。
        second = os.path.join(self.workspace, ".copilot-tasks", "t2")
        os.makedirs(second, exist_ok=True)
        state_path = os.path.join(second, "state.json")
        G.start_state(state_path, goal="第二个任务", contract=CONTRACT,
                      test_policy={"required": False}, workspace=self.workspace)
        G.plan_state(state_path, PLAN)
        self.bind(OTHER_SESSION, second)
        proc = self.run_guard(session=None, payload={"cwd": self.workspace})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stderr.strip(), "")
        self.assertEqual(proc.stdout.strip(), "")

    def test_no_session_identity_with_single_task_still_enforces(self):
        # 只有一个候选任务时归属无歧义：保留强制续跑能力，不因缺标识放行。
        proc = self.run_guard(session=None, payload={"cwd": self.workspace})
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("COPILOT 续跑守卫", proc.stderr)
        self.assertIn(self.state()["task_id"], proc.stderr)

    def test_no_session_identity_with_anomaly_fails_closed(self):
        # 多任务且无标识时虽不选任务，但状态异常仍 fail-closed（不静默放行）。
        second = os.path.join(self.workspace, ".copilot-tasks", "t2")
        os.makedirs(second, exist_ok=True)
        state_path = os.path.join(second, "state.json")
        G.start_state(state_path, goal="第二个任务", contract=CONTRACT,
                      test_policy={"required": False}, workspace=self.workspace)
        G.plan_state(state_path, PLAN)
        with open(os.path.join(second, "session.json"), "w", encoding="utf-8") as fh:
            fh.write("[]")
        proc = self.run_guard(session=None, payload={"cwd": self.workspace})
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("fail-closed", proc.stderr)
        self.assertIn("session.json", proc.stderr)

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
        environment.setdefault("COPILOT_SOL_AUTO", "off")
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
        environment.setdefault("COPILOT_SOL_AUTO", "off")
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
        environment.setdefault("COPILOT_SOL_AUTO", "off")
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
                environment.setdefault("COPILOT_SOL_AUTO", "off")
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
        environment.setdefault("COPILOT_SOL_AUTO", "off")
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
        self.assertEqual(group.get("matcher"), "startup|resume|compact")
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

    resume/compact 且任务绑定本会话：注入完整恢复快照。
    其它情况（startup、任务归属其它会话或未绑定）：只注入只读指针，不披露完整
    任务正文（状态路径/续接命令），避免把无关会话引向旧任务。
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

    def test_resume_replays_prior_recall_without_search(self):
        env = self.recall_fixture()
        proc = self.run_guard(
            args=["user-prompt"],
            payload={"cwd": self.workspace, "session_id": SESSION,
                     "prompt": "帮我修复 stop hook 的 session 绑定问题"},
            env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        text = self.injected(self.run_session_start(source="resume", env=env))
        self.assertIn("<copilot-recall-state>", text)
        self.assertIn("Memory/hook-note.md", text)
        self.assertIn("不重复检索", text)

    def test_startup_and_clear_do_not_inject(self):
        for source in ("startup", "clear"):
            proc = self.run_session_start(source=source)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "")
            self.assertEqual(proc.stderr.strip(), "")

    def test_other_session_task_gets_pointer_not_snapshot(self):
        # 任务绑定的是 SESSION；当前会话是 OTHER_SESSION，属于其它会话的任务。
        # 只给只读指针（避免重启换会话标识后彻底丢失现场），不给恢复快照、不绑定。
        text = self.injected(self.run_session_start(session=OTHER_SESSION))
        self.assertIn("<copilot-task-pointer>", text)
        self.assertIn(self.state()["task_id"], text)
        self.assertIn("归属其它会话", text)
        self.assertIn("不是用户新指令", text)
        self.assertNotIn("<copilot-session-recovery>", text)
        self.assertNotIn(self.state_path, text)

    def test_unbound_task_gets_pointer_not_snapshot(self):
        os.unlink(os.path.join(self.task_dir, "session.json"))
        text = self.injected(self.run_session_start())
        self.assertIn("<copilot-task-pointer>", text)
        self.assertIn("未绑定会话", text)
        self.assertIn("不是用户新指令", text)
        self.assertNotIn("<copilot-session-recovery>", text)
        self.assertNotIn(self.state_path, text)

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

    def test_unbound_corrupt_state_gets_pointer_repair_hint(self):
        # 不属于本会话的损坏任务不注入完整恢复快照；但只读指针仍提示异常，
        # 避免会话误以为工作区没有未完成工作而丢弃现场。
        bad = os.path.join(self.workspace, ".copilot-tasks", "bad")
        os.makedirs(bad, exist_ok=True)
        with open(os.path.join(bad, "state.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        os.unlink(os.path.join(self.task_dir, "session.json"))
        text = self.injected(self.run_session_start())
        self.assertIn("<copilot-task-pointer>", text)
        self.assertIn("任务状态读取异常", text)
        self.assertIn("不得伪造 PASS", text)
        self.assertNotIn("<copilot-session-recovery>", text)

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

    def test_startup_with_unbound_task_gets_pointer(self):
        # 新会话（startup）没有绑定任务，但工作区仍有未完成现场：只给指针。
        os.unlink(os.path.join(self.task_dir, "session.json"))
        text = self.injected(self.run_session_start(source="startup"))
        self.assertIn("<copilot-task-pointer>", text)
        self.assertIn("未绑定会话", text)
        self.assertNotIn("<copilot-session-recovery>", text)
        self.assertNotIn(self.state_path, text)

    def test_pointer_includes_blocked_reason_and_needs(self):
        G.add_block(self.state_path, "缺少真机", "设备可用",
                    work_items=["A2"], criteria=["C2"])
        os.unlink(os.path.join(self.task_dir, "session.json"))
        text = self.injected(self.run_session_start(source="startup"))
        self.assertIn("缺少真机", text)
        self.assertIn("需要：设备可用", text)


class UserPromptHookTests(StopGuardBase):
    """UserPromptSubmit：用户每回合带进新授权时重新提示未闭合任务。"""

    def run_prompt(self, *, payload: dict | None = None,
                   text: str | None = None, env: dict | None = None,
                   session: str | None = SESSION):
        return self.run_guard(args=["user-prompt"], payload=payload,
                              text=text, env=env, session=session)

    def injected(self, proc) -> str:
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        data = json.loads(proc.stdout)
        out = data["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "UserPromptSubmit")
        return out["additionalContext"]

    def test_open_task_is_injected_with_resume_rules(self):
        text = self.injected(self.run_prompt())
        self.assertIn("<copilot-task-pointer>", text)
        self.assertIn(self.state()["task_id"], text)
        self.assertIn("不是用户新指令", text)
        self.assertIn("unblock --by user --quote", text)
        self.assertIn("先 status --json", text)
        self.assertLessEqual(len(text), 1800)

    def test_blocked_task_shows_reason_and_needs(self):
        G.add_block(self.state_path, "等待官方答复", "官方工单回复",
                    work_items=["A1"], criteria=["C1"])
        text = self.injected(self.run_prompt())
        self.assertIn("等待官方答复", text)
        self.assertIn("官方工单回复", text)
        self.assertIn("A1", text)

    def test_foreign_task_is_not_expanded_per_turn(self):
        # 任务绑定 SESSION；当前会话是 OTHER_SESSION。逐回合提示不得展开（也不重复披露）
        # 别的会话的旧任务——现场由 SessionStart 指针披露一次。
        G.add_block(self.state_path, "等待官方答复", "官方工单回复",
                    work_items=["A1"], criteria=["C1"])
        proc = self.run_prompt(
            payload={"cwd": self.workspace, "session_id": OTHER_SESSION,
                     "prompt": "你好"},
            session=OTHER_SESSION)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_own_task_suppresses_foreign_detail(self):
        # 本会话有任务时只展开本会话的，其它任务给一行计数，不展开原因。
        other_task = os.path.join(self.workspace, ".copilot-tasks", "t2")
        os.makedirs(other_task, exist_ok=True)
        other_state = os.path.join(other_task, "state.json")
        G.start_state(other_state, goal="别人的任务", contract=CONTRACT,
                      test_policy={"required": False}, workspace=self.workspace)
        G.plan_state(other_state, PLAN)
        self.bind(OTHER_SESSION, task_dir=other_task)
        G.add_block(other_state, "别人的阻塞原因", "别人的条件",
                    work_items=["A1"], criteria=["C1"])
        text = self.injected(self.run_prompt())
        self.assertIn(self.state()["task_id"], text)
        self.assertIn("归属其它会话或未绑定会话", text)
        self.assertNotIn("别人的阻塞原因", text)

    def test_missing_session_id_still_points_at_single_task(self):
        # 宿主没给会话标识且只有一个候选任务：归属无歧义，不能整块静默。
        text = self.injected(self.run_prompt(payload={"cwd": self.workspace},
                                            session=None))
        self.assertIn("<copilot-task-pointer>", text)
        self.assertIn(self.state()["task_id"], text)

    def test_missing_session_id_with_multiple_tasks_reports_ambiguity(self):
        # 无会话标识 + 多个候选任务：只报数量，不展开任一个任务的细节（也不挑最近的），
        # 避免把别的会话的任务当成本会话的来恢复。
        other_task = os.path.join(self.workspace, ".copilot-tasks", "t2")
        os.makedirs(other_task, exist_ok=True)
        other_state = os.path.join(other_task, "state.json")
        G.start_state(other_state, goal="别人的任务", contract=CONTRACT,
                      test_policy={"required": False}, workspace=self.workspace)
        G.plan_state(other_state, PLAN)
        self.bind(OTHER_SESSION, task_dir=other_task)
        G.add_block(other_state, "别人的阻塞原因", "别人的条件",
                    work_items=["A1"], criteria=["C1"])
        other_id = G.load_state(other_state)["task_id"]
        text = self.injected(self.run_prompt(payload={"cwd": self.workspace},
                                            session=None))
        self.assertIn("<copilot-task-pointer>", text)
        self.assertIn("无法判定归属", text)
        self.assertIn("2 个未闭合任务", text)
        self.assertNotIn(self.state()["task_id"], text)
        self.assertNotIn(other_id, text)
        self.assertNotIn("别人的阻塞原因", text)

    def test_no_open_task_does_not_inject(self):
        state = self.state()
        state["closure"] = {"closed": True}
        self.save(state)
        proc = self.run_prompt()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_recall_works_without_tasks_dir(self):
        env = self.recall_fixture()
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        subprocess.run(["git", "init", "-q"], cwd=outside.name, check=True,
                       capture_output=True)
        text = self.injected(self.run_prompt(
            payload={"cwd": outside.name, "session_id": SESSION,
                     "prompt": "帮我修复 stop hook 的 session 绑定问题"},
            env=env))
        self.assertNotIn("<copilot-task-pointer>", text)
        self.assertIn("<copilot-recall>", text)
        self.assertIn("Vault/15-Engineering-Memory/demo/hook-session.md", text)
        self.assertIn("核验 2026-11-20", text)

    def test_social_prompt_keeps_task_pointer_without_recall(self):
        env = self.recall_fixture()
        text = self.injected(self.run_prompt(
            payload={"cwd": self.workspace, "session_id": SESSION, "prompt": "你好"},
            env=env))
        self.assertIn("<copilot-task-pointer>", text)
        self.assertNotIn("<copilot-recall>", text)

    def test_recall_failure_does_not_break_user_prompt(self):
        env = self.recall_fixture()
        env["COPILOT_RECALL_MEMORY_ROOT"] = os.path.join(self.workspace, "missing-mem")
        env["COPILOT_RECALL_VAULT_ROOT"] = os.path.join(self.workspace, "missing-vault")
        text = self.injected(self.run_prompt(
            payload={"cwd": self.workspace, "session_id": SESSION,
                     "prompt": "帮我修复 stop hook 的 session 绑定问题"},
            env=env))
        self.assertIn("<copilot-task-pointer>", text)
        self.assertNotIn("<copilot-recall>", text)

    def test_marker_off_disables_recall_too(self):
        env = self.recall_fixture()
        marker = os.path.join(self.workspace, ".copilot-tasks", ".stop-guard-off")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("off\n")
        proc = self.run_prompt(
            payload={"cwd": self.workspace, "session_id": SESSION,
                     "prompt": "帮我修复 stop hook 的 session 绑定问题"},
            env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_marker_off_does_not_inject(self):
        marker = os.path.join(self.workspace, ".copilot-tasks", ".stop-guard-off")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("{}")
        proc = self.run_prompt()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_malformed_input_fails_open(self):
        proc = self.run_prompt(text="{not json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_prompt_hook_is_read_only(self):
        tracked = [self.state_path, os.path.join(self.task_dir, "session.json")]
        before = {path: read_bytes(path) for path in tracked}
        self.run_prompt(text=json.dumps({"cwd": self.workspace,
                                         "prompt": "继续"}))
        self.assertEqual(before, {path: read_bytes(path) for path in tracked},
                         "用户回合提示不得写状态或绑定文件")


if __name__ == "__main__":
    unittest.main()

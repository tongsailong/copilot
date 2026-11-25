#!/usr/bin/env python3
"""xmc=3 适配回归：单回合 3 次续跑上限、跨回合交接与 Goal 模式边界。

宿主每回合最多接受 3 次 Stop 自动续跑（发行版硬编码），第 4 次起退出码 2 被忽略。
守卫因此必须把“上限存在、剩余次数不可知、一次拦截做一件可验证的事、结束前落盘、
不得改状态规避”写进注入文本，同时保持只读、不持久化计数器。
"""
from __future__ import annotations

import json
import os
import re
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
AUTO = os.path.join(SCRIPTS, "copilot_auto.py")
SKILL = os.path.join(ROOT, "SKILL.md")
RUNTIME = os.path.join(ROOT, "references", "runtime.md")
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
NEW_SESSION = "sess_next_turn_0002"


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


class WorkspaceCase(unittest.TestCase):
    """建一个绑定当前会话的 PARTIAL 任务。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = self.tmp.name
        self.task_dir = os.path.join(self.workspace, ".copilot-tasks", "t1")
        os.makedirs(self.task_dir, exist_ok=True)
        self.state_path = os.path.join(self.task_dir, "state.json")
        G.start_state(self.state_path, goal="上限适配回归", contract=CONTRACT,
                      test_policy={"required": False}, workspace=self.workspace)
        G.plan_state(self.state_path, PLAN)
        self.bind(SESSION)

    def bind(self, session: str) -> None:
        payload = {"sessions": [{"session_id": session,
                                 "at": "2026-11-20T00:00:00+00:00"}],
                   "updated": "2026-11-20T00:00:00+00:00"}
        with open(os.path.join(self.task_dir, "session.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    def run_guard(self, *, payload: dict | None = None, session: str | None = SESSION,
                  cwd: str | None = None):
        environment = dict(os.environ)
        environment.setdefault("COPILOT_COGNITION_AUTO", "off")
        for key in ("COPILOT_STOP_GUARD", "COPILOT_STOP_GUARD_LOG",
                    "ZCODE_SESSION_ID", "CLAUDE_SESSION_ID",
                    "CLAUDE_CODE_SESSION_ID"):
            environment.pop(key, None)
        if session is not None:
            environment["ZCODE_SESSION_ID"] = session
        data = dict(payload or {"cwd": cwd or self.workspace})
        if session is not None:
            data.setdefault("session_id", session)
        return subprocess.run(
            [sys.executable, "-B", GUARD], input=json.dumps(data),
            capture_output=True, text=True, cwd=cwd or self.workspace,
            env=environment,
        )


class ContinuationCapTests(WorkspaceCase):
    def test_states_host_cap_and_its_origin(self):
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        text = proc.stderr
        self.assertIn("3 次", text)
        self.assertIn("硬编码", text)
        self.assertIn("不可配置", text)

    def test_does_not_fabricate_remaining_count(self):
        for continued in (False, True):
            proc = self.run_guard(payload={"cwd": self.workspace,
                                           "stop_hook_active": continued})
            text = proc.stderr
            # 守卫拿不到计数：不得给出剩余次数或“最后一次”的断言。
            self.assertIsNone(re.search(r"还剩\s*\d", text), text)
            self.assertIsNone(re.search(r"还有\s*\d+\s*次", text), text)
            self.assertIsNone(re.search(r"剩余\s*\d+\s*次", text), text)
            self.assertNotIn("下一次 Stop", text)
            self.assertNotIn("将放行", text)
            self.assertNotIn("最后一次机会", text)
            if continued:
                self.assertIn("剩余次数未知", text)

    def test_one_minimal_verifiable_unit_per_interception(self):
        text = self.run_guard().stderr
        self.assertIn("最小可验证工作单元", text)
        self.assertIn("一个工作项或一条完成条件", text)
        self.assertIn("不要用只读核对", text)

    def test_requires_persisted_handoff_before_ending(self):
        text = self.run_guard().stderr
        self.assertIn("落盘", text)
        self.assertIn("record/report", text)
        self.assertIn("evidence --add", text)
        self.assertIn("未完成工作项", text)
        self.assertIn("不重做已完成工作", text)
        self.assertIn("不覆盖既有证据", text)

    def test_emits_copyable_absolute_resume_command(self):
        text = self.run_guard().stderr
        self.assertIn("可原样粘贴的续接命令", text)
        self.assertIn(os.path.join(SCRIPTS, "copilot_auto.py"), text)
        self.assertIn("drive --state " + os.path.abspath(self.state_path), text)

    def test_forbids_status_rewrite_to_evade_host_limit(self):
        text = self.run_guard().stderr
        self.assertIn("不得为结束回合把 PARTIAL 改写成 BLOCKED 或 PASS", text)
        self.assertIn("改状态规避", text)
        self.assertIn("G1–G3", text)

    def test_third_continuation_keeps_full_handoff_and_read_only(self):
        state_before = read_bytes(self.state_path)
        session_before = read_bytes(os.path.join(self.task_dir, "session.json"))
        listing_before = sorted(os.listdir(self.task_dir))
        for _ in range(3):
            proc = self.run_guard(payload={"cwd": self.workspace,
                                           "stop_hook_active": True})
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            text = proc.stderr
            self.assertIn("可原样粘贴的续接命令", text)
            self.assertIn(self.state_path, text)
            self.assertIn("落盘", text)
            self.assertLessEqual(len(text), 4000, "宿主把 stderr 截断到 4000 字符")
        # 守卫只读：不写状态、不写绑定、不产生计数器文件。
        self.assertEqual(read_bytes(self.state_path), state_before)
        self.assertEqual(read_bytes(os.path.join(self.task_dir, "session.json")),
                         session_before)
        self.assertEqual(sorted(os.listdir(self.task_dir)), listing_before)


class CrossTurnResumeTests(WorkspaceCase):
    def test_next_turn_reuses_state_without_redoing_done_work(self):
        G.record_action(self.state_path, "A1", "done", result="已完成实现")
        state = G.load_state(self.state_path)
        entry = G.register_evidence(state, "E1", text="diff 与测试输出")
        G.save_state(self.state_path, state)
        evidence_sha = entry["sha256"]
        state_before = read_bytes(self.state_path)

        # 新回合、新会话：只读 status 不建立绑定，守卫不劫持。
        proc = self.run_guard(session=NEW_SESSION)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(read_bytes(self.state_path), state_before)

        # 真正续接要先跑一次会改写状态的命令（drive），建立本回合绑定。
        bound = subprocess.run([sys.executable, "-B", AUTO, "drive",
                                "--state", self.state_path],
                               capture_output=True, text=True,
                               cwd=self.workspace,
                               env={**os.environ, "ZCODE_SESSION_ID": NEW_SESSION,
                                    "COPILOT_COGNITION_AUTO": "off"})
        self.assertIn(bound.returncode, (0, 10, 11), bound.stdout + bound.stderr)

        proc = self.run_guard(session=NEW_SESSION)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("t1", proc.stderr)

        state = G.load_state(self.state_path)
        actions = {a["id"]: a for a in state["actions"]}
        self.assertEqual(actions["A1"]["status"], "done")
        sha = {e["id"]: e["sha256"] for e in state["evidence"]}["E1"]
        self.assertEqual(sha, evidence_sha)
        cont = G.continuation(state)
        self.assertNotIn("A1", cont.get("executable") or [])

    def test_other_workspace_task_is_not_hijacked(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        other_dir = os.path.join(other.name, ".copilot-tasks", "t9")
        os.makedirs(other_dir, exist_ok=True)
        other_state = os.path.join(other_dir, "state.json")
        G.start_state(other_state, goal="另一个工作区", contract=CONTRACT,
                      test_policy={"required": False}, workspace=other.name)
        G.plan_state(other_state, PLAN)
        # 两个工作区的任务都绑定同一会话：判定必须按 cwd 隔离，不得串扰。
        with open(os.path.join(other_dir, "session.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"sessions": [{"session_id": SESSION,
                                    "at": "2026-11-20T00:00:00+00:00"}]}, fh)

        here = self.run_guard()
        self.assertEqual(here.returncode, 2, here.stdout + here.stderr)
        self.assertIn("t1", here.stderr)
        self.assertNotIn("t9", here.stderr, "不得把其它工作区的任务当成当前任务")

        there = self.run_guard(payload={"cwd": other.name})
        self.assertEqual(there.returncode, 2, there.stdout + there.stderr)
        self.assertIn("t9", there.stderr)
        self.assertNotIn("t1", there.stderr, "不得把本工作区的任务带进其它工作区")


class GoalModeDocsTests(unittest.TestCase):
    def setUp(self):
        self.skill = read_text(SKILL)
        self.runtime = read_text(RUNTIME)

    def test_goal_is_optional_and_user_initiated(self):
        for doc in (self.skill, self.runtime):
            self.assertIn("只能由用户发起", doc)
        self.assertIn("Goal 模式（可选，仅用户发起）", self.skill)
        self.assertIn("不是默认机制", self.skill)

    def test_goal_constraints_documented(self):
        for doc in (self.skill, self.runtime):
            self.assertIn("Goal 与 Plan 互斥", doc)
            self.assertIn("objective", doc)
            self.assertIn("yolo", doc)
            self.assertIn("常驻", doc)
        self.assertIn("objective 对齐合同目标", self.skill)
        self.assertIn("不调用未公开", self.skill + self.runtime)

    def test_goal_does_not_override_copilot_gates(self):
        for doc in (self.skill, self.runtime):
            self.assertIn("G1–G3", doc)
            self.assertIn("验收仍以 Copilot", doc)
            self.assertIn("工程 PASS", doc)
            self.assertRegex(doc, r"不能当作|不是工程 PASS")

    def test_no_auto_yolo_bypass(self):
        for doc in (self.skill, self.runtime):
            self.assertRegex(doc, r"不得用[^\n]*yolo")
            self.assertIn("权限客户端", doc)

    def test_continuation_cap_and_handoff_documented(self):
        for doc in (self.skill, self.runtime):
            self.assertIn("3 次", doc)
            self.assertIn("剩余次数", doc)
            self.assertIn("同一 `--state`", doc)
            self.assertIn("不重做已完成工作", doc)
            self.assertIn("保持原授权", doc)


if __name__ == "__main__":
    unittest.main()

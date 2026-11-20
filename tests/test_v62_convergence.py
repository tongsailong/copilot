#!/usr/bin/env python3
"""v6.2 收敛：只读策略补全、G4 诊断、迭代生命周期、证据提醒与只读交付清单。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import governance as G  # noqa: E402
import native_bridge as NB  # noqa: E402
import v6_support as F  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""


class CodeAssertions(unittest.TestCase):
    def assert_code(self, code: str, func, *args, **kwargs) -> None:
        with self.assertRaises(NB.BridgeError) as ctx:
            func(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))




class IterationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="目标", contract=CONTRACT)
        G.plan_state(self.state_path, {"summary": "s", "actions": [
            {"id": "A1", "title": "实现", "goal": "产出结果",
             "expected_evidence": "结果文件"}]})

    def test_new_iteration_ends_previous(self):
        state = G.load_state(self.state_path)
        G.begin_iteration(state, note="第一轮")
        G.begin_iteration(state, note="第二轮")
        iterations = state["iterations"]
        self.assertEqual(len(iterations), 2)
        self.assertIsNotNone(iterations[0]["ended"])
        self.assertEqual(iterations[0]["phase"], "ENDED")
        self.assertIsNone(iterations[1]["ended"])
        self.assertEqual(iterations[1]["phase"], "RUNNING")

    def test_close_ends_open_iteration(self):
        state = G.load_state(self.state_path)
        G.begin_iteration(state, note="唯一一轮")
        G.save_state(self.state_path, state)
        evidence = os.path.join(self.dir, "result.txt")
        with open(evidence, "w", encoding="utf-8") as fh:
            fh.write("ok\n")
        state = G.load_state(self.state_path)
        G.register_evidence(state, "E1", file_path=evidence)
        G.save_state(self.state_path, state)
        G.record_action(self.state_path, "A1", "done", result="完成",
                        evidence_ids=["E1"])
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=["E1"])
        G.close_task(self.state_path)
        iterations = G.load_state(self.state_path)["iterations"]
        self.assertEqual(iterations[0]["phase"], "CLOSED")
        self.assertIsNotNone(iterations[0]["ended"])

    def test_reopen_starts_new_iteration(self):
        state = G.load_state(self.state_path)
        G.begin_iteration(state, note="第一轮")
        G.save_state(self.state_path, state)
        evidence = os.path.join(self.dir, "result.txt")
        with open(evidence, "w", encoding="utf-8") as fh:
            fh.write("ok\n")
        state = G.load_state(self.state_path)
        G.register_evidence(state, "E1", file_path=evidence)
        G.save_state(self.state_path, state)
        G.record_action(self.state_path, "A1", "done", result="完成",
                        evidence_ids=["E1"])
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=["E1"])
        G.close_task(self.state_path)
        G.reopen_task(self.state_path, "用户追加需求", authorized_by="user")
        state = G.load_state(self.state_path)
        self.assertFalse(state["closure"]["closed"])
        iterations = state["iterations"]
        self.assertEqual(iterations[0]["phase"], "CLOSED", "历史迭代不可改写")
        self.assertEqual(iterations[-1]["phase"], "RUNNING")
        self.assertEqual(G._current_iteration_id(state),
                         iterations[-1]["iteration_id"])


class EvidenceWarningsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="目标", contract=CONTRACT)
        G.plan_state(self.state_path, {"summary": "s", "actions": [
            {"id": "A1", "title": "实现", "goal": "产出结果",
             "expected_evidence": "结果文件"}]})

    def _file(self, name: str, text: str) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_duplicate_path_and_content_warn_without_blocking(self):
        state = G.load_state(self.state_path)
        path = self._file("result.txt", "ok\n")
        G.register_evidence(state, "E1", file_path=path)
        G.register_evidence(state, "E2", file_path=path)
        twin = self._file("twin.txt", "ok\n")
        G.register_evidence(state, "E3", file_path=twin)
        G.save_state(self.state_path, state)
        state = G.load_state(self.state_path)
        warnings = G.evidence_warnings(state)
        self.assertTrue(any("E1/E2" in w and "重复登记" in w for w in warnings), warnings)
        self.assertTrue(any("E1/E2/E3" in w and "内容完全相同" in w for w in warnings),
                        warnings)
        check = G.closure_check(state, task_dir=self.dir)
        self.assertTrue(check["warnings"], "收口检查应带出非阻断提醒")
        self.assertFalse(any("E1" in r.get("detail", "")
                             for r in check["reasons"]),
                         "证据提醒不应进入阻断原因")

    def test_temp_dir_evidence_warns(self):
        # 不依赖本机 TMPDIR：直接构造指向 /dev/shm 的已登记证据。
        state = {"evidence": [{"id": "E1", "path_hint": "/dev/shm/result.md",
                               "sha256": "0" * 64}]}
        warnings = G.evidence_warnings(state)
        self.assertTrue(any("临时目录" in w and "E1" in w for w in warnings),
                        warnings)

    def test_dropped_evidence_file_is_not_unregistered(self):
        evidence_dir = os.path.join(self.dir, "evidence")
        os.makedirs(evidence_dir)
        dropped = os.path.join(evidence_dir, "E1.md")
        with open(dropped, "w", encoding="utf-8") as fh:
            fh.write("归档证据\n")
        state = G.load_state(self.state_path)
        state["evidence"] = [{"id": "E1", "path_hint": dropped,
                              "sha256": "0" * 64, "dropped": True}]
        warnings = G.evidence_warnings(state, task_dir=self.dir)
        self.assertFalse(any("未登记文件" in w for w in warnings), warnings)

    def test_unregistered_evidence_file_warns(self):
        evidence_dir = os.path.join(self.dir, "evidence")
        os.makedirs(evidence_dir)
        stray = os.path.join(evidence_dir, "E7.md")
        with open(stray, "w", encoding="utf-8") as fh:
            fh.write("未登记\n")
        state = G.load_state(self.state_path)
        warnings = G.evidence_warnings(state, task_dir=self.dir)
        self.assertTrue(any("未登记文件" in w and "E7.md" in w for w in warnings), warnings)

    def test_closure_check_prints_warnings(self):
        state = G.load_state(self.state_path)
        path = self._file("result.txt", "ok\n")
        G.register_evidence(state, "E1", file_path=path)
        G.register_evidence(state, "E2", file_path=path)
        G.save_state(self.state_path, state)
        proc = F.run_cli("copilot_dispatch.py", "closure-check",
                         "--state", self.state_path)
        # 该夹具没有通过 G1–G3，退出码必须如实反映 FAIL（历史实现恒返回 0）。
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("提醒（不阻断）", proc.stdout)
        self.assertIn("重复登记", proc.stdout)

    def test_idempotent_evidence_add_is_reported(self):
        path = self._file("result.txt", "ok\n")
        first = F.run_cli("copilot_dispatch.py", "evidence", "--state",
                          self.state_path, "--add", "--id", "E1",
                          "--file", path)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("已登记", first.stdout)
        second = F.run_cli("copilot_dispatch.py", "evidence", "--state",
                           self.state_path, "--add", "--id", "E1",
                           "--file", path)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("已存在且摘要一致", second.stdout)

    def test_capture_refuses_existing_id_without_overwriting(self):
        path = self._file("result.txt", "original\n")
        first = F.run_cli("copilot_dispatch.py", "evidence", "--state",
                          self.state_path, "--add", "--id", "E1",
                          "--file", path)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = F.run_cli("copilot_dispatch.py", "evidence", "--state",
                           self.state_path, "--add", "--id", "E1",
                           "--capture-cmd", "printf captured")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("已登记或已归档", second.stderr + second.stdout)
        # 采集目标是 <state>/evidence/E1.md；拒绝必须发生在写文件之前。
        self.assertFalse(os.path.exists(os.path.join(self.dir, "evidence", "E1.md")),
                         "拒绝覆盖时不得创建/改写 evidence/E1.md")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "original\n", "原证据文件不得被改写")

    def test_unreadable_evidence_dir_warns(self):
        os.makedirs(os.path.join(self.dir, "evidence"))
        state = G.load_state(self.state_path)
        with mock.patch.object(G.os, "listdir", side_effect=OSError("denied")):
            warnings = G.evidence_warnings(state, task_dir=self.dir)
        self.assertTrue(any("无法读取证据目录" in w for w in warnings), warnings)


class HandoffTests(unittest.TestCase):
    """handoff 只读分类；不提交、不删除、不改写忽略规则。"""

    def setUp(self):
        if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
            self.skipTest("git 不可用")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "test")

    def _git(self, *args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=self.repo,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def _write(self, relative: str, text: str = "x\n") -> str:
        path = os.path.join(self.repo, relative)
        os.makedirs(os.path.dirname(path) or self.repo, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_classifies_worktree_without_writing(self):
        self._write("tracked.txt")
        self._git("add", "tracked.txt")
        self._git("commit", "-q", "-m", "init")
        self._write("tracked.txt", "changed\n")
        self._write("staged.txt")
        self._git("add", "staged.txt")
        task_dir = os.path.join(self.repo, ".copilot-tasks", "demo")
        os.makedirs(task_dir)
        state_path = os.path.join(task_dir, "state.json")
        G.start_state(state_path, goal="目标", contract=CONTRACT)
        self._write("screenshots/first.png")
        self._write("scratch/data.bin")

        before = self._git("status", "--porcelain=v1", "--untracked-files=all")
        proc = F.run_cli("copilot_dispatch.py", "handoff",
                         "--state", state_path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        after = self._git("status", "--porcelain=v1", "--untracked-files=all")
        self.assertEqual(before, after, "handoff 不得改变工作区或暂存区")

        report = json.loads(proc.stdout)
        self.assertTrue(report["git_available"])
        self.assertTrue(report["head"].strip())
        self.assertEqual([e["path"] for e in report["staged"]], ["staged.txt"])
        self.assertEqual([e["path"] for e in report["unstaged"]], ["tracked.txt"])
        buckets = report["untracked_buckets"]
        governance = next(v for k, v in buckets.items() if "治理记录" in k)
        self.assertIn(".copilot-tasks/demo/state.json", governance)
        artifacts = next(v for k, v in buckets.items() if "验收产物" in k)
        self.assertIn("screenshots/first.png", artifacts)
        unknown = next(v for k, v in buckets.items() if "未分类" in k)
        self.assertIn("scratch/data.bin", unknown)
        self.assertIn("不提交", report["note"])

    def test_rename_is_reported_with_source(self):
        self._write("old.txt")
        self._git("add", "old.txt")
        self._git("commit", "-q", "-m", "init")
        self._git("mv", "old.txt", "new.txt")
        proc = F.run_cli("copilot_dispatch.py", "handoff",
                         "--cwd", self.repo, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        staged = [e for e in report["staged"] if e["path"] == "new.txt"]
        self.assertTrue(staged, report["staged"])
        self.assertEqual(staged[0]["from"], "old.txt")
        self.assertEqual(report["unstaged"], [])

    def test_text_output_mentions_readonly_note(self):
        self._write("tracked.txt")
        self._git("add", "tracked.txt")
        self._git("commit", "-q", "-m", "init")
        self._write("new.txt")
        proc = F.run_cli("copilot_dispatch.py", "handoff", "--cwd", self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("工作区交付清单（只读）", proc.stdout)
        self.assertIn("未跟踪文件", proc.stdout)
        self.assertIn("不提交、不删除", proc.stdout)

    def test_non_git_directory_is_reported(self):
        with tempfile.TemporaryDirectory() as plain:
            proc = F.run_cli("copilot_dispatch.py", "handoff", "--cwd", plain)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("不是可用 git 仓库", proc.stdout)


if __name__ == "__main__":
    unittest.main()

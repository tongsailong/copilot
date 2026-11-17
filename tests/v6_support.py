#!/usr/bin/env python3
"""v6 测试共用夹具：任务状态、原生信封、宿主 metadata/rollout、CLI 调用。"""
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
import native_bridge as NB  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""


def run_cli(script: str, *args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    """以子进程运行 scripts/ 下的 CLI；返回完整 stdout/stderr 供断言。"""
    return subprocess.run(
        [sys.executable, "-B", os.path.join(SCRIPTS, script), *args],
        capture_output=True, text=True, cwd=cwd or ROOT,
    )


def init_git_workspace(directory: str) -> str:
    """把临时目录初始化为可生成代码范围快照的 git 工作区。"""
    os.makedirs(directory, exist_ok=True)
    subprocess.run(["git", "init", "-q", directory], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", directory, "config", "user.email", "test@example.invalid"],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", directory, "config", "user.name", "test"],
                   check=True, capture_output=True, text=True)
    with open(os.path.join(directory, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write("state.json\n.state.json.lock\nreview-attempts.jsonl\n__pycache__/\n*.pyc\n"
                 "metadata.json\nrollout*.jsonl\nreview-*.json\noutput*.json\n"
                 "*.json\n*.jsonl\n")
    with open(os.path.join(directory, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("test workspace\n")
    subprocess.run(["git", "-C", directory, "add", "."], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", directory, "commit", "-qm", "baseline"], check=True,
                   capture_output=True, text=True)
    return directory


class BridgeBase(unittest.TestCase):
    """构造一个 native 后端高风险任务，并提供合法信封/宿主记录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        init_git_workspace(self.dir)
        with open(os.path.join(self.dir, "work.py"), "w", encoding="utf-8") as fh:
            fh.write("VALUE = 1\n")
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="目标", contract=CONTRACT,
                      execution_backend="native", risk="high",
                      test_policy={"required": False})
        G.plan_state(self.state_path, {"summary": "两个工作项", "actions": [
            {"id": "A1", "title": "实现一", "goal": "产出结果",
             "expected_evidence": "结果文件"},
            {"id": "A2", "title": "实现二", "goal": "补充说明",
             "expected_evidence": "说明文件"},
        ]})
        self.state = G.load_state(self.state_path)
        self.evidence = os.path.join(self.dir, "evidence.txt")
        with open(self.evidence, "w", encoding="utf-8") as fh:
            fh.write("result\n")
        self.dispatch = NB.build_dispatch(self.state, G.ready_actions(self.state))
        self._bindings: dict[str, dict] = {}

    def binding(self, wid: str = "A1") -> dict:
        current = self._bindings.get(wid)
        if current:
            state = G.load_state(self.state_path)
            stored = G.find_execution_binding(state, current["binding_id"])
            if stored is not None and not stored.get("consumed"):
                return current
        state = G.load_state(self.state_path)
        if state.get("native_dispatch") != self.dispatch:
            state["native_dispatch"] = self.dispatch
            G.save_state(self.state_path, state)
        self._bindings[wid] = G.create_execution_binding(
            self.state_path, work_item_id=wid, workspace=self.dir)
        loaded = G.load_state(self.state_path)
        self.state.clear()
        self.state.update(loaded)
        return self._bindings[wid]

    # ── 信封构造 ──────────────────────────────────────────────────────────
    def item(self, wid: str = "A1") -> dict:
        return next(i for i in self.dispatch["actions"] if i["work_item_id"] == wid)

    def execution(self, wid: str = "A1", **overrides) -> dict:
        item = self.item(wid)
        env = {
            "schema": NB.RESULT_SCHEMA,
            "task_id": self.state["task_id"],
            "contract_sha256": self.state["contract_sha256"],
            "plan_id": self.dispatch["plan_id"],
            "plan_sha256": self.dispatch["plan_sha256"],
            "iteration_id": self.dispatch["iteration_id"],
            "dispatch_id": item["dispatch_id"],
            "idempotency_key": item["idempotency_key"],
            "work_item_id": wid,
            "status": "done",
            "summary": "完成",
            "result": "实际结果：文件已生成",
            "evidence": [{
                "id": "E1", "file": self.evidence,
                "sha256": NB.sha256_file(self.evidence), "kind": "file",
            }],
            "executor": {
                "kind": "subagent", "agent_id": "agent-exec",
                "child_session_id": "child-exec", "profile_id": "executor",
                "profile": {"tools": ["*"]},
                "model_reported": "some-model",
            },
            "timing": {"duration_ms": 1200},
            "usage": {"total_tokens": 321},
        }
        bound = self.binding(wid)
        env["execution_binding"] = {
            "binding_id": bound["binding_id"], "nonce": bound["nonce"],
            "created_at": bound["created_at"], "workspace": bound["workspace"],
            "test_commands": bound["test_commands"],
        }
        env.update(overrides)
        env["result_sha256"] = NB.envelope_sha256(env)
        return env


    def assert_code(self, code: str, func, *args, **kwargs) -> None:
        with self.assertRaises(NB.BridgeError) as ctx:
            func(*args, **kwargs)
        assert ctx.exception.code == code, str(ctx.exception)

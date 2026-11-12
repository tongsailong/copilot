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


class BridgeBase(unittest.TestCase):
    """构造一个 native 后端高风险任务，并提供合法信封/宿主记录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="目标", contract=CONTRACT,
                      execution_backend="native", risk="high")
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
                "model_reported": "some-model",
            },
            "timing": {"duration_ms": 1200},
            "usage": {"total_tokens": 321},
        }
        env.update(overrides)
        env["result_sha256"] = NB.envelope_sha256(env)
        return env

    def host_files(self) -> dict:
        meta = os.path.join(self.dir, "metadata.json")
        output = os.path.join(self.dir, "output.json")
        rollout = os.path.join(self.dir, "rollout.jsonl")
        for path, text in ((meta, "{}\n"), (output, "{}\n"), (rollout, "{}\n")):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        return {"metadata_path": meta, "output_file": output, "rollout_path": rollout}

    def review(self, execution: dict | None = None, **overrides) -> dict:
        execution = execution or self.execution()
        env = {
            "schema": NB.REVIEW_SCHEMA,
            "task_id": self.state["task_id"],
            "contract_sha256": self.state["contract_sha256"],
            "plan_id": self.dispatch["plan_id"],
            "plan_sha256": self.dispatch["plan_sha256"],
            "dispatch_id": execution["dispatch_id"],
            "work_item_id": execution["work_item_id"],
            "execution_result_sha256": execution["result_sha256"],
            "target_sha256": G.final_evidence_digest(self.state),
            "reviewer": {
                "kind": "subagent", "agent_id": "agent-review",
                "child_session_id": "child-review", "profile_id": "reviewer",
                "model_reported": "some-model",
            },
            "host": {
                **self.host_files(),
                "tool_calls": [{"name": "Read", "count": 2},
                               {"name": "Bash", "count": 1}],
                "agent_id": "agent-review", "child_session_id": "child-review",
            },
            "verdict": "pass",
            "findings": [],
            "files_inspected": [self.evidence],
            "commands_run": ["git status --short"],
            "changed_files": [],
            "unverified": [],
            "residual_risks": [],
            "g4_eligible": True,
        }
        env.update(overrides)
        env["result_sha256"] = NB.envelope_sha256(env)
        return env

    def assert_code(self, code: str, func, *args, **kwargs) -> None:
        with self.assertRaises(NB.BridgeError) as ctx:
            func(*args, **kwargs)
        assert ctx.exception.code == code, str(ctx.exception)


def make_fake_review_host(directory: str, *, execution: dict, dispatch: dict,
                          evidence: str, task_id: str) -> dict:
    """写一套可被 verify_review_host_record 重新核对的 Reviewer 宿主记录。"""
    agent = "agent-review"
    child = "child-review"
    output = os.path.join(directory, "review-output.json")
    meta = os.path.join(directory, "review-metadata.json")
    rollout = os.path.join(directory, "rollout-review.jsonl")
    payload = {
        "schema": NB.REVIEW_SCHEMA,
        "task_id": task_id,
        "plan_id": dispatch["plan_id"],
        "dispatch_id": execution["dispatch_id"],
        "work_item_id": execution["work_item_id"],
        "execution_result_sha256": execution["result_sha256"],
        "verdict": "pass",
        "findings": [],
        "files_inspected": [evidence],
        "commands_run": ["git status --short"],
        "changed_files": [],
        "unverified": [],
        "residual_risks": [],
    }
    with open(output, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    calls = [
        {"id": "t1", "name": "Read", "input": {"file_path": evidence}},
        {"id": "t2", "name": "Bash", "input": {"command": "git status --short"}},
    ]
    rows = [
        {"sessionId": child,
         "model": {"providerId": "fake", "modelId": "reviewer-model"}},
        {"sessionId": child, "response": {"toolCalls": calls}},
    ]
    with open(rollout, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    metadata = {
        "agentId": agent,
        "childSessionId": child,
        "profileId": "reviewer",
        "profileSnapshot": {
            "name": "reviewer",
            "tools": ["Read", "Grep", "Glob", "Bash"],
            "disallowedTools": ["Write", "Edit"],
        },
        "status": "completed",
        "totalToolUseCount": len(calls),
        "outputFile": output,
        "metadataFile": meta,
    }
    with open(meta, "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return {"metadata_path": meta, "output_file": output,
            "rollout_path": rollout,
            "tool_calls": [{"name": "Read", "count": 1},
                           {"name": "Bash", "count": 1}],
            "agent_id": agent, "child_session_id": child}

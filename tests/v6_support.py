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


def readonly_profile_policy(**overrides) -> dict:
    """宿主只读复核 Profile 的有效权限策略（与 profileSnapshot 派生字段同构）。

    正式 G4 只接受宿主内置 Agent（`source=built-in`）；名称/路径仍不参与判定，
    工具集与禁用项不能变。
    """
    policy = {
        "name": "reviewer",
        "source": "built-in",
        "path": "",
        "tools": ["Read", "Grep", "Glob"],
        "disallowedTools": ["Write", "Edit", "Bash", "WebFetch", "WebSearch",
                            "Agent", "Task", "TodoWrite", "NotebookEdit"],
        "permissionMode": "default",
    }
    policy.update(overrides)
    return policy


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


def new_attempt_id() -> str:
    return "RA" + os.urandom(12).hex()


def fake_attempt(state: dict, *, target: str, work_item: str = "A1",
                 consumed: bool = True, reviewer: dict | None = None,
                 scope: str = "full", based_on: str = "",
                 manifest: list[str] | None = None,
                 workspace: str = "") -> dict:
    """在状态里放一条与 attempt 同构的记录，供不经过 ingest 的门禁单测使用。"""
    reviewer = reviewer or {"agent_id": "agent-review",
                            "child_session_id": "child-review"}
    code_snapshot = G.capture_code_snapshot(
        workspace, exclude_paths=G._evidence_paths(state))
    attempt = {
        "review_attempt_id": new_attempt_id(),
        "task_id": str(state.get("task_id") or ""),
        "contract_sha256": str(state.get("contract_sha256") or ""),
        "plan_id": str((state.get("native_dispatch") or {}).get("plan_id") or ""),
        "plan_sha256": str((state.get("native_dispatch") or {}).get("plan_sha256") or ""),
        "dispatch_id": "",
        "work_item_id": work_item,
        "execution_result_sha256": "",
        "target_sha256": target,
        "criteria_sha256": G._review_criteria_state_digest(state),
        "code_snapshot": code_snapshot,
        "code_scope_sha256": code_snapshot.get("digest") or "",
        "evidence_snapshot": G._review_evidence_snapshot(state),
        "review_scope": scope,
        "based_on_review_sha256": based_on,
        "evidence_manifest": list(manifest or []),
        "created_at": G.now_iso(),
        "created_by": "test",
        "consumed": consumed,
        "consumed_by_result_sha256": "",
        "consumed_at": G.now_iso() if consumed else "",
        "reviewer": dict(reviewer) if consumed else {},
    }
    state.setdefault("review_attempts", []).append(attempt)
    return attempt


def attach_review(state: dict, record: dict, attempt: dict) -> dict:
    """测试专用：把复核记录与 attempt 消费一起写入状态（模拟 ingest 原子性）。"""
    bound = next(a for a in state.get("review_attempts") or []
                 if a.get("review_attempt_id") == attempt.get("review_attempt_id"))
    bound["consumed"] = True
    bound["consumed_by_result_sha256"] = str(record.get("result_sha256") or "")
    bound["consumed_at"] = G.now_iso()
    bound["reviewer"] = {
        "agent_id": str((record.get("reviewer") or {}).get("agent_id") or ""),
        "child_session_id": str(
            (record.get("reviewer") or {}).get("child_session_id") or ""),
    }
    state.setdefault("native_reviews", []).append(record)
    return record


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

    def host_files(self) -> dict:
        meta = os.path.join(self.dir, "metadata.json")
        output = os.path.join(self.dir, "output.json")
        rollout = os.path.join(self.dir, "rollout.jsonl")
        for path, text in ((meta, "{}\n"), (output, "{}\n"), (rollout, "{}\n")):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        return {"metadata_path": meta, "output_file": output, "rollout_path": rollout}

    def create_attempt(self, execution: dict, *, scope: str = "full",
                       based_on: str = "", manifest: list[str] | None = None) -> dict:
        """为已回填的执行结果创建真实 attempt（测试端到端绑定用）。"""
        state = G.load_state(self.state_path)
        if state.get("native_dispatch") != self.dispatch:
            state["native_dispatch"] = self.dispatch
            G.save_state(self.state_path, state)
        return G.create_review_attempt(
            self.state_path,
            work_item_id=str(execution.get("work_item_id") or ""),
            execution_result_sha256=str(execution.get("result_sha256") or ""),
            review_scope=scope, based_on_review_sha256=based_on,
            evidence_manifest=manifest, workspace=self.dir,
        )

    def review(self, execution: dict | None = None, attempt: dict | None = None,
               **overrides) -> dict:
        execution = execution or self.execution()
        attempt_id = (attempt or {}).get("review_attempt_id") or new_attempt_id()
        attempt_created = (attempt or {}).get("created_at") or \
            "2026-01-01T00:00:00+00:00"
        env = {
            "schema": NB.REVIEW_SCHEMA,
            "task_id": self.state["task_id"],
            "contract_sha256": self.state["contract_sha256"],
            "plan_id": self.dispatch["plan_id"],
            "plan_sha256": self.dispatch["plan_sha256"],
            "dispatch_id": execution["dispatch_id"],
            "work_item_id": execution["work_item_id"],
            "execution_result_sha256": execution["result_sha256"],
            "target_sha256": G.final_evidence_digest(
                G.load_state(self.state_path)),
            "review_attempt_id": attempt_id,
            "review_attempt_created_at": attempt_created,
            "reviewer": {
                "kind": "subagent", "agent_id": "agent-review",
                "child_session_id": "child-review", "profile_id": "reviewer",
                "profile": readonly_profile_policy(),
                "model_reported": "some-model",
            },
            "host": {
                **self.host_files(),
                "tool_calls": [{"name": "Read", "count": 2}],
                "agent_id": "agent-review", "child_session_id": "child-review",
                "workspace_root": self.dir,
            },
            "verdict": "pass",
            "findings": [],
            "files_inspected": [self.evidence],
            "commands_run": [],
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
                          evidence: str, task_id: str,
                          attempt_id: str = "",
                          session_created_at: str = "",
                          prompt: str = "", workspace: str = "") -> dict:
    """写一套模拟真实 Reviewer 宿主 metadata/output/rollout 的夹具。"""
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
        "commands_run": [],
        "changed_files": [],
        "unverified": [],
        "residual_risks": [],
    }
    with open(output, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    calls = [
        {"id": "t1", "name": "Read", "input": {"file_path": evidence}},
        {"id": "t2", "name": "Grep", "input": {"pattern": "result",
                                               "path": evidence}},
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
        "parentSessionId": "sess_parent",
        "profileId": "reviewer",
        "profileSnapshot": {
            "name": "reviewer",
            "source": "built-in",
            "tools": ["Read", "Grep", "Glob"],
            "disallowedTools": ["Write", "Edit", "Bash", "WebFetch",
                                "WebSearch", "Agent", "Task", "TodoWrite",
                                "NotebookEdit"],
        },
        "status": "completed",
        "totalToolUseCount": len(calls),
        "outputFile": output,
        "metadataFile": meta,
        # G4 快照绑定：会话开始时间必须晚于 attempt 创建，prompt 必须携带
        # attempt id。默认用当前时间，老会话测试显式传更早的时间。
        "createdAt": session_created_at or G.now_iso(),
        "completedAt": session_created_at or G.now_iso(),
        "prompt": prompt or (f"复核 {attempt_id}" if attempt_id else "复核"),
        "workspaceRoot": workspace or directory,
    }
    with open(meta, "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return {"metadata_path": meta, "output_file": output,
            "rollout_path": rollout,
            "tool_calls": [{"name": "Read", "count": 1},
                           {"name": "Grep", "count": 1}],
            "agent_id": agent, "child_session_id": child,
            "session_created_at": metadata["createdAt"],
            "session_completed_at": metadata["completedAt"],
            "prompt_sha256": NB.sha256_text(metadata["prompt"]),
            "workspace_root": metadata["workspaceRoot"]}

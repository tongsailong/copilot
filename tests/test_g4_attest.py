#!/usr/bin/env python3
"""L2 宿主取证契约测试：合成 metadata + 临时 SQLite 夹具。

覆盖原型 27 项矩阵与新增安全边界：permissionMode 放宽、MCP/skills、Profile 来源
与路径、符号链接/路径穿越/敏感范围、工具失败、part 与 tool_usage 不一致、rollout
缺失、宿主记录清理、重放、跨任务与自审、Reviewer 输出类型与绑定、代码快照边界。

这些测试不调用模型、不访问真实 ZCode 数据库、不修改真实任务台账。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import g4_attest as A  # noqa: E402

TASK = "task-g4-attest-fixture"
CHILD = "sess_subagent_agent_fixture"
PARENT = "sess_parent_fixture"
TRACE = "trace-fixture"
TURN = "turn-fixture"
CODE_DIGEST = "d" * 64  # 由 Fixture 按真实快照重算覆盖


def _sha(path: str) -> str:
    import hashlib
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _sha_text(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@contextmanager
def temp_home(root: str):
    """把 HOME 指向临时目录，避免测试触碰真实的 ~/.zcode/agents。"""
    old = os.environ.get("HOME")
    os.environ["HOME"] = root
    try:
        agents = os.path.join(root, ".zcode", "agents")
        os.makedirs(agents, exist_ok=True)
        yield agents
    finally:
        if old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old


class Fixture:
    """一次性构造可运行的合成宿主记录。"""

    def __init__(self, tmp: str):
        self.tmp = tmp
        self.ws = os.path.join(tmp, "ws")
        os.makedirs(os.path.join(self.ws, "src"), exist_ok=True)
        os.makedirs(os.path.join(self.ws, "evidence"), exist_ok=True)
        os.makedirs(os.path.join(self.ws, ".zcode", "agents"), exist_ok=True)
        self.source = os.path.join(self.ws, "src", "a.js")
        with open(self.source, "w", encoding="utf-8") as fh:
            fh.write("const LIMIT = 3;\n")
        self.evidence = os.path.join(self.ws, "evidence", "report.txt")
        with open(self.evidence, "w", encoding="utf-8") as fh:
            fh.write("PASS 4/4\n")
        self.profile = os.path.join(self.ws, ".zcode", "agents",
                                    "readonly-probe.md")
        with open(self.profile, "w", encoding="utf-8") as fh:
            fh.write("---\nname: readonly-probe\n---\nread only\n")
        self.attempt_id = "RA0123456789abcdef01234567"
        self.attempt = {
            "review_attempt_id": self.attempt_id,
            "task_id": TASK,
            "review_scope": "full",
            "created_at": "2026-11-15T08:00:00+08:00",
            "target_sha256": "a" * 64,
            "code_scope_sha256": CODE_DIGEST,
            "contract_sha256": "e" * 64,
            "work_item_id": "P1",
            "execution_result_sha256": "f" * 64,
            "code_snapshot": {
                "available": True,
                "workspace": self.ws,
                "git_root": self.ws,
                "git_head": "c" * 40,
                "files": [{
                    "path": "src/a.js", "status": " M",
                    "sha256": _sha(self.source), "size": 16, "kind": "file",
                }],
                "digest": CODE_DIGEST,
            },
            "evidence_snapshot": [{
                "id": "E1", "path": self.evidence,
                "sha256": _sha(self.evidence),
            }],
            "evidence_manifest": [],
            "consumed": False,
            "consumed_at": "",
        }
        self.meta = {
            "agentId": "agent_fixture",
            "childSessionId": CHILD,
            "parentSessionId": PARENT,
            "profileId": "readonly-probe",
            "status": "completed",
            "createdAt": "2026-11-15T09:00:00+08:00",
            "completedAt": "2026-11-15T09:05:00+08:00",
            "workspaceRoot": self.ws,
            "outputFile": os.path.join(tmp, "output.txt"),
            "totalToolUseCount": 3,
            "prompt": f"你是 attempt {self.attempt_id} 的只读复核方。",
            "profileSnapshot": {
                "name": "readonly-probe",
                "source": "project",
                "path": self.profile,
                "systemPrompt": "read only",
                "tools": ["Read", "Grep", "Glob"],
                "disallowedTools": ["Write", "Edit", "Bash"],
                "injectAgentsMd": False,
                "maxTurns": 20,
            },
        }
        self.code_digest = A._snapshot_digest(self.attempt["code_snapshot"])
        self.attempt["code_snapshot"]["digest"] = self.code_digest
        self.attempt["code_scope_sha256"] = self.code_digest
        self.review = {
            "schema": "copilot-native-review/v2",
            "review_attempt_id": self.attempt_id,
            "task_id": TASK,
            "work_item_id": "P1",
            "execution_result_sha256": "f" * 64,
            "contract_sha256": "e" * 64,
            "verdict": "pass",
            "findings": [],
            "unverified": [],
            "changed_files": [],
        }
        self.db = os.path.join(tmp, "db.sqlite")
        self.consume = os.path.join(tmp, "consume.marker")
        self.write()
        self.write_db()

    # ── 写入 ────────────────────────────────────────────────────────────
    def write(self) -> None:
        """只写 JSON 夹具；DB 由 write_db() 构建，避免覆盖已注入的行。"""
        for path, payload in ((os.path.join(self.tmp, "attempt.json"), self.attempt),
                              (os.path.join(self.tmp, "metadata.json"), self.meta)):
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
        with open(self.meta["outputFile"], "w", encoding="utf-8") as fh:
            fh.write(json.dumps(self.review, ensure_ascii=False))

    def write_db(self) -> None:
        if os.path.exists(self.db):
            os.remove(self.db)
        con = sqlite3.connect(self.db)
        cur = con.cursor()
        cur.execute("create table session (id text primary key, parent_id text,"
                    " task_type text, trace_id text)")
        cur.execute("create table tool_usage (tool_call_id text, session_id text,"
                    " tool_name text, read_only integer, side_effect_scope text,"
                    " status text, trace_id text, turn_id text, started_at integer)")
        cur.execute("create table part (id text, session_id text, data text,"
                    " sequence integer, message_id text)")
        cur.execute("create table session_entry (session_id text, type text,"
                    " data text, time_created integer)")
        con.commit()
        con.close()
        self.sql("insert into session values (?,?,?,?)",
                 (CHILD, PARENT, "subagent_child", TRACE))
        self._seq = 0
        self.add_call("call_1", "Read", {"file_path": "src/a.js"})
        self.add_call("call_2", "Read", {"file_path": self.evidence})
        self.add_call("call_3", "Grep", {"pattern": "LIMIT", "path": self.ws})
        self.sql("insert into session_entry values (?,?,?,?)",
                 (CHILD, "runtime/model_selection", json.dumps({
                     "modelSelection": {"providerId": "prov",
                                        "modelId": "model",
                                        "options": {"reasoningLevel": "max"}}
                 }, ensure_ascii=False), 1))

    # ── 行构造 ──────────────────────────────────────────────────────────
    def sql(self, statement: str, params: tuple = ()) -> None:
        con = sqlite3.connect(self.db)
        try:
            con.execute(statement, params)
            con.commit()
        finally:
            con.close()

    def add_call(self, call_id: str, tool: str, payload: dict, *,
                 name: str | None = None, read_only: int | None = 1,
                 scope: str | None = "none", status: str = "completed",
                 trace: str = TRACE, turn: str = TURN) -> None:
        """同时写入 tool_usage 与 part，保持同一 call id。"""
        self.sql("insert into tool_usage values (?,?,?,?,?,?,?,?,?)",
                 (call_id, CHILD, name or tool, read_only, scope, status,
                  trace, turn, self._seq))
        self._seq += 1
        self.add_part(call_id, tool, payload, status)

    def add_part(self, call_id: str, tool: str, payload: dict,
                 status: str = "completed") -> None:
        self._seq += 1
        self.sql("insert into part values (?,?,?,?,?)",
                 (f"{call_id}:part", CHILD,
                  json.dumps({"type": "tool", "tool": tool, "callID": call_id,
                              "state": {"status": status, "input": payload}},
                             ensure_ascii=False), self._seq,
                  f"msg_call_{call_id}"))

    def add_assistant_output(self, text: str, *, message_id: str = "msg_final",
                             finish: str = "stop") -> None:
        """写入持久化 assistant 消息与 text part（模拟宿主 message/part）。"""
        self.sql("create table if not exists message (id text primary key,"
                 " session_id text, sequence integer, time_created integer,"
                 " data text)")
        self.sql("insert into message values (?,?,?,?,?)",
                 (message_id, CHILD, 99, 1794000000000, json.dumps({
                     "role": "assistant", "finish": finish,
                     "providerId": "prov", "modelId": "model",
                 }, ensure_ascii=False)))
        self.sql("insert into part values (?,?,?,?,?)",
                 (f"{message_id}:text", CHILD, json.dumps({
                     "type": "text", "text": text}, ensure_ascii=False), 100,
                  message_id))

    def reset_parts(self, items: list[tuple[str, str, dict, str]]) -> None:
        self.sql("delete from part")
        for call_id, tool, payload, status in items:
            self.add_part(call_id, tool, payload, status)

    def set_meta(self, **over) -> None:
        self.meta.update(over)
        self.write()

    def set_profile(self, **over) -> None:
        self.meta["profileSnapshot"].update(over)
        self.write()

    def set_attempt(self, **over) -> None:
        self.attempt.update(over)
        self.write()

    def set_review(self, **over) -> None:
        self.review.update(over)
        self.write()

    def refresh_code_digest(self) -> None:
        """改动 code_snapshot 后按同一公式重算 digest/声明摘要。"""
        snapshot = self.attempt["code_snapshot"]
        snapshot["digest"] = A._snapshot_digest(snapshot)
        self.attempt["code_scope_sha256"] = snapshot["digest"]
        self.code_digest = snapshot["digest"]
        self.write()

    def run(self, **over):
        kwargs = dict(metadata=os.path.join(self.tmp, "metadata.json"),
                      attempt=os.path.join(self.tmp, "attempt.json"),
                      task_id=TASK, workspace=self.ws, db=self.db,
                      expected_target=self.attempt["target_sha256"],
                      task_dir=self.tmp,
                      executor_agent="agent_executor_fixture")
        kwargs.update(over)
        return A.attest(**kwargs)


class AttestTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="g4-attest-")
        self.f = Fixture(self.tmpdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def assertCode(self, record: dict, code: str) -> None:
        self.assertFalse(record["eligible"], record.get("residual"))
        self.assertEqual(record["code"], code,
                         [c for c in record["checks"] if not c["passed"]])


class HostOutputFallbackTests(AttestTestCase):
    def output_text(self) -> str:
        return open(self.f.meta["outputFile"], encoding="utf-8").read()

    def test_db_output_is_used_when_output_file_is_pruned(self):
        text = self.output_text()
        self.f.add_assistant_output(text)
        os.remove(self.f.meta["outputFile"])
        record = self.f.run()
        self.assertTrue(record["eligible"], record["rejections"])
        self.assertEqual(record["output_source"], "db.message.part")
        self.assertEqual(record["output_message_id"], "msg_final")
        self.assertEqual(record["output_sha256"], _sha_text(text))

    def test_output_file_and_db_must_match(self):
        self.f.add_assistant_output("different text")
        record = self.f.run()
        self.assertCode(record, "host_output_db_mismatch")

    def test_expected_output_sha_mismatch_is_rejected(self):
        text = self.output_text()
        self.f.add_assistant_output(text)
        record = self.f.run(expected_output_sha256="f" * 64)
        self.assertCode(record, "host_output_sha_mismatch")

    def test_output_file_with_matching_db_is_recorded(self):
        text = self.output_text()
        self.f.add_assistant_output(text)
        record = self.f.run()
        self.assertTrue(record["eligible"], record["rejections"])
        self.assertEqual(record["output_source"], "outputFile")
        self.assertEqual(record["output_db_sha256"], _sha_text(text))

    def test_missing_output_file_and_db_fails_closed(self):
        os.remove(self.f.meta["outputFile"])
        record = self.f.run()
        self.assertCode(record, "host_output_missing")


class HappyPathTests(AttestTestCase):
    def test_eligible(self) -> None:
        record = self.f.run()
        self.assertTrue(record["eligible"], record["rejections"])
        self.assertEqual(record["schema"], A.SCHEMA)
        self.assertEqual(record["tool_counts"], {"Grep": 1, "Read": 2})
        self.assertEqual(record["trace_id"], TRACE)
        self.assertEqual(record["turn_id"], TURN)
        self.assertEqual(record["model_selection"]["modelId"], "model")
        self.assertEqual(record["review_verdict"], "pass")
        self.assertEqual(record["review_scope"], "full")
        self.assertTrue(record["attempt_sha256"])

    def test_record_has_review_detail_and_profile(self) -> None:
        self.f.set_review(findings=[{"id": "F1", "severity": "Minor",
                                     "title": "文案"}])
        record = self.f.run()
        self.assertTrue(record["eligible"], record["rejections"])
        self.assertEqual(record["review_findings_detail"],
                         [{"id": "F1", "severity": "minor"}])
        self.assertEqual(record["profile"]["tools"],
                         ["Read", "Grep", "Glob"])
        self.assertEqual(record["profile"]["permissionMode"], "")
        self.assertTrue(record["profile"]["sha256"])

    def test_review_sha_matches_raw_output_bytes(self) -> None:
        with open(self.f.meta["outputFile"], "wb") as fh:
            fh.write(b"\r\n" + json.dumps(self.f.review).encode("utf-8")
                     + b"\r\n")
        record = self.f.run()
        self.assertTrue(record["eligible"], record["rejections"])
        self.assertEqual(record["review_sha256"],
                         _sha(self.f.meta["outputFile"]))

    def test_attestation_id_is_deterministic(self) -> None:
        first = self.f.run()
        second = self.f.run()
        self.assertEqual(first["attestation_id"], second["attestation_id"])
        self.f.set_meta(description="changed")
        self.assertNotEqual(first["attestation_id"],
                            self.f.run()["attestation_id"])

    def test_record_contains_evidence_and_code(self) -> None:
        record = self.f.run()
        self.assertEqual(record["evidence"][0]["path"],
                         os.path.realpath(self.f.evidence))
        self.assertEqual(record["evidence"][0]["sha256"], _sha(self.f.evidence))
        self.assertEqual(record["code_snapshot"]["digest"], self.f.code_digest)
        self.assertEqual(record["code_snapshot"]["files"][0]["path"],
                         "src/a.js")

    def test_relative_evidence_path(self) -> None:
        self.f.attempt["evidence_snapshot"][0]["path"] = "evidence/report.txt"
        self.f.write()
        self.assertTrue(self.f.run()["eligible"])

    def test_missing_expected_target_fails_closed(self) -> None:
        self.assertCode(self.f.run(expected_target=""), "target_unverified")

    def test_missing_expected_target_allowed_for_diagnostics(self) -> None:
        record = self.f.run(expected_target="", require_target=False)
        self.assertTrue(record["eligible"])
        self.assertTrue(any("expected_target" in item for item in record["residual"]))

    def test_rollout_absence_is_not_required(self) -> None:
        record = self.f.run()
        self.assertTrue(record["eligible"])
        self.assertTrue(any("rollout" in item for item in record["residual"]))

    def test_limits_are_recorded(self) -> None:
        record = self.f.run()
        self.assertEqual(len(record["limitations"]), 3)


class AttemptBindingTests(AttestTestCase):
    def test_task_mismatch(self) -> None:
        self.assertCode(self.f.run(task_id="other"), "attempt_task_mismatch")

    def test_invalid_attempt_id(self) -> None:
        self.f.set_attempt(review_attempt_id="bad id")
        self.assertCode(self.f.run(), "attempt_id_invalid")

    def test_consumed_attempt(self) -> None:
        self.f.set_attempt(consumed=True, consumed_at="2026-11-15T09:10:00+08:00")
        self.assertCode(self.f.run(), "attempt_consumed")

    def test_consume_marker_replay(self) -> None:
        first = self.f.run(consume_marker=self.f.consume)
        self.assertTrue(first["eligible"])
        A.write_consume_marker(self.f.consume, first["attestation_id"])
        self.assertCode(self.f.run(consume_marker=self.f.consume),
                        "attempt_replayed")

    def test_consume_marker_is_atomic(self) -> None:
        A.write_consume_marker(self.f.consume, "token")
        with self.assertRaises(FileExistsError):
            A.write_consume_marker(self.f.consume, "token2")

    def test_prompt_unbound(self) -> None:
        self.f.set_meta(prompt="没有绑定任何 attempt")
        self.assertCode(self.f.run(), "attempt_prompt_unbound")

    def test_prompt_substring_does_not_bind(self) -> None:
        self.f.set_meta(prompt=f"attempt {self.f.attempt_id}0 extra")
        self.assertCode(self.f.run(), "attempt_prompt_unbound")

    def test_session_precedes_attempt(self) -> None:
        self.f.set_attempt(created_at="2026-11-15T23:59:00+08:00")
        self.assertCode(self.f.run(), "attempt_session_precedes")

    def test_workspace_mismatch(self) -> None:
        self.assertCode(self.f.run(workspace="/tmp"), "attempt_workspace_mismatch")

    def test_target_stale(self) -> None:
        self.assertCode(self.f.run(expected_target="0" * 64), "target_stale")

    def test_invalid_attempt_time(self) -> None:
        self.f.set_attempt(created_at="not-a-time")
        self.assertCode(self.f.run(), "attempt_time_invalid")

    def test_incremental_requires_manifest(self) -> None:
        self.f.set_attempt(review_scope="incremental",
                           based_on_review_sha256="9" * 64)
        self.assertCode(self.f.run(), "review_scope_manifest_missing")

    def test_incremental_requires_baseline(self) -> None:
        self.f.set_attempt(review_scope="incremental",
                           evidence_manifest=["E1"])
        self.assertCode(self.f.run(), "review_scope_baseline_missing")

    def test_incremental_unknown_manifest_id(self) -> None:
        self.f.set_attempt(review_scope="incremental",
                           evidence_manifest=["E9"],
                           based_on_review_sha256="9" * 64)
        self.assertCode(self.f.run(), "review_manifest_unknown")

    def test_incremental_limits_required_evidence(self) -> None:
        second = os.path.join(self.f.ws, "evidence", "old.txt")
        with open(second, "w", encoding="utf-8") as fh:
            fh.write("old\n")
        self.f.attempt["evidence_snapshot"].append(
            {"id": "E2", "path": second, "sha256": _sha(second)})
        self.f.set_attempt(review_scope="incremental",
                           evidence_manifest=["E1"],
                           based_on_review_sha256="9" * 64)
        self.assertTrue(self.f.run()["eligible"])


class ProfileBoundaryTests(AttestTestCase):
    def test_tools_superset(self) -> None:
        self.f.set_profile(tools=["Read", "Grep", "Glob", "Bash"])
        self.assertCode(self.f.run(), "profile_tools_not_exact")

    def test_tools_empty(self) -> None:
        self.f.set_profile(tools=[])
        self.assertCode(self.f.run(), "profile_tools_empty")

    def test_tools_missing(self) -> None:
        self.f.set_profile(tools=None)
        self.assertCode(self.f.run(), "profile_tools_empty")

    def test_disallowed_missing(self) -> None:
        self.f.set_profile(disallowedTools=["Write"])
        self.assertCode(self.f.run(), "profile_disallowed_missing")

    def test_disallowed_wrong_type(self) -> None:
        self.f.set_profile(disallowedTools={"Write": True})
        self.assertCode(self.f.run(), "profile_disallowed_invalid")

    def test_permission_mode_bypass(self) -> None:
        self.f.set_profile(permissionMode="bypassPermissions")
        self.assertCode(self.f.run(), "profile_permission_mode")

    def test_permission_mode_default_ok(self) -> None:
        self.f.set_profile(permissionMode="default")
        self.assertTrue(self.f.run()["eligible"])

    def test_permission_mode_absent_is_inherit_for_project(self) -> None:
        self.assertTrue(self.f.run()["eligible"])

    def test_skills_rejected(self) -> None:
        self.f.set_profile(skills=["app-testing"])
        self.assertCode(self.f.run(), "profile_skills_not_allowed")

    def test_mcp_servers_rejected(self) -> None:
        self.f.set_profile(mcpServers=["playwright"])
        self.assertCode(self.f.run(), "profile_mcp_servers")

    def test_profile_source_user_rejected(self) -> None:
        self.f.set_profile(source="user")
        self.assertCode(self.f.run(), "profile_source_not_allowed")

    def test_profile_source_user_requires_permission_mode(self) -> None:
        with temp_home(self.f.tmp) as user_dir:
            path = os.path.join(user_dir, "readonly.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("read only\n")
            self.f.set_profile(source="user", path=path)
            record = self.f.run(allowed_profile_sources=("project", "user"))
            self.assertFalse(record["eligible"])
            code = record["code"]
            self.assertEqual(code, "profile_permission_mode_missing", record["checks"])

    def test_profile_source_user_allowed_when_opted_in(self) -> None:
        with temp_home(self.f.tmp) as user_dir:
            path = os.path.join(user_dir, "readonly.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("read only\n")
            self.f.set_profile(source="user", path=path,
                               permissionMode="default")
            record = self.f.run(allowed_profile_sources=("project", "user"))
            self.assertTrue(record["eligible"], record["rejections"])

    def test_profile_path_outside_workspace(self) -> None:
        outside = os.path.join(self.f.tmp, "outside-profile.md")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("read only\n")
        self.f.set_profile(path=outside)
        self.assertCode(self.f.run(), "profile_path_outside_workspace")

    def test_profile_content_mismatch(self) -> None:
        with open(self.f.profile, "w", encoding="utf-8") as fh:
            fh.write("---\nname: readonly-probe\n---\nother body\n")
        self.assertCode(self.f.run(), "profile_content_mismatch")

    def test_profile_path_missing(self) -> None:
        self.f.set_profile(path="")
        self.assertCode(self.f.run(), "profile_path_missing")

    def test_profile_path_not_file(self) -> None:
        self.f.set_profile(path=os.path.join(self.f.ws, "nope.md"))
        self.assertCode(self.f.run(), "profile_path_missing")

    def test_reviewer_not_completed(self) -> None:
        self.f.set_meta(status="running")
        self.assertCode(self.f.run(), "reviewer_not_completed")


class HostRecordTests(AttestTestCase):
    def test_executor_session_self_review(self) -> None:
        self.assertCode(self.f.run(executor_session=CHILD),
                        "reviewer_not_independent")

    def test_empty_turn_is_rejected(self) -> None:
        self.f.sql("update tool_usage set turn_id=''")
        self.assertCode(self.f.run(), "host_execution_split")

    def test_session_missing(self) -> None:
        self.f.sql("delete from session")
        self.assertCode(self.f.run(), "host_session_missing")

    def test_parent_mismatch(self) -> None:
        self.f.sql("update session set parent_id='sess_other'")
        self.assertCode(self.f.run(), "host_session_parent_mismatch")

    def test_session_kind_mismatch(self) -> None:
        self.f.sql("update session set task_type='normal'")
        self.assertCode(self.f.run(), "host_session_kind_mismatch")

    def test_tool_count_mismatch(self) -> None:
        self.f.set_meta(totalToolUseCount=4)
        self.assertCode(self.f.run(), "host_tool_count_mismatch")

    def test_tool_name_not_allowed(self) -> None:
        self.f.add_call("call_4", "Bash", {"command": "id"})
        self.f.set_meta(totalToolUseCount=4)
        self.assertCode(self.f.run(), "host_tool_not_allowed")

    def test_tool_not_readonly(self) -> None:
        self.f.sql("update tool_usage set read_only=0 where tool_name='Grep'")
        self.assertCode(self.f.run(), "host_tool_not_readonly")

    def test_tool_side_effect_scope(self) -> None:
        self.f.sql("update tool_usage set side_effect_scope='workspace' "
                   "where tool_name='Grep'")
        self.assertCode(self.f.run(), "host_tool_side_effect")

    def test_tool_status_running(self) -> None:
        self.f.sql("update tool_usage set status='running' "
                   "where tool_name='Grep'")
        self.assertCode(self.f.run(), "host_tool_status_invalid")

    def test_failed_tool_call_is_allowed(self) -> None:
        self.f.sql("update tool_usage set status='error', read_only=NULL, "
                   "side_effect_scope=NULL where tool_name='Grep'")
        self.f.sql("update part set data=replace(data, '\"completed\"', "
                   "'\"error\"') where id='call_3:part'")
        record = self.f.run()
        self.assertTrue(record["eligible"], record["rejections"])
        self.assertEqual(record["failed_tool_calls"], ["Grep"])

    def test_part_count_mismatch(self) -> None:
        self.f.sql("delete from part where id='call_3:part'")
        self.assertCode(self.f.run(), "host_part_count_mismatch")

    def test_part_tool_mismatch(self) -> None:
        self.f.sql("update part set data=replace(data, '\"tool\": \"Grep\"', "
                   "'\"tool\": \"Glob\"') where id='call_3:part'")
        self.assertCode(self.f.run(), "host_part_tool_mismatch")

    def test_part_call_id_mismatch(self) -> None:
        self.f.sql("update part set id='call_x:part' where id='call_3:part'")
        self.f.sql("update part set data=replace(data, 'call_3', 'call_x') "
                   "where id='call_x:part'")
        self.assertCode(self.f.run(), "host_part_call_mismatch")

    def test_call_status_mismatch(self) -> None:
        self.f.sql("update part set data=replace(data, '\"completed\"', "
                   "'\"error\"') where id='call_3:part'")
        self.assertCode(self.f.run(), "host_call_status_mismatch")

    def test_trace_split(self) -> None:
        self.f.add_call("call_4", "Read", {"file_path": "src/a.js"},
                        trace="trace-other")
        self.f.set_meta(totalToolUseCount=4)
        self.assertCode(self.f.run(), "host_execution_split")

    def test_trace_mismatch_with_session(self) -> None:
        self.f.sql("update session set trace_id='trace-other'")
        self.assertCode(self.f.run(), "host_trace_mismatch")

    def test_model_selection_missing(self) -> None:
        self.f.sql("delete from session_entry")
        self.assertCode(self.f.run(), "host_model_selection_missing")

    def test_model_selection_optional(self) -> None:
        self.f.sql("delete from session_entry")
        self.assertTrue(self.f.run(require_model_selection=False)["eligible"])

    def test_db_missing(self) -> None:
        os.remove(self.f.db)
        self.assertCode(self.f.run(), "host_db_unavailable")

    def test_db_pruned(self) -> None:
        self.f.sql("delete from part")
        self.f.sql("delete from tool_usage")
        self.f.sql("delete from session")
        self.assertCode(self.f.run(), "host_session_missing")

    def test_db_wrong_schema(self) -> None:
        empty = os.path.join(self.f.tmp, "empty.sqlite")
        sqlite3.connect(empty).close()
        self.assertCode(self.f.run(db=empty), "host_db_query_failed")

    def test_executor_identity_required(self) -> None:
        self.assertCode(self.f.run(executor_agent=""), "executor_identity_missing")

    def test_executor_identity_optional_for_diagnostics(self) -> None:
        self.assertTrue(self.f.run(executor_agent="",
                                   require_executor_identity=False)["eligible"])


    def test_call_tool_name_swap(self) -> None:
        for table in ("tool_usage", "part"):
            self.f.sql(f"delete from {table}")
        self.f._seq = 0
        self.f.add_call("call_1", "Grep", {"pattern": "LIMIT", "path": self.f.ws},
                        name="Read")
        self.f.add_call("call_2", "Read", {"file_path": self.f.evidence})
        self.f.add_call("call_3", "Read", {"file_path": "src/a.js"}, name="Grep")
        self.assertCode(self.f.run(), "host_call_tool_mismatch")


class EvidenceBoundaryTests(AttestTestCase):
    def test_evidence_entry_without_path_rejected(self) -> None:
        self.f.attempt["evidence_snapshot"].append(
            {"id": "E2", "path": "", "sha256": "a" * 64})
        self.f.write()
        self.assertCode(self.f.run(), "evidence_entry_invalid")

    def test_evidence_entry_without_id_rejected(self) -> None:
        self.f.attempt["evidence_snapshot"][0]["id"] = ""
        self.f.write()
        self.assertCode(self.f.run(), "evidence_entry_invalid")

    def test_evidence_entry_without_sha_rejected(self) -> None:
        self.f.attempt["evidence_snapshot"][0]["sha256"] = ""
        self.f.write()
        self.assertCode(self.f.run(), "evidence_snapshot_missing_sha")

    def test_evidence_not_inspected(self) -> None:
        extra = os.path.join(self.f.ws, "src", "b.js")
        with open(extra, "w", encoding="utf-8") as fh:
            fh.write("// not read\n")
        self.assertCode(self.f.run(evidence=[extra]), "evidence_not_inspected")

    def test_parent_dir_only_does_not_count(self) -> None:
        extra = os.path.join(self.f.ws, "src", "b.js")
        with open(extra, "w", encoding="utf-8") as fh:
            fh.write("// not read\n")
        self.f.reset_parts([
            ("call_1", "Read", {"file_path": "src/a.js"}, "completed"),
            ("call_2", "Read", {"file_path": self.f.evidence}, "completed"),
            ("call_3", "Grep", {"pattern": "LIMIT",
                                "path": os.path.join(self.f.ws, "src")},
             "completed"),
        ])
        self.assertCode(self.f.run(evidence=[extra]),
                        "evidence_not_read_exact_file")

    def test_partial_read_does_not_count(self) -> None:
        self.f.reset_parts([
            ("call_1", "Read", {"file_path": "src/a.js"}, "completed"),
            ("call_2", "Read", {"file_path": self.f.evidence,
                                "offset": 0, "limit": 1}, "completed"),
            ("call_3", "Grep", {"pattern": "LIMIT", "path": self.f.ws},
             "completed"),
        ])
        self.assertCode(self.f.run(), "evidence_partial_read")

    def test_evidence_drift(self) -> None:
        with open(self.f.evidence, "a", encoding="utf-8") as fh:
            fh.write("tampered\n")
        self.assertCode(self.f.run(), "evidence_drift")

    def test_evidence_missing(self) -> None:
        os.remove(self.f.evidence)
        self.assertCode(self.f.run(), "evidence_missing")

    def test_evidence_snapshot_missing_sha(self) -> None:
        self.f.attempt["evidence_snapshot"][0].pop("sha256")
        self.f.write()
        self.assertCode(self.f.run(), "evidence_snapshot_missing_sha")

    def test_evidence_outside_roots(self) -> None:
        outside = "/etc/hostname"
        self.f.attempt["evidence_snapshot"].append(
            {"id": "E2", "path": outside, "sha256": _sha(outside)})
        self.f.write()
        self.assertCode(self.f.run(), "evidence_outside_roots")

    def test_evidence_symlink_escape(self) -> None:
        link = os.path.join(self.f.ws, "evidence", "link.txt")
        os.symlink("/etc/hostname", link)
        self.f.attempt["evidence_snapshot"].append(
            {"id": "E2", "path": link, "sha256": _sha("/etc/hostname")})
        self.f.write()
        self.assertCode(self.f.run(), "evidence_symlink_escape")

    def test_sensitive_evidence(self) -> None:
        secret = os.path.join(self.f.ws, "evidence", ".env")
        with open(secret, "w", encoding="utf-8") as fh:
            fh.write("TOKEN=fixture\n")
        self.f.attempt["evidence_snapshot"].append(
            {"id": "E2", "path": secret, "sha256": _sha(secret)})
        self.f.write()
        self.assertCode(self.f.run(), "evidence_sensitive_path")

    def test_outside_workspace_read_rejected(self) -> None:
        self.f.reset_parts([
            ("call_1", "Read", {"file_path": "/etc/hostname"}, "completed"),
            ("call_2", "Read", {"file_path": self.f.evidence}, "completed"),
            ("call_3", "Grep", {"pattern": "LIMIT", "path": self.f.ws},
             "completed"),
        ])
        self.assertCode(self.f.run(), "read_outside_workspace")

    def test_outside_workspace_read_recorded(self) -> None:
        self.f.reset_parts([
            ("call_1", "Read", {"file_path": "/etc/hostname"}, "completed"),
            ("call_2", "Read", {"file_path": self.f.evidence}, "completed"),
            ("call_3", "Grep", {"pattern": "LIMIT", "path": self.f.ws},
             "completed"),
        ])
        record = self.f.run(outside_reads="record")
        self.assertTrue(record["eligible"])
        self.assertIn("/etc/hostname", record["outside_reads"])

    def test_symlink_escape_read(self) -> None:
        link = os.path.join(self.f.ws, "link-outside")
        os.symlink("/etc/hostname", link)
        self.f.reset_parts([
            ("call_1", "Read", {"file_path": link}, "completed"),
            ("call_2", "Read", {"file_path": self.f.evidence}, "completed"),
            ("call_3", "Grep", {"pattern": "LIMIT", "path": self.f.ws},
             "completed"),
        ])
        self.assertCode(self.f.run(), "read_symlink_escape")

    def test_path_traversal(self) -> None:
        self.f.reset_parts([
            ("call_1", "Read",
             {"file_path": "../../../../../../etc/hostname"}, "completed"),
            ("call_2", "Read", {"file_path": self.f.evidence}, "completed"),
            ("call_3", "Grep", {"pattern": "LIMIT", "path": self.f.ws},
             "completed"),
        ])
        self.assertCode(self.f.run(), "read_outside_workspace")

    def test_sensitive_path_read(self) -> None:
        secret = os.path.join(self.f.ws, ".env")
        with open(secret, "w", encoding="utf-8") as fh:
            fh.write("TOKEN=fixture\n")
        self.f.reset_parts([
            ("call_1", "Read", {"file_path": secret}, "completed"),
            ("call_2", "Read", {"file_path": self.f.evidence}, "completed"),
            ("call_3", "Grep", {"pattern": "LIMIT", "path": self.f.ws},
             "completed"),
        ])
        self.assertCode(self.f.run(), "read_sensitive_path")

    def test_sensitive_grep_target(self) -> None:
        secret_dir = os.path.join(self.f.ws, ".secrets")
        os.makedirs(secret_dir, exist_ok=True)
        self.f.reset_parts([
            ("call_1", "Read", {"file_path": "src/a.js"}, "completed"),
            ("call_2", "Read", {"file_path": self.f.evidence}, "completed"),
            ("call_3", "Grep", {"pattern": "x", "path": secret_dir},
             "completed"),
        ])
        self.assertCode(self.f.run(), "read_sensitive_path")

    def test_task_dir_read_allowed(self) -> None:
        extra_dir = os.path.join(self.f.tmp, "artifacts")
        os.makedirs(extra_dir, exist_ok=True)
        extra = os.path.join(extra_dir, "log.txt")
        with open(extra, "w", encoding="utf-8") as fh:
            fh.write("log\n")
        self.f.add_call("call_4", "Read", {"file_path": extra})
        self.f.set_meta(totalToolUseCount=4)
        self.assertTrue(self.f.run(evidence=[extra])["eligible"])

    def test_grep_without_path_defaults_to_workspace(self) -> None:
        self.f.reset_parts([
            ("call_1", "Read", {"file_path": "src/a.js"}, "completed"),
            ("call_2", "Read", {"file_path": self.f.evidence}, "completed"),
            ("call_3", "Grep", {"pattern": "LIMIT"}, "completed"),
        ])
        self.assertTrue(self.f.run()["eligible"])


    def test_partial_read_of_evidence_rejected(self) -> None:
        self.f.reset_parts([
            ("call_1", "Read", {"file_path": "src/a.js"}, "completed"),
            ("call_2", "Read", {"file_path": self.f.evidence,
                                "offset": 1, "limit": 5}, "completed"),
            ("call_3", "Grep", {"pattern": "LIMIT", "path": self.f.ws},
             "completed"),
        ])
        self.assertCode(self.f.run(), "evidence_partial_read")

    def test_partial_read_outside_workspace_rejected(self) -> None:
        self.f.reset_parts([
            ("call_1", "Read", {"file_path": "/etc/hostname",
                                "offset": 1, "limit": 5}, "completed"),
            ("call_2", "Read", {"file_path": self.f.evidence}, "completed"),
            ("call_3", "Grep", {"pattern": "LIMIT", "path": self.f.ws},
             "completed"),
        ])
        self.assertCode(self.f.run(), "read_outside_workspace")

    def test_partial_read_sensitive_path_rejected(self) -> None:
        secret = os.path.join(self.f.ws, ".env")
        with open(secret, "w", encoding="utf-8") as fh:
            fh.write("TOKEN=fixture\n")
        self.f.reset_parts([
            ("call_1", "Read", {"file_path": secret, "offset": 1, "limit": 5},
             "completed"),
            ("call_2", "Read", {"file_path": self.f.evidence}, "completed"),
            ("call_3", "Grep", {"pattern": "LIMIT", "path": self.f.ws},
             "completed"),
        ])
        self.assertCode(self.f.run(), "read_sensitive_path")


class CodeScopeTests(AttestTestCase):
    def test_code_drift(self) -> None:
        with open(self.f.source, "a", encoding="utf-8") as fh:
            fh.write("// drift\n")
        self.assertCode(self.f.run(), "code_scope_drift")

    def test_code_snapshot_unavailable(self) -> None:
        self.f.attempt["code_snapshot"]["available"] = False
        self.f.attempt["code_snapshot"]["reason"] = "not_git_repository"
        self.f.write()
        self.assertCode(self.f.run(), "code_snapshot_unavailable")

    def test_code_snapshot_workspace_mismatch(self) -> None:
        self.f.attempt["code_snapshot"]["workspace"] = "/tmp/other-workspace"
        self.f.write()
        self.assertCode(self.f.run(), "code_snapshot_workspace_mismatch")

    def test_code_scope_empty(self) -> None:
        self.f.attempt["code_snapshot"]["files"] = []
        self.f.write()
        self.assertCode(self.f.run(), "code_scope_empty")

    def test_code_scope_declared_digest_missing(self) -> None:
        self.f.attempt["code_scope_sha256"] = ""
        self.f.write()
        self.assertCode(self.f.run(), "code_scope_digest_missing")

    def test_code_path_absolute_rejected(self) -> None:
        self.f.attempt["code_snapshot"]["files"][0]["path"] = "/etc/hostname"
        self.f.write()
        self.assertCode(self.f.run(), "code_scope_path_invalid")

    def test_code_kind_missing_rejected(self) -> None:
        self.f.attempt["code_snapshot"]["files"][0]["kind"] = ""
        self.f.write()
        self.assertCode(self.f.run(), "code_scope_file_invalid")

    def test_task_dir_outside_workspace_rejected(self) -> None:
        self.assertCode(self.f.run(task_dir="/"), "task_dir_outside_workspace")

    def test_code_scope_digest_missing(self) -> None:
        self.f.attempt["code_snapshot"]["digest"] = ""
        self.f.write()
        self.assertCode(self.f.run(), "code_scope_digest_missing")

    def test_code_snapshot_digest_recompute_mismatch(self) -> None:
        self.f.attempt["code_snapshot"]["digest"] = "1" * 64
        self.f.attempt["code_scope_sha256"] = "1" * 64
        self.f.write()
        self.assertCode(self.f.run(), "code_snapshot_digest_mismatch")

    def test_code_scope_digest_mismatch(self) -> None:
        self.f.attempt["code_scope_sha256"] = "1" * 64
        self.f.write()
        self.assertCode(self.f.run(), "code_scope_digest_mismatch")

    def test_file_kind_with_symlink_rejected(self) -> None:
        link = os.path.join(self.f.ws, "src", "link.js")
        os.symlink("a.js", link)
        self.f.attempt["code_snapshot"]["files"][0].update(
            {"path": "src/link.js", "kind": "file",
             "sha256": _sha(self.f.source)})
        self.f.write()
        self.assertCode(self.f.run(), "code_scope_file_invalid")

    def test_code_scope_path_traversal(self) -> None:
        self.f.attempt["code_snapshot"]["files"][0]["path"] = "../outside.js"
        self.f.write()
        self.assertCode(self.f.run(), "code_scope_path_invalid")

    def test_submodule_worktree_outside_root_rejected(self) -> None:
        self.f.attempt["code_snapshot"]["submodule_worktrees"] = [{
            "path": "../outside", "head": "a" * 40, "dirty": True,
            "files": [], "nested": [], "error": ""}]
        self.f.refresh_code_digest()
        record = self.f.run()
        self.assertCode(record, "code_scope_submodule_state_invalid")

    def test_code_scope_kind_other_non_submodule_rejected(self) -> None:
        self.f.attempt["code_snapshot"]["files"][0].update(
            {"kind": "other", "sha256": ""})
        self.f.write()
        self.assertCode(self.f.run(), "code_scope_file_invalid")

    def test_code_scope_submodule_gitlink_is_residual(self) -> None:
        sub = os.path.join(self.f.ws, "sub")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, ".git"), "w", encoding="utf-8") as fh:
            fh.write("gitdir: ../.git/modules/sub\n")
        self.f.attempt["code_snapshot"]["files"].append(
            {"path": "sub", "kind": "other", "sha256": "", "status": " M"})
        self.f.attempt["code_snapshot"]["submodules"] = [
            " " + "a" * 40 + " sub (heads/main)"]
        self.f.refresh_code_digest()
        record = self.f.run()
        self.assertTrue(record["eligible"], record["rejections"])
        self.assertTrue(any("submodule" in item for item in record["residual"]))

    def test_code_scope_sha_missing(self) -> None:
        self.f.attempt["code_snapshot"]["files"][0]["sha256"] = ""
        self.f.write()
        self.assertCode(self.f.run(), "code_scope_file_invalid")

    def test_missing_code_file(self) -> None:
        os.remove(self.f.source)
        self.assertCode(self.f.run(), "code_scope_drift")

    def test_deleted_file_snapshot_is_stable(self) -> None:
        os.remove(self.f.source)
        self.f.attempt["code_snapshot"]["files"][0].update(
            {"kind": "missing", "sha256": "", "status": " D"})
        self.f.refresh_code_digest()
        self.assertTrue(self.f.run()["eligible"])


    def test_git_root_filesystem_root_rejected(self) -> None:
        self.f.attempt["code_snapshot"]["git_root"] = "/"
        self.f.write()
        self.assertCode(self.f.run(), "code_snapshot_root_outside")

    def test_ancestor_git_root_without_git_dir_rejected(self) -> None:
        parent = os.path.dirname(self.f.ws)
        self.f.attempt["code_snapshot"]["git_root"] = parent
        self.f.write()
        self.assertCode(self.f.run(), "code_snapshot_root_outside")

    def test_symlink_code_file_uses_link_text(self) -> None:
        link = os.path.join(self.f.ws, "src", "alias.js")
        os.symlink("a.js", link)
        self.f.attempt["code_snapshot"]["files"].append({
            "path": "src/alias.js", "kind": "symlink", "status": "??",
            "sha256": _sha_text("a.js")})
        self.f.refresh_code_digest()
        record = self.f.run()
        self.assertTrue(record["eligible"], record["checks"])


class OutputTests(AttestTestCase):
    def test_output_missing(self) -> None:
        os.remove(self.f.meta["outputFile"])
        self.assertCode(self.f.run(), "host_output_missing")

    def test_output_outside_agent_dir(self) -> None:
        other = os.path.join(self.f.tmp, "elsewhere")
        os.makedirs(other, exist_ok=True)
        path = os.path.join(other, "output.txt")
        shutil.copy(self.f.meta["outputFile"], path)
        self.f.set_meta(outputFile=path)
        self.assertCode(self.f.run(), "host_output_outside_agent_dir")

    def test_output_unparseable(self) -> None:
        with open(self.f.meta["outputFile"], "w", encoding="utf-8") as fh:
            fh.write("no json here")
        self.assertCode(self.f.run(), "review_output_unparseable")

    def test_prose_with_braces_is_not_parsed(self) -> None:
        with open(self.f.meta["outputFile"], "w", encoding="utf-8") as fh:
            fh.write("结论 {\"verdict\": \"pass\"} 仅供参考")
        self.assertCode(self.f.run(), "review_output_unparseable")

    def test_multiple_json_objects_are_rejected(self) -> None:
        with open(self.f.meta["outputFile"], "w", encoding="utf-8") as fh:
            fh.write("```json\n" + json.dumps(self.f.review)
                     + "\n```\n```json\n" + json.dumps(self.f.review)
                     + "\n```\n")
        self.assertCode(self.f.run(), "review_output_unparseable")

    def test_fenced_json_is_accepted(self) -> None:
        with open(self.f.meta["outputFile"], "w", encoding="utf-8") as fh:
            fh.write("\n```json\n"
                     + json.dumps({**self.f.review, "verdict": "PASS"})
                     + "\n```\n\n")
        self.assertTrue(self.f.run()["eligible"])

    def test_fenced_json_with_prose_is_rejected(self) -> None:
        with open(self.f.meta["outputFile"], "w", encoding="utf-8") as fh:
            fh.write("结论如下：\n```json\n" + json.dumps(self.f.review)
                     + "\n```\n以上仅供参考。\n")
        self.assertCode(self.f.run(), "review_output_unparseable")

    def test_attempt_mismatch(self) -> None:
        self.f.set_review(review_attempt_id="RAold")
        self.assertCode(self.f.run(), "review_attempt_mismatch")

    def test_verdict_not_pass(self) -> None:
        self.f.set_review(verdict="partial")
        self.assertCode(self.f.run(), "review_verdict_not_pass")

    def test_findings_wrong_type(self) -> None:
        self.f.set_review(findings={"id": "F1", "severity": "blocking"})
        self.assertCode(self.f.run(), "review_findings_invalid")

    def test_blocking_finding(self) -> None:
        self.f.set_review(findings=[{"id": "F1", "severity": "blocking"}])
        self.assertCode(self.f.run(), "review_blocking_findings")

    def test_unverified_wrong_type(self) -> None:
        self.f.set_review(unverified="未核对核心逻辑")
        self.assertCode(self.f.run(), "review_unverified_invalid")

    def test_changed_files(self) -> None:
        self.f.set_review(changed_files=["src/a.js"])
        self.assertCode(self.f.run(), "review_changed_files")

    def test_unknown_schema(self) -> None:
        self.f.set_review(schema="copilot-native-review/v0")
        self.assertCode(self.f.run(), "review_schema_unknown")

    def test_unverified_relative_code_path_blocks(self) -> None:
        self.f.set_review(unverified=["src/a.js 未核对"])
        self.assertCode(self.f.run(), "review_unverified_evidence")

    def test_unverified_basename_blocks(self) -> None:
        self.f.set_review(unverified=["a.js 的边界行为未核对"])
        self.assertCode(self.f.run(), "review_unverified_evidence")

    def test_unverified_file_line_blocks(self) -> None:
        self.f.set_review(unverified=["a.js:12 未核对超时语义"])
        self.assertCode(self.f.run(), "review_unverified_evidence")

    def test_unverified_naming_evidence_blocks(self) -> None:
        self.f.set_review(unverified=[f"{self.f.evidence} 未核对"])
        self.assertCode(self.f.run(), "review_unverified_evidence")

    def test_cjk_adjacent_code_name_blocks(self) -> None:
        self.f.set_review(unverified=["未核对a.js"])
        self.assertCode(self.f.run(), "review_unverified_evidence")

    def test_cjk_adjacent_evidence_id_blocks(self) -> None:
        self.f.set_review(unverified=["证据E1未核对"])
        self.assertCode(self.f.run(), "review_unverified_evidence")

    def test_cjk_adjacent_work_item_blocks(self) -> None:
        self.f.set_review(unverified=["工作项P1未核对"])
        self.assertCode(self.f.run(), "review_unverified_evidence")

    def test_unverified_environment_is_residual(self) -> None:
        self.f.set_review(unverified=["工作区非 git，无法核对 changed_files"])
        record = self.f.run()
        self.assertTrue(record["eligible"])
        self.assertTrue(record["residual"])

    def test_missing_schema_rejected(self) -> None:
        self.f.review.pop("schema")
        self.f.write()
        self.assertCode(self.f.run(), "review_schema_missing")

    def test_empty_review_object_rejected(self) -> None:
        self.f.review = {}
        self.f.write()
        self.assertCode(self.f.run(), "review_schema_missing")

    def test_duplicate_json_keys_rejected(self) -> None:
        body = json.dumps(self.f.review)
        with open(self.f.meta["outputFile"], "w", encoding="utf-8") as fh:
            fh.write('{"schema": "copilot-native-review/v2", '
                     + body.lstrip("{").rstrip("}") + "}")
        self.assertCode(self.f.run(), "review_output_unparseable")

    def test_severity_case_is_normalized(self) -> None:
        self.f.set_review(findings=[{"id": "F1", "severity": "Blocking"}])
        self.assertCode(self.f.run(), "review_blocking_findings")

    def test_unknown_severity_rejected(self) -> None:
        self.f.set_review(findings=[{"id": "F1", "severity": "critical"}])
        self.assertCode(self.f.run(), "review_finding_severity_invalid")

    def test_unverified_evidence_id_blocks(self) -> None:
        self.f.set_review(unverified=["E1"])
        self.assertCode(self.f.run(), "review_unverified_evidence")

    def test_review_binding_missing_is_rejected(self) -> None:
        self.f.review.pop("execution_result_sha256")
        self.f.write()
        self.assertCode(self.f.run(), "review_binding_missing")

    def test_review_plan_binding_required(self) -> None:
        self.f.set_attempt(plan_id="PL1")
        self.assertCode(self.f.run(), "review_binding_missing")

    def test_unverified_trailing_period_blocks(self) -> None:
        self.f.set_review(unverified=["a.js. 未核对"])
        self.assertCode(self.f.run(), "review_unverified_evidence")

    def test_record_mode_marks_diagnostic(self) -> None:
        self.f.reset_parts([
            ("call_1", "Read", {"file_path": "/etc/hostname"}, "completed"),
            ("call_2", "Read", {"file_path": self.f.evidence}, "completed"),
            ("call_3", "Grep", {"pattern": "LIMIT", "path": self.f.ws},
             "completed"),
        ])
        record = self.f.run(outside_reads="record")
        self.assertTrue(record["eligible"])
        self.assertTrue(record["policy"]["diagnostic"])

    def test_contract_binding_is_optional(self) -> None:
        self.f.review.pop("contract_sha256")
        self.f.write()
        self.assertTrue(self.f.run()["eligible"])

    def test_prefixed_or_suffixed_name_is_not_a_hit(self) -> None:
        self.f.set_review(unverified=["b.a.js 与 a.js.bak 均未核对"])
        record = self.f.run()
        self.assertTrue(record["eligible"], record["rejections"])

    def test_skip_target_marks_diagnostic(self) -> None:
        record = self.f.run(expected_target="", require_target=False)
        self.assertTrue(record["eligible"])
        self.assertTrue(record["policy"]["diagnostic"])

    def test_review_binding_mismatch(self) -> None:
        self.f.set_review(work_item_id="P9")
        self.assertCode(self.f.run(), "review_binding_mismatch")


class ParserRobustnessTests(AttestTestCase):
    def test_recursion_error_fails_closed(self) -> None:
        path = os.path.join(self.f.tmp, "deep.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{}")
        with mock.patch.object(A, "_parse_json", side_effect=RecursionError("deep")):
            with self.assertRaises(A.AttestError):
                A._load_json_with_sha(path)

class CliTests(AttestTestCase):
    def _main(self, *extra: str) -> int:
        argv = ["--metadata", os.path.join(self.f.tmp, "metadata.json"),
                "--attempt", os.path.join(self.f.tmp, "attempt.json"),
                "--task-id", TASK, "--workspace", self.f.ws,
                "--db", self.f.db,
                "--expected-target", self.f.attempt["target_sha256"],
                "--executor-agent", "agent_executor_fixture",
                *extra]
        return A.main(argv)

    def test_cli_eligible_writes_output(self) -> None:
        out = os.path.join(self.f.tmp, "attestation.json")
        self.assertEqual(self._main("--output", out), 0)
        with open(out, encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertTrue(record["eligible"])
        self.assertEqual(record["schema"], A.SCHEMA)

    def test_cli_rejected_returns_one(self) -> None:
        self.f.set_meta(status="failed")
        self.assertEqual(self._main(), 1)

    def test_cli_bad_metadata_returns_one(self) -> None:
        self.assertEqual(A.main(["--metadata", os.path.join(self.f.tmp, "nope.json"),
                                 "--attempt", os.path.join(self.f.tmp, "attempt.json"),
                                 "--task-id", TASK, "--workspace", self.f.ws,
                                 "--db", self.f.db]), 1)

    def test_cli_allow_user_profile_flag(self) -> None:
        with temp_home(self.f.tmp) as user_dir:
            path = os.path.join(user_dir, "readonly.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("read only\n")
            self.f.set_profile(source="user", path=path,
                               permissionMode="default")
            self.assertEqual(self._main("--allow-user-profile"), 0)

    def test_cli_output_conflicts_with_input(self) -> None:
        self.assertEqual(self._main("--output",
                                    os.path.join(self.f.tmp, "attempt.json")), 1)

    def test_cli_consume_marker_then_replay(self) -> None:
        out = os.path.join(self.f.tmp, "attestation.json")
        self.assertEqual(self._main("--consume-marker", self.f.consume,
                                    "--output", out), 0)
        self.assertTrue(os.path.exists(self.f.consume))
        self.assertEqual(self._main("--consume-marker", self.f.consume,
                                    "--output", out), 1)

    def test_cli_output_failure_removes_marker(self) -> None:
        code = self._main("--consume-marker", self.f.consume,
                          "--output", os.path.join(self.f.tmp, "no-dir", "out.json"))
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.f.consume))


    def test_cli_marker_requires_output(self) -> None:
        self.assertEqual(self._main("--consume-marker", self.f.consume), 1)
        self.assertFalse(os.path.exists(self.f.consume))

    def test_cli_output_conflicts_with_evidence(self) -> None:
        before = open(self.f.evidence, encoding="utf-8").read()
        self.assertEqual(self._main("--output", self.f.evidence), 1)
        self.assertEqual(open(self.f.evidence, encoding="utf-8").read(), before)

    def test_cli_output_conflicts_with_code_file(self) -> None:
        before = open(self.f.source, encoding="utf-8").read()
        self.assertEqual(self._main("--output", self.f.source), 1)
        self.assertEqual(open(self.f.source, encoding="utf-8").read(), before)

    def test_cli_output_conflicts_with_review_output(self) -> None:
        before = open(self.f.meta["outputFile"], encoding="utf-8").read()
        self.assertEqual(self._main("--output", self.f.meta["outputFile"]), 1)
        self.assertEqual(open(self.f.meta["outputFile"], encoding="utf-8").read(),
                         before)


class StoredVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "raw.txt")
        self.raw = ('{"schema":"copilot-native-review/v2","verdict":"pass",'
                    '"findings":[]}').encode()
        with open(self.path, "wb") as fh:
            fh.write(self.raw)
        self.sha = __import__("hashlib").sha256(self.raw).hexdigest()
        self.record = {"attestation": {
            "raw_output": {"path": self.path, "sha256": self.sha},
            "review_sha256": self.sha, "verdict": "pass",
            "attempt_id": "RA" + "a" * 24, "target_sha256": "b" * 64}}

    def test_stored_output_reverifies_without_host(self):
        result = A.verify_stored(self.record)
        self.assertTrue(result["ok"], result["checks"])
        self.assertEqual(result["raw_output_sha256"], self.sha)
        self.assertEqual(result["review_verdict"], "pass")

    def test_tampered_stored_output_is_rejected(self):
        with open(self.path, "ab") as fh:
            fh.write(b"\n")
        result = A.verify_stored(self.record)
        self.assertFalse(result["ok"])
        self.assertFalse([c for c in result["checks"]
                          if c["code"] == "stored_output_sha"][0]["passed"])

    def _set_raw(self, payload: dict) -> str:
        raw = json.dumps(payload).encode()
        with open(self.path, "wb") as fh:
            fh.write(raw)
        sha = __import__("hashlib").sha256(raw).hexdigest()
        self.record["attestation"]["raw_output"]["sha256"] = sha
        self.record["attestation"]["review_sha256"] = sha
        return sha

    def test_stored_findings_match_is_verified(self):
        self.record["attestation"]["review_findings_detail"] = [
            {"id": "F1", "severity": "minor"}]
        self._set_raw({"schema": "copilot-native-review/v2",
                       "verdict": "pass",
                       "findings": [{"id": "F1", "severity": "minor",
                                     "summary": "x"}]})
        result = A.verify_stored(self.record)
        self.assertTrue(result["ok"], result["checks"])

    def test_tampered_stored_findings_are_rejected(self):
        self.record["attestation"]["review_findings_detail"] = [
            {"id": "F1", "severity": "minor"}]
        self._set_raw({"schema": "copilot-native-review/v2",
                       "verdict": "pass",
                       "findings": [{"id": "F1", "severity": "major",
                                     "summary": "x"}]})
        result = A.verify_stored(self.record)
        self.assertFalse(result["ok"])
        self.assertFalse([c for c in result["checks"]
                          if c["code"] == "stored_findings_match"][0]["passed"])

    def test_legacy_record_without_findings_detail_is_limited(self):
        self._set_raw({"schema": "copilot-native-review/v2",
                       "verdict": "pass",
                       "findings": [{"id": "F1", "severity": "info",
                                     "summary": "x"}]})
        result = A.verify_stored(self.record)
        self.assertTrue(result["ok"], result["checks"])
        self.assertTrue(any("没有 findings 摘要" in item
                            for item in result["limitations"]))

    def test_missing_stored_output_is_rejected(self):
        os.remove(self.path)
        result = A.verify_stored(self.record)
        self.assertFalse(result["ok"])

if __name__ == "__main__":
    unittest.main()

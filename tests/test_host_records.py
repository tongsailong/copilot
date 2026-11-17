#!/usr/bin/env python3
"""宿主持久记录只读访问：assistant 输出重建、session、模型选择。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import host_records as H  # noqa: E402

SESSION = "sess_child"


class HostRecordsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "db.sqlite")
        con = sqlite3.connect(self.db)
        con.execute("create table session (id text, parent_id text,"
                    " task_type text, trace_id text, directory text)")
        con.execute("create table session_entry (session_id text, type text,"
                    " data text, time_created integer)")
        con.execute("create table message (id text, session_id text,"
                    " sequence integer, time_created integer, data text)")
        con.execute("create table part (id text, message_id text, session_id"
                    " text, data text, sequence integer)")
        con.execute("insert into session values (?,?,?,?,?)",
                    (SESSION, "sess_parent", "subagent_child", "trace-1",
                     "/tmp/ws"))
        con.commit()
        con.close()

    def _sql(self, statement, params=()):
        con = sqlite3.connect(self.db)
        con.execute(statement, params)
        con.commit()
        con.close()

    def _message(self, mid, role, sequence, text=None, finish="stop"):
        self._sql("insert into message values (?,?,?,?,?)",
                  (mid, SESSION, sequence, 1000 + sequence, json.dumps({
                      "role": role, "finish": finish,
                      "providerId": "p", "modelId": "m"})))
        if text is not None:
            self._sql("insert into part values (?,?,?,?,?)",
                      (f"{mid}-p", mid, SESSION, json.dumps({
                          "type": "text", "text": text}), 0))

    def test_last_non_empty_assistant_output(self):
        self._message("m1", "assistant", 1, "first")
        self._message("m2", "assistant", 2, "second")
        self._message("m3", "assistant", 3, None)
        out = H.assistant_output(self.db, SESSION)
        self.assertEqual(out["text"], "second")
        self.assertEqual(out["sha256"],
                         hashlib.sha256(b"second").hexdigest())
        self.assertEqual(out["message_id"], "m2")
        self.assertEqual(out["source"], "db.message.part")
        self.assertEqual(out["model"]["model_id"], "m")

    def test_user_messages_are_ignored(self):
        self._message("u1", "user", 1, "hello")
        self.assertIsNone(H.assistant_output(self.db, SESSION))

    def test_missing_message_table_returns_none(self):
        os.remove(self.db)
        con = sqlite3.connect(self.db)
        con.execute("create table part (id text, message_id text, session_id"
                    " text, data text, sequence integer)")
        con.commit()
        con.close()
        self.assertIsNone(H.assistant_output(self.db, SESSION))

    def test_session_row(self):
        row = H.session_row(self.db, SESSION)
        self.assertEqual(row["parent_id"], "sess_parent")
        self.assertEqual(row["task_type"], "subagent_child")
        self.assertIsNone(H.session_row(self.db, "sess-missing"))

    def test_model_selection(self):
        self._sql("insert into session_entry values (?,?,?,?)",
                  (SESSION, "runtime/model_selection", json.dumps({
                      "modelSelection": {"providerId": "prov",
                                         "modelId": "model-x"}}), 1))
        selection = H.model_selection(self.db, SESSION)
        self.assertEqual(selection["provider_id"], "prov")
        self.assertEqual(selection["model_id"], "model-x")
        self.assertIn("session_entry", selection["source"])


class ToolExecutionStateTests(unittest.TestCase):
    """工具请求与执行状态的统一分类：拒绝必须有完整诊断证据。"""

    def test_completed_is_execution(self):
        self.assertEqual(H.tool_execution_state(
            {"status": "completed", "name": "Read"}), "completed")

    def test_denied_requires_full_diagnostics(self):
        record = {"status": "error", "error_type": "permission_denied",
                  "duration_ms": 0, "output_bytes": 0, "exit_code": None}
        self.assertEqual(H.tool_execution_state(record),
                         "denied_not_executed")

    def test_missing_diagnostics_fail_closed(self):
        # 只有 error_type，没有 duration_ms/output_bytes：无法证明未执行。
        record = {"status": "error", "error_type": "permission_denied"}
        self.assertEqual(H.tool_execution_state(record),
                         "execution_unknown")
        # 列存在但值为 NULL：同样没有“零执行”证据。
        record = {"status": "error", "error_type": "permission_denied",
                  "duration_ms": None, "output_bytes": None}
        self.assertEqual(H.tool_execution_state(record),
                         "execution_unknown")

    def test_denied_with_output_is_executed(self):
        record = {"status": "error", "error_type": "permission_denied",
                  "duration_ms": 0, "output_bytes": 12}
        self.assertEqual(H.tool_execution_state(record), "executed_failed")

    def test_denied_with_exit_code_is_executed(self):
        record = {"status": "error", "error_type": "permission_denied",
                  "duration_ms": 0, "output_bytes": 0, "exit_code": 1}
        self.assertEqual(H.tool_execution_state(record), "executed_failed")

    def test_non_policy_error_is_executed(self):
        record = {"status": "error", "error_type": "tool_execution_failed",
                  "duration_ms": 0, "output_bytes": 0}
        self.assertEqual(H.tool_execution_state(record), "executed_failed")

    def test_running_or_cancelled_is_unknown(self):
        for status in ("running", "cancelled"):
            self.assertEqual(H.tool_execution_state({"status": status}),
                             "execution_unknown")


class ExecutionStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "db.sqlite")
        con = sqlite3.connect(self.db)
        con.execute("create table session_entry (session_id text, type text,"
                    " data text, time_created integer)")
        con.commit()
        con.close()

    def _sql(self, statement, params=()):
        con = sqlite3.connect(self.db)
        con.execute(statement, params)
        con.commit()
        con.close()

    def test_execution_state_plan_mode_and_unknown(self):
        self._sql("insert into session_entry values (?,?,?,?)",
                  (SESSION, "runtime/execution_state", json.dumps(
                      {"mode": "build", "planEnabled": True}), 1))
        state = H.session_execution_state(self.db, SESSION)
        self.assertTrue(state["found"])
        self.assertTrue(state["plan_enabled"])
        self.assertEqual([code for code, _d in H.execution_state_problems(state)],
                         ["host_session_plan_mode"])

        self._sql("update session_entry set data=? "
                  "where type='runtime/execution_state'",
                  (json.dumps({"mode": "yolo"}),))
        state = H.session_execution_state(self.db, SESSION)
        self.assertEqual([code for code, _d in H.execution_state_problems(state)],
                         ["host_session_state_unknown"])

    def test_execution_state_missing_and_malformed(self):
        self.assertEqual(
            [code for code, _d in H.execution_state_problems(
                H.session_execution_state(self.db, SESSION))],
            ["host_session_state_missing"])
        self._sql("insert into session_entry values (?,?,?,?)",
                  (SESSION, "runtime/execution_state", "{bad json", 1))
        state = H.session_execution_state(self.db, SESSION)
        self.assertTrue(state["found"])
        self.assertEqual([code for code, _d in H.execution_state_problems(state)],
                         ["host_session_state_unknown"])

    def test_execution_state_non_plan_is_clean(self):
        self._sql("insert into session_entry values (?,?,?,?)",
                  (SESSION, "runtime/execution_state", json.dumps(
                      {"mode": "yolo", "planEnabled": False}), 1))
        state = H.session_execution_state(self.db, SESSION)
        self.assertFalse(state["plan_enabled"])
        self.assertEqual(H.execution_state_problems(state), [])

    def test_host_version_is_probed_not_cached(self):
        report = H.host_version(asar_path=os.path.join(self.tmp.name,
                                                       "missing.asar"))
        self.assertEqual(report["source"], "unavailable")
        self.assertEqual(report["version"], "")

    def test_host_version_accepts_integer_offset_zero(self):
        import struct
        payload = json.dumps({"name": "@zcode/desktop",
                              "version": "3.19.0"}).encode("utf-8")
        header = json.dumps({"files": {"package.json": {
            "offset": 0, "size": len(payload)}}}).encode("utf-8")
        header_size = len(header) + 9
        blob = b"".join([
            struct.pack("<I", 4),
            struct.pack("<I", header_size),
            struct.pack("<I", header_size - 4),
            struct.pack("<I", len(header)),
            header,
        ])
        blob += b"\0" * (8 + header_size - len(blob))
        blob += payload
        path = os.path.join(self.tmp.name, "fake.asar")
        with open(path, "wb") as fh:
            fh.write(blob)
        report = H.host_version(asar_path=path)
        self.assertEqual(report["version"], "3.19.0")
        self.assertEqual(report["source"], "app.asar:package.json")

    def test_host_version_malformed_header_is_unavailable(self):
        import struct
        header = json.dumps({"files": "not-an-object"}).encode("utf-8")
        header_size = len(header) + 9
        blob = b"".join([
            struct.pack("<I", 4),
            struct.pack("<I", header_size),
            struct.pack("<I", header_size - 4),
            struct.pack("<I", len(header)),
            header,
        ])
        blob += b"\0" * (8 + header_size - len(blob))
        path = os.path.join(self.tmp.name, "bad.asar")
        with open(path, "wb") as fh:
            fh.write(blob)
        report = H.host_version(asar_path=path)
        self.assertEqual(report["source"], "unavailable")
        self.assertEqual(report["version"], "")


if __name__ == "__main__":
    unittest.main()

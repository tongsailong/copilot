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


if __name__ == "__main__":
    unittest.main()

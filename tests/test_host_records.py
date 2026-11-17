#!/usr/bin/env python3
"""宿主 SQLite 只读访问层：模型选择记录。

v8 起 G4 退役，本模块只保留执行信封所需的模型记录读取；tool_usage /
最终输出 / 子会话执行模式等 G4 专用取证已移除。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import host_records as HR  # noqa: E402


class HostRecordsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "host.sqlite")
        con = sqlite3.connect(self.db)
        con.execute("create table session_entry (session_id text, type text,"
                    " data text, time_created integer)")
        con.commit()
        con.close()

    def add_entry(self, session_id: str, payload: dict, created: int = 1):
        con = sqlite3.connect(self.db)
        con.execute("insert into session_entry values (?,?,?,?)",
                    (session_id, "runtime/model_selection",
                     json.dumps(payload, ensure_ascii=False), created))
        con.commit()
        con.close()

    def test_model_selection_reads_last_valid_record(self):
        self.add_entry("c1", {"modelSelection": {
            "providerId": "p1", "modelId": "m1"}}, created=1)
        self.add_entry("c1", {"modelSelection": {
            "providerId": "p2", "modelId": "m2",
            "options": {"reasoningLevel": "high"}}}, created=2)
        selection = HR.model_selection(self.db, "c1")
        self.assertEqual(selection["provider_id"], "p2")
        self.assertEqual(selection["model_id"], "m2")
        self.assertEqual(selection["reasoning"], "high")
        self.assertEqual(selection["source"],
                         "session_entry/runtime/model_selection")

    def test_missing_session_returns_empty(self):
        self.assertEqual(HR.model_selection(self.db, "nope"), {})

    def test_missing_db_returns_empty(self):
        self.assertEqual(
            HR.model_selection(
                os.path.join(self.tmp.name, "missing.sqlite"), "c1"), {})

    def test_malformed_payload_is_skipped(self):
        con = sqlite3.connect(self.db)
        con.execute("insert into session_entry values (?,?,?,?)",
                    ("c1", "runtime/model_selection", "not-json", 1))
        con.commit()
        con.close()
        self.add_entry("c1", {"modelSelection": {
            "providerId": "p", "modelId": "m"}}, created=2)
        selection = HR.model_selection(self.db, "c1")
        self.assertEqual(selection["provider_id"], "p")

    def test_connect_ro_does_not_create_db(self):
        missing = os.path.join(self.tmp.name, "absent.sqlite")
        with self.assertRaises(OSError):
            HR.connect_ro(missing)
        self.assertFalse(os.path.exists(missing))

    def test_table_columns(self):
        con = HR.connect_ro(self.db)
        self.addCleanup(con.close)
        self.assertEqual(
            HR.table_columns(con, "session_entry"),
            {"session_id", "type", "data", "time_created"})
        self.assertEqual(HR.table_columns(con, "missing_table"), set())


if __name__ == "__main__":
    unittest.main()

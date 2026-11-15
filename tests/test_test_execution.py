#!/usr/bin/env python3
"""真实测试执行证据绑定测试（合成 SQLite + 临时证据文件）。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import test_execution as TE  # noqa: E402

sys.path.insert(0, HERE)
import governance as G  # noqa: E402
import native_agent as NA  # noqa: E402
import v6_support as F  # noqa: E402

SESSION = "sess_exec_test"
PARENT = "sess_parent"
AGENT = "agent_test"
TRACE = "trace-1"
COMMAND = "node --test tests/calc.test.js"
META_START = 1794700800000   # 2026-11-15T00:00:00Z
META_END = 1794701400000     # 2026-11-15T00:10:00Z
CALL_START = 1794700860000   # 2026-11-15T00:01:00Z
CALL_END = 1794700920000     # 2026-11-15T00:02:00Z
ATTEMPT_CREATED = "2026-11-15T00:20:00.000Z"


def sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class TestExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.db = os.path.join(self.dir, "db.sqlite")
        self.artifact = os.path.join(self.dir, "test-output.txt")
        with open(self.artifact, "w", encoding="utf-8") as fh:
            fh.write("tests 2 / pass 2 / fail 0\n")
        self.sha = sha(self.artifact)
        self.metadata = os.path.join(self.dir, "metadata.json")
        with open(self.metadata, "w", encoding="utf-8") as fh:
            json.dump({
                "agentId": AGENT,
                "childSessionId": SESSION,
                "parentSessionId": PARENT,
                "profileId": "executor",
                "profileSnapshot": {"name": "executor"},
                "metadataFile": self.metadata,
                "cwd": self.dir,
                "status": "completed",
                "createdAt": "2026-11-15T00:00:00.000Z",
                "completedAt": "2026-11-15T00:10:00.000Z",
            }, fh)
        self.attempt = {
            "review_attempt_id": "RA" + "a" * 24,
            "code_snapshot": {"workspace": self.dir},
            "code_scope_sha256": "c" * 64,
            "created_at": ATTEMPT_CREATED,
            "evidence_snapshot": [{"id": "E1", "path": self.artifact,
                                   "sha256": self.sha}],
        }
        self.execution = {
            "task_id": "T1",
            "work_item_id": "A1",
            "executor": {"agent_id": AGENT,
                         "child_session_id": SESSION},
            "executor_metadata_path": self.metadata,
            "test_commands": [COMMAND],
            "test_artifacts": ["E1"],
            "code_scope_sha256": "c" * 64,
            "evidence": [{"id": "E1", "file": self.artifact,
                          "sha256": self.sha}],
        }
        self._db()

    def _db(self, *, exit_code=0, status="completed", command=COMMAND,
            output="ok\n", call_id="call-1", trace=TRACE, turn="turn-1",
            started=CALL_START, completed=CALL_END):
        con = sqlite3.connect(self.db)
        con.execute("create table if not exists tool_usage (tool_call_id text,"
                    " session_id text, tool_name text, status text,"
                    " exit_code integer, started_at integer, completed_at integer,"
                    " output_bytes integer, error_code text, error_message text,"
                    " trace_id text, turn_id text)")
        con.execute("create table if not exists part (session_id text, data text)")
        con.execute("create table if not exists session (id text, parent_id text,"
                    " task_type text, trace_id text)")
        con.execute("delete from tool_usage")
        con.execute("delete from part")
        con.execute("insert into tool_usage values (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (call_id, SESSION, "Bash", status, exit_code, started,
                     completed, len(output), "", "", trace, turn))
        con.execute("insert into session values (?,?,?,?)",
                    (SESSION, PARENT, "subagent_child", TRACE))
        con.execute("insert into part values (?,?)", (SESSION, json.dumps({
            "type": "tool", "tool": "Bash", "callID": call_id,
            "state": {"status": status, "input": {"command": command},
                      "output": output}}, ensure_ascii=False)))
        con.commit()
        con.close()

    def test_verified_binding(self):
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "verified", record["problems"])
        self.assertEqual(len(record["commands"]), 1)
        call = record["commands"][0]
        self.assertEqual(call["command"], COMMAND)
        self.assertEqual(call["exit_code"], 0)
        self.assertEqual(call["trace_id"], TRACE)
        self.assertEqual(call["turn_id"], "turn-1")
        self.assertEqual(record["artifacts"][0]["sha256"], self.sha)
        self.assertTrue(record["checks"]["code_scope_bound"])
        self.assertEqual(record["executor_metadata_sha256"], sha(self.metadata))
        self.assertEqual(record["session_parent_id"], PARENT)
        self.assertEqual(record["session_trace_id"], TRACE)

    def test_nonzero_exit_is_failed(self):
        self._db(exit_code=1)
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "failed")
        self.assertTrue(any("exit_code_nonzero" in p
                            for p in record["problems"]))

    def test_missing_exit_is_unverified(self):
        self._db(exit_code=None)
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "unverified")
        self.assertTrue(any("exit_code_missing" in p
                            for p in record["problems"]))

    def test_command_not_found_is_unverified(self):
        self._db(command="npm test")
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "unverified")
        self.assertTrue(any("test_command_not_found" in p
                            for p in record["problems"]))

    def test_missing_test_commands_is_unverified(self):
        self.execution.pop("test_commands")
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "unverified")
        self.assertIn("test_commands_not_declared", record["problems"])

    def test_stale_artifact_is_failed(self):
        old = CALL_START / 1000 - 3600
        os.utime(self.artifact, (old, old))
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "failed", record["problems"])
        self.assertTrue(any("test_artifact_stale" in item
                            for item in record["problems"]))

    def test_artifact_drift_is_failed(self):
        with open(self.artifact, "a", encoding="utf-8") as fh:
            fh.write("drift\n")
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "failed")
        self.assertTrue(any("artifact_drift" in p for p in record["problems"]))

    def test_artifact_must_be_frozen_in_attempt(self):
        self.attempt["evidence_snapshot"] = []
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertNotEqual(record["status"], "verified")
        self.assertTrue(any("artifact_not_frozen" in p
                            for p in record["problems"]))

    def test_code_scope_mismatch_is_not_verified(self):
        self.execution["code_scope_sha256"] = "d" * 64
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertNotEqual(record["status"], "verified")
        self.assertIn("execution_code_scope_mismatch", record["problems"])

    def test_missing_execution_code_scope_is_unverified(self):
        self.execution.pop("code_scope_sha256")
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "unverified")
        self.assertIn("execution_code_scope_missing", record["problems"])

    def _rewrite_metadata(self, **updates):
        with open(self.metadata, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data.update(updates)
        with open(self.metadata, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _session(self, **updates):
        row = {"parent_id": PARENT, "task_type": "subagent_child",
               "trace_id": TRACE}
        row.update(updates)
        con = sqlite3.connect(self.db)
        con.execute("delete from session")
        con.execute("insert into session values (?,?,?,?)",
                    (SESSION, row["parent_id"], row["task_type"],
                     row["trace_id"]))
        con.commit()
        con.close()

    def test_metadata_file_copy_is_rejected(self):
        self._rewrite_metadata(metadataFile="/tmp/other-metadata.json")
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "failed")
        self.assertIn("executor_metadata_file_mismatch", record["problems"])

    def test_metadata_without_file_field_is_unverified(self):
        self._rewrite_metadata()
        data = json.load(open(self.metadata, encoding="utf-8"))
        data.pop("metadataFile")
        with open(self.metadata, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "unverified")
        self.assertIn("executor_metadata_file_missing", record["problems"])

    def test_metadata_without_workspace_is_rejected(self):
        data = json.load(open(self.metadata, encoding="utf-8"))
        data.pop("cwd")
        with open(self.metadata, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "unverified")
        self.assertIn("executor_workspace_missing", record["problems"])

    def test_cwd_under_workspace_is_allowed(self):
        sub = os.path.join(self.dir, "sub", "dir")
        os.makedirs(sub)
        self._rewrite_metadata(cwd=sub)
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "verified", record["problems"])

    def test_cwd_outside_workspace_is_rejected(self):
        self._rewrite_metadata(cwd="/tmp/outside-workspace")
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "failed")
        self.assertIn("executor_workspace_mismatch", record["problems"])

    def test_workspace_root_conflict_is_rejected(self):
        self._rewrite_metadata(workspaceRoot="/tmp/other-workspace")
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "failed")
        self.assertIn("executor_workspace_mismatch", record["problems"])

    def test_readonly_profile_cannot_be_executor(self):
        # 判定按宿主有效工具策略，与 Profile 名称无关：任何名字的只读 Profile
        # 都不能承担工程执行。
        for name in ("reviewer", "readonly-any"):
            with self.subTest(name=name):
                self._rewrite_metadata(
                    profileId=name,
                    profileSnapshot={
                        "name": name,
                        "tools": ["Read", "Grep", "Glob"],
                        "disallowedTools": ["Write", "Edit", "Bash"],
                    })
                record = TE.build(self.execution, self.attempt, db=self.db)
                self.assertEqual(record["status"], "failed")
                self.assertIn("executor_profile_readonly", record["problems"])

    def test_profile_snapshot_mismatch_is_failed(self):
        self._rewrite_metadata(profileSnapshot={"name": "other"})
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "failed")
        self.assertIn("executor_profile_mismatch", record["problems"])

    def test_workspace_mismatch_is_failed(self):
        self.attempt["code_snapshot"]["workspace"] = "/tmp/other-workspace"
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "failed")
        self.assertIn("executor_workspace_mismatch", record["problems"])

    def test_metadata_missing_is_unverified(self):
        os.remove(self.metadata)
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "unverified")
        self.assertTrue(any("executor_metadata_missing" in p
                            for p in record["problems"]))

    def test_metadata_agent_mismatch_is_failed(self):
        self._rewrite_metadata(agentId="agent_other")
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "failed")
        self.assertIn("executor_metadata_agent_mismatch", record["problems"])

    def test_metadata_child_session_substitution_is_failed(self):
        self._rewrite_metadata(childSessionId="sess_other")
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "failed")
        self.assertIn("executor_metadata_session_mismatch",
                      record["problems"])

    def test_host_session_kind_is_failed(self):
        self._session(task_type="main")
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "failed")
        self.assertIn("executor_session_kind", record["problems"])

    def test_host_session_parent_is_failed(self):
        self._session(parent_id="sess_other")
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "failed")
        self.assertIn("executor_session_parent_mismatch",
                      record["problems"])

    def test_host_session_trace_is_failed(self):
        self._session(trace_id="trace-other")
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "failed")
        self.assertIn("executor_session_trace_mismatch", record["problems"])

    def test_call_outside_session_window_is_failed(self):
        self._db(started=META_END, completed=META_END + 60000)
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "failed")
        self.assertIn("test_execution_time_outside_session",
                      record["problems"])

    def test_call_after_attempt_is_failed(self):
        self.attempt["created_at"] = "2026-11-15T00:01:30.000Z"
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "failed")
        self.assertIn("test_execution_after_attempt", record["problems"])

    def test_duplicate_command_is_ambiguous(self):
        self._db(call_id="call-1")
        con = sqlite3.connect(self.db)
        con.execute("insert into tool_usage values (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("call-2", SESSION, "Bash", "completed", 0,
                     CALL_START + 1000, CALL_END + 1000, 3, "", "", TRACE,
                     "turn-1"))
        con.execute("insert into part values (?,?)", (SESSION, json.dumps({
            "type": "tool", "tool": "Bash", "callID": "call-2",
            "state": {"status": "completed", "input": {"command": COMMAND},
                      "output": "ok"}}, ensure_ascii=False)))
        con.commit()
        con.close()
        record = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(record["status"], "unverified")
        self.assertTrue(any("test_command_ambiguous" in p
                            for p in record["problems"]))


    def _binding(self, **over):
        data = {
            "schema": "copilot-execution-binding/v1",
            "binding_id": "EB" + "b" * 24,
            "nonce": "a" * 32,
            "task_id": "T1",
            "contract_sha256": "",
            "work_item_id": "A1",
            "workspace": os.path.realpath(self.dir),
            "test_commands": [COMMAND],
            "created_at": "2026-11-14T00:00:00+00:00",
            "consumed": True,
            "consumed_by_result_sha256": "e" * 64,
        }
        data.update(over)
        return data

    def _with_binding(self, **over):
        self._rewrite_metadata(prompt=f"run tests nonce {'a' * 32} please")
        self.execution["result_sha256"] = "e" * 64
        self.execution["execution_binding"] = {
            "binding_id": "EB" + "b" * 24, "nonce": "a" * 32,
            "created_at": "2026-11-14T00:00:00+00:00",
            "workspace": os.path.realpath(self.dir),
            "test_commands": [COMMAND]}
        self.execution.update(over)
        return self._binding()

    def test_contract_no_test_is_not_applicable(self):
        self._rewrite_metadata(prompt=f"run nonce {'a' * 32}")
        self.execution["result_sha256"] = "e" * 64
        self.execution["test_commands"] = []
        self.execution["test_artifacts"] = []
        self.execution["execution_binding"] = {
            "binding_id": "EB" + "b" * 24, "nonce": "a" * 32,
            "created_at": "2026-11-14T00:00:00+00:00",
            "workspace": os.path.realpath(self.dir), "test_commands": []}
        binding = self._binding(
            test_commands=[], require_test_result=False,
            require_artifact_integrity=False,
            requirement_source="contract",
            contract_test_policy={
                "required": False, "commands": [], "report": "",
                "artifacts": [], "require_test_result": False,
                "require_artifact_integrity": False},
            contract_report="", contract_artifacts=[])
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True)
        self.assertEqual(record["status"], "not_applicable",
                         record["problems"])
        self.assertFalse(any(record["layers"].values()))
        self.assertEqual(record["required_layers"], 0)

    def test_contract_artifact_missing_is_failed(self):
        binding = self._with_binding()
        binding["contract_artifacts"] = ["reports/other.xml"]
        binding["requirement_source"] = "contract"
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True)
        self.assertEqual(record["status"], "failed", record["problems"])
        self.assertTrue(any("contract_artifact_missing" in item
                            for item in record["problems"]))

    def test_contract_path_outside_workspace_is_failed(self):
        binding = self._with_binding()
        binding["contract_report"] = "/etc/passwd"
        binding["requirement_source"] = "contract"
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True)
        self.assertEqual(record["status"], "failed", record["problems"])
        self.assertTrue(any("contract_path_outside_workspace" in item
                            for item in record["problems"]))

    def test_contract_report_missing_is_failed(self):
        binding = self._with_binding()
        binding["contract_report"] = "reports/junit.xml"
        binding["requirement_source"] = "contract"
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True)
        self.assertEqual(record["status"], "failed", record["problems"])
        self.assertTrue(any("contract_report_missing" in item
                            for item in record["problems"]))

    def test_binding_bound_and_verified(self):
        binding = self._with_binding()
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True)
        self.assertEqual(record["status"], "verified", record["problems"])
        self.assertEqual(record["execution_binding_id"], "EB" + "b" * 24)

    def test_required_binding_missing_is_failed(self):
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=None, require_binding=True)
        self.assertEqual(record["status"], "failed")
        self.assertIn("execution_binding_missing", record["problems"])

    def test_contract_changed_after_binding_is_failed(self):
        binding = self._with_binding()
        binding["contract_sha256"] = "c" * 64
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True,
                          expected_contract_sha256="d" * 64)
        self.assertEqual(record["status"], "failed", record["problems"])
        self.assertIn("execution_binding_contract_changed",
                      record["problems"])

    def test_session_before_binding_is_failed(self):
        binding = self._with_binding()
        binding["created_at"] = "2026-11-15T00:05:00+00:00"
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True)
        self.assertEqual(record["status"], "failed")
        self.assertIn("execution_binding_session_precedes", record["problems"])

    def test_prompt_without_nonce_is_failed(self):
        binding = self._with_binding()
        self._rewrite_metadata(prompt="run tests please")
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True)
        self.assertEqual(record["status"], "failed")
        self.assertIn("execution_binding_prompt_unbound", record["problems"])

    def test_binding_consumed_by_other_result_is_failed(self):
        binding = self._with_binding()
        binding["consumed_by_result_sha256"] = "f" * 64
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True)
        self.assertEqual(record["status"], "failed")
        self.assertIn("execution_binding_consumed_by_other",
                      record["problems"])

    def test_binding_command_change_is_failed(self):
        binding = self._with_binding()
        binding["test_commands"] = ["npm test"]
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True)
        self.assertEqual(record["status"], "failed")
        self.assertIn("execution_binding_command_mismatch",
                      record["problems"])


    def _add_junit(self, text, *, eid="E9"):
        path = os.path.join(self.dir, f"{eid}.xml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        digest = sha(path)
        self.execution.setdefault("evidence", []).append(
            {"id": eid, "file": path, "sha256": digest})
        self.execution["test_artifacts"] = [eid]
        self.attempt["evidence_snapshot"].append(
            {"id": eid, "path": path, "sha256": digest})
        return path

    def test_junit_pass_satisfies_required_test_result(self):
        self._add_junit('<testsuite tests="2" failures="0" errors="0"'
                        ' skipped="0"><testcase name="a"/>'
                        '<testcase name="b"/></testsuite>')
        binding = self._with_binding()
        binding["require_test_result"] = True
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True)
        self.assertTrue(record["layers"]["test_result_verified"])
        self.assertTrue(record["layers"]["expected_test_verified"])
        self.assertTrue(record["layers"]["artifact_integrity_verified"])
        self.assertEqual(record["status"], "verified", record["problems"])
        self.assertEqual(record["test_result"]["tests"], 2)
        self.assertEqual(record["test_result"]["passed"], 2)

    def test_junit_failure_does_not_verify_result(self):
        self._add_junit('<testsuite tests="2" failures="1" errors="0">'
                        '<testcase name="a"><failure/></testcase>'
                        '<testcase name="b"/></testsuite>')
        binding = self._with_binding()
        binding["require_test_result"] = True
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True)
        self.assertFalse(record["layers"]["test_result_verified"])
        self.assertNotEqual(record["status"], "verified")
        self.assertEqual(record["test_result"]["failures"], 1)
        self.assertIn("a", record["test_result"]["failed_cases"])


    def test_junit_failure_element_without_attribute_is_not_verified(self):
        self._add_junit('<testsuite tests="1"><testcase name="a">'
                        '<failure/></testcase></testsuite>')
        binding = self._with_binding()
        binding["require_test_result"] = True
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True)
        self.assertFalse(record["layers"]["test_result_verified"])
        self.assertEqual(record["test_result"]["failed_cases"], ["a"])

    def test_junit_without_testcases_is_not_verified(self):
        self._add_junit('<testsuite tests="1"/>')
        binding = self._with_binding()
        binding["require_test_result"] = True
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True)
        self.assertFalse(record["layers"]["test_result_verified"])

    def test_junit_skipped_child_without_attribute_is_counted(self):
        self._add_junit('<testsuite tests="1"><testcase name="a">'
                        '<skipped/></testcase></testsuite>')
        binding = self._with_binding()
        binding["require_test_result"] = True
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True)
        self.assertFalse(record["layers"]["test_result_verified"])
        self.assertEqual(record["test_result"]["skipped"], 1)

    def test_junit_tests_attribute_mismatch_is_unparseable(self):
        self._add_junit('<testsuite tests="100"><testcase name="a"/>'
                        '</testsuite>')
        binding = self._with_binding()
        binding["require_test_result"] = True
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True)
        self.assertFalse(record["layers"]["test_result_verified"])
        self.assertIsNone(record["test_result"])

    def test_junit_all_skipped_is_not_verified(self):
        self._add_junit('<testsuite tests="2" skipped="2">'
                        '<testcase name="a"><skipped/></testcase>'
                        '<testcase name="b"><skipped/></testcase>'
                        '</testsuite>')
        binding = self._with_binding()
        binding["require_test_result"] = True
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True)
        self.assertFalse(record["layers"]["test_result_verified"])
        self.assertEqual(record["test_result"]["passed"], 0)

    def test_required_test_result_without_junit_is_unverified(self):
        binding = self._with_binding()
        binding["require_test_result"] = True
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True)
        self.assertFalse(record["layers"]["test_result_verified"])
        self.assertEqual(record["status"], "unverified")
        self.assertTrue(any("JUnit" in item for item in record["limitations"]))

    def test_expected_command_mismatch_fails_expected_layer(self):
        binding = self._with_binding()
        binding["test_commands"] = ["npm test"]
        record = TE.build(self.execution, self.attempt, db=self.db,
                          binding=binding, require_binding=True)
        self.assertFalse(record["layers"]["expected_test_verified"])
        self.assertIn("execution_binding_command_mismatch",
                      record["problems"])

    def test_binding_is_deterministic(self):
        first = TE.build(self.execution, self.attempt, db=self.db)
        second = TE.build(self.execution, self.attempt, db=self.db)
        self.assertEqual(first["binding_id"], second["binding_id"])

    def test_db_missing_is_unverified(self):
        record = TE.build(self.execution, self.attempt,
                          db=os.path.join(self.dir, "missing.sqlite"))
        self.assertEqual(record["status"], "unverified")
        self.assertTrue(any("host_db_unavailable" in p
                            for p in record["problems"]))


class ExecutionIngestBindingTest(F.BridgeBase):
    """build_execution → ingest → attempt → test_execution 的真实字段贯通。"""

    COMMAND = "node --test tests/calc.test.js"

    def _iso(self, minutes: int) -> str:
        moment = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _ms(self, minutes: int) -> int:
        return int((datetime.now(timezone.utc) - timedelta(minutes=minutes))
                   .timestamp() * 1000)

    def test_execution_envelope_feeds_verified_test_binding(self):
        state = G.load_state(self.state_path)
        state["native_dispatch"] = self.dispatch
        G.save_state(self.state_path, state)
        binding = G.create_execution_binding(
            self.state_path, work_item_id="A1",
            test_commands=[self.COMMAND], workspace=self.dir)
        meta = os.path.join(self.dir, "exec-metadata.json")
        with open(meta, "w", encoding="utf-8") as fh:
            json.dump({
                "agentId": "agent-exec", "childSessionId": "child-exec",
                "parentSessionId": "sess_parent",
                "profileId": "executor",
                "profileSnapshot": {"name": "executor"},
                "metadataFile": meta,
                "cwd": self.dir,
                "status": "completed",
                "createdAt": self._iso(10),
                "completedAt": self._iso(5),
                "totalDurationMs": 300000,
                "usage": {"totalTokens": 100},
            }, fh)
        outdir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, outdir, True)
        rollout = os.path.join(outdir, "rollout.jsonl")
        with open(rollout, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"sessionId": "child-exec",
                                 "model": {"providerId": "p",
                                           "modelId": "m"}}) + "\n")
        args = SimpleNamespace(
            state=self.state_path, dispatch=write_json_path(
                outdir, "dispatch.json", self.dispatch),
            work_item="A1", metadata=meta, model_rollout=rollout,
            result_file="", result_text="实际结果", summary="完成",
            status="done", evidence=[f"E1={self.evidence}"],
            test_command=[self.COMMAND], test_artifact=["E1"],
            workspace=self.dir, base="", output="",
            binding=binding["binding_id"])
        env = NA.build_execution(args)
        self.assertEqual(env["test_commands"], [self.COMMAND])
        self.assertEqual(env["test_artifacts"], ["E1"])
        self.assertEqual(len(env["code_scope_sha256"]), 64)
        path = write_json_path(outdir, "execution.json", env)
        proc = F.run_cli("copilot_auto.py", "ingest", "--state",
                         self.state_path, "--result-file", path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = G.load_state(self.state_path)
        stored = state["native_results"][-1]
        attempt = G.create_review_attempt(
            self.state_path, work_item_id="A1",
            execution_result_sha256=stored["result_sha256"],
            workspace=self.dir)
        self.assertEqual(stored["code_scope_sha256"],
                         attempt["code_scope_sha256"])
        self.assertEqual(stored["executor_metadata_path"], meta)
        db = os.path.join(outdir, "db.sqlite")
        con = sqlite3.connect(db)
        con.execute("create table tool_usage (tool_call_id text, session_id"
                    " text, tool_name text, status text, exit_code integer,"
                    " started_at integer, completed_at integer, output_bytes"
                    " integer, error_code text, error_message text, trace_id"
                    " text, turn_id text)")
        con.execute("create table part (session_id text, data text)")
        con.execute("create table session (id text, parent_id text,"
                    " task_type text, trace_id text)")
        con.execute("insert into tool_usage values (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("call-1", "child-exec", "Bash", "completed", 0,
                     self._ms(8), self._ms(7), 2, "", "", "trace-e2e", "t1"))
        con.execute("insert into part values (?,?)", ("child-exec", json.dumps({
            "type": "tool", "tool": "Bash", "callID": "call-1",
            "state": {"status": "completed",
                      "input": {"command": self.COMMAND}, "output": "ok"}})))
        con.execute("insert into session values (?,?,?,?)",
                    ("child-exec", "sess_parent", "subagent_child", "trace-e2e"))
        con.commit()
        con.close()
        record = TE.build(stored, attempt, db=db)
        self.assertEqual(record["status"], "verified", record["problems"])
        self.assertTrue(record["checks"]["code_scope_bound"])
        self.assertEqual(record["executor_metadata_path"], meta)


def write_json_path(directory: str, name: str, payload) -> str:
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


if __name__ == "__main__":
    unittest.main()

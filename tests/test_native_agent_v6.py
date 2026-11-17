#!/usr/bin/env python3
"""v6 原生复核生成与驱动保护。

阶段九起 Reviewer 只允许 Read/Grep/Glob，复核身份、工具调用与最终输出只从宿主
持久化 DB 取证；`build_review` 生成的 `g4_eligible` 只是结构预检，正式 G4 由
`g4_attest` 新链决定。驱动上限仍不是任务终点。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import sqlite3  # noqa: E402

import copilot_auto as auto  # noqa: E402
import governance as G  # noqa: E402
import native_agent as NA  # noqa: E402
import native_bridge as NB  # noqa: E402
import v6_support as F  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""

PLAN = {"summary": "实现", "actions": [
    {"id": "A1", "title": "实现", "goal": "产出结果",
     "expected_evidence": "结果文件"},
]}


def write_json(path: str, payload) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


class BuildReviewTests(unittest.TestCase):
    """`native_agent.py review` 只从 metadata/宿主 DB 取证，不信任自报字段。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="目标", contract=CONTRACT,
                      execution_backend="native", risk="high",
                      test_policy={"required": False})
        G.plan_state(self.state_path, PLAN)
        state = G.load_state(self.state_path)
        self.dispatch = NB.build_dispatch(state, G.ready_actions(state))
        state["native_dispatch"] = self.dispatch
        G.save_state(self.state_path, state)
        self.dispatch_path = write_json(
            os.path.join(self.dir, "dispatch.json"), self.dispatch)
        self.evidence = os.path.join(self.dir, "result.txt")
        with open(self.evidence, "w", encoding="utf-8") as fh:
            fh.write("done\n")
        item = self.dispatch["actions"][0]
        self.execution = {
            "schema": NB.RESULT_SCHEMA,
            "task_id": state["task_id"],
            "contract_sha256": state["contract_sha256"],
            "plan_id": self.dispatch["plan_id"],
            "plan_sha256": self.dispatch["plan_sha256"],
            "iteration_id": self.dispatch["iteration_id"],
            "dispatch_id": item["dispatch_id"],
            "idempotency_key": item["idempotency_key"],
            "work_item_id": item["work_item_id"],
            "status": "done",
            "summary": "完成",
            "result": "实际结果",
            "evidence": [{"id": "E1", "file": self.evidence,
                          "sha256": NB.sha256_file(self.evidence), "kind": "file"}],
            "executor": {"kind": "subagent", "agent_id": "agent-exec",
                         "child_session_id": "child-exec",
                         "profile_id": "executor",
                         "profile": {"tools": ["*"]},
                         "model_reported": "m"},
            "timing": {"duration_ms": 10},
            "usage": {"total_tokens": 10},
        }
        self.execution["result_sha256"] = NB.envelope_sha256(self.execution)
        self.execution_path = write_json(
            os.path.join(self.dir, "execution.json"), self.execution)
        proc = F.run_cli("copilot_auto.py", "ingest", "--state", self.state_path,
                         "--result-file", self.execution_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.state = G.load_state(self.state_path)
        self.attempt = G.create_review_attempt(
            self.state_path, work_item_id="A1",
            execution_result_sha256=self.execution["result_sha256"],
            workspace=self.dir)
        # Reviewer 宿主产物放在工作区外，避免把取证文件算进执行代码快照。
        self.host_dir = tempfile.mkdtemp(prefix="review-host-")
        self.addCleanup(shutil.rmtree, self.host_dir, True)
        self.host = F.make_fake_review_host(
            self.host_dir, execution=self.execution, dispatch=self.dispatch,
            evidence=self.evidence, task_id=self.state["task_id"],
            attempt_id=self.attempt["review_attempt_id"],
            session_created_at=self.attempt["created_at"],
            workspace=self.dir)
        self.db = self._db()

    def _db(self, *, calls=None, with_output: bool = True,
            tools_ok: bool = True) -> str:
        self.db_seq = getattr(self, "db_seq", 0) + 1
        db = os.path.join(self.host_dir, f"host-{self.db_seq}.sqlite")
        output = ""
        if with_output:
            with open(self.host["output_file"], encoding="utf-8") as fh:
                output = fh.read()
        con = sqlite3.connect(db)
        con.execute("create table session (id text, parent_id text,"
                    " task_type text, trace_id text)")
        con.execute("create table session_entry (session_id text, type text,"
                    " data text, time_created integer)")
        con.execute("create table tool_usage (tool_call_id text, session_id text,"
                    " tool_name text, status text, started_at integer,"
                    " error_type text, error_code text, duration_ms integer,"
                    " output_bytes integer, stdout_bytes integer,"
                    " stderr_bytes integer, exit_code integer,"
                    " error_message text)")
        con.execute("create table part (id text, message_id text,"
                    " session_id text, data text, sequence integer)")
        con.execute("create table message (id text, session_id text,"
                    " sequence integer, time_created integer, data text)")
        child = self.host["child_session_id"]
        con.execute("insert into session values (?,?,?,?)",
                    (child, "sess-parent", "subagent_child", "trace-x"))
        con.execute("insert into session_entry values (?,?,?,?)",
                    (child, "runtime/model_selection", json.dumps({
                        "modelSelection": {"providerId": "fake",
                                           "modelId": "reviewer-model"}}), 1))
        con.execute("insert into session_entry values (?,?,?,?)",
                    (child, "runtime/execution_state", json.dumps({
                        "mode": "yolo", "planEnabled": False}), 2))
        if calls is None:
            calls = [("c1", "Read", {"file_path": self.evidence}),
                     ("c2", "Grep", {"pattern": "done", "path": self.evidence})]
        for idx, (cid, name, payload) in enumerate(calls):
            con.execute(
                "insert into tool_usage (tool_call_id, session_id, tool_name,"
                " status, started_at) values (?,?,?,?,?)",
                (cid, child, name, "completed", idx))
            con.execute("insert into part values (?,?,?,?,?)",
                        (f"p{idx}", f"m{idx}", child, json.dumps({
                            "type": "tool", "tool": name, "callID": cid,
                            "state": {"status": "completed",
                                      "input": payload}}, ensure_ascii=False),
                         idx))
        con.execute("insert into message values (?,?,?,?,?)",
                    ("m-final", child, 9, 999, json.dumps({
                        "role": "assistant", "finish": "stop"}, ensure_ascii=False)))
        if with_output:
            con.execute("insert into part values (?,?,?,?,?)",
                        ("p-final", "m-final", child, json.dumps({
                            "type": "text", "text": output}, ensure_ascii=False), 10))
        con.commit()
        con.close()
        return db

    def _args(self, **overrides) -> SimpleNamespace:
        args = SimpleNamespace(
            state=self.state_path, dispatch=self.dispatch_path,
            execution=self.execution_path, work_item="A1",
            metadata=self.host["metadata_path"],
            input=self.host["output_file"], output="",
            attempt=self.attempt["review_attempt_id"], db=self.db)
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def _set_total_tool_use(self, count: int) -> None:
        path = self.host["metadata_path"]
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
        meta["totalToolUseCount"] = count
        write_json(path, meta)

    def _add_tool_usage(self, db: str, *, call_id: str, tool: str,
                        status: str, error_type: str = "",
                        duration_ms: int | None = None,
                        output_bytes: int = 0) -> None:
        con = sqlite3.connect(db)
        con.execute(
            "insert into tool_usage (tool_call_id, session_id, tool_name,"
            " status, started_at, error_type, duration_ms, output_bytes)"
            " values (?,?,?,?,?,?,?,?)",
            (call_id, self.host["child_session_id"], tool, status, 5,
             error_type or None, duration_ms, output_bytes))
        con.commit()
        con.close()

    def test_denied_bash_request_does_not_invalidate_review(self):
        # plan 模式拒绝 Bash：宿主记录有请求，但确认没有执行。
        db = self._db()
        self._add_tool_usage(db, call_id="c-denied", tool="Bash",
                             status="error", error_type="permission_denied",
                             duration_ms=0, output_bytes=0)
        self._set_total_tool_use(3)
        env = NA.build_review(self._args(db=db))
        self.assertTrue(env["g4_eligible"])
        self.assertEqual([c["name"] for c in env["host"]["tool_calls"]],
                         ["Grep", "Read"])
        self.assertEqual(env["files_inspected"], [self.evidence])

    def test_project_profile_source_is_not_a_g4_carrier(self):
        path = self.host["metadata_path"]
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
        meta["profileSnapshot"]["source"] = "project"
        write_json(path, meta)
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review(self._args())
        self.assertIn("profile_source_not_builtin", str(ctx.exception))

    def test_plan_mode_session_is_rejected(self):
        db = self._db()
        con = sqlite3.connect(db)
        con.execute(
            "update session_entry set data=? "
            "where type='runtime/execution_state'",
            (json.dumps({"mode": "build", "planEnabled": True}),))
        con.commit()
        con.close()
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review(self._args(db=db))
        self.assertIn("host_session_plan_mode", str(ctx.exception))

    def test_missing_execution_state_is_rejected(self):
        db = self._db()
        con = sqlite3.connect(db)
        con.execute(
            "delete from session_entry where type='runtime/execution_state'")
        con.commit()
        con.close()
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review(self._args(db=db))
        self.assertIn("host_session_state_missing", str(ctx.exception))

    def test_executed_bash_request_still_rejected(self):
        db = self._db()
        self._add_tool_usage(db, call_id="c-exec", tool="Bash",
                             status="error", error_type="tool_execution_failed",
                             duration_ms=9, output_bytes=3)
        self._set_total_tool_use(3)
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review(self._args(db=db))
        self.assertIn("review_tool_not_allowed", str(ctx.exception))

    def test_unknown_bash_request_state_still_rejected(self):
        db = self._db()
        self._add_tool_usage(db, call_id="c-unknown", tool="Bash",
                             status="error", error_type="", duration_ms=None,
                             output_bytes=0)
        self._set_total_tool_use(3)
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review(self._args(db=db))
        self.assertIn("review_tool_not_allowed", str(ctx.exception))

    def test_build_review_uses_db_records_for_identity(self):
        env = NA.build_review(self._args())
        # 阶段九：g4_eligible 只是结构预检；正式资格由 g4_attest 决定。
        self.assertTrue(env["g4_eligible"])
        self.assertEqual(env["schema"], NB.REVIEW_SCHEMA)
        self.assertEqual(env["reviewer"]["agent_id"], self.host["agent_id"])
        self.assertEqual(env["reviewer"]["model_reported"],
                         "fake/reviewer-model")
        self.assertEqual(env["host"]["tool_call_source"],
                         "db.tool_usage+part")
        self.assertEqual(env["host"]["rollout_path"], "")
        self.assertEqual(env["commands_run"], [])
        self.assertEqual(
            sorted(env["host"]["tool_calls"], key=lambda c: c["name"]),
            [{"name": "Grep", "count": 1}, {"name": "Read", "count": 1}])

    def _rewrite_metadata(self, **profile_fields) -> None:
        path = self.host["metadata_path"]
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
        if "profile_id" in profile_fields:
            meta["profileId"] = profile_fields.pop("profile_id")
        meta["profileSnapshot"].update(profile_fields)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

    def test_reviewer_profile_name_is_not_gating(self):
        # Profile 名称改变不影响合法复核：信封记录真实名称，资格由有效策略决定。
        self._rewrite_metadata(name="generic-readonly", profile_id="generic-readonly")
        env = NA.build_review(self._args())
        self.assertTrue(env["g4_eligible"])
        self.assertEqual(env["reviewer"]["profile_id"], "generic-readonly")
        self.assertEqual(env["reviewer"]["profile"]["tools"],
                         ["Read", "Grep", "Glob"])
        self.assertEqual(env["reviewer"]["profile"]["name"], "generic-readonly")

    def test_reviewer_profile_with_bash_is_rejected(self):
        self._rewrite_metadata(tools=["Read", "Grep", "Glob", "Bash"])
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review(self._args())
        self.assertIn("profile_tools_not_exact", str(ctx.exception))

    def test_builtin_explore_is_rejected_with_capability_remedy(self):
        # 内置 Explore 含 Bash；拒绝信息必须说明它不是只读代理，而不是只报工具不符。
        self._rewrite_metadata(name="Explore", profile_id="Explore",
                               source="built-in",
                               tools=["Bash", "Glob", "Grep", "Read",
                                      "WebFetch", "WebSearch", "TodoWrite"])
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review(self._args())
        message = str(ctx.exception)
        self.assertIn("profile_tools_not_exact", message)
        self.assertIn("内置 Explore", message)
        self.assertIn("Bash", message)

    def test_builtin_general_purpose_is_rejected_as_reviewer(self):
        self._rewrite_metadata(name="general-purpose",
                               profile_id="general-purpose", source="built-in",
                               tools=["*"])
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review(self._args())
        message = str(ctx.exception)
        self.assertIn("profile_tools_not_exact", message)
        self.assertIn("general-purpose", message)

    def test_reviewer_profile_with_write_is_rejected(self):
        self._rewrite_metadata(tools=["Read", "Grep", "Glob", "Write"])
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review(self._args())
        self.assertIn("profile_tools_not_exact", str(ctx.exception))

    def test_reviewer_profile_mcp_is_rejected(self):
        self._rewrite_metadata(mcpServers=["playwright"])
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review(self._args())
        self.assertIn("profile_mcp_servers", str(ctx.exception))

    def test_reviewer_policy_missing_fails_closed(self):
        # 只有 Prompt/README 声明只读、没有宿主有效策略时不得成为复核证据。
        self._rewrite_metadata(tools=None, disallowedTools=None)
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review(self._args())
        self.assertIn("profile_tools_empty", str(ctx.exception))

    def test_non_pass_review_is_generated_but_not_g4(self):
        # 高风险 Reviewer 可以如实报告 partial/blocking；官方生成路径不能因此
        # 拒绝整轮复核，但信封不得自称 G4。
        with open(self.host["output_file"], "w", encoding="utf-8") as fh:
            json.dump({"schema": NB.REVIEW_SCHEMA,
                       "task_id": self.state["task_id"],
                       "plan_id": self.dispatch["plan_id"],
                       "dispatch_id": self.execution["dispatch_id"],
                       "work_item_id": "A1",
                       "execution_result_sha256":
                           self.execution["result_sha256"],
                       "verdict": "partial", "findings": [
                           {"id": "F1", "severity": "major",
                            "summary": "需要修复", "file": self.evidence,
                            "evidence": "实际读取"}],
                       "files_inspected": [self.evidence],
                       "commands_run": [], "changed_files": [],
                       "unverified": [], "residual_risks": []},
                      fh, ensure_ascii=False)
        con = sqlite3.connect(self.db)
        con.execute("update part set data=? where id='p-final'", (json.dumps({
            "type": "text", "text": open(self.host["output_file"],
                                          encoding="utf-8").read()},
            ensure_ascii=False),))
        con.commit(); con.close()
        env = NA.build_review(self._args())
        self.assertFalse(env["g4_eligible"])
        self.assertEqual(env["verdict"], "partial")
        self.assertEqual(env["findings"][0]["severity"], "major")

    def test_non_readonly_tool_is_rejected(self):
        db = self._db(calls=[("c1", "Read", {"file_path": self.evidence}),
                             ("c2", "Bash", {"command": "git status"})])
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review(self._args(db=db))
        self.assertIn("review_tool_not_allowed", str(ctx.exception))

    def test_output_file_symlink_outside_agent_dir_is_rejected(self):
        outside = tempfile.mkdtemp(prefix="outside-output-")
        self.addCleanup(shutil.rmtree, outside, True)
        target = os.path.join(outside, "output.json")
        with open(self.host["output_file"], encoding="utf-8") as src:
            text = src.read()
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.remove(self.host["output_file"])
        os.symlink(target, self.host["output_file"])
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review(self._args())
        self.assertIn("review_output_misplaced", str(ctx.exception))

    def test_db_query_failure_raises_record_error_not_nameerror(self):
        con = sqlite3.connect(self.db)
        con.execute("drop table tool_usage")
        con.commit()
        con.close()
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review(self._args())
        self.assertIn("review_host_db_unavailable", str(ctx.exception))

    def test_output_missing_everywhere_fails_closed(self):
        os.remove(self.host["output_file"])
        db = self._db(with_output=False)
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA.build_review(self._args(db=db))
        self.assertIn("review_output_missing", str(ctx.exception))


class DriveGuardTests(unittest.TestCase):
    def test_partial_returns_executable_work_order_instead_of_stopping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            G.start_state(path, goal="目标", contract=CONTRACT)
            # 没有任何工作项：任务只能是 PARTIAL，drive 必须本地派生并给出工作单。
            code = auto.drive(SimpleNamespace(state=path, max_actions=1,
                                              dispatch_out=""))
            self.assertEqual(code, auto.EXIT_EXECUTE)
            state = G.load_state(path)
            self.assertIn("AUTO-C1", [a.get("id") for a in state["actions"]])
            self.assertEqual(G.evaluate(state)["phase"], "RUNNING")

    def test_step_limit_is_an_internal_error_code_not_a_verdict(self):
        # 32 步上限只是防死循环的本地保护；达到上限时返回内部错误而不是 PASS/BLOCKED。
        # 当前派生规则下 PARTIAL 总会先产生可领取工作项并以 10 退出，所以这里核对
        # 常量与退出码映射，而不是构造不可达的 32 步场景。
        self.assertEqual(auto.MAX_DRIVE_STEPS, 32)
        self.assertEqual(auto.EXIT_ERROR, 12)
        self.assertNotIn(auto.EXIT_ERROR, (auto.EXIT_DONE, auto.EXIT_WAIT))


class RepeatedGapTests(unittest.TestCase):
    def test_capability_gap_derives_capability_work_not_review(self):
        # 最近一次取证是“宿主/Profile 能力缺口”时，不应继续派生普通 G4 派发。
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            G.start_state(path, goal="目标", contract=CONTRACT,
                          execution_backend="native", risk="high",
                          test_policy={"required": False})
            G.plan_state(path, PLAN)
            state = G.load_state(path)
            state.setdefault("actions", []).append({
                "id": "A1", "title": "实现", "origin": "zcode-plan",
                "status": "done", "result": "完成", "evidence_ids": [],
            })
            state["g4_attestations"] = [{
                "status": "attested",
                "new_reason": ["host_session_plan_mode"],
                "attestation": {"rejections": [
                    {"code": "host_session_plan_mode", "detail": "plan mode"}]},
            }]
            G.save_state(path, state)
            state = G.load_state(path)
            created = G.derive_work(state)
            ids = [a["id"] for a in created]
            self.assertTrue(any(item.startswith("AUTO-G4-CAPABILITY")
                                for item in ids), ids)
            self.assertFalse(any(item == "AUTO-G4" or item.startswith("AUTO-G4-INGEST")
                                 for item in ids), ids)

    def test_recent_capability_gap_does_not_block_after_fix(self):
        # 更早的能力缺口不应长期阻断：只看最近一次取证。
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            G.start_state(path, goal="目标", contract=CONTRACT,
                          execution_backend="native", risk="high",
                          test_policy={"required": False})
            G.plan_state(path, PLAN)
            state = G.load_state(path)
            state.setdefault("actions", []).append({
                "id": "A1", "title": "实现", "origin": "zcode-plan",
                "status": "done", "result": "完成", "evidence_ids": [],
            })
            state["g4_attestations"] = [
                {"status": "attested", "new_reason": ["host_session_plan_mode"],
                 "attestation": {"rejections": [
                     {"code": "host_session_plan_mode", "detail": ""}]}},
                {"status": "attested", "new_reason": ["host_tool_not_allowed"],
                 "attestation": {"rejections": [
                     {"code": "host_tool_not_allowed", "detail": ""}]}},
            ]
            G.save_state(path, state)
            state = G.load_state(path)
            created = G.derive_work(state)
            ids = [a["id"] for a in created]
            self.assertFalse(any(item.startswith("AUTO-G4-CAPABILITY")
                                 for item in ids), ids)

    def test_repeated_g4_gap_derives_replan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            G.start_state(path, goal="目标", contract=CONTRACT,
                          execution_backend="native", risk="high",
                          test_policy={"required": False})
            G.plan_state(path, PLAN)
            state = G.load_state(path)
            # 模拟连续三轮 G4 工作项被反复派生/标记完成但门禁仍缺。
            for index in range(3):
                action_id = "AUTO-G4" if index == 0 else f"AUTO-G4-R{index + 1}"
                state["actions"].append({
                    "id": action_id, "title": "G4 复核", "origin": "g4-review",
                    "status": "done", "result": "复核未通过", "evidence_ids": [],
                })
            G.save_state(path, state)
            state = G.load_state(path)
            created = G.derive_work(state)
            ids = [a["id"] for a in created]
            self.assertTrue(any(a["id"] == "AUTO-REPLAN" for a in created), ids)
            self.assertIn("AUTO-REPLAN", [a["id"] for a in state["actions"]])


if __name__ == "__main__":
    unittest.main()

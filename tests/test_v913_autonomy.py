#!/usr/bin/env python3
"""v9.13 自主执行闭环回归（T1–T6）。

对应任务完成条件 C1–C7：用户批准必须落到原任务并解除对应阻塞；PARTIAL 之后
ZCode 真正承接 NEXT 并再次 drive；认知失败不阻断工程；Stop/跨会话恢复可靠；
局部阻塞不冻结独立工作；未完成义务有可审计终态。T7（真实宿主 E2E）在交付时
单独执行，不在本文件内模拟。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import v6_support as F  # noqa: E402

G = F.G
import copilot_inbox as INBOX  # noqa: E402
import cognition_call as CC  # noqa: E402
import cognition_schedule as SCH  # noqa: E402
import copilot_dispatch as CD  # noqa: E402

CONTRACT = """## 完成条件
- 代码改动完成
- 真机验证完成

## 边界
- 不修改无关项目
"""
PLAN = {"summary": "实现与验证", "actions": [
    {"id": "A1", "title": "实现", "goal": "完成代码改动",
     "expected_evidence": "diff 与测试输出"},
    {"id": "A2", "title": "真机验证", "goal": "在真机上验证",
     "expected_evidence": "截图"},
]}
SESSION = "sess_v913_current"
OTHER = "sess_v913_other"
DISPATCH = "copilot_dispatch.py"
GUARD = "copilot_stop_guard.py"


def think_result(mode: str = "CONSULT") -> dict:
    return {
        "schema": CC.THINK_SCHEMA, "mode": mode,
        "model": CC.MODEL, "model_resolved": CC.MODEL,
        "reasoning_effort": "high", "reasoning_mode": "standard",
        "reasoning_resolved": {"effort": "high"}, "reasoning_verified": True,
        "zero_tool": True, "attempts": 1, "request_id": "req-1",
        "trace_id": "trace-1", "response_id": "resp-1",
        "materials_sha256": "e" * 64, "materials_chars": 10,
        "request_sha256": "r" * 64, "reply_text": "judgment: ok",
        "reply_sha256": "a" * 64, "reply_chars": 10, "usage": {"total_tokens": 42},
        "usage_available": True, "latency_ms": 1,
        "judgment": "先补测试", "essence": "缺少验证", "strategy": "先测后改",
        "tradeoffs": [], "risks": [], "priority": [],
        "verify": [], "reconsult": [], "unverified": [],
        "completed_at": G.now_iso(),
    }


class AutonomyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = self.tmp.name
        F.init_git_workspace(self.workspace)
        self.task_dir = os.path.join(self.workspace, ".copilot-tasks", "t1")
        os.makedirs(self.task_dir, exist_ok=True)
        self.state_path = os.path.join(self.task_dir, "state.json")
        G.start_state(self.state_path, goal="自主执行闭环回归",
                      contract=CONTRACT, task_id="v913-autonomy",
                      test_policy={"required": False}, workspace=self.workspace)
        G.plan_state(self.state_path, PLAN)
        self.bind(SESSION)

    def bind(self, session: str, task_dir: str | None = None) -> None:
        target = task_dir or self.task_dir
        payload = {"sessions": [{"session_id": session,
                                 "at": "2026-11-26T00:00:00+00:00"}],
                   "updated": "2026-11-26T00:00:00+00:00"}
        with open(os.path.join(target, "session.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    def state(self) -> dict:
        return G.load_state(self.state_path)

    def save(self, state: dict) -> None:
        G.save_state(self.state_path, state)

    def ev(self) -> dict:
        return G.evaluate(self.state(), owner=SESSION)

    def cont(self) -> dict:
        return self.ev().get("continuation") or {}

    def run_cli(self, script: str, *args: str, env: dict | None = None):
        merged = {"ZCODE_SESSION_ID": SESSION,
                  "COPILOT_COGNITION_AUTO": "off"}
        merged.update(env or {})
        return F.run_cli(script, *args, cwd=self.workspace, env=merged)

    def run_guard(self, *, payload: dict | None = None,
                  args: list[str] | None = None, session: str | None = SESSION):
        environment = dict(os.environ)
        environment["COPILOT_COGNITION_AUTO"] = "off"
        for key in ("COPILOT_STOP_GUARD", "ZCODE_SESSION_ID", "CLAUDE_SESSION_ID",
                    "CLAUDE_CODE_SESSION_ID"):
            environment.pop(key, None)
        if session:
            environment["ZCODE_SESSION_ID"] = session
        body = json.dumps(dict(payload or {"cwd": self.workspace},
                               **({"session_id": session} if session else {})))
        import subprocess
        return subprocess.run(
            [sys.executable, "-B", os.path.join(F.SCRIPTS, GUARD),
             *(args or [])],
            input=body, capture_output=True, text=True,
            cwd=self.workspace, env=environment)

    def block_all(self, reason: str = "缺少用户授权",
                  needs: str = "用户批准方案 A 并授权继续") -> str:
        block = G.add_block(self.state_path, reason, needs,
                            work_items=["A1", "A2"], criteria=["C1", "C2"])
        return block["block_id"]


# ── T1：用户批准恢复 ──────────────────────────────────────────────────────
class ApprovalRecoveryTests(AutonomyBase):
    def test_approval_maps_to_original_task_and_unblocks(self):
        task_id = self.state()["task_id"]
        block_id = self.block_all()
        self.assertEqual(self.ev()["phase"], "BLOCKED")
        # 用户回合由钩子原样落盘（不推断授权）。
        event = INBOX.record(self.state_path, "同意方案 A，按这个继续",
                             session=SESSION, state=self.state())
        self.assertTrue(event["actionable"])
        self.assertEqual([row["input_id"] for row in INBOX.actionable_pending(
            self.state_path)], [event["input_id"]])
        # 同一 task_id 不会被复制成第二个任务。
        self.assertEqual(os.path.abspath(G.find_task_state(self.workspace, task_id)),
                         os.path.abspath(self.state_path))
        proc = self.run_cli(
            DISPATCH, "input", "--state", self.state_path,
            "--consume", event["input_id"], "--block-id", block_id,
            "--reason", "用户原话满足 needs：批准方案 A 并授权继续",
            "--quote", "同意方案 A，按这个继续")
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
        state = self.state()
        block = next(item for item in state["blocks"]
                     if item.get("block_id") == block_id)
        self.assertTrue(block.get("resolved_at"))
        self.assertEqual(block.get("resolved_by"), "user")
        self.assertIn("同意方案 A", block.get("authorization_quote") or "")
        self.assertEqual(block.get("authorization_input_id"), event["input_id"])
        decision = state["decisions"][-1]
        self.assertEqual(decision["source"], "user")
        self.assertIn(block_id, decision["block_ids"])
        self.assertEqual(decision["input_id"], event["input_id"])
        self.assertIn("同意方案 A", decision["authorization_quote"])
        # 受影响工作项回到可执行；输入已处置。
        self.assertEqual(sorted(a["id"] for a in G.ready_actions(state)),
                         ["A1", "A2"])
        self.assertEqual(INBOX.pending(self.state_path), [])
        self.assertTrue(self.cont()["required"])

    def test_duplicate_task_id_is_refused(self):
        criteria = os.path.join(self.workspace, "criteria.json")
        with open(criteria, "w", encoding="utf-8") as fh:
            json.dump([{"criterion_id": "C1", "description": "条件"}], fh)
        other = os.path.join(self.workspace, ".copilot-tasks", "t2", "state.json")
        proc = self.run_cli(
            DISPATCH, "start", "--state", other, "--goal", "重复任务",
            "--task-id", "v913-autonomy", "--workspace", self.workspace,
            "--criteria-file", criteria)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("已存在", proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(other))


# ── T2：PARTIAL 真实承接与再次 drive ──────────────────────────────────────
class PartialContinuationTests(AutonomyBase):
    def test_drive_delivers_work_order_then_resumes(self):
        block_id = self.block_all()
        event = INBOX.record(self.state_path, "批准，继续执行", session=SESSION,
                             state=self.state())
        proc = self.run_cli(DISPATCH, "input", "--state", self.state_path,
                            "--consume", event["input_id"], "--block-id", block_id,
                            "--reason", "用户批准", "--quote", "批准，继续执行")
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
        first = self.run_cli("copilot_auto.py", "drive", "--state", self.state_path)
        self.assertEqual(first.returncode, 10, first.stdout + first.stderr)
        self.assertIn("工作项", first.stdout)
        self.assertIn("drive", first.stdout)
        # 回填一个工作项后再次 drive：必须继续下一项，而不是结束回合。
        G.record_action(self.state_path, "A1", "done", result="实现完成")
        second = self.run_cli("copilot_auto.py", "drive", "--state", self.state_path)
        self.assertEqual(second.returncode, 10, second.stdout + second.stderr)
        self.assertIn("工作项", second.stdout)
        self.assertNotIn("AUTO-C1 已完成", second.stdout)
        # next_step 已持久化，跨会话/宿主上限后仍能恢复“下一步是什么”。
        next_step = self.state().get("next_step") or {}
        self.assertEqual(next_step.get("schema"), "copilot-next-step/v1")
        self.assertTrue(next_step.get("required"))


# ── T3：认知失败不阻断工程 ────────────────────────────────────────────────
class CognitionFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="认知失败不阻断", contract=CONTRACT,
                      task_id="v913-cognition", workspace=self.dir,
                      task_kind="architecture", test_policy={"required": False})
        state = G.load_state(self.state_path)
        synced = G.sync_cognition_schedule(state, policy="auto",
                                           auto_execution=True, origin="test")
        if synced["changes"]:
            self.save(state)

    def save(self, state: dict) -> None:
        G.save_state(self.state_path, state)

    def state(self) -> dict:
        return G.load_state(self.state_path)

    def step(self):
        args = argparse.Namespace(state=self.state_path, cognition="auto",
                                  command="cognition-schedule")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = CD._cognition_schedule_step(self.state_path, args)
        return result, out.getvalue(), err.getvalue()

    def test_failure_is_recorded_and_engineering_continues(self):
        due = SCH.due_obligations(self.state())
        self.assertTrue(due, "启动窗口应已物化为义务")
        calls = {"count": 0}

        def boom(*_args, **_kwargs):
            calls["count"] += 1
            raise CC.CognitionError("upstream unavailable (test)")

        with mock.patch.dict(os.environ, {"COPILOT_COGNITION_AUTO": "on"}), \
                mock.patch.object(CC, "run_consult", side_effect=boom), \
                mock.patch.object(CC, "run_plan", side_effect=boom):
            result, out, err = self.step()
        self.assertEqual(calls["count"], 1)
        self.assertEqual(result.get("executed"), due[0]["obligation_id"])
        state = self.state()
        obligation = next(item for item in SCH.obligations(state)
                          if item.get("obligation_id") == due[0]["obligation_id"])
        self.assertEqual(obligation["status"], "failed")
        self.assertTrue(obligation.get("call_id"))
        self.assertNotEqual(obligation.get("status"), "satisfied")
        completed = [call for call in state.get("cognition_calls") or []
                     if str(call.get("status") or "") == "completed"]
        self.assertEqual(completed, [], "失败不得伪造成功")
        # 工程续跑判定不受影响：认知不是门禁。
        ev = G.evaluate(state)
        self.assertNotEqual(ev.get("phase"), "BLOCKED")
        # 同一事实不重复无意义请求。
        with mock.patch.dict(os.environ, {"COPILOT_COGNITION_AUTO": "on"}), \
                mock.patch.object(CC, "run_consult", side_effect=boom):
            self.step()
        self.assertEqual(calls["count"], 1)

    def test_success_path_still_records_call(self):
        due = SCH.due_obligations(self.state())
        calls = {"count": 0}

        def ok(*_args, **_kwargs):
            calls["count"] += 1
            return think_result()

        with mock.patch.dict(os.environ, {"COPILOT_COGNITION_AUTO": "on"}), \
                mock.patch.object(CC, "run_consult", side_effect=ok):
            result, _out, _err = self.step()
        self.assertEqual(calls["count"], 1)
        self.assertEqual(result.get("executed"), due[0]["obligation_id"])
        state = self.state()
        obligation = next(item for item in SCH.obligations(state)
                          if item.get("obligation_id") == due[0]["obligation_id"])
        self.assertEqual(obligation["status"], "satisfied")
        self.assertTrue(obligation.get("call_id"))


# ── T4：Stop / 跨会话恢复 ─────────────────────────────────────────────────
class CrossSessionRecoveryTests(AutonomyBase):
    def test_foreign_session_allows_but_bound_session_blocks(self):
        proc = self.run_guard(session=OTHER)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = self.run_guard(session=SESSION)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("续跑守卫", proc.stderr)

    def test_session_start_restores_next_step(self):
        self.run_cli("copilot_auto.py", "drive", "--state", self.state_path)
        proc = self.run_guard(args=["session-start"], session=SESSION,
                              payload={"cwd": self.workspace,
                                       "source": "resume",
                                       "session_id": SESSION})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("copilot-session-recovery", proc.stdout)
        self.assertIn("下一步命令", proc.stdout)
        self.assertIn("v913-autonomy", proc.stdout)

    def test_new_session_gets_pointer_not_takeover(self):
        proc = self.run_guard(args=["session-start"], session=OTHER,
                              payload={"cwd": self.workspace, "source": "startup",
                                       "session_id": OTHER})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("copilot-task-pointer", proc.stdout)
        self.assertNotIn("copilot-session-recovery", proc.stdout)

    def test_foreign_session_prompt_is_not_recorded(self):
        proc = self.run_guard(args=["user-prompt"], session=OTHER,
                              payload={"cwd": self.workspace,
                                       "session_id": OTHER, "source": "user",
                                       "prompt": "同意方案 A，按这个继续"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(INBOX.pending(self.state_path), [],
                         "其它会话的输入不得写进本会话的任务收件箱")

    def test_unbound_single_task_records_prompt(self):
        os.remove(os.path.join(self.task_dir, "session.json"))
        proc = self.run_guard(args=["user-prompt"], session=OTHER,
                              payload={"cwd": self.workspace,
                                       "session_id": OTHER, "source": "user",
                                       "prompt": "同意方案 A，按这个继续"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        pending = INBOX.pending(self.state_path)
        self.assertEqual(len(pending), 1, "无绑定会话且只有一个任务时应记录")
        self.assertTrue(pending[0]["actionable"])


# ── T5：局部阻塞不冻结独立工作 ────────────────────────────────────────────
class LocalBlockTests(AutonomyBase):
    def test_one_block_does_not_freeze_independent_work(self):
        G.plan_state(self.state_path, {"summary": "三件事", "actions": [
            {"id": "B1", "title": "本地工作", "goal": "本地实现",
             "expected_evidence": "diff"},
            {"id": "B2", "title": "需要授权", "goal": "推送远端",
             "expected_evidence": "远端分支"},
            {"id": "B3", "title": "补测试", "goal": "跑测试",
             "expected_evidence": "测试输出"},
        ]})
        block = G.add_block(self.state_path, "push 未授权", "用户授权 push",
                            work_items=["B2"], criteria=[])
        other = G.add_block(self.state_path, "等待外部接口", "接口恢复",
                            work_items=["B3"], criteria=[])
        state = self.state()
        ready = [a["id"] for a in G.ready_actions(state)]
        self.assertEqual(ready, ["A1", "A2", "B1"])
        ev = G.evaluate(state, owner=SESSION)
        self.assertNotEqual(ev["phase"], "BLOCKED")
        G.resolve_block(self.state_path, "用户授权 push", block_id=block["block_id"],
                        by="user", quote="同意推送")
        state = self.state()
        self.assertIn("B2", [a["id"] for a in G.ready_actions(state)])
        remaining = [item for item in state["blocks"] if not item.get("resolved_at")]
        self.assertEqual([item["block_id"] for item in remaining], [other["block_id"]],
                         "解除一个阻塞不得自动解除其他阻塞")

    def test_identical_block_is_not_duplicated(self):
        first = G.add_block(self.state_path, "需要用户批准", "用户批准方案 A",
                            work_items=["A2"])
        second = G.add_block(self.state_path, "需要用户批准", "用户批准方案 A",
                             work_items=["A2"])
        self.assertEqual(first["block_id"], second["block_id"])
        blocks = [item for item in self.state()["blocks"]
                  if item["work_items"] == ["A2"]]
        self.assertEqual(len(blocks), 1, "同一工作项同一原因只应有一条阻塞")
        self.assertEqual(blocks[0].get("duplicate_hits"), 1)


# ── T6：义务终态与审计 ────────────────────────────────────────────────────
class ObligationFixture(unittest.TestCase):
    """义务终态测试夹具：不包含测试方法，避免子类继承到不适用于自己的用例。"""

    POLICY = "off"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="义务终态", contract=CONTRACT,
                      task_id="v913-obligation", workspace=self.dir,
                      task_kind="architecture", test_policy={"required": False})
        state = G.load_state(self.state_path)
        synced = G.sync_cognition_schedule(
            state, policy=self.POLICY,
            auto_execution=(self.POLICY == "auto"), origin="test")
        if synced["changes"]:
            self.save(state)

    def state(self) -> dict:
        return G.load_state(self.state_path)

    def save(self, state: dict) -> None:
        G.save_state(self.state_path, state)

    def evidence(self) -> str:
        path = os.path.join(self.dir, "evidence.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("证据\n")
        state = self.state()
        eid = G.next_evidence_id(state)
        G.register_evidence(state, eid, file_path=path, note="测试证据")
        self.save(state)
        return eid

    def close_after_met(self) -> dict:
        eid = self.evidence()
        for criterion in self.state()["contract"]["criteria"]:
            G.record_criterion(self.state_path, criterion["criterion_id"],
                               "met", evidence_ids=[eid])
        return G.close_task(self.state_path, cognition_policy=self.POLICY,
                            cognition_auto=(self.POLICY == "auto"))


class PolicyOffObligationTests(ObligationFixture):
    """显式 `--cognition off`：义务窗口仍被物化，但从未被要求执行。"""

    POLICY = "off"

    def test_policy_off_obligation_is_not_counted_as_missed(self):
        """窗口打开时已显式关闭自动认知：义务终态如实记录，但不计漏调。"""
        due = SCH.due_obligations(self.state())
        self.assertTrue(due)
        result = self.close_after_met()
        self.assertEqual(result.get("verdict"), "pass")
        state = self.state()
        self.assertNotIn("next_step", state,
                         "收口后不得留下 required=true 的旧下一步快照")
        closed = [item for item in SCH.obligations(state)
                  if item.get("obligation_id") == due[0]["obligation_id"]][0]
        self.assertEqual(closed.get("window"), "closed")
        self.assertEqual(closed.get("disposition"), "not-executed")
        self.assertFalse(closed.get("missed"),
                         "策略关闭下未执行不得被记成漏调")
        self.assertTrue(closed.get("finished_at"), "关闭必须写 finished_at")
        self.assertIn("不计漏调", closed.get("disposition_basis") or "")
        audit = SCH.audit_summary(state)
        self.assertEqual(audit["dangling"], 0)
        self.assertEqual(audit["missing_audit"], 0)
        self.assertEqual(audit["missed"], 0,
                         "off 策略下的义务不能计入漏调")
        # 策略文案不能自相矛盾：off 不能渲染成“允许自动执行”。
        reasons = " ".join(row["disposition_reason"] for row in audit["rows"])
        self.assertIn("调度关闭", reasons)
        self.assertNotIn("off（允许自动执行）", reasons)
        warnings = " ".join(G.cognition_schedule_warnings(state))
        self.assertIn("不计漏调", warnings, "策略关闭的未执行窗口仍要在收口提醒里可见")

    def test_user_waived_obligation_is_legal_terminal_state(self):
        due = SCH.due_obligations(self.state())
        self.assertTrue(due)
        proc = F.run_cli(DISPATCH, "cognition-skip", "--state", self.state_path,
                         "--obligation-id", due[0]["obligation_id"],
                         "--kind", "waiver", "--by", "user",
                         "--quote", "这条认知判断不用做了",
                         "--reason", "用户明确批准放弃")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        state = self.state()
        row = next(item for item in SCH.obligations(state)
                   if item.get("obligation_id") == due[0]["obligation_id"])
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["skip_by"], "user-waived")
        self.assertEqual(row["authorization_by"], "user")
        audit = SCH.audit_summary(state)
        target = next(item for item in audit["rows"]
                      if item["obligation_id"] == due[0]["obligation_id"])
        self.assertEqual(target["disposition"], "skipped")
        self.assertFalse(target["missed"])
        self.assertEqual(audit["dangling"], 0)


class MissedObligationTests(ObligationFixture):
    """窗口打开时策略为 auto：关闭时仍未执行才算真正漏调。"""

    POLICY = "auto"

    def test_unexecuted_obligation_has_auditable_terminal_state(self):
        due = SCH.due_obligations(self.state())
        self.assertTrue(due)
        result = self.close_after_met()
        self.assertEqual(result.get("verdict"), "pass")
        state = self.state()
        self.assertNotIn("next_step", state,
                         "收口后不得留下 required=true 的旧下一步快照")
        closed = [item for item in SCH.obligations(state)
                  if item.get("obligation_id") == due[0]["obligation_id"]][0]
        self.assertEqual(closed.get("window"), "closed")
        self.assertEqual(closed.get("disposition"), "not-executed")
        self.assertTrue(closed.get("finished_at"), "关闭必须写 finished_at")
        self.assertEqual(closed.get("responsible"), "zcode")
        self.assertTrue(closed.get("resolution_condition"))
        self.assertTrue(closed.get("disposition_basis"))
        audit = SCH.audit_summary(state)
        self.assertEqual(audit["dangling"], 0)
        self.assertEqual(audit["missing_audit"], 0)
        self.assertGreaterEqual(audit["missed"], 1,
                                "auto 策略下未执行必须计入漏调")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""认知策略作用域回归（v9.13.1 后续修复，C1–C9）。

本文件锁定一条语义边界：**单次命令的 `--cognition` / 环境变量只影响本次调用**，
任务级策略只能由受控入口（`start --cognition`、`cognition-policy`）持久化。
v9.13.0 的真实缺陷是：一次 `evidence --cognition off` 把 `off` 写进任务级策略
快照，于是整条任务后续的 A/B/C/E 全部静默——C/E 再也触发不了。

C1  单次 `--cognition off` 不改变任务级策略
C2  任务级 off 必须经受控的持久化操作（`cognition-policy` + 原因）
C3  纯登记/报告/状态类命令不会隐式改变任务级策略
C4  任务级 auto 时 C（重大事件）能触发真实调用并关联 task/cycle/window/call
C5  任务级 auto 时 E（收口）能触发真实调用并记录 c_id/摘要
C6  单次 off 之后，后续 C/E 仍然正常触发（联合场景）
C7  任务级 off 时 C/E 按规则跳过且不误报漏调
C8  既有 A/B 调度、自主续跑与输入承接没有回归
C9  历史任务（无 `cognition_policy` 字段）只读兼容、不被追溯改写

所有认知通道都用替身；本文件不发起任何真实模型请求（真实调用证据在宿主 E2E 中
单独采集）。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import v6_support as F

G = F.G
import cognition_call as CC  # noqa: E402
import cognition_schedule as SCH  # noqa: E402
import copilot_dispatch as CD  # noqa: E402
import copilot_auto as CA  # noqa: E402

CONTRACT = "## 完成条件\n- 产出可复核的结果文件\n"


def think_result(mode: str = "CONSULT") -> dict:
    """认知通道成功的结构化结果（通道已替换，不联网）。"""
    return {
        "schema": CC.THINK_SCHEMA, "mode": mode,
        "model": CC.MODEL, "model_resolved": CC.MODEL,
        "reasoning_effort": "high", "reasoning_mode": "standard",
        "reasoning_resolved": {"effort": "high"}, "reasoning_verified": True,
        "zero_tool": True, "attempts": 1, "request_id": "req-1",
        "trace_id": "trace-1", "response_id": "resp-1",
        "materials_sha256": "e" * 64, "materials_chars": 10,
        "request_sha256": "r" * 64, "reply_text": "judgment: ok",
        "reply_sha256": "a" * 64, "reply_chars": 10,
        "usage": {"total_tokens": 42}, "usage_available": True,
        "latency_ms": 1, "judgment": "先补测试", "essence": "缺少验证",
        "strategy": "先测后改", "tradeoffs": [], "risks": [], "priority": [],
        "verify": [], "reconsult": [], "unverified": [],
        "completed_at": G.now_iso(),
    }


class PolicyScopeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        self.contract_path = os.path.join(self.dir, "contract.md")
        with open(self.contract_path, "w", encoding="utf-8") as fh:
            fh.write(CONTRACT)
        self.plan_path = os.path.join(self.dir, "plan.json")

    # ── 状态与 CLI ───────────────────────────────────────────────────────
    def state(self) -> dict:
        return G.load_state(self.state_path)

    def save(self, state: dict) -> None:
        G.save_state(self.state_path, state)

    def calls(self) -> list[dict]:
        return [item for item in self.state().get("cognition_calls") or []
                if isinstance(item, dict)]

    def obligations(self, family: str = "") -> list[dict]:
        return [item for item in SCH.obligations(self.state())
                if not family or str(item.get("family")) == family]

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = CD.main(argv)
            except SystemExit as exc:  # argparse 拒绝参数
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def start_cli(self, *extra: str, kind: str = "governance",
                  risk: str = "medium") -> tuple[int, str, str]:
        return self.run_cli([
            "start", "--state", self.state_path, "--goal", "交付可复核结果",
            "--contract-file", self.contract_path, "--task-kind", kind,
            "--risk", risk, "--workspace", self.dir, *extra])

    def write_plan(self, actions: int = 1) -> None:
        items = [{"id": f"A{index}", "title": f"工作{index}",
                  "goal": f"目标{index}", "expected_evidence": "证据"}
                 for index in range(1, actions + 1)]
        with open(self.plan_path, "w", encoding="utf-8") as fh:
            json.dump({"summary": "计划", "actions": items}, fh)

    def plan(self, actions: int = 1, *extra: str) -> tuple[int, str, str]:
        self.write_plan(actions)
        return self.run_cli(["plan", "--state", self.state_path,
                             "--plan", self.plan_path, *extra])

    def add_evidence(self, name: str = "e.txt") -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("证据\n")
        state = self.state()
        eid = G.next_evidence_id(state)
        G.register_evidence(state, eid, file_path=path, note="测试证据")
        self.save(state)
        return eid

    def decision(self, text: str = "采用方案 A") -> None:
        """登记一个决策点：这是 C 事件（decision-point）的确定性来源。"""
        G.add_decision(self.state_path, text=text, source="user")

    @contextlib.contextmanager
    def channel(self, *, auto: str = "on", env: dict | None = None):
        """替换认知通道并显式允许自动执行；一次也不联网。"""
        calls: list[dict] = []

        def fake(mode: str):
            def run(*_args, **_kwargs):
                calls.append({"mode": mode})
                return think_result(mode)
            return run

        envs = {"COPILOT_COGNITION_AUTO": auto, **(env or {})}
        with mock.patch.dict(os.environ, envs), \
                mock.patch.object(CC, "run_consult", side_effect=fake("CONSULT")), \
                mock.patch.object(CC, "run_plan", side_effect=fake("PLAN")):
            yield calls


# ── C1：单次覆盖不改变任务级策略 ─────────────────────────────────────────
class SingleCommandOverrideTests(PolicyScopeBase):
    def setUp(self):
        super().setUp()
        with self.channel():
            code, _out, _err = self.start_cli("--cognition", "auto")
        self.assertEqual(code, 0)
        # 消耗掉启动窗口，后续断言只看“覆盖是否写进任务级策略”。
        self.assertEqual(SCH.task_policy_value(self.state()), "auto")

    def test_c1_single_off_does_not_change_task_policy(self):
        eid = self.add_evidence("scope.txt")
        code, _out, _err = self.run_cli([
            "evidence", "--state", self.state_path, "--add", "--file",
            os.path.join(self.dir, "scope.txt"), "--note", "一次纯登记",
            "--cognition", "off"])
        self.assertEqual(code, 0)
        state = self.state()
        # 任务级策略不变，且权威来源仍是 state.cognition_policy（不是快照）。
        self.assertEqual(SCH.task_policy_value(state), "auto")
        self.assertEqual(G.cognition_policy_view(state)["origin"],
                         "cognition_policy")
        self.assertEqual(state["cognition_policy"]["value"], "auto")
        snapshot = SCH.policy_snapshot(SCH.schedule_view(state), 1)
        self.assertEqual(snapshot.get("value"), "auto")
        # 单次覆盖只进审计环，并写明当时的任务级策略。
        overrides = SCH.schedule_view(state).get("policy_overrides") or []
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0]["value"], "off")
        self.assertEqual(overrides[0]["task_policy"], "auto")
        self.assertEqual(overrides[0]["command"], "evidence")
        # 后续命令（不传开关）仍按任务级 auto 求值。
        effective = CD._cognition_policy(
            argparse.Namespace(state="", cognition="", cognition_default="advise"),
            state)
        self.assertEqual(effective, "auto")
        self.assertEqual(eid, "E1")

    def test_c1_single_off_on_lifecycle_command_does_not_persist(self):
        """生命周期命令上的单次 off 同样只是本次覆盖。"""
        eid = self.add_evidence("life.txt")
        G.plan_state(self.state_path, {"summary": "计划", "actions": [
            {"id": "A1", "title": "工作", "goal": "目标",
             "expected_evidence": "证据"}]})
        with self.channel() as calls:
            code, _out, _err = self.run_cli([
                "record", "--state", self.state_path, "--id", "A1",
                "--status", "done", "--result", "完成", "--evidence", eid,
                "--cognition", "off"])
        self.assertIn(code, (0, CD.EXIT_CONTINUE))
        self.assertEqual(calls, [])          # 本次命令真的没调用
        self.assertEqual(SCH.task_policy_value(self.state()), "auto")
        self.assertEqual(
            SCH.policy_snapshot(SCH.schedule_view(self.state()), 1)["value"],
            "auto")
        overrides = [item for item in (SCH.schedule_view(self.state())
                                      .get("policy_overrides") or [])
                     if item.get("command") == "record"]
        self.assertTrue(overrides, "命令级覆盖必须留审计痕迹")


# ── C2：任务级 off 只能经受控入口 ────────────────────────────────────────
class TaskPolicyEntryTests(PolicyScopeBase):
    def setUp(self):
        super().setUp()
        with self.channel():
            self.start_cli("--cognition", "auto")

    def test_c2_start_persists_task_policy_record(self):
        record = self.state()["cognition_policy"]
        self.assertEqual(record["value"], "auto")
        self.assertEqual(record["source"], "start")
        self.assertTrue(record["set_at"])
        self.assertTrue(record["reason"])

    def test_c2_rejects_unknown_policy_and_missing_reason(self):
        code, _out, err = self.run_cli([
            "cognition-policy", "--state", self.state_path,
            "--policy", "maybe", "--reason", "测试"])
        self.assertNotEqual(code, 0)
        self.assertIn("非法", err)
        code, _out, err = self.run_cli([
            "cognition-policy", "--state", self.state_path, "--policy", "off"])
        self.assertNotEqual(code, 0)
        self.assertIn("原因", err)
        self.assertEqual(SCH.task_policy_value(self.state()), "auto")

    def test_c2_cognition_policy_persists_with_audit_fields(self):
        code, out, _err = self.run_cli([
            "cognition-policy", "--state", self.state_path,
            "--policy", "off", "--reason", "用户要求本任务停止自动认知调用",
            "--by", "user"])
        self.assertEqual(code, 0)
        self.assertIn("任务级认知策略已更新", out)
        state = self.state()
        record = state["cognition_policy"]
        self.assertEqual(record["value"], "off")
        self.assertEqual(record["previous"], "auto")
        self.assertEqual(record["set_by"], "user")
        self.assertTrue(record["set_at"])
        self.assertEqual(record["reason"], "用户要求本任务停止自动认知调用")
        self.assertEqual(SCH.task_policy_value(state), "off")
        history = [item for item in state.get("history") or []
                   if item.get("event") == "cognition_policy_set"]
        self.assertTrue(history, "任务级策略变更必须进历史")
        self.assertEqual(history[-1]["detail"]["previous"], "auto")
        self.assertEqual(history[-1]["detail"]["value"], "off")
        # 再改回来：previous 记录上一条真实值。
        code, _out, _err = self.run_cli([
            "cognition-policy", "--state", self.state_path,
            "--policy", "auto", "--reason", "恢复自动认知"])
        self.assertEqual(code, 0)
        self.assertEqual(self.state()["cognition_policy"]["previous"], "off")

    def test_c2_closed_task_rejects_policy_change(self):
        state = self.state()
        state["closure"] = {"closed": True}
        self.save(state)
        code, _out, err = self.run_cli([
            "cognition-policy", "--state", self.state_path,
            "--policy", "off", "--reason", "收口后修改"])
        self.assertNotEqual(code, 0)
        self.assertIn("收口", err)


# ── C3：纯登记命令永不隐式改策略 ─────────────────────────────────────────
class RegistrationCommandTests(PolicyScopeBase):
    def setUp(self):
        super().setUp()
        with self.channel():
            self.start_cli("--cognition", "auto")

    def test_c3_record_commands_never_change_task_policy(self):
        self.add_evidence("c3.txt")
        commands = [
            ["judgment", "--status", "pass", "--summary", "阶段判断"],
            ["report", "--summary", "执行报告"],
            ["decide", "--text", "决定：继续", "--source", "user"],
            ["evidence", "--list"],
            ["finding", "--list"],
        ]
        for extra_policy in ("off", "advise", "auto"):
            for base in commands:
                argv = [base[0], "--state", self.state_path, *base[1:],
                        "--cognition", extra_policy]
                code, _out, err = self.run_cli(argv)
                self.assertIn(code, (0, CD.EXIT_CONTINUE),
                              f"{base} 失败：{err}")
                state = self.state()
                self.assertEqual(SCH.task_policy_value(state), "auto",
                                 f"{base} 改动了任务级策略")
                snapshot = SCH.policy_snapshot(SCH.schedule_view(state), 1)
                self.assertEqual(snapshot.get("value"), "auto")

    def test_c3_status_and_audit_are_read_only(self):
        before = json.dumps(self.state(), ensure_ascii=False, sort_keys=True)
        for argv in (["status"], ["next"], ["cognition-audit"],
                     ["cognition-stats"]):
            code, _out, err = self.run_cli([argv[0], "--state", self.state_path])
            self.assertEqual(code, 0, err)
        after = self.state()
        self.assertEqual(SCH.task_policy_value(after), "auto")
        self.assertEqual(before, json.dumps(after, ensure_ascii=False,
                                           sort_keys=True))


# ── C4：任务级 auto 时 C 事件真实触发 ────────────────────────────────────
class EventWindowTests(PolicyScopeBase):
    def setUp(self):
        super().setUp()
        with self.channel():
            self.start_cli("--cognition", "auto")
            self.plan(1)

    def test_c4_decision_point_triggers_event_consult(self):
        # 只登记一个决策：这是可归因的 C 事件（decision-point），不是普通证据。
        self.decision("采用方案 A（用户决策）")
        advice = G.cognition_advice_summary(self.state())
        self.assertEqual(advice.get("family"), "C")
        self.assertEqual(advice.get("event"), "decision-point")
        with self.channel() as calls:
            code, _out, _err = self.run_cli([
                "record", "--state", self.state_path, "--id", "A1",
                "--status", "done", "--result", "完成"])
        self.assertEqual(code in (0, CD.EXIT_CONTINUE), True)
        self.assertEqual([item["mode"] for item in calls], ["CONSULT"])
        state = self.state()
        rows = self.obligations("C")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "satisfied")
        self.assertEqual(rows[0]["event"], "decision-point")
        call = [item for item in state["cognition_calls"]
                if item.get("obligation_id") == rows[0]["obligation_id"]][0]
        # 调用记录本身必须能读出归属：task_id / family / cycle / call_id。
        self.assertEqual(call["task_id"], state["task_id"])
        self.assertEqual(call["family"], "C")
        self.assertEqual(call["cycle"], 1)
        self.assertEqual(call["call_id"], rows[0]["call_id"])
        self.assertTrue(call["window_opened_at"])

    def test_c4_same_event_is_not_called_twice(self):
        self.decision("采用方案 A")
        with self.channel() as calls:
            self.run_cli(["record", "--state", self.state_path, "--id", "A1",
                          "--status", "done", "--result", "完成"])
            self.assertEqual(len(calls), 1)
            # 没有新信息：再跑一条生命周期命令，不产生第二条义务、也不重复付费。
            self.run_cli(["judgment", "--state", self.state_path, "--status",
                          "pass", "--summary", "阶段判断"])
            self.run_cli(["judgment", "--state", self.state_path, "--status",
                          "pass", "--summary", "阶段判断（重复）"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.obligations("C")), 1)


# ── C5/C6：收口窗口与联合场景 ─────────────────────────────────────────────
class ClosureAndJointTests(PolicyScopeBase):
    def _finish_work(self) -> str:
        """完成工作项、核销条件、登记测试证据：让 E 窗口具备准入。"""
        eid = self.add_evidence("tests-run.log")
        state = self.state()
        for action in state.get("actions") or []:
            G.record_action(self.state_path, action["id"], status="done",
                            result="完成", evidence_ids=[eid])
        state = self.state()
        for criterion in state["contract"]["criteria"]:
            G.record_criterion(self.state_path, criterion["criterion_id"],
                               "met", evidence_ids=[eid])
        report = os.path.join(self.dir, "report.md")
        with open(report, "w", encoding="utf-8") as fh:
            fh.write("Ran 9 tests\nOK\n")
        state = self.state()
        rid = G.next_evidence_id(state)
        G.register_evidence(state, rid, file_path=report, note="测试执行结果",
                            kind="test-report")
        self.save(state)
        for action in self.state().get("actions") or []:
            G.record_action(self.state_path, action["id"], status="done",
                            result="完成（测试通过）", evidence_ids=[rid])
        return rid

    def test_c5_task_level_auto_triggers_closure_consult(self):
        with self.channel():
            self.start_cli("--cognition", "auto")
            self.plan(1)
        self._finish_work()
        with self.channel() as calls:
            code, _out, _err = self.run_cli([
                "closure", "--state", self.state_path, "--close",
                "--by", "zcode-native"])
        self.assertEqual(code, 0, _err)
        self.assertEqual([item["mode"] for item in calls], ["CONSULT"])
        state = self.state()
        calls_done = [item for item in state["cognition_calls"]
                      if item.get("stage") == "closure"]
        self.assertEqual(len(calls_done), 1)
        self.assertEqual(calls_done[0]["family"], "E")
        self.assertEqual(calls_done[0]["cycle"], 1)
        self.assertTrue(calls_done[0]["final_verification_digest"])
        rows = self.obligations("E")
        self.assertEqual(rows[0]["status"], "satisfied")
        self.assertEqual(rows[0]["call_id"], calls_done[0]["call_id"])
        self.assertEqual(state["closure"]["cognition_schedule"]["missed"], 0)

    def test_c6_joint_scenario_single_off_then_c_and_e_still_trigger(self):
        """最重要回归：任务级 auto → 一次普通证据登记用 off → C 与 E 仍触发。"""
        with self.channel():
            self.start_cli("--cognition", "auto")
            self.plan(1)
        # 1) 一次纯登记命令显式 --cognition off（本次不调用）。
        eid = self.add_evidence("joint.txt")
        with self.channel() as calls:
            code, _out, _err = self.run_cli([
                "evidence", "--state", self.state_path, "--add", "--file",
                os.path.join(self.dir, "joint.txt"), "--note", "联合场景",
                "--cognition", "off"])
            self.assertEqual(code, 0)
            self.assertEqual(calls, [])
        self.assertEqual(SCH.task_policy_value(self.state()), "auto")
        # 2) 之后发生重大变化（决策点）→ C 正常触发。
        self.decision("联合场景：采用方案 B")
        with self.channel() as calls:
            code, _out, _err = self.run_cli([
                "record", "--state", self.state_path, "--id", "A1",
                "--status", "done", "--result", "完成", "--evidence", eid])
        self.assertEqual(code in (0, CD.EXIT_CONTINUE), True)
        self.assertEqual([item["mode"] for item in calls], ["CONSULT"])
        self.assertEqual(len(self.obligations("C")), 1)
        # 3) 最终 E 仍然触发，并在收口前完成。
        self._finish_work()
        with self.channel() as calls:
            code, _out, _err = self.run_cli([
                "closure", "--state", self.state_path, "--close",
                "--by", "zcode-native"])
        self.assertEqual(code, 0, _err)
        self.assertEqual([item["mode"] for item in calls], ["CONSULT"])
        state = self.state()
        closures = [item for item in state["cognition_calls"]
                    if item.get("stage") == "closure"]
        self.assertEqual(len(closures), 1)
        self.assertEqual(closures[0]["family"], "E")
        self.assertEqual(state["closure"]["cognition_schedule"]["missed"], 0)


# ── C7：任务级 off 下按策略跳过且不误报 ───────────────────────────────────
class TaskLevelOffTests(PolicyScopeBase):
    def test_c7_task_level_off_skips_c_and_e_without_false_missed(self):
        with self.channel():
            self.start_cli("--cognition", "off")
            self.plan(1)
        # 任务级 off：窗口仍然物化（可审计），但没有自动调用。
        with self.channel() as calls:
            self.decision("任务级 off 下的决策点")
            code, _out, _err = self.run_cli([
                "record", "--state", self.state_path, "--id", "A1",
                "--status", "done", "--result", "完成"])
        self.assertEqual(calls, [])
        self.assertIn(code, (0, CD.EXIT_CONTINUE))
        # off 下不会执行任何窗口，因此第一条义务（A）仍是 due；同一时刻只开一个
        # 窗口，C 事件不会单独物化成第二条义务——跳过是按策略的结果，不是漏做。
        self.assertEqual(self.obligations("C"), [])
        self.assertEqual([row["status"] for row in self.obligations("A")],
                         ["due"])
        # 收口：E 窗口按事实关闭，落 not-executed，但不计漏调。
        eid = self.add_evidence("off-tests.log")
        state = self.state()
        for criterion in state["contract"]["criteria"]:
            G.record_criterion(self.state_path, criterion["criterion_id"],
                               "met", evidence_ids=[eid])
        with self.channel() as calls:
            code, _out, _err = self.run_cli([
                "closure", "--state", self.state_path, "--close",
                "--by", "zcode-native"])
        self.assertEqual(code, 0, _err)
        self.assertEqual(calls, [])
        state = self.state()
        schedule = state["closure"]["cognition_schedule"]
        self.assertEqual(schedule["missed"], 0)
        rows = [item for item in SCH.obligations(state)
                if item.get("family") == "E"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # 未执行就关闭：状态仍是 due，窗口 closed，终态处置 not-executed。
        self.assertEqual(row["status"], "due")
        self.assertEqual(row["window"], "closed")
        self.assertEqual(row["disposition"], "not-executed")
        self.assertFalse(row["missed"])
        self.assertEqual(row["responsible"], "zcode")
        self.assertTrue(row["resolution_condition"])
        self.assertIn("off", row["disposition_basis"])
        self.assertFalse(SCH.is_missed(row))

    def test_c7_one_off_override_still_counts_as_missed(self):
        """对照：任务级 auto 下的单次 off 不改变任务策略，窗口如实记漏调。"""
        with self.channel():
            self.start_cli("--cognition", "auto")
            self.plan(1)
        eid = self.add_evidence("miss.txt")
        G.record_action(self.state_path, "A1", status="done", result="完成",
                        evidence_ids=[eid])
        state = self.state()
        for criterion in state["contract"]["criteria"]:
            G.record_criterion(self.state_path, criterion["criterion_id"],
                               "met", evidence_ids=[eid])
        with self.channel() as calls:
            code, _out, err = self.run_cli([
                "closure", "--state", self.state_path, "--close",
                "--by", "zcode-native", "--cognition", "off"])
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [])
        state = self.state()
        schedule = state["closure"]["cognition_schedule"]
        self.assertEqual(schedule["missed"], 1)
        self.assertEqual(schedule["dispositions"]["not-executed"], 1)
        self.assertEqual(SCH.task_policy_value(state), "auto")


# ── C8：既有调度/续跑/输入承接无回归 ─────────────────────────────────────
class ExistingFlowTests(PolicyScopeBase):
    def test_c8_ab_windows_still_fire_under_task_policy_auto(self):
        with self.channel() as calls:
            self.start_cli("--cognition", "auto")
            self.plan(1)
        self.assertEqual([item["mode"] for item in calls], ["CONSULT", "PLAN"])
        state = self.state()
        self.assertEqual([row["status"] for row in self.obligations("A")],
                         ["satisfied"])
        self.assertEqual([row["status"] for row in self.obligations("B")],
                         ["satisfied"])

    def test_c8_drive_off_override_does_not_pollute_following_drive(self):
        with self.channel():
            self.start_cli("--cognition", "auto")
            self.plan(2)
        args = argparse.Namespace(state=self.state_path, cognition="off",
                                  max_actions=1, dispatch_out="")
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = CA.drive(args)
        self.assertEqual(code, CA.EXIT_EXECUTE)   # 仍有工作项：必须继续
        self.assertEqual(SCH.task_policy_value(self.state()), "auto")
        args = argparse.Namespace(state=self.state_path, cognition="",
                                  max_actions=1, dispatch_out="")
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = CA.drive(args)
        self.assertEqual(code, CA.EXIT_EXECUTE)
        self.assertEqual(SCH.task_policy_value(self.state()), "auto")

    def test_c8_user_input_is_not_affected_by_command_policy(self):
        """输入承接是确定性事实，与认知策略无关。"""
        import copilot_inbox as INBOX
        with self.channel():
            self.start_cli("--cognition", "auto")
        INBOX.record(self.state_path, text="批准按方案 A 执行", session="s1")
        pending = INBOX.pending(self.state_path)
        self.assertEqual(len(pending), 1)
        with self.channel() as calls:
            self.run_cli(["evidence", "--state", self.state_path, "--add",
                          "--file", os.path.join(self.dir, "contract.md"),
                          "--cognition", "off"])
        self.assertEqual(calls, [])
        self.assertEqual(len(INBOX.pending(self.state_path)), 1)


# ── C9：历史任务兼容 ────────────────────────────────────────────────────
class LegacyStateTests(PolicyScopeBase):
    def test_c9_legacy_snapshot_still_read_without_migration(self):
        """升级前创建的任务只有周期快照：只读回退，不追溯改写。"""
        state = G.start_state(
            self.state_path, goal="历史任务", contract=CONTRACT,
            risk="medium", task_id="legacy-task", workspace=self.dir,
            task_kind="governance")
        state.pop("cognition_policy", None)
        state["cognition_schedule"] = {
            "policy_snapshots": {"1": {"value": "off", "at": G.now_iso(),
                                       "auto_execution": False}},
        }
        self.save(state)
        before = json.dumps(self.state(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(SCH.task_policy_value(self.state()), "off")
        view = G.cognition_policy_view(self.state())
        self.assertEqual(view["value"], "off")
        self.assertEqual(view["origin"], "legacy-snapshot")
        # 纯读取不写状态：历史任务不被迁移或补字段。
        self.run_cli(["cognition-policy", "--state", self.state_path])
        after = json.dumps(self.state(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(before, after)
        self.assertNotIn("cognition_policy", self.state())

    def test_c9_history_and_audit_records_remain_readable(self):
        state = G.start_state(
            self.state_path, goal="历史审计", contract=CONTRACT,
            risk="medium", task_id="legacy-audit", workspace=self.dir,
            task_kind="governance")
        state.pop("cognition_policy", None)
        state["sol_calls"] = [{"call_id": "SClegacy", "mode": "CONSULT",
                               "status": "completed",
                               "completed_at": G.now_iso()}]
        state["cognition_schedule"] = {"obligations": [{
            "obligation_id": "SO1", "family": "A", "event": "startup",
            "stage": "understanding", "mode": "CONSULT", "level": "recommended",
            "status": "due", "window": "open", "cycle": 1,
            "opened_at": G.now_iso(), "opened_by": "start", "task_id": "legacy-audit",
        }]}
        self.save(state)
        code, out, _err = self.run_cli([
            "cognition-audit", "--state", self.state_path, "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["open"], 1)
        # 历史状态里的 `sol_calls` 仍然只读可见，不被迁移成新字段。
        self.assertEqual(len(self.state().get("sol_calls") or []), 1)
        self.assertNotIn("cognition_policy", self.state())


if __name__ == "__main__":
    unittest.main()

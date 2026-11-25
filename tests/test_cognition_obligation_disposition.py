#!/usr/bin/env python3
"""SOL 调度义务的终态处置与收口周期隔离回归。

覆盖 v9.7.1 的最小改进（P3）：

1. 每个义务在窗口关闭后都有一个可复核的终态处置：completed / covered /
   skipped / failed / not-executed / unsettled，理由来自已持久化的事实。
2. due 不等于强制调用：策略快照只记录“当时是什么策略”，`off` 时不发起请求；
   没有快照的历史义务一律记 unknown，不用当前配置倒推。
3. 条件成立但未能调度（例如 SOL 锁被占用）要留痕（次数与真实原因），不静默
   清除、也不改写成漏调或失败。
4. 处置不改 missed 口径、不改 coverage 分子分母、不阻止正常收口。
5. 授权 reopen 产生新周期：新义务带新周期编号，旧周期的窗口不被新周期的收口
   判断追溯性地“覆盖”。
6. 历史义务的处置可以只读推导，不回写历史状态。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import v6_support as F

G = F.G
import cognition_schedule as SCH  # noqa: E402  v6_support 已把 scripts/ 加入 sys.path
import copilot_dispatch as CD  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""


class DispositionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(
            self.state_path, goal="交付可复核结果", contract=CONTRACT,
            risk="medium", task_id="obligation-disposition", workspace=self.dir,
            task_kind="standard")

    # ── 构造 ────────────────────────────────────────────────────────────
    def state(self) -> dict:
        return G.load_state(self.state_path)

    def save(self, state: dict) -> None:
        G.save_state(self.state_path, state)

    def add(self, **fields) -> dict:
        """插入一条义务（默认：打开、due、recommended 的 PLAN 窗口）。"""
        state = self.state()
        schedule = SCH.ensure_schedule(state)
        row = {
            "schema": SCH.OBLIGATION_SCHEMA,
            "obligation_id": "",
            "key": "k",
            "task_id": state["task_id"],
            "contract_sha256": state["contract_sha256"],
            "family": "B", "mode": "PLAN", "stage": "planning",
            "event": "plan", "instance": "i1", "level": "recommended",
            "question": "方案是否成立", "reason": "方案类任务",
            "opened_at": "2026-11-24T00:00:00+08:00",
            "window": "open", "closed_at": "", "closed_reason": "",
            "status": "due", "attempts": 0, "source": "", "call_id": "",
            "reuse_of": "", "skip_reason": "", "skip_by": "", "failure": "",
            "started_at": "", "finished_at": "",
        }
        row.update(fields)
        row["obligation_id"] = (row["obligation_id"]
                               or f"SO{len(schedule['obligations']) + 1}")
        # 模拟生产路径：sync 物化义务时会把“打开这一刻生效的策略”固化到义务上。
        policy = SCH.policy_snapshot(schedule, SCH.current_cycle(state))
        if isinstance(policy.get("value"), str) and policy["value"]:
            row["policy_at_open"] = {
                "value": policy["value"],
                "auto_execution": policy.get("auto_execution"),
                "at": policy.get("at", ""),
            }
        schedule["obligations"].append(row)
        self.save(state)
        return dict(row)

    def sync(self, *, advice: dict | None = None, closed: bool = False,
             policy: str = "", auto_execution: bool | None = None) -> list[str]:
        state = self.state()
        changes = SCH.sync(SCH.ensure_schedule(state), state,
                          advice or {"action": "none"},
                          now="2026-11-24T01:00:00+08:00", closed=closed,
                          policy=policy, auto_execution=auto_execution)
        self.save(state)
        return changes

    def rows(self) -> list[dict]:
        return [item for item in SCH.obligations(self.state())
                if isinstance(item, dict)]

    def row(self, obligation_id: str = "SO1") -> dict:
        return [item for item in self.rows()
                if item["obligation_id"] == obligation_id][0]

    def reopen(self) -> None:
        state = self.state()
        state["closure"].setdefault("reopen_history", []).append(
            {"at": "2026-11-24T02:00:00+08:00", "reason": "用户授权重开",
             "authorized_by": "user"})
        self.save(state)

    def close_all(self) -> None:
        """让 G1–G3 与最终验证成立：条件核销 + 工作项终态（无工作项时只核销条件）。"""
        state = self.state()
        ids = []
        path = os.path.join(self.dir, "result.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("结果\n")
        eid = G.next_evidence_id(state)
        G.register_evidence(state, eid, file_path=path, note="可复核结果")
        self.save(state)
        ids.append(eid)
        for action in state.get("actions") or []:
            G.record_action(self.state_path, action["id"], status="done",
                            result="完成", evidence_ids=ids)
        state = self.state()
        for criterion in state["contract"]["criteria"]:
            G.record_criterion(self.state_path, criterion["criterion_id"],
                               "met", evidence_ids=ids)


class NotExecutedTests(DispositionBase):
    def test_due_obligation_at_close_is_not_executed(self):
        self.add(family="B", mode="PLAN")
        self.sync(closed=True)
        row = self.row()
        # 状态与漏调口径不变：处置只描述“关闭时仍未执行”，不改 status。
        self.assertEqual(row["status"], "due")
        self.assertEqual(row["closed_reason"], "task-closed")
        self.assertTrue(SCH.is_missed(row))
        self.assertEqual(row["disposition"], "not-executed")
        self.assertIn("窗口关闭（task-closed）时仍未执行", row["disposition_reason"])
        # 没有当时的策略快照：记 unknown，不用当前配置倒推。
        self.assertIn("unknown", row["disposition_reason"])
        summary = SCH.summary(self.state())
        self.assertEqual(summary["missed"], 1)
        self.assertEqual(summary["dispositions"]["not-executed"], 1)

    def test_policy_snapshot_explains_policy_off_without_calling(self):
        # 生产路径：先记录当时策略，再在同一/后续同步里物化义务。
        changes = self.sync(policy="off", auto_execution=False)
        self.assertTrue(any("off" in item for item in changes), changes)
        self.add(family="B", mode="PLAN")
        # due 不等于强制调用：策略 off 时没有任何 SOL 调用记录。
        self.assertEqual(self.state().get("cognition_calls") or [], [])
        self.assertEqual(self.row()["status"], "due")
        self.sync(closed=True)
        row = self.row()
        self.assertEqual(row["disposition"], "not-executed")
        self.assertIn("当时调度策略=off", row["disposition_reason"])
        self.assertNotIn("unknown", row["disposition_reason"])
        self.assertEqual(SCH.summary(self.state())["policy"]["value"], "off")

    def test_policy_at_open_survives_mid_cycle_change(self):
        advice = {"action": "plan", "family": "B", "event": "plan",
                  "instance": "i1", "mode": "PLAN", "stage": "planning",
                  "level": "recommended", "question": "方案是否成立",
                  "reason": "方案类任务"}
        self.sync(advice=advice, policy="advise", auto_execution=False)
        # 窗口打开后策略改为 auto：不得把周期内最新策略当成“当时”策略。
        self.sync(policy="auto", auto_execution=True)
        self.sync(closed=True)
        row = self.row()
        self.assertEqual(row["disposition"], "not-executed")
        self.assertIn("当时调度策略=advise", row["disposition_reason"])
        self.assertIn("周期内调度策略后改为=auto", row["disposition_reason"])

    def test_legacy_obligation_uses_snapshot_history_at_open(self):
        """历史义务没有 policy_at_open 时，按快照变更历史回推打开时点策略。"""
        state = self.state()
        schedule = SCH.ensure_schedule(state)
        schedule["policy_snapshots"] = {"1": {
            "first_seen_at": "2026-11-23T20:31:00+08:00",
            "value": "auto", "auto_execution": True,
            "at": "2026-11-23T21:16:00+08:00",
            "history": [
                {"value": "advise", "auto_execution": False,
                 "at": "2026-11-23T20:31:00+08:00"},
                {"value": "auto", "auto_execution": True,
                 "at": "2026-11-23T21:16:00+08:00"},
            ],
        }}
        self.save(state)
        row = self.add(family="A", mode="CONSULT", stage="startup",
                       event="startup", instance="s1",
                       opened_at="2026-11-23T20:32:00+08:00")
        state = self.state()
        target = SCH.find_obligation(state, obligation_id=row["obligation_id"])
        # 模拟修复前的历史义务：没有 policy_at_open，窗口已关闭但未写处置。
        target.pop("policy_at_open", None)
        target["window"] = "closed"
        target["closed_at"] = "2026-11-23T22:00:00+08:00"
        target["closed_reason"] = "task-closed"
        self.save(state)
        # 只读推导：历史义务不被补写字段。
        disposition = SCH.obligation_disposition(
            target, SCH.schedule_view(self.state()))
        self.assertFalse(disposition["stored"])
        self.assertIn("当时调度策略=advise", disposition["reason"])
        self.assertIn("周期内调度策略后改为=auto", disposition["reason"])

    def test_policy_change_is_recorded_per_cycle(self):
        self.sync(policy="advise", auto_execution=False)
        self.sync(policy="auto", auto_execution=True)
        state = self.state()
        snapshot = SCH.policy_snapshot(SCH.schedule_view(state), 1)
        self.assertEqual(snapshot["value"], "auto")
        self.assertTrue(snapshot["auto_execution"])
        self.assertEqual([row["value"] for row in snapshot["history"]],
                         ["advise", "auto"])
        # 同一策略重复记录不产生新变更，也不追加历史。
        before = len(snapshot["history"])
        self.assertEqual(self.sync(policy="auto", auto_execution=True), [])
        self.assertEqual(
            len(SCH.policy_snapshot(SCH.schedule_view(self.state()), 1)["history"]),
            before)


class CoveredAndSkippedTests(DispositionBase):
    def test_closure_judgment_marks_earlier_window_covered(self):
        self.add(family="D", mode="CONSULT", stage="stage", event="milestone",
                 instance="m1")
        self.add(family="E", mode="CONSULT", stage="closure", event="closure",
                 instance="c1", status="satisfied", window="closed",
                 closed_at="2026-11-24T00:30:00+08:00", closed_reason="executed",
                 source="executed", call_id="SCclosure")
        self.sync(closed=True)
        row = self.row("SO1")
        self.assertEqual(row["skip_by"], "closure-covered")
        self.assertEqual(row["disposition"], "covered")
        self.assertFalse(SCH.is_missed(row))
        self.assertIn("收口综合判断", row["disposition_reason"])

    def test_closure_coverage_does_not_depend_on_earlier_satisfied_window(self):
        # 生产现场的真实形状：同周期启动 CONSULT（A）先满足，收口判断（E）随后满足，
        # 规划窗口（B）仍开着。收口覆盖必须照样成立——否则 drive 会在收口前多跑
        # 一次 B 调用（这是本任务实跑中观察到的缺陷）。
        self.add(family="A", mode="CONSULT", stage="understanding",
                 event="startup", instance="start", status="satisfied",
                 window="closed", closed_at="2026-11-24T00:10:00+08:00",
                 closed_reason="executed", source="executed", call_id="SCstart")
        self.add(family="B", mode="PLAN", instance="plan-1")
        self.add(family="E", mode="CONSULT", stage="closure", event="closure",
                 instance="c1", status="satisfied", window="closed",
                 closed_at="2026-11-24T00:30:00+08:00", closed_reason="executed",
                 source="executed", call_id="SCclosure")
        self.sync(closed=True)
        row = self.row("SO2")
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["skip_by"], "closure-covered")
        self.assertEqual(row["disposition"], "covered")
        self.assertFalse(SCH.is_missed(row))

    def test_contract_amendment_marks_earlier_window_skipped(self):
        self.add(family="B", mode="PLAN", contract_sha256="0" * 64)
        self.sync()
        row = self.row()
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["skip_by"], "contract-amended")
        self.assertEqual(row["disposition"], "skipped")
        self.assertIn("合同已修订", row["disposition_reason"])
        self.assertFalse(SCH.is_missed(row))

    def test_superseded_window_records_reason(self):
        state = self.state()
        schedule = SCH.ensure_schedule(state)
        self.save(state)
        self.add(family="D", mode="CONSULT", stage="stage", event="milestone",
                 instance="old")
        self.sync(advice={"action": "consult", "family": "D",
                          "event": "milestone", "instance": "new",
                          "mode": "CONSULT", "stage": "stage",
                          "level": "recommended"})
        old = self.row("SO1")
        self.assertEqual(old["status"], "skipped")
        self.assertEqual(old["disposition"], "skipped")
        self.assertIn("取代", old["disposition_reason"])
        self.assertEqual(self.row("SO2")["window"], "open")


class CompletedAndFailedTests(DispositionBase):
    def test_executed_obligation_is_completed(self):
        self.add(family="A", mode="CONSULT", stage="understanding",
                 event="startup", instance="s1")
        state = self.state()
        G.resolve_cognition_obligation(state, "SO1", status="satisfied",
                                 source="executed", call_id="SCstartup")
        self.save(state)
        row = self.row()
        self.assertEqual(row["disposition"], "completed")
        self.assertIn("SCstartup", row["disposition_reason"])
        self.assertTrue(SCH.obligation_disposition(row)["stored"])

    def test_reused_obligation_is_completed_with_reuse_reason(self):
        self.add(family="A", mode="CONSULT", stage="understanding",
                 event="startup", instance="s1")
        self.sync(advice={"action": "reuse", "covered": {"CONSULT": True},
                          "call_id": "SCprevious"})
        row = self.row()
        self.assertEqual(row["status"], "satisfied")
        self.assertEqual(row["source"], "reuse")
        self.assertEqual(row["disposition"], "completed")
        self.assertIn("复用", row["disposition_reason"])
        self.assertIn("SCprevious", row["disposition_reason"])

    def test_failed_call_is_failed_disposition(self):
        self.add(family="B", mode="PLAN")
        G.record_cognition_call_failure(
            self.state_path, mode="PLAN", error_code="http_429",
            detail="上游限流", obligation_id="SO1", stage="planning",
            request_attempted=True, send_observed="response_received")
        row = self.row()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["disposition"], "failed")
        self.assertIn("http_429", row["disposition_reason"])
        # 真实失败不是漏调：漏调只统计“关闭时仍未执行”。
        self.assertFalse(SCH.is_missed(row))


class DeferralTests(DispositionBase):
    def test_deferral_keeps_obligation_due_and_records_reason(self):
        self.add(family="B", mode="PLAN")
        state = self.state()
        SCH.note_deferral(SCH.find_obligation(state, obligation_id="SO1"),
                         now="2026-11-24T01:00:00+08:00",
                         reason="另一个进程正在调用 SOL")
        self.save(state)
        row = self.row()
        self.assertEqual(row["status"], "due")
        self.assertEqual(row["deferrals"], 1)
        self.assertEqual(row["last_deferral_reason"], "另一个进程正在调用 SOL")
        self.assertFalse(row.get("disposition"))
        self.sync(closed=True)
        row = self.row()
        self.assertEqual(row["disposition"], "not-executed")
        self.assertIn("1 次未能调度", row["disposition_reason"])
        self.assertIn("另一个进程正在调用 SOL", row["disposition_reason"])

    def test_lock_busy_execution_records_deferral_not_failure(self):
        row = self.add(family="B", mode="PLAN")
        with mock.patch.object(CD, "_run_cognition_channel",
                               side_effect=G.LockBusyError("SOL 锁被占用")):
            self.assertEqual(CD._execute_obligation(self.state_path, row), 0)
        saved = self.row()
        self.assertEqual(saved["status"], "due")
        self.assertEqual(saved["deferrals"], 1)
        self.assertIn("锁被占用", saved["last_deferral_reason"])
        self.assertFalse(saved["failure"])
        self.assertFalse(saved.get("disposition"))


class IdempotencyTests(DispositionBase):
    def test_disposition_is_never_overwritten(self):
        self.add(family="B", mode="PLAN")
        self.sync(closed=True)
        first = dict(self.row())
        self.assertEqual(first["disposition"], "not-executed")
        # 再次同步（含收口路径）与再次结算都不改写已落定的处置。
        self.sync(closed=True)
        state = self.state()
        SCH.settle(SCH.find_obligation(state, obligation_id="SO1"),
                  status="satisfied", now="2026-11-24T03:00:00+08:00",
                  reason="late", schedule=SCH.schedule_view(state))
        self.save(state)
        row = self.row()
        self.assertEqual(row["disposition"], "not-executed")
        self.assertEqual(row["disposition_at"], first["disposition_at"])
        self.assertEqual(row["disposition_reason"], first["disposition_reason"])
        # 迟到结算只更新 status/window 事实，不追认成“已完成”。
        self.assertEqual(row["status"], "satisfied")


class CycleIsolationTests(DispositionBase):
    def test_new_obligation_carries_current_cycle(self):
        self.add(family="B", mode="PLAN", instance="i1", cycle=1)
        self.reopen()
        self.sync(advice={"action": "plan", "family": "B", "event": "plan",
                          "instance": "i2", "mode": "PLAN",
                          "stage": "planning", "level": "recommended"})
        rows = {item["obligation_id"]: item for item in self.rows()}
        self.assertEqual(rows["SO1"]["cycle"], 1)
        self.assertEqual(rows["SO2"]["cycle"], 2)
        summary = SCH.summary(self.state())
        self.assertEqual(summary["cycle"], 2)
        self.assertEqual([item["cycle"] for item in summary["by_cycle"]], [1, 2])
        # 旧周期的窗口以“被更新窗口取代”终止，理由留在自己这一行。
        self.assertEqual(rows["SO1"]["status"], "skipped")
        self.assertEqual(rows["SO1"]["skip_by"], "superseded-by-newer")

    def test_closure_judgment_does_not_cover_previous_cycle(self):
        # 只留一个打开窗口在上一周期，避免“同窗口去重”干扰本测试的判定对象。
        self.add(family="B", mode="PLAN", instance="old-cycle", cycle=1)
        self.add(family="D", mode="CONSULT", stage="stage", event="milestone",
                 instance="new-cycle", cycle=2)
        self.add(family="E", mode="CONSULT", stage="closure", event="closure",
                 instance="c2", cycle=2, status="satisfied", window="closed",
                 closed_at="2026-11-24T02:30:00+08:00",
                 closed_reason="executed", source="executed",
                 call_id="SCclosure2")
        self.sync(advice={"action": "consult", "family": "C",
                          "event": "decision-point", "instance": "d1",
                          "mode": "CONSULT", "stage": "development",
                          "level": "recommended"})
        rows = {item["obligation_id"]: item for item in self.rows()}
        # 新周期的收口判断只覆盖本周期的窗口。
        self.assertEqual(rows["SO2"]["skip_by"], "closure-covered")
        self.assertEqual(rows["SO2"]["disposition"], "covered")
        # 上一周期的窗口保持原本的未执行事实，不被追溯覆盖。
        self.assertEqual(rows["SO1"]["window"], "open")
        self.assertEqual(rows["SO1"]["status"], "due")
        self.assertFalse(rows["SO1"].get("disposition"))

    def test_cycle_counts_keep_missed_and_coverage_semantics(self):
        self.add(family="B", mode="PLAN", instance="i1", cycle=1)
        self.reopen()
        self.add(family="B", mode="PLAN", instance="i2", cycle=2)
        before = SCH.task_stats(self.state())
        self.sync(closed=True)
        stats = SCH.task_stats(self.state())
        self.assertEqual(stats["cycle"], 2)
        self.assertEqual([item["cycle"] for item in stats["by_cycle"]], [1, 2])
        rows = {item["obligation_id"]: item for item in self.rows()}
        # 旧周期的未执行窗口按“被更新窗口取代”终止（不计漏调），新周期按“未执行”
        # 终止：两条都带可查理由，各自留在自己的周期里。
        self.assertEqual(rows["SO1"]["disposition"], "skipped")
        self.assertEqual(rows["SO1"]["cycle"], 1)
        self.assertEqual(rows["SO2"]["disposition"], "not-executed")
        self.assertEqual(rows["SO2"]["cycle"], 2)
        self.assertEqual(stats["dispositions"]["skipped"], 1)
        self.assertEqual(stats["dispositions"]["not-executed"], 1)
        # 分母与分子不因周期拆分或处置记录而变化（missed/skipped 是窗口关闭后的
        # 既有事实，本就应在收口时变化）。
        for key in ("startup", "plan", "closure", "events"):
            self.assertEqual(stats["coverage"][key]["denominator"],
                             before["coverage"][key]["denominator"], key)
            self.assertEqual(stats["coverage"][key]["numerator"],
                             before["coverage"][key]["numerator"], key)
        self.assertEqual(stats["obligations"], before["obligations"])
        self.assertEqual(SCH.summary(self.state())["missed"], 1)


    def test_late_settlement_of_old_cycle_does_not_cover_new_cycle(self):
        # 周期 1 的启动窗口保持 due，授权重开进入周期 2，再打开一个新的决策窗口。
        self.add(family="A", mode="CONSULT", stage="understanding",
                 event="startup", instance="start", level="recommended")
        self.reopen()
        self.sync(advice={"action": "consult", "family": "C",
                          "event": "decision-point", "instance": "d1",
                          "mode": "CONSULT", "stage": "development",
                          "level": "recommended"})
        rows = {item["obligation_id"]: item for item in self.rows()}
        self.assertEqual(SCH.obligation_cycle(rows["SO1"]), 1)
        self.assertEqual(rows["SO2"]["cycle"], 2)

        # 迟到的旧周期事件：重开之后才把周期 1 的窗口结算为已执行。
        state = self.state()
        G.resolve_cognition_obligation(state, "SO1", status="satisfied",
                                 source="executed", call_id="SClate")
        self.save(state)

        rows = {item["obligation_id"]: item for item in self.rows()}
        self.assertEqual(rows["SO1"]["status"], "satisfied")
        self.assertEqual(SCH.obligation_cycle(rows["SO1"]), 1)
        self.assertEqual(rows["SO1"]["disposition"], "completed")
        # 新周期的 due 不被旧周期事件抵扣，也不被追溯写处置。
        self.assertEqual(rows["SO2"]["status"], "due")
        self.assertFalse(rows["SO2"].get("disposition"))
        # 统计按周期归属：已执行只计在周期 1，周期 2 仍是 0。
        stats = SCH.task_stats(self.state())
        by_cycle = {item["cycle"]: item for item in stats["by_cycle"]}
        self.assertEqual(by_cycle[1]["dispositions"]["completed"], 1)
        self.assertEqual(by_cycle[2]["dispositions"]["completed"], 0)
        self.assertEqual(stats["dispositions"]["completed"], 1)


    def test_same_key_obligation_in_new_cycle_is_not_settled_by_old_event(self):
        # 新旧周期使用同一义务键（family/event/instance 相同）：迟到的旧周期事件
        # 只能结算旧周期那一行，不能关掉新周期同键的窗口。
        self.add(family="C", mode="CONSULT", stage="development",
                 event="decision-point", instance="same-key")
        self.reopen()
        self.add(family="C", mode="CONSULT", stage="development",
                 event="decision-point", instance="same-key", cycle=2)
        state = self.state()
        G.resolve_cognition_obligation(state, "SO1", status="satisfied",
                                 source="executed", call_id="SClate-same-key")
        self.save(state)
        rows = {item["obligation_id"]: item for item in self.rows()}
        self.assertEqual(rows["SO1"]["status"], "satisfied")
        self.assertEqual(rows["SO1"]["disposition"], "completed")
        self.assertEqual(SCH.obligation_cycle(rows["SO1"]), 1)
        self.assertEqual(rows["SO2"]["status"], "due")
        self.assertFalse(rows["SO2"].get("disposition"))
        # 新周期自己的事件到达后，新周期窗口按规则正常结算。
        state = self.state()
        G.resolve_cognition_obligation(state, "SO2", status="satisfied",
                                 source="executed", call_id="SCnew-cycle")
        self.save(state)
        rows = {item["obligation_id"]: item for item in self.rows()}
        self.assertEqual(rows["SO2"]["disposition"], "completed")
        stats = SCH.task_stats(self.state())
        by_cycle = {item["cycle"]: item for item in stats["by_cycle"]}
        self.assertEqual(by_cycle[1]["dispositions"]["completed"], 1)
        self.assertEqual(by_cycle[2]["dispositions"]["completed"], 1)


class ReadOnlyHistoryTests(DispositionBase):
    def test_historical_obligation_disposition_is_derived_not_written(self):
        state = self.state()
        schedule = SCH.ensure_schedule(state)
        legacy = {
            "obligation_id": "SO9", "family": "C", "event": "decision-point",
            "instance": "legacy", "mode": "CONSULT", "stage": "development",
            "level": "recommended", "status": "due", "window": "closed",
            "closed_at": "2026-11-20T00:00:00+08:00",
            "closed_reason": "task-closed",
            "opened_at": "2026-11-19T00:00:00+08:00",
        }
        schedule["obligations"].append(legacy)
        disposition = SCH.obligation_disposition(legacy, schedule)
        self.assertEqual(disposition["kind"], "not-executed")
        self.assertFalse(disposition["stored"])
        self.assertIn("unknown", disposition["reason"])
        # 只读推导：历史义务不被补写字段。
        self.assertNotIn("disposition", legacy)
        self.assertEqual(SCH.summary(state)["dispositions"]["not-executed"], 1)
        self.assertNotIn("disposition", legacy)


class ClosureNotBlockedTests(DispositionBase):
    def test_close_task_records_dispositions_without_gating(self):
        # 生产路径：策略先于义务打开被记录，收口时同一策略不覆盖打开时点事实。
        self.sync(policy="advise", auto_execution=False)
        self.add(family="D", mode="CONSULT", stage="stage",
                 event="milestone", instance="m1")
        self.close_all()
        closure = G.close_task(self.state_path, by="zcode-native",
                               cognition_policy="advise", cognition_auto=False)
        self.assertTrue(closure["closed"])
        self.assertEqual(closure["verdict"], "pass")
        self.assertTrue(closure["gate"]["passed"])
        # 收口快照同时保留未执行事实与终态处置，且仍是非阻断提醒。
        self.assertEqual(closure["cognition_schedule"]["missed"], 1)
        self.assertEqual(closure["cognition_schedule"]["dispositions"]["not-executed"], 1)
        row = self.row()
        self.assertEqual(row["disposition"], "not-executed")
        self.assertIn("当时调度策略=advise", row["disposition_reason"])
        self.assertTrue(G.cognition_schedule_warnings(self.state()))
        self.assertTrue(G.closure_check(self.state())["passed"])


if __name__ == "__main__":
    unittest.main()

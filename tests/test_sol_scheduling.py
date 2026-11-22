#!/usr/bin/env python3
"""v9.6.0 SOL 生命周期主动调度回归：义务、窗口、有界执行与覆盖统计。

覆盖完成条件 C2–C10 的可复核部分：

1. 五阶段真的进入调度路径：生命周期命令把建议物化为义务，`--sol auto` 有界执行
   1 条、`advise` 只给可直接执行的命令、`off` 只记录；读命令永不执行。
2. PLAN 准入不依赖工作项数量：单工作项方案类任务与声明触发都能进入 PLAN。
3. 六类动态事件各有可归因判据；普通证据追加不触发。
4. medium/high 里程碑与收口 CONSULT 接在真实生命周期上（drive/closure 入口），
   收口顺序为 G1–G3 → 收口判断 → 收口。
5. 复用不重复付费、合同修订使旧义务失效、失败只记录不阻断、并发锁有界。
6. 统计区分建议/执行/复用/跳过/失败/漏调，分母显式且不靠缩小分母制造覆盖率。
7. G1–G3、REVIEW 授权边界与"SOL 不是门禁"不变。

所有 SOL 通道都用替身；本文件不发起任何真实模型请求。
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
import review_policy as RP  # noqa: E402  v6_support 已把 scripts/ 加入 sys.path
import sol_review as SR  # noqa: E402
import sol_schedule as SS  # noqa: E402
import copilot_dispatch as CD  # noqa: E402
import copilot_auto as CA  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""


def think_result(mode: str = "CONSULT") -> dict:
    """SOL CONSULT/PLAN 成功的结构化结果（通道已替换，不联网）。"""
    return {
        "schema": SR.THINK_SCHEMA, "mode": mode,
        "model": SR.MODEL, "model_resolved": SR.MODEL,
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


class SchedulingBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")

    # ── 状态构造 ─────────────────────────────────────────────────────────
    def start(self, *, risk: str = "medium", kind: str = "standard",
              triggers: list[str] | None = None) -> None:
        G.start_state(
            self.state_path, goal="交付可复核结果", contract=CONTRACT,
            risk=risk, task_id="sol-schedule", workspace=self.dir,
            task_kind=kind, triggers=triggers,
            test_policy={"required": False} if risk == "high" else None)

    def state(self) -> dict:
        return G.load_state(self.state_path)

    def _state_bytes(self) -> bytes:
        with open(self.state_path, "rb") as fh:
            return fh.read()

    def save(self, state: dict) -> None:
        G.save_state(self.state_path, state)

    def plan(self, actions: int, *, deps: bool = False) -> None:
        items = []
        for index in range(1, actions + 1):
            item = {"id": f"A{index}", "title": f"工作{index}",
                    "goal": f"目标{index}", "expected_evidence": "证据"}
            if deps and index > 1:
                item["depends_on"] = [f"A{index - 1}"]
            items.append(item)
        G.plan_state(self.state_path, {"summary": "计划", "actions": items})

    def evidence(self, count: int = 1, *, modules: int = 1) -> list[str]:
        state = self.state()
        ids = []
        for index in range(count):
            folder = os.path.join(self.dir, f"mod{index % max(modules, 1) + 1}")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"e{index}.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"证据{index}\n")
            eid = G.next_evidence_id(state)
            G.register_evidence(state, eid, file_path=path, note="测试证据")
            ids.append(eid)
        self.save(state)
        return ids

    def done(self, count: int) -> None:
        ids = self.evidence()
        state = self.state()
        for action in (state.get("actions") or [])[:count]:
            G.record_action(self.state_path, action["id"], status="done",
                            result="完成", evidence_ids=[ids[0]])

    def close_all(self) -> None:
        ids = self.evidence()
        state = self.state()
        for action in state.get("actions") or []:
            G.record_action(self.state_path, action["id"], status="done",
                            result="完成", evidence_ids=[ids[0]])
        state = self.state()
        for criterion in state["contract"]["criteria"]:
            G.record_criterion(self.state_path, criterion["criterion_id"],
                               "met", evidence_ids=[ids[0]])

    def record(self, mode: str, *, signals: list[str] | None = None,
               stage: str = "", trigger: str = "",
               status: str = "completed") -> dict:
        state = self.state()
        call = {
            "call_id": f"SC{mode.lower()}{len(state.get('sol_calls') or [])}",
            "mode": mode, "status": status, "stage": stage,
            "trigger": trigger, "question": "本次需要判断的事项",
            "contract_sha256": state.get("contract_sha256") or "",
            "materials_sha256": "m" * 64, "summary": "建议",
            "progress_digest": (RP.sol_progress_key(state)
                                if stage == "stage" else ""),
            "completed_at": G.now_iso(),
        }
        if signals is not None:
            call["complexity"] = "medium"
            call["complexity_signals"] = list(signals)
        return G.record_sol_call(self.state_path, call, mode=mode,
                                materials_sha256="m" * 64, trigger=trigger)

    def accept(self, *, status: str = "accepted",
               rationale: str = "ZCode 独立判断：结论与证据一致") -> dict:
        """登记 ZCode 对最近一次收口 CONSULT 的采纳判断。

        顺序是「最终验证 → 收口 CONSULT → ZCode 验收 → G1–G3 收口」：这一步
        缺省会被拒绝收口，因为收口后状态为 CLOSED，再登记就不再被允许。
        """
        calls = [item for item in self.state().get("sol_calls") or []
                 if isinstance(item, dict)
                 and str(item.get("stage") or "") == "closure"
                 and str(item.get("status") or "") == "completed"]
        self.assertTrue(calls, "没有可验收的收口 CONSULT")
        return G.record_sol_acceptance(
            self.state_path, calls[-1]["call_id"], status=status,
            rationale=rationale)

    def advice(self, **kwargs) -> dict:
        return G.sol_advice_summary(self.state(), **kwargs)

    def sync(self, **kwargs) -> tuple[dict, dict]:
        state = self.state()
        synced = G.sync_sol_schedule(state, advice=self.advice(**kwargs))
        if synced["changes"]:
            self.save(state)
        return self.state(), synced

    def obligations(self, family: str = "") -> list[dict]:
        rows = SS.obligations(self.state())
        return [item for item in rows
                if not family or str(item.get("family")) == family]

    # ── 调度步与 SOL 替身 ────────────────────────────────────────────────
    @staticmethod
    def namespace(policy: str = "auto") -> argparse.Namespace:
        return argparse.Namespace(state="", sol=policy)

    def step(self, policy: str = "auto", **kwargs) -> tuple[dict, str, str]:
        args = self.namespace(policy)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = CD._sol_schedule_step(self.state_path, args, **kwargs)
        return result, out.getvalue(), err.getvalue()

    @contextlib.contextmanager
    def sol_channel(self, *, error: Exception | None = None,
                    result: dict | None = None):
        """显式允许自动执行，并用替身替换真实 SOL 通道（一次也不联网）。"""
        calls = {"count": 0}

        def fake(*_args, **_kwargs):
            calls["count"] += 1
            if error is not None:
                raise error
            return dict(result or think_result())

        with mock.patch.dict(os.environ, {"COPILOT_SOL_AUTO": "on"}), \
                mock.patch.object(SR, "run_consult", side_effect=fake), \
                mock.patch.object(SR, "run_plan", side_effect=fake):
            yield calls


# ── A：启动 CONSULT 与有界执行 ────────────────────────────────────────────
class StartupTests(SchedulingBase):
    def test_trivial_task_registers_no_obligation(self):
        self.start(risk="low", kind="trivial")
        self.plan(1)
        self.evidence()
        state, _ = self.sync()
        self.assertEqual(SS.obligations(state), [])
        stats = SS.task_stats(state)
        self.assertFalse(stats["eligible"])
        self.assertFalse(stats["not_registered"])
        self.assertEqual(stats["coverage"]["startup"]["denominator"], 0)
        self.assertIsNone(stats["coverage"]["startup"]["rate"])

    def test_standard_startup_consult_is_executed_by_lifecycle_command(self):
        self.start()
        state, _ = self.sync()
        rows = self.obligations("A")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "due")
        self.assertEqual(rows[0]["event"], "startup")
        with self.sol_channel() as calls:
            result, _out, _err = self.step("auto")
        self.assertEqual(calls["count"], 1)
        self.assertEqual(result["executed"], rows[0]["obligation_id"])
        state = self.state()
        completed = [item for item in state["sol_calls"]
                     if item.get("status") == "completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["mode"], "CONSULT")
        self.assertEqual(completed[0]["stage"], "understanding")
        obligation = SS.obligations(state)[0]
        self.assertEqual(obligation["status"], "satisfied")
        self.assertEqual(obligation["source"], "executed")
        self.assertEqual(obligation["call_id"], completed[0]["call_id"])
        # 结算即关闭窗口：已执行的义务不能停在 open 被算成“收口前仍未执行”。
        self.assertEqual(obligation["window"], "closed")
        self.assertEqual(SS.summary(state)["pending"], 0)
        coverage = SS.task_stats(state)["coverage"]["startup"]
        self.assertEqual((coverage["numerator"], coverage["denominator"]),
                         (1, 1))
        self.assertEqual(coverage["rate"], 1.0)

    def _start_cli(self, policy: str) -> tuple[int, str]:
        criteria = os.path.join(self.dir, "criteria.json")
        with open(criteria, "w", encoding="utf-8") as fh:
            json.dump(["产出可复核的结果文件"], fh, ensure_ascii=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = CD.main(["start", "--state", self.state_path,
                            "--goal", "交付可复核结果",
                            "--criteria-file", criteria,
                            "--task-kind", "feature", "--risk", "medium",
                            "--workspace", self.dir, "--sol", policy])
        return code, out.getvalue()

    def test_start_command_itself_executes_startup_consult(self):
        """真实入口 `start` 自己兑现 A 窗口：不是只打印“下一步”。"""
        with self.sol_channel() as calls:
            code, output = self._start_cli("auto")
        self.assertEqual(code, 0)
        self.assertEqual(calls["count"], 1)
        self.assertIn("已执行义务", output)
        state = self.state()
        self.assertEqual(len(state["sol_calls"]), 1)
        self.assertEqual(state["sol_calls"][0]["mode"], "CONSULT")

    def test_start_command_advise_records_obligation_without_calling(self):
        with self.sol_channel() as calls:
            code, _output = self._start_cli("advise")
        self.assertEqual(code, 0)
        self.assertEqual(calls["count"], 0)
        rows = self.obligations("A")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "due")
        self.assertEqual(rows[0]["window"], "open")

    def test_advise_policy_gives_runnable_command_without_calling(self):
        self.start()
        self.sync()
        result, out, _err = self.step("advise")
        self.assertEqual(result["executed"], "")
        state = self.state()
        self.assertEqual(state.get("sol_calls") or [], [])
        self.assertEqual(SS.obligations(state)[0]["status"], "due")
        self.assertIn("sol-consult", result["command"])
        self.assertIn("--obligation", result["command"])
        self.assertNotIn("<", result["command"])  # 没有占位符
        self.assertIn("可直接执行", out)

    def test_off_policy_records_obligation_silently(self):
        self.start()
        result, out, _err = self.step("off")
        self.assertEqual(result["executed"], "")
        self.assertEqual(result["command"], "")
        self.assertNotIn("可直接执行", out)
        self.assertEqual(SS.obligations(self.state())[0]["status"], "due")

    def test_auto_execution_is_suppressed_in_test_process_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COPILOT_SOL_AUTO", None)
            self.assertFalse(CD._auto_execution_allowed())

    def test_same_window_never_creates_two_obligations(self):
        self.start()
        self.sync()
        self.sync()
        self.sync()
        rows = self.obligations("A")
        self.assertEqual(len(rows), 1)
        self.assertEqual(SS.summary(self.state())["total"], 1)


# ── B：PLAN 准入不依赖工作项数量 ──────────────────────────────────────────
class PlanAdmissionTests(SchedulingBase):
    def test_single_work_item_plan_kind_still_needs_plan(self):
        self.start(kind="architecture")
        self.record("CONSULT")
        self.plan(1)
        self.evidence()
        admission = RP.plan_admission(self.state())
        self.assertTrue(admission["required"])
        advice = self.advice()
        self.assertEqual(advice["action"], "plan")
        self.assertEqual(advice["family"], "B")
        self.assertEqual(advice["event"], "plan-needed")

    def test_single_work_item_with_declared_trigger_needs_plan(self):
        self.start(kind="feature", triggers=["architecture"])
        self.record("CONSULT")
        self.plan(1)
        self.evidence()
        self.assertTrue(RP.plan_admission(self.state())["required"])
        self.assertEqual(self.advice()["action"], "plan")

    def test_plan_obligation_is_settled_by_real_record(self):
        self.start(kind="architecture")
        self.record("CONSULT")
        self.plan(1)
        self.evidence()
        state, _ = self.sync()
        rows = self.obligations("B")
        self.assertEqual(len(rows), 1)
        with self.sol_channel() as calls:
            result, _out, _err = self.step("auto")
        self.assertEqual(calls["count"], 1)
        self.assertEqual(result["executed"], rows[0]["obligation_id"])
        state = self.state()
        plan_calls = [item for item in state["sol_calls"]
                      if item.get("mode") == "PLAN"
                      and item.get("status") == "completed"]
        self.assertEqual(len(plan_calls), 1)
        self.assertEqual(self.obligations("B")[0]["status"], "satisfied")

    def test_no_plan_for_low_complexity_single_action(self):
        self.start(kind="feature", risk="low")
        self.record("CONSULT")
        self.plan(1)
        self.evidence()
        self.assertFalse(RP.plan_admission(self.state())["required"])


# ── C：六类动态事件 ───────────────────────────────────────────────────────
class DynamicEventTests(SchedulingBase):
    def _covered(self) -> None:
        self.start(kind="architecture")
        self.record("CONSULT")
        self.plan(2)
        self.evidence()
        self.record("PLAN")

    def test_material_evidence_change_triggers_dynamic_consult(self):
        self._covered()
        state = self.state()
        state.setdefault("finding_ledger", {}).setdefault("items", []).append({
            "finding_id": "F1", "status": "regressed", "summary": "修复后复现"})
        self.save(state)
        advice = self.advice()
        self.assertEqual(advice["action"], "consult")
        self.assertEqual(advice["family"], "C")
        self.assertEqual(advice["event"], "material-evidence-change")
        state, _ = self.sync()
        rows = self.obligations("C")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "material-evidence-change")

    def test_ordinary_evidence_addition_does_not_trigger(self):
        self._covered()
        self.evidence(count=2)
        advice = self.advice()
        self.assertNotEqual(advice["action"], "consult")
        self.assertEqual(advice["action"], "reuse")
        state, _ = self.sync()
        self.assertEqual(self.obligations("C"), [])

    def test_replan_triggers_dynamic_consult(self):
        self._covered()
        state = self.state()
        state.setdefault("actions", []).append({
            "id": "AUTO-REPLAN-1", "title": "重规划", "goal": "重规划",
            "status": "pending", "origin": "replan"})
        self.save(state)
        advice = self.advice()
        self.assertEqual(advice["family"], "C")
        self.assertEqual(advice["event"], "replan-history")

    def test_risk_escalation_triggers_dynamic_consult(self):
        self._covered()
        state = self.state()
        state["risk"] = "high"
        state["contract"]["test_policy"] = {"required": False}
        self.save(state)
        advice = self.advice()
        self.assertEqual(advice["family"], "C")
        self.assertEqual(advice["event"], "risk-escalation")

    def test_recorded_decision_triggers_decision_point(self):
        self._covered()
        G.add_decision(self.state_path, "选择方案 A：先做兼容层", source="user")
        advice = self.advice()
        self.assertEqual(advice["family"], "C")
        self.assertEqual(advice["event"], "decision-point")
        self.assertEqual(self.state()["decisions"][0]["decision_id"], "D1")

    def test_contract_amendment_triggers_scope_changed(self):
        self._covered()
        G.amend_contract(self.state_path,
                         contract=CONTRACT + "- 追加：保留旧接口兼容\n")
        advice = self.advice()
        self.assertEqual(advice["family"], "C")
        self.assertEqual(advice["event"], "scope-changed")

    def test_reopen_triggers_dynamic_consult(self):
        self._covered()
        self.close_all()
        G.close_task(self.state_path, by="zcode-native")
        G.reopen_task(self.state_path, "用户要求补充验证",
                      authorized_by="user")
        advice = self.advice()
        self.assertEqual(advice["family"], "C")
        self.assertEqual(advice["event"], "reopened")

    def test_block_creation_and_resolution_are_events(self):
        self._covered()
        state = self.state()
        state["actions"][0]["status"] = "running"
        self.save(state)
        block = G.add_block(self.state_path, "等待必要资源", "恢复后继续",
                            work_items=[state["actions"][0]["id"]])
        advice = self.advice()
        self.assertEqual(advice["event"], "blockage-change")
        state = self.state()
        snapshot = RP.sol_event_snapshot(state)
        state["sol_calls"][-1]["sol_event_snapshot"] = snapshot
        self.save(state)
        G.resolve_block(self.state_path, "资源已到位", block["block_id"])
        advice = self.advice()
        self.assertEqual(advice["event"], "blockage-change")

    def test_repeat_failure_threshold_is_one_event_per_episode(self):
        self._covered()
        state = self.state()
        action = state["actions"][0]
        action.update(status="failed", fail_streak=1)
        self.save(state)
        self.assertNotEqual(self.advice().get("event"), "repeated-work-failure")
        state = self.state()
        state["actions"][0]["fail_streak"] = 2
        self.save(state)
        advice = self.advice()
        self.assertEqual(advice["event"], "repeated-work-failure")
        snapshot = RP.sol_event_snapshot(self.state())
        state = self.state()
        state["sol_calls"][-1]["sol_event_snapshot"] = snapshot
        self.save(state)
        state = self.state()
        state["actions"][0]["fail_streak"] = 3
        self.save(state)
        self.assertEqual(self.advice().get("event"), "repeated-work-failure")


# ── D/E：里程碑与收口 ─────────────────────────────────────────────────────
class MilestoneAndClosureTests(SchedulingBase):
    def _medium_with_milestone(self) -> None:
        self.start(kind="feature", triggers=["architecture"])
        self.record("CONSULT")
        self.plan(3)
        self.evidence()
        self.record("PLAN")
        self.done(2)

    def test_medium_milestone_is_recommended_and_auto_executed(self):
        """medium 里程碑复盘是 recommended：真的进入自动执行，而不是永久挂起。"""
        self._medium_with_milestone()
        advice = self.advice()
        self.assertEqual(advice["family"], "D")
        self.assertEqual(advice["event"], "milestone")
        self.assertEqual(advice["recommended_action"], "recommended")
        state, _ = self.sync()
        rows = self.obligations("D")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["level"], "recommended")
        with self.sol_channel() as calls:
            result, _out, _err = self.step("auto")
        self.assertEqual(calls["count"], 1)  # recommended 级自动执行一次
        self.assertEqual(result["executed"], rows[0]["obligation_id"])
        row = self.obligations("D")[0]
        self.assertEqual(row["status"], "satisfied")
        self.assertEqual(row["window"], "closed")
        self.assertEqual(SS.summary(self.state())["missed"], 0)

    def test_closure_window_covers_fresh_milestone_window(self):
        """最后一批进展刚打开 D 窗口就收口：E 已满足时算合理跳过，不是漏调。"""
        self._medium_with_milestone()
        self.sync()
        state = self.state()
        schedule = SS.ensure_schedule(state)
        schedule["obligations"].append(
            {"obligation_id": "SX8", "family": "E", "event": "closure",
             "instance": "closure", "status": "satisfied", "window": "closed",
             "level": "recommended", "opened_at": "2026-11-21T20:00:01+08:00"})
        self.save(state)
        state = self.state()
        G.sync_sol_schedule(state, advice={"action": "none"}, closed=True)
        self.save(state)
        rows = [item for item in SS.obligations(self.state())
                if item["obligation_id"] == "SX8"][0]
        self.assertEqual(rows["status"], "satisfied")
        milestone = [item for item in self.obligations("D") if item["status"] != "satisfied"]
        self.assertTrue(milestone)
        self.assertEqual(milestone[0]["status"], "skipped")
        self.assertEqual(milestone[0]["skip_by"], "closure-covered")
        self.assertFalse(SS.is_missed(milestone[0]))

    def test_optional_window_at_close_is_skipped_not_missed(self):
        """历史 optional 义务收口时算合理跳过：漏调只统计应执行而未执行的窗口。"""
        self._medium_with_milestone()
        self.sync()
        state = self.state()
        schedule = SS.ensure_schedule(state)
        schedule["obligations"].append({
            "obligation_id": "SX9", "family": "C", "event": "decision-point",
            "instance": "legacy-optional", "status": "due", "window": "open",
            "opened_at": "2026-11-01T00:00:00+08:00", "level": "optional"})
        self.save(state)
        state = self.state()
        G.sync_sol_schedule(state, advice=self.advice(), closed=True)
        self.save(state)
        legacy = [item for item in SS.obligations(self.state())
                  if item["obligation_id"] == "SX9"][0]
        self.assertEqual(legacy["status"], "skipped")
        self.assertEqual(legacy["skip_by"], "not-required")
        self.assertFalse(SS.is_missed(legacy))

    def test_stage_review_is_not_repeated_for_same_progress(self):
        self._medium_with_milestone()
        digest = RP.sol_progress_key(self.state())
        self.record("CONSULT", stage="stage",
                    signals=["declared-trigger"])
        self.assertEqual(self.state()["sol_calls"][-1]["progress_digest"],
                         digest)
        self.assertNotEqual(self.advice()["family"], "D")

    def test_routine_progress_does_not_create_new_milestone_window(self):
        """工作项继续推进不会因 progress digest 改变反复新开 D 窗口。"""
        self._medium_with_milestone()
        self.sync()
        first = self.obligations("D")
        self.assertEqual(len(first), 1)
        self.done(1)
        state, _ = self.sync()
        rows = self.obligations("D")
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["obligation_id"], first[0]["obligation_id"])
        self.assertEqual(rows[0]["status"], "due")
        self.assertEqual(SS.task_stats(state)["by_family"]["D"]["missed"], 0)

    def test_reopen_starts_a_new_milestone_cycle(self):
        self._medium_with_milestone()
        self.sync()
        first = self.obligations("D")[0]
        state = self.state()
        state["closure"]["reopen_history"].append({"at": "now"})
        # The reopen event (C) has priority; record it as the new judgment
        # baseline, then ensure D gets a distinct identity for this cycle.
        baseline = RP.sol_event_snapshot(state)
        for call in state["sol_calls"]:
            call["sol_event_snapshot"] = baseline
        self.save(state)
        advice = self.advice()
        self.assertEqual(advice["family"], "D")
        self.assertNotEqual(advice["instance"], first["instance"])

    def test_legacy_duplicate_milestone_windows_are_collapsed(self):
        """历史状态里已堆积的多条打开窗口在下一次同步时收成一条。"""
        self._medium_with_milestone()
        state = self.state()
        schedule = SS.ensure_schedule(state)
        for index, opened in enumerate(("2026-11-01T00:00:00+08:00",
                                        "2026-11-02T00:00:00+08:00"), start=1):
            schedule["obligations"].append({
                "obligation_id": f"SX{index}", "family": "D", "event": "milestone",
                "instance": f"legacy-{index}", "status": "due", "window": "open",
                "opened_at": opened, "level": "optional"})
        self.save(state)
        state, synced = self.sync()
        self.assertTrue(any("取代" in change for change in synced["changes"]))
        legacy = [item for item in self.obligations("D")
                  if str(item["obligation_id"]).startswith("SX")]
        self.assertEqual(sorted(item["status"] for item in legacy),
                         ["skipped", "skipped"])
        due = [item for item in self.obligations("D") if item["status"] == "due"]
        self.assertEqual(len(due), 1)
        self.assertGreater(due[0]["opened_at"], "2026-11-02T00:00:00+08:00")
        self.assertEqual(SS.task_stats(state)["by_family"]["D"]["missed"], 0)

    def test_drive_executes_closure_consult_before_close(self):
        """drive 先执行 E，再停在等待 ZCode 验收，最后才收口。"""
        self.start(risk="high", kind="feature")
        self.plan(1)
        self.close_all()
        self.assertEqual(G.evaluate(self.state())["phase"], "PASS")
        args = argparse.Namespace(state=self.state_path, sol="auto",
                                  max_actions=1, dispatch_out="")
        out = io.StringIO()
        with self.sol_channel() as calls, \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = CA.drive(args)
        # 第一次 drive：E 已执行，但 ZCode 还没有登记判断 → 不收口。
        self.assertEqual(code, CA.EXIT_EXECUTE)
        self.assertEqual(calls["count"], 1)
        state = self.state()
        self.assertFalse(state["closure"]["closed"])
        closure_calls = [item for item in state["sol_calls"]
                         if item.get("stage") == "closure"]
        self.assertEqual(len(closure_calls), 1)
        rows = [item for item in SS.obligations(state)
                if item.get("family") == "E"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "satisfied")
        self.assertEqual(rows[0]["call_id"], closure_calls[0]["call_id"])
        self.assertIn("等待 ZCode 独立验收", out.getvalue())
        self.assertIn("sol-accept", out.getvalue())
        pending = G.closure_acceptance_pending(state)
        self.assertEqual(pending["call_id"], closure_calls[0]["call_id"])
        # 登记验收后再 drive：此时才执行 G1–G3 收口（E 不重复付费）。
        self.accept()
        with self.sol_channel() as again, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(CA.drive(args), CA.EXIT_DONE)
        self.assertEqual(again["count"], 0)
        state = self.state()
        self.assertEqual(state["closure"]["closed"], True)
        self.assertEqual(state["closure"]["sol_entry"], "drive")
        self.assertIsNone(G.closure_acceptance_pending(state))
        # 顺序：SOL 调用 → ZCode 验收 → 收口（history 只增）。
        kinds = [item.get("event") for item in state.get("history") or []]
        self.assertLess(kinds.index("sol_call_completed"),
                        kinds.index("sol_call_acceptance"))
        self.assertLess(kinds.index("sol_call_acceptance"),
                        kinds.index("task_closed"))

    def test_closure_command_marks_sol_entry(self):
        self.start(risk="high", kind="feature")
        self.plan(1)
        self.close_all()
        args = argparse.Namespace(state=self.state_path, close=True,
                                  by="zcode-native", sol="off")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(CD.cmd_closure(args), 0)
        state = self.state()
        self.assertEqual(state["closure"]["sol_entry"], "closure-command")
        # off 策略不执行，但窗口按事实关闭并计为漏调。
        self.assertEqual(state["closure"]["sol_schedule"]
                         ["at_close"]["pending_before_close"], 1)
        self.assertEqual(state["closure"]["sol_schedule"]["missed"], 1)
        warnings = G.closure_check(state)["warnings"]
        self.assertTrue(any("SOL 调度" in line for line in warnings), warnings)

    def test_medium_closure_command_executes_closure_consult(self):
        """medium 任务的收口 CONSULT 也是 recommended：closure 入口真的执行它。"""
        self.start(kind="feature", triggers=["architecture"])
        self.plan(2)
        self.close_all()
        args = argparse.Namespace(state=self.state_path, close=True,
                                  by="zcode-native", sol="auto")
        out = io.StringIO()
        with self.sol_channel() as calls, \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = CD.cmd_closure(args)
        # E 已执行，但还缺 ZCode 验收：--close 不能绕过这一步。
        self.assertEqual(code, CD.EXIT_CONTINUE)
        self.assertEqual(calls["count"], 1)
        state = self.state()
        self.assertFalse(state["closure"]["closed"])
        closure_calls = [item for item in state["sol_calls"]
                         if item.get("stage") == "closure"]
        self.assertEqual(len(closure_calls), 1)
        rows = [item for item in SS.obligations(state)
                if item.get("family") == "E"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "satisfied")
        self.assertEqual(rows[0]["window"], "closed")
        self.assertIn("等待 ZCode 独立验收", out.getvalue())
        self.assertNotIn("任务已收口", out.getvalue())
        # 登记验收后再执行 closure --close：E 复用，不重复付费。
        self.accept()
        with self.sol_channel() as again, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(CD.cmd_closure(args), 0)
        self.assertEqual(again["count"], 0)
        state = self.state()
        self.assertTrue(state["closure"]["closed"])
        self.assertEqual(state["closure"]["sol_entry"], "closure-command")
        self.assertEqual(state["closure"]["sol_schedule"]["missed"], 0)

    def test_closure_consult_is_reused_within_same_cycle(self):
        self.start(risk="high", kind="feature")
        self.plan(1)
        self.close_all()
        state, _ = self.sync(gates_passed=True)
        rows = self.obligations("E")
        self.assertEqual(len(rows), 1)
        self.record("CONSULT", stage="closure", signals=[])
        self.assertEqual(self.advice(gates_passed=True)["action"], "reuse")
        state, _ = self.sync(gates_passed=True)
        rows = [item for item in SS.obligations(state)
                if item.get("family") == "E"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "satisfied")
        self.assertEqual(rows[0]["source"], "reuse")

    def test_low_complexity_closure_gets_no_consult(self):
        self.start(risk="low", kind="feature")
        self.plan(1)
        self.close_all()
        state, _ = self.sync(gates_passed=True)
        self.assertEqual(self.obligations("E"), [])
        self.assertEqual(SS.task_stats(state)["coverage"]["closure"]
                         ["denominator"], 0)


# ── 复用、合同修订、失败与阻塞 ────────────────────────────────────────────
class DeduplicationTests(SchedulingBase):
    def test_reuse_satisfies_open_obligation_without_paying_again(self):
        self.start()
        self.sync()
        self.record("CONSULT")
        state, _ = self.sync()
        rows = self.obligations("A")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "satisfied")
        self.assertEqual(rows[0]["source"], "reuse")
        self.assertEqual(len(state["sol_calls"]), 1)  # 没有第二次付费

    def test_contract_amendment_skips_old_obligations(self):
        self.start()
        self.sync()
        old = self.obligations("A")[0]
        G.amend_contract(self.state_path,
                         contract=CONTRACT + "- 追加：保留旧接口兼容\n")
        state, _ = self.sync()
        rows = [item for item in SS.obligations(state)
                if item.get("family") == "A"]
        self.assertEqual(len(rows), 2)
        skipped = [item for item in rows
                   if item["obligation_id"] == old["obligation_id"]][0]
        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(skipped["window"], "closed")
        self.assertEqual(skipped["skip_by"], "contract-amended")
        fresh = [item for item in rows if item is not skipped][0]
        self.assertEqual(fresh["status"], "due")
        self.assertNotEqual(fresh["key"], skipped["key"])
        stats = SS.task_stats(state)
        self.assertEqual(stats["by_family"]["A"]["skipped"], 1)
        self.assertEqual(stats["by_family"]["A"]["missed"], 0)

    def test_stats_distinguish_call_records_from_transport_observations(self):
        self.start()
        self.sync()
        failed = G.record_sol_call_failure(
            self.state_path, mode="CONSULT", error_code="sol_timeout",
            request_attempted=True, send_observed="unknown")
        state = self.state()
        state["sol_calls"].append({
            "call_id": "SClegacy", "mode": "CONSULT", "status": "failed",
            "error": "legacy", "created_at": G.now_iso()})
        G.save_state(self.state_path, state)
        stats = SS.task_stats(self.state())
        self.assertEqual(stats["calls"]["total"], 2)
        self.assertEqual(stats["calls"]["request_attempted"], 1)
        self.assertEqual(stats["calls"]["send_unknown"], 1)
        self.assertEqual(stats["calls"]["transport_observation_missing"], 1)
        self.assertEqual(failed["send_observed"], "unknown")

    def test_failure_records_real_reason_and_keeps_task_running(self):
        self.start()
        self.sync()
        with self.sol_channel(error=SR.SolQuotaError("sol_quota", "429")):
            result, _out, err = self.step("auto")
        self.assertEqual(result["executed"], "SO1")
        self.assertEqual(result["exit"], 0)
        state = self.state()
        failed = [item for item in state["sol_calls"]
                  if item.get("status") == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["error"], "sol_quota")
        rows = self.obligations("A")
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIn("sol_quota", rows[0]["failure"])
        self.assertEqual(state.get("blocks") or [], [])
        self.assertIn(G.evaluate(state)["phase"], ("RUNNING", "PARTIAL"))
        self.assertIn("失败", err)

    def test_task_level_block_suppresses_obligations(self):
        self.start()
        G.add_block(self.state_path, "等待用户授权 push", "用户授权",
                    work_items=[], criteria=[], force=True)
        state, _ = self.sync()
        self.assertEqual(SS.obligations(state), [])
        self.assertEqual(self.advice()["action"], "none")

    def test_item_level_block_does_not_freeze_schedule(self):
        self.start()
        self.plan(2)
        G.add_block(self.state_path, "A1 等待用户授权", "用户授权",
                    work_items=["A1"], criteria=[])
        state, _ = self.sync()
        rows = self.obligations("A")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "due")

    def test_review_is_never_auto_scheduled(self):
        self.start(kind="architecture")
        self.record("CONSULT")
        self.plan(2)
        self.evidence()
        self.record("PLAN")
        self.assertNotEqual(self.advice()["action"], "review")
        requested = self.advice(review_requested=True)
        self.assertEqual(requested["action"], "review")
        state, _ = self.sync(review_requested=True)
        self.assertEqual([item for item in SS.obligations(state)
                          if item.get("mode") == "REVIEW"], [])

    def test_task_lock_times_out_instead_of_blocking(self):
        with G.task_lock(self.dir, ".sol.lock"):
            with self.assertRaises(G.LockBusyError):
                with G.task_lock(self.dir, ".sol.lock", timeout=0.1):
                    self.fail("不应取得已被占用的锁")

    def test_auto_obligation_argv_carries_bounded_lock_wait(self):
        """自动路径的“有界执行”必须真的带上锁等待上限，而不是无限等待。

        调度路径靠 `_obligation_argv` 构造子命令参数；缺这个参数时
        `task_lock(timeout=None)` 会一直等另一个进程的请求，工程命令被无限期
        挂住，文档里的“有界执行”也就不成立。
        """
        argv = CD._obligation_argv(self.state_path,
                                   {"obligation_id": "SO1", "mode": "CONSULT"},
                                   round_two=False)
        self.assertIn("--lock-timeout", argv)
        bound = float(argv[argv.index("--lock-timeout") + 1])
        self.assertEqual(bound, SS.AUTO_LOCK_WAIT_S)
        self.assertGreater(bound, 0)
        with mock.patch.dict(os.environ,
                             {"COPILOT_SOL_LOCK_TIMEOUT": "7.5"}):
            argv = CD._obligation_argv(self.state_path,
                                       {"obligation_id": "SO1", "mode": "PLAN"},
                                       round_two=False)
            self.assertEqual(float(argv[argv.index("--lock-timeout") + 1]), 7.5)
        # 非法/非正值回落到默认上限：环境变量不能把等待变成“永不放弃”。
        for raw in ("nope", "0", "-3"):
            with mock.patch.dict(os.environ,
                                 {"COPILOT_SOL_LOCK_TIMEOUT": raw}):
                argv = CD._obligation_argv(self.state_path,
                                           {"obligation_id": "SO1"},
                                           round_two=False)
                self.assertEqual(float(argv[argv.index("--lock-timeout") + 1]),
                                 SS.AUTO_LOCK_WAIT_S, raw)
        # 三个 SOL 子命令都必须接受这个参数；否则调度会因“参数不合法”永久待执行。
        for sub in ("sol-consult", "sol-plan", "sol-review"):
            ns = CD.build_parser().parse_args(
                [sub, "--state", self.state_path, "--lock-timeout", "5"])
            self.assertEqual(ns.lock_timeout, 5.0, sub)


# ── 门禁与统计口径 ────────────────────────────────────────────────────────
class GateAndStatsTests(SchedulingBase):
    def test_sol_windows_do_not_change_g1_g3(self):
        self.start(risk="high", kind="feature")
        self.plan(1)
        self.close_all()
        self.sync(gates_passed=True)
        check = G.closure_check(self.state())
        self.assertTrue(check["passed"])
        self.assertEqual(check["reasons"], [])
        self.assertTrue(any("SOL 调度" in line for line in check["warnings"]))
        closure = G.close_task(self.state_path, by="zcode-native")
        self.assertEqual(closure["closed"], True)

    def test_missed_is_derived_from_closed_window(self):
        state = {"task_id": "t", "contract_sha256": "c",
                 "sol_schedule": {"schema": SS.SCHEDULE_SCHEMA,
                                  "next_id": 3, "obligations": [
            {"obligation_id": "SO1", "family": "A", "event": "startup",
             "mode": "CONSULT", "instance": "i1", "status": "due",
             "window": "open", "level": "recommended", "opened_at": "1"},
            {"obligation_id": "SO2", "family": "B", "event": "plan-needed",
             "mode": "PLAN", "instance": "i2", "status": "satisfied",
             "window": "closed", "level": "recommended", "opened_at": "2",
             "closed_reason": "executed"},
        ]}}
        SS.sync(state["sol_schedule"], state, {"action": "reuse"}, now="3")
        rows = {item["obligation_id"]: item for item in
                SS.obligations(state)}
        self.assertTrue(SS.is_missed(rows["SO1"]))
        self.assertEqual(rows["SO1"]["closed_reason"], "superseded")
        self.assertFalse(SS.is_missed(rows["SO2"]))
        stats = SS.task_stats(state)
        self.assertEqual(stats["by_family"]["A"]["missed"], 1)

    def test_reopen_closure_windows_never_exceed_full_coverage(self):
        """多轮 reopen 的多次收口判断按任务计 1 次：覆盖率不得 > 100%。

        回归 `coverage.closure.rate=3.0`：分子取满足义务条数（3 次收口）而分母是
        任务级适用（1），单位不一致会把覆盖率算成 300%。义务条数仍保留在
        `by_family` 里解释“实际覆盖了几次”。
        """
        state = {
            "task_id": "t-reopen", "goal": "g", "risk": "high",
            "task_kind": "governance", "contract_sha256": "c3",
            "contract": {"criteria": [{"criterion_id": "C1", "status": "met",
                                      "description": "x"}]},
            "actions": [{"id": "A1", "status": "done", "title": "x"}],
            "evidence": [{"id": "E1"}],
            "sol_calls": [],
            "sol_schedule": {"schema": SS.SCHEDULE_SCHEMA, "next_id": 9,
                             "obligations": [
                {"obligation_id": "SO1", "family": "E", "event": "closure",
                 "mode": "CONSULT", "status": "satisfied", "window": "closed",
                 "source": "executed", "level": "recommended"},
                {"obligation_id": "SO2", "family": "E", "event": "closure",
                 "mode": "CONSULT", "status": "skipped", "window": "closed",
                 "skip_by": "superseded-by-newer", "level": "recommended"},
                {"obligation_id": "SO3", "family": "E", "event": "closure",
                 "mode": "CONSULT", "status": "satisfied", "window": "closed",
                 "source": "executed", "level": "recommended"},
                {"obligation_id": "SO4", "family": "E", "event": "closure",
                 "mode": "CONSULT", "status": "satisfied", "window": "closed",
                 "source": "executed", "level": "recommended"},
            ]}}
        stats = SS.task_stats(state)
        coverage = stats["coverage"]["closure"]
        self.assertTrue(stats["closure_eligible"])
        self.assertEqual((coverage["numerator"], coverage["denominator"]),
                         (1, 1))
        self.assertEqual(coverage["rate"], 1.0)
        self.assertEqual(coverage["skipped"], 1)
        # 义务条数仍可解释：这一任务实际做过 3 次收口判断。
        self.assertEqual(stats["by_family"]["E"]["satisfied"], 3)

    def test_events_denominator_excludes_legitimately_skipped_windows(self):
        """事件分母只含适用窗口：合同修订/被取代的窗口不进入分母。"""
        state = {
            "task_id": "t-events", "goal": "g", "risk": "medium",
            "task_kind": "standard", "contract_sha256": "c4",
            "actions": [{"id": "A1", "status": "done", "title": "x"}],
            "sol_calls": [],
            "sol_schedule": {"schema": SS.SCHEDULE_SCHEMA, "next_id": 5,
                             "obligations": [
                {"obligation_id": "SO1", "family": "C",
                 "event": "decision-point", "mode": "CONSULT",
                 "status": "satisfied", "window": "closed",
                 "source": "executed", "level": "recommended"},
                {"obligation_id": "SO2", "family": "C",
                 "event": "scope-changed", "mode": "CONSULT",
                 "status": "skipped", "window": "closed",
                 "skip_by": "contract-amended", "level": "recommended"},
                {"obligation_id": "SO3", "family": "C",
                 "event": "reopened", "mode": "CONSULT", "status": "due",
                 "window": "closed", "level": "recommended"},
            ]}}
        coverage = SS.task_stats(state)["coverage"]["events"]
        self.assertEqual(coverage["skipped"], 1)
        self.assertEqual(coverage["missed"], 1)
        self.assertEqual((coverage["numerator"], coverage["denominator"]),
                         (1, 2))
        self.assertEqual(coverage["rate"], 0.5)

    def test_all_windows_skipped_reports_not_applicable(self):
        """全部合理跳过时事件覆盖率是“不适用”，不是伪造的 0% 或 100%。"""
        state = {
            "task_id": "t-skip", "goal": "g", "risk": "medium",
            "task_kind": "standard", "contract_sha256": "c5",
            "actions": [{"id": "A1", "status": "done", "title": "x"}],
            "sol_calls": [],
            "sol_schedule": {"schema": SS.SCHEDULE_SCHEMA, "next_id": 3,
                             "obligations": [
                {"obligation_id": "SO1", "family": "C",
                 "event": "scope-changed", "mode": "CONSULT",
                 "status": "skipped", "window": "closed",
                 "skip_by": "contract-amended", "level": "recommended"},
            ]}}
        coverage = SS.task_stats(state)["coverage"]["events"]
        self.assertEqual(coverage["denominator"], 0)
        self.assertIsNone(coverage["rate"])

    def test_stats_separate_suggestion_execution_reuse_skip_failure(self):
        state = {
            "task_id": "t2", "goal": "g", "risk": "medium",
            "task_kind": "standard", "contract_sha256": "c2",
            "contract": {"criteria": [{"criterion_id": "C1", "status": "met",
                                      "description": "x"}]},
            "actions": [{"id": "A1", "status": "done", "title": "x"}],
            "evidence": [{"id": "E1"}],
            "sol_calls": [
                {"call_id": "SC1", "mode": "CONSULT", "status": "completed",
                 "usage": {"total_tokens": 100},
                 "acceptance": {"status": "accepted", "latency_s": 1.5}},
                {"call_id": "SC2", "mode": "PLAN", "status": "failed",
                 "error": "sol_quota"}],
            "sol_schedule": {"schema": SS.SCHEDULE_SCHEMA, "next_id": 5,
                             "obligations": [
                {"obligation_id": "SO1", "family": "A", "event": "startup",
                 "mode": "CONSULT", "status": "satisfied", "window": "closed",
                 "source": "executed", "call_id": "SC1", "level": "recommended"},
                {"obligation_id": "SO2", "family": "B", "event": "plan-needed",
                 "mode": "PLAN", "status": "failed", "window": "closed",
                 "failure": "sol_quota", "level": "recommended"},
                {"obligation_id": "SO3", "family": "C",
                 "event": "replan-history", "mode": "CONSULT",
                 "status": "skipped", "window": "closed",
                 "skip_by": "contract-amended", "level": "recommended"},
                {"obligation_id": "SO4", "family": "C",
                 "event": "decision-point", "mode": "CONSULT",
                 "status": "satisfied", "window": "closed",
                 "source": "reuse", "level": "recommended"},
                {"obligation_id": "SO5", "family": "C",
                 "event": "reopened", "mode": "CONSULT", "status": "due",
                 "window": "closed", "level": "recommended"},
            ]}}
        stats = SS.task_stats(state)
        self.assertTrue(stats["eligible"])
        self.assertTrue(stats["progressed"])
        self.assertEqual(stats["by_family"]["A"]["executed"], 1)
        self.assertEqual(stats["by_family"]["C"]["reused"], 1)
        self.assertEqual(stats["by_family"]["C"]["skipped"], 1)
        self.assertEqual(stats["by_family"]["B"]["failed"], 1)
        self.assertEqual(stats["by_family"]["C"]["missed"], 1)
        self.assertEqual(stats["calls"], {
            "completed": 1, "failed": 1, "total": 2,
            "request_attempted": 0, "response_received": 0,
            "send_unknown": 0, "not_attempted": 0,
            "transport_observation_missing": 2})
        self.assertEqual(stats["acceptance"]["accepted"], 1)
        self.assertEqual(stats["acceptance"]["unrecorded"], 0)
        self.assertEqual(stats["usage"]["total_tokens"], 100)
        self.assertEqual(stats["coverage"]["startup"]["numerator"], 1)
        self.assertIsNotNone(stats["coverage"]["events"]["rate"])

    def test_stats_command_aggregates_workspace_with_explicit_denominator(self):
        tasks = os.path.join(self.dir, ".copilot-tasks")
        plain = os.path.join(tasks, "t-plain", "state.json")
        os.makedirs(os.path.dirname(plain))
        G.start_state(plain, goal="普通任务", contract=CONTRACT,
                      risk="medium", task_id="t-plain", workspace=self.dir)
        trivial = os.path.join(tasks, "t-trivial", "state.json")
        os.makedirs(os.path.dirname(trivial))
        G.start_state(trivial, goal="琐碎任务", contract=CONTRACT,
                      risk="low", task_id="t-trivial", workspace=self.dir,
                      task_kind="trivial")
        proc = F.run_cli("copilot_dispatch.py", "sol-stats",
                         "--workspace", self.dir, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["totals"]["tasks"], 2)
        self.assertEqual(payload["totals"]["eligible_tasks"], 1)
        self.assertEqual(payload["coverage"]["startup"]["denominator"], 1)
        self.assertEqual(payload["coverage"]["startup"]["numerator"], 0)

    def test_sol_schedule_view_is_read_only_without_execute(self):
        self.start()
        before = self._state_bytes()
        proc = F.run_cli("copilot_dispatch.py", "sol-schedule",
                         "--state", self.state_path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        # 视图在内存里物化义务（便于显示），但绝不写盘：状态文件逐字节不变。
        self.assertEqual(payload["summary"]["pending"], 1)
        self.assertEqual(len(payload["obligations"]), 1)
        self.assertEqual(self._state_bytes(), before)


# ── E 收口时序：只有最终工程验证完成才执行收口 CONSULT ────────────────────
class ClosureAdmissionTests(SchedulingBase):
    """E（收口 CONSULT）准入回归。

    工作项全部关闭不等于最终工程验证完成：合同要求的测试、证据与条件核销任一
    缺口时，E 保持待执行；完成后自动执行一次，同一份最终验证摘要不重复付费，
    验证事实变化后旧结论失效并允许新的 E；收口仍由 ZCode 依 G1–G3 完成。
    """

    def start_strict(self) -> None:
        G.start_state(
            self.state_path, goal="交付可复核结果", contract=CONTRACT,
            risk="medium", task_id="sol-closure", workspace=self.dir,
            task_kind="feature", triggers=["architecture"],
            test_policy={"required": True,
                         "commands": ["python3 -m unittest"]})

    def covered(self) -> None:
        """A/B 两个上游窗口已由真实记录覆盖，只留 E 待判断。"""
        self.start_strict()
        self.record("CONSULT")
        self.plan(2)
        self.evidence()
        self.record("PLAN")

    def actions_done(self) -> None:
        state = self.state()
        for action in state.get("actions") or []:
            G.record_action(self.state_path, action["id"], status="done",
                            result="完成", evidence_ids=["E1"])

    def criteria_met(self) -> None:
        state = self.state()
        for criterion in state["contract"]["criteria"]:
            G.record_criterion(self.state_path, criterion["criterion_id"],
                               "met", evidence_ids=["E1"])

    def register_tests_evidence(self, *, capture: bool = True,
                                name: str = "tests.log",
                                content: str = "Ran 7 tests\nOK\n",
                                command: str = "") -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        state = self.state()
        eid = G.next_evidence_id(state)
        G.register_evidence(
            state, eid, kind="capture" if capture else "file",
            file_path=path, note="测试输出",
            capture_cmd=(command or "python3 -m unittest discover -s tests")
            if capture else "")
        self.save(state)
        return eid

    def verified(self) -> None:
        self.covered()
        self.actions_done()
        self.criteria_met()
        self.register_tests_evidence()

    def closure_calls(self) -> list[dict]:
        return [item for item in self.state().get("sol_calls") or []
                if isinstance(item, dict)
                and str(item.get("stage") or "") == "closure"]

    def test_last_item_closed_without_tests_does_not_run_closure_consult(self):
        """最后一个工作项关闭、条件也核销了，但必要测试没跑：E 不得执行。"""
        self.covered()
        self.actions_done()
        self.criteria_met()
        readiness = G.closure_readiness(self.state())
        self.assertFalse(readiness["ready"])
        self.assertEqual([item["code"] for item in readiness["blockers"]],
                         ["required_tests_missing"])
        self.assertNotEqual(self.advice()["family"], "E")
        with self.sol_channel() as calls:
            result, _out, _err = self.step("auto")
        self.assertEqual(calls["count"], 0)
        self.assertEqual(result["executed"], "")
        self.assertEqual(self.obligations("E"), [])
        self.assertEqual(self.closure_calls(), [])
        continuation = G.evaluate(self.state())["continuation"]
        self.assertTrue(continuation["required"])
        self.assertEqual(continuation["action"], "verify")

    def test_test_evidence_without_capture_command_does_not_unlock(self):
        """“测试跑过了”只是自述：没有可核对的采集命令/质量检查时不算验证完成。"""
        self.covered()
        self.actions_done()
        self.criteria_met()
        self.register_tests_evidence(capture=False)
        G.add_report(self.state_path, "自述：单元测试已通过")
        readiness = G.closure_readiness(self.state())
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["tests_source"], "missing")
        self.assertNotEqual(self.advice()["family"], "E")

    def test_declared_test_command_matches_with_extra_runner_flags(self):
        """合同写 `python3 -m unittest`，仓库规范跑 `python3 -B -m unittest …` 仍应命中。"""
        self.covered()
        self.actions_done()
        self.criteria_met()
        self.register_tests_evidence(
            command="python3 -B -m unittest discover -s tests -p 'test_*.py'")
        readiness = G.closure_readiness(self.state())
        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["tests_source"], "captured-command")

    def test_unrelated_test_command_does_not_unlock(self):
        """换成别的运行器不算执行了合同要求的测试。"""
        self.covered()
        self.actions_done()
        self.criteria_met()
        self.register_tests_evidence(command="pytest -q tests")
        readiness = G.closure_readiness(self.state())
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["tests_source"], "missing")

    def test_closure_consult_waits_for_criteria(self):
        self.covered()
        self.actions_done()
        self.register_tests_evidence()
        readiness = G.closure_readiness(self.state())
        self.assertFalse(readiness["ready"])
        self.assertIn("criteria_not_satisfied",
                      [item["code"] for item in readiness["blockers"]])
        self.assertNotEqual(self.advice()["family"], "E")

    def test_closure_consult_executes_after_final_verification(self):
        self.verified()
        digest = G.closure_readiness(self.state())["digest"]
        advice = self.advice()
        self.assertEqual((advice["family"], advice["event"]), ("E", "closure"))
        self.assertEqual(advice["instance"], digest)
        self.assertEqual(advice["final_verification_digest"], digest)
        state, _ = self.sync()
        rows = self.obligations("E")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["instance"], digest)
        with self.sol_channel() as calls:
            result, _out, _err = self.step("auto")
        self.assertEqual(calls["count"], 1)
        self.assertEqual(result["executed"], rows[0]["obligation_id"])
        calls_rows = self.closure_calls()
        self.assertEqual(len(calls_rows), 1)
        record = calls_rows[0]
        self.assertEqual(record["mode"], "CONSULT")
        self.assertEqual(record["stage"], "closure")
        self.assertEqual(record["final_verification_digest"], digest)
        self.assertTrue(record["zero_tool"])
        self.assertTrue(record["completed_at"])
        self.assertEqual(self.obligations("E")[0]["status"], "satisfied")
        # E 只是认知判断：任务没有被收口，也没有被验收。
        self.assertFalse(self.state()["closure"]["closed"])

    def test_same_verification_digest_is_not_paid_twice(self):
        self.verified()
        with self.sol_channel() as calls:
            self.step("auto")
        self.assertEqual(calls["count"], 1)
        self.assertEqual(self.advice()["action"], "reuse")
        with self.sol_channel() as second:
            result, _out, _err = self.step("auto")
        self.assertEqual(second["count"], 0)
        self.assertEqual(result["executed"], "")
        self.assertEqual(len(self.closure_calls()), 1)

    def test_new_verification_invalidates_old_closure_conclusion(self):
        self.verified()
        digest_before = G.closure_readiness(self.state())["digest"]
        with self.sol_channel():
            self.step("auto")
        old = self.closure_calls()[0]["final_verification_digest"]
        self.assertEqual(old, digest_before)
        # 实质变更后重新验证：新的采集证据进入最终验证摘要。
        self.register_tests_evidence(name="tests-rerun.log",
                                     content="Ran 9 tests\nOK\n")
        digest_after = G.closure_readiness(self.state())["digest"]
        self.assertNotEqual(digest_before, digest_after)
        advice = self.advice()
        self.assertEqual(advice["action"], "consult")
        self.assertEqual(advice["family"], "E")
        self.assertEqual(advice["instance"], digest_after)
        self.assertNotEqual(advice["instance"], old)

    def test_materials_schema_bump_invalidates_old_closure_conclusion(self):
        """材料口径变了但验证事实没变：旧 E 结论不得冒充覆盖新材料。"""
        self.verified()
        with self.sol_channel():
            self.step("auto")
        old = self.closure_calls()[0]
        digest_before = old["final_verification_digest"]
        self.assertEqual(self.advice()["action"], "reuse")
        with mock.patch.object(SS, "MATERIALS_SCHEMA",
                               "copilot-sol-schedule-materials/v999"):
            digest_after = G.closure_readiness(self.state())["digest"]
            self.assertNotEqual(digest_before, digest_after)
            advice = self.advice()
        self.assertEqual(advice["action"], "consult")
        self.assertEqual(advice["family"], "E")
        self.assertEqual(advice["instance"], digest_after)

    def test_materials_declare_their_schema_version(self):
        """材料自报口径版本，审计能看出这份材料按哪套规则生成。"""
        self.start_strict()
        obligation = {"family": "E", "mode": "CONSULT", "stage": "closure",
                      "event": "closure", "question": "收口判断"}
        text = SS.build_materials(self.state(), obligation)
        self.assertIn(f"- 材料口径：{SS.MATERIALS_SCHEMA}", text)

    def test_second_closure_consult_allowed_after_reverification(self):
        self.verified()
        with self.sol_channel() as first:
            self.step("auto")
        self.assertEqual(first["count"], 1)
        self.register_tests_evidence(name="tests-rerun.log",
                                     content="Ran 9 tests\nOK\n")
        digest = G.closure_readiness(self.state())["digest"]
        with self.sol_channel() as second:
            self.step("auto")
        self.assertEqual(second["count"], 1)
        rows = self.closure_calls()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["final_verification_digest"], digest)
        instances = {item["instance"] for item in self.obligations("E")}
        self.assertEqual(len(instances), 2)

    def test_drive_does_not_run_closure_consult_before_verification(self):
        """drive 命中 PASS 也不能在验证未完成时执行 E 或收口。"""
        self.covered()
        self.actions_done()
        self.criteria_met()
        self.assertEqual(G.evaluate(self.state())["phase"], "PASS")
        args = argparse.Namespace(state=self.state_path, sol="auto",
                                  max_actions=1, dispatch_out="")
        with self.sol_channel() as calls, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = CA.drive(args)
        self.assertEqual(code, CA.EXIT_EXECUTE)
        self.assertEqual(calls["count"], 0)
        state = self.state()
        self.assertFalse(state["closure"]["closed"])
        self.assertEqual(self.closure_calls(), [])
        self.assertEqual(self.obligations("E"), [])

    def test_criterion_command_reevaluates_closure_admission(self):
        """核销最后一条条件后，E 由该命令自身的调度步兑现（不是永久挂起）。"""
        self.covered()
        self.actions_done()
        self.register_tests_evidence()
        pending = [item["criterion_id"]
                   for item in self.state()["contract"]["criteria"]]
        self.assertTrue(pending)
        argv = ["criterion", "--state", self.state_path,
                "--id", pending[-1], "--status", "met",
                "--evidence", "E1", "--sol", "auto"]
        with self.sol_channel() as calls, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = CD.main(argv)
        # 收口判断已兑现，但收口本身仍是下一步：命令以续跑退出码提示继续。
        self.assertEqual(code, CD.EXIT_CONTINUE)
        self.assertEqual(calls["count"], 1)
        rows = self.obligations("E")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "satisfied")
        self.assertEqual(len(self.closure_calls()), 1)

    def test_drive_runs_closure_consult_after_verification(self):
        """验证完成后同一条 drive 路径执行 E，再停在等待验收，最后才收口。"""
        self.verified()
        digest = G.closure_readiness(self.state())["digest"]
        args = argparse.Namespace(state=self.state_path, sol="auto",
                                  max_actions=1, dispatch_out="")
        with self.sol_channel() as calls, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = CA.drive(args)
        self.assertEqual(code, CA.EXIT_EXECUTE)
        self.assertEqual(calls["count"], 1)
        state = self.state()
        self.assertFalse(state["closure"]["closed"])
        self.assertEqual(self.closure_calls()[0]["final_verification_digest"],
                         digest)
        self.accept()
        with self.sol_channel() as again, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(CA.drive(args), CA.EXIT_DONE)
        self.assertEqual(again["count"], 0)
        state = self.state()
        self.assertTrue(state["closure"]["closed"])
        self.assertEqual(state["closure"]["final_verification_digest"], digest)

    def test_closure_consult_failure_degrades_without_blocking(self):
        self.verified()
        with self.sol_channel(error=SR.SolQuotaError("sol_quota", "429")):
            result, _out, err = self.step("auto")
        self.assertEqual(result["exit"], 0)
        state = self.state()
        failed = [item for item in state["sol_calls"]
                  if item.get("status") == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["error"], "sol_quota")
        # 失败记录带窗口阶段：收口窗口的失败可在审计里定位，且仍不构成任何结论。
        self.assertEqual(failed[0]["stage"], "closure")
        self.assertEqual(state.get("blocks") or [], [])
        self.assertEqual(G.evaluate(state)["phase"], "PASS")
        self.assertIn("失败", err)
        # SOL 不可用不等于验证失败：工程验收仍按真实证据完成，不因此阻塞。
        args = argparse.Namespace(state=self.state_path, close=True,
                                  by="zcode-native", sol="advise")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(CD.cmd_closure(args), 0)
        self.assertTrue(self.state()["closure"]["closed"])

    def test_closure_risks_are_advisory_not_automatic_blocks(self):
        self.verified()
        before = G.closure_check(self.state())["passed"]
        risky = think_result()
        risky["risks"] = [{"risk": "缺少真机验证", "impact": "可能不兼容"}]
        with self.sol_channel(result=risky):
            self.step("auto")
        state = self.state()
        self.assertTrue(self.closure_calls()[0]["risks"])
        self.assertEqual(state.get("blocks") or [], [])
        self.assertEqual(G.closure_check(state)["passed"], before)
        # 未由主代理核实并登记前，SOL 风险不会变成 finding/阻塞。
        self.assertEqual(G.list_findings(state)["total"], 0)

    def test_close_command_does_not_bypass_final_verification(self):
        self.covered()
        self.actions_done()
        self.criteria_met()
        args = argparse.Namespace(state=self.state_path, close=True,
                                  by="zcode-native", sol="auto")
        out = io.StringIO()
        with self.sol_channel() as calls, contextlib.redirect_stdout(out):
            code = CD.cmd_closure(args)
        self.assertEqual(code, 1)
        self.assertEqual(calls["count"], 0)
        state = self.state()
        self.assertFalse(state["closure"]["closed"])
        self.assertEqual(self.closure_calls(), [])
        self.assertIn("最终工程验证尚未完成", out.getvalue())

    def test_close_task_refuses_missing_required_tests(self):
        self.covered()
        self.actions_done()
        self.criteria_met()
        with self.assertRaises(G.GovernanceError) as ctx:
            G.close_task(self.state_path)
        self.assertIn("测试", str(ctx.exception))

    def test_closure_consult_runs_before_acceptance_and_close(self):
        """顺序固定：E → ZCode 验收 → 收口；`--close` 不能跳过验收。"""
        self.verified()
        digest = G.closure_readiness(self.state())["digest"]
        with self.sol_channel() as calls:
            self.step("auto")
        self.assertEqual(calls["count"], 1)
        self.assertFalse(self.state()["closure"]["closed"])
        self.assertTrue(G.closure_check(self.state())["passed"])
        args = argparse.Namespace(state=self.state_path, close=True,
                                  by="zcode-native", sol="advise")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(CD.cmd_closure(args), CD.EXIT_CONTINUE)
        state = self.state()
        self.assertFalse(state["closure"]["closed"])
        self.assertIn("等待 ZCode 独立验收", out.getvalue())
        # 直接调用 governance 收口同样被拒绝：任何入口都不能跳过验收。
        with self.assertRaises(G.GovernanceError) as ctx:
            G.close_task(self.state_path)
        self.assertIn("采纳判断", str(ctx.exception))
        self.accept()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(CD.cmd_closure(args), 0)
        state = self.state()
        self.assertTrue(state["closure"]["closed"])
        self.assertEqual(state["closure"]["final_verification_digest"], digest)
        events = [item.get("event") for item in state.get("history") or []]
        self.assertLess(events.index("sol_call_completed"),
                        events.index("sol_call_acceptance"))
        self.assertLess(events.index("sol_call_acceptance"),
                        events.index("task_closed"))
        self.assertEqual(state["closure"]["sol_schedule"]["missed"], 0)

    def _reset_workspace(self) -> None:
        """换一个干净的工作区（同一用例内要跑多个互不影响的收口场景）。"""
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")

    def test_closure_acceptance_gate_accepts_every_verdict(self):
        """验收不是批准权：采纳、修改、否决任一记录都能继续收口。"""
        for status in ("accepted", "modified", "rejected"):
            with self.subTest(status=status):
                self._reset_workspace()
                self.verified()
                with self.sol_channel():
                    self.step("auto")
                self.accept(status=status,
                            rationale="ZCode 独立判断，与验收结论无关")
                self.assertIsNone(
                    G.closure_acceptance_pending(self.state()))
                self.assertEqual(CD.cmd_closure(argparse.Namespace(
                    state=self.state_path, close=True, by="zcode-native",
                    sol="off")), 0)
                self.assertTrue(self.state()["closure"]["closed"])

    def test_closed_task_cannot_take_acceptance_after_the_fact(self):
        """收口后再登记验收会被拒绝：这就是顺序必须在收口前完成的原因。"""
        self.verified()
        with self.sol_channel():
            self.step("auto")
        self.accept()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(CD.cmd_closure(argparse.Namespace(
                state=self.state_path, close=True, by="zcode-native",
                sol="off")), 0)
        call_id = self.closure_calls()[0]["call_id"]
        with self.assertRaises(G.GovernanceError) as ctx:
            G.record_sol_acceptance(self.state_path, call_id,
                                    status="modified", rationale="补记")
        self.assertIn("已收口", str(ctx.exception))

    def _consult_args(self, obligation_id: str = "") -> argparse.Namespace:
        materials = os.path.join(self.dir, "closure-materials.md")
        with open(materials, "w", encoding="utf-8") as fh:
            fh.write("最终验证已完成：请做收口综合判断。\n")
        return argparse.Namespace(
            state=self.state_path, materials=materials, question="",
            stage="closure", reason="", trigger="", force=False,
            dry_run=False, list=False, json=True, timeout=5,
            max_output_tokens=100, audit_log="", output_dir="",
            knowledge_file="", obligation_id=obligation_id)

    def test_manual_closure_consult_refused_before_verification(self):
        self.covered()
        self.actions_done()
        self.criteria_met()
        with self.sol_channel() as calls, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(CD.CLIError) as ctx:
                CD.cmd_sol_consult(self._consult_args())
        self.assertIn("最终工程验证尚未完成", str(ctx.exception))
        self.assertEqual(calls["count"], 0)

    def test_stale_closure_obligation_refused_after_digest_change(self):
        self.verified()
        self.sync()
        obligation = self.obligations("E")[0]
        self.register_tests_evidence(name="tests-rerun.log",
                                     content="Ran 9 tests\nOK\n")
        with self.sol_channel() as calls, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(CD.CLIError) as ctx:
                CD.cmd_sol_consult(
                    self._consult_args(obligation["obligation_id"]))
        self.assertIn("最终验证摘要已变化", str(ctx.exception))
        self.assertEqual(calls["count"], 0)

    def test_newer_digest_is_not_covered_by_earlier_acceptance(self):
        """旧摘要的判断与验收都不覆盖新摘要：E 未新跑时收口只能如实记漏调。

        收口前又产生了新验证事实（新摘要）时，旧 E 结论与旧验收都只属于旧摘要；
        这里确认系统不会把它们当成新摘要的覆盖（否则顺序执行的 E2E 也排除不了
        误收口），而是把没执行的 E 实例记成漏调。
        """
        self.verified()
        with self.sol_channel():
            self.step("auto")
        first = self.closure_calls()[0]
        self.accept()
        self.register_tests_evidence(name="tests-rerun.log",
                                     content="Ran 9 tests\nOK\n")
        digest = G.closure_readiness(self.state())["digest"]
        self.assertNotEqual(digest, first["final_verification_digest"])
        self.assertIsNone(G.closure_acceptance_pending(self.state()))
        self.assertEqual(self.advice()["action"], "consult")
        self.assertEqual(self.advice()["instance"], digest)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(CD.cmd_closure(argparse.Namespace(
                state=self.state_path, close=True, by="zcode-native",
                sol="off")), 0)
        closure = self.state()["closure"]
        self.assertEqual(closure["final_verification_digest"], digest)
        self.assertEqual(
            closure["sol_schedule"]["at_close"]["pending_before_close"], 1)
        self.assertEqual(closure["sol_schedule"]["missed"], 1)
        self.assertEqual(self.closure_calls()[0]["final_verification_digest"],
                         first["final_verification_digest"])

    def test_materials_carry_contract_body_instead_of_none(self):
        """材料必须带合同正文：`body` 不是状态键，读错会印出 “None”。"""
        self.start_strict()
        state = self.state()
        self.assertEqual(
            state["contract"]["done_criteria_text"].strip(), CONTRACT.strip())
        obligation = {"family": "E", "mode": "CONSULT", "stage": "closure",
                      "event": "closure", "question": "收口判断"}
        text = SS.build_materials(state, obligation)
        self.assertNotIn("合同正文摘要：None", text)
        self.assertIn("产出可复核的结果文件", text)

    def test_materials_mark_absent_contract_body_as_unregistered(self):
        """合同正文为空时显式写未登记，不用空串或 None 冒充内容。"""
        state = {"goal": "无正文任务", "task_kind": "standard", "risk": "low",
                 "contract": {"done_criteria_text": "", "criteria": [],
                              "test_policy": {"required": False}},
                 "actions": [], "evidence": []}
        obligation = {"family": "A", "mode": "CONSULT",
                      "stage": "understanding", "event": "startup"}
        text = SS.build_materials(state, obligation)
        self.assertIn("合同正文摘要：（未登记）", text)


if __name__ == "__main__":
    unittest.main()
#!/usr/bin/env python3
"""Astra 自动调度回归：内容复杂度、任务级策略继承、调度点边界与生命周期观测。

本文件只验证 v9.11 调度优化引入的行为，避免重复 `test_cognition_scheduling.py`
已经覆盖的窗口/门禁语义：

1. 复杂度按**合同内容**识别：治理/架构/协作类文字即使任务类型是 docs 也进入
   中等复杂度并产生启动 CONSULT；真正的简单文档任务保持 0 调用。
2. 策略随任务生效：`start` 记录的策略（默认 auto）对后续生命周期命令持续有效，
   不需要每条命令重复 `--cognition auto`；任务级 `off` 同样对 `drive` 生效。
3. 调度点边界：只有改变生命周期事实的命令（start/plan/record/criterion/rerun/
   unblock/reopen/closure/drive）会消费义务；judgment/evidence 等纯记录命令
   即使在环境变量 auto 下也只物化义务、不付费。
4. 生命周期可观测：triggered / invoked / succeeded / failed / skipped / missed /
   deferred 与 opened_by 归因，多任务聚合同口径。

所有认知通道都用替身；本文件不发起任何真实模型请求。
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
import review_policy as RP  # noqa: E402
import cognition_call as CC  # noqa: E402
import cognition_schedule as SCH  # noqa: E402
import copilot_dispatch as CD  # noqa: E402
import copilot_auto as CA  # noqa: E402

CONTRACT = "## 完成条件\n- 产出可复核的结果文件\n"
GOVERNANCE_CONTRACT = """## 完成条件
- 复核全局规则与角色边界，确认认知调度规则是否仍然成立
- 给出规则优化后的验证结论
"""


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


class AutoDispatchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        self.contract_path = os.path.join(self.dir, "contract.md")
        self.plan_path = os.path.join(self.dir, "plan.json")

    # ── 状态构造 ─────────────────────────────────────────────────────────
    def write_contract(self, text: str = CONTRACT) -> None:
        with open(self.contract_path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def write_plan(self, actions: int = 1) -> None:
        items = [{"id": f"A{index}", "title": f"工作{index}",
                  "goal": f"目标{index}", "expected_evidence": "证据"}
                 for index in range(1, actions + 1)]
        with open(self.plan_path, "w", encoding="utf-8") as fh:
            json.dump({"summary": "计划", "actions": items}, fh)

    def start_state(self, *, goal: str = "交付可复核结果",
                    contract: str = CONTRACT, risk: str = "medium",
                    kind: str = "architecture") -> dict:
        return G.start_state(
            self.state_path, goal=goal, contract=contract, risk=risk,
            task_id="auto-dispatch", workspace=self.dir, task_kind=kind)

    def state(self) -> dict:
        return G.load_state(self.state_path)

    def calls(self) -> list[dict]:
        return [item for item in self.state().get("cognition_calls") or []
                if isinstance(item, dict)]

    def obligations(self, family: str = "") -> list[dict]:
        rows = SCH.obligations(self.state())
        return [item for item in rows
                if not family or str(item.get("family")) == family]

    def sync(self, *, policy: str = "advise", auto: bool = False,
             origin: str = "", advice: dict | None = None) -> dict:
        state = self.state()
        synced = G.sync_cognition_schedule(
            state, advice=advice if advice is not None
            else G.cognition_advice_summary(state),
            policy=policy, auto_execution=auto, origin=origin)
        if synced["changes"]:
            G.save_state(self.state_path, state)
        return synced

    # ── CLI 与认知通道 ───────────────────────────────────────────────────
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = CD.main(argv)
        return code, out.getvalue(), err.getvalue()

    def start_cli(self, *extra: str) -> tuple[int, str, str]:
        self.write_contract()
        return self.run_cli([
            "start", "--state", self.state_path, "--goal", "交付可复核结果",
            "--contract-file", self.contract_path, "--task-kind", "architecture",
            "--workspace", self.dir, *extra])

    @contextlib.contextmanager
    def channel(self, *, auto: str = "on", error: Exception | None = None,
                env: dict | None = None):
        """替换认知通道并显式控制 `COPILOT_COGNITION_AUTO`；一次也不联网。"""
        calls = {"count": 0, "modes": []}

        def fake(mode: str):
            def run(*_args, **_kwargs):
                calls["count"] += 1
                calls["modes"].append(mode)
                if error is not None:
                    raise error
                return think_result(mode)
            return run

        envs = {"COPILOT_COGNITION_AUTO": auto, **(env or {})}
        with mock.patch.dict(os.environ, envs), \
                mock.patch.object(CC, "run_consult", side_effect=fake("CONSULT")), \
                mock.patch.object(CC, "run_plan", side_effect=fake("PLAN")):
            yield calls


# ── A：按内容识别认知价值 ─────────────────────────────────────────────────
class ContentComplexityTests(AutoDispatchBase):
    def test_governance_docs_task_is_not_skipped_by_file_type(self):
        """任务类型是 docs，但合同在评审规则/架构边界：必须进入认知流程。"""
        self.start_state(goal="评审全局规则与角色边界，优化认知调度",
                         contract=GOVERNANCE_CONTRACT, risk="medium",
                         kind="docs")
        complexity = RP.task_complexity(self.state())
        self.assertEqual(complexity["level"], "medium")
        self.assertIn("high-impact-scope", complexity["signal_codes"])
        self.sync()
        rows = self.obligations("A")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "startup")
        self.assertEqual(rows[0]["status"], "due")
        self.assertEqual(rows[0]["level"], "recommended")

    def test_mechanical_task_with_global_rule_content_gets_consult(self):
        """机械类任务若改的是全局规范，同样不能被类型静默跳过。"""
        self.start_state(goal="调整 AGENTS.md 的协作规则与认知调度条款",
                         contract=GOVERNANCE_CONTRACT, risk="medium",
                         kind="mechanical")
        complexity = RP.task_complexity(self.state())
        self.assertIn("high-impact-scope", complexity["signal_codes"])
        self.assertIn(complexity["level"], ("medium", "high"))

    def test_plain_docs_task_keeps_zero_calls(self):
        self.start_state(goal="修正文档中的错别字", risk="low", kind="docs")
        complexity = RP.task_complexity(self.state())
        self.assertEqual(complexity["level"], "low")
        self.assertEqual(complexity["signal_codes"], [])
        self.sync()
        self.assertEqual(SCH.obligations(self.state()), [])
        args = argparse.Namespace(state="", cognition="auto", command="record")
        with self.channel() as calls:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                CD._cognition_schedule_step(self.state_path, args)
        self.assertEqual(calls["count"], 0)
        self.assertEqual(self.calls(), [])


# ── B：策略随任务生效，不需要每条命令传开关 ───────────────────────────────
class TaskScopedPolicyTests(AutoDispatchBase):
    def test_dispatch_commands_inherit_task_policy_without_flags(self):
        """start 记录的 auto 策略由 plan / record 继承：全程不传 --cognition。"""
        with self.channel(auto="off"):
            code, _out, _err = self.start_cli()
        self.assertEqual(code, 0)
        snapshot = SCH.policy_snapshot(SCH.schedule_view(self.state()), 1)
        self.assertEqual(snapshot.get("value"), "auto")
        self.assertEqual(self.obligations("A")[0]["status"], "due")
        self.assertEqual(self.calls(), [])

        # 纯记录命令不是调度点：即使环境变量 auto 也不付费。
        with self.channel(env={"COPILOT_COGNITION": "auto"}):
            code, _out, _err = self.run_cli(
                ["judgment", "--state", self.state_path,
                 "--status", "pass", "--summary", "阶段判断"])
            self.assertEqual(code, 0)
            self.assertEqual(self.calls(), [])

            self.write_plan(2)
            code, _out, _err = self.run_cli(
                ["plan", "--state", self.state_path, "--plan", self.plan_path])
            self.assertIn(code, (0, 10))
            self.assertEqual(len(self.calls()), 1)
            self.assertEqual(self.calls()[0]["mode"], "CONSULT")

            code, _out, _err = self.run_cli(
                ["record", "--state", self.state_path, "--id", "A1",
                 "--status", "done", "--result", "已完成"])
            self.assertIn(code, (0, 10))
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(self.calls()[1]["mode"], "PLAN")
        self.assertEqual(SCH.summary(self.state())["pending"], 0)

    def test_task_level_off_is_inherited_by_drive(self):
        with self.channel():
            code, _out, _err = self.start_cli("--cognition", "off")
            self.assertEqual(code, 0)
            self.write_plan(1)
            self.run_cli(["plan", "--state", self.state_path,
                          "--plan", self.plan_path])
            CA.main(["drive", "--state", self.state_path])
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.obligations("A")[0]["status"], "due")
        opened = self.obligations("A")[0].get("policy_at_open") or {}
        self.assertEqual(opened.get("value"), "off")


# ── 生命周期观测：命中 / 调用 / 漏调归因 ───────────────────────────────────
def _row(oid: str, *, family: str = "A", event: str = "startup",
         status: str = "due", window: str = "open", mode: str = "CONSULT",
         **extra) -> dict:
    row = {"obligation_id": oid, "family": family, "event": event,
           "mode": mode, "instance": oid, "status": status, "window": window,
           "level": "recommended", "opened_at": "2026-01-01T00:00:00Z",
           "cycle": 1}
    row.update(extra)
    return row


class LifecycleObservabilityTests(AutoDispatchBase):
    def _state(self, obligations: list[dict]) -> dict:
        return {
            "task_id": "t-lifecycle", "goal": "g", "risk": "high",
            "task_kind": "architecture", "contract_sha256": "c",
            "contract": {"criteria": []}, "actions": [], "evidence": [],
            "cognition_calls": [{"call_id": "SC1", "mode": "CONSULT",
                                 "status": "completed"}],
            "cognition_schedule": {"schema": SCH.SCHEDULE_SCHEMA,
                                   "next_id": 9,
                                   "obligations": obligations},
        }

    def test_lifecycle_translates_obligation_states_once(self):
        state = self._state([
            _row("SO1", status="satisfied", window="closed", call_id="SC1",
                 closed_at="2026-01-01T01:00:00Z", closed_reason="executed"),
            _row("SO2", family="B", event="plan-needed", status="failed",
                 window="closed", mode="PLAN",
                 failure="cognition_quota: 429", cycle=1),
            _row("SO3", event="new-risk", status="skipped", window="closed",
                 skip_by="closure-covered"),
            _row("SO4", event="material-evidence-change", status="due",
                 window="closed", opened_by="record", closed_reason="superseded"),
            _row("SO5", status="due", opened_by="start",
                 deferrals=["lock-busy"]),
        ])
        lifecycle = SCH.task_stats(state)["lifecycle"]
        self.assertEqual(lifecycle["triggered"], 5)
        self.assertEqual(lifecycle["invoked"], 1)
        self.assertEqual(lifecycle["succeeded"], 1)
        self.assertEqual(lifecycle["failed"], 1)
        self.assertEqual(lifecycle["failed_before_request"], 1)
        self.assertEqual(lifecycle["skipped"], 1)
        self.assertEqual(lifecycle["missed"], 1)
        self.assertEqual(lifecycle["deferred"], 1)
        self.assertEqual(lifecycle["skip_reasons"], {"closure-covered": 1})
        self.assertEqual(lifecycle["missed_by_origin"], {"record": 1})

    def test_lifecycle_aggregates_across_tasks(self):
        first = self._state([
            _row("SO1", status="satisfied", window="closed", call_id="SC1"),
            _row("SO2", event="new-risk", status="due", window="closed",
                 opened_by="record"),
        ])
        second = self._state([
            _row("SO1", status="skipped", window="closed",
                 skip_by="policy-advise", deferred=None),
        ])
        aggregated = SCH.stats([first, second])["lifecycle"]
        self.assertEqual(aggregated["triggered"], 3)
        self.assertEqual(aggregated["invoked"], 1)
        self.assertEqual(aggregated["succeeded"], 1)
        self.assertEqual(aggregated["skipped"], 1)
        self.assertEqual(aggregated["missed"], 1)
        self.assertEqual(aggregated["skip_reasons"], {"policy-advise": 1})
        self.assertEqual(aggregated["missed_by_origin"], {"record": 1})

    def test_opened_by_records_which_lifecycle_command_triggered_window(self):
        self.start_state(kind="architecture")
        self.sync(policy="auto", auto=False, origin="start")
        row = self.obligations("A")[0]
        self.assertEqual(row.get("opened_by"), "start")
        pending = SCH.summary(self.state())["pending_obligations"]
        self.assertEqual(pending[0].get("opened_by"), "start")

    def test_missed_window_is_attributed_to_its_origin(self):
        self.start_state(kind="architecture")
        self.sync(policy="advise", origin="start")
        # 后一个窗口已满足 → 更早仍未执行的窗口按事实记为漏调（不是消失）。
        later = {"action": "plan", "family": "B", "event": "plan-needed",
                 "mode": "PLAN", "instance": "plan-1", "level": "recommended",
                 "stage": "planning"}
        self.sync(policy="advise", origin="plan", advice=later)
        state = self.state()
        row = [item for item in SCH.obligations(state)
               if str(item.get("family")) == "B"][0]
        SCH.settle(row, status="satisfied", now=G.now_iso(),
                   reason="executed", schedule=SCH.schedule_view(state))
        G.save_state(self.state_path, state)
        self.sync(policy="advise", origin="plan")
        stats = SCH.task_stats(self.state())
        self.assertEqual(stats["lifecycle"]["missed"], 1)
        self.assertEqual(stats["lifecycle"]["missed_by_origin"], {"start": 1})

    def test_stats_command_exposes_lifecycle(self):
        self.start_state(kind="architecture")
        self.sync(policy="advise", origin="start")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = CD.main(["cognition-stats", "--state", self.state_path])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["lifecycle"]["triggered"], 1)
        self.assertEqual(payload["lifecycle"]["invoked"], 0)


# ── 失败只记录，不阻断工程 ─────────────────────────────────────────────────
class FailureDoesNotBlockTests(AutoDispatchBase):
    def test_failed_startup_call_is_recorded_and_drive_continues(self):
        with self.channel(auto="off"):
            self.start_cli()
        with self.channel(error=CC.CognitionQuotaError("cognition_quota", "429")):
            code = CA.main(["drive", "--state", self.state_path])
        self.assertIn(code, (0, 10))
        failed = [item for item in self.calls() if item.get("status") == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].get("error"), "cognition_quota")
        row = self.obligations("A")[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(self.state().get("blocks") or [], [])
        self.assertIn(G.evaluate(self.state())["phase"], ("RUNNING", "PARTIAL"))


if __name__ == "__main__":
    unittest.main()

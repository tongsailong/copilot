#!/usr/bin/env python3
"""v8.4 SOL 默认认知引擎（THINK）契约。

覆盖本轮的验收不变量：

1. 简单任务直接执行，不强制调用 SOL；
2. 复杂任务获得一次 THINK 策略建议（命令可直接执行）；
3. 不存在固定的 CONSULT / PLAN / REVIEW 链路；
4. SOL 不承担任何工程工具操作（零工具、无状态写入权）；
5. SOL 不拥有工程执行与最终技术决策权；
6. ZCode 可以采纳、调整或否决建议，不产生强制工作项；
7. 没有新增信息时不重复调用；
8. 重大新证据可以触发必要的再咨询；
9. THINK 超时、429 与异常不阻断工程推进；
10. PARTIAL 继续派生并推进；
11. G1–G3 保持原样；
12. 不存在 G4、强制 Reviewer 或自定义子 Agent 回归。
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
import agent_policy as AP  # noqa: E402
import copilot_auto as CA  # noqa: E402
import copilot_dispatch as CD  # noqa: E402
import review_policy as RP  # noqa: E402
import sol_review as SR  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""


def think_reply(**overrides) -> str:
    payload = {
        "schema": SR.THINK_SCHEMA,
        "judgment": "先补定向测试再合并",
        "essence": "风险集中在缺少验证",
        "strategy": "一次定向测试锁定行为",
        "tradeoffs": ["灰度更慢但回滚面更小"],
        "risks": ["缺少集成测试"],
        "priority": ["P1 验证", "P2 合并"],
        "verify": ["定向测试覆盖关键分支"],
        "reconsult": ["出现并发写入"],
        "unverified": [],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def response(text: str) -> dict:
    return {
        "model": SR.MODEL,
        "status": "completed",
        "output": [{"type": "message", "status": "completed",
                    "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "_http_status": 200,
        "_request_id": "req-1",
        "_trace_id": "trace-1",
    }


class ThinkBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        self.materials = os.path.join(self.dir, "materials.md")
        with open(self.materials, "w", encoding="utf-8") as fh:
            fh.write("## 已确认事实\n- 单进程写入\n\n## 本次问题\n风险在哪？\n")

    def start(self, *, risk: str = "medium") -> None:
        G.start_state(
            self.state_path, goal="交付可复核结果", contract=CONTRACT,
            risk=risk, task_id="sol-think", workspace=self.dir,
            test_policy={"required": False} if risk == "high" else None)

    def plan(self, actions: int, *, deps: bool = False) -> None:
        items = []
        for index in range(1, actions + 1):
            item = {"id": f"A{index}", "title": f"工作{index}",
                    "goal": f"目标{index}", "expected_evidence": "证据"}
            if deps and index > 1:
                item["depends_on"] = [f"A{index - 1}"]
            items.append(item)
        G.plan_state(self.state_path, {"summary": "计划", "actions": items})

    def evidence(self, modules: int = 1, count: int | None = None) -> list[str]:
        total = count or max(modules, 1)
        state = G.load_state(self.state_path)
        ids = []
        for index in range(total):
            folder = os.path.join(self.dir, f"mod{index % max(modules, 1) + 1}")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"e{index}.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"证据{index}\n")
            eid = G.next_evidence_id(state)
            G.register_evidence(state, eid, file_path=path, note="测试证据")
            ids.append(eid)
        G.save_state(self.state_path, state)
        return ids

    def advice(self, **kwargs) -> dict:
        return G.sol_advice_summary(G.load_state(self.state_path), **kwargs)

    def cli_think(self, *extra: str):
        return F.run_cli("copilot_dispatch.py", "sol-think", "--state",
                         self.state_path, "--materials", self.materials,
                         *extra)


class RoutingTests(ThinkBase):
    def test_simple_task_stays_zero_call(self):
        self.start(risk="low")
        self.plan(1)
        self.evidence()
        advice = self.advice()
        self.assertEqual(advice["action"], "none")
        self.assertEqual(advice["mode"], "")
        evaluation = G.evaluate(G.load_state(self.state_path))
        self.assertFalse([cmd for cmd in evaluation.get("next_commands") or []
                          if "sol-" in cmd])

    def test_complex_task_gets_one_think_with_command(self):
        self.start(risk="high")
        self.plan(5, deps=True)
        self.evidence(modules=2, count=2)
        advice = self.advice()
        self.assertEqual(advice["action"], "think")
        self.assertEqual(advice["mode"], "THINK")
        self.assertEqual(advice["recommended_action"], "recommended")
        self.assertIn("sol-think", advice["command"])
        self.assertIn("--question", advice["command"])

    def test_no_fixed_consult_or_plan_chain(self):
        """CONSULT / PLAN 不再是可选命令面；旧名称只做历史别名归一。"""
        self.start(risk="high")
        self.assertEqual(SR.SOL_MODES, ("THINK", "REVIEW"))
        self.assertEqual(SR.normalize_mode("CONSULT"), "THINK")
        self.assertEqual(SR.normalize_mode("PLAN"), "THINK")
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                CD.main(["sol-advisory", "--state", self.state_path,
                         "--materials", self.materials])
        # 建议动作只有 none / reuse / think / review。
        state = G.load_state(self.state_path)
        actions = {RP.sol_advice(state)["action"] for _ in range(1)}
        self.assertTrue(actions <= {"none", "reuse", "think", "review"})


class ZeroToolAndAuthorityTests(ThinkBase):
    def test_think_payload_is_zero_tool(self):
        payload = SR.build_payload("材料", mode="THINK")
        self.assertNotIn("tools", payload)
        self.assertNotIn("functions", payload)
        self.assertEqual(payload["tool_choice"], "none")
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertIn("不能使用任何工具", payload["instructions"])
        self.assertIn("不承担工程工作", payload["instructions"])

    def test_think_output_is_structured_and_strict(self):
        parsed = SR.parse_think_output(think_reply())
        for key in ("judgment", "essence", "strategy"):
            self.assertTrue(parsed[key])
        for key in ("tradeoffs", "risks", "priority", "verify", "reconsult"):
            self.assertIsInstance(parsed[key], list)
        with self.assertRaises(SR.SolResponseError):
            SR.parse_think_output(think_reply(risks="风险"))
        with self.assertRaises(SR.SolResponseError):
            SR.parse_think_output(think_reply(judgment="", strategy=""))

    def test_decision_channel_does_not_exist(self):
        with self.assertRaises(SR.SolUsageError):
            SR.normalize_mode("DECIDE")
        self.assertNotIn("DECIDE", SR.SOL_MODES)

    def test_think_does_not_change_phase_or_gates(self):
        self.start(risk="high")
        self.plan(1)
        G.record_sol_call(self.state_path, {
            "call_id": "SCthink", "mode": "THINK", "status": "completed",
            "contract_sha256": G.load_state(self.state_path).get(
                "contract_sha256") or "",
            "judgment": "继续", "unverified": [],
            "completed_at": G.now_iso(),
        }, mode="THINK", materials_sha256="m" * 64)
        state = G.load_state(self.state_path)
        self.assertIn(G.evaluate(state)["phase"], ("RUNNING", "PARTIAL"))
        check = G.closure_check(state)
        self.assertEqual(set(check["gates"]), {"G1", "G2", "G3"})
        self.assertEqual(G.list_findings(state)["items"], [])
        # SOL 结论不是完成证据：条件仍待主代理登记真实证据。
        self.assertEqual(
            [c["status"] for c in state["contract"]["criteria"]], ["pending"])

    def test_advice_is_not_a_work_item(self):
        self.start(risk="high")
        self.plan(5, deps=True)
        self.evidence(modules=2, count=2)
        state = G.load_state(self.state_path)
        derived = [item["id"] for item in G.derive_work(state)]
        self.assertFalse([ident for ident in derived
                          if "SOL" in ident or "G4" in ident])


class RepeatAndReconsultTests(ThinkBase):
    def test_same_contract_is_reused(self):
        self.start(risk="high")
        self.plan(5, deps=True)
        self.evidence(modules=2, count=2)
        profile = self.advice()["complexity"]
        G.record_sol_call(self.state_path, {
            "call_id": "SC1", "mode": "THINK", "status": "completed",
            "contract_sha256": G.load_state(self.state_path).get(
                "contract_sha256") or "",
            "complexity_signals": profile["signal_codes"],
            "completed_at": G.now_iso(),
        }, mode="THINK", materials_sha256="m" * 64)
        advice = self.advice()
        self.assertEqual(advice["action"], "reuse")
        self.assertEqual(advice["recommended_action"], "not_needed")
        self.assertEqual(advice["new_signals"], [])

    def test_new_evidence_allows_exactly_one_more_call(self):
        self.start(risk="high")
        self.plan(3, deps=True)
        self.evidence(modules=2, count=2)
        profile = self.advice()["complexity"]
        G.record_sol_call(self.state_path, {
            "call_id": "SC1", "mode": "THINK", "status": "completed",
            "contract_sha256": G.load_state(self.state_path).get(
                "contract_sha256") or "",
            "complexity_signals": profile["signal_codes"],
            "completed_at": G.now_iso(),
        }, mode="THINK", materials_sha256="m" * 64)
        state = G.load_state(self.state_path)
        state.setdefault("actions", []).append({
            "id": "AUTO-REPLAN", "title": "重新分析根因", "goal": "替代路线",
            "assignee": "zcode", "status": "pending", "origin": "replan",
            "constraints": "", "expected_evidence": "根因分析", "depends_on": [],
            "result": "", "evidence_ids": []})
        G.save_state(self.state_path, state)
        advice = self.advice()
        self.assertEqual(advice["action"], "think")
        self.assertIn("replan-history", advice["new_signals"])
        self.assertTrue(advice["second_round"])
        self.assertIn("--force --trigger new-risk", advice["command"])


class DegradationTests(ThinkBase):
    def test_timeout_degrades_without_blocking(self):
        self.start(risk="high")
        args = argparse.Namespace(
            state=self.state_path, materials=self.materials,
            question="风险在哪？", reason="", trigger="", force=False,
            dry_run=False, list=False, json=True, timeout=1,
            max_output_tokens=100, audit_log="", output_dir="",
            knowledge_file="")
        out = io.StringIO()
        with mock.patch.object(
                SR, "run_think",
                side_effect=SR.SolTimeoutError("sol_timeout", "1s")), \
                contextlib.redirect_stdout(out):
            code = CD.cmd_sol_think(args)
        self.assertEqual(code, 0)
        degraded = json.loads(out.getvalue())
        self.assertEqual(degraded["status"], "unavailable")
        self.assertEqual(degraded["action"], "continue")
        state = G.load_state(self.state_path)
        failed = [item for item in G.iter_sol_calls(state)
                  if item.get("status") == "failed"]
        self.assertEqual(failed[-1]["mode"], "THINK")
        self.assertEqual(state.get("blocks") or [], [])
        self.assertIn(G.evaluate(state)["phase"], ("RUNNING", "PARTIAL"))

    def test_review_failure_still_fails_loudly(self):
        self.start(risk="high")
        args = argparse.Namespace(
            state=self.state_path, materials=self.materials, reason="",
            trigger="", force=False, dry_run=False, list=False, json=True,
            timeout=1, max_output_tokens=100, audit_log="", output_dir="",
            knowledge_file="")
        with mock.patch.object(
                SR, "run_review",
                side_effect=SR.SolQuotaError("sol_quota", "429")):
            self.assertEqual(CD.cmd_sol_review(args), SR.EXIT_QUOTA)

    def test_permanent_failure_does_not_block_partial_progress(self):
        self.start(risk="medium")
        state = G.load_state(self.state_path)
        state["sol_calls"] = [{
            "call_id": "SCfail", "mode": "THINK", "status": "failed",
            "error": "sol_quota", "completed_at": G.now_iso()}]
        G.save_state(self.state_path, state)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = CA.drive(argparse.Namespace(
                state=self.state_path, max_actions=1, out="", backend=""))
        self.assertEqual(code, CA.EXIT_EXECUTE)
        self.assertIn("AUTO-C1", out.getvalue())


class InputBudgetTests(ThinkBase):
    def test_think_materials_have_a_tighter_budget(self):
        self.assertLess(SR.MATERIALS_LIMITS["THINK"],
                        SR.MATERIALS_LIMITS["REVIEW"])
        with self.assertRaises(SR.SolUsageError) as ctx:
            SR.validate_materials("x" * (SR.MATERIALS_LIMITS["THINK"] + 1),
                                  mode="THINK")
        self.assertEqual(ctx.exception.code, "sol_materials_too_large")
        # REVIEW 仍接受更大的材料，但同样不是静默截断。
        SR.validate_materials("x" * 200_000, mode="REVIEW")

    def test_think_prompt_requires_curated_sections(self):
        self.start(risk="medium")
        prompt = SR.build_prompt(G.load_state(self.state_path), "材料",
                                 mode="THINK", question="风险在哪？")
        self.assertIn("## 本次需要判断的事项", prompt)
        self.assertIn("## THINK 输出要求", prompt)
        self.assertIn("不要包含完整仓库、冗长日志", prompt)
        self.assertIn("已尝试方案及结果", prompt)


class GovernanceBoundaryTests(ThinkBase):
    def test_gates_are_still_g1_g2_g3(self):
        self.start(risk="high")
        state = G.load_state(self.state_path)
        check = G.closure_check(state)
        self.assertEqual(set(check["gates"]), {"G1", "G2", "G3"})
        self.assertFalse(check["passed"])
        rendered = json.dumps(G.evaluate(state), ensure_ascii=False)
        for marker in ("g4_eligible", "host_capability", "native_review"):
            self.assertNotIn(marker, rendered)

    def test_no_mandatory_reviewer_or_custom_agent(self):
        decision = AP.dispatch_advice(
            needs_independent_context=False, parallelizable=False,
            independent_deliverable=False, write_conflict=False,
            saves_time=False, research_only=False, subagent_unavailable=False)
        self.assertEqual(decision["executor"], "main")
        self.assertFalse(os.path.exists(
            os.path.join(ROOT, ".zcode", "agents", "reviewer.md")))


if __name__ == "__main__":
    unittest.main()

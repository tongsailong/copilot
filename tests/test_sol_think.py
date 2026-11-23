#!/usr/bin/env python3
"""v9 SOL 默认认知引擎（CONSULT / PLAN，历史 THINK 别名）契约。

覆盖的核心不变量：

1. CONSULT / PLAN 是一等认知模式，THINK 只做历史别名；
2. SOL 零工具、单轮、advisory；
3. SOL 不拥有工程执行与最终技术决策权；
4. 没有新增决策价值时不重复调用；
5. 认知失败只记录并降级，不阻断工程推进；
6. G1–G3 保持原样，不存在 G4、强制 Reviewer 或自定义子 Agent 回归。
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


def cognitive_reply(**overrides) -> str:
    payload = {
        "schema": SR.THINK_SCHEMA,
        "judgment": "先补定向测试再合并",
        "goal_understanding": "交付可复核、可回归的结果",
        "essence": "风险集中在缺少验证",
        "strategy": "一次定向测试锁定行为",
        "alternatives": ["直接合并", "先灰度再合并"],
        "tradeoffs": ["灰度更慢但回滚面更小"],
        "risks": ["缺少集成测试"],
        "priority": ["P1 验证", "P2 合并"],
        "investigation": ["读取现有测试基线"],
        "phase_goals": ["第一阶段锁定行为"],
        "assumptions": ["单进程写入"],
        "omissions": ["未覆盖并发场景"],
        "decisions": ["是否接受灰度成本"],
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

    def start(self, *, risk: str = "medium", kind: str = "feature") -> None:
        G.start_state(
            self.state_path, goal="交付可复核结果", contract=CONTRACT,
            risk=risk, task_id="sol-think", workspace=self.dir,
            task_kind=kind,
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

    def cli_consult(self, *extra: str):
        return F.run_cli("copilot_dispatch.py", "sol-consult", "--state",
                         self.state_path, "--materials", self.materials,
                         "--question", "风险在哪？", *extra)


class ModeContractTests(ThinkBase):
    def test_modes_are_consult_plan_review(self):
        self.assertEqual(SR.SOL_MODES, ("CONSULT", "PLAN", "REVIEW"))
        self.assertEqual(SR.COGNITIVE_MODES, ("CONSULT", "PLAN"))
        self.assertEqual(SR.normalize_mode("THINK"), "CONSULT")
        self.assertEqual(SR.normalize_mode("consult"), "CONSULT")
        self.assertEqual(SR.normalize_mode("plan"), "PLAN")
        self.assertEqual(G.SOL_MODES, ("CONSULT", "PLAN", "REVIEW"))
        self.assertEqual(G.LEGACY_SOL_MODES, ("THINK",))

    def test_legacy_think_records_are_read_as_consult(self):
        self.start()
        state = G.load_state(self.state_path)
        state["sol_calls"] = [{
            "call_id": "SCold", "mode": "THINK", "status": "completed",
            "contract_sha256": state.get("contract_sha256"),
            "completed_at": G.now_iso()}]
        G.save_state(self.state_path, state)
        state = G.load_state(self.state_path)
        self.assertEqual(G.sol_ref_mode(state["sol_calls"][0]), "CONSULT")
        self.assertEqual(len(G.list_sol_consult_calls(state)), 1)
        self.assertEqual(len(G.list_sol_calls(state, mode="PLAN")), 0)

    def test_sol_advisory_command_is_removed(self):
        self.start()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                CD.main(["sol-advisory", "--state", self.state_path,
                         "--materials", self.materials])

    def test_mode_instructions_are_distinct_and_zero_tool(self):
        self.assertIn("咨询", SR.CONSULT_INSTRUCTIONS)
        self.assertIn("规划", SR.PLAN_INSTRUCTIONS)
        self.assertNotEqual(SR.CONSULT_INSTRUCTIONS, SR.PLAN_INSTRUCTIONS)
        for mode in ("CONSULT", "PLAN", "REVIEW"):
            payload = SR.build_payload("材料", mode=mode)
            self.assertNotIn("tools", payload)
            self.assertNotIn("functions", payload)
            self.assertEqual(payload["tool_choice"], "none")
            self.assertEqual(payload["reasoning"]["effort"], "high")
            self.assertIn("不能使用任何工具", payload["instructions"])


class ZeroToolAndAuthorityTests(ThinkBase):
    def test_payload_is_zero_tool(self):
        payload = SR.build_payload("材料", mode="CONSULT")
        self.assertNotIn("tools", payload)
        self.assertNotIn("functions", payload)
        self.assertEqual(payload["tool_choice"], "none")
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertIn("不能使用任何工具", payload["instructions"])
        self.assertIn("不承担工程工作", payload["instructions"])

    def test_output_is_structured_and_strict(self):
        parsed = SR.parse_think_output(cognitive_reply())
        for key in ("judgment", "goal_understanding", "essence", "strategy"):
            self.assertTrue(parsed[key])
        for key in ("alternatives", "tradeoffs", "risks", "priority",
                    "investigation", "phase_goals", "assumptions",
                    "omissions", "decisions", "verify", "reconsult"):
            self.assertIsInstance(parsed[key], list)
        with self.assertRaises(SR.SolResponseError):
            SR.parse_think_output(cognitive_reply(risks="风险"))
        with self.assertRaises(SR.SolResponseError):
            SR.parse_think_output(cognitive_reply(judgment="", strategy=""))

    def test_decision_channel_does_not_exist(self):
        with self.assertRaises(SR.SolUsageError):
            SR.normalize_mode("DECIDE")
        self.assertNotIn("DECIDE", SR.SOL_MODES)

    def test_cognitive_call_does_not_change_phase_or_gates(self):
        self.start(risk="high")
        self.plan(1)
        state = G.load_state(self.state_path)
        G.record_sol_call(self.state_path, {
            "call_id": "SCconsult", "mode": "CONSULT", "status": "completed",
            "contract_sha256": state.get("contract_sha256") or "",
            "judgment": "继续", "unverified": [],
            "completed_at": G.now_iso(),
        }, mode="CONSULT", materials_sha256="m" * 64)
        state = G.load_state(self.state_path)
        self.assertIn(G.evaluate(state)["phase"], ("RUNNING", "PARTIAL"))
        check = G.closure_check(state)
        self.assertEqual(set(check["gates"]), {"G1", "G2", "G3"})
        self.assertEqual(G.list_findings(state)["items"], [])
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
    def test_same_progress_reuses_consult(self):
        self.start(risk="high")
        self.plan(3, deps=True)
        self.evidence(modules=2, count=2)
        state = G.load_state(self.state_path)
        profile = self.advice()["complexity"]
        for mode, stage in (("CONSULT", "understanding"), ("PLAN", "planning")):
            G.record_sol_call(self.state_path, {
                "call_id": "SC" + mode, "mode": mode, "status": "completed",
                "stage": stage,
                "contract_sha256": state.get("contract_sha256") or "",
                "complexity_signals": profile["signal_codes"],
                "completed_at": G.now_iso(),
            }, mode=mode, materials_sha256="m" * 64)
        advice = self.advice()
        self.assertEqual(advice["action"], "reuse")
        self.assertEqual(advice["new_signals"], [])

    def test_new_evidence_allows_dynamic_consult(self):
        self.start(risk="high")
        self.plan(3, deps=True)
        self.evidence(modules=2, count=2)
        state = G.load_state(self.state_path)
        profile = self.advice()["complexity"]
        for mode, stage in (("CONSULT", "understanding"), ("PLAN", "planning")):
            G.record_sol_call(self.state_path, {
                "call_id": "SC" + mode, "mode": mode, "status": "completed",
                "stage": stage,
                "contract_sha256": state.get("contract_sha256") or "",
                "complexity_signals": profile["signal_codes"],
                "completed_at": G.now_iso(),
            }, mode=mode, materials_sha256="m" * 64)
        state = G.load_state(self.state_path)
        state.setdefault("actions", []).append({
            "id": "AUTO-REPLAN", "title": "重新分析根因", "goal": "替代路线",
            "assignee": "zcode", "status": "pending", "origin": "replan",
            "constraints": "", "expected_evidence": "根因分析", "depends_on": [],
            "result": "", "evidence_ids": []})
        G.save_state(self.state_path, state)
        advice = self.advice()
        self.assertEqual(advice["action"], "consult")
        self.assertIn("replan-history", advice["new_signals"])
        self.assertTrue(advice["second_round"])
        self.assertIn("--force --trigger new-risk", advice["command"])


class DegradationTests(ThinkBase):
    def test_timeout_degrades_without_blocking(self):
        self.start(risk="high")
        args = argparse.Namespace(
            state=self.state_path, materials=self.materials,
            question="风险在哪？", stage="", reason="", trigger="", force=False,
            dry_run=False, list=False, json=True, timeout=1,
            max_output_tokens=100, audit_log="", output_dir="",
            knowledge_file="")
        out = io.StringIO()
        with mock.patch.object(
                SR, "run_consult",
                side_effect=SR.SolTimeoutError("sol_timeout", "1s")), \
                contextlib.redirect_stdout(out):
            code = CD.cmd_sol_consult(args)
        self.assertEqual(code, 0)
        degraded = json.loads(out.getvalue())
        self.assertEqual(degraded["status"], "unavailable")
        self.assertEqual(degraded["action"], "continue")
        state = G.load_state(self.state_path)
        failed = [item for item in G.iter_sol_calls(state)
                  if item.get("status") == "failed"]
        self.assertEqual(failed[-1]["mode"], "CONSULT")
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
            "call_id": "SCfail", "mode": "CONSULT", "status": "failed",
            "error": "sol_quota", "completed_at": G.now_iso()}]
        G.save_state(self.state_path, state)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = CA.drive(argparse.Namespace(
                state=self.state_path, max_actions=1, out="", backend=""))
        self.assertEqual(code, CA.EXIT_EXECUTE)
        self.assertIn("AUTO-C1", out.getvalue())


class InputBudgetTests(ThinkBase):
    def test_cognitive_materials_are_tighter_than_review(self):
        self.assertLess(SR.MATERIALS_LIMITS["CONSULT"],
                        SR.MATERIALS_LIMITS["REVIEW"])
        self.assertLess(SR.MATERIALS_LIMITS["PLAN"],
                        SR.MATERIALS_LIMITS["REVIEW"])
        with self.assertRaises(SR.SolUsageError) as ctx:
            SR.validate_materials("x" * (SR.MATERIALS_LIMITS["CONSULT"] + 1),
                                  mode="CONSULT")
        self.assertEqual(ctx.exception.code, "sol_materials_too_large")
        SR.validate_materials("x" * 200_000, mode="REVIEW")

    def test_prompt_requires_curated_sections_and_stage(self):
        self.start(risk="medium")
        prompt = SR.build_prompt(G.load_state(self.state_path), "材料",
                                 mode="PLAN", question="方案怎么选？",
                                 stage="planning")
        self.assertIn("## 本次需要判断的事项", prompt)
        self.assertIn("## 输出要求", prompt)
        self.assertIn("阶段：planning", prompt)
        self.assertIn("不要包含完整仓库、冗长日志", prompt)
        self.assertIn("已尝试方案及结果", prompt)

    def test_stage_normalization_is_mode_aware(self):
        self.assertEqual(SR.normalize_stage("", mode="CONSULT"), "development")
        self.assertEqual(SR.normalize_stage("", mode="PLAN"), "planning")
        self.assertEqual(SR.normalize_stage("closure", mode="CONSULT"),
                         "closure")
        with self.assertRaises(SR.SolUsageError):
            SR.normalize_stage("bogus", mode="CONSULT")


class ProactiveAgendaTests(ThinkBase):
    """主动议程（可选 agenda）：只做认知提示，不是工作项、不影响契约与门禁。"""

    def test_agenda_is_optional_and_normalized(self):
        parsed = SR.parse_think_output(cognitive_reply())
        self.assertEqual(parsed["agenda"], [])
        parsed = SR.parse_think_output(cognitive_reply(agenda=[
            {"kind": "wrong-premise", "text": "假设单进程写入可能不成立"},
            "最该先验证并发写入",
            {"kind": "missing-risk", "text": "假设单进程写入可能不成立"},
            {"kind": "unknown-kind", "text": "未知类型归入核心问题"},
        ]))
        self.assertEqual([item["kind"] for item in parsed["agenda"]],
                         ["wrong-premise", "core-question", "missing-risk",
                          "core-question"])
        self.assertEqual(parsed["agenda"][0]["label"],
                         RP.AGENDA_KINDS["wrong-premise"])
        with self.assertRaises(SR.SolResponseError):
            SR.parse_think_output(cognitive_reply(agenda="不是数组"))

    def test_agenda_is_bounded(self):
        parsed = SR.parse_think_output(cognitive_reply(agenda=[
            {"kind": "verify", "text": f"验证项 {index}"} for index in range(12)
        ]))
        self.assertEqual(len(parsed["agenda"]), RP.AGENDA_MAX_ITEMS)
        long_text = SR.parse_think_output(cognitive_reply(
            agenda=[{"kind": "verify", "text": "长" * 500}]))["agenda"][0]["text"]
        self.assertEqual(len(long_text), RP.AGENDA_TEXT_LIMIT)

    def test_prompt_carries_prior_judgment_and_open_agenda(self):
        self.start()
        self.plan(2)
        state = G.load_state(self.state_path)
        G.record_sol_call(self.state_path, {
            "call_id": "SCprior", "mode": "CONSULT", "status": "completed",
            "stage": "development", "judgment": "先验证并发写入再合并",
            "agenda": [{"kind": "missing-risk", "text": "并发写入未验证"}],
            "sol_event_snapshot": RP.sol_event_snapshot(state),
            "completed_at": G.now_iso(),
        }, mode="CONSULT")
        state = G.load_state(self.state_path)
        prompt = SR.build_prompt(state, "材料", mode="CONSULT",
                                 question="下一步做什么？", stage="development")
        self.assertIn("历史 SOL 认知", prompt)
        self.assertIn("先验证并发写入再合并", prompt)
        self.assertIn("并发写入未验证", prompt)
        # 历史判断只是只读事实，不能伪装成本轮结论或改变输出契约。
        self.assertIn("不是本轮结论", prompt)
        self.assertIn("agenda", SR.CONSULT_INSTRUCTIONS)


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

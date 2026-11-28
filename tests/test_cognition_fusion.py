#!/usr/bin/env python3
"""v8.2 SOL 与 G1–G3 融合契约。

覆盖本轮融合的核心不变量：

1. 三模式共用一份记录流（`cognition_calls`）、一份审计与一把锁；历史键只读兼容。
2. SOL 与门禁共享同一份合同、完成条件、工作项与证据登记。
3. 确定性优先：门禁仍有缺口时准入建议是 `not_needed`，不调用模型。
4. SOL 结论是 advisory：不改变阶段、不改变门禁、失败不停摆。
5. 单轮复用：同一主体摘要不重复付费；合同/证据/提示词版本变化即失效。
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
import cognition_call as CC  # noqa: E402  v6_support 已把 scripts/ 加入 sys.path
import copilot_dispatch as CD  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""


def review_record(sha: str = "a" * 64, **overrides) -> dict:
    record = {
        "schema": CC.REVIEW_SCHEMA,
        "model": CC.MODEL,
        "requested_model": CC.MODEL,
        "actual_model": CC.MODEL,
        "reasoning_effort": "high",
        "materials_sha256": "m" * 64,
        "reply_sha256": sha,
        "summary": "结论",
        "verdict": "pass",
        "findings": [],
        "findings_count": {"blocking": 0, "major": 0, "minor": 0, "info": 0},
        "unverified": [],
        "prompt_version": CC.PROMPT_VERSION,
        "completed_at": G.now_iso(),
    }
    record.update(overrides)
    return record


def think_record(mode: str = "THINK", **overrides) -> dict:
    record = {
        "schema": CC.THINK_SCHEMA,
        "mode": mode,
        "model": CC.MODEL,
        "summary": "判断",
        "judgment": "先补测试",
        "essence": "缺少验证",
        "strategy": "先测后改",
        "tradeoffs": [],
        "risks": [],
        "priority": [],
        "verify": [],
        "reconsult": [],
        "unverified": [],
        "prompt_version": CC.PROMPT_VERSION,
        "completed_at": G.now_iso(),
    }
    record.update(overrides)
    return record


class FusionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="目标：交付可复核结果",
                      contract=CONTRACT, risk="high", task_id="fusion-task",
                      workspace=self.dir,
                      test_policy={"required": False})

    def evidence(self, name: str = "evidence.txt", body: str = "结果\n") -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        state = G.load_state(self.state_path)
        eid = G.next_evidence_id(state)
        G.register_evidence(state, eid, file_path=path, note="测试证据")
        G.save_state(self.state_path, state)
        return eid

    def plan_and_close_actions(self) -> None:
        G.plan_state(self.state_path, {"summary": "两个工作项", "actions": [
            {"id": "A1", "title": "实现", "goal": "产出结果",
             "expected_evidence": "结果文件"},
            {"id": "A2", "title": "验证", "goal": "补充测试",
             "expected_evidence": "测试输出"},
        ]})
        eid = self.evidence()
        G.record_action(self.state_path, "A1", status="done", result="实现",
                        evidence_ids=[eid])
        G.record_action(self.state_path, "A2", status="done", result="测试",
                        evidence_ids=[eid])
        state = G.load_state(self.state_path)
        for criterion in state["contract"]["criteria"]:
            G.record_criterion(self.state_path, criterion["criterion_id"],
                               "met", evidence_ids=[eid])


class UnifiedRecordTests(FusionBase):
    def test_all_modes_share_one_record_stream(self):
        G.record_cognition_call(self.state_path, review_record(),
                          mode="REVIEW", materials_sha256="m" * 64)
        G.record_cognition_call(self.state_path, think_record("THINK"),
                          mode="THINK", materials_sha256="c" * 64)
        # 历史 CONSULT / PLAN 记录仍可读、写入仍兼容旧调用方。
        G.record_cognition_call(self.state_path, think_record("PLAN"),
                          mode="PLAN", materials_sha256="p" * 64)
        G.record_cognition_call_failure(self.state_path, mode="THINK",
                                  error_code="cognition_timeout", detail="300s",
                                  materials_sha256="x" * 64)
        state = G.load_state(self.state_path)
        self.assertEqual(len(state["cognition_calls"]), 4)
        # 不再写历史键：一份记录、一套口径。
        self.assertNotIn("sol_reviews", state)
        self.assertNotIn("sol_advisories", state)
        self.assertEqual(len(G.list_cognition_reviews(state)), 1)
        self.assertEqual(len(G.list_cognition_consult_calls(state)), 2)
        self.assertEqual(len(G.list_cognition_plan_calls(state)), 1)
        metrics = G.review_metrics(state)
        self.assertEqual(metrics["cognition_reviews"]["calls_total"], 4)
        # CONSULT / PLAN / REVIEW 各自独立计数；历史 THINK 归入 CONSULT。
        self.assertEqual(metrics["cognition_reviews"]["by_mode"],
                         {"CONSULT": 2, "PLAN": 1, "REVIEW": 1})
        self.assertEqual(metrics["cognition"]["consult_total"], 2)
        self.assertEqual(metrics["cognition"]["plan_total"], 1)
        self.assertEqual(metrics["cognition"]["failed"], 1)

    def test_record_ids_are_unified(self):
        record = G.record_cognition_call(self.state_path, think_record("THINK"),
                                   mode="THINK")
        self.assertTrue(record["call_id"].startswith("SC"))
        self.assertEqual(record["call_id"], G.cognition_call_id(record))
        self.assertEqual(G.cognition_call_mode(record), "THINK")
        legacy = G.record_cognition_call(self.state_path,
                                   think_record("PLAN"),
                                   mode="PLAN")
        self.assertEqual(G.cognition_call_mode(legacy), "PLAN")
        self.assertEqual(G.cognition_ref_mode(legacy), "PLAN")

    def test_legacy_records_stay_readable(self):
        """v8.1 及更早的任务状态只读兼容：不迁移、不改写、不重复计数。"""
        state = G.load_state(self.state_path)
        state["sol_reviews"] = [{
            "status": "completed", "review_id": "SRlegacy", "verdict": "partial",
            "materials_sha256": "l" * 64, "findings_count": {"major": 1},
            "created_at": "2026-11-01T00:00:00+08:00"}]
        state["sol_advisories"] = [{
            "status": "completed", "advisory_id": "SAlegacy", "mode": "PLAN",
            "materials_sha256": "o" * 64,
            "created_at": "2026-11-02T00:00:00+08:00"}]
        merged = G.iter_cognition_calls(state)
        self.assertEqual([G.cognition_call_id(item) for item in merged],
                         ["SRlegacy", "SAlegacy"])
        self.assertEqual(G.cognition_call_mode(merged[0]), "REVIEW")
        self.assertEqual(G.cognition_call_mode(merged[1]), "PLAN")
        self.assertEqual(G.cognition_ref_mode(merged[1]), "PLAN")
        self.assertEqual(len(G.list_cognition_reviews(state)), 1)
        self.assertEqual(len(G.list_cognition_consult_calls(state)), 0)
        self.assertEqual(len(G.list_cognition_plan_calls(state)), 1)
        metrics = G.review_metrics(state)
        self.assertEqual(metrics["cognition_reviews"]["calls_total"], 2)
        self.assertEqual(metrics["cognition_reviews"]["verdicts"]["partial"], 1)
        admission = G.review_admission(state)
        self.assertEqual(admission["cognition_calls_total"], 2)
        # 历史 PLAN 记录按 PLAN 计入覆盖：任何已有认知结论都不重复付费。
        self.assertEqual(admission["cognition_calls_by_mode"]["PLAN"], 1)
        # 历史记录仍可读、仍计入覆盖，但旧 SOL 结论没有模型归属，不能被当成
        # Astra 的新响应复用：同一材料在当前默认模型下重新判定为首次调用。
        legacy = merged[0]
        decision = CC.single_round_decision(
            state, legacy["materials_sha256"], mode="REVIEW")
        self.assertEqual(decision["action"], "call")

    def test_same_id_in_both_stores_is_counted_once(self):
        state = {
            "cognition_calls": [{"call_id": "SC1", "mode": "REVIEW",
                           "status": "completed"}],
            "sol_reviews": [{"review_id": "SC1", "status": "completed"}],
        }
        self.assertEqual(len(G.iter_cognition_calls(state)), 1)

    def test_think_never_enters_finding_ledger_or_gates(self):
        G.record_cognition_call(self.state_path, think_record("THINK"),
                          mode="THINK")
        G.record_cognition_call(self.state_path,
                          review_record(findings=[{
                              "id": "F1", "severity": "major",
                              "file": "a.py", "line": 1, "summary": "可疑"}],
                              verdict="partial"),
                          mode="REVIEW", materials_sha256="m" * 64)
        state = G.load_state(self.state_path)
        ledger = G.list_findings(state)
        self.assertEqual([item["finding_id"] for item in ledger["items"]],
                         ["F1"])
        self.assertEqual(ledger["items"][0]["authority"], "advisory")


class SharedContextTests(FusionBase):
    def test_prompt_injects_contract_criteria_actions_and_evidence(self):
        eid = self.evidence()
        G.plan_state(self.state_path, {"summary": "一个工作项", "actions": [
            {"id": "A1", "title": "实现", "goal": "产出结果",
             "expected_evidence": "结果文件"}]})
        state = G.load_state(self.state_path)
        cid = state["contract"]["criteria"][0]["criterion_id"]
        G.record_criterion(self.state_path, cid, "met", evidence_ids=[eid])
        state = G.load_state(self.state_path)
        prompt = CC.build_prompt(state, "本次 Diff 与测试结果", mode="REVIEW")
        self.assertIn(f"- 提示词版本：{CC.PROMPT_VERSION}", prompt)
        self.assertIn("## 任务目标", prompt)
        self.assertIn(cid, prompt)
        self.assertIn("## 工作项（按任务状态自动注入）", prompt)
        self.assertIn("- A1 [pending] 实现", prompt)
        self.assertIn("## 证据登记（按任务状态自动注入）", prompt)
        self.assertIn(eid, prompt)
        self.assertIn(f"支持 {cid}", prompt)
        self.assertIn("本次 Diff 与测试结果", prompt)
        think = CC.build_prompt(state, "材料", mode="CONSULT",
                                question="关键风险是什么？")
        self.assertIn("## 本次需要判断的事项", think)
        self.assertIn("关键风险是什么？", think)
        self.assertIn("## 输出要求", think)
        self.assertIn("不要包含完整仓库、冗长日志", think)

    def test_evidence_injection_is_bounded(self):
        state = G.load_state(self.state_path)
        for index in range(140):
            state["evidence"].append({
                "id": f"E{index + 1}", "kind": "file",
                "sha256": f"{index:064d}", "size": 10,
                "path_hint": f"/tmp/e{index}.txt", "dropped": False})
        prompt = CC.build_prompt(state, "材料", mode="REVIEW")
        self.assertIn("条证据已省略", prompt)
        self.assertLess(prompt.count("- E"), 141)

    def test_subject_key_covers_contract_evidence_prompt_version(self):
        self.evidence()
        state = G.load_state(self.state_path)
        materials = "材料正文"

        def subject(**changes):
            probe = json.loads(json.dumps(state))
            for key, value in changes.items():
                probe[key] = value
            return CC.review_subject_sha256(probe, materials, mode="REVIEW")

        base = subject()
        # 条件状态变化不是新审查对象。
        status_changed = json.loads(json.dumps(state))
        status_changed["contract"]["criteria"][0]["status"] = "met"
        self.assertEqual(base, CC.review_subject_sha256(
            status_changed, materials, mode="REVIEW"))
        # 证据集合变化即失效。
        evidence_changed = json.loads(json.dumps(state))
        evidence_changed["evidence"][0]["sha256"] = "f" * 64
        self.assertNotEqual(base, CC.review_subject_sha256(
            evidence_changed, materials, mode="REVIEW"))
        # 提示词版本变化即失效（提示词/策略升级后旧结论不复用）。
        with mock.patch.object(CC, "PROMPT_VERSION", "sol-prompt-next"):
            self.assertNotEqual(base, subject())
        # 合同正文、目标与材料变化即失效。
        contract_changed = json.loads(json.dumps(state))
        contract_changed["contract"]["done_criteria_text"] += "\n新增边界"
        self.assertNotEqual(base, CC.review_subject_sha256(
            contract_changed, materials, mode="REVIEW"))
        self.assertNotEqual(base, subject(goal="另一个目标"))
        self.assertNotEqual(base, CC.review_subject_sha256(
            state, materials + "+", mode="REVIEW"))

    def test_dry_run_reports_cache_key_components(self):
        mat = os.path.join(self.dir, "materials.md")
        with open(mat, "w", encoding="utf-8") as fh:
            fh.write("材料\n")
        proc = F.run_cli("copilot_dispatch.py", "cognition-review", "--state",
                         self.state_path, "--materials", mat,
                         "--dry-run", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["prompt_version"], CC.PROMPT_VERSION)
        self.assertIn("evidence_digest", payload["cache_key_components"])
        self.assertEqual(payload["mode"], "REVIEW")
        self.assertTrue(payload["zero_tool"])


class DeterministicFirstTests(FusionBase):
    def test_admission_reports_consult_until_gates_pass(self):
        state = G.load_state(self.state_path)
        decision = G.review_admission(state)
        self.assertEqual(decision["decision"], "recommended")  # 风险策略
        self.assertEqual(decision["recommended_action"], "recommended")
        self.assertEqual(decision["recommended_mode"], "CONSULT")
        self.assertFalse(decision["gates"]["passed"])
        self.assertTrue(decision["gates"]["codes"]["G2"])

    def test_admission_returns_closure_consult_after_gates_pass(self):
        """收口阶段不再自动建议 REVIEW：高风险在未认知时给一次收口 CONSULT。"""
        self.plan_and_close_actions()
        state = G.load_state(self.state_path)
        self.assertTrue(G.closure_check(state)["passed"])
        decision = G.review_admission(state)
        self.assertEqual(decision["recommended_action"], "recommended")
        self.assertEqual(decision["recommended_mode"], "CONSULT")
        self.assertEqual(decision["recommended_stage"], "closure")
        self.assertTrue(decision["gates"]["passed"])

    def test_low_risk_task_needs_no_sol(self):
        state = G.load_state(self.state_path)
        state["risk"] = "low"
        state["task_kind"] = "trivial"
        decision = G.review_admission(state)
        self.assertEqual(decision["recommended_action"], "not_needed")

    def test_cognition_call_pass_does_not_change_gates_or_phase(self):
        G.record_cognition_call(self.state_path,
                          review_record(findings=[], verdict="pass"),
                          mode="REVIEW", materials_sha256="m" * 64)
        state = G.load_state(self.state_path)
        self.assertEqual(G.evaluate(state)["phase"], "PARTIAL")
        check = G.closure_check(state)
        self.assertFalse(check["passed"])
        self.assertEqual(set(check["gates"]), {"G1", "G2", "G3"})

    def test_cognition_failure_is_recorded_and_does_not_stop_the_task(self):
        args = argparse.Namespace(
            state=self.state_path, materials=self._materials(), reason="",
            trigger="", force=False, dry_run=False, list=False, json=True,
            timeout=5, max_output_tokens=100, audit_log="", output_dir="")
        with mock.patch.object(
                CC, "run_review",
                side_effect=CC.CognitionTimeoutError("cognition_timeout", "300s")):
            code = CD.cmd_cognition_review(args)
        self.assertEqual(code, CC.EXIT_TIMEOUT)
        state = G.load_state(self.state_path)
        failed = [item for item in G.iter_cognition_calls(state)
                  if item.get("status") == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["error"], "cognition_timeout")
        self.assertEqual(failed[0]["mode"], "REVIEW")
        self.assertEqual(G.evaluate(state)["phase"], "PARTIAL")
        # 失败不会产生阻塞，也不会派生 SOL/复核类工作项（只有普通缺口派生）。
        self.assertEqual(state.get("blocks") or [], [])
        derived = [item["id"] for item in G.derive_work(state)]
        self.assertTrue(all(ident.startswith(("AUTO-C", "AUTO-R-", "AUTO-FIX-",
                                             "AUTO-VERIFY-", "AUTO-NEXT",
                                             "AUTO-REPLAN"))
                            for ident in derived), derived)

    def test_think_failure_degrades_without_stopping_the_task(self):
        """THINK 是默认认知引擎：429/超时只记录并降级，进程返回 0 让主代理继续。"""
        args = argparse.Namespace(
            state=self.state_path, materials=self._materials(),
            question="下一步？", reason="", trigger="", force=False,
            dry_run=False, list=False, json=True, timeout=5,
            max_output_tokens=100, audit_log="", output_dir="",
            knowledge_file="")
        out = io.StringIO()
        with mock.patch.object(
                CC, "run_consult",
                side_effect=CC.CognitionQuotaError("cognition_quota", "429")), \
                contextlib.redirect_stdout(out):
            code = CD.cmd_cognition_consult(args)
        self.assertEqual(code, 0)
        degraded = json.loads(out.getvalue())
        self.assertEqual(degraded["status"], "unavailable")
        self.assertEqual(degraded["action"], "continue")
        state = G.load_state(self.state_path)
        failed = [item for item in G.iter_cognition_calls(state)
                  if item.get("status") == "failed"]
        self.assertEqual(failed[0]["mode"], "CONSULT")
        self.assertEqual(failed[0]["error"], "cognition_quota")
        self.assertEqual(G.evaluate(state)["phase"], "PARTIAL")
        self.assertEqual(state.get("blocks") or [], [])

    def _materials(self) -> str:
        path = os.path.join(self.dir, "materials.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("## Diff\n+ x = 1\n")
        return path


class SingleRoundReuseTests(FusionBase):
    def test_same_subject_never_calls_twice(self):
        materials = "材料正文"
        state = G.load_state(self.state_path)
        digest = CC.review_subject_sha256(state, materials, mode="REVIEW")
        G.record_cognition_call(self.state_path,
                          review_record(materials_sha256=digest),
                          mode="REVIEW", materials_sha256=digest)
        args = argparse.Namespace(
            state=self.state_path, materials=self._materials_file(materials),
            reason="", trigger="", force=False, dry_run=False, list=False,
            json=True, timeout=5, max_output_tokens=100, audit_log="",
            output_dir="")
        out = io.StringIO()
        with mock.patch.object(
                CC, "run_review",
                side_effect=AssertionError("must not call")) as call, \
                contextlib.redirect_stdout(out):
            self.assertEqual(CD.cmd_cognition_review(args), 0)
        self.assertEqual(call.call_count, 0)
        self.assertIn("call_id", json.loads(out.getvalue()))

    def _materials_file(self, body: str) -> str:
        path = os.path.join(self.dir, "materials.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path


class RecordPathTests(FusionBase):
    def test_modes_share_audit_file_and_lock(self):
        results = {
            "REVIEW": review_record(reply_text="{}"),
            "CONSULT": think_record("CONSULT", reply_text="{}"),
        }

        audit_paths: list[str] = []

        def fake(mode: str):
            def call(materials, *args, **kwargs):
                audit_paths.append(str(kwargs.get("audit_log") or ""))
                return {**results[mode], "materials_sha256": "m" * 64,
                        "materials_chars": len(materials),
                        "request_sha256": "r" * 64, "reply_chars": 2,
                        "usage": {}, "usage_available": True, "latency_ms": 1,
                        "audit_written": True, "audit_error": "",
                        "completed_at": G.now_iso()}
            return call

        with mock.patch.object(CC, "run_review", side_effect=fake("REVIEW")), \
                mock.patch.object(CC, "run_consult", side_effect=fake("CONSULT")):
            self.assertEqual(CD.main([
                "cognition-review", "--state", self.state_path, "--materials",
                self._materials()]), 0)
            self.assertEqual(CD.main([
                "cognition-consult", "--state", self.state_path, "--materials",
                self._materials(), "--question", "下一步怎么走？"]), 0)
        self.assertEqual(len(audit_paths), 2)
        self.assertTrue(all(path.endswith("cognition-audit.jsonl")
                            for path in audit_paths), audit_paths)
        state = G.load_state(self.state_path)
        self.assertEqual(sorted(item["mode"] for item in state["cognition_calls"]),
                         ["CONSULT", "REVIEW"])
        ids = [item["call_id"] for item in state["cognition_calls"]]
        self.assertTrue(all(ident.startswith("SC") for ident in ids))
        # 原始输出文件名必须与统一记录 ID 一致（可按 call_id 直接定位取证）。
        for item in state["cognition_calls"]:
            self.assertEqual(os.path.basename(item["reply_path"]),
                             item["call_id"] + ".raw.txt")

    def _materials(self) -> str:
        path = os.path.join(self.dir, "materials.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("材料\n")
        return path


if __name__ == "__main__":
    unittest.main()

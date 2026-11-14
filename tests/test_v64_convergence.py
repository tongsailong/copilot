#!/usr/bin/env python3
"""v6.4 收敛回归：BLOCKED 受控写入、复核顺序与正式 REVIEW amendment。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import copilot_auto as AC  # noqa: E402
import governance as G  # noqa: E402
import native_agent as NA  # noqa: E402
import v6_support as F  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件

## 边界
- 不修改无关项目
"""


class BlockedWritePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state = os.path.join(self.dir, "state.json")
        G.start_state(self.state, goal="目标", contract=CONTRACT)
        G.plan_state(self.state, {"summary": "实现", "actions": [
            {"id": "A1", "title": "实现", "goal": "产出结果",
             "expected_evidence": "结果文件"},
        ]})
        self.evidence = os.path.join(self.dir, "evidence.txt")
        with open(self.evidence, "w", encoding="utf-8") as fh:
            fh.write("result\n")
        state = G.load_state(self.state)
        G.register_evidence(state, "E1", file_path=self.evidence)
        G.save_state(self.state, state)
        G.add_block(self.state, "缺少用户授权", "用户授权或取消")

    def test_blocked_allows_factual_progress_records(self):
        G.record_action(self.state, "A1", "done", result="完成",
                        evidence_ids=["E1"])
        G.record_criterion(self.state, "C1", "met", evidence_ids=["E1"])
        G.record_judgment(self.state, "partial", "仍等待用户授权")
        G.add_report(self.state, "阻塞期间已完成实现", blocked=True,
                     reason="等待用户授权")
        G.add_block(self.state, "第二个真实阻塞", "外部资源")
        state = G.load_state(self.state)
        self.assertEqual(G.evaluate(state)["phase"], "BLOCKED")
        self.assertEqual(len([b for b in state["blocks"] if not b.get("resolved_at")]), 2)
        self.assertEqual(state["actions"][0]["status"], "done")
        self.assertEqual(state["contract"]["criteria"][0]["status"], "met")
        self.assertTrue(state["reports"][0]["blocked"])

    def test_multiple_blocks_require_explicit_block_id(self):
        first = G.load_state(self.state)["blocks"][0]["block_id"]
        second = G.add_block(self.state, "第二个真实阻塞", "外部资源")["block_id"]
        with self.assertRaises(G.GovernanceError):
            G.resolve_block(self.state, "一条笼统理由")
        G.resolve_block(self.state, "第一条已满足", block_id=first)
        state = G.load_state(self.state)
        active = [b for b in state["blocks"] if not b.get("resolved_at")]
        self.assertEqual([b["block_id"] for b in active], [second])
        self.assertEqual(G.evaluate(state)["phase"], "BLOCKED")
        G.resolve_block(self.state, "第二条已满足", block_id=second)
        self.assertNotEqual(G.evaluate(G.load_state(self.state))["phase"], "BLOCKED")

    def test_blocked_allows_formal_review_registration_with_authorization(self):
        G.authorize_formal_review(self.state, authorized_by="user",
                                  reason="用户要求在阻塞期间登记复核")
        record = G.record_formal_review(self.state, verdict="pass",
                                        summary="阻塞期间的正式复核结论")
        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(G.evaluate(G.load_state(self.state))["phase"], "BLOCKED")

    def test_blocked_still_rejects_scope_changes_and_destruction(self):
        with self.assertRaises(G.GovernanceError):
            G.plan_state(self.state, {"summary": "扩大范围", "actions": [
                {"id": "A9", "title": "新工作", "goal": "g",
                 "expected_evidence": "e"},
            ]})
        with self.assertRaises(G.GovernanceError):
            G.add_decision(self.state, "自行改变优先级")
        state = G.load_state(self.state)
        with self.assertRaises(G.GovernanceError):
            G.drop_evidence(state, "E1", "尝试删除证据")

    def test_blocked_cannot_close_and_does_not_auto_unblock(self):
        G.record_action(self.state, "A1", "done", result="完成",
                        evidence_ids=["E1"])
        G.record_criterion(self.state, "C1", "met", evidence_ids=["E1"])
        with self.assertRaises(G.GovernanceError):
            G.close_task(self.state)
        self.assertEqual(G.evaluate(G.load_state(self.state))["phase"], "BLOCKED")


class FormalReviewAmendmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state.json")
        G.start_state(self.state, goal="目标", contract=CONTRACT)

    def _authorize(self):
        G.authorize_formal_review(self.state, authorized_by="user",
                                  reason="用户要求正式复核")

    def test_amendment_under_blocked_keeps_original_and_verdict(self):
        self._authorize()
        record = G.record_formal_review(self.state, verdict="pass",
                                        summary="复核通过")
        G.add_block(self.state, "外部资源未就绪", "等待资源")
        result = G.record_formal_review(
            self.state, amend=record["review_id"],
            review_ref="docs/review-v2.md",
            reason="仅更正报告引用，结论未变")
        self.assertEqual(result["review_id"], record["review_id"])
        self.assertEqual(result["verdict"], "pass")
        state = G.load_state(self.state)
        self.assertEqual(len(state["formal_reviews"]), 1)
        self.assertEqual(len(state["formal_reviews"][0]["amendments"]), 1)
        self.assertEqual(G.evaluate(state)["phase"], "BLOCKED")

    def test_amendment_requires_reason_and_no_verdict(self):
        self._authorize()
        record = G.record_formal_review(self.state, verdict="pass",
                                        summary="复核通过")
        with self.assertRaises(G.GovernanceError):
            G.record_formal_review(self.state, amend=record["review_id"],
                                   review_ref="x")
        with self.assertRaises(G.GovernanceError):
            G.record_formal_review(self.state, amend=record["review_id"],
                                   verdict="pass", reason="重复裁决")

    def test_amendment_rejects_changed_target(self):
        self._authorize()
        record = G.record_formal_review(self.state, verdict="pass",
                                        summary="复核通过")
        state = G.load_state(self.state)
        G.register_evidence(state, "E1", text="new evidence")
        G.save_state(self.state, state)
        with self.assertRaises(G.GovernanceError):
            G.record_formal_review(self.state, amend=record["review_id"],
                                   review_ref="E1", reason="证据已变化")

    def test_combined_amend_and_supersede_rejected(self):
        self._authorize()
        record = G.record_formal_review(self.state, verdict="pass",
                                        summary="复核通过")
        with self.assertRaises(G.GovernanceError):
            G.record_formal_review(
                self.state, amend=record["review_id"],
                supersedes=record["review_id"], verdict="pass",
                summary="重复语义", reason="不应同时使用")

    def test_supersede_requires_reason(self):
        self._authorize()
        record = G.record_formal_review(self.state, verdict="pass",
                                        summary="复核通过")
        with self.assertRaises(G.GovernanceError):
            G.record_formal_review(
                self.state, verdict="pass", summary="新结论",
                supersedes=record["review_id"])

    def test_blocked_target_change_amendment_rejected(self):
        self._authorize()
        record = G.record_formal_review(self.state, verdict="pass",
                                        summary="复核通过")
        G.add_block(self.state, "等待外部资源", "资源")
        state = G.load_state(self.state)
        G.register_evidence(state, "E9", text="blocked new evidence")
        G.save_state(self.state, state)
        with self.assertRaises(G.GovernanceError):
            G.record_formal_review(
                self.state, amend=record["review_id"],
                review_ref="E9", reason="阻塞期间证据变化")

    def test_metrics_ignore_records_without_review_id(self):
        metrics = G.review_metrics({"formal_reviews": [{"verdict": "pass"}]})
        self.assertEqual(metrics["formal_reviews_total"], 1)
        self.assertEqual(metrics["superseded_formal_reviews_total"], 0)
        self.assertEqual(metrics["active_formal_reviews_total"], 1)

    def test_supersede_links_new_record_and_metrics(self):
        self._authorize()
        old = G.record_formal_review(self.state, verdict="pass",
                                     summary="旧证据通过")
        state = G.load_state(self.state)
        G.register_evidence(state, "E1", text="new evidence")
        G.save_state(self.state, state)
        new = G.record_formal_review(
            self.state, verdict="pass", summary="新证据通过",
            supersedes=old["review_id"], reason="证据实质变化")
        self.assertEqual(new["supersedes"], old["review_id"])
        metrics = G.review_metrics(G.load_state(self.state))
        self.assertEqual(metrics["formal_reviews_total"], 2)
        self.assertEqual(metrics["superseded_formal_reviews_total"], 1)
        self.assertEqual(metrics["active_formal_reviews_total"], 1)


class ReviewContextWarningTests(F.BridgeBase):
    def setUp(self):
        super().setUp()
        state = G.load_state(self.state_path)
        state["native_dispatch"] = self.dispatch
        G.save_state(self.state_path, state)
        self.execution_env = self.execution()
        self.execution_path = os.path.join(self.dir, "execution.json")
        with open(self.execution_path, "w", encoding="utf-8") as fh:
            json.dump(self.execution_env, fh, ensure_ascii=False)
        self.dispatch_path = os.path.join(self.dir, "dispatch.json")
        with open(self.dispatch_path, "w", encoding="utf-8") as fh:
            json.dump(self.dispatch, fh, ensure_ascii=False)
        AC._ingest_execution(self.state_path, self.execution_env)

    def args(self, **overrides):
        base = {"state": self.state_path, "dispatch": self.dispatch_path,
                "execution": self.execution_path, "work_item": "A1",
                "previous_review": "", "workspace": self.dir}
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_previous_pass_target_change_warns_but_does_not_auto_reuse(self):
        attempt = self.create_attempt(self.execution_env)
        host = F.make_fake_review_host(
            self.dir, execution=self.execution_env, dispatch=self.dispatch,
            evidence=self.evidence, task_id=self.state["task_id"],
            attempt_id=attempt["review_attempt_id"],
            session_created_at=attempt["created_at"])
        env = self.review(self.execution_env, attempt=attempt, host=host)
        AC._ingest_review(self.state_path, env)
        review_path = os.path.join(self.dir, "previous.json")
        with open(review_path, "w", encoding="utf-8") as fh:
            json.dump(env, fh, ensure_ascii=False)
        state = G.load_state(self.state_path)
        G.register_evidence(state, "E2", text="post-pass evidence")
        G.save_state(self.state_path, state)
        payload = NA.build_review_context(
            self.args(previous_review=review_path))
        self.assertTrue(any("上轮 PASS 绑定的是旧目标摘要" in w
                            for w in payload["warnings"]))
        self.assertTrue(any("minor/info" in w for w in payload["warnings"]))
        # 只提示，不擅自把旧 PASS 当成新摘要的结论。
        self.assertNotEqual(payload["target_sha256"],
                            env["target_sha256"])


class ReviewOrderTests(unittest.TestCase):
    def test_auto_g4_work_order_requires_final_evidence_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state.json")
            G.start_state(state, goal="目标", contract=CONTRACT,
                          execution_backend="native", risk="high")
            G.plan_state(state, {"summary": "实现", "actions": [
                {"id": "A1", "title": "实现", "goal": "产出结果",
                 "expected_evidence": "结果文件"},
            ]})
            evidence = os.path.join(tmp, "evidence.txt")
            with open(evidence, "w", encoding="utf-8") as fh:
                fh.write("result\n")
            data = G.load_state(state)
            G.register_evidence(data, "E1", file_path=evidence)
            G.save_state(state, data)
            G.record_action(state, "A1", "done", result="完成",
                            evidence_ids=["E1"])
            G.record_criterion(state, "C1", "met", evidence_ids=["E1"])
            created = G.derive_work(G.load_state(state))
            g4 = next(a for a in created if a["id"].startswith("AUTO-G4"))
            self.assertIn("先", g4["goal"])
            self.assertIn("最终", g4["goal"])
            self.assertIn("review-context", g4["goal"])
            self.assertIn("PASS 后", g4["goal"])
            self.assertIn("residual_risks", g4["goal"])


if __name__ == "__main__":
    unittest.main()

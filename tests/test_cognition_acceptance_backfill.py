#!/usr/bin/env python3
"""历史采纳回填入口（--historical）的边界与幂等回归。

覆盖 v9.7.1 的最小改进：

1. 已收口任务**只能**走历史通道：正常入口仍拒绝 CLOSED，不放开原有约束。
2. 历史通道只写 `cognition_calls[*].acceptance` 与历史事件，不改 verdict / closure /
   条件 / 门禁。
3. `pending` 只在此通道可用且必须写明原因：用于“复核过但无法用任务记录证明
   采纳”的调用，不为了消除 pending 而降低证据要求。
4. 已决结论不被覆盖、不被降级为 pending；完全相同的重复执行幂等返回
   `duplicate`，不追加历史、不改登记次数。
"""
from __future__ import annotations

import os
import tempfile
import unittest

import v6_support as F

G = F.G
import review_policy as RP  # noqa: E402  v6_support 已把 scripts/ 加入 sys.path

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""
MARKER = "任务记录未把该判断与实际决策关联，不能据结果一致推断采纳。"


class BackfillBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(
            self.state_path, goal="交付可复核结果", contract=CONTRACT,
            risk="medium", task_id="backfill", workspace=self.dir,
            task_kind="standard")
        self.call_id = self.record("CONSULT", stage="understanding")

    def record(self, mode: str, *, stage: str = "") -> str:
        state = G.load_state(self.state_path)
        call = {
            "call_id": f"SC{mode.lower()}{len(state.get('cognition_calls') or [])}",
            "mode": mode,
            "status": "completed",
            "stage": stage,
            "question": "本次需要判断的事项",
            "contract_sha256": state.get("contract_sha256") or "",
            "materials_sha256": "m" * 64,
            "summary": "建议",
            "findings": [],
            "findings_count": {},
            "completed_at": G.now_iso(),
        }
        G.record_cognition_call(self.state_path, call, mode=mode,
                          materials_sha256="m" * 64)
        return call["call_id"]

    def close_task(self) -> dict:
        """直接置收口事实：本测试只验证采纳回填，不重复验证收口门禁。"""
        state = G.load_state(self.state_path)
        state["closure"] = {
            **(state.get("closure") or {}),
            "closed": True, "verdict": "pass", "closed_at": G.now_iso(),
            "closed_by": "zcode-native",
            "gate": {"passed": True, "reasons": []},
        }
        state["mode"] = "CLOSED"
        G.save_state(self.state_path, state)
        return G.load_state(self.state_path)

    def accept(self, call_id: str, **kwargs) -> dict:
        return G.record_cognition_acceptance(self.state_path, call_id, **kwargs)

    def pending_review(self, call_id: str | None = None) -> dict:
        return self.accept(
            call_id or self.call_id, status="pending", historical=True,
            reason=MARKER, reason_code="no-attribution")


class ClosedTaskGateTests(BackfillBase):
    def test_normal_entry_still_rejects_closed_task(self):
        self.close_task()
        with self.assertRaises(G.GovernanceError) as ctx:
            self.accept(self.call_id, status="accepted",
                        decision="按建议收口", rationale="现场一致")
        self.assertIn("已收口", str(ctx.exception))

    def test_historical_is_rejected_for_open_task(self):
        with self.assertRaises(G.GovernanceError) as ctx:
            self.pending_review()
        self.assertIn("只用于已收口", str(ctx.exception))

    def test_pending_requires_historical_channel(self):
        # 未收口任务走正常入口；pending 不是结论，正常入口一律拒绝。
        with self.assertRaises(G.GovernanceError) as ctx:
            self.accept(self.call_id, status="pending", reason=MARKER)
        self.assertIn("pending", str(ctx.exception))

    def test_pending_requires_reason(self):
        self.close_task()
        with self.assertRaises(G.GovernanceError) as ctx:
            self.accept(self.call_id, status="pending", historical=True)
        self.assertIn("必须写明原因", str(ctx.exception))


class PendingReviewTests(BackfillBase):
    def test_pending_review_records_reason_and_stays_pending(self):
        before = self.close_task()
        result = self.pending_review()
        entry = result["acceptance"]
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["origin"], "historical-review")
        self.assertEqual(entry["reason_code"], "no-attribution")
        self.assertEqual(entry["reason"], MARKER)
        self.assertEqual(entry["basis"], "zcode-review-no-decision")
        self.assertFalse(entry.get("decision"))
        self.assertFalse(result.get("duplicate"))
        self.assertIn(self.call_id, [row["call_id"] for row in
                                     RP.pending_acceptance(
                                         G.load_state(self.state_path)["cognition_calls"])])
        self.assertEqual(
            RP.prior_cognition(G.load_state(self.state_path)["cognition_calls"])[-1][
                "acceptance"], "pending")

    def test_pending_review_does_not_touch_closure_or_criteria(self):
        before = self.close_task()
        self.pending_review()
        after = G.load_state(self.state_path)
        self.assertEqual(after["closure"]["verdict"], before["closure"]["verdict"])
        self.assertEqual(after["closure"]["gate"], before["closure"]["gate"])
        self.assertEqual(after["closure"]["closed_at"],
                         before["closure"]["closed_at"])
        self.assertEqual(after["contract"]["criteria"],
                         before["contract"]["criteria"])
        self.assertEqual(after["mode"], "CLOSED")
        events = [h["event"] for h in after["history"]]
        self.assertEqual(events.count("cognition_call_acceptance_review"), 1)
        self.assertEqual(events.count("cognition_call_acceptance"), 0)
        self.assertEqual(events.count("task_reopened"), 0)

    def test_pending_review_is_idempotent(self):
        self.close_task()
        first = self.pending_review()
        state = G.load_state(self.state_path)
        history_before = len(state["history"])
        second = self.pending_review()
        self.assertTrue(second["duplicate"])
        self.assertTrue(second["unchanged"])
        self.assertEqual(second["acceptance"], first["acceptance"])
        state = G.load_state(self.state_path)
        self.assertEqual(len(state["history"]), history_before)
        self.assertEqual(
            [h["event"] for h in state["history"]].count(
                "cognition_call_acceptance_review"), 1)

    def test_pending_review_reason_change_appends_history(self):
        self.close_task()
        self.pending_review()
        second = self.accept(self.call_id, status="pending", historical=True,
                             reason="补充：任务内另一条调用已有采纳，但未声明覆盖本条。",
                             reason_code="covered-by-other-acceptance")
        self.assertFalse(second["duplicate"])
        self.assertEqual(second["acceptance"]["reason_code"],
                         "covered-by-other-acceptance")
        self.assertEqual(
            [h["event"] for h in G.load_state(self.state_path)["history"]].count(
                "cognition_call_acceptance_review"), 2)

    def test_pending_review_does_not_count_as_decided(self):
        self.close_task()
        self.pending_review()
        quality = G.review_metrics(G.load_state(self.state_path))[
            "cognition"]["acceptance_quality"]
        self.assertEqual(quality["records"], 0)
        self.assertEqual(quality["legacy_records"], 0)


class DecidedBackfillTests(BackfillBase):
    def test_decided_backfill_marks_origin_and_records_history(self):
        self.close_task()
        result = self.accept(self.call_id, status="accepted", historical=True,
                             decision="按该判断先调查再修复",
                             rationale="证据登记与该判断一致")
        entry = result["acceptance"]
        self.assertEqual(entry["status"], "accepted")
        self.assertEqual(entry["origin"], "historical-backfill")
        self.assertEqual(entry["decision"], "按该判断先调查再修复")
        self.assertEqual(entry["registration_count"], 1)
        event = [h for h in G.load_state(self.state_path)["history"]
                 if h["event"] == "cognition_call_acceptance"][-1]
        self.assertTrue(event["detail"]["historical"])
        self.assertEqual(event["detail"]["status"], "accepted")

    def test_decided_backfill_duplicate_is_noop(self):
        self.close_task()
        first = self.accept(self.call_id, status="accepted", historical=True,
                            decision="按该判断先调查再修复",
                            rationale="证据登记与该判断一致")
        history_before = len(G.load_state(self.state_path)["history"])
        second = self.accept(self.call_id, status="accepted", historical=True,
                             decision="按该判断先调查再修复",
                             rationale="证据登记与该判断一致")
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["acceptance"], first["acceptance"])
        state = G.load_state(self.state_path)
        self.assertEqual(len(state["history"]), history_before)
        self.assertEqual(state["cognition_calls"][0]["acceptance"]["registration_count"], 1)

    def test_decided_backfill_never_overwrites(self):
        self.close_task()
        original = self.accept(self.call_id, status="accepted", historical=True,
                               decision="按该判断先调查再修复",
                               rationale="证据登记与该判断一致")["acceptance"]
        with self.assertRaises(G.GovernanceError) as ctx:
            self.accept(self.call_id, status="rejected", historical=True,
                        decision="改为直接修复", rationale="事后判断")
        self.assertIn("不覆盖既有结论", str(ctx.exception))
        state = G.load_state(self.state_path)
        self.assertEqual(state["cognition_calls"][0]["acceptance"], original)

    def test_pending_cannot_downgrade_decided_record(self):
        self.close_task()
        original = self.accept(self.call_id, status="modified", historical=True,
                               decision="部分采纳", rationale="仅采纳调查顺序",
                               delta="结论范围收窄")["acceptance"]
        with self.assertRaises(G.GovernanceError) as ctx:
            self.pending_review()
        self.assertIn("不把它改写成 pending", str(ctx.exception))
        self.assertEqual(G.load_state(self.state_path)["cognition_calls"][0]["acceptance"],
                         original)

    def test_decided_backfill_from_pending_review_is_allowed(self):
        self.close_task()
        self.pending_review()
        result = self.accept(self.call_id, status="accepted", historical=True,
                             decision="按该判断先调查再修复",
                             rationale="补到当时的判断与决策记录")
        entry = result["acceptance"]
        self.assertEqual(entry["status"], "accepted")
        self.assertEqual(entry["first_registered_at"], entry["at"])
        # 复核（pending）不是登记：计数只统计已决结论的登记次数。
        self.assertEqual(entry["registration_count"], 1)


class CliTests(BackfillBase):
    def test_cli_accepts_historical_pending_and_reports_duplicate(self):
        self.close_task()
        args = ("cognition-accept", "--state", self.state_path, "--call-id",
                self.call_id, "--status", "pending", "--historical",
                "--reason-code", "no-attribution", "--reason", MARKER)
        first = F.run_cli("copilot_dispatch.py", *args)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("历史复核已登记", first.stdout)
        self.assertIn("no-attribution", first.stdout)
        second = F.run_cli("copilot_dispatch.py", *args)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("重复执行", second.stdout)


if __name__ == "__main__":
    unittest.main()

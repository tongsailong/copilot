#!/usr/bin/env python3
"""REVIEW 预算只读预检：不联网、不创建台账、不改变门禁。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LEGACY = os.path.dirname(HERE)
SCRIPTS = os.path.join(os.path.dirname(LEGACY), "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, LEGACY)

import call_gpt  # noqa: E402
import review_ledger  # noqa: E402


def write_ledger(path: str, rounds: list[dict], *, risk: str = "normal",
                 audit_log: str = "", subject: str = "budget-test") -> None:
    review_ledger._save(path, {
        "version": review_ledger.LEDGER_VERSION,
        "created_at": "2026-11-04T00:00:00+08:00",
        "updated_at": "2026-11-04T00:00:00+08:00",
        "subject": subject,
        "audit_log": os.path.abspath(audit_log) if audit_log else "",
        "risk": risk,
        "max_rounds": review_ledger.RISK_MAX_ROUNDS[risk],
        "rounds": rounds,
    })


def write_audit(path: str, records: list[dict]) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def review_call(call_id: str, ledger: str, **extra) -> dict:
    return {
        "event": "call_result", "mode": "REVIEW", "ok": True,
        "call_id": call_id, "ledger": os.path.abspath(ledger),
        "subject": "budget-test", "review_round": 1, "review_max_rounds": 2,
        **extra,
    }


class AuditRoundMetaTests(unittest.TestCase):
    def test_extra_round_is_stamped_in_audit_metadata(self):
        """审计必须能区分普通轮、弹性追加轮与追加理由。"""
        meta = call_gpt._review_round_meta({
            "round": 3, "max_rounds": 2, "default_rounds": 2,
            "review_kind": "extra", "extra_index": 1, "extra_allowance": 2,
            "extra_remaining_after": 1, "extra_basis": "remediation",
            "extra_reason": "按第2轮 F1 整改，新增测试输出",
        })
        self.assertEqual(meta["review_round"], 3)
        self.assertEqual(meta["review_max_rounds"], 2)
        self.assertEqual(meta["review_default_rounds"], 2)
        self.assertEqual(meta["review_kind"], "extra")
        self.assertEqual(meta["review_extra_index"], 1)
        self.assertEqual(meta["review_extra_allowance"], 2)
        self.assertEqual(meta["review_extra_remaining_after"], 1)
        self.assertEqual(meta["review_basis"], "remediation")
        self.assertIn("F1", meta["review_reason"])

    def test_normal_round_is_stamped_as_normal(self):
        meta = call_gpt._review_round_meta({
            "round": 1, "max_rounds": 2, "default_rounds": 2,
            "review_kind": "normal", "extra_index": 0, "extra_allowance": 2,
            "extra_remaining_after": 2, "extra_basis": "", "extra_reason": "",
        })
        self.assertEqual(meta["review_kind"], "normal")
        self.assertEqual(meta["review_extra_index"], 0)
        self.assertNotIn("review_new_evidence", meta)

    def test_security_round_is_stamped_with_new_evidence(self):
        """Security 轮次必须留下风险、独立额度与新增证据的 SHA-256，供事后审计。"""
        meta = call_gpt._review_round_meta({
            "round": 5, "max_rounds": 2, "default_rounds": 2,
            "review_kind": "security", "risk": "normal",
            "extra_index": 0, "extra_allowance": 2, "extra_remaining_after": 2,
            "ordinary_used": 4, "security_used": 0,
            "security_index": 1, "security_allowance": 1, "security_remaining_after": 0,
            "extra_basis": "security", "extra_reason": "新发现越权读取风险",
            "new_ids": ["E5"], "items": [
                {"id": "E4", "digest": "a" * 64},
                {"id": "E5", "digest": "b" * 64},
            ],
        })
        self.assertEqual(meta["review_kind"], "security")
        self.assertEqual(meta["review_risk"], "normal")
        self.assertEqual(meta["review_ordinary_used"], 4)
        self.assertEqual(meta["review_security_used"], 0)
        self.assertEqual(meta["review_security_index"], 1)
        self.assertEqual(meta["review_security_allowance"], 1)
        self.assertEqual(meta["review_security_remaining"], 0)
        self.assertEqual(meta["review_basis"], "security")
        self.assertIn("越权", meta["review_reason"])
        self.assertEqual(meta["review_new_evidence"],
                         [{"id": "E5", "sha256": "b" * 64}])


class BudgetStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "review.json")
        self.audit = os.path.join(self.tmp.name, "audit.jsonl")

    def test_existing_ledger_reports_used_and_remaining(self):
        write_ledger(self.path, [{"n": 1, "call_id": "c1", "verdict": "PASS"}],
                     audit_log=self.audit)
        info = review_ledger.budget_status(self.path, audit_log=self.audit)
        self.assertEqual(info["status"], "active")
        self.assertEqual(info["subject"], "budget-test")
        self.assertEqual(info["risk"], "normal")
        self.assertEqual(info["max_rounds"], 2)
        self.assertEqual(info["used"], 1)
        self.assertEqual(info["remaining"], 1)
        self.assertEqual(info["default_rounds"], 2)
        self.assertEqual(info["extra_used"], 0)
        self.assertEqual(info["extra_allowance"], 2)
        self.assertEqual(info["extra_remaining"], 2)
        self.assertEqual(info["total_remaining"], 3)
        self.assertEqual(info["pending"], 0)
        self.assertTrue(info["remediation_available"])
        # 用量字段只用于观察；继续复核由新增证据与理由决定。
        self.assertFalse(info["limits_enforced"])
        self.assertTrue(info["unbounded"])
        self.assertTrue(info["allowed"])
        self.assertIn("没有固定轮次上限", info["next_action"])
        self.assertIn("新增/变化证据", info["next_action"])
        self.assertNotIn("终检", info["next_action"])

    def test_pending_counts_toward_used(self):
        write_ledger(self.path, [{"n": 1, "call_id": "c1", "verdict": "PASS"}],
                     audit_log=self.audit)
        write_audit(self.audit, [review_call("c2", self.path)])
        info = review_ledger.budget_status(self.path, audit_log=self.audit)
        self.assertEqual(info["used"], 2)
        self.assertEqual(info["remaining"], 0)
        self.assertEqual(info["extra_used"], 0)
        self.assertEqual(info["extra_remaining"], 2)
        self.assertEqual(info["pending"], 1)
        self.assertEqual(info["pending_call_ids"], ["c2"])
        # pending 占用轮次编号与用量观察；remediation 标签本身仍未被使用。
        self.assertTrue(info["remediation_available"])
        self.assertFalse(info["limits_enforced"])
        self.assertIn("没有固定轮次上限", info["next_action"])

    def test_pending_remediation_counts_as_used(self):
        write_ledger(self.path, [{"n": 1, "call_id": "c1", "verdict": "PASS"}],
                     audit_log=self.audit)
        write_audit(self.audit, [review_call(
            "c2", self.path, review_basis=review_ledger.REMEDIATION_BASIS)])
        info = review_ledger.budget_status(self.path, audit_log=self.audit)
        self.assertTrue(info["remediation_used"])
        self.assertFalse(info["remediation_available"])
        self.assertNotIn("终检", info["next_action"])

    def test_extra_round_is_counted_and_reason_reported(self):
        """追加轮次计入 used，并在预算里区分追加轮次、理由与剩余额度。"""
        write_ledger(self.path, [
            {"n": 1, "call_id": "c1", "verdict": "PARTIAL"},
            {"n": 2, "call_id": "c2", "verdict": "PARTIAL"},
            {"n": 3, "call_id": "c3", "verdict": "PARTIAL",
             "extra_basis": "remediation", "extra_reason": "按 F1 整改并补充行为测试"},
        ], audit_log=self.audit)
        info = review_ledger.budget_status(self.path, audit_log=self.audit)
        self.assertEqual(info["used"], 3)
        self.assertEqual(info["remaining"], 0)
        self.assertEqual(info["extra_used"], 1)
        self.assertEqual(info["extra_allowance"], 2)
        self.assertEqual(info["extra_remaining"], 1)
        self.assertEqual(info["total_remaining"], 1)
        self.assertEqual(info["extra_reasons"], [{
            "round": 3, "basis": "remediation",
            "reason": "按 F1 整改并补充行为测试",
        }])
        self.assertFalse(info["limits_enforced"])
        self.assertIn("没有固定轮次上限", info["next_action"])

    def test_extra_allowance_exhaustion_does_not_stop_review(self):
        """旧「弹性追加用尽后必须 PARTIAL/BLOCKED」已取消：用量只做观察。"""
        rounds = [{"n": idx, "call_id": f"c{idx}", "verdict": "PARTIAL"}
                  for idx in (1, 2, 3, 4)]
        write_ledger(self.path, rounds, audit_log=self.audit)
        info = review_ledger.budget_status(self.path, audit_log=self.audit)
        self.assertEqual(info["used"], 4)
        self.assertEqual(info["extra_used"], 2)
        self.assertEqual(info["extra_remaining"], 0)
        self.assertEqual(info["total_remaining"], 0)
        self.assertTrue(info["allowed"])
        self.assertFalse(info["limits_enforced"])
        self.assertIn("没有固定轮次上限", info["next_action"])
        self.assertNotIn("PARTIAL/BLOCKED", info["next_action"])
        self.assertNotIn("不新建台账", info["next_action"])

    def test_pending_extra_is_counted_but_does_not_block(self):
        """审计中成功但未入账的追加调用计入用量，但不构成停止条件。"""
        write_ledger(self.path, [
            {"n": 1, "call_id": "c1", "verdict": "PARTIAL"},
            {"n": 2, "call_id": "c2", "verdict": "PARTIAL"},
        ], audit_log=self.audit)
        write_audit(self.audit, [
            review_call("c3", self.path, review_round=3, review_basis="remediation"),
            review_call("c4", self.path, review_round=4, review_basis="new-evidence"),
        ])
        info = review_ledger.budget_status(self.path, audit_log=self.audit)
        self.assertEqual(info["used"], 4)
        self.assertEqual(info["pending"], 2)
        self.assertEqual(info["extra_used"], 2)
        self.assertEqual(info["extra_remaining"], 0)
        self.assertFalse(info["limits_enforced"])
        self.assertIn("没有固定轮次上限", info["next_action"])
        self.assertIn("未入账", info["next_action"])

    def test_consult_calls_do_not_consume_review_budget(self):
        """CONSULT/PLAN 不是正式复核，不占用 REVIEW 预算。"""
        write_ledger(self.path, [{"n": 1, "call_id": "c1", "verdict": "PASS"}],
                     audit_log=self.audit)
        write_audit(self.audit, [
            {"event": "call_result", "mode": "CONSULT", "ok": True,
             "call_id": "consult-1", "ledger": os.path.abspath(self.path),
             "subject": "budget-test"},
            {"event": "call_result", "mode": "PLAN", "ok": True,
             "call_id": "plan-1", "ledger": os.path.abspath(self.path),
             "subject": "budget-test"},
            # 即便 CONSULT 记录里误写了 security 依据，也不得占用 Security 额度。
            {"event": "call_result", "mode": "CONSULT", "ok": True,
             "call_id": "consult-sec", "ledger": os.path.abspath(self.path),
             "subject": "budget-test", "review_basis": "security",
             "review_kind": "security"},
        ])
        info = review_ledger.budget_status(self.path, audit_log=self.audit)
        self.assertEqual(info["used"], 1)
        self.assertEqual(info["pending"], 0)
        self.assertEqual(info["remaining"], 1)
        self.assertEqual(info["extra_remaining"], 2)
        self.assertEqual(info["security_used"], 0)
        self.assertEqual(info["security_remaining"], 1)

    def test_legacy_rounds_without_kind_count_as_ordinary(self):
        """旧台账没有 review_kind/extra_basis：按普通预算计数，不凭空生成 Security 记录。"""
        write_ledger(self.path, [
            {"n": 1, "call_id": "c1", "verdict": "PARTIAL"},
            {"n": 2, "call_id": "c2", "verdict": "PARTIAL"},
        ], audit_log=self.audit, risk="high")
        info = review_ledger.budget_status(self.path, audit_log=self.audit)
        self.assertEqual(info["used"], 2)
        self.assertEqual(info["ordinary_used"], 2)
        self.assertEqual(info["security_used"], 0)
        self.assertEqual(info["security_allowance"], 2)
        self.assertEqual(info["security_remaining"], 2)
        self.assertEqual(info["ordinary_remaining"], 1)
        self.assertFalse(info["security_available"])

    def test_ordinary_exhausted_reports_security_available(self):
        rounds = [{"n": idx, "call_id": f"c{idx}", "verdict": "PARTIAL"}
                  for idx in (1, 2, 3, 4)]
        write_ledger(self.path, rounds, audit_log=self.audit)
        info = review_ledger.budget_status(self.path, audit_log=self.audit)
        self.assertEqual(info["ordinary_used"], 4)
        self.assertTrue(info["ordinary_exhausted"])
        self.assertEqual(info["security_used"], 0)
        self.assertEqual(info["security_allowance"], 1)
        self.assertEqual(info["security_remaining"], 1)
        self.assertTrue(info["security_available"])
        # 普通用量用尽后并非「必须走 Security」：普通补证仍可继续。
        self.assertFalse(info["limits_enforced"])
        self.assertIn("没有固定轮次上限", info["next_action"])

    def test_security_rounds_do_not_reset_ordinary_budget(self):
        rounds = [{"n": idx, "call_id": f"c{idx}", "verdict": "PARTIAL"}
                  for idx in (1, 2, 3, 4)]
        rounds.append({"n": 5, "call_id": "sec-1", "verdict": "PARTIAL",
                       "review_kind": "security", "extra_basis": "security",
                       "extra_reason": "新发现越权风险"})
        write_ledger(self.path, rounds, audit_log=self.audit)
        info = review_ledger.budget_status(self.path, audit_log=self.audit)
        self.assertEqual(info["used"], 5)
        self.assertEqual(info["ordinary_used"], 4)
        self.assertEqual(info["ordinary_extra_remaining"], 0)
        self.assertEqual(info["total_remaining"], 0)
        self.assertEqual(info["security_used"], 1)
        self.assertEqual(info["security_remaining"], 0)
        self.assertFalse(info["security_available"])
        self.assertEqual(info["security_reasons"], [{
            "round": 5, "basis": "security",
            "reason": "新发现越权风险", "state": "recorded",
        }])
        self.assertFalse(info["limits_enforced"])
        self.assertIn("没有固定轮次上限", info["next_action"])

    def test_pending_security_is_counted_but_not_a_stop_condition(self):
        rounds = [{"n": idx, "call_id": f"c{idx}", "verdict": "PARTIAL"}
                  for idx in (1, 2, 3, 4)]
        write_ledger(self.path, rounds, audit_log=self.audit)
        write_audit(self.audit, [review_call(
            "sec-pending", self.path, review_round=5,
            review_basis="security", review_kind="security")])
        info = review_ledger.budget_status(self.path, audit_log=self.audit)
        self.assertEqual(info["used"], 5)
        self.assertEqual(info["pending"], 1)
        self.assertEqual(info["ordinary_pending"], 0)
        self.assertEqual(info["security_pending"], 1)
        self.assertEqual(info["security_used"], 1)
        self.assertEqual(info["security_remaining"], 0)
        self.assertFalse(info["security_available"])
        self.assertTrue(info["security_basis_allowed"])
        self.assertFalse(info["limits_enforced"])
        self.assertIn("没有固定轮次上限", info["next_action"])
        self.assertIn("未入账", info["next_action"])

    def test_conflicting_security_classification_is_rejected(self):
        """review_kind 与 basis 冲突说明台账/审计被改写，不得猜一个继续计数。"""
        write_ledger(self.path, [{
            "n": 1, "call_id": "c1", "verdict": "PARTIAL",
            "review_kind": "security", "extra_basis": "new-evidence",
        }], audit_log=self.audit)
        with self.assertRaises(review_ledger.LedgerError) as ctx:
            review_ledger.budget_status(self.path, audit_log=self.audit)
        self.assertIn("复核类型不一致", str(ctx.exception))

    def test_duplicate_call_id_in_ledger_is_rejected(self):
        write_ledger(self.path, [
            {"n": 1, "call_id": "same", "verdict": "PARTIAL"},
            {"n": 2, "call_id": "same", "verdict": "PARTIAL"},
        ], audit_log=self.audit)
        with self.assertRaises(review_ledger.LedgerError) as ctx:
            review_ledger.budget_status(self.path, audit_log=self.audit)
        self.assertIn("重复出现", str(ctx.exception))

    def test_missing_ledger_is_reported_without_creating_file(self):
        info = review_ledger.budget_status(
            self.path, risk="high", subject="planned-deliverable")
        self.assertEqual(info["status"], "not_created")
        self.assertEqual(info["subject"], "planned-deliverable")
        self.assertEqual(info["risk"], "high")
        self.assertEqual(info["max_rounds"], 3)
        self.assertEqual(info["used"], 0)
        self.assertEqual(info["remaining"], 3)
        self.assertEqual(info["pending"], 0)
        self.assertFalse(os.path.exists(self.path))

    def test_invalid_risk_is_rejected(self):
        with self.assertRaises(review_ledger.LedgerError):
            review_ledger.budget_status(self.path, risk="low")

    def test_audit_binding_mismatch_is_rejected(self):
        other = os.path.join(self.tmp.name, "other.jsonl")
        write_ledger(self.path, [], audit_log=self.audit)
        with self.assertRaises(review_ledger.LedgerError) as ctx:
            review_ledger.budget_status(self.path, audit_log=other)
        self.assertIn("绑定", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

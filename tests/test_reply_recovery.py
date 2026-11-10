#!/usr/bin/env python3
"""已消费回复安全恢复测试：不联网、不产生真实模型调用。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import copilot_dispatch  # noqa: E402
import reply_protocol  # noqa: E402
from lib_common import sha256_text  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "third-reply-old-fields.md")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def jsonl_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def audit_records(path: str) -> list[dict]:
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


class RecoveryFixture(unittest.TestCase):
    """任务级状态 + 隔离审计；制造一个「已消费但未应用」的拒绝轮次。"""

    CRITERIA = ["条件一", "条件二", "条件三"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "state.json")
        self.audit = os.path.join(self.tmp.name, "audit.jsonl")
        self.recovery_audit = os.path.join(self.tmp.name, "recovery-audit.jsonl")
        self.state = copilot_dispatch.start(
            self.path, "恢复测试任务", mode="PLAN",
            contract="完成条件：\n- 条件一\n- 条件二\n- 条件三\n边界：测试",
            task_id="task-recovery", criteria=list(self.CRITERIA),
            risk="normal")
        self.confirm()
        for eid, text in (("E1", "测试输出一"), ("E2", "测试输出二"),
                          ("E3", "测试输出三")):
            copilot_dispatch.register_evidence(self.state, eid, text=text)
        self.save()

    # ── 构造现场 ──────────────────────────────────────────────────────────
    def save(self) -> None:
        copilot_dispatch.save_state(self.path, self.state)

    def reply(self, plan: dict) -> str:
        return ("结论：见安排。\n\n```copilot-plan\n"
                + json.dumps(plan, ensure_ascii=False) + "\n```\n")

    def confirm(self) -> dict:
        return self.ingest(self.reply({
            "decision": "continue", "verdict": "partial",
            "criteria_confirmed": True, "summary": "确认完成条件",
            "criteria": [{"criterion_id": f"C{i}", "status": "pending"}
                         for i in range(1, 4)],
        }))

    def ingest(self, reply: str) -> dict:
        call_id = f"recovery-call-{len(self.state['rounds']) + 1}"
        pending = copilot_dispatch.prepare_pending(
            self.state, call_id=call_id, mode="PLAN",
            model_requested=copilot_dispatch.AUTO_MODEL,
            reasoning_effort=copilot_dispatch.AUTO_REASONING_EFFORT,
            model_source="default")
        fields = {
            "task_id": pending["task_id"], "round_id": pending["round_id"],
            "contract_sha256": pending["contract_sha256"],
            "call_id": call_id, "mode": "PLAN",
            "reply_sha256": sha256_text(reply),
            "model_requested": copilot_dispatch.AUTO_MODEL,
            "model_resolved": copilot_dispatch.AUTO_MODEL,
            "model_source": "default", "model_identity_ok": True,
            "reasoning_effort": copilot_dispatch.AUTO_REASONING_EFFORT,
        }
        outcome = copilot_dispatch.commit_reply(self.state, reply, "PLAN",
                                                receipt=fields)
        self.save()
        return outcome

    def rejected_round(self, reply: str, *, call_id: str = "call-recovery-1",
                       audit_sha: str | None = None,
                       note: str | None = None) -> dict:
        """模拟真实失败：pending 已消费、轮次记为 rejected、审计有请求与成功结果。"""
        pending = copilot_dispatch.prepare_pending(
            self.state, call_id=call_id, mode="PLAN",
            model_requested=copilot_dispatch.AUTO_MODEL,
            reasoning_effort=copilot_dispatch.AUTO_REASONING_EFFORT,
            model_source="default")
        audit_sha = audit_sha or sha256_text(reply.rstrip("\n"))
        fields = {
            "task_id": pending["task_id"], "round_id": pending["round_id"],
            "contract_sha256": pending["contract_sha256"],
            "call_id": call_id, "mode": "PLAN",
            "reply_sha256": audit_sha,
            "model_requested": copilot_dispatch.AUTO_MODEL,
            "model_resolved": copilot_dispatch.AUTO_MODEL,
            "model_source": "default", "model_identity_ok": True,
            "reasoning_effort": copilot_dispatch.AUTO_REASONING_EFFORT,
        }
        self.state["rounds"].append({
            "ts": "2026-11-03T06:00:00+08:00", "mode": "PLAN",
            "decision": None, "attempted_decision": "stop",
            "attempted_verdict": "pass", "rejected": True,
            "actions": [], "updated": [],
            "note": note or "安排被拒绝：plan.criteria 第 1 项 id 非法：''。",
            "reply_digest": "摘要", **fields,
        })
        self.state["pending_call"] = None
        self.state["last_call"] = {
            "call_id": call_id, "ts": "2026-11-03T06:00:01+08:00", "ok": True,
            **{k: v for k, v in fields.items() if k != "mode"},
        }
        self.save()
        self.write_audit(call_id, fields, audit_sha)
        return fields

    def write_audit(self, call_id: str, fields: dict, reply_sha: str) -> None:
        base = {
            "call_id": call_id, "task_id": fields["task_id"],
            "round_id": fields["round_id"], "mode": "PLAN",
            "contract_sha256": fields["contract_sha256"],
            "model_requested": copilot_dispatch.AUTO_MODEL,
            "reasoning_effort": copilot_dispatch.AUTO_REASONING_EFFORT,
            "tools_sent": False,
        }
        with open(self.audit, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "call_requested", **base},
                                ensure_ascii=False) + "\n")
            fh.write(json.dumps({
                "event": "call_result", **base, "ok": True,
                "http_status": 200, "model_resolved": copilot_dispatch.AUTO_MODEL,
                "model_source": "default", "model_identity_ok": True,
                "reply_sha256": reply_sha,
            }, ensure_ascii=False) + "\n")

    def recover(self, call_id: str = "call-recovery-1", reply: str | None = None,
                **kwargs) -> dict:
        if reply is None:
            reply = read(FIXTURE)
        return copilot_dispatch.recover_consumed_reply(
            self.path, self.state, self.audit,
            call_id=call_id, reply=reply, recovery_audit=self.recovery_audit,
            **kwargs)

    def real_plan(self) -> str:
        with open(FIXTURE, encoding="utf-8") as fh:
            return fh.read()

    def criterion(self, cid: str) -> dict:
        for item in self.state["contract"]["criteria"]:
            if item["criterion_id"] == cid:
                return item
        raise AssertionError(cid)

    def state_digest(self) -> str:
        return copilot_dispatch._state_fingerprint(self.state)

    def assert_state_unchanged(self, before: str) -> None:
        self.assertEqual(copilot_dispatch._state_fingerprint(self.state), before)

    def assert_audit_unchanged(self, before: bytes) -> None:
        self.assertEqual(jsonl_bytes(self.audit), before)


class RecoverOldFieldsTests(RecoveryFixture):
    def test_old_fields_are_recovered_without_new_request(self):
        reply = self.real_plan()
        self.rejected_round(reply)
        audit_before = jsonl_bytes(self.audit)
        requests_before = sum(1 for r in audit_records(self.audit)
                              if r.get("event") == "call_requested")
        before = self.state_digest()
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("不得联网")):
            result = self.recover(reply=reply)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(result["mapping"]), 6)
        self.assertNotEqual(before, result["after_state_sha256"])
        self.assertEqual(len(self.state["rounds"]), 2)
        # 不新增轮次、不新增请求、不改模型审计。
        self.assertEqual(sum(1 for r in audit_records(self.audit)
                             if r.get("event") == "call_requested"),
                         requests_before)
        self.assert_audit_unchanged(audit_before)
        target = self.state["rounds"][-1]
        self.assertFalse(target["rejected"])
        self.assertTrue(target["recovered"])
        self.assertEqual(target["decision"], "stop")
        self.assertEqual(target["verdict"], "pass")
        self.assertEqual(target["recovery"]["normalizer_version"],
                         reply_protocol.NORMALIZER_VERSION)
        self.assertIn("id 非法", target["recovery"]["previous_note"])
        self.assertEqual(target["recovery"]["audit_reply_sha256"],
                         sha256_text(reply.rstrip("\n")))
        self.assertEqual(target["recovery"]["normalized_reply_sha256"],
                         result["normalized_reply_sha256"])
        # 条件按真实第三次回复的证据引用核销。
        self.assertEqual(self.criterion("C1")["status"], "met")
        self.assertEqual([r["id"] for r in self.criterion("C1")["evidence_refs"]],
                         ["E2", "E3"])
        self.assertEqual([r["id"] for r in self.criterion("C3")["evidence_refs"]],
                         ["E1"])
        self.assertTrue(copilot_dispatch.pass_pending(self.state))
        # 恢复审计：intent + result，含前后状态摘要和字段映射。
        records = audit_records(self.recovery_audit)
        self.assertEqual([r["event"] for r in records],
                         ["recover_intent", "recover_result"])
        self.assertEqual(records[0]["result"], "intent")
        self.assertEqual(records[1]["result"], "applied")
        self.assertEqual(records[1]["before_state_sha256"], before)
        self.assertEqual(records[1]["after_state_sha256"],
                         result["after_state_sha256"])
        self.assertEqual(len(records[1]["mapping"]), 6)
        self.assertEqual(records[1]["call_id"], "call-recovery-1")

    def test_evidence_ids_reply_is_recovered_without_new_request(self):
        reply = self.reply({
            "decision": "stop", "verdict": "pass", "summary": "按 evidence_ids 核销",
            "criteria": [
                {"id": "C1", "status": "met", "evidence_ids": ["E1"]},
                {"id": "C2", "status": "met", "evidence_ids": ["E2"]},
                {"id": "C3", "status": "met", "evidence_ids": ["E3"]},
            ],
        })
        self.rejected_round(reply)
        before = self.state_digest()
        result = self.recover(reply=reply)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["normalizer_version"],
                         reply_protocol.NORMALIZER_VERSION)
        self.assertNotEqual(before, result["after_state_sha256"])
        self.assertIn(("evidence_ids", "evidence_refs"),
                      [(m["from"], m["to"]) for m in result["mapping"]])
        self.assertEqual(self.criterion("C1")["status"], "met")
        self.assertEqual([r["id"] for r in self.criterion("C1")["evidence_refs"]],
                         ["E1"])
        self.assertTrue(copilot_dispatch.pass_pending(self.state))

    def test_normal_gate_closure_after_recovery(self):
        reply = self.real_plan()
        self.rejected_round(reply)
        self.recover(reply=reply)
        check = copilot_dispatch.closure_check(self.state)
        self.assertTrue(check["passed"], check)
        for gate in ("G1", "G2", "G3", "G4"):
            self.assertTrue(check["gates"][gate]["passed"], gate)
        step = copilot_dispatch.loop_step(self.path)
        self.assertEqual(step["phase"], "CLOSURE_CHECK")
        closure = copilot_dispatch.close_task(self.path)
        self.assertTrue(closure["closed"])
        self.assertEqual(closure["closed_by"], "gate")

    def test_high_risk_g4_is_not_bypassed_by_recovery(self):
        self.state["risk"] = "high"
        self.save()
        reply = self.real_plan()
        self.rejected_round(reply)
        self.recover(reply=reply)
        check = copilot_dispatch.closure_check(self.state)
        self.assertFalse(check["passed"])
        self.assertFalse(check["gates"]["G4"]["passed"])
        self.assertIn("native_review_missing",
                      [r["code"] for r in check["gates"]["G4"]["reasons"]])

    def test_credential_and_audit_hash_mismatch_are_rejected(self):
        reply = self.real_plan()
        self.rejected_round(reply, audit_sha="0" * 64)
        before = self.state_digest()
        with self.assertRaises(copilot_dispatch.DispatchError) as ctx:
            self.recover(reply=reply)
        self.assertIn("摘要与调用", str(ctx.exception))
        self.assert_state_unchanged(before)
        self.assertFalse(os.path.exists(self.recovery_audit))

    def test_call_id_must_be_a_consumed_round(self):
        self.rejected_round(self.real_plan())
        before = self.state_digest()
        with self.assertRaises(copilot_dispatch.DispatchError) as ctx:
            self.recover(call_id="call-not-in-state", reply=self.real_plan())
        self.assertIn("没有 call_id", str(ctx.exception))
        self.assert_state_unchanged(before)
        self.assertFalse(os.path.exists(self.recovery_audit))

    def test_already_applied_is_idempotent(self):
        reply = self.real_plan()
        self.rejected_round(reply)
        self.recover(reply=reply)
        state_after = self.state_digest()
        recovery_lines = len(audit_records(self.recovery_audit))
        result = self.recover(reply=reply)
        self.assertEqual(result["status"], "already_applied")
        self.assert_state_unchanged(state_after)
        self.assertEqual(len(audit_records(self.recovery_audit)), recovery_lines)

    def test_missing_result_record_is_repaired_without_reapplying(self):
        reply = self.real_plan()
        self.rejected_round(reply)
        self.recover(reply=reply)
        state_after = self.state_digest()
        records = audit_records(self.recovery_audit)
        with open(self.recovery_audit, "w", encoding="utf-8") as fh:
            for record in records:
                if record["event"] == "recover_intent":
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        result = self.recover(reply=reply)
        self.assertEqual(result["status"], "already_applied")
        self.assert_state_unchanged(state_after)
        repaired = audit_records(self.recovery_audit)
        self.assertEqual([r["event"] for r in repaired],
                         ["recover_intent", "recover_result"])
        self.assertIn("补记", repaired[-1]["reason"])

    def test_later_round_blocks_recovery(self):
        self.rejected_round(self.real_plan())
        self.state["rounds"].append({
            "ts": "2026-11-03T07:00:00+08:00", "mode": "PLAN",
            "decision": "continue", "verdict": "partial", "call_id": "later",
            "round_id": "task-recovery-r9", "model_identity_ok": True,
        })
        self.save()
        before = self.state_digest()
        with self.assertRaises(copilot_dispatch.DispatchError) as ctx:
            self.recover(reply=self.real_plan())
        self.assertIn("之后已有后续轮次", str(ctx.exception))
        self.assert_state_unchanged(before)

    def test_conflicting_action_progress_blocks_recovery(self):
        self.rejected_round(self.real_plan())
        target_round = self.state["rounds"][-1]["round_id"]
        self.state["actions"].append({
            "id": "T1", "title": "x", "status": "done",
            "source_round_id": target_round,
        })
        self.save()
        before = self.state_digest()
        with self.assertRaises(copilot_dispatch.DispatchError) as ctx:
            self.recover(reply=self.real_plan())
        self.assertIn("已有进展", str(ctx.exception))
        self.assert_state_unchanged(before)


class RecoveryValidationTests(RecoveryFixture):
    def test_dry_run_does_not_write_state_or_audit(self):
        reply = self.real_plan()
        self.rejected_round(reply)
        before = self.state_digest()
        result = self.recover(reply=reply, dry_run=True)
        self.assertEqual(result["status"], "preview")
        self.assertEqual(len(result["mapping"]), 6)
        self.assert_state_unchanged(before)
        self.assertFalse(os.path.exists(self.recovery_audit))

    def test_both_fields_equal_is_accepted(self):
        plan = {
            "decision": "stop", "verdict": "pass", "summary": "ok",
            "criteria": [
                {"criterion_id": "C1", "id": "C1", "status": "met",
                 "evidence_refs": ["E1"], "evidence": ["E1"]},
                {"criterion_id": "C2", "id": "C2", "status": "met",
                 "evidence_refs": ["E2"], "evidence": ["E2"]},
                {"criterion_id": "C3", "id": "C3", "status": "met",
                 "evidence_refs": ["E3"], "evidence": ["E3"]},
            ],
        }
        reply = self.reply(plan)
        self.rejected_round(reply)
        result = self.recover(reply=reply)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["mapping"], [])

    def test_canonical_only_is_accepted(self):
        plan = {
            "decision": "stop", "verdict": "pass", "summary": "ok",
            "criteria": [
                {"criterion_id": f"C{i}", "status": "met",
                 "evidence_refs": [f"E{i}"]} for i in range(1, 4)
            ],
        }
        reply = self.reply(plan)
        self.rejected_round(reply)
        result = self.recover(reply=reply)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["mapping"], [])

    def test_conflicting_fields_reject_without_state_change(self):
        plan = {
            "decision": "stop", "verdict": "pass",
            "criteria": [
                {"criterion_id": "C1", "id": "C2", "status": "met",
                 "evidence_refs": ["E1"]},
                {"criterion_id": "C2", "status": "met", "evidence_refs": ["E2"]},
                {"criterion_id": "C3", "status": "met", "evidence_refs": ["E3"]},
            ],
        }
        reply = self.reply(plan)
        self.rejected_round(reply)
        before = self.state_digest()
        with self.assertRaises(copilot_dispatch.DispatchError) as ctx:
            self.recover(reply=reply)
        self.assertIn("冲突", str(ctx.exception))
        self.assert_state_unchanged(before)
        records = audit_records(self.recovery_audit)
        self.assertEqual(records[-1]["result"], "rejected")

    def test_type_error_rejects_without_state_change(self):
        plan = {
            "decision": "stop", "verdict": "pass",
            "criteria": [
                {"id": 123, "status": "met", "evidence": ["E1"]},
                {"criterion_id": "C2", "status": "met", "evidence_refs": ["E2"]},
                {"criterion_id": "C3", "status": "met", "evidence_refs": ["E3"]},
            ],
        }
        reply = self.reply(plan)
        self.rejected_round(reply)
        before = self.state_digest()
        with self.assertRaises(copilot_dispatch.DispatchError):
            self.recover(reply=reply)
        self.assert_state_unchanged(before)

    def test_apply_failure_leaves_no_half_update(self):
        plan = {
            "decision": "continue", "verdict": "partial",
            "summary": "带一个非法工作项",
            "criteria": [
                {"criterion_id": f"C{i}", "status": "pending"} for i in range(1, 4)
            ],
            "actions": [{"id": "T1", "title": "x", "assignee": "gpt"}],
        }
        reply = self.reply(plan)
        self.rejected_round(reply)
        before = self.state_digest()
        with self.assertRaises(copilot_dispatch.DispatchError) as ctx:
            self.recover(reply=reply)
        self.assertIn("assignee", str(ctx.exception))
        self.assert_state_unchanged(before)
        self.assertEqual(self.state["actions"], [])
        for cid in ("C1", "C2", "C3"):
            self.assertEqual(self.criterion(cid)["status"], "pending")
        self.assertEqual(audit_records(self.recovery_audit)[-1]["result"],
                         "rejected")

    def test_rejected_round_without_plan_block_is_not_recovered(self):
        reply = "只有结论，没有安排块。\n"
        self.rejected_round(reply)
        before = self.state_digest()
        with self.assertRaises(copilot_dispatch.DispatchError) as ctx:
            self.recover(reply=reply)
        self.assertIn("不含 copilot-plan", str(ctx.exception))
        self.assert_state_unchanged(before)

    def test_closed_task_cannot_be_recovered(self):
        reply = self.real_plan()
        self.rejected_round(reply)
        self.state["closure"]["closed"] = True
        self.save()
        with self.assertRaises(copilot_dispatch.DispatchError) as ctx:
            self.recover(reply=reply)
        self.assertIn("已收口", str(ctx.exception))

    def test_state_sha256_cas_rejects_concurrent_change(self):
        loaded = copilot_dispatch.load_state(self.path)
        fingerprint = copilot_dispatch._state_fingerprint(loaded)
        disk = copilot_dispatch.load_state(self.path)
        disk["note"] = "外部修改"
        copilot_dispatch.save_state(self.path, disk)
        loaded["note"] = "恢复写入"
        with self.assertRaises(copilot_dispatch.DispatchError) as ctx:
            copilot_dispatch.save_state(
                self.path, loaded, expect={"state_sha256": fingerprint})
        self.assertIn("state_sha256", str(ctx.exception))
        self.assertEqual(copilot_dispatch.load_state(self.path)["note"], "外部修改")


class RecoveryCasBaselineTests(RecoveryFixture):
    """恢复入口的 CAS 基线必须与比较对象同源（磁盘原文），而非升级后的内存态。"""

    def strip_default_keys(self, keys: tuple[str, ...]) -> dict:
        """模拟旧状态文件：磁盘上缺 v2 默认节，加载时由 `_upgrade_state` 补齐。"""
        with open(self.path, encoding="utf-8") as fh:
            raw = json.load(fh)
        for key in keys:
            raw.pop(key, None)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, ensure_ascii=False)
        return raw

    def test_legacy_disk_without_default_keys_can_still_recover(self):
        reply = self.real_plan()
        self.rejected_round(reply)
        raw = self.strip_default_keys(("review_consumed", "native_results",
                                       "native_reviews"))
        loaded = copilot_dispatch.load_state(self.path)
        result = copilot_dispatch.recover_consumed_reply(
            self.path, loaded, self.audit, call_id="call-recovery-1",
            reply=reply, recovery_audit=self.recovery_audit)
        self.assertEqual(result["status"], "applied")
        record = [r for r in audit_records(self.recovery_audit)
                  if r.get("event") == "recover_result"][-1]
        self.assertEqual(record["result"], "applied")
        self.assertEqual(record["before_state_sha256_basis"], "disk-raw")
        disk_fp = sha256_text(json.dumps(raw, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")))
        self.assertEqual(record["before_state_sha256"], disk_fp)
        self.assertNotEqual(record["loaded_state_sha256"], disk_fp,
                            "该现场正是「加载后补默认键」导致内存态与磁盘原文不同")
        self.assertEqual(len(loaded["rounds"]), 2)
        self.assertFalse(loaded["rounds"][-1]["rejected"])

    def test_concurrent_write_between_caller_load_and_baseline_is_preserved(self):
        """调用方 load 之后、取基线之前的写入必须被读入，不能被恢复静默回退。"""
        reply = self.real_plan()
        self.rejected_round(reply)
        stale = copilot_dispatch.load_state(self.path)
        with open(self.path, encoding="utf-8") as fh:
            current = json.load(fh)
        current["note"] = "基线前外部写入"
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(current, fh, ensure_ascii=False)
        result = copilot_dispatch.recover_consumed_reply(
            self.path, stale, self.audit, call_id="call-recovery-1",
            reply=reply, recovery_audit=self.recovery_audit)
        self.assertEqual(result["status"], "applied")
        after = copilot_dispatch.load_state(self.path)
        self.assertEqual(after["note"], "基线前外部写入",
                         "基线来自磁盘原文，工作副本必须同源")
        self.assertFalse(after["rounds"][-1]["rejected"])

    def test_concurrent_write_between_baseline_and_save_is_rejected(self):
        reply = self.real_plan()
        self.rejected_round(reply)
        original = copilot_dispatch.save_state

        def racing_save(path, state, expect=None):
            if expect and "state_sha256" in expect:
                with open(path, encoding="utf-8") as fh:
                    current = json.load(fh)
                current["note"] = "外部进程写入"
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(current, fh, ensure_ascii=False)
            return original(path, state, expect=expect)

        with mock.patch.object(copilot_dispatch, "save_state",
                               side_effect=racing_save):
            with self.assertRaises(copilot_dispatch.DispatchError) as ctx:
                self.recover(reply=reply)
        self.assertIn("state_sha256", str(ctx.exception))
        after = copilot_dispatch.load_state(self.path)
        self.assertEqual(after["note"], "外部进程写入", "恢复不得覆盖外部写入")
        self.assertTrue(after["rounds"][-1]["rejected"], "恢复失败时轮次保持原状")
        record = [r for r in audit_records(self.recovery_audit)
                  if r.get("event") == "recover_result"][-1]
        self.assertEqual(record["result"], "failed")


if __name__ == "__main__":
    unittest.main()

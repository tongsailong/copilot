#!/usr/bin/env python3
"""自动驱动的已消费回复恢复：只做本地状态/审计模拟，不联网。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LEGACY = os.path.dirname(HERE)
SCRIPTS = os.path.join(os.path.dirname(LEGACY), "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, LEGACY)

import copilot_auto  # noqa: E402
import copilot_dispatch  # noqa: E402
import reply_protocol  # noqa: E402
from lib_common import sha256_text  # noqa: E402


class AutoRecoveryFixture(unittest.TestCase):
    CRITERIA = ["条件一", "条件二", "条件三"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "dispatch.json")
        self.audit = os.path.join(self.tmp.name, "audit.jsonl")
        self.state = copilot_dispatch.start(
            self.path, "自动恢复测试", mode="PLAN",
            contract="完成条件：\n- 条件一\n- 条件二\n- 条件三\n边界：测试",
            task_id="task-auto-recovery", criteria=list(self.CRITERIA),
            risk="normal")
        self.ingest(self.reply({
            "decision": "continue", "verdict": "partial",
            "criteria_confirmed": True, "summary": "确认完成条件",
            "criteria": [{"criterion_id": f"C{i}", "status": "pending"}
                         for i in range(1, 4)],
        }))
        for eid, text in (("E1", "测试输出一"), ("E2", "测试输出二"),
                          ("E3", "测试输出三")):
            copilot_dispatch.register_evidence(self.state, eid, text=text)
        self.save()

    def save(self) -> None:
        copilot_dispatch.save_state(self.path, self.state)

    def reply(self, plan: dict) -> str:
        return ("结论：见安排。\n\n```copilot-plan\n"
                + json.dumps(plan, ensure_ascii=False) + "\n```\n")

    def ingest(self, reply: str) -> dict:
        call_id = f"call-{len(self.state['rounds']) + 1}"
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

    def rejected_round(self, reply: str, *,
                       call_id: str = "call-rejected-1") -> str:
        pending = copilot_dispatch.prepare_pending(
            self.state, call_id=call_id, mode="PLAN",
            model_requested=copilot_dispatch.AUTO_MODEL,
            reasoning_effort=copilot_dispatch.AUTO_REASONING_EFFORT,
            model_source="default")
        fields = {
            "task_id": pending["task_id"], "round_id": pending["round_id"],
            "contract_sha256": pending["contract_sha256"],
            "call_id": call_id, "mode": "PLAN",
            "reply_sha256": sha256_text(reply.rstrip("\n")),
            "model_requested": copilot_dispatch.AUTO_MODEL,
            "model_resolved": copilot_dispatch.AUTO_MODEL,
            "model_source": "default", "model_identity_ok": True,
            "reasoning_effort": copilot_dispatch.AUTO_REASONING_EFFORT,
        }
        self.state["rounds"].append({
            "ts": "2026-11-04T06:00:00+08:00", "mode": "PLAN",
            "decision": None, "attempted_decision": "stop",
            "attempted_verdict": "pass", "rejected": True,
            "actions": [], "updated": [],
            "note": "安排被拒绝：测试构造", **fields,
        })
        self.state["pending_call"] = None
        self.state["last_call"] = {
            "call_id": call_id, "ts": "2026-11-04T06:00:01+08:00", "ok": True,
            **{k: v for k, v in fields.items() if k != "mode"},
        }
        self.save()
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
                "reply_sha256": fields["reply_sha256"],
            }, ensure_ascii=False) + "\n")
            fh.write(json.dumps({
                "event": "call_post_failure", **base,
                "error": "dispatch_plan_rejected", "detail": "测试构造",
            }, ensure_ascii=False) + "\n")
        reply_path = os.path.join(self.tmp.name, "round-rejected-reply.md")
        with open(reply_path, "w", encoding="utf-8") as fh:
            fh.write(reply)
        return reply_path

    def args(self):
        return types.SimpleNamespace(audit_log=self.audit)

    def call(self, reply_path: str) -> dict:
        return {"exit": 2, "reply_file": reply_path}

    def fingerprint(self) -> str:
        return copilot_dispatch._state_fingerprint(self.state)

    def criterion(self, cid: str) -> dict:
        for item in self.state["contract"]["criteria"]:
            if item["criterion_id"] == cid:
                return item
        raise AssertionError(cid)


class AutoRecoverRejectedTests(AutoRecoveryFixture):
    def test_evidence_ids_is_applied_without_new_call(self):
        reply = self.reply({
            "decision": "stop", "verdict": "pass", "summary": "按 evidence_ids 核销",
            "criteria": [
                {"id": "C1", "status": "met", "evidence_ids": ["E1"]},
                {"id": "C2", "status": "met", "evidence_ids": ["E2"]},
                {"id": "C3", "status": "met", "evidence_ids": ["E3"]},
            ],
        })
        path = self.rejected_round(reply)
        before_rounds = len(self.state["rounds"])
        result = copilot_auto._auto_recover_rejected(
            self.args(), self.path, self.state, self.call(path))
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["normalizer_version"],
                         reply_protocol.NORMALIZER_VERSION)
        self.assertIn(("evidence_ids", "evidence_refs"),
                      [(m["from"], m["to"]) for m in result["mapping"]])
        self.assertEqual(len(self.state["rounds"]), before_rounds)
        target = self.state["rounds"][-1]
        self.assertFalse(target["rejected"])
        self.assertTrue(target["recovered"])
        self.assertEqual(target["call_id"], "call-rejected-1")
        self.assertEqual(self.criterion("C1")["status"], "met")
        self.assertEqual([r["id"] for r in self.criterion("C1")["evidence_refs"]],
                         ["E1"])

    def test_conflicting_alias_is_rejected_without_state_change(self):
        reply = self.reply({
            "decision": "stop", "verdict": "pass", "summary": "冲突",
            "criteria": [
                {"id": "C1", "status": "met", "evidence": ["E1"],
                 "evidence_ids": ["E2"]},
                {"id": "C2", "status": "met", "evidence_ids": ["E2"]},
                {"id": "C3", "status": "met", "evidence_ids": ["E3"]},
            ],
        })
        path = self.rejected_round(reply)
        before = self.fingerprint()
        result = copilot_auto._auto_recover_rejected(
            self.args(), self.path, self.state, self.call(path))
        self.assertEqual(result["status"], "rejected")
        self.assertIn("dry-run", result["reason"])
        self.assertEqual(self.fingerprint(), before)
        self.assertTrue(self.state["rounds"][-1]["rejected"])

    def test_empty_id_is_rejected_without_state_change(self):
        reply = self.reply({
            "decision": "stop", "verdict": "pass", "summary": "空 id",
            "criteria": [
                {"id": "", "status": "met", "evidence_ids": ["E1"]},
                {"id": "C2", "status": "met", "evidence_ids": ["E2"]},
                {"id": "C3", "status": "met", "evidence_ids": ["E3"]},
            ],
        })
        path = self.rejected_round(reply)
        before = self.fingerprint()
        result = copilot_auto._auto_recover_rejected(
            self.args(), self.path, self.state, self.call(path))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.fingerprint(), before)

    def test_credential_failure_is_not_auto_recovered(self):
        reply = self.reply({
            "decision": "stop", "verdict": "pass", "summary": "不会被恢复",
            "criteria": [
                {"id": "C1", "status": "met", "evidence_ids": ["E1"]},
                {"id": "C2", "status": "met", "evidence_ids": ["E2"]},
                {"id": "C3", "status": "met", "evidence_ids": ["E3"]},
            ],
        })
        path = self.rejected_round(reply)
        with open(self.audit, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "event": "call_post_failure", "call_id": "call-rejected-1",
                "round_id": self.state["rounds"][-1]["round_id"],
                "error": "dispatch_credential_rejected", "detail": "凭证失败",
            }, ensure_ascii=False) + "\n")
        before = self.fingerprint()
        result = copilot_auto._auto_recover_rejected(
            self.args(), self.path, self.state, self.call(path))
        self.assertEqual(result["status"], "skipped")
        self.assertIn("dispatch_plan_rejected", result["reason"])
        self.assertEqual(self.fingerprint(), before)

    def test_stale_rejection_is_not_auto_recovered(self):
        reply = self.reply({
            "decision": "stop", "verdict": "pass", "summary": "不会被恢复",
            "criteria": [
                {"id": "C1", "status": "met", "evidence_ids": ["E1"]},
                {"id": "C2", "status": "met", "evidence_ids": ["E2"]},
                {"id": "C3", "status": "met", "evidence_ids": ["E3"]},
            ],
        })
        path = self.rejected_round(reply)
        with open(self.audit, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "event": "call_post_failure", "call_id": "other-call",
                "round_id": "other-round",
                "error": "dispatch_plan_rejected", "detail": "旧失败",
            }, ensure_ascii=False) + "\n")
        before = self.fingerprint()
        result = copilot_auto._auto_recover_rejected(
            self.args(), self.path, self.state, self.call(path))
        self.assertEqual(result["status"], "skipped")
        self.assertIn("不匹配", result["reason"])
        self.assertEqual(self.fingerprint(), before)


class DriveAutoRecoveryHookTests(unittest.TestCase):
    """drive 的 exit=2 分支必须调用自动恢复，且不在同一次 drive 再发请求。"""

    def test_exit_two_attempts_recovery_once_and_skips_second_call(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "dispatch.json")
        audit = os.path.join(tmp.name, "audit.jsonl")
        copilot_dispatch.start(
            path, "驱动恢复接线", mode="PLAN",
            contract="完成条件：\n- 条件一\n边界：测试",
            task_id="task-drive-hook", criteria=["条件一"], risk="normal")
        args = types.SimpleNamespace(
            state=path, audit_log=audit,
            work_dir=os.path.join(tmp.name, "auto"),
            result_file="", native_result="", evidence=[],
            no_auto_evidence=False, question="", question_file="",
            recover_reply="", base_url="", timeout=5.0,
            max_output_tokens=4096, max_retries=0, dry_run=False)
        reply_path = os.path.join(tmp.name, "reply.md")
        with open(reply_path, "w", encoding="utf-8") as fh:
            fh.write("```copilot-plan\n"
                     + json.dumps({"decision": "continue", "verdict": "partial",
                                   "summary": "测试"})
                     + "\n```\n")
        with mock.patch.object(copilot_auto, "_call_gpt",
                               return_value={
                                   "exit": 2, "stderr": "", "stdout": "",
                                   "prompt_file": "", "reply_file": reply_path,
                                   "evidence": []}) as call, \
             mock.patch.object(copilot_auto, "_auto_recover_rejected",
                               return_value={"status": "applied",
                                             "call_id": "c1",
                                             "round_id": "r1"}) as recover:
            code = copilot_auto.drive(args)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(recover.call_count, 1)
        self.assertIn(code, (copilot_auto.EXIT_OK, copilot_auto.EXIT_EXECUTE,
                             copilot_auto.EXIT_WAIT))


if __name__ == "__main__":
    unittest.main()

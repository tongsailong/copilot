#!/usr/bin/env python3
"""v9.3 加固回归：破坏性合同修订、条件包装项同步、恢复项家族、重开进展、多阻塞、
SOL 单轮键与端点严格性、finding 证据列表。

这些行为都在 v9.3 变更，测试按“状态迁移 + CLI 退出码”断言，而不是只看提示文本。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import governance as G  # noqa: E402
import cognition_call as CC  # noqa: E402
import v6_support as F  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件
- 兼容旧接口
"""

SINGLE = """## 完成条件
- 产出可复核的结果文件
"""

RENAMED = """## 完成条件
- 产出可复核的结果文件
- 向后兼容旧接口
"""

PLAN = {"summary": "实现", "actions": [
    {"id": "A1", "title": "实现", "goal": "完成实现", "expected_evidence": "diff"},
]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state_path = os.path.join(self.dir, "state.json")

    def start(self, contract: str = CONTRACT, *, plan: bool = True) -> None:
        G.start_state(self.state_path, goal="v9.3 加固", contract=contract,
                      test_policy={"required": False}, workspace=self.dir)
        if plan:
            G.plan_state(self.state_path, PLAN)

    def state(self) -> dict:
        return G.load_state(self.state_path)

    def evidence(self, name: str = "E1", text: str = "ok\n") -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        state = self.state()
        G.register_evidence(state, name, file_path=path, note="结果")
        G.save_state(self.state_path, state)
        return name


class ContractAmendmentGuardTests(Base):
    def test_rename_without_net_removal_needs_no_authorization(self):
        self.start()
        result = G.amend_contract(self.state_path, contract=RENAMED)
        self.assertEqual(len(result["criteria"]), 2)
        state = self.state()
        entry = state["contract"]["history"][-1]
        self.assertEqual(entry["event"], "contract_amended")
        self.assertEqual(entry["authorized_by"], "")
        # 改写措辞仍留审计痕迹，但不要求用户授权。
        self.assertEqual([c["criterion_id"] for c in entry["removed_criteria"]],
                         ["C2"])

    def test_net_removal_requires_explicit_user_authorization(self):
        self.start()
        with self.assertRaisesRegex(G.GovernanceError, "破坏性修订"):
            G.amend_contract(self.state_path, contract=SINGLE)
        # 未授权时状态未被改写。
        self.assertEqual(len(self.state()["contract"]["criteria"]), 2)
        # 只声明 user 但不写用户要求，仍然拒绝：自报身份不是授权证据。
        with self.assertRaisesRegex(G.GovernanceError, "授权说明"):
            G.amend_contract(self.state_path, contract=SINGLE,
                             authorized_by="user")
        self.assertEqual(len(self.state()["contract"]["criteria"]), 2)
        G.amend_contract(self.state_path, contract=SINGLE, authorized_by="user",
                         authorization_note="用户要求删除“兼容旧接口”条件")
        state = self.state()
        self.assertEqual(len(state["contract"]["criteria"]), 1)
        entry = state["contract"]["history"][-1]
        self.assertEqual(entry["authorized_by"], "user")
        self.assertEqual(entry["authorization_source"], "caller-declared")
        self.assertEqual(entry["authorization_note"],
                         "用户要求删除“兼容旧接口”条件")
        self.assertTrue(entry["destructive"])
        self.assertEqual([c["criterion_id"] for c in entry["removed_criteria"]],
                         ["C2"])
        self.assertEqual((entry["outstanding_before"],
                          entry["outstanding_after"]), (2, 1))
        self.assertEqual((entry["removed_unmet"], entry["added_unmet"]), (1, 0))

    def test_removing_met_criterion_is_not_destructive(self):
        self.start()
        eid = self.evidence()
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=[eid])
        # 待核销条数 1 → 1（C2 仍在），删掉已核销条件不算减少工作量。
        result = G.amend_contract(self.state_path, contract="## 完成条件\n- 兼容旧接口\n")
        self.assertEqual([c["criterion_id"] for c in result["criteria"]], ["C1"])

    def test_cli_requires_authorization_note_for_destructive_amend(self):
        self.start()
        path = os.path.join(self.dir, "contract-single.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(SINGLE)
        base = ["contract", "--state", self.state_path, "--amend",
                "--contract-file", path, "--authorized-by", "user"]
        proc = F.run_cli("copilot_dispatch.py", *base)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("授权说明", proc.stderr)
        self.assertEqual(len(self.state()["contract"]["criteria"]), 2)
        proc = F.run_cli("copilot_dispatch.py", *base,
                         "--authorization-note", "用户要求删除第二条条件")
        self.assertIn(proc.returncode, (0, 10))
        entry = self.state()["contract"]["history"][-1]
        self.assertEqual(entry["authorization_note"], "用户要求删除第二条条件")
        self.assertEqual(entry["authorization_source"], "caller-declared")

    def test_noop_amend_keeps_acceptance_criterion(self):
        self.start(contract=SINGLE)
        eid = self.evidence()
        state = self.state()
        report = G._acceptance_criterion(state)
        # 只有 PWW 流程会产生接受条件；这里直接构造其形态验证“空操作修订保留”。
        state["contract"]["criteria"].append({
            "criterion_id": "C2", "description": "PWW 用户接受条件",
            "status": "met", "evidence_refs": [], "acceptance": {
                "status": "accepted", "accepted_ids": ["W1"], "accepted_by": "user",
                "accepted_at": G.now_iso(), "quote": "我接受", "report_id": "VR-1",
                "binding_sha256": "x" * 64,
            },
            "updated_at": G.now_iso(), "history": [],
        })
        G.save_state(self.state_path, state)
        if report is not None:
            self.fail("夹具不应预先存在接受条件")
        G.amend_contract(self.state_path)
        state = self.state()
        acceptance = G._acceptance_criterion(state)
        self.assertIsNotNone(acceptance)
        self.assertEqual(acceptance["criterion_id"], "C2")
        self.assertEqual(acceptance["acceptance"]["status"], "accepted")
        self.assertEqual(acceptance["acceptance"]["accepted_ids"], ["W1"])
        self.assertEqual(acceptance["status"], "met")
        self.assertEqual(len(state["contract"]["criteria"]), 2)
        self.assertEqual([e["id"] for e in state["evidence"]], [eid])


class CriterionWorkItemSyncTests(Base):
    def test_criterion_met_drops_its_pending_wrapper(self):
        self.start(contract=SINGLE)
        state = self.state()
        G.derive_work(state)
        G.save_state(self.state_path, state)
        self.assertIn("AUTO-C1", [a["id"] for a in self.state()["actions"]])
        eid = self.evidence()
        G.record_action(self.state_path, "A1", "done", result="完成",
                        evidence_ids=[eid])
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=[eid])
        action = next(a for a in self.state()["actions"] if a["id"] == "AUTO-C1")
        self.assertEqual(action["status"], "dropped")
        self.assertEqual(action["auto_closed_for"], "C1")
        self.assertIn("随完成条件 C1 核销自动撤销", action["result"])
        # 条件 met 之后不再要求回填包装项：PASS/close，而不是永远差一条。
        self.assertEqual(G.evaluate(self.state())["phase"], "PASS")

    def test_criterion_back_to_pending_reopens_wrapper(self):
        self.start(contract=SINGLE)
        state = self.state()
        G.derive_work(state)
        G.save_state(self.state_path, state)
        eid = self.evidence()
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=[eid])
        G.record_criterion(self.state_path, "C1", "pending")
        action = next(a for a in self.state()["actions"] if a["id"] == "AUTO-C1")
        self.assertEqual(action["status"], "pending")
        self.assertEqual(action["auto_closed_for"], "")
        self.assertEqual(action["result"], "")

    def test_planned_action_targeting_criterion_is_not_auto_dropped(self):
        # 计划里的人写工作项即使声明 target_criteria，也可能有独立价值，不能被
        # 条件核销顺带撤销。
        self.start(contract=SINGLE)
        G.plan_state(self.state_path, {"summary": "实现", "actions": [
            {"id": "A1", "title": "实现", "goal": "完成实现",
             "expected_evidence": "diff", "target_criteria": ["C1"]},
        ]})
        eid = self.evidence()
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=[eid])
        action = next(a for a in self.state()["actions"] if a["id"] == "A1")
        self.assertEqual(action["status"], "pending")
        self.assertEqual(action.get("auto_closed_for") or "", "")

    def test_worked_wrapper_is_not_auto_dropped(self):
        # 已经产出结果/证据的包装项保留自己的执行记录，不能被“条件已 met”抹掉。
        self.start(contract=SINGLE)
        state = self.state()
        G.derive_work(state)
        G.save_state(self.state_path, state)
        eid = self.evidence()
        G.record_action(self.state_path, "AUTO-C1", "running", result="进行中")
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=[eid])
        action = next(a for a in self.state()["actions"] if a["id"] == "AUTO-C1")
        self.assertEqual(action["status"], "running")
        self.assertEqual(action.get("auto_closed_for") or "", "")


class RecoveryFamilyTests(Base):
    def test_recovery_ids_do_not_nest_after_restarts(self):
        self.start()
        state = self.state()
        state["actions"].append({
            "id": "AUTO-R-W1", "title": "处理停摆工作项 W1", "assignee": "zcode",
            "goal": "g", "constraints": "", "expected_evidence": "e",
            "depends_on": [], "status": "failed", "result": "失败",
            "evidence_ids": [], "fail_streak": 1, "origin": "recovery",
            "created": G.now_iso(), "updated": G.now_iso(),
        })
        G.save_state(self.state_path, state)
        state = self.state()
        first = [a["id"] for a in G.derive_work(state)]
        G.save_state(self.state_path, state)
        self.assertIn("AUTO-R-W1-R2", first)
        state = self.state()
        next(a for a in state["actions"]
             if a["id"] == "AUTO-R-W1-R2")["status"] = "failed"
        G.save_state(self.state_path, state)
        state = self.state()
        second = [a["id"] for a in G.derive_work(state)]
        G.save_state(self.state_path, state)
        self.assertIn("AUTO-R-W1-R3", second)
        ids = [a["id"] for a in self.state()["actions"]]
        self.assertFalse([aid for aid in ids if "AUTO-R-AUTO-R" in aid], ids)


class ReopenProgressTests(Base):
    def _reach_pass(self) -> None:
        self.start(contract=SINGLE)
        eid = self.evidence()
        G.record_action(self.state_path, "A1", "done", result="完成",
                        evidence_ids=[eid])
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=[eid])
        self.assertEqual(G.evaluate(self.state())["phase"], "PASS")

    def test_reopen_without_progress_cannot_reuse_old_pass(self):
        self._reach_pass()
        G.close_task(self.state_path)
        G.reopen_task(self.state_path, "用户要求补充验证", authorized_by="user")
        entry = self.state()["closure"]["reopen_history"][-1]
        # 重开授权同样是调用方自报：历史显式标注来源，避免当成可验证凭证。
        self.assertEqual(entry["authorization_source"], "caller-declared")
        self.assertEqual(entry["authorized_by"], "user")
        self.assertEqual(entry["reason"], "用户要求补充验证")
        evaluation = G.evaluate(self.state())
        self.assertEqual(evaluation["phase"], "PARTIAL")
        self.assertIn("重开后尚未产生新的实质进展", evaluation["reason"])
        self.assertTrue(evaluation["continuation"]["required"])
        with self.assertRaises(G.GovernanceError):
            G.close_task(self.state_path)

    def test_new_evidence_after_reopen_allows_pass_again(self):
        self._reach_pass()
        G.close_task(self.state_path)
        G.reopen_task(self.state_path, "用户要求补充验证", authorized_by="user")
        self.evidence("E2", "补充验证\n")
        self.assertEqual(G.evaluate(self.state())["phase"], "PASS")

    def test_cli_reopen_records_caller_declared_source_in_history(self):
        """CLI 参数 -> reopen_task -> reopen_history 的来源标注必须端到端贯通。"""
        self._reach_pass()
        G.close_task(self.state_path)
        base = ["reopen", "--state", self.state_path,
                "--reason", "用户要求补充提交与推送验证"]
        proc = F.run_cli("copilot_dispatch.py", *base, "--authorized-by", "agent")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("用户授权", proc.stderr)
        self.assertFalse((self.state()["closure"] or {}).get("reopen_history"))
        proc = F.run_cli("copilot_dispatch.py", *base, "--authorized-by", "user")
        self.assertIn(proc.returncode, (0, 10))
        entry = self.state()["closure"]["reopen_history"][-1]
        self.assertEqual(entry["authorized_by"], "user")
        # 自报声明在历史里显式标注来源，CLI 传参与落盘一致。
        self.assertEqual(entry["authorization_source"], "caller-declared")
        self.assertEqual(entry["reason"], "用户要求补充提交与推送验证")


class MultiBlockResolutionTests(Base):
    def test_item_stays_blocked_until_every_block_resolves(self):
        self.start()
        first = G.add_block(self.state_path, "缺少 push 授权", "用户授权 push",
                            work_items=["A1"])
        second = G.add_block(self.state_path, "等待用户选择方案", "用户决定方案",
                             work_items=["A1"])
        self.assertNotEqual(first["block_id"], second["block_id"])
        result = G.resolve_block(self.state_path, "用户已授权 push",
                                 block_id=first["block_id"])
        self.assertEqual(result["resumed"], [])
        state = self.state()
        action = next(a for a in state["actions"] if a["id"] == "A1")
        self.assertEqual(action["status"], "blocked")
        self.assertEqual(action["blocked_by"], second["block_id"])
        self.assertEqual(action["blocked_reason"], "等待用户选择方案")
        result = G.resolve_block(self.state_path, "用户已决定方案",
                                 block_id=second["block_id"])
        self.assertEqual(result["resumed"], ["A1"])
        action = next(a for a in self.state()["actions"] if a["id"] == "A1")
        self.assertEqual(action["status"], "pending")
        self.assertIsNone(action["blocked_by"])


class SolSingleRoundKeyTests(Base):
    def test_surrounding_whitespace_does_not_create_second_paid_call(self):
        self.start()
        state = self.state()
        left = CC.review_subject_sha256(state, "  \n材料正文\n\n", mode="CONSULT")
        right = CC.review_subject_sha256(state, "材料正文", mode="CONSULT")
        self.assertEqual(left, right)

    def test_prompt_template_change_invalidates_cached_decision(self):
        self.start()
        state = self.state()
        before = CC.review_subject_sha256(state, "材料", mode="CONSULT")
        with mock.patch.object(CC, "prompt_template_sha256",
                               return_value="0" * 16):
            after = CC.review_subject_sha256(state, "材料", mode="CONSULT")
        self.assertNotEqual(before, after)

    def test_force_overrides_same_materials_reuse(self):
        state = {"cognition_calls": [{"status": "completed", "mode": "REVIEW",
                                "model": CC.DEFAULT_MODEL,
                                "requested_model": CC.DEFAULT_MODEL,
                                "actual_model": CC.DEFAULT_MODEL,
                                "materials_sha256": "a" * 64}]}
        self.assertEqual(CC.single_round_decision(
            state, "a" * 64, mode="REVIEW")["action"], "reuse")
        forced = CC.single_round_decision(
            state, "a" * 64, mode="REVIEW", force=True,
            trigger="scope-changed", reason="用户要求重新复核")
        self.assertEqual(forced["action"], "call")

    def test_cpa_url_rejects_query_and_extra_paths(self):
        self.assertTrue(CC._is_local_cpa_url("http://127.0.0.1:8317"))
        self.assertTrue(CC._is_local_cpa_url("http://127.0.0.1:8317/v1"))
        self.assertTrue(CC._is_local_cpa_url("http://127.0.0.1:8317/v1/"))
        self.assertFalse(CC._is_local_cpa_url("http://127.0.0.1:8317/v1?token=x"))
        self.assertFalse(CC._is_local_cpa_url("http://127.0.0.1:8317/v1#frag"))
        self.assertFalse(CC._is_local_cpa_url("http://127.0.0.1:8317/v1/responses"))
        self.assertFalse(CC._is_local_cpa_url("http://127.0.0.1:9999/v1"))


class KnowledgeHandoffOptionalTests(unittest.TestCase):
    def test_missing_extension_fails_only_when_requested(self):
        import copilot_dispatch as D
        self.assertEqual(D._compose_materials("材料", ""), ("材料", {}))
        saved = sys.modules.get("knowledge_handoff")
        sys.modules["knowledge_handoff"] = None
        try:
            with self.assertRaisesRegex(D.CLIError, "KNOWLEDGE_EXTENSION_MISSING"):
                D._compose_materials("材料", "/tmp/handoff.json")
        finally:
            if saved is None:
                sys.modules.pop("knowledge_handoff", None)
            else:
                sys.modules["knowledge_handoff"] = saved


class FindingEvidenceListTests(Base):
    def test_finding_accepts_comma_separated_evidence(self):
        self.start()
        state = self.state()
        G.register_evidence(state, "E1", text="修复输出")
        G.register_evidence(state, "E2", text="定向测试输出")
        state["finding_ledger"] = {
            "schema": "copilot-finding-ledger/v1", "next_id": 2,
            "items": [{
                "finding_id": "F1", "severity": "major", "status": "open",
                "file": "src/a.py", "summary": "问题", "occurrences": 1,
            }],
        }
        G.save_state(self.state_path, state)
        proc = F.run_cli("copilot_dispatch.py", "finding", "--state",
                         self.state_path, "--id", "F1", "--status", "fixed",
                         "--evidence", "E1,E2")
        self.assertEqual(proc.returncode, 10, proc.stderr)
        state = self.state()
        item = state["finding_ledger"]["items"][0]
        self.assertEqual(item["history"][-1]["status"], "fixed")
        self.assertEqual(item["history"][-1]["evidence_ids"], ["E1", "E2"])


if __name__ == "__main__":
    unittest.main()

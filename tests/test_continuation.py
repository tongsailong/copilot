#!/usr/bin/env python3
"""续跑闭环回归：PARTIAL 不是终点、单项阻塞不冻结任务、授权范围按工作项。

覆盖用户要求的 15 个场景，全部断言真实状态迁移与 CLI 退出码，而不是提示文本：

1. PARTIAL 仍有未闭合条件 → 必须继续（derive/execute），不得停在报告；
2. 单项 BLOCKED，其余工作项照常可执行；
3. 未授权 commit 只阻塞 commit，测试/修复继续；
4. 未授权 push 只阻塞 push，本地已授权工作继续；
5. 等待官方回复时，不依赖它的工作继续；
6. 阶段报告不自动 CLOSE，报告后可执行工作仍要求继续；
7. 主代理回填阶段结果后派生/派发下一步，而不是结束；
8. 派发/执行失败可恢复，工作项不丢失；
9. 上下文恢复后复用既有证据与进度继续；
10. 已完成的工作项与已核销条件不被重复派生；
11. 同一失败路径不无限重复，转为替代路线（AUTO-REPLAN）；
12. 仅当全部剩余工作真实阻塞时才整体 BLOCKED；
13. 全部条件满足 → 正常 PASS 并收口；
14. 用户补充授权后只恢复对应工作项；
15. 用户决策只影响对应工作项，不冻结无关工作。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import governance as G  # noqa: E402
import v6_support as F  # noqa: E402

CONTRACT = """## 完成条件
- 代码改动完成
- 推送完成

## 边界
- 不修改无关项目
"""

PLAN = {"summary": "实现并推送", "actions": [
    {"id": "A1", "title": "实现并测试", "goal": "完成代码改动与测试",
     "expected_evidence": "diff 与测试输出"},
    {"id": "A2", "title": "推送", "goal": "推送到远端",
     "expected_evidence": "远端分支"},
]}


class ContinuationBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="续跑回归", contract=CONTRACT,
                      test_policy={"required": False}, workspace=self.dir)
        G.plan_state(self.state_path, PLAN)

    # ── 夹具 ──────────────────────────────────────────────────────────────
    def state(self) -> dict:
        return G.load_state(self.state_path)

    def ev(self) -> dict:
        return G.evaluate(self.state())

    def cont(self) -> dict:
        return self.ev().get("continuation") or {}

    def ready(self) -> list[str]:
        return [a["id"] for a in G.ready_actions(self.state())]

    def evidence(self, name: str = "E1", text: str = "ok\n") -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        state = self.state()
        G.register_evidence(state, name, file_path=path, note="结果")
        G.save_state(self.state_path, state)
        return name

    def block(self, aid: str, *, criteria: list[str] | None = None,
              reason: str = "缺少授权", needs: str = "用户明确授权") -> str:
        block = G.add_block(self.state_path, reason, needs,
                            work_items=[aid], criteria=criteria or [])
        return block["block_id"]


class PartialContinuationTests(ContinuationBase):
    def test_01_partial_with_open_criteria_requires_continuation(self):
        # 计划内工作项全部回填，但完成条件未核销：PARTIAL 必须继续派生。
        G.record_action(self.state_path, "A1", "done", result="已完成")
        G.record_action(self.state_path, "A2", "done", result="已完成")
        evaluation = self.ev()
        self.assertEqual(evaluation["phase"], "PARTIAL")
        cont = evaluation["continuation"]
        self.assertTrue(cont["required"])
        self.assertEqual(cont["action"], "derive")
        self.assertIn("AUTO-C1", cont["derivable"])
        # 派生后进入可执行状态，仍然必须继续。
        state = self.state()
        G.derive_work(state)
        G.save_state(self.state_path, state)
        self.assertEqual(self.ev()["phase"], "RUNNING")
        self.assertEqual(self.cont()["action"], "execute")

    def test_02_single_blocked_item_does_not_freeze_others(self):
        self.block("A2")
        self.assertEqual(self.ready(), ["A1"])
        evaluation = self.ev()
        self.assertEqual(evaluation["phase"], "RUNNING")
        self.assertNotEqual(evaluation["phase"], "BLOCKED")
        cont = evaluation["continuation"]
        self.assertTrue(cont["required"])
        self.assertEqual(cont["action"], "execute")
        self.assertIn("A1", cont["executable"])

    def test_03_unauthorized_commit_blocks_only_commit(self):
        # 计划里 A2 代表 commit；缺授权只冻结 A2，测试与修复（A1）继续。
        self.block("A2", criteria=["C2"], reason="未获 commit 授权")
        self.assertIn("A1", self.ready())
        self.assertNotIn("A2", self.ready())
        self.assertEqual(self.cont()["action"], "execute")
        state = self.state()
        action = next(a for a in state["actions"] if a["id"] == "A2")
        self.assertEqual(action["status"], "blocked")
        self.assertEqual(
            next(a for a in state["actions"] if a["id"] == "A1")["status"],
            "pending")

    def test_04_unauthorized_push_keeps_local_work_running(self):
        self.block("A2", criteria=["C2"], reason="未获 push 授权",
                   needs="用户明确授权 push")
        proc = F.run_cli("copilot_dispatch.py", "report", "--state",
                         self.state_path, "--summary", "阶段报告：A1 待执行")
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
        self.assertIn("必须继续", proc.stdout)
        self.assertIn("A1", proc.stdout)

    def test_05_waiting_external_reply_does_not_stop_independent_work(self):
        self.block("A1", reason="等待官方工单回复", needs="官方答复")
        self.assertEqual(self.ready(), ["A2"])
        proc = F.run_cli("copilot_auto.py", "drive", "--state", self.state_path)
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
        self.assertIn("A2", proc.stdout)

    def test_06_stage_report_is_not_terminal(self):
        report = G.add_report(self.state_path, "阶段报告：待回填")
        self.assertTrue(report)
        state = self.state()
        self.assertFalse(bool((state.get("closure") or {}).get("closed")))
        self.assertNotEqual(self.ev()["phase"], "CLOSED")
        proc = F.run_cli("copilot_dispatch.py", "report", "--state",
                         self.state_path, "--summary", "阶段报告：待回填")
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
        self.assertIn("报告是进度记录", proc.stdout)
        self.assertNotIn("已收口", proc.stdout)

    def test_07_stage_result_derives_next_and_reports_it(self):
        G.record_action(self.state_path, "A1", "done", result="A1 完成")
        cont = self.cont()
        self.assertTrue(cont["required"])
        self.assertEqual(cont["action"], "execute")
        self.assertIn("A2", cont["executable"])
        proc = F.run_cli("copilot_dispatch.py", "record", "--state",
                         self.state_path, "--id", "A2", "--status", "done",
                         "--result", "A2 完成")
        # 两个工作项都完成但条件未核销：record 必须继续要求派生。
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
        self.assertIn("derive", proc.stdout)

    def test_08_failure_is_recoverable_without_losing_items(self):
        G.record_action(self.state_path, "A2", "done", result="A2 完成")
        G.record_action(self.state_path, "A1", "failed", result="测试失败")
        state = self.state()
        self.assertIn("A1", [a["id"] for a in state["actions"]])
        self.assertNotEqual(self.ev()["phase"], "BLOCKED")
        cont = self.cont()
        self.assertTrue(cont["required"])
        self.assertIn("AUTO-R-A1", cont["derivable"])
        # 恢复工作项由状态机派生，失败项本身保留原始结果。
        state = self.state()
        created = G.derive_work(state)
        G.save_state(self.state_path, state)
        recovery = [a for a in created if a["id"] == "AUTO-R-A1"]
        self.assertTrue(recovery)
        self.assertEqual(recovery[0]["status"], "pending")
        self.assertIn("AUTO-R-A1", self.ready())
        original = next(a for a in self.state()["actions"] if a["id"] == "A1")
        self.assertEqual(original["status"], "failed")
        self.assertEqual(original["result"], "测试失败")

    def test_09_context_recovery_reuses_evidence_and_progress(self):
        eid = self.evidence()
        G.record_action(self.state_path, "A1", "done", result="完成",
                        evidence_ids=[eid])
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=[eid])
        # 模拟新上下文：重新从磁盘加载，不依赖任何内存对象。
        recovered = G.load_state(self.state_path)
        self.assertEqual(
            next(a for a in recovered["actions"] if a["id"] == "A1")["status"],
            "done")
        self.assertEqual(
            next(c for c in recovered["contract"]["criteria"]
                 if c["criterion_id"] == "C1")["status"], "met")
        self.assertTrue(recovered["evidence"][0].get("sha256"))
        cont = G.continuation(recovered)
        self.assertTrue(cont["required"])
        self.assertIn(cont["action"], {"execute", "derive"})

    def test_10_completed_work_is_not_rederived(self):
        eid = self.evidence()
        G.record_action(self.state_path, "A1", "done", result="完成",
                        evidence_ids=[eid])
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=[eid])
        state = self.state()
        G.derive_work(state)
        G.save_state(self.state_path, state)
        state = self.state()
        G.derive_work(state)
        G.save_state(self.state_path, state)
        ids = [a["id"] for a in self.state()["actions"]]
        # 已核销的 C1 不会再派生；未核销的 C2 只派生一次。
        self.assertFalse([aid for aid in ids if aid.startswith("AUTO-C1")])
        self.assertEqual(len([aid for aid in ids if aid.startswith("AUTO-C2")]), 1)

    def test_11_repeated_failure_switches_to_alternative_path(self):
        state = self.state()
        created = G.derive_work(state)
        G.save_state(self.state_path, state)
        aid = created[0]["id"]
        for attempt in range(3):
            G.record_action(self.state_path, aid, "failed",
                            result=f"第 {attempt + 1} 次失败")
            state = self.state()
            created = G.derive_work(state)
            G.save_state(self.state_path, state)
            if created:
                aid = created[0]["id"]
        ids = [a["id"] for a in self.state()["actions"]]
        self.assertIn("AUTO-REPLAN", ids)
        self.assertFalse([i for i in ids if i.endswith("-R4")])
        # 再派生一次不应复制第二条替代路线。
        state = self.state()
        created = G.derive_work(state)
        G.save_state(self.state_path, state)
        self.assertFalse([a for a in created if a["id"] == "AUTO-REPLAN"])

    def test_11b_finished_replan_with_open_gap_keeps_concrete_next_step(self):
        # 无进展保护不能变成静默停摆：替代路线已结束、原条件仍未闭合时，
        # 必须继续给出具体工作项（新的 REPLAN，说明上一路线为何未闭合）。
        G.record_action(self.state_path, "A1", "done", result="已完成")
        G.record_action(self.state_path, "A2", "done", result="已完成")
        state = self.state()
        created = G.derive_work(state)
        G.save_state(self.state_path, state)
        aid = created[0]["id"]
        for attempt in range(3):
            G.record_action(self.state_path, aid, "failed",
                            result=f"第 {attempt + 1} 次失败")
            state = self.state()
            created = G.derive_work(state)
            G.save_state(self.state_path, state)
            if created:
                aid = created[0]["id"]
        G.record_action(self.state_path, "AUTO-REPLAN", "done",
                        result="改用另一条路线，但未闭合")
        # 同批派生的其它缺口项一并回填，只留下 C1 这一条未闭合缺口。
        eid = self.evidence("E9")
        G.record_criterion(self.state_path, "C2", "met", evidence_ids=[eid])
        for action in self.state()["actions"]:
            if action["status"] in ("pending", "running"):
                G.record_action(self.state_path, action["id"], "done",
                                result="已回填")
        self.assertEqual(self.ev()["phase"], "PARTIAL")
        cont = self.cont()
        self.assertTrue(cont["required"], cont)
        self.assertEqual(cont["action"], "derive")
        self.assertTrue(cont["derivable"], cont)
        state = self.state()
        created = G.derive_work(state)
        G.save_state(self.state_path, state)
        replans = [a for a in created if a["id"].startswith("AUTO-REPLAN")]
        self.assertEqual([a["id"] for a in replans], ["AUTO-REPLAN-R2"])
        self.assertIn("上一替代路线未闭合", replans[0]["goal"])
        self.assertEqual(self.ev()["phase"], "RUNNING")
        self.assertEqual(self.cont()["action"], "execute")

    def test_12_task_blocked_only_when_all_remaining_blocked(self):
        self.block("A2", criteria=["C2"])
        self.assertNotEqual(self.ev()["phase"], "BLOCKED")
        eid = self.evidence()
        G.record_action(self.state_path, "A1", "done", result="完成",
                        evidence_ids=[eid])
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=[eid])
        evaluation = self.ev()
        self.assertEqual(evaluation["phase"], "BLOCKED")
        cont = evaluation["continuation"]
        self.assertFalse(cont["required"])
        self.assertEqual(cont["action"], "wait")
        self.assertEqual([i["id"] for i in cont["blocked_items"]], ["A2"])

    def test_12b_dependency_blocked_items_count_as_remaining(self):
        G.plan_state(self.state_path, {"summary": "串联", "actions": [
            {"id": "B1", "title": "实现", "goal": "g",
             "expected_evidence": "e"},
            {"id": "B2", "title": "推送", "goal": "g",
             "expected_evidence": "e", "depends_on": ["B1"]},
            {"id": "B3", "title": "发布", "goal": "g",
             "expected_evidence": "e", "depends_on": ["B2"]},
        ]})
        eid = self.evidence("E2")
        G.record_action(self.state_path, "A1", "done", result="完成",
                        evidence_ids=[eid])
        G.record_action(self.state_path, "A2", "done", result="完成",
                        evidence_ids=[eid])
        G.record_action(self.state_path, "B1", "done", result="完成",
                        evidence_ids=[eid])
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=[eid])
        self.block("B2", criteria=["C2"])
        evaluation = self.ev()
        self.assertEqual(evaluation["phase"], "BLOCKED")
        self.assertIn("B3", evaluation.get("dependency_blocked") or [])

    def test_12c_explicit_task_block_freezes_everything(self):
        G.add_block(self.state_path, "目标级阻塞", "用户重新定义目标")
        self.assertEqual(self.ev()["phase"], "BLOCKED")
        self.assertEqual(self.ready(), [])

    def test_12d_partial_unblock_reopens_task_without_faking_blocked(self):
        # BLOCKED 的前提由状态迁移验证，而不是只看某次输出的自洽性：
        # 两条项级阻塞覆盖全部剩余工作时是 BLOCKED；解除其中一条后必须回到
        # RUNNING，另一条阻塞仍保留，不能继续声称整体 BLOCKED。
        first = self.block("A1", criteria=["C1"])
        self.block("A2", criteria=["C2"])
        evaluation = self.ev()
        self.assertEqual(evaluation["phase"], "BLOCKED", evaluation)
        self.assertFalse(evaluation["continuation"]["required"])
        G.resolve_block(self.state_path, "用户已授权方案一", block_id=first)
        evaluation = self.ev()
        self.assertEqual(evaluation["phase"], "RUNNING", evaluation)
        self.assertEqual(evaluation["continuation"]["action"], "execute")
        self.assertEqual(evaluation["continuation"]["executable"], ["A1"])
        self.assertEqual([i["id"] for i in evaluation["blocked_items"]], ["A2"])

    def test_13_all_conditions_met_reaches_pass_and_closes(self):
        eid = self.evidence()
        G.record_action(self.state_path, "A1", "done", result="完成",
                        evidence_ids=[eid])
        G.record_action(self.state_path, "A2", "done", result="完成",
                        evidence_ids=[eid])
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=[eid])
        G.record_criterion(self.state_path, "C2", "met", evidence_ids=[eid])
        evaluation = self.ev()
        self.assertEqual(evaluation["phase"], "PASS", evaluation)
        self.assertEqual(evaluation["continuation"]["action"], "close")
        closure = G.close_task(self.state_path)
        self.assertTrue(closure["closed"])
        closed = self.ev()
        self.assertEqual(closed["phase"], "CLOSED")
        self.assertFalse(closed["continuation"]["required"])

    def test_14_authorization_update_resumes_only_that_item(self):
        block_id = self.block("A2", criteria=["C2"])
        result = G.resolve_block(self.state_path, "用户已授权 push",
                                 block_id=block_id)
        self.assertEqual(result["resumed"], ["A2"])
        self.assertIn("A2", self.ready())
        state = self.state()
        action = next(a for a in state["actions"] if a["id"] == "A2")
        self.assertEqual(action["status"], "pending")
        self.assertEqual(action.get("blocked_by"), None)
        self.assertEqual(self.cont()["action"], "execute")

    def test_15_user_decision_affects_only_its_item(self):
        first = self.block("A1", reason="等待用户选择方案")
        second = self.block("A2", criteria=["C2"], reason="等待 push 授权")
        self.assertNotEqual(first, second)
        G.resolve_block(self.state_path, "用户已选择方案", block_id=first)
        self.assertIn("A1", self.ready())
        state = self.state()
        self.assertEqual(
            next(a for a in state["actions"] if a["id"] == "A1")["status"],
            "pending")
        self.assertEqual(
            next(a for a in state["actions"] if a["id"] == "A2")["status"],
            "blocked")


class BlockScopeValidationTests(ContinuationBase):
    def test_block_requires_work_item_ids_to_exist(self):
        with self.assertRaises(G.GovernanceError):
            G.add_block(self.state_path, "缺授权", "授权", work_items=["A9"])

    def test_block_requires_known_criterion_ids(self):
        with self.assertRaises(G.GovernanceError):
            G.add_block(self.state_path, "缺授权", "授权",
                        work_items=["A2"], criteria=["C9"])

    def test_cli_block_rejects_item_and_task_scope_together(self):
        proc = F.run_cli("copilot_dispatch.py", "block", "--state",
                         self.state_path, "--reason", "缺授权",
                         "--needs", "授权", "--work-item", "A2",
                         "--scope", "task")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("不能同时使用", proc.stdout + proc.stderr)

    def test_cli_block_requires_explicit_scope(self):
        proc = F.run_cli("copilot_dispatch.py", "block", "--state",
                         self.state_path, "--reason", "缺授权",
                         "--needs", "授权")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("--work-item", proc.stdout + proc.stderr)

    def test_blocked_criterion_is_not_derived_repeatedly(self):
        self.block("A2", criteria=["C2"])
        state = self.state()
        created = G.derive_work(state)
        G.save_state(self.state_path, state)
        ids = [a["id"] for a in created]
        self.assertNotIn("AUTO-C2", ids)
        self.assertIn("AUTO-C1", ids)


class DriveExitCodeTests(ContinuationBase):
    def test_drive_returns_continue_when_work_remains(self):
        proc = F.run_cli("copilot_auto.py", "drive", "--state", self.state_path)
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)

    def test_drive_returns_wait_only_when_all_remaining_blocked(self):
        eid = self.evidence()
        G.record_action(self.state_path, "A1", "done", result="完成",
                        evidence_ids=[eid])
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=[eid])
        self.block("A2", criteria=["C2"])
        proc = F.run_cli("copilot_auto.py", "drive", "--state", self.state_path)
        self.assertEqual(proc.returncode, 11, proc.stdout + proc.stderr)

    def test_criterion_command_reports_continuation(self):
        eid = self.evidence()
        G.record_action(self.state_path, "A1", "done", result="完成",
                        evidence_ids=[eid])
        proc = F.run_cli("copilot_dispatch.py", "criterion", "--state",
                         self.state_path, "--id", "C1", "--status", "met",
                         "--evidence", eid)
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)


class CriterionAttributionTests(ContinuationBase):
    """阻塞必须能归属到完成条件，否则条件悬空会被反复派生。"""

    def test_blocking_derived_autoc_auto_links_its_criterion(self):
        G.record_action(self.state_path, "A1", "done", result="完成")
        G.record_action(self.state_path, "A2", "done", result="完成")
        state = self.state()
        created = G.derive_work(state)
        G.save_state(self.state_path, state)
        target = next(a for a in created if a["id"] == "AUTO-C1")
        self.assertEqual(target.get("target_criteria"), ["C1"])

        block = G.add_block(self.state_path, "缺少外部通道", "设备可用",
                            work_items=["AUTO-C1"])
        self.assertEqual(block["criteria"], ["C1"])
        self.assertIn("C1", G._blocked_criteria(self.state()))

    def test_blocking_plan_item_with_target_criteria_auto_links(self):
        state = self.state()
        for action in state["actions"]:
            if action["id"] == "A2":
                action["target_criteria"] = ["C2"]
        G.save_state(self.state_path, state)
        block = G.add_block(self.state_path, "缺少推送授权", "用户授权 push",
                            work_items=["A2"])
        self.assertEqual(block["criteria"], ["C2"])

    def test_explicit_criteria_wins_over_auto_link(self):
        state = self.state()
        for action in state["actions"]:
            if action["id"] == "A2":
                action["target_criteria"] = ["C2"]
        G.save_state(self.state_path, state)
        block = G.add_block(self.state_path, "缺少推送授权", "用户授权 push",
                            work_items=["A2"], criteria=["C1"])
        self.assertEqual(block["criteria"], ["C1"])

    def test_unattributed_criteria_get_attribution_command(self):
        G.record_action(self.state_path, "A1", "done", result="完成")
        self.block("A2")  # 未声明条件归属
        evaluation = self.ev()
        self.assertEqual(evaluation["phase"], "PARTIAL")
        self.assertEqual(evaluation.get("unattributed_criteria"), ["C1", "C2"])
        joined = " ".join(evaluation["next_commands"])
        self.assertIn("block --state", joined)
        self.assertIn("--criterion", joined)
        self.assertTrue(self.cont()["required"])
        self.assertIn("未归属任何阻塞项", self.cont()["reason"])

        # 补齐归属后进入真实 BLOCKED，不再派生执行不了的 AUTO-C。
        G.add_block(self.state_path, "缺少推送授权", "用户授权 push",
                    work_items=["A2"], criteria=["C1", "C2"])
        evaluation = self.ev()
        self.assertEqual(evaluation["phase"], "BLOCKED")
        self.assertFalse(evaluation["continuation"]["required"])

    def test_attribution_survives_next_evaluation(self):
        self.block("A2", criteria=["C2"])
        state = self.state()
        created = [a["id"] for a in G.derive_work(state)]
        G.save_state(self.state_path, state)
        self.assertIn("AUTO-C1", created)
        self.assertNotIn("AUTO-C2", created)


class ClosureCheckExitCodeTests(ContinuationBase):
    def test_closure_check_returns_nonzero_when_gates_fail(self):
        proc = F.run_cli("copilot_dispatch.py", "closure-check",
                         "--state", self.state_path)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("FAIL", proc.stdout)
        self.assertIn("不是任务终点", proc.stdout)

    def test_closure_check_returns_zero_when_gates_pass(self):
        eid = self.evidence()
        for aid in ("A1", "A2"):
            G.record_action(self.state_path, aid, "done", result="完成",
                            evidence_ids=[eid])
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=[eid])
        G.record_criterion(self.state_path, "C2", "met", evidence_ids=[eid])
        proc = F.run_cli("copilot_dispatch.py", "closure-check",
                         "--state", self.state_path)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PASS", proc.stdout)


if __name__ == "__main__":
    unittest.main()

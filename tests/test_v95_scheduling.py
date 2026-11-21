#!/usr/bin/env python3
"""v9.5 持续推进回归 C1–C9：调度、领取、恢复、阻塞与收口。

这些用例断言真实状态迁移、退出码、领取记录与证据绑定，而不是提示文本：

C1 PARTIAL 自动续跑：drive 派生后继续执行，不停在阶段报告；
C2 派生 → 调度 → 执行 → 回填 → 证据 → 再求值的完整闭环；
C3 单项阻塞只冻结该项，其余工作照常领取；
C4 用户补充授权后对应工作项恢复并重新进入调度；
C5 条件变化（合同新增完成条件）后重新求值并派生新工作项；
C6 并发领取去重、租约到期回收、回填/阻塞自动释放，工作项不永久丢失；
C7 上下文恢复后从状态文件继续执行，而不是只复述摘要；
C8 只有全部剩余工作真实阻塞才整体 BLOCKED，且必须带原因与所需条件；
C9 PASS 只在条件与门禁满足时产生，PARTIAL/BLOCKED 不被包装成 PASS。
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

# target_criteria 让计划工作项声明自己承载的条件：同一条件不会被两个会话
# 各派生一次 AUTO-C，也让“领取中”的阶段不会凭空长出重复工作项。
PLAN = {"summary": "实现并推送", "actions": [
    {"id": "A1", "title": "实现并测试", "goal": "完成代码改动与测试",
     "expected_evidence": "diff 与测试输出", "target_criteria": ["C1"]},
    {"id": "A2", "title": "推送", "goal": "推送到远端",
     "expected_evidence": "远端分支", "target_criteria": ["C2"]},
]}


class SchedulingBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="v9.5 持续推进",
                      contract=CONTRACT, test_policy={"required": False},
                      workspace=self.dir)
        G.plan_state(self.state_path, PLAN)

    # ── 夹具 ──────────────────────────────────────────────────────────────
    def state(self) -> dict:
        return G.load_state(self.state_path)

    def save(self, state: dict) -> None:
        G.save_state(self.state_path, state)

    def ev(self) -> dict:
        return G.evaluate(self.state())

    def cont(self) -> dict:
        return self.ev().get("continuation") or {}

    def ready(self, *, owner: str | None = None,
              include_claimed: bool = False) -> list[str]:
        return [a["id"] for a in G.ready_actions(
            self.state(), owner=owner, include_claimed=include_claimed)]

    def drive(self, *extra: str):
        return F.run_cli("copilot_auto.py", "drive", "--state",
                         self.state_path, *extra)

    def evidence(self, name: str = "E1", text: str = "ok\n") -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        state = self.state()
        G.register_evidence(state, name, file_path=path, note="结果")
        self.save(state)
        return name

    def record(self, aid: str, status: str = "done", *,
               evidence_ids: list[str] | None = None) -> None:
        G.record_action(self.state_path, aid, status, result=f"{aid} {status}",
                        evidence_ids=evidence_ids or [])

    def met(self, cid: str, eid: str) -> None:
        G.record_criterion(self.state_path, cid, "met", evidence_ids=[eid])

    def claim(self, ids: list[str], owner: str, ttl: int | None = None) -> dict:
        state = self.state()
        result = G.claim_actions(state, ids, owner=owner, ttl=ttl)
        self.save(state)
        return result


class C1PartialAutoContinueTests(SchedulingBase):
    def test_partial_derives_then_schedules_without_stopping(self):
        self.record("A1")
        self.record("A2")
        self.assertEqual(self.ev()["phase"], "PARTIAL")
        proc = self.drive()
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
        self.assertIn("已派生工作项", proc.stdout)
        self.assertIn("调度领取", proc.stdout)
        # 领取已落盘：不是只打印了下一步，而是真实交办。
        state = self.state()
        claims = G.active_claims(state)
        self.assertTrue(claims, state.get("claims"))
        self.assertIn(next(iter(claims)), ["AUTO-C1", "AUTO-C2"])
        self.assertNotIn("任务已收口", proc.stdout)

    def test_report_after_partial_still_exit_continue(self):
        self.record("A1")
        proc = F.run_cli("copilot_dispatch.py", "report", "--state",
                         self.state_path, "--summary", "阶段报告")
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
        self.assertIn("必须继续", proc.stdout)


class C2SchedulingClosureTests(SchedulingBase):
    def test_derive_schedule_execute_evidence_and_recheck(self):
        self.record("A1")
        self.record("A2")
        eid = self.evidence()
        # 第一次 drive：派生并领取一个缺口工作项。
        first = self.drive()
        self.assertEqual(first.returncode, 10, first.stdout + first.stderr)
        claimed = next(iter(G.active_claims(self.state())))
        self.record(claimed, evidence_ids=[eid])
        self.met(claimed.removeprefix("AUTO-"), eid)
        # 第二次 drive：继续领取剩余缺口，直到条件全部闭合。
        second = self.drive()
        self.assertEqual(second.returncode, 10, second.stdout + second.stderr)
        remaining = next(iter(G.active_claims(self.state())))
        self.record(remaining, evidence_ids=[eid])
        self.met(remaining.removeprefix("AUTO-"), eid)
        third = self.drive()
        self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
        state = self.state()
        self.assertTrue(state["closure"]["closed"])
        self.assertEqual(G.evaluate(state)["phase"], "CLOSED")
        # 执行结果与证据仍绑定在回填的工作项上。
        done = next(a for a in state["actions"] if a["id"] == claimed)
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["evidence_ids"], [eid])

    def test_claim_cleared_when_result_recorded(self):
        self.claim(["A1"], owner="sess_a")
        self.assertTrue(G.active_claims(self.state()))
        self.record("A1")
        self.assertEqual(G.active_claims(self.state()), {})


class C3ItemBlockIsolationTests(SchedulingBase):
    def test_blocking_one_item_does_not_freeze_others(self):
        proc = F.run_cli("copilot_dispatch.py", "block", "--state",
                         self.state_path, "--reason", "缺少 push 授权",
                         "--needs", "用户明确授权 push",
                         "--work-item", "A2", "--criterion", "C2")
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
        evaluation = self.ev()
        self.assertEqual(evaluation["phase"], "RUNNING", evaluation)
        self.assertEqual([i["id"] for i in evaluation["blocked_items"]], ["A2"])
        drive = self.drive()
        self.assertEqual(drive.returncode, 10, drive.stdout + drive.stderr)
        self.assertIn("A1", drive.stdout)
        self.assertNotIn("A2", G.active_claims(self.state()))
        # 阻塞项仍在状态里保留原因与所需条件。
        blocked = next(a for a in self.state()["actions"] if a["id"] == "A2")
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["blocked_needs"], "用户明确授权 push")

    def test_task_scope_block_requires_force_when_work_is_ready(self):
        denied = F.run_cli("copilot_dispatch.py", "block", "--state",
                           self.state_path, "--reason", "整体不可推进",
                           "--needs", "用户决策", "--scope", "task")
        self.assertEqual(denied.returncode, 2, denied.stdout + denied.stderr)
        self.assertIn("--work-item", denied.stdout + denied.stderr)
        forced = F.run_cli("copilot_dispatch.py", "block", "--state",
                           self.state_path, "--reason", "整体不可推进",
                           "--needs", "用户决策", "--scope", "task", "--force")
        self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
        self.assertIn("任务级 BLOCKED", forced.stdout)
        block = self.state()["blocks"][0]
        self.assertEqual(block["scope"], "task")
        self.assertEqual(block["ready_at_creation"], ["A1", "A2"])
        self.assertTrue(block["forced_scope"])
        self.assertEqual(G.evaluate(self.state())["phase"], "BLOCKED")


class C4AuthorizationRecoveryTests(SchedulingBase):
    def test_user_authorization_resumes_exact_item(self):
        eid = self.evidence()
        self.record("A1", evidence_ids=[eid])
        self.met("C1", eid)
        block = G.add_block(self.state_path, "缺少 push 授权",
                            "用户明确授权 push", work_items=["A2"],
                            criteria=["C2"])
        state = self.state()
        before = G.evaluate(state)
        self.assertEqual(before["phase"], "BLOCKED", before)
        denied = F.run_cli("copilot_dispatch.py", "unblock", "--state",
                           self.state_path, "--reason", "用户已授权",
                           "--block-id", block["block_id"], "--by", "user")
        self.assertEqual(denied.returncode, 2, denied.stdout + denied.stderr)
        self.assertIn("--quote", denied.stdout + denied.stderr)
        ok = F.run_cli("copilot_dispatch.py", "unblock", "--state",
                       self.state_path, "--reason", "用户已授权 push",
                       "--block-id", block["block_id"], "--by", "user",
                       "--quote", "可以 push 到 origin/main")
        self.assertEqual(ok.returncode, 10, ok.stdout + ok.stderr)
        self.assertIn("A2", ok.stdout)
        resolved = next(b for b in self.state()["blocks"]
                        if b["block_id"] == block["block_id"])
        self.assertEqual(resolved["resolved_by"], "user")
        self.assertEqual(resolved["authorization_quote"], "可以 push 到 origin/main")
        self.assertEqual(resolved["authorization_source"], "caller-declared")
        drive = self.drive()
        self.assertEqual(drive.returncode, 10, drive.stdout + drive.stderr)
        self.assertIn("A2", G.active_claims(self.state()))


class C5ConditionChangeTests(SchedulingBase):
    def test_new_condition_is_re_evaluated_and_derived(self):
        eid = self.evidence()
        self.record("A1", evidence_ids=[eid])
        self.record("A2", evidence_ids=[eid])
        self.met("C1", eid)
        self.met("C2", eid)
        self.assertEqual(self.ev()["phase"], "PASS")
        G.amend_contract(self.state_path,
                         criteria=["代码改动完成", "推送完成", "新增验收条件",
                                   "补充回归"],
                         authorized_by="user",
                         authorization_note="用户要求补充验收与回归")
        evaluation = self.ev()
        self.assertEqual(evaluation["phase"], "PARTIAL", evaluation)
        cont = evaluation["continuation"]
        self.assertTrue(cont["required"])
        self.assertEqual(cont["action"], "derive")
        self.assertIn("AUTO-C3", cont["derivable"])
        drive = self.drive()
        self.assertEqual(drive.returncode, 10, drive.stdout + drive.stderr)
        self.assertIn("AUTO-C3", G.active_claims(self.state()))

    def test_met_condition_is_not_derived_again(self):
        eid = self.evidence()
        self.record("A1")
        self.record("A2")
        self.met("C1", eid)
        state = self.state()
        created = [a["id"] for a in G.derive_work(state)]
        self.save(state)
        self.assertNotIn("AUTO-C1", created)
        self.assertIn("AUTO-C2", created)


class C6DuplicateSchedulingTests(SchedulingBase):
    def test_claim_prevents_concurrent_duplicate_execution(self):
        first = self.claim(["A1"], owner="sess_a")
        self.assertEqual(first["claimed"], ["A1"])
        # 其它会话看不到、也抢不到：不会重复执行同一工作项。
        self.assertEqual(self.ready(owner="sess_b"), ["A2"])
        second = self.claim(["A1"], owner="sess_b")
        self.assertEqual(second["claimed"], [])
        self.assertEqual(second["busy"], ["A1"])
        self.assertIn("A1", self.ready(owner="sess_a"))
        self.assertIn("A1", self.ready(owner="sess_b", include_claimed=True))

    def test_claimed_by_other_is_wait_not_blocked(self):
        self.claim(["A1"], owner="sess_other")
        # A2 阻塞、A1 被其它会话领取：没有本会话可执行项，但这不是整体阻塞。
        G.add_block(self.state_path, "缺少 push 授权", "用户授权",
                    work_items=["A2"], criteria=["C2"])
        evaluation = self.ev()
        self.assertEqual(evaluation["phase"], "PARTIAL", evaluation)
        cont = evaluation["continuation"]
        self.assertFalse(cont["required"])
        self.assertEqual(cont["action"], "wait")
        self.assertEqual(cont["claimed_by_others"], ["A1"])
        self.assertEqual(self.drive().returncode, 11)

    def test_expired_lease_is_reclaimable(self):
        self.claim(["A1"], owner="sess_a", ttl=0)
        takeover = self.claim(["A1"], owner="sess_b")
        self.assertEqual(takeover["claimed"], ["A1"])
        self.assertEqual(G.active_claims(self.state())["A1"]["owner"], "sess_b")

    def test_block_releases_claim(self):
        self.claim(["A1"], owner="sess_a")
        G.add_block(self.state_path, "等待用户选择", "用户决策",
                    work_items=["A1"], criteria=["C1"])
        self.assertEqual(G.active_claims(self.state()), {})

    def test_claim_survives_state_reload(self):
        self.claim(["A1"], owner="sess_a")
        reloaded = G.load_state(self.state_path)
        self.assertEqual(G.active_claims(reloaded)["A1"]["owner"], "sess_a")


class C7ContextRecoveryTests(SchedulingBase):
    def test_reload_continues_execution_from_state(self):
        eid = self.evidence()
        self.claim(["A1"], owner="sess_a")
        self.record("A1", evidence_ids=[eid])
        self.met("C1", eid)
        # 模拟 compaction：丢弃内存状态，只从磁盘恢复。
        recovered = G.load_state(self.state_path)
        evaluation = G.evaluate(recovered)
        cont = evaluation["continuation"]
        self.assertEqual(evaluation["phase"], "RUNNING")
        self.assertEqual(cont["action"], "execute")
        self.assertEqual(cont["executable"], ["A2"])
        self.assertEqual(G.active_claims(recovered), {})
        self.assertEqual(recovered["evidence"][0]["id"], eid)
        self.assertTrue(recovered["evidence"][0].get("sha256"))

    def test_restart_does_not_duplicate_completed_or_claimed_work(self):
        eid = self.evidence()
        self.record("A1", evidence_ids=[eid])
        self.met("C1", eid)
        self.claim(["A2"], owner="sess_old")
        cont = G.continuation(G.load_state(self.state_path))
        # 已完成工作不再派生，已领取工作不再执行；恢复后只剩等待或收口。
        self.assertNotEqual(cont["action"], "close")
        self.assertNotIn("A2", cont["executable"])
        self.assertIn(cont["action"], {"derive", "wait"})


class C8TrueBlockedTests(SchedulingBase):
    def test_task_blocked_only_with_reasons_and_needs(self):
        G.add_block(self.state_path, "缺少 push 授权", "用户明确授权 push",
                    work_items=["A1"], criteria=["C1"])
        G.add_block(self.state_path, "等待用户选择发布方案", "用户决策",
                    work_items=["A2"], criteria=["C2"])
        evaluation = self.ev()
        self.assertEqual(evaluation["phase"], "BLOCKED", evaluation)
        cont = evaluation["continuation"]
        self.assertFalse(cont["required"])
        self.assertEqual(cont["action"], "wait")
        self.assertEqual([i["id"] for i in cont["blocked_items"]], ["A1", "A2"])
        for item in cont["blocked_items"]:
            self.assertTrue(item["reason"])
            self.assertTrue(item["needs"])
            self.assertTrue(item["block_id"])
        drive = self.drive()
        self.assertEqual(drive.returncode, 11, drive.stdout + drive.stderr)
        self.assertIn("真实阻塞", drive.stdout)

    def test_task_block_records_ready_work_for_audit(self):
        block = G.add_block(self.state_path, "目标整体失效", "用户重新定义目标")
        self.assertEqual(G.evaluate(self.state())["phase"], "BLOCKED")
        # 函数级记录保留 ready_at_creation 事实，避免把单项缺口伪装成整体阻塞。
        self.assertIsInstance(block.get("ready_at_creation"), list)
        self.assertEqual(sorted(block["ready_at_creation"]), ["A1", "A2"])
        self.assertFalse(block["forced_scope"])
        self.assertEqual(block["scope"], "task")


class C9AccuratePassTests(SchedulingBase):
    def test_pass_and_close_only_when_all_conditions_met(self):
        eid = self.evidence()
        self.record("A1", evidence_ids=[eid])
        self.record("A2", evidence_ids=[eid])
        self.met("C1", eid)
        self.assertEqual(self.ev()["phase"], "PARTIAL")
        self.assertEqual(self.drive().returncode, 10)
        self.assertFalse(self.state()["closure"]["closed"])
        self.met("C2", eid)
        self.assertEqual(self.ev()["phase"], "PASS")
        proc = self.drive()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("任务已收口", proc.stdout)
        self.assertNotIn("PARTIAL", proc.stdout.split("任务已收口")[-1])
        self.assertTrue(self.state()["closure"]["closed"])

    def test_blocked_task_is_not_reported_as_pass(self):
        G.add_block(self.state_path, "缺少授权", "用户授权",
                    work_items=["A1"], criteria=["C1"])
        G.add_block(self.state_path, "缺少决策", "用户决策",
                    work_items=["A2"], criteria=["C2"])
        proc = self.drive()
        self.assertEqual(proc.returncode, 11, proc.stdout + proc.stderr)
        self.assertNotIn("任务已收口", proc.stdout)
        self.assertFalse(self.state()["closure"]["closed"])


if __name__ == "__main__":
    unittest.main()

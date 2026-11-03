#!/usr/bin/env python3
"""阶段 4：copilot_auto.py 自动协作驱动 —— 真实 CLI + Fake Upstream 集成测试。

与 test_e2e_fake_upstream.py 的分工：
- 那个文件验证「命令级」契约（call_gpt / copilot_dispatch 单独可用、门禁正确）；
- 本文件验证「驱动级」契约：一次 drive 只推进一个机械步骤、执行结果自动回填、
  下一轮自动携带上轮报告与证据、PASS 后自动四门收口、停摆/检查点/配额/恢复不越界。

Fake Upstream 只监听 127.0.0.1，不联网、不产生真实费用。测试中的「执行方」是一个
脚本化的模拟执行器（写本地文件后提交 manifest），它证明的是驱动与状态机的衔接；
真实 GPT 付费调用与真实 Agent 自主执行不在这里冒充。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
AUTO = os.path.join(SCRIPTS, "copilot_auto.py")
DISPATCH = os.path.join(SCRIPTS, "copilot_dispatch.py")

sys.path.insert(0, HERE)
from test_e2e_fake_upstream import Reply, Upstream, plan, success  # noqa: E402

MODEL = "gpt-6.1-sol"


class AutoLoopBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state = os.path.join(self.dir, "dispatch.json")
        self.audit = os.path.join(self.dir, "audit.jsonl")
        self.workdir = os.path.join(self.dir, "auto")
        self.upstream = Upstream()
        self.upstream.start()
        self.addCleanup(self.upstream.close)
        self.env = dict(os.environ)
        self.env.update({
            "CUSTOM_127_API_KEY": "sk-test-auto-not-real",
            "COPILOT_AUDIT_TEST_MODE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        })

    # ── 基础操作 ──────────────────────────────────────────────────────────
    def cli(self, script: str, *args: str, timeout: float = 60):
        return subprocess.run(
            [sys.executable, "-B", script, *args],
            capture_output=True, text=True, timeout=timeout,
            env=self.env, cwd=self.dir)

    def dispatch(self, cmd: str, *args: str):
        return self.cli(DISPATCH, cmd, "--state", self.state, *args)

    def drive(self, *args: str, timeout: float = 60):
        proc = self.cli(
            AUTO, "drive", "--state", self.state, "--audit-log", self.audit,
            "--work-dir", self.workdir, "--base-url", self.upstream.url,
            "--timeout", "5", "--max-output-tokens", "4096", *args,
            timeout=timeout)
        payload = {}
        if proc.stdout.strip():
            try:
                payload = json.loads(proc.stdout)
            except ValueError:
                self.fail(f"drive 输出不是 JSON：{proc.stdout[:500]}")
        return proc, payload

    def start(self, criteria: list[str] | None = None, *,
              task_id: str = "auto-task", risk: str = "normal") -> dict:
        args = ["--task-id", task_id, "--mode", "PLAN", "--risk", risk,
                "--goal", f"自动驱动测试任务 {task_id}",
                "--contract", "完成条件见清单\n边界：仅测试"]
        if criteria:
            path = os.path.join(self.dir, "criteria.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump([{"criterion_id": f"C{i + 1}", "description": text}
                           for i, text in enumerate(criteria)], fh,
                          ensure_ascii=False)
            args += ["--criteria-file", path]
        proc = self.dispatch("start", *args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.state_json()

    def state_json(self) -> dict:
        with open(self.state, encoding="utf-8") as fh:
            return json.load(fh)

    def queue(self, plan_obj: dict, model: str = MODEL) -> None:
        self.upstream.push(success(plan(plan_obj), model=model))

    def write(self, name: str, text: str) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def manifest(self, data: dict, name: str = "manifest.json") -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        return path

    def payload_text(self, index: int) -> str:
        return json.dumps(self.upstream.requests[index]["payload"],
                          ensure_ascii=False)

    def audit_results(self) -> list[dict]:
        if not os.path.exists(self.audit):
            return []
        out = []
        with open(self.audit, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if record.get("event") == "call_result":
                    out.append(record)
        return out

    def action(self, aid: str = "T1") -> dict:
        return {"id": aid, "title": f"执行 {aid}", "assignee": "zcode",
                "goal": "完成测试动作", "constraints": "仅本地测试",
                "expected_evidence": "原始输出", "depends_on": []}

    def first_round(self, with_action: bool = True) -> dict:
        self.start(["启动成功", "首屏可见"])
        self.queue({
            "decision": "continue", "verdict": "partial",
            "criteria_confirmed": True, "summary": "确认条件并开始",
            **({"actions": [self.action()]} if with_action else {}),
        })
        proc, res = self.drive()
        self.assertEqual(proc.returncode, 10 if with_action else 11,
                         proc.stderr)
        self.assertEqual(res["status"], "execute" if with_action else "wait_user")
        return res

    # ── 1. 多轮 PARTIAL → PARTIAL → PASS 全自动闭环 ─────────────────────
    def test_01_partial_partial_pass_closes_without_user_relay(self):
        self.start(["启动成功", "首屏可见"])
        self.queue({
            "decision": "continue", "verdict": "partial",
            "criteria_confirmed": True, "summary": "确认条件并安排 T1",
            "actions": [self.action("T1")],
        })
        proc, first = self.drive()
        self.assertEqual(proc.returncode, 10, proc.stderr)
        self.assertEqual(first["status"], "execute")
        self.assertEqual([a["id"] for a in first["actions"]], ["T1"])
        self.assertEqual(first["round_seq"], 1)
        self.assertFalse(first["pass_pending"])
        self.assertIn("原始目标", self.payload_text(0))
        self.assertIn("criteria_confirmed", self.payload_text(0))

        # 执行方（模拟）完成 T1，并提交原始输出 + manifest；驱动自动登记并回传。
        raw1 = self.write("t1.log", "T1-RESULT-MARKER: ok\nexit=0\n")
        m1 = self.manifest({
            "round_id": first["round_id"],
            "actions": [{"id": "T1", "status": "done",
                         "result": "T1 已执行：T1-RESULT-MARKER",
                         "evidence_ids": ["E1"]}],
            "evidence": [{"id": "E1", "file": raw1, "kind": "test-output",
                          "note": "T1 原始输出"}],
        }, "m1.json")
        self.queue({
            "decision": "continue", "verdict": "partial",
            "summary": "T1 通过，继续 T2",
            "actions": [self.action("T2")],
        })
        proc, second = self.drive("--result-file", m1)
        self.assertEqual(proc.returncode, 10, proc.stderr)
        self.assertEqual(second["status"], "execute")
        self.assertEqual([a["id"] for a in second["actions"]], ["T2"])
        self.assertEqual(second["round_seq"], 2)
        text2 = self.payload_text(1)
        self.assertIn("最近一次执行报告", text2)
        self.assertIn("自动驱动执行回传", text2)
        self.assertIn("T1-RESULT-MARKER", text2)
        self.assertIn("T1 原始输出", text2)       # 证据包条目
        self.assertIn("T1 已执行", text2)          # 工作项执行反馈

        raw2 = self.write("t2.log", "T2-RESULT-MARKER: ok\n")
        m2 = self.manifest({
            "round_id": second["round_id"],
            "actions": [{"id": "T2", "status": "done",
                         "result": "T2 已执行：T2-RESULT-MARKER",
                         "evidence_ids": ["E2"]}],
            "evidence": [{"id": "E2", "file": raw2, "kind": "test-output",
                          "note": "T2 原始输出"}],
        }, "m2.json")
        self.queue({
            "decision": "stop", "verdict": "pass", "summary": "全部完成",
            "criteria": [
                {"criterion_id": "C1", "status": "met", "evidence_refs": ["E1"]},
                {"criterion_id": "C2", "status": "met", "evidence_refs": ["E2"]},
            ],
        })
        proc, third = self.drive("--result-file", m2)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(third["status"], "closed")
        self.assertEqual(third["closure"]["verdict"], "pass")
        self.assertEqual(third["closure"]["closed_by"], "gate")
        text3 = self.payload_text(2)
        self.assertIn("T2-RESULT-MARKER", text3)
        self.assertIn("T2 原始输出", text3)

        state = self.state_json()
        self.assertEqual(state["round_seq"], 3)
        self.assertEqual([r["verdict"] for r in state["rounds"]],
                         ["partial", "partial", "pass"])
        self.assertTrue(state["closure"]["closed"])
        self.assertEqual(state["closure"]["closed_by"], "gate")
        self.assertEqual(state["closure"]["verdict"], "pass")
        self.assertIsNone(state["pending_call"])
        self.assertEqual([a["status"] for a in state["actions"]], ["done", "done"])
        self.assertEqual([c["status"] for c in state["contract"]["criteria"]],
                         ["met", "met"])
        self.assertEqual({e["id"] for e in state["evidence"]}, {"E1", "E2"})
        self.assertEqual(len(self.upstream.requests), 3)
        results = self.audit_results()
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.get("model_identity_ok") is True for r in results))

        # 收口后 drive 不再发起调用，也不改状态。
        before = self.state_json()
        proc, closed = self.drive()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(len(self.upstream.requests), 3)
        self.assertEqual(self.state_json()["round_seq"], before["round_seq"])

    # ── 2. REJUDGE：GPT PASS 但四门失败，自动报告并重新判断 ─────────────
    def test_02_gate_failure_rejudges_automatically_and_overrides_old_pass(self):
        self.start(["启动成功"])
        self.queue({
            "decision": "continue", "verdict": "partial",
            "criteria_confirmed": True, "summary": "确认并执行 T1/T2",
            "actions": [self.action("T1"), self.action("T2")],
        })
        proc, first = self.drive()
        self.assertEqual(proc.returncode, 10, proc.stderr)

        # T1 完成、T2 失败，但 GPT 下一轮仍误判 PASS：G1（T2 未终态）与
        # G3（挂置项）必须拦住收口，驱动自动留痕失败原因并转入 REJUDGE。
        raw = self.write("t1.log", "EVIDENCE-FOR-T1\n")
        manifest = self.manifest({
            "round_id": first["round_id"],
            "actions": [
                {"id": "T1", "status": "done", "result": "T1 完成",
                 "evidence_ids": ["E1"]},
                {"id": "T2", "status": "failed", "result": "T2 失败：缺少依赖"},
            ],
            "evidence": [{"id": "E1", "file": raw, "kind": "test-output"}],
        })
        self.queue({
            "decision": "stop", "verdict": "pass", "summary": "误判通过",
            "criteria": [{"criterion_id": "C1", "status": "met",
                          "evidence_refs": ["E1"]}],
        })
        proc, second = self.drive("--result-file", manifest)
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(second["status"], "wait_user")
        self.assertEqual(second["reason"], "rejudge_required")
        self.assertTrue(second["pass_pending"])
        gate_text = json.dumps(second["gate"], ensure_ascii=False)
        self.assertIn("G1", gate_text)
        self.assertIn("action_open", gate_text)
        self.assertIn("G3", gate_text)
        state = self.state_json()
        self.assertEqual([r["verdict"] for r in state["rounds"]],
                         ["partial", "pass"])
        self.assertFalse(state["closure"]["closed"])

        # 下一轮 drive 先自动留痕四门失败原因，再携带失败原因重新判断；
        # GPT 改判 partial 并给修复项，旧的 PASS 被后续判断覆盖。
        # （一次 drive 至多一次付费调用，所以留痕+重判发生在下一次调用中。）
        self.queue({
            "decision": "continue", "verdict": "partial",
            "summary": "改判 partial：先修复 T2 依赖",
            "actions": [{"id": "T3", "title": "修复 T2 缺失依赖",
                         "assignee": "zcode", "goal": "补齐依赖并重跑 T2",
                         "constraints": "仅本地", "expected_evidence": "安装与重跑输出",
                         "depends_on": []}],
        })
        proc, third = self.drive()
        self.assertEqual(proc.returncode, 10, proc.stderr)
        self.assertEqual(third["status"], "execute")
        self.assertEqual([a["id"] for a in third["actions"]], ["T3"])
        text = self.payload_text(2)
        self.assertIn("上一轮 PASS 未通过本地四门", text)
        self.assertIn("G1:action_open", text)
        self.assertIn("G3:stalled", text)
        self.assertIn("自动驱动：上一轮 PASS 未通过本地四门", text)
        state = self.state_json()
        self.assertEqual([r["verdict"] for r in state["rounds"]],
                         ["partial", "pass", "partial"])
        self.assertFalse(state["closure"]["closed"])
        self.assertIsNone(state["closure"].get("round_id"))
        self.assertFalse(self.state_json().get("pending_call"))
        blocked = [r for r in state["reports"] if r.get("blocked")]
        self.assertTrue(blocked, "REJUDGE 前必须自动留痕四门失败原因")
        self.assertIn("G1:action_open", blocked[-1]["summary"])
        self.assertIn("G3:stalled", blocked[-1]["summary"])
        self.assertEqual(len(self.upstream.requests), 3)

    # ── 3. 无进展停摆：不重复刷付费调用 ─────────────────────────────────
    def test_03_no_progress_stops_before_paid_call(self):
        self.start()
        for _ in range(2):
            self.queue({"decision": "continue", "verdict": "partial",
                        "summary": "继续但无新动作"})
            proc, res = self.drive()
            self.assertEqual(proc.returncode, 11, proc.stderr)
            self.assertEqual(res["status"], "wait_user")
            self.assertEqual(res["reason"], "guidance_without_actions")
        self.assertEqual(len(self.upstream.requests), 2)

        # 第三次：stamp 使 streak 达到 2，必须在出站前停下。
        proc, res = self.drive()
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(res["status"], "wait_user")
        self.assertEqual(res["reason"], "no_progress")
        self.assertEqual(len(self.upstream.requests), 2)
        self.assertEqual(self.state_json()["round_seq"], 2)

    # ── 4. 8 轮检查点 / 轮次软上限 ──────────────────────────────────────
    def test_04_checkpoint_and_round_limit_stop_before_call(self):
        self.start(task_id="auto-checkpoint")
        state = self.state_json()
        state["round_seq"] = 8
        state["budget"]["progress_streak"] = 0
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        proc, res = self.drive()
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(res["status"], "wait_user")
        self.assertEqual(res["reason"], "checkpoint")
        self.assertTrue(res["checkpoint"])
        self.assertEqual(len(self.upstream.requests), 0)

        state = self.state_json()
        state["round_seq"] = 2
        state["budget"]["rounds_limit"] = 2
        state["budget"]["progress_streak"] = 0
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        proc, res = self.drive()
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(res["reason"], "rounds_limit")
        self.assertEqual(len(self.upstream.requests), 0)

    # ── 5. 配额等待：不重发、不执行 ─────────────────────────────────────
    def test_05_quota_wait_does_not_resend(self):
        self.start(task_id="auto-quota")
        self.upstream.push(Reply(429, {"error": {
            "code": "usage_limit_reached", "resets_in_seconds": 600}}))
        proc, res = self.drive()
        self.assertEqual(proc.returncode, 8, proc.stderr)
        self.assertEqual(res["status"], "failed")
        self.assertEqual(self.state_json()["quota"]["state"], "waiting")
        self.assertEqual(len(self.upstream.requests), 1)

        proc, res = self.drive()
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(res["status"], "quota_wait")
        self.assertEqual(len(self.upstream.requests), 1)
        self.assertIsNone(self.state_json()["pending_call"])

    # ── 6. 超时恢复：pending 不重发 ─────────────────────────────────────
    def test_06_timeout_leaves_pending_and_blocks_resend(self):
        self.start(task_id="auto-recover")
        self.upstream.push(Reply(200, {"id": "late", "model": MODEL,
                                       "output": []}, delay=2.0))
        proc, res = self.drive("--timeout", "0.5")
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertEqual(res["status"], "failed")
        state = self.state_json()
        self.assertIsNotNone(state["pending_call"])
        self.assertEqual(state["rounds"], [])
        self.assertEqual(len(self.upstream.requests), 1)

        proc, res = self.drive()
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(res["status"], "recover")
        self.assertEqual(len(self.upstream.requests), 1)

    # ── 7. manifest 校验失败不产生半更新 ────────────────────────────────
    def test_07_invalid_manifest_writes_nothing(self):
        self.first_round()
        before = self.state_json()
        cases = [
            ({"actions": [{"id": "NOPE", "status": "done", "result": "x"}]},
             "不存在工作项"),
            ({"actions": [{"id": "T1", "status": "done"}]},
             "缺少执行结果"),
            ({"actions": [{"id": "T1", "status": "running", "result": "x"}]},
             "不被自动驱动接受"),
            ({"round_id": "R999", "actions": [
                {"id": "T1", "status": "done", "result": "x"}]},
             "不是最近已验证轮次"),
            ({"actions": [{"id": "T1", "status": "done", "result": "x",
                           "evidence_ids": ["E9"]}]},
             "未登记证据"),
        ]
        for index, (manifest, message) in enumerate(cases):
            path = self.manifest(manifest, f"bad-{index}.json")
            proc, _ = self.drive("--result-file", path)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn(message, proc.stderr)
            after = self.state_json()
            self.assertEqual(after["actions"], before["actions"])
            self.assertEqual(after["reports"], before["reports"])
            self.assertEqual(after["evidence"], before["evidence"])
            self.assertEqual(after["round_seq"], before["round_seq"])
        self.assertEqual(len(self.upstream.requests), 1)

    # ── 8. dry-run 只预测，不写不调用 ──────────────────────────────────
    def test_08_dry_run_predicts_without_side_effects(self):
        self.start(task_id="auto-dry")
        self.queue({"decision": "continue", "verdict": "partial",
                    "summary": "不应被消费"})
        before = self.state_json()
        proc, res = self.drive("--dry-run", "--question", "预检问题")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(res["status"], "call")
        self.assertTrue(res["dry_run"])
        self.assertTrue(res["would_call"])
        self.assertIn("预检问题", res["prompt"])
        self.assertEqual(len(self.upstream.requests), 0)
        self.assertEqual(self.state_json(), before)
        self.assertFalse(os.path.exists(self.workdir))

    # ── 9. 高风险任务：PASS 也不能绕过 G4 自动收口 ───────────────────────
    def test_09_high_risk_pass_stays_blocked_on_g4(self):
        self.start(["启动成功"], task_id="auto-high-risk", risk="high")
        self.queue({
            "decision": "continue", "verdict": "partial",
            "criteria_confirmed": True, "summary": "高风险任务先执行 T1",
            "actions": [self.action("T1")],
        })
        proc, first = self.drive()
        self.assertEqual(proc.returncode, 10, proc.stderr)

        raw = self.write("t1.log", "HIGH-RISK-EVIDENCE\n")
        manifest = self.manifest({
            "round_id": first["round_id"],
            "actions": [{"id": "T1", "status": "done", "result": "完成",
                         "evidence_ids": ["E1"]}],
            "evidence": [{"id": "E1", "file": raw, "kind": "test-output"}],
        })
        self.queue({
            "decision": "stop", "verdict": "pass", "summary": "证据满足",
            "criteria": [{"criterion_id": "C1", "status": "met",
                          "evidence_refs": ["E1"]}],
        })
        proc, res = self.drive("--result-file", manifest)
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(res["status"], "blocked")
        self.assertIn("native_review_missing",
                      json.dumps(res["gate"], ensure_ascii=False))
        state = self.state_json()
        self.assertFalse(state["closure"]["closed"])
        self.assertIsNone(state["closure"]["verdict"])

        # 停在 BLOCKED 后不再调用，也不能靠自动循环把高风险任务收口。
        proc, res = self.drive()
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(res["status"], "blocked")
        self.assertEqual(len(self.upstream.requests), 2)
        self.assertFalse(self.state_json()["closure"]["closed"])

    # ── 10. RECOVER 后按审计恢复，不重复调用 ────────────────────────────
    def test_10_recover_reply_path_continues_without_new_call(self):
        self.start(task_id="auto-recover-reply")
        self.upstream.push(Reply(200, {"id": "late", "model": MODEL,
                                       "output": []}, delay=2.0))
        proc, _ = self.drive("--timeout", "0.5")
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertIsNotNone(self.state_json()["pending_call"])

        # 模拟「回复已保存但提交失败」的人工恢复材料：审计没有成功结果时，
        # 驱动不得把回复文件当成可用凭证，必须 fail-closed。
        reply = self.write("late-reply.md", plan({
            "decision": "continue", "verdict": "partial", "summary": "迟到回复"}))
        proc, res = self.drive("--recover-reply", reply)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("恢复挂起轮次失败", proc.stderr)
        self.assertIsNotNone(self.state_json()["pending_call"])
        self.assertEqual(len(self.upstream.requests), 1)


class AutoDocsTest(unittest.TestCase):
    """运行文档必须把自动驱动的边界说清楚，不能只靠实现存在。"""

    def setUp(self):
        self.base = os.path.dirname(HERE)
        with open(os.path.join(self.base, "SKILL.md"), encoding="utf-8") as fh:
            self.skill = fh.read()
        with open(os.path.join(self.base, "references", "role.md"),
                  encoding="utf-8") as fh:
            self.role = fh.read()

    def test_docs_declare_driver_and_no_user_relay(self):
        self.assertIn("copilot_auto.py drive", self.skill)
        self.assertIn("一次 `drive` 至多发起一次真实 GPT 调用", self.skill)
        self.assertIn("不需要用户转发", self.skill)
        self.assertIn("主代理仍是唯一执行方", self.skill)
        self.assertIn("不需要用户转发", self.role)
        self.assertIn("自动驱动通道（阶段 4）", self.role)

    def test_docs_keep_safety_and_test_boundaries(self):
        # 自动循环不得被写成能绕过 G4、授权或冒充独立复核。
        for text in (self.skill, self.role):
            self.assertIn("G4", text)
            self.assertIn("独立复核", text)
        self.assertIn("Fake Upstream", self.skill)
        self.assertIn("不等于真实 GPT 调用验证", self.skill)
        self.assertIn("REVIEW 仍走复核台账", self.skill)


if __name__ == "__main__":
    unittest.main()

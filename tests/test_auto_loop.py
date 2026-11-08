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
sys.path.insert(0, SCRIPTS)
from test_e2e_fake_upstream import Reply, Upstream, plan, success  # noqa: E402
import copilot_auto  # noqa: E402

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
            "--timeout", "5", *args,
            timeout=timeout)
        payload = {}
        if proc.stdout.strip():
            try:
                payload = json.loads(proc.stdout)
            except ValueError:
                self.fail(f"drive 输出不是 JSON：{proc.stdout[:500]}")
        return proc, payload

    def start(self, criteria: list[str] | None = None, *,
              task_id: str = "auto-task", risk: str = "normal",
              mode: str = "PLAN") -> dict:
        args = ["--task-id", task_id, "--mode", mode, "--risk", risk,
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

    def pack(self, name: str, source: str, body: str) -> str:
        """写一个当前格式（v2）的最小证据包，供 REVIEW 首轮使用。"""
        import hashlib
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        text = "\n".join([
            "# 测试证据包", "", "- 证据包格式：2", "",
            "## 已纳入的证据", "",
            f"### E1 — file: {source}", "",
            "- 采集方式：全文", f"- sha256：`{digest}`", "",
            "```text", body, "```", "",
        ])
        return self.write(name, text)

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
        # 没有 copilot-plan 时由驱动本地派生工作项（origin=partial-without-plan），
        # 仍然进入 EXECUTE，不以 wait_user 等用户中转。
        self.assertEqual(proc.returncode, 10, proc.stderr)
        self.assertEqual(res["status"], "execute")
        if not with_action:
            self.assertTrue(res["actions"][0]["id"].startswith("AUTO-"))
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

    # ── 1b. PARTIAL 无 plan → 派生 → 执行 → 证据 → 再判断 → PASS ────────
    def test_01b_partial_without_plan_derives_executes_and_passes(self):
        self.start(["启动成功"])
        self.queue({"decision": "continue", "verdict": "partial",
                    "criteria_confirmed": True,
                    "summary": "首屏已通过，但缺少启动耗时的原始日志"})
        proc, first = self.drive()
        self.assertEqual(proc.returncode, 10, proc.stderr)
        self.assertEqual(first["status"], "execute")
        self.assertEqual(len(self.upstream.requests), 1)

        # 派生工作项：本地生成，但明确标注来源、完成条件、授权边界与回传要求。
        work = first["actions"][0]
        self.assertTrue(work["id"].startswith("AUTO-"), work)
        order = work["work_order"]
        self.assertEqual(order["origin"], "partial-without-plan")
        self.assertEqual(order["executed_by"], "main-agent")
        self.assertTrue(order["directive_complete"])
        self.assertEqual(order["derived_from_round_id"], first["round_id"])
        self.assertEqual([c["criterion_id"] for c in order["completion_criteria"]],
                         ["C1"])
        self.assertIn("不得自行扩大授权", order["authorization"])
        self.assertIn("manifest", order["return_contract"])
        self.assertEqual(self.state_json()["actions"][-1]["status"], "pending")

        # 执行方回填真实产物；摘要本身不算证据。
        raw = self.write("auto1.log", "AUTO-1-RESULT: ok\nexit=0\n")
        manifest = self.manifest({
            "round_id": first["round_id"],
            "actions": [{"id": work["id"], "status": "done",
                         "result": "AUTO-1 已执行：补齐启动耗时日志",
                         "evidence_ids": ["E1"]}],
            "evidence": [{"id": "E1", "file": raw, "kind": "test-output",
                          "note": "AUTO-1 原始日志"}],
        }, "auto1.json")

        # 新材料自动交回 SOL 判断，不需要用户复制粘贴或逐轮授权。
        self.queue({"decision": "stop", "verdict": "pass",
                    "summary": "条件已满足",
                    "criteria": [{"criterion_id": "C1", "status": "met",
                                  "evidence_refs": ["E1"]}]})
        proc, second = self.drive("--result-file", manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(second["status"], "closed")
        self.assertEqual(len(self.upstream.requests), 2)
        text = self.payload_text(1)
        self.assertIn("最近一次执行报告", text)
        self.assertIn("AUTO-1-RESULT", text)
        self.assertIn("AUTO-1 原始日志", text)

        state = self.state_json()
        self.assertEqual([r["verdict"] for r in state["rounds"]],
                         ["partial", "pass"])
        self.assertEqual([a["id"] for a in state["actions"]], [work["id"]])
        self.assertEqual(state["actions"][0]["status"], "done")
        self.assertEqual(state["closure"]["verdict"], "pass")
        self.assertEqual([c["status"] for c in state["contract"]["criteria"]],
                         ["met"])

    # ── 1c. 真实 BLOCKED：不自动执行、不无限重试 ───────────────────────
    def test_01c_sol_blocked_stops_with_explicit_reason(self):
        self.start(["启动成功"])
        self.queue({"decision": "blocked", "verdict": "blocked",
                    "summary": "需要用户提供正式签名文件；超出当前授权范围"})
        proc, res = self.drive()
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(res["status"], "blocked")
        self.assertEqual(len(self.upstream.requests), 1)
        state = self.state_json()
        self.assertFalse([a for a in state["actions"]
                          if a.get("status") in ("pending", "running")],
                         "BLOCKED 不得派生或保留可执行工作项")
        self.assertFalse(state["closure"]["closed"])

        # 再次 drive 不会自动重发：阻塞等待用户决定。
        proc, again = self.drive()
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(again["status"], "blocked")
        self.assertEqual(len(self.upstream.requests), 1)

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

    # ── 3. 连续无进展：提交 SOL 重新分析，而不是计数即停 ───────────────
    def test_03_no_progress_triggers_sol_reanalysis(self):
        self.start()
        for _ in range(2):
            self.queue({"decision": "continue", "verdict": "partial",
                        "summary": "继续但无新动作"})
            proc, res = self.drive()
            self.assertEqual(proc.returncode, 11, proc.stderr)
            self.assertEqual(res["status"], "wait_user")
            self.assertEqual(res["reason"], "guidance_without_actions")
        self.assertEqual(len(self.upstream.requests), 2)

        # 第三次：stamp 使 streak 达到 2。旧实现直接停；新实现必须把失败/
        # 未闭合项提交 SOL，要求重新分析并给出可执行替代方案。
        self.queue({"decision": "continue", "verdict": "partial",
                    "summary": "原方案失效：改为按证据来源分段核验",
                    "actions": [self.action("T1")]})
        proc, res = self.drive()
        self.assertEqual(proc.returncode, 10, proc.stderr)
        self.assertEqual(res["status"], "execute", res)
        self.assertEqual(len(self.upstream.requests), 3)
        prompt = self.payload_text(-1)
        self.assertIn("连续无进展", prompt)
        self.assertIn("替代方案", prompt)
        self.assertIn("方案失效", prompt)
        self.assertIn("授权边界", prompt)
        state = self.state_json()
        self.assertTrue(state["budget"].get("last_no_progress_digest"))
        self.assertEqual(state["round_seq"], 3)

    def test_03b_no_progress_without_new_information_does_not_repeat_call(self):
        """相同失败/证据不得无限重复同一个 SOL 请求。"""
        self.start()
        for _ in range(2):
            self.queue({"decision": "continue", "verdict": "partial",
                        "summary": "继续但无新动作"})
            self.drive()
        state = self.state_json()
        material = copilot_auto._no_progress_material(state)
        state["budget"]["progress_streak"] = 2
        state["budget"]["last_no_progress_digest"] = material["digest"]
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        before = len(self.upstream.requests)
        proc, res = self.drive()
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(res["status"], "wait_user")
        self.assertEqual(res["reason"], "no_progress_no_new_information")
        self.assertEqual(len(self.upstream.requests), before,
                         "没有新信息时不得再发相同请求")

    def test_03c_no_progress_reanalysis_requires_review_authorization(self):
        """无进展恢复不能绕过正式 REVIEW 的显式授权门槛。"""
        self.start(mode="REVIEW")
        state = self.state_json()
        state["budget"]["progress_streak"] = 2
        state["budget"]["last_progress_digest"] = "same"
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        proc, res = self.drive()
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(res["status"], "wait_user")
        self.assertEqual(res["reason"], "no_progress_reanalysis_requires_auto_review")
        self.assertEqual(self.upstream.requests, [])

    def test_03d_no_progress_sol_blocked_with_reason_stops(self):
        """SOL 重新分析后给出有理由的 blocked 时停止，不伪造替代方案。"""
        self.start()
        for _ in range(2):
            self.queue({"decision": "continue", "verdict": "partial",
                        "summary": "继续但无新动作"})
            self.drive()
        self.queue({"decision": "blocked", "verdict": "blocked",
                    "summary": "缺少用户必须提供的业务决策；没有安全可执行的替代方案",
                    "reason": "需要用户确认验收口径"})
        proc, res = self.drive()
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(res["status"], "blocked")
        state = self.state_json()
        self.assertFalse(state["closure"]["closed"])
        self.assertEqual(state["rounds"][-1]["decision"], "blocked")
        self.assertIn("缺少用户", state["rounds"][-1]["summary"])

    # ── 4. 8 轮检查点 / 轮次上限只是观察信号，不停止调用 ────────────────
    def test_04_checkpoint_and_round_limit_do_not_stop(self):
        self.start(task_id="auto-checkpoint")
        self.queue({"decision": "continue", "verdict": "partial",
                    "summary": "检查点轮继续判断", "actions": [self.action("T1")]})
        state = self.state_json()
        state["round_seq"] = 8
        state["budget"]["rounds_limit"] = 2
        state["budget"]["progress_streak"] = 0
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        proc, res = self.drive()
        self.assertEqual(proc.returncode, 10, proc.stderr)
        self.assertEqual(res["status"], "execute")
        self.assertEqual(len(self.upstream.requests), 1)
        self.assertEqual(res["loop_stats"]["round_seq"], 9)
        self.assertFalse(res["loop_stats"]["limits_enforced"])
        # 旧格式状态里的 rounds_limit 只保留为观察字段，不构成停止条件。
        self.assertNotIn(res.get("reason"), ("checkpoint", "rounds_limit"))

    # ── 4b. 连续多轮不因人为数字停止 ────────────────────────────────────
    def test_04b_many_rounds_are_not_capped(self):
        self.start(task_id="auto-many-rounds")
        result_file = None
        for index in range(1, 5):
            self.queue({"decision": "continue", "verdict": "partial",
                        "summary": f"第 {index} 轮仍缺证据",
                        "actions": [self.action(f"T{index}")]})
            args = ["--result-file", result_file] if result_file else []
            proc, res = self.drive(*args)
            self.assertEqual(proc.returncode, 10, proc.stderr)
            self.assertEqual(res["status"], "execute")
            self.assertEqual(res["round_seq"], index)
            self.assertNotIn(res.get("reason"),
                             ("checkpoint", "rounds_limit", "request_limit_reached",
                              "token_budget_reached", "budget_exhausted"))
            raw = self.write(f"t{index}.log", f"T{index}-MARKER: ok\nexit=0\n")
            result_file = self.manifest({
                "round_id": res["round_id"],
                "actions": [{"id": f"T{index}", "status": "done",
                             "result": f"T{index} 已执行",
                             "evidence_ids": [f"E{index}"]}],
                "evidence": [{"id": f"E{index}", "file": raw,
                              "kind": "test-output",
                              "note": f"T{index} 原始输出"}],
            }, f"m{index}.json")
        self.assertEqual(len(self.upstream.requests), 4)

    # ── 4c. 截断自动升档：同一轮次内恢复，不占正式 REVIEW 轮次 ──────────
    def test_04c_truncation_escalates_within_the_same_round(self):
        self.start(["启动成功"])
        truncated = {
            "id": "resp_cut", "status": "incomplete", "model": MODEL,
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "半份结论：还不能作为裁决"}]}],
            "usage": {"input_tokens": 10, "output_tokens": 4096},
        }
        self.upstream.push(Reply(200, truncated))
        self.queue({"decision": "continue", "verdict": "partial",
                    "criteria_confirmed": True, "summary": "确认条件并安排 T1",
                    "actions": [self.action("T1")]})
        proc, res = self.drive()
        self.assertEqual(proc.returncode, 10, proc.stderr)
        self.assertEqual(res["status"], "execute")
        # 两次请求、只消耗一轮：截断请求不进入正式轮次，也不推进任务状态。
        self.assertEqual(len(self.upstream.requests), 2)
        self.assertEqual(self.state_json()["round_seq"], 1)
        self.assertEqual(res["round_seq"], 1)
        self.assertNotIn("max_output_tokens",
                         self.upstream.requests[0]["payload"])
        self.assertEqual(self.upstream.requests[1]["payload"]["max_output_tokens"],
                         8192)
        # 半份内容与截断请求的用量都只留在审计里，不进入任务状态。
        state_text = json.dumps(self.state_json(), ensure_ascii=False)
        self.assertNotIn("半份结论", state_text)
        records = self.audit_results()
        self.assertTrue([r for r in records if r.get("truncated") is True])
        self.assertIsNone(self.state_json()["pending_call"])

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

    # ── 11. REVIEW 自动闭环：PARTIAL → 派生执行 → 新证据 → 下一轮 REVIEW ──
    def review_reply(self, verdict: str, plan_obj: dict | None = None) -> str:
        text = f"裁决：{verdict}\n\n评审依据见证据包。\n"
        if plan_obj is not None:
            text += "\n```copilot-plan\n" + json.dumps(plan_obj, ensure_ascii=False) \
                    + "\n```\n"
        return text

    def test_11_review_requires_explicit_auto_review(self):
        self.start(["条件A"], mode="REVIEW", task_id="review-guard")
        ev = self.write("ev0.md", "REVIEW-GUARD-EVIDENCE\n")
        proc, _ = self.drive("--evidence", ev)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--auto-review", proc.stderr)
        self.assertEqual(len(self.upstream.requests), 0)
        state = self.state_json()
        self.assertIsNone(state["pending_call"])
        self.assertEqual(state["round_seq"], 0)

    def test_11b_review_partial_auto_advances_and_uses_new_evidence(self):
        self.start(["条件A"], mode="REVIEW", task_id="review-auto")
        # 预置一份绑定本测试审计日志的空台账：新建台账要求标准审计日志，
        # 该规则由 review_ledger 自身测试覆盖；这里验证驱动侧的 REVIEW 闭环衔接。
        ledger = os.path.join(self.dir, "review-ledger.json")
        with open(ledger, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "created_at": "2026-11-08T00:00:00+08:00",
                       "updated_at": "2026-11-08T00:00:00+08:00",
                       "subject": "review-auto", "audit_log": self.audit,
                       "risk": "normal", "max_rounds": 2, "rounds": []},
                      fh)
        ev1 = self.pack("ev1.md", "baseline-claim.txt",
                        "REVIEW-ROUND1-EVIDENCE: baseline claim\n")
        self.upstream.push(success(self.review_reply("PARTIAL")))
        proc, first = self.drive("--auto-review", "--review-ledger", ledger,
                                 "--evidence", ev1)
        self.assertEqual(proc.returncode, 10, proc.stderr)
        self.assertEqual(first["status"], "execute")
        work = first["actions"][0]
        self.assertTrue(work["id"].startswith("AUTO-"), work)
        self.assertEqual(work["work_order"]["origin"], "partial-without-plan")
        self.assertEqual(len(self.upstream.requests), 1)

        # 执行方回填新的原始证据；下一轮 REVIEW 必须带上这份新证据。
        raw = self.write("auto1.log", "REVIEW-ROUND2-EVIDENCE: new raw artifact\n")
        manifest = self.manifest({
            "round_id": first["round_id"],
            "actions": [{"id": work["id"], "status": "done",
                         "result": "补齐条件A所需的原始证据",
                         "evidence_ids": ["E1"]}],
            "evidence": [{"id": "E1", "file": raw, "kind": "test-output",
                          "note": "REVIEW 第二轮新增证据"}],
        }, "review-m1.json")
        self.upstream.push(success(self.review_reply("PASS", {
            "decision": "stop", "verdict": "pass",
            "criteria_confirmed": True,
            "summary": "新增证据已覆盖条件A",
            "criteria": [{"criterion_id": "C1", "status": "met",
                          "evidence_refs": ["E1"]}],
        })))
        proc, second = self.drive("--auto-review", "--review-ledger", ledger,
                                  "--result-file", manifest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(second["status"], "closed")
        self.assertEqual(len(self.upstream.requests), 2)
        text2 = self.payload_text(1)
        self.assertIn("REVIEW-ROUND2-EVIDENCE", text2)
        self.assertIn("自动驱动第 2 轮 REVIEW", text2)
        self.assertIn("本轮新增/变化证据 E1，", text2)
        self.assertNotIn("EE1", text2)

        with open(ledger, encoding="utf-8") as fh:
            book = json.load(fh)
        self.assertEqual(len(book["rounds"]), 2)
        self.assertEqual(book["rounds"][-1]["verdict"], "PASS")
        self.assertIn("自动驱动第 2 轮 REVIEW",
                      book["rounds"][-1]["extra_reason"])
        state = self.state_json()
        self.assertEqual(state["round_seq"], 2)
        self.assertEqual([r["verdict"] for r in state["rounds"]],
                         ["partial", "pass"])
        self.assertTrue(state["closure"]["closed"])
        self.assertEqual(state["closure"]["closed_by"], "gate")

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
        self.assertIn("必须显式 `--auto-review`", self.skill)
        self.assertIn("未显式授权时 `drive` 拒绝 REVIEW", self.skill)


if __name__ == "__main__":
    unittest.main()

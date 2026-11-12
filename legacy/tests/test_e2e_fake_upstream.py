#!/usr/bin/env python3
"""T6 端到端模拟验收：真实 CLI + 本地 Fake Upstream + 临时状态/审计/凭据。

本文件不导入 call_gpt / copilot_dispatch 的内部函数来「模拟主路径」：每个场景都通过
`python3 scripts/call_gpt.py ...` 与 `python3 scripts/copilot_dispatch.py ...` 子进程执行，
Fake Upstream 只监听 127.0.0.1，不联网、不产生真实费用。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Queue

HERE = os.path.dirname(os.path.abspath(__file__))
LEGACY = os.path.dirname(HERE)
SCRIPTS = os.path.join(os.path.dirname(LEGACY), "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, LEGACY)
CALL = os.path.join(LEGACY, "call_gpt.py")
DISPATCH = os.path.join(LEGACY, "copilot_dispatch.py")
MODEL = "gpt-6.1-sol"


@dataclass
class Reply:
    status: int = 200
    body: dict | None = None
    delay: float = 0.0


@dataclass
class Upstream:
    requests: list[dict] = field(default_factory=list)
    replies: Queue = field(default_factory=Queue)
    server: ThreadingHTTPServer | None = None

    def start(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802 - http.server 协议名
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = {}
                owner.requests.append({
                    "path": self.path,
                    "headers": dict(self.headers),
                    "payload": payload,
                })
                try:
                    reply = owner.replies.get(timeout=10)
                except Empty:
                    reply = Reply(500, {"error": {"code": "no_fake_reply"}})
                if reply.delay:
                    time.sleep(reply.delay)
                body = json.dumps(reply.body or {}, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(reply.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def push(self, reply: Reply) -> None:
        self.replies.put(reply)

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()


def success(text: str, model: str = MODEL) -> Reply:
    return Reply(200, {
        "id": "resp_e2e", "status": "completed", "model": model,
        "output": [{"type": "message", "content": [
            {"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })


def plan(plan_obj: dict) -> str:
    return ("结论：见安排。\n\n```copilot-plan\n"
            + json.dumps(plan_obj, ensure_ascii=False) + "\n```\n")


class E2EBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.prompt_dir = os.path.join(self.dir, "prompts")
        os.makedirs(self.prompt_dir, exist_ok=True)
        self.state = os.path.join(self.dir, "dispatch.json")
        self.audit = os.path.join(self.dir, "audit.jsonl")
        self.upstream = Upstream()
        self.upstream.start()
        self.addCleanup(self.upstream.close)
        self.env = dict(os.environ)
        self.env.update({
            "CUSTOM_127_API_KEY": "sk-test-e2e-not-real",
            "COPILOT_AUDIT_TEST_MODE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        self.counter = 0

    def cli(self, script: str, *args: str, timeout: float = 40) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-B", script, *args],
            capture_output=True, text=True, timeout=timeout,
            env=self.env, cwd=self.dir,
        )

    def dispatch(self, cmd: str, *args: str) -> subprocess.CompletedProcess:
        return self.cli(DISPATCH, cmd, "--state", self.state, *args)

    def start(self, criteria: list[str] | None = None, *, task_id: str = "e2e",
              risk: str = "normal", contract: str = "完成条件见清单\n边界：仅测试") -> dict:
        extra = ["--task-id", task_id, "--mode", "PLAN", "--risk", risk,
                 "--goal", f"T6 任务 {task_id}", "--contract", contract]
        if criteria:
            path = os.path.join(self.dir, f"criteria-{task_id}.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump([{"criterion_id": f"C{i + 1}", "description": text}
                           for i, text in enumerate(criteria)], fh,
                          ensure_ascii=False)
            extra += ["--criteria-file", path]
        proc = self.cli(DISPATCH, "start", "--state", self.state, *extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.state_json()

    def state_json(self) -> dict:
        with open(self.state, encoding="utf-8") as fh:
            return json.load(fh)

    def loop(self) -> dict:
        proc = self.dispatch("loop-step", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def closure_check(self, *extra: str) -> dict:
        proc = self.dispatch("closure-check", "--json", *extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def call(self, text: str, *, prompt: str | None = None,
             mode: str = "PLAN", timeout: float = 20, extra: tuple[str, ...] = (),
             out: str | None = None) -> tuple[subprocess.CompletedProcess, str]:
        self.counter += 1
        prompt = text if prompt is None else prompt
        prompt_path = os.path.join(self.prompt_dir, f"prompt-{self.counter}.md")
        with open(prompt_path, "w", encoding="utf-8") as fh:
            fh.write(prompt + "\n")
        out_path = out or os.path.join(self.dir, f"reply-{self.counter}.md")
        proc = self.cli(
            CALL, "--mode", mode,
            "--dispatch-state", self.state,
            "--prompt-file", prompt_path,
            "--audit-log", self.audit,
            "--out", out_path,
            "--base-url", self.upstream.url,
            "--timeout", "5", "--max-retries", "0",
            *extra, timeout=timeout,
        )
        return proc, out_path

    def queue_plan(self, plan_obj: dict, model: str = MODEL) -> None:
        self.upstream.push(success(plan(plan_obj), model=model))

    def confirm(self) -> None:
        self.queue_plan({
            "decision": "continue", "verdict": "partial",
            "criteria_confirmed": True, "summary": "确认完成条件",
            "criteria": [{"criterion_id": "C1", "status": "pending"},
                         {"criterion_id": "C2", "status": "pending"}],
        })
        proc, _ = self.call("请确认完成条件。")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def evidence(self, eid: str, text: str) -> None:
        proc = self.dispatch("evidence", "--id", eid, "--text", text,
                             "--kind", "inline")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def passed_state(self, *, risk: str = "normal",
                     action_status: str = "",
                     with_action: bool = True) -> dict:
        self.start(["启动成功", "首屏可见"], task_id=f"e2e-pass-{risk}",
                   risk=risk)
        action = {"id": "T1", "title": "执行", "assignee": "zcode",
                  "goal": "g", "constraints": "c", "expected_evidence": "e",
                  "depends_on": []}
        self.queue_plan({
            "decision": "continue", "verdict": "partial",
            "criteria_confirmed": True, "summary": "确认并开始",
            "criteria": [{"criterion_id": "C1", "status": "pending"},
                         {"criterion_id": "C2", "status": "pending"}],
            **({"actions": [action]} if with_action else {}),
        })
        proc, _ = self.call("请确认并安排。")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        if action_status:
            proc = self.dispatch("record", "--id", "T1",
                                 "--status", action_status, "--result", "模拟结果")
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.evidence("E1", "启动成功")
        self.evidence("E2", "首屏可见")
        self.queue_plan({
            "decision": "stop", "verdict": "pass", "summary": "全部完成",
            "criteria": [
                {"criterion_id": "C1", "status": "met", "evidence_refs": ["E1"]},
                {"criterion_id": "C2", "status": "met", "evidence_refs": ["E2"]},
            ],
        })
        proc, _ = self.call("请做最终判断。")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.state_json()

    # ── 1. 单轮任务 ───────────────────────────────────────────────────────
    def test_01_single_round_real_cli(self):
        self.start(task_id="e2e-single")
        self.queue_plan({
            "decision": "continue", "summary": "单轮完成",
            "actions": [{"id": "T1", "title": "执行", "assignee": "zcode",
                         "goal": "g", "constraints": "c",
                         "expected_evidence": "e", "depends_on": []}],
        })
        proc, out = self.call("单轮任务。")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))
        state = self.state_json()
        self.assertEqual(state["round_seq"], 1)
        self.assertEqual(len(state["rounds"]), 1)
        self.assertIsNone(state["pending_call"])
        self.assertEqual([a["id"] for a in state["actions"]], ["T1"])
        self.assertEqual(len(self.upstream.requests), 1)
        payload = self.upstream.requests[0]["payload"]
        self.assertEqual(self.upstream.requests[0]["path"], "/responses")
        self.assertEqual(payload["model"], MODEL)
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(payload["tool_choice"], "none")
        self.assertNotIn("tools", payload)

    # ── 2 + 10. 多轮上下文完整性 ─────────────────────────────────────────
    def test_02_multi_round_context_integrity(self):
        self.start(task_id="e2e-multi",
                   contract="完成条件：保持上下文\n边界：BOUNDARY-MARKER")
        self.queue_plan({
            "decision": "continue", "summary": "ROUND-ALPHA",
            "actions": [{"id": "T1", "title": "执行", "assignee": "zcode",
                         "goal": "g", "constraints": "c",
                         "expected_evidence": "e", "depends_on": []}],
        })
        proc, _ = self.call("第一轮问题。")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.queue_plan({"decision": "continue", "summary": "ROUND-BETA"})
        proc, _ = self.call("第二轮：请继续。")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.state_json()
        self.assertEqual(state["round_seq"], 2)
        self.assertEqual(len(state["rounds"]), 2)
        text = json.dumps(self.upstream.requests[1]["payload"], ensure_ascii=False)
        for marker in ("ROUND-ALPHA", "BOUNDARY-MARKER", "T6 任务 e2e-multi",
                       "第二轮：请继续"):
            self.assertIn(marker, text)
        self.assertNotIn("e2e-other", text)

    # ── 3. PARTIAL 后继续执行 ─────────────────────────────────────────────
    def test_03_partial_then_execute(self):
        self.start(task_id="e2e-partial")
        self.queue_plan({
            "decision": "continue", "verdict": "partial", "summary": "先取证",
            "actions": [{"id": "T1", "title": "执行", "assignee": "zcode",
                         "goal": "g", "constraints": "c",
                         "expected_evidence": "e", "depends_on": []}],
        })
        proc, _ = self.call("请安排。")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.loop()["phase"], "EXECUTE")
        proc = self.dispatch("record", "--id", "T1", "--status", "done",
                             "--result", "已完成")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.queue_plan({"decision": "continue", "verdict": "partial",
                         "summary": "继续下一轮"})
        proc, _ = self.call("继续。")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.state_json()
        self.assertEqual(len(state["rounds"]), 2)
        self.assertFalse(self.loop()["pass_pending"])

    # ── 4. PASS 后完成收口 ────────────────────────────────────────────────
    def test_04_pass_closes_through_four_gates(self):
        self.passed_state(with_action=False)
        check = self.closure_check()
        self.assertTrue(check["passed"], check)
        self.assertTrue(all(g["passed"] for g in check["gates"].values()))
        self.assertEqual(self.loop()["phase"], "CLOSURE_CHECK")
        proc = self.dispatch("closure", "--close")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.state_json()
        self.assertTrue(state["closure"]["closed"])
        self.assertEqual(state["closure"]["closed_by"], "gate")
        self.assertEqual(state["closure"]["verdict"], "pass")
        self.assertEqual(self.loop()["phase"], "CLOSED")
        before = len(self.upstream.requests)
        proc, _ = self.call("收口后不应再调用。")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(len(self.upstream.requests), before)

    # ── 5. BLOCKED 后正确暂停 ─────────────────────────────────────────────
    def test_05_blocked_pauses(self):
        self.start(task_id="e2e-blocked")
        self.queue_plan({"decision": "blocked", "verdict": "blocked",
                         "summary": "缺少授权"})
        proc, _ = self.call("请判断。")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        stepped = self.loop()
        self.assertEqual(stepped["phase"], "BLOCKED")
        self.assertEqual(stepped["ready_actions"], [])
        self.assertFalse(self.state_json()["closure"]["closed"])

    # ── 6. 模型身份不一致 ────────────────────────────────────────────────
    def test_06_model_identity_mismatch(self):
        self.start(task_id="e2e-identity")
        self.queue_plan({"decision": "continue", "summary": "不应被消费"},
                        model="gpt-wrong-model")
        proc, out = self.call("请判断。")
        self.assertEqual(proc.returncode, 9, proc.stderr)
        self.assertFalse(os.path.exists(out))
        state = self.state_json()
        self.assertEqual(state["rounds"], [])
        self.assertIsNone(state["pending_call"])
        self.assertEqual(len(self.upstream.requests), 1)

    # ── 7. 执行失败及报告回传 ────────────────────────────────────────────
    def test_07_execution_failure_and_report(self):
        self.start(task_id="e2e-failed")
        self.queue_plan({
            "decision": "continue", "verdict": "partial", "summary": "安排",
            "actions": [{"id": "T1", "title": "执行", "assignee": "zcode",
                         "goal": "g", "constraints": "c",
                         "expected_evidence": "e", "depends_on": []}],
        })
        proc, _ = self.call("请安排。")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.dispatch("record", "--id", "T1", "--status", "failed",
                             "--result", "构建失败：缺少工具链")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.dispatch("report", "--summary", "T1 失败，需用户决定",
                             "--blocked", "--reason", "缺少工具链授权")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.state_json()
        self.assertEqual(state["actions"][0]["status"], "failed")
        self.assertTrue(state["reports"][-1]["blocked"])
        self.assertIn("T1 失败", state["reports"][-1]["summary"])
        # 执行失败是执行反馈，不是 GPT 的 blocked 裁决：阶段回到 GUIDANCE_NEEDED，
        # 下一轮把失败报告交给 GPT 判断，而不是由主代理自行改判。
        self.assertEqual(self.loop()["phase"], "GUIDANCE_NEEDED")
        self.queue_plan({"decision": "blocked", "verdict": "blocked",
                         "summary": "根据失败报告停止"})
        proc, _ = self.call("根据执行失败报告判断。")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("缺少工具链授权",
                      json.dumps(self.upstream.requests[1]["payload"], ensure_ascii=False))
        self.assertEqual(self.loop()["phase"], "BLOCKED")

    # ── 8. 配额熔断与恢复 ────────────────────────────────────────────────
    def test_08_quota_circuit_and_user_clear(self):
        self.start(task_id="e2e-quota")
        self.upstream.push(Reply(429, {"error": {
            "code": "usage_limit_reached", "resets_in_seconds": 600}}))
        proc, _ = self.call("第一次。")
        self.assertEqual(proc.returncode, 8, proc.stderr)
        state = self.state_json()
        self.assertEqual(state["quota"]["state"], "waiting")
        self.assertEqual(len(state["rounds"]), 0)
        self.assertIsNone(state["pending_call"])
        self.assertEqual(len(self.upstream.requests), 1)

        proc, _ = self.call("冷却期间重复调用。")
        self.assertEqual(proc.returncode, 8, proc.stderr)
        self.assertEqual(len(self.upstream.requests), 1, "冷却门必须阻止出站")
        self.assertIsNone(self.state_json()["pending_call"])
        self.assertIn("尚未结束", proc.stderr)

        proc = self.dispatch("quota", "--clear", "--source", "user",
                             "--reason", "用户确认额度已恢复")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.queue_plan({"decision": "continue", "summary": "恢复后继续"})
        proc, _ = self.call("恢复后调用。")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.upstream.requests), 2)
        self.assertEqual(len(self.state_json()["rounds"]), 1)

    # ── 9. 中断恢复及防重复调用 ──────────────────────────────────────────
    def test_09_timeout_recover_without_resend(self):
        self.start(task_id="e2e-recover")
        self.upstream.push(Reply(200, {"id": "late"}, delay=2.0))
        proc, _ = self.call("超时调用。", timeout=15,
                            extra=("--timeout", "0.5"))
        self.assertEqual(proc.returncode, 4, proc.stderr)
        state = self.state_json()
        self.assertIsNotNone(state["pending_call"])
        self.assertEqual(state["rounds"], [])
        self.assertEqual(len(self.upstream.requests), 1)

        proc, _ = self.call("重复调用。")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(len(self.upstream.requests), 1, "pending 期间不得再次出站")
        self.assertEqual(self.loop()["phase"], "RECOVER")

        proc = self.dispatch("recover", "--abandon")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.state_json()
        self.assertIsNone(state["pending_call"])
        self.assertEqual(state["rounds"], [])
        self.assertEqual(len(self.upstream.requests), 1)

        self.queue_plan({"decision": "continue", "summary": "恢复后新调用"})
        proc, _ = self.call("恢复后重新发起。")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.upstream.requests), 2)
        self.assertEqual(len(self.state_json()["rounds"]), 1)

    # ── 11. 并发任务隔离 ─────────────────────────────────────────────────
    def test_11_concurrent_tasks_are_isolated(self):
        self.start(task_id="e2e-iso-a")
        other = os.path.join(self.dir, "other.json")
        self.queue_plan({"decision": "continue", "summary": "A"})
        self.queue_plan({"decision": "continue", "summary": "B"})

        def fire(state_path: str, prompt: str, out: str):
            path = os.path.join(self.dir, prompt)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("并发任务\n")
            return subprocess.Popen(
                [sys.executable, "-B", CALL, "--mode", "PLAN",
                 "--dispatch-state", state_path, "--prompt-file", path,
                 "--audit-log", self.audit, "--out", out,
                 "--base-url", self.upstream.url, "--timeout", "10",
                 "--max-retries", "0"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=self.env, cwd=self.dir)

        proc = self.cli(DISPATCH, "start", "--state", other, "--goal", "另一个任务",
                        "--contract", "完成条件：隔离\n边界：测试",
                        "--task-id", "e2e-iso-b", "--mode", "PLAN")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out_a = os.path.join(self.dir, "a.md")
        out_b = os.path.join(self.dir, "b.md")
        a = fire(self.state, "pa.md", out_a)
        b = fire(other, "pb.md", out_b)
        _, err_a = a.communicate(timeout=30)
        _, err_b = b.communicate(timeout=30)
        self.assertEqual(a.returncode, 0, err_a)
        self.assertEqual(b.returncode, 0, err_b)
        with open(self.state, encoding="utf-8") as fh:
            state_a = json.load(fh)
        with open(other, encoding="utf-8") as fh:
            state_b = json.load(fh)
        self.assertEqual(state_a["task_id"], "e2e-iso-a")
        self.assertEqual(state_b["task_id"], "e2e-iso-b")
        self.assertEqual({r["task_id"] for r in state_a["rounds"]}, {"e2e-iso-a"})
        self.assertEqual({r["task_id"] for r in state_b["rounds"]}, {"e2e-iso-b"})
        self.assertEqual(len(self.upstream.requests), 2)

    # ── 12. 8 轮进度检查点 ───────────────────────────────────────────────
    def test_12_eight_round_checkpoint(self):
        self.start(task_id="e2e-checkpoint")
        for n in range(1, 9):
            self.queue_plan({"decision": "continue", "summary": f"第 {n} 轮"})
            proc, _ = self.call(f"第 {n} 轮。")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            stepped = self.loop()
            self.assertEqual(stepped["revision"], n)
            self.assertEqual(stepped["checkpoint"], n % 8 == 0)
        self.assertTrue(self.loop()["checkpoint"])
        self.assertEqual(len(self.state_json()["rounds"]), 8)

    # ── 13. 异常和状态写入失败 ──────────────────────────────────────────
    def test_13_state_write_failure_stops_before_http(self):
        self.start(task_id="e2e-write-fail")
        self.queue_plan({"decision": "continue", "summary": "不应发送"})
        os.chmod(self.dir, 0o555)
        self.addCleanup(os.chmod, self.dir, 0o755)
        proc, _ = self.call("写盘失败。")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(len(self.upstream.requests), 0)
        self.assertEqual(self.state_json()["rounds"], [])
        self.assertIsNone(self.state_json()["pending_call"])

    # ── 14. verdict 缺失 ────────────────────────────────────────────────
    def test_14_missing_verdict_is_not_pass(self):
        self.start(task_id="e2e-no-verdict")
        self.queue_plan({"decision": "continue", "summary": "只给安排"})
        proc, _ = self.call("请判断。")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.state_json()
        self.assertIsNone(state["rounds"][-1]["verdict"])
        self.assertFalse(self.loop()["pass_pending"])
        self.assertEqual(self.loop()["phase"], "GUIDANCE_NEEDED")

    # ── 15. verdict 冲突 ────────────────────────────────────────────────
    def test_15_conflicting_verdict_is_rejected(self):
        self.start(task_id="e2e-conflict")
        self.queue_plan({"decision": "continue", "verdict": "pass",
                         "summary": "冲突"})
        proc, _ = self.call("请判断。")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("不一致", proc.stderr)
        self.assertFalse(self.loop()["pass_pending"])
        self.assertFalse(self.state_json()["closure"]["closed"])

    # ── 16. G1 / G2 / G3 / G4 分别失败 ───────────────────────────────────
    def test_16a_g1_open_action_blocks_closure(self):
        self.passed_state(action_status="")
        state = self.state_json()
        self.assertEqual(state["actions"][0]["status"], "pending")
        check = self.closure_check()
        self.assertFalse(check["gates"]["G1"]["passed"])
        self.assertIn("action_open", [r["code"] for r in check["gates"]["G1"]["reasons"]])
        proc = self.dispatch("closure", "--close")
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.state_json()["closure"]["closed"])

    def test_16b_g2_dropped_evidence_blocks_closure(self):
        self.passed_state()
        proc = self.dispatch("evidence", "--id", "E1", "--drop",
                             "--reason", "模拟证据失效")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        check = self.closure_check()
        self.assertFalse(check["gates"]["G2"]["passed"])
        self.assertIn("evidence_missing", [r["code"] for r in check["gates"]["G2"]["reasons"]])
        proc = self.dispatch("closure", "--close")
        self.assertNotEqual(proc.returncode, 0)

    def test_16c_g3_stalled_action_blocks_closure(self):
        self.passed_state(action_status="failed")
        check = self.closure_check()
        self.assertFalse(check["gates"]["G3"]["passed"])
        self.assertIn("stalled", [r["code"] for r in check["gates"]["G3"]["reasons"]])
        self.assertFalse(check["gates"]["G1"]["passed"])
        proc = self.dispatch("closure", "--close")
        self.assertNotEqual(proc.returncode, 0)

    def test_16d_g4_high_risk_fails_closed_even_with_ledger(self):
        self.passed_state(risk="high")
        ledger = os.path.join(self.dir, "fake-ledger.json")
        with open(ledger, "w", encoding="utf-8") as fh:
            json.dump({"subject": "fake", "rounds": [
                {"verdict": "PASS", "model_identity_ok": True}]}, fh)
        check = self.closure_check("--review-ledger", ledger)
        self.assertFalse(check["gates"]["G4"]["passed"])
        self.assertEqual(check["gates"]["G4"]["reasons"][0]["code"],
                         "native_review_missing")
        proc = self.dispatch("closure", "--close", "--review-ledger", ledger)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.state_json()["closure"]["closed"])
        self.assertEqual(self.loop()["phase"], "BLOCKED")

    # ── 17. 裸 429 不误判为配额 ─────────────────────────────────────────
    def test_17_bare_429_is_upstream_error_not_quota(self):
        self.start(task_id="e2e-429")
        self.upstream.push(Reply(429, {"error": {"code": "rate_limit_exceeded"}}))
        proc, _ = self.call("请判断。")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        state = self.state_json()
        self.assertEqual(state["quota"]["state"], "ok")
        self.assertFalse(state["quota"].get("reset_at"))

    # ── 18. 配额成功后正确清除 ──────────────────────────────────────────
    def test_18_success_clears_quota_waiting(self):
        self.start(task_id="e2e-quota-clear")
        self.upstream.push(Reply(429, {"error": {
            "code": "usage_limit_reached", "resets_in_seconds": 0}}))
        proc, _ = self.call("触发配额。")
        self.assertEqual(proc.returncode, 8, proc.stderr)
        self.assertEqual(self.state_json()["quota"]["state"], "waiting")

        self.queue_plan({"decision": "continue", "summary": "冷却已过"})
        proc, _ = self.call("冷却到期后调用。")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        quota = self.state_json()["quota"]
        self.assertEqual(quota["state"], "ok")
        self.assertTrue(quota["cleared_at"])
        self.assertIn("call:", quota["clear_reason"])
        self.assertEqual(len(self.upstream.requests), 2)
        self.assertEqual(len(self.state_json()["rounds"]), 1)

    # ── 19. v1 → v2 迁移幂等 ───────────────────────────────────────────
    def test_19_v1_to_v2_migration_is_idempotent(self):
        legacy = {
            "version": 1, "task_id": "legacy-task", "goal": "旧任务",
            "round_seq": 3,
            "contract": {
                "done_criteria_text": "", "criteria": [],
                "criteria_state": "draft", "criteria_sha256": "",
                "confirmed_round_id": "",
            },
        }
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump(legacy, fh, ensure_ascii=False)
        proc = self.dispatch("decide", "--text", "第一次迁移写入", "--source", "user")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        first = self.state_json()
        self.assertEqual(first["version"], 2)
        self.assertEqual(first["risk"], "normal")
        self.assertEqual(first["task_id"], "legacy-task")
        self.assertEqual(first["goal"], "旧任务")
        self.assertEqual(first["round_seq"], 3)
        self.assertEqual(first["quota"]["state"], "ok")
        proc = self.dispatch("decide", "--text", "第二次写入", "--source", "user")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        second = self.state_json()
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["risk"], "normal")
        self.assertEqual(second["task_id"], "legacy-task")
        self.assertEqual(second["goal"], "旧任务")
        self.assertEqual(second["round_seq"], 3)
        self.assertEqual(second["quota"], first["quota"])
        self.assertEqual(len(second["decisions"]), 2)


if __name__ == "__main__":
    unittest.main()

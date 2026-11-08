#!/usr/bin/env python3
"""native 闭环可靠性：复核顺序、ingest/drive 分离、请求与 Token 预算。

全部使用 Fake Upstream 与构造的 native 信封，不联网、不调用真实模型。
这里验证的是状态机与代码门槛；真实 ZCode Reviewer 子代理的能力与自主性
不在此冒充，需另行真机验证。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)

from test_e2e_fake_upstream import Upstream, plan, success  # noqa: E402

import copilot_auto as A  # noqa: E402
import copilot_dispatch as D  # noqa: E402
import native_bridge as NB  # noqa: E402

AUTO = os.path.join(SCRIPTS, "copilot_auto.py")
CALL = os.path.join(SCRIPTS, "call_gpt.py")
DISPATCH = os.path.join(SCRIPTS, "copilot_dispatch.py")
MODEL = "gpt-6.1-sol"


class NativeFlowBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state_path = os.path.join(self.dir, "state.json")
        self.audit = os.path.join(self.dir, "audit.jsonl")
        self.workdir = os.path.join(self.dir, "auto")
        self.evidence_path = os.path.join(self.dir, "raw.log")
        with open(self.evidence_path, "w", encoding="utf-8") as fh:
            fh.write("NATIVE-FLOW-EVIDENCE: ok\nexit=0\n")
        self.upstream = Upstream()
        self.upstream.start()
        self.addCleanup(self.upstream.close)
        self.env = dict(os.environ)
        self.env.update({
            "CUSTOM_127_API_KEY": "sk-test-native-flow-not-real",
            "COPILOT_AUDIT_TEST_MODE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        })

    # ── 状态与信封 ────────────────────────────────────────────────────────
    def make_state(self, *, task_id: str = "native-flow", risk: str = "normal",
                   with_action: bool = True, backend: str = "native",
                   verdict: str = "partial",
                   max_requests: int | None = None,
                   max_total_tokens: int | None = None,
                   budget_projection: bool | None = None) -> dict:
        D.start(
            self.state_path, "native 闭环测试", "PLAN",
            contract="## 完成条件\n- 工作项完成\n边界：仅测试",
            task_id=task_id, criteria=["工作项完成"], risk=risk,
            execution_backend=backend, max_requests=max_requests,
            max_total_tokens=max_total_tokens,
            budget_projection=budget_projection)
        state = D.load_state(self.state_path)
        self.add_round(state, 1, verdict=verdict, decision="continue")
        if with_action:
            state["actions"].append({
                "id": "T1", "title": "执行 T1", "assignee": "zcode",
                "goal": "完成 T1", "constraints": "仅测试目录",
                "expected_evidence": "原始日志", "depends_on": [],
                "status": "pending", "result": "", "evidence_ids": [],
                "source_round_id": state["rounds"][-1]["round_id"],
                "created": D.now_iso(), "updated": D.now_iso(),
            })
        D.save_state(self.state_path, state)
        return D.load_state(self.state_path)

    def add_round(self, state: dict, seq: int, *, verdict: str,
                  decision: str) -> str:
        rid = f"{state['task_id']}-r{seq}"
        state["rounds"].append({
            "round_id": rid, "call_id": f"{rid}-call",
            "reply_sha256": NB.sha256_text(rid), "model_identity_ok": True,
            "mode": "PLAN", "decision": decision, "verdict": verdict,
            "ts": D.now_iso(),
        })
        state["round_seq"] = max(int(state.get("round_seq") or 0), seq)
        return rid

    def emit(self, state: dict | None = None) -> tuple[dict, dict]:
        return A._emit_native_dispatch(
            self.state_path, state or D.load_state(self.state_path), persist=True)

    def execution(self, state: dict, dispatch: dict,
                  status: str = "done") -> dict:
        action = dispatch["actions"][0]
        digest = NB.sha256_file(self.evidence_path)
        env = {
            "schema": NB.RESULT_SCHEMA,
            "task_id": state["task_id"],
            "round_id": dispatch["round_id"],
            "contract_sha256": state["contract_sha256"],
            "plan_call_id": dispatch["plan_call_id"],
            "plan_reply_sha256": dispatch["plan_reply_sha256"],
            "dispatch_id": action["dispatch_id"],
            "idempotency_key": action["idempotency_key"],
            "work_item_id": action["work_item_id"],
            "status": status,
            "summary": f"{action['work_item_id']} 已完成",
            "result": "实际执行输出：NATIVE-FLOW-EVIDENCE",
            "evidence": [{
                "id": "E1", "file": self.evidence_path,
                "sha256": digest, "kind": "test-log",
                "note": "T1 原始日志",
            }],
            "executor": {
                "kind": "subagent", "agent_id": "agent-executor",
                "child_session_id": "child-executor",
                "profile_id": "general-purpose",
                "model_reported": "deepseek/deepseek-v4-pro"},
            "timing": {"started_at": "2026-11-02T10:00:00+08:00",
                       "finished_at": "2026-11-02T10:00:01+08:00",
                       "duration_ms": 1000},
            "usage": {"total_tokens": 12, "tool_use_count": 1},
        }
        env["result_sha256"] = NB.envelope_sha256(env)
        return env

    def review(self, state: dict, dispatch: dict, execution: dict, *,
               verdict: str = "pass", findings: list[dict] | None = None,
               unverified: list[str] | None = None,
               reviewer: dict | None = None) -> dict:
        action = dispatch["actions"][0]
        env = {
            "schema": NB.REVIEW_SCHEMA,
            "task_id": state["task_id"],
            "round_id": dispatch["round_id"],
            "contract_sha256": state["contract_sha256"],
            "dispatch_id": action["dispatch_id"],
            "work_item_id": action["work_item_id"],
            "execution_result_sha256": execution["result_sha256"],
            "reviewer": reviewer or {
                "kind": "subagent", "agent_id": "agent-reviewer",
                "child_session_id": "child-reviewer",
                "profile_id": "reviewer",
                "model_reported": "deepseek/deepseek-v4-pro"},
            "verdict": verdict,
            "findings": findings or [],
            "files_inspected": [self.evidence_path],
            "commands_run": [],
            "changed_files": [],
            "unverified": unverified or [],
            "residual_risks": [],
            "g4_eligible": False,
        }
        env["result_sha256"] = NB.envelope_sha256(env)
        return env

    def write_json(self, name: str, obj: object) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
        return path

    def write_text(self, name: str, text: str) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def inject_pass_round(self, state: dict | None = None) -> str:
        """模拟「SOL 已给出 PASS」：直接构造已验证 PASS 轮并把条件核销到该轮。

        只用于测试本地门槛；真实 PASS 必须来自带任务凭证的 GPT 回复。
        """
        state = state or D.load_state(self.state_path)
        rid = self.add_round(state, int(state.get("round_seq") or 0) + 1,
                             verdict="pass", decision="stop")
        evidence = (state.get("evidence") or [{}])[0]
        for criterion in state["contract"]["criteria"]:
            criterion["status"] = "met"
            criterion["evidence_refs"] = [{
                "id": evidence.get("id", "E1"),
                "sha256": evidence.get("sha256", "")}]
            criterion["updated_round_id"] = rid
            criterion["updated_by"] = "test-pass-round"
            criterion["note"] = ""
        state["contract"]["criteria_state"] = "confirmed"
        state["contract"]["criteria_sha256"] = D.criteria_digest(
            state["contract"]["criteria"])
        D.save_state(self.state_path, state)
        return rid

    # ── CLI ───────────────────────────────────────────────────────────────
    def cli(self, script: str, *args: str, timeout: float = 60):
        return subprocess.run(
            [sys.executable, "-B", script, *args],
            capture_output=True, text=True, timeout=timeout,
            env=self.env, cwd=self.dir)

    def ingest(self, *args: str, timeout: float = 30):
        proc = self.cli(AUTO, "ingest", "--state", self.state_path,
                        "--audit-log", self.audit, *args, timeout=timeout)
        payload = {}
        if proc.stdout.strip():
            try:
                payload = json.loads(proc.stdout)
            except ValueError:
                self.fail(f"ingest 输出不是 JSON：{proc.stdout[:500]}")
        return proc, payload

    def drive(self, *args: str, timeout: float = 60):
        proc = self.cli(
            AUTO, "drive", "--state", self.state_path, "--audit-log", self.audit,
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

    def closure(self, *args: str):
        return self.cli(DISPATCH, "closure", "--state", self.state_path, *args)

    def state_json(self) -> dict:
        return D.load_state(self.state_path)

    def audit_events(self, event: str) -> list[dict]:
        if not os.path.exists(self.audit):
            return []
        out = []
        with open(self.audit, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if record.get("event") == event:
                    out.append(record)
        return out

    def seed_audit(self, task_id: str, calls: list[dict]) -> None:
        """写入已发生的调用记录：calls=[{input,output,total}]（缺省为成功）。"""
        with open(self.audit, "a", encoding="utf-8") as fh:
            for index, usage in enumerate(calls, 1):
                call_id = f"seed-{index}"
                fh.write(json.dumps({
                    "event": "call_requested", "task_id": task_id,
                    "call_id": call_id, "channel": "cpa",
                    "max_attempts": 1}) + "\n")
                record = {"event": "call_result", "task_id": task_id,
                          "call_id": call_id, "channel": "cpa", "ok": True,
                          "attempts": 1}
                if usage is not None:
                    record["usage"] = {
                        "input_tokens": usage.get("input", 0),
                        "output_tokens": usage.get("output", 0),
                        "total_tokens": usage.get(
                            "total", usage.get("input", 0) + usage.get("output", 0)),
                    }
                fh.write(json.dumps(record) + "\n")

    def queue(self, plan_obj: dict) -> None:
        self.upstream.push(success(plan(plan_obj), model=MODEL))


class NativeFullChainTests(NativeFlowBase):
    def test_partial_without_plan_derives_executes_reviews_and_passes(self):
        """PARTIAL 无 plan：本地派生 → native 派工 → 回填执行/复核 → 再判断 → PASS。"""
        state = self.make_state(with_action=False)

        # 1) 派生工作项并生成可绑定的 native dispatch（本步骤 0 请求）。
        proc, first = self.drive()
        self.assertEqual(proc.returncode, 10, proc.stderr)
        self.assertEqual(first["status"], "execute")
        self.assertEqual(self.upstream.requests, [])
        work = first["actions"][0]
        order = work["work_order"]
        self.assertTrue(work["id"].startswith("AUTO-"), work)
        self.assertEqual(order["origin"], "partial-without-plan")
        self.assertEqual(order["executed_by"], "main-agent")
        self.assertTrue(order["directive_complete"])
        self.assertIn("不得自行扩大授权", order["authorization"])
        dispatch = first["native_dispatch"]
        self.assertTrue(dispatch["dispatch_id"])
        item = dispatch["actions"][0]
        self.assertEqual(item["work_item_id"], work["id"])
        self.assertTrue(item["idempotency_key"])
        self.assertEqual(dispatch["round_id"], first["round_id"])

        # 2) ZCode 主 Agent（模拟执行方）执行并回填原始证据；复核独立回填。
        state = self.state_json()
        execution = self.execution(state, dispatch)
        execution_path = self.write_json("chain-execution.json", execution)
        proc, ingested = self.ingest("--native-result", execution_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(ingested["http_requests"], 0)
        self.assertEqual(self.upstream.requests, [])

        review = self.review(self.state_json(), dispatch, execution)
        review_path = self.write_json("chain-review.json", review)
        proc, reviewed = self.ingest("--native-result", review_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(reviewed["http_requests"], 0)

        # 3) 新材料交回 SOL：下一次 drive 必须携带执行与复核材料再判断。
        self.queue({
            "decision": "stop", "verdict": "pass", "summary": "已完成并复核",
            "criteria_confirmed": True,
            "criteria": [{"criterion_id": "C1", "status": "met",
                          "evidence_refs": ["E1"]}],
        })
        proc, second = self.drive()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(second["status"], "closed")
        self.assertEqual(len(self.upstream.requests), 1)

        payload_text = json.dumps(self.upstream.requests[0]["payload"],
                                  ensure_ascii=False)
        self.assertIn("最近一次执行报告", payload_text)
        self.assertIn("原生独立复核报告", payload_text)
        self.assertIn(order["id"], payload_text)

        saved = self.state_json()
        self.assertEqual(len(saved["native_results"]), 1)
        self.assertEqual(saved["native_results"][0]["work_item_id"], order["id"])
        self.assertEqual(len(saved["native_reviews"]), 1)
        self.assertEqual(saved["native_reviews"][0]["g4_eligible"], False)
        self.assertEqual([a["status"] for a in saved["actions"]], ["done"])
        self.assertEqual(saved["closure"]["verdict"], "pass")
        self.assertEqual(saved["closure"]["closed_by"], "gate")


class IngestSeparationTests(NativeFlowBase):
    def test_ingests_are_local_and_drive_is_explicit(self):
        """执行/复核 ingest 均为 0 请求；只有随后显式 drive 才请求 SOL。"""
        state = self.make_state()
        dispatch, state = self.emit(state)
        execution = self.execution(state, dispatch)
        execution_path = self.write_json("execution.json", execution)
        review = self.review(state, dispatch, execution)
        review_path = self.write_json("review.json", review)
        round_before = state["round_seq"]

        # dry-run 只校验信封，不登记证据、不写报告、不产生请求。
        proc_dry, payload_dry = self.ingest(
            "--native-result", execution_path, "--dry-run")
        self.assertEqual(proc_dry.returncode, 0, proc_dry.stderr)
        self.assertEqual(payload_dry["status"], "ingest_valid")
        self.assertEqual(len(self.state_json()["native_results"]), 0)

        proc, payload = self.ingest("--native-result", execution_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(payload["status"], "ingested")
        self.assertFalse(payload["duplicate"])
        self.assertEqual(payload["http_requests"], 0)
        self.assertEqual(payload["phase"], "REVIEW_PENDING")
        self.assertEqual(payload["round_seq"], round_before)
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(self.audit_events("call_requested"), [])
        saved = self.state_json()
        self.assertEqual(len(saved["native_results"]), 1)
        self.assertIsNone(saved["pending_call"])
        self.assertEqual(saved["round_seq"], round_before)

        # 幂等：同一执行信封重复回填不改变状态、不报错。
        proc_dup, payload_dup = self.ingest("--native-result", execution_path)
        self.assertEqual(proc_dup.returncode, 0, proc_dup.stderr)
        self.assertTrue(payload_dup["duplicate"])
        self.assertEqual(len(self.state_json()["native_results"]), 1)

        # 复核未回填时 drive 停在 REVIEW_PENDING，不产生 SOL 请求。
        proc_wait, payload_wait = self.drive()
        self.assertEqual(proc_wait.returncode, 11, proc_wait.stderr)
        self.assertEqual(payload_wait["status"], "review_pending")
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(self.audit_events("call_requested"), [])

        # 复核 ingest 同样 0 请求，并解除 REVIEW_PENDING。
        proc_review, payload_review = self.ingest("--native-result", review_path)
        self.assertEqual(proc_review.returncode, 0, proc_review.stderr)
        self.assertEqual(payload_review["http_requests"], 0)
        self.assertEqual(payload_review["phase"], "GUIDANCE_NEEDED")
        self.assertTrue(payload_review["native_review"]["passed"])
        self.assertEqual(self.upstream.requests, [])
        proc_dup_review, payload_dup_review = self.ingest(
            "--native-result", review_path)
        self.assertTrue(payload_dup_review["duplicate"])
        self.assertEqual(len(self.state_json()["native_reviews"]), 1)

        # 两份报告齐备后，只有显式 drive 产生一次 SOL 请求。
        self.queue({
            "decision": "continue", "summary": "复核通过，安排 T2",
            "actions": [{
                "id": "T2", "title": "T2", "assignee": "zcode",
                "goal": "继续", "constraints": "仅测试",
                "expected_evidence": "日志", "depends_on": ["T1"]}],
        })
        proc_call, payload_call = self.drive()
        self.assertEqual(proc_call.returncode, 10, proc_call.stderr)
        self.assertEqual(payload_call["status"], "execute")
        self.assertEqual(len(self.upstream.requests), 1)
        requested = self.audit_events("call_requested")
        self.assertEqual(len(requested), 1)
        self.assertEqual(requested[0]["max_attempts"], 1)
        self.assertEqual(self.state_json()["round_seq"], round_before + 1)

    def test_closed_state_rejects_ingest(self):
        state = self.make_state()
        dispatch, state = self.emit(state)
        execution = self.execution(state, dispatch)
        execution_path = self.write_json("execution.json", execution)
        review_path = self.write_json(
            "review.json", self.review(state, dispatch, execution))
        self.assertEqual(self.ingest("--native-result", execution_path)[0].returncode, 0)
        self.assertEqual(self.ingest("--native-result", review_path)[0].returncode, 0)
        self.inject_pass_round()
        loop = D.loop_step(self.state_path, persist=True)
        self.assertEqual(loop["phase"], "CLOSURE_CHECK", loop)
        closed = self.closure("--close")
        self.assertEqual(closed.returncode, 0, closed.stderr)
        before = self.state_json()
        self.assertTrue(before["closure"]["closed"])

        proc, _ = self.ingest("--native-result", execution_path)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("已收口", proc.stderr)
        after = self.state_json()
        self.assertEqual(len(after["native_results"]), len(before["native_results"]))
        self.assertEqual(len(after["native_reviews"]), len(before["native_reviews"]))
        proc_drive, payload_drive = self.drive()
        self.assertEqual(proc_drive.returncode, 0, proc_drive.stderr)
        self.assertEqual(payload_drive["status"], "closed")
        self.assertEqual(self.upstream.requests, [])


class ReviewOrderingTests(NativeFlowBase):
    def test_gpt_pass_without_review_cannot_close(self):
        state = self.make_state()
        dispatch, state = self.emit(state)
        execution = self.execution(state, dispatch)
        execution_path = self.write_json("execution.json", execution)
        self.assertEqual(self.ingest("--native-result", execution_path)[0].returncode, 0)
        # 模拟 SOL 已 PASS（本地构造），但复核尚未回填。
        self.inject_pass_round()
        self.assertTrue(D.pass_pending(self.state_json()))

        proc, payload = self.drive()
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(payload["status"], "review_pending")
        self.assertFalse(payload["native_review"]["passed"])
        self.assertEqual(payload["native_review"]["unreviewed"][0]["work_item_id"], "T1")
        self.assertEqual(self.upstream.requests, [])
        self.assertFalse(self.state_json()["closure"]["closed"])

        closed = self.closure("--close")
        self.assertNotEqual(closed.returncode, 0)
        self.assertIn("native_review_missing", closed.stderr)
        self.assertFalse(self.state_json()["closure"]["closed"])

        # 回填通过复核后才允许收口（收口本身是本地操作，0 请求）。
        review_path = self.write_json(
            "review.json", self.review(state, dispatch, execution))
        self.assertEqual(self.ingest("--native-result", review_path)[0].returncode, 0)
        proc_close, payload_close = self.drive()
        self.assertEqual(proc_close.returncode, 0, proc_close.stderr)
        self.assertEqual(payload_close["status"], "closed")
        self.assertEqual(self.upstream.requests, [])
        saved = self.state_json()
        self.assertTrue(saved["closure"]["closed"])
        self.assertTrue(saved["closure"]["gate"]["passed"])
        self.assertEqual(saved["native_reviews"][0]["g4_eligible"], False)

    def test_blocking_review_prevents_close_and_reaches_sol(self):
        state = self.make_state()
        dispatch, state = self.emit(state)
        execution = self.execution(state, dispatch)
        self.assertEqual(
            self.ingest("--native-result", self.write_json("execution.json", execution))[0].returncode,
            0)
        blocking = [{
            "severity": "blocking", "summary": "日志缺少失败退出码",
            "file": self.evidence_path,
            "evidence": "raw.log 只记录了 exit=0，没有失败分支的证据"}]
        review_path = self.write_json(
            "review.json", self.review(state, dispatch, execution,
                                       verdict="partial", findings=blocking))
        proc_review, payload_review = self.ingest("--native-result", review_path)
        self.assertEqual(proc_review.returncode, 0, proc_review.stderr)
        self.assertFalse(payload_review["native_review"]["passed"])
        self.inject_pass_round()

        closed = self.closure("--close")
        self.assertNotEqual(closed.returncode, 0)
        self.assertIn("native_review_blocking", closed.stderr)
        self.assertFalse(self.state_json()["closure"]["closed"])

        self.queue({
            "decision": "continue", "summary": "先修复 blocking finding",
            "actions": [{
                "id": "T2", "title": "补齐失败分支日志", "assignee": "zcode",
                "goal": "补证据", "constraints": "仅测试",
                "expected_evidence": "失败分支日志", "depends_on": ["T1"]}],
        })
        proc, payload = self.drive()
        self.assertEqual(proc.returncode, 10, proc.stderr)
        self.assertEqual(payload["status"], "execute")
        self.assertEqual(len(self.upstream.requests), 1)
        prompt = json.dumps(self.upstream.requests[0]["payload"], ensure_ascii=False)
        self.assertIn("日志缺少失败退出码", prompt)
        self.assertIn("native_review_blocking", prompt)
        # 即便 SOL 安排了修复，CLOSED 仍被复核门槛挡住。
        self.assertFalse(self.state_json()["closure"]["closed"])

    def test_high_risk_g4_still_fail_closed(self):
        state = self.make_state(risk="high")
        dispatch, state = self.emit(state)
        execution = self.execution(state, dispatch)
        self.assertEqual(
            self.ingest("--native-result", self.write_json("execution.json", execution))[0].returncode,
            0)
        review_path = self.write_json(
            "review.json", self.review(state, dispatch, execution))
        self.assertEqual(self.ingest("--native-result", review_path)[0].returncode, 0)
        self.inject_pass_round()

        check = D.closure_check(self.state_json())
        self.assertFalse(check["passed"])
        self.assertEqual(check["gates"]["G4"]["passed"], False)
        # Reviewer PASS 不是 G4 凭证。
        saved = self.state_json()
        self.assertEqual(saved["native_reviews"][0]["g4_eligible"], False)

        # 落一次四门记录（G4 失败），收口必须报告 G4 缺口而不是笼统要求 loop-step。
        D.loop_step(self.state_path, persist=True)
        closed = self.closure("--close")
        self.assertNotEqual(closed.returncode, 0)
        self.assertIn("native_review_missing", closed.stderr)
        proc, payload = self.drive()
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(self.upstream.requests, [])


class BudgetTests(NativeFlowBase):
    def test_native_defaults_are_unbounded_and_derive_work(self):
        """默认不设请求/Token 上限；PARTIAL 无安排时本地派生工作项而不是等用户。"""
        state = self.make_state(with_action=False)
        limits = D.budget_limits(state)
        self.assertIsNone(limits["max_requests"])
        self.assertIsNone(limits["max_total_tokens"])
        self.assertTrue(limits["default_unbounded"])
        status = D.budget_status(state, self.audit)
        self.assertTrue(status["allowed"])
        self.assertEqual(status["reason"], "")

        proc, payload = self.drive()
        self.assertEqual(proc.returncode, 10, proc.stderr)
        self.assertEqual(payload["status"], "execute")
        saved = self.state_json()
        self.assertEqual(saved["actions"][-1]["origin"], "partial-without-plan")
        self.assertIsNone(saved["pending_call"])
        # 派生是纯本地的：不消耗付费请求，也不要求用户中转。
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(self.audit_events("call_requested"), [])

    def test_request_cap_six_is_enforced_before_send(self):
        """显式运营上限仍然生效：这是调用方设置的上限，不是代码默认值。"""
        state = self.make_state(with_action=False, verdict="continue",
                                max_requests=6)
        self.assertEqual(state["budget"]["max_requests"], 6)
        limits = D.budget_limits(state)
        self.assertTrue(limits["enforced"])
        self.assertTrue(limits["explicit"])
        self.seed_audit(state["task_id"], [None] * 6)
        proc, payload = self.drive()
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(payload["status"], "budget_exhausted")
        self.assertEqual(payload["reason"], "request_limit_reached")
        self.assertEqual(payload["budget"]["spend"]["requests"], 6)
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(len(self.audit_events("call_requested")), 6)
        self.assertEqual(self.state_json()["round_seq"], state["round_seq"])

    def test_call_gpt_itself_refuses_without_send(self):
        """预算门也在 call_gpt 内：绕过 drive 也不能超过显式上限。"""
        state = self.make_state(with_action=False, verdict="continue",
                                max_requests=6)
        self.seed_audit(state["task_id"], [None] * 6)
        prompt = self.write_text("prompt.md", "预算测试")
        before = self.state_json()
        proc = self.cli(
            CALL, "--mode", "PLAN", "--dispatch-state", self.state_path,
            "--prompt-file", prompt, "--audit-log", self.audit,
            "--out", os.path.join(self.dir, "reply.md"),
            "--base-url", self.upstream.url, "--timeout", "5",
            "--max-output-tokens", "4096")
        self.assertEqual(proc.returncode, 12, proc.stderr)
        self.assertIn("预算拒绝出站", proc.stderr)
        self.assertEqual(self.upstream.requests, [])
        after = self.state_json()
        self.assertEqual(after["round_seq"], before["round_seq"])
        self.assertIsNone(after["pending_call"])
        self.assertEqual(len(self.audit_events("call_requested")), 6)

    def test_token_usage_accumulates_and_survives_restart(self):
        state = self.make_state(with_action=False)
        self.seed_audit(state["task_id"], [
            {"input": 100_000, "output": 50_000, "total": 150_000},
            {"input": 30_000, "output": 20_000, "total": 50_000},
        ])
        # 身份校验失败也消耗了真实额度：有 usage 就必须计入，不能只看 ok=true。
        with open(self.audit, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "event": "call_requested", "task_id": state["task_id"],
                "call_id": "identity-fail"}) + "\n")
            fh.write(json.dumps({
                "event": "call_result", "task_id": state["task_id"],
                "call_id": "identity-fail", "ok": False,
                "usage": {"input_tokens": 1000, "output_tokens": 0,
                          "total_tokens": 1000}}) + "\n")
        status = D.budget_status(state, self.audit)
        self.assertTrue(status["allowed"])
        self.assertEqual(status["spend"]["requests"], 3)
        self.assertEqual(status["spend"]["input_tokens"], 131_000)
        self.assertEqual(status["spend"]["output_tokens"], 70_000)
        self.assertEqual(status["spend"]["total_tokens"], 201_000)
        self.assertEqual(status["spend"]["unknown_usage_calls"], 0)

        # 模拟进程重启：删掉本地缓存，仍从审计重建出相同用量。
        on_disk = D.load_state(self.state_path)
        on_disk["budget"]["usage"] = {}
        D.save_state(self.state_path, on_disk)
        restarted = D.budget_status(D.load_state(self.state_path), self.audit)
        self.assertEqual(restarted["spend"]["total_tokens"], 201_000)
        self.assertEqual(restarted["spend"]["requests"], 3)

    def test_token_threshold_and_projection_refuse_outbound(self):
        """Token 阈值同样只在显式设置时生效；默认不因累计费用阻止调用。"""
        state = self.make_state(with_action=False, verdict="continue",
                                max_total_tokens=500_000,
                                budget_projection=True)
        plain = json.loads(json.dumps(state))
        plain["budget"]["max_total_tokens"] = None
        self.assertTrue(D.budget_status(plain, self.audit)["allowed"])
        self.seed_audit(state["task_id"], [
            {"input": 300_000, "output": 160_000, "total": 460_000},
        ])
        status = D.budget_status(state, self.audit)
        self.assertFalse(status["allowed"])
        self.assertEqual(status["reason"], "token_budget_projected")
        self.assertEqual(status["projected_total"], 920_000)

        # 达到阈值后即便关闭预估也拒绝。
        exhausted = D.load_state(self.state_path)
        exhausted["budget"]["projection"] = False
        D.save_state(self.state_path, exhausted)
        self.seed_audit(state["task_id"], [
            {"input": 20_000, "output": 20_000, "total": 40_000},
        ])
        status2 = D.budget_status(D.load_state(self.state_path), self.audit)
        self.assertFalse(status2["allowed"])
        self.assertEqual(status2["reason"], "token_budget_reached")
        self.assertEqual(status2["spend"]["total_tokens"], 500_000)

        proc, payload = self.drive()
        self.assertEqual(proc.returncode, 11, proc.stderr)
        self.assertEqual(payload["status"], "budget_exhausted")
        self.assertEqual(payload["reason"], "token_budget_reached")
        self.assertEqual(self.upstream.requests, [])


class LegacyCompatibilityTests(NativeFlowBase):
    def test_legacy_default_budget_is_unbounded(self):
        """legacy 默认同样不设上限，PARTIAL 无安排时同样派生工作项。"""
        state = self.make_state(with_action=False, backend="legacy",
                                task_id="legacy-flow")
        self.assertIsNone(state["budget"]["max_requests"])
        self.assertIsNone(state["budget"]["max_total_tokens"])
        status = D.budget_status(state, self.audit)
        self.assertFalse(status["limits"]["applicable"])
        self.assertFalse(status["limits"]["enforced"])
        self.assertTrue(status["allowed"])
        self.assertTrue(status["unbounded_default"])

        proc, payload = self.drive()
        self.assertEqual(proc.returncode, 10, proc.stderr)
        self.assertEqual(payload["status"], "execute")
        self.assertEqual(self.state_json()["execution_backend"], "legacy")
        saved = self.state_json()
        self.assertEqual(saved["actions"][-1]["origin"], "partial-without-plan")
        self.assertEqual(self.upstream.requests, [])
        # legacy 保留 call_gpt 的历史默认：2 次重试（max_attempts=3）只在真正
        # 出站时体现；本轮没有出站，因此不产生 call_requested 记录。
        self.assertEqual(self.audit_events("call_requested"), [])


if __name__ == "__main__":
    unittest.main()

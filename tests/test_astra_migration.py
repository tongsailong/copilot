#!/usr/bin/env python3
"""v9.9：Astra 默认认知引擎迁移、调用效率、模型级退避隔离与响应完整性。

覆盖四组要求：
- A 迁移：所有默认入口都是 `gpt-6.2-astra` + high + standard + 零工具；SOL 只
  作为显式兼容模型，不做自动回退，历史 SOL 结论不被当成 Astra 的新响应。
- B 效率：纯登记/报告命令不隐式付费调用；复用命中可观测；复用键含 task_id。
- C 隔离：退避窗口按 provider+model+error_class 隔离，互不冻结。
- D 完整性：incomplete / max_output_tokens 不算成功，24000 预算不下调。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

import v6_support as F

import copilot_dispatch as CD
import governance as G
import sol_review as SR
import sol_schedule as SS


def think_text() -> str:
    return ("## 判断\n先补测试\n\n## 方案\n- 先测后改\n\n"
            "## 风险\n- 回归不足\n")


def think_result(mode: str = "CONSULT", model: str = "") -> dict:
    """通道替身：结构与真实 run_sol 一致，不联网。"""
    resolved = model or SR.DEFAULT_MODEL
    return {
        "schema": SR.THINK_SCHEMA, "mode": mode,
        "model": resolved, "model_resolved": resolved,
        "provider": SR.PROVIDER,
        "reasoning_effort": "high", "reasoning_mode": "standard",
        "reasoning_resolved": {"effort": "high"}, "reasoning_verified": True,
        "zero_tool": True, "attempts": 1, "request_id": "req-1",
        "trace_id": "trace-1", "response_id": "resp-1",
        "materials_sha256": "e" * 64, "materials_chars": 10,
        "request_sha256": "r" * 64, "reply_text": think_text(),
        "reply_sha256": "a" * 64, "reply_chars": 10,
        "usage": {"total_tokens": 42, "input_tokens": 30, "output_tokens": 12},
        "usage_available": True, "latency_ms": 1,
        "judgment": "先补测试", "essence": "缺少验证", "strategy": "先测后改",
        "tradeoffs": [], "risks": [], "priority": [],
        "verify": [], "reconsult": [], "unverified": [],
        "completed_at": G.now_iso(),
    }


# ── A. 默认入口迁移 ────────────────────────────────────────────────────────
class DefaultModelMigrationTests(unittest.TestCase):
    def test_payload_defaults_to_astra_high_zero_tool(self):
        payload = SR.build_payload("材料")
        self.assertEqual(payload["model"], "gpt-6.2-astra")
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(payload["tool_choice"], "none")
        self.assertNotIn("tools", payload)
        self.assertNotIn("functions", payload)
        self.assertEqual(SR.DEFAULT_MODEL, "gpt-6.2-astra")
        self.assertEqual(SR.MODEL, SR.DEFAULT_MODEL)
        self.assertEqual(SR.LEGACY_MODEL, "gpt-6.2-sol")
        self.assertEqual(SR.PROVIDER, "codex")

    def test_every_mode_defaults_to_astra(self):
        for mode in SR.SOL_MODES:
            with self.subTest(mode=mode):
                payload = SR.build_payload("材料", mode=mode)
                self.assertEqual(payload["model"], "gpt-6.2-astra")
                self.assertEqual(payload["reasoning"], {"effort": "high"})
                self.assertEqual(payload["tool_choice"], "none")

    def test_sol_is_explicit_compat_only(self):
        self.assertEqual(SR.resolve_model(""), SR.DEFAULT_MODEL)
        self.assertEqual(SR.resolve_model("gpt-6.2-sol"), "gpt-6.2-sol")
        self.assertEqual(SR.model_key("gpt-6.2-sol"), "codex:gpt-6.2-sol")
        with self.assertRaises(SR.SolUsageError) as ctx:
            SR.resolve_model("gpt-6.1-sol")
        self.assertEqual(ctx.exception.code, "sol_model_unsupported")
        self.assertEqual(SR.build_payload("材料", model="gpt-6.2-sol")["model"],
                         "gpt-6.2-sol")

    def test_legacy_sol_record_is_not_reused_as_astra(self):
        state = {"sol_calls": [
            {"status": "completed", "mode": "CONSULT",
             "model": SR.LEGACY_MODEL, "materials_sha256": "a" * 64},
            # v8/v9.8 记录没有模型归属：任何模型都不把它当作新响应复用。
            {"status": "completed", "mode": "CONSULT",
             "materials_sha256": "b" * 64},
        ]}
        self.assertEqual(SR.single_round_decision(
            state, "a" * 64, mode="CONSULT")["action"], "call")
        refused = SR.single_round_decision(
            state, "b" * 64, mode="CONSULT", model="gpt-6.2-sol")
        self.assertEqual(refused["action"], "refuse")
        self.assertEqual(refused["code"], "sol_review_requires_reason")
        # 显式选择 SOL 时同模型记录仍可复用：兼容能力保留，只是不再是默认。
        self.assertEqual(SR.single_round_decision(
            state, "a" * 64, mode="CONSULT", model="gpt-6.2-sol")["action"],
            "reuse")


class DispatchMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="交付可复核结果",
                      contract=F.CONTRACT, risk="medium",
                      task_id="astra-migration", workspace=self.dir,
                      task_kind="standard")
        self.materials = os.path.join(self.dir, "materials.md")
        with open(self.materials, "w", encoding="utf-8") as fh:
            fh.write("材料正文\n")

    def state(self) -> dict:
        return G.load_state(self.state_path)

    def _consult(self, *extra: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return CD.main(["sol-consult", "--state", self.state_path,
                            "--materials", self.materials,
                            "--question", "本次要判断什么", *extra])

    def test_default_call_records_astra_and_provider(self):
        with mock.patch.object(SR, "run_consult",
                               return_value=think_result()) as run:
            self.assertEqual(self._consult(), 0)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs.get("model"),
                         SR.DEFAULT_MODEL)
        record = self.state()["sol_calls"][-1]
        self.assertEqual(record["model"], SR.DEFAULT_MODEL)
        self.assertEqual(record["model_key"], SR.model_key())
        self.assertEqual(record["provider"], SR.PROVIDER)

    def test_astra_failure_never_falls_back_to_sol(self):
        error = SR.SolOverloadedError(
            "sol_upstream_overloaded", "上游瞬时容量不足",
            http_status=502, retry_after=300,
            upstream_code="server_is_overloaded",
            error_class=SR.UPSTREAM_OVERLOADED, response_received=True)
        error.request_attempted = True
        error.send_observed = "response_received"
        with mock.patch.object(SR, "run_consult", side_effect=error) as run:
            self.assertEqual(self._consult(), 0)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs.get("model"),
                         SR.DEFAULT_MODEL)
        records = self.state()["sol_calls"]
        self.assertEqual([item.get("model") for item in records],
                         [SR.DEFAULT_MODEL])
        self.assertNotIn(SR.LEGACY_MODEL,
                         [item.get("model") for item in records])

    def test_explicit_model_flag_is_the_only_way_to_sol(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = CD.main(["sol-consult", "--state", self.state_path,
                            "--materials", self.materials,
                            "--question", "本次要判断什么",
                            "--model", SR.LEGACY_MODEL,
                            "--dry-run", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["model"], SR.LEGACY_MODEL)
        self.assertEqual(payload["model_key"], SR.model_key(SR.LEGACY_MODEL))

    def test_unknown_model_is_rejected_before_any_request(self):
        with mock.patch.object(SR, "run_consult",
                               side_effect=AssertionError("不应请求")):
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    CD.main(["sol-consult", "--state", self.state_path,
                             "--materials", self.materials,
                             "--question", "本次要判断什么",
                             "--model", "gpt-6.0-fake"])


# ── B. 调用效率：纯登记命令不隐式付费、复用可观测 ─────────────────────────
class RegistrationNoImplicitCallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="交付可复核结果",
                      contract=F.CONTRACT, risk="medium",
                      task_id="astra-efficiency", workspace=self.dir,
                      task_kind="standard")
        self.materials = os.path.join(self.dir, "materials.md")
        with open(self.materials, "w", encoding="utf-8") as fh:
            fh.write("材料正文\n")

    def state(self) -> dict:
        return G.load_state(self.state_path)

    def _judgment(self, *extra: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return CD.main(["judgment", "--state", self.state_path,
                            "--status", "pass", "--summary", "阶段判断",
                            *extra])

    def test_judgment_does_not_call_gpt_even_with_env_auto(self):
        env = {"COPILOT_SOL": "auto", "COPILOT_SOL_AUTO": "on"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(SR, "run_consult",
                                  side_effect=AssertionError("不应请求")), \
                mock.patch.object(SR, "run_plan",
                                  side_effect=AssertionError("不应请求")):
            self.assertEqual(self._judgment(), 0)
        self.assertEqual([item for item in self.state().get("sol_calls") or []
                          if item.get("status") == "completed"], [])

    def test_explicit_auto_still_executes_one_obligation(self):
        with mock.patch.dict(os.environ, {"COPILOT_SOL_AUTO": "on"}), \
                mock.patch.object(SR, "run_consult",
                                  return_value=think_result()) as consult, \
                mock.patch.object(SR, "run_plan",
                                  return_value=think_result("PLAN")) as plan:
            self.assertEqual(self._judgment("--cognition", "auto"), 0)
        self.assertEqual(consult.call_count + plan.call_count, 1)

    def test_registration_default_is_advise_and_alias_parses(self):
        parser = CD.build_parser()
        required = {
            "judgment": ["--status", "pass", "--summary", "判断"],
            "record": ["--id", "A1", "--status", "done"],
            "report": ["--summary", "摘要"],
            "criterion": ["--id", "C1", "--status", "met"],
        }
        for command, extra in required.items():
            with self.subTest(command=command):
                args = parser.parse_args(
                    [command, "--state", self.state_path, *extra])
                self.assertEqual(args.sol, "")
                self.assertEqual(args.sol_default, "advise")
        args = parser.parse_args(["judgment", "--state", self.state_path,
                                  "--status", "pass", "--summary", "判断",
                                  "--cognition", "auto"])
        self.assertEqual(args.sol, "auto")


class ReuseEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="交付可复核结果",
                      contract=F.CONTRACT, risk="medium",
                      task_id="astra-reuse", workspace=self.dir,
                      task_kind="standard")
        self.materials = os.path.join(self.dir, "materials.md")
        with open(self.materials, "w", encoding="utf-8") as fh:
            fh.write("材料正文\n")

    def state(self) -> dict:
        return G.load_state(self.state_path)

    def _consult(self) -> int:
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return CD.main(["sol-consult", "--state", self.state_path,
                            "--materials", self.materials,
                            "--question", "本次要判断什么"])

    def test_same_context_is_reused_once_and_counted(self):
        with mock.patch.object(SR, "run_consult",
                               return_value=think_result()) as run:
            self.assertEqual(self._consult(), 0)
            self.assertEqual(self._consult(), 0)
        self.assertEqual(run.call_count, 1)
        state = self.state()
        self.assertEqual(len(state["sol_calls"]), 1)
        self.assertEqual(len(state.get("sol_reuses") or []), 1)
        stats = SS.task_stats(state)
        self.assertEqual(stats["reuse"]["hits"], 1)
        self.assertEqual(stats["reuse"]["by_model"],
                         [{"model_key": SR.model_key(), "hits": 1}])
        row = stats["by_model"][0]
        self.assertEqual(row["model_key"], SR.model_key())
        self.assertEqual(row["repeat_calls"], 0)
        self.assertEqual(row["input_tokens"], 30)
        self.assertEqual(row["output_tokens"], 12)

    def test_reuse_key_includes_task_id(self):
        state = self.state()
        other = {**state, "task_id": "task-other"}
        self.assertNotEqual(
            SR.review_subject_sha256(state, "材料", mode="CONSULT"),
            SR.review_subject_sha256(other, "材料", mode="CONSULT"))

    def test_new_evidence_invalidates_reuse(self):
        state = self.state()
        before = SR.review_subject_sha256(state, "材料", mode="CONSULT")
        state = {**state, "evidence": [{"id": "E1", "sha256": "a" * 64,
                                        "kind": "text"}]}
        after = SR.review_subject_sha256(state, "材料", mode="CONSULT")
        self.assertNotEqual(before, after)


# ── C. 模型级退避隔离 ──────────────────────────────────────────────────────
class ModelBackoffIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="目标", contract=F.CONTRACT,
                      risk="medium", task_id="astra-isolation")

    def state(self) -> dict:
        return G.load_state(self.state_path)

    def _window(self, model: str, seconds: float, error_class: str) -> dict:
        window = G._sol_backoff_from_failure({
            "error_class": error_class, "retry_after": seconds,
            "http_status": 502, "call_id": "SCfail",
            "model": model, "provider": SR.PROVIDER,
            "model_key": SR.model_key(model)})
        return window

    def _store(self, model: str, seconds: float,
               error_class: str = SR.UPSTREAM_OVERLOADED) -> None:
        state = self.state()
        G.store_model_backoff(state, self._window(model, seconds, error_class))
        G.save_state(self.state_path, state)

    def test_sol_window_does_not_block_astra(self):
        self._store(SR.LEGACY_MODEL, 600)
        state = self.state()
        self.assertTrue(G.sol_backoff_status(
            state, model=SR.model_key(SR.LEGACY_MODEL))["active"])
        self.assertFalse(G.sol_backoff_status(
            state, model=SR.model_key())["active"])
        self.assertFalse(G.sol_backoff_status(
            state, model=SR.model_key(),
            include_legacy=True)["active"])
        self.assertFalse(G.sol_backoff_status(
            state, model=SR.model_key())["active"])

    def test_astra_window_does_not_block_sol(self):
        self._store(SR.DEFAULT_MODEL, 600)
        state = self.state()
        self.assertTrue(G.sol_backoff_status(
            state, model=SR.model_key())["active"])
        self.assertFalse(G.sol_backoff_status(
            state, model=SR.model_key(SR.LEGACY_MODEL),
            include_legacy=True)["active"])

    def test_legacy_flat_window_is_readable_but_does_not_block_astra(self):
        state = self.state()
        state[G.SOL_BACKOFF_KEY] = G._sol_backoff_from_failure({
            "error_class": SR.UPSTREAM_OVERLOADED, "retry_after": 600,
            "http_status": 502, "call_id": "SClegacy"})
        G.save_state(self.state_path, state)
        state = self.state()
        # 旧单例仍可按原有语义只读（v9.8 调用方），但不继承给 Astra。
        self.assertTrue(G.sol_backoff_status(state)["active"])
        self.assertFalse(G.sol_backoff_status(
            state, model=SR.model_key())["active"])

    def test_retry_after_is_independent_per_model(self):
        self._store(SR.DEFAULT_MODEL, 900)
        self._store(SR.LEGACY_MODEL, 120)
        state = self.state()
        astra = G.sol_backoff_status(state, model=SR.model_key())
        sol = G.sol_backoff_status(
            state, model=SR.model_key(SR.LEGACY_MODEL))
        self.assertGreater(astra["remaining_seconds"],
                           sol["remaining_seconds"] + 600)

    def test_success_clears_only_its_own_model(self):
        self._store(SR.DEFAULT_MODEL, 600)
        self._store(SR.LEGACY_MODEL, 600)
        G.record_sol_call(self.state_path, {
            "call_id": "SCok", "mode": "CONSULT", "summary": "ok",
            "model": SR.DEFAULT_MODEL, "model_key": SR.model_key(),
            "provider": SR.PROVIDER, "materials_sha256": "m" * 64,
            "completed_at": G.now_iso()},
            mode="CONSULT", materials_sha256="m" * 64)
        state = self.state()
        self.assertFalse(G.sol_backoff_status(
            state, model=SR.model_key())["active"])
        self.assertTrue(G.sol_backoff_status(
            state, model=SR.model_key(SR.LEGACY_MODEL))["active"])

    def test_failure_record_carries_model_identity(self):
        record = G.record_sol_call_failure(
            self.state_path, mode="CONSULT", model=SR.DEFAULT_MODEL,
            provider=SR.PROVIDER, error_code="sol_upstream_overloaded",
            detail="HTTP 502", http_status=502, retry_after=300,
            error_class=SR.UPSTREAM_OVERLOADED)
        self.assertEqual(record["model"], SR.DEFAULT_MODEL)
        self.assertEqual(record["model_key"], SR.model_key())
        self.assertEqual(record["provider"], SR.PROVIDER)
        state = self.state()
        self.assertNotIn(G.SOL_BACKOFF_KEY, state)
        self.assertTrue(G.sol_backoff_status(
            state, model=SR.model_key())["active"])

    def test_auth_failure_creates_no_window(self):
        G.record_sol_call_failure(
            self.state_path, mode="CONSULT", model=SR.DEFAULT_MODEL,
            provider=SR.PROVIDER, error_code="sol_upstream_auth",
            detail="401", http_status=401,
            error_class=SR.UPSTREAM_AUTH)
        state = self.state()
        self.assertFalse(G.sol_backoff_status(
            state, model=SR.model_key())["active"])
        self.assertNotIn(G.SOL_BACKOFF_KEY, state)

    def test_expired_sol_window_never_freezes_astra(self):
        state = self.state()
        window = self._window(SR.LEGACY_MODEL, 600, SR.UPSTREAM_OVERLOADED)
        window["until"] = (datetime.now().astimezone()
                           - timedelta(seconds=1)).isoformat(timespec="seconds")
        G.store_model_backoff(state, window)
        G.save_state(self.state_path, state)
        self.assertFalse(G.sol_backoff_status(
            self.state(), model=SR.model_key())["active"])


# ── D. 响应完整性 ──────────────────────────────────────────────────────────
class ResponseIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="目标", contract=F.CONTRACT,
                      risk="medium", task_id="astra-integrity")
        self.materials = os.path.join(self.dir, "materials.md")
        with open(self.materials, "w", encoding="utf-8") as fh:
            fh.write("材料正文\n")

    def test_incomplete_is_failure_not_success(self):
        error = SR.SolResponseError(
            "sol_response_incomplete",
            "响应未完成：incomplete/max_output_tokens",
            error_class=SR.RESPONSE_INCOMPLETE, response_received=True)
        error.request_attempted = True
        error.send_observed = "response_received"
        with mock.patch.object(SR, "run_consult", side_effect=error):
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = CD.main(["sol-consult", "--state", self.state_path,
                                "--materials", self.materials,
                                "--question", "本次要判断什么", "--json"])
        self.assertEqual(code, 0)
        state = G.load_state(self.state_path)
        self.assertEqual(state["sol_calls"][-1]["status"], "failed")
        self.assertEqual(state["sol_calls"][-1]["error_class"],
                         SR.RESPONSE_INCOMPLETE)
        self.assertEqual([item for item in state["sol_calls"]
                          if item["status"] == "completed"], [])

    def test_output_budget_is_not_lowered(self):
        self.assertEqual(SR.DEFAULT_MAX_OUTPUT_TOKENS, 24000)
        self.assertEqual(SR.build_payload("材料")["max_output_tokens"], 24000)


if __name__ == "__main__":
    unittest.main()

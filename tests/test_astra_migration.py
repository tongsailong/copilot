#!/usr/bin/env python3
"""v9.9：认知默认模型迁移、调用效率、模型级退避隔离与响应完整性。

覆盖四组要求：
- A 迁移：默认入口是 `gpt-6.2-sol` + high + standard + 零工具（v9.14.0 回切）；
  `gpt-6.2-astra` 只作为注册表内的单次兜底目标与显式可选模型（v9.15.0），
  历史模型名永不参与回退，历史结论不被当成新模型的响应。
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
import cognition_call as CC
import cognition_schedule as SCH


def think_text() -> str:
    return ("## 判断\n先补测试\n\n## 方案\n- 先测后改\n\n"
            "## 风险\n- 回归不足\n")


def think_result(mode: str = "CONSULT", model: str = "") -> dict:
    """通道替身：结构与真实 run_cognition 一致，不联网。"""
    resolved = model or CC.DEFAULT_MODEL
    return {
        "schema": CC.THINK_SCHEMA, "mode": mode,
        "model": resolved, "model_resolved": resolved,
        "provider": CC.PROVIDER,
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
# v9.14.0 起默认是 SOL，Astra 转为显式可选；更早的历史模型名一律拒绝。
HISTORICAL_MODEL = "gpt-6.1-sol"
HISTORICAL_KEY = "codex:gpt-6.1-sol"

class DefaultModelMigrationTests(unittest.TestCase):
    def test_payload_defaults_to_sol_high_zero_tool(self):
        with mock.patch.dict(os.environ, {CC.MODEL_ENV: ""}):
            payload = CC.build_payload("材料")
        self.assertEqual(payload["model"], "gpt-6.2-sol")
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(payload["tool_choice"], "none")
        self.assertNotIn("tools", payload)
        self.assertNotIn("functions", payload)
        self.assertEqual(CC.DEFAULT_MODEL, "gpt-6.2-sol")
        self.assertEqual(CC.MODEL, CC.DEFAULT_MODEL)
        self.assertEqual(CC.PROVIDER, "codex")

    def test_every_mode_defaults_to_sol(self):
        for mode in CC.CALL_MODES:
            with self.subTest(mode=mode):
                with mock.patch.dict(os.environ, {CC.MODEL_ENV: ""}):
                    payload = CC.build_payload("材料", mode=mode)
                self.assertEqual(payload["model"], "gpt-6.2-sol")
                self.assertEqual(payload["reasoning"], {"effort": "high"})
                self.assertEqual(payload["tool_choice"], "none")

    def test_default_and_explicit_model_registry(self):
        with mock.patch.dict(os.environ, {CC.MODEL_ENV: ""}):
            self.assertEqual(CC.resolve_model(""), CC.DEFAULT_MODEL)
            self.assertEqual(CC.model_key(), "codex:gpt-6.2-sol")
        self.assertEqual(CC.resolve_model(CC.DEFAULT_MODEL), CC.DEFAULT_MODEL)
        # Astra 是注册表内的显式可选模型，也是唯一允许的单次兜底目标；
        # 只有默认模型请求失败且故障可归因时才由它接管。
        self.assertEqual(CC.resolve_model("gpt-6.2-astra"), "gpt-6.2-astra")
        for rejected in (HISTORICAL_MODEL, "gpt-6.2-sol-x", "gpt-6.0-fake"):
            with self.subTest(model=rejected):
                with self.assertRaises(CC.CognitionUsageError) as ctx:
                    CC.resolve_model(rejected)
                self.assertEqual(ctx.exception.code,
                                 "cognition_model_unsupported")

    def test_legacy_cognition_record_is_not_reused_as_current_model(self):
        state = {"cognition_calls": [
            {"status": "completed", "mode": "CONSULT",
             "model": HISTORICAL_MODEL, "materials_sha256": "a" * 64},
            # v8/v9.8 记录没有模型归属：任何模型都不把它当作新响应复用。
            {"status": "completed", "mode": "CONSULT",
             "materials_sha256": "b" * 64},
        ]}
        for digest in ("a" * 64, "b" * 64):
            with self.subTest(digest=digest[:4]):
                self.assertEqual(CC.single_round_decision(
                    state, digest, mode="CONSULT")["action"], "call")


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
            return CD.main(["cognition-consult", "--state", self.state_path,
                            "--materials", self.materials,
                            "--question", "本次要判断什么", *extra])

    def test_default_call_records_astra_and_provider(self):
        with mock.patch.object(CC, "run_consult",
                               return_value=think_result()) as run:
            self.assertEqual(self._consult(), 0)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs.get("model"),
                         CC.DEFAULT_MODEL)
        record = self.state()["cognition_calls"][-1]
        self.assertEqual(record["model"], CC.DEFAULT_MODEL)
        self.assertEqual(record["model_key"], CC.model_key())
        self.assertEqual(record["provider"], CC.PROVIDER)

    def test_sol_failure_falls_back_once_to_registered_astra_only(self):
        error = CC.CognitionOverloadedError(
            "cognition_upstream_overloaded", "上游瞬时容量不足",
            http_status=502, retry_after=300,
            upstream_code="server_is_overloaded",
            error_class=CC.UPSTREAM_OVERLOADED, response_received=True)
        error.request_attempted = True
        error.send_observed = "response_received"
        seen: list[str] = []

        def boom(*_args, **kwargs):
            seen.append(str(kwargs.get("model") or ""))
            raise error

        with mock.patch.object(CC, "run_consult", side_effect=boom):
            self.assertEqual(self._consult(), 0)
        # 只有两次尝试：默认 SOL → 注册表内的 Astra；历史模型永不参与。
        self.assertEqual(seen, [CC.DEFAULT_MODEL, CC.FALLBACK_MODEL])
        records = self.state()["cognition_calls"]
        self.assertEqual(len(records), 1)
        record = records[-1]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["requested_model"], CC.DEFAULT_MODEL)
        self.assertEqual(record["actual_model"], CC.FALLBACK_MODEL)
        self.assertTrue(record["fallback_used"])
        self.assertTrue(record["fallback_exhausted"])
        self.assertEqual(record["attempt_count"], 2)
        self.assertEqual([item.get("model") for item in record["attempt_trail"]],
                         [CC.DEFAULT_MODEL, CC.FALLBACK_MODEL])
        self.assertNotIn(HISTORICAL_MODEL,
                         [item.get("model") for item in records])

    def test_model_flag_is_gone_and_no_route_can_switch_model(self):
        for value in (HISTORICAL_MODEL, "gpt-6.0-fake"):
            with self.subTest(value=value):
                with mock.patch.object(CC, "run_consult",
                                       side_effect=AssertionError("不应请求")):
                    with self.assertRaises(SystemExit):
                        with contextlib.redirect_stderr(io.StringIO()):
                            CD.main(["cognition-consult",
                                     "--state", self.state_path,
                                     "--materials", self.materials,
                                     "--question", "本次要判断什么",
                                     "--model", value])

    def test_legacy_sol_cli_entries_are_gone(self):
        # v9.10.0：旧 `sol-*` 子命令与 `--sol` 别名都没有真实调用方，
        # 已整体移除；解析阶段即拒绝，不会形成第二条认知入口。
        parser = CD.build_parser()
        for argv in (["sol-consult", "--state", "s.json"],
                     ["sol-accept", "--state", "s.json"],
                     ["judgment", "--state", "s.json", "--status", "pass",
                      "--summary", "判断", "--sol", "auto"]):
            with self.subTest(argv=argv[0]):
                with self.assertRaises(SystemExit):
                    with contextlib.redirect_stderr(io.StringIO()):
                        parser.parse_args(argv)
        help_text = io.StringIO()
        with contextlib.redirect_stdout(help_text):
            with self.assertRaises(SystemExit):
                parser.parse_args(["--help"])
        self.assertNotIn("sol-", help_text.getvalue())
        self.assertNotIn("--sol", help_text.getvalue())


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
                      # 方案类任务＝真实的认知价值信号：默认 standard/medium 不再
                      # 产生任何义务（P0-3），这里要验证的是“显式 auto 会执行”。
                      task_kind="architecture")
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
        env = {"COPILOT_COGNITION": "auto", "COPILOT_COGNITION_AUTO": "on"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(CC, "run_consult",
                                  side_effect=AssertionError("不应请求")), \
                mock.patch.object(CC, "run_plan",
                                  side_effect=AssertionError("不应请求")):
            self.assertEqual(self._judgment(), 0)
        self.assertEqual([item for item in self.state().get("cognition_calls") or []
                          if item.get("status") == "completed"], [])

    def test_explicit_auto_still_executes_one_obligation(self):
        with mock.patch.dict(os.environ, {"COPILOT_COGNITION_AUTO": "on"}), \
                mock.patch.object(CC, "run_consult",
                                  return_value=think_result()) as consult, \
                mock.patch.object(CC, "run_plan",
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
                self.assertEqual(args.cognition, "")
                self.assertEqual(args.cognition_default, "advise")
        args = parser.parse_args(["judgment", "--state", self.state_path,
                                  "--status", "pass", "--summary", "判断",
                                  "--cognition", "auto"])
        self.assertEqual(args.cognition, "auto")


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
            return CD.main(["cognition-consult", "--state", self.state_path,
                            "--materials", self.materials,
                            "--question", "本次要判断什么"])

    def test_same_context_is_reused_once_and_counted(self):
        with mock.patch.object(CC, "run_consult",
                               return_value=think_result()) as run:
            self.assertEqual(self._consult(), 0)
            self.assertEqual(self._consult(), 0)
        self.assertEqual(run.call_count, 1)
        state = self.state()
        self.assertEqual(len(state["cognition_calls"]), 1)
        self.assertEqual(len(state.get("cognition_reuses") or []), 1)
        stats = SCH.task_stats(state)
        self.assertEqual(stats["reuse"]["hits"], 1)
        self.assertEqual(stats["reuse"]["by_model"],
                         [{"model_key": CC.model_key(), "hits": 1}])
        row = stats["by_model"][0]
        self.assertEqual(row["model_key"], CC.model_key())
        self.assertEqual(row["repeat_calls"], 0)
        self.assertEqual(row["input_tokens"], 30)
        self.assertEqual(row["output_tokens"], 12)

    def test_reuse_key_includes_task_id(self):
        state = self.state()
        other = {**state, "task_id": "task-other"}
        self.assertNotEqual(
            CC.review_subject_sha256(state, "材料", mode="CONSULT"),
            CC.review_subject_sha256(other, "材料", mode="CONSULT"))

    def test_new_evidence_invalidates_reuse(self):
        state = self.state()
        before = CC.review_subject_sha256(state, "材料", mode="CONSULT")
        state = {**state, "evidence": [{"id": "E1", "sha256": "a" * 64,
                                        "kind": "text"}]}
        after = CC.review_subject_sha256(state, "材料", mode="CONSULT")
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
        window = G._cognition_backoff_from_failure({
            "error_class": error_class, "retry_after": seconds,
            "http_status": 502, "call_id": "SCfail",
            "model": model, "provider": CC.PROVIDER,
            "model_key": f"{CC.PROVIDER}:{model}"})
        return window

    def _store(self, model: str, seconds: float,
               error_class: str = CC.UPSTREAM_OVERLOADED) -> None:
        state = self.state()
        G.store_model_backoff(state, self._window(model, seconds, error_class))
        G.save_state(self.state_path, state)

    def test_historical_model_window_does_not_block_astra(self):
        self._store(HISTORICAL_MODEL, 600)
        state = self.state()
        self.assertTrue(G.cognition_backoff_status(
            state, model=HISTORICAL_KEY)["active"])
        self.assertFalse(G.cognition_backoff_status(
            state, model=CC.model_key())["active"])
        self.assertFalse(G.cognition_backoff_status(
            state, model=CC.model_key(),
            include_legacy=True)["active"])
        self.assertFalse(G.cognition_backoff_status(
            state, model=CC.model_key())["active"])

    def test_astra_window_does_not_block_historical_model(self):
        self._store(CC.DEFAULT_MODEL, 600)
        state = self.state()
        self.assertTrue(G.cognition_backoff_status(
            state, model=CC.model_key())["active"])
        self.assertFalse(G.cognition_backoff_status(
            state, model=HISTORICAL_KEY,
            include_legacy=True)["active"])

    def test_legacy_flat_window_is_readable_but_does_not_block_astra(self):
        state = self.state()
        state[G.LEGACY_BACKOFF_KEY] = G._cognition_backoff_from_failure({
            "error_class": CC.UPSTREAM_OVERLOADED, "retry_after": 600,
            "http_status": 502, "call_id": "SClegacy"})
        G.save_state(self.state_path, state)
        state = self.state()
        # 旧单例仍可按原有语义只读（v9.8 调用方），但不继承给 Astra。
        self.assertTrue(G.cognition_backoff_status(state)["active"])
        self.assertFalse(G.cognition_backoff_status(
            state, model=CC.model_key())["active"])

    def test_retry_after_is_independent_per_model(self):
        self._store(CC.DEFAULT_MODEL, 900)
        self._store(HISTORICAL_MODEL, 120)
        state = self.state()
        astra = G.cognition_backoff_status(state, model=CC.model_key())
        historical = G.cognition_backoff_status(
            state, model=HISTORICAL_KEY)
        self.assertGreater(astra["remaining_seconds"],
                           historical["remaining_seconds"] + 600)

    def test_success_clears_only_its_own_model(self):
        self._store(CC.DEFAULT_MODEL, 600)
        self._store(HISTORICAL_MODEL, 600)
        G.record_cognition_call(self.state_path, {
            "call_id": "SCok", "mode": "CONSULT", "summary": "ok",
            "model": CC.DEFAULT_MODEL, "model_key": CC.model_key(),
            "provider": CC.PROVIDER, "materials_sha256": "m" * 64,
            "completed_at": G.now_iso()},
            mode="CONSULT", materials_sha256="m" * 64)
        state = self.state()
        self.assertFalse(G.cognition_backoff_status(
            state, model=CC.model_key())["active"])
        self.assertTrue(G.cognition_backoff_status(
            state, model=HISTORICAL_KEY)["active"])

    def test_failure_record_carries_model_identity(self):
        record = G.record_cognition_call_failure(
            self.state_path, mode="CONSULT", model=CC.DEFAULT_MODEL,
            provider=CC.PROVIDER, error_code="cognition_upstream_overloaded",
            detail="HTTP 502", http_status=502, retry_after=300,
            error_class=CC.UPSTREAM_OVERLOADED)
        self.assertEqual(record["model"], CC.DEFAULT_MODEL)
        self.assertEqual(record["model_key"], CC.model_key())
        self.assertEqual(record["provider"], CC.PROVIDER)
        state = self.state()
        self.assertNotIn(G.LEGACY_BACKOFF_KEY, state)
        self.assertTrue(G.cognition_backoff_status(
            state, model=CC.model_key())["active"])

    def test_auth_failure_creates_no_window(self):
        G.record_cognition_call_failure(
            self.state_path, mode="CONSULT", model=CC.DEFAULT_MODEL,
            provider=CC.PROVIDER, error_code="cognition_upstream_auth",
            detail="401", http_status=401,
            error_class=CC.UPSTREAM_AUTH)
        state = self.state()
        self.assertFalse(G.cognition_backoff_status(
            state, model=CC.model_key())["active"])
        self.assertNotIn(G.LEGACY_BACKOFF_KEY, state)

    def test_expired_cognition_window_never_freezes_astra(self):
        state = self.state()
        window = self._window(HISTORICAL_MODEL, 600, CC.UPSTREAM_OVERLOADED)
        window["until"] = (datetime.now().astimezone()
                           - timedelta(seconds=1)).isoformat(timespec="seconds")
        G.store_model_backoff(state, window)
        G.save_state(self.state_path, state)
        self.assertFalse(G.cognition_backoff_status(
            self.state(), model=CC.model_key())["active"])


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
        error = CC.CognitionResponseError(
            "cognition_response_incomplete",
            "响应未完成：incomplete/max_output_tokens",
            error_class=CC.RESPONSE_INCOMPLETE, response_received=True)
        error.request_attempted = True
        error.send_observed = "response_received"
        with mock.patch.object(CC, "run_consult", side_effect=error):
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = CD.main(["cognition-consult", "--state", self.state_path,
                                "--materials", self.materials,
                                "--question", "本次要判断什么", "--json"])
        self.assertEqual(code, 0)
        state = G.load_state(self.state_path)
        self.assertEqual(state["cognition_calls"][-1]["status"], "failed")
        self.assertEqual(state["cognition_calls"][-1]["error_class"],
                         CC.RESPONSE_INCOMPLETE)
        self.assertEqual([item for item in state["cognition_calls"]
                          if item["status"] == "completed"], [])

    def test_output_budget_is_not_lowered(self):
        self.assertEqual(CC.DEFAULT_MAX_OUTPUT_TOKENS, 24000)
        self.assertEqual(CC.build_payload("材料")["max_output_tokens"], 24000)


if __name__ == "__main__":
    unittest.main()

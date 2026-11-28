#!/usr/bin/env python3
"""v9.15：默认 SOL 主导认知 + Astra 单次自动兜底。

覆盖范围：
- A 调度：默认模型成功时只有一次请求，不触发兜底；
- B 故障分类：允许回退的模型级/传输级故障 → 恰好一次 Astra 接管；
  认证、账户额度、请求构造、回答质量问题 → 不回退；
- C 预算：最多两次尝试，Astra 也失败时如实记录双失败，不再找第三个模型；
- D 隔离：显式选择 Astra 时不反向回退；下一任务仍从 SOL 开始；
  退避窗口按模型隔离，Astra 自己的窗口内不发送兜底请求；
- E 可观测：requested_model / actual_model / fallback_used / fallback_reason /
  primary_error / attempt_count / attempt_trail / 两次 trace 都可从记录读出；
- F 复用：兜底结论可被同一请求模型复用，但输出如实说明实际产出模型。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import v6_support as F

import copilot_dispatch as CD
import governance as G
import cognition_call as CC
import cognition_schedule as SCH


def think_text() -> str:
    return ("## 判断\n先补测试\n\n## 方案\n- 先测后改\n\n"
            "## 风险\n- 回归不足\n")


def think_result(model: str = "", *, request_id: str = "req-1",
                 trace_id: str = "trace-1") -> dict:
    """通道替身：结构与真实 run_cognition 一致，不联网。"""
    resolved = model or CC.DEFAULT_MODEL
    return {
        "schema": CC.THINK_SCHEMA, "mode": "CONSULT",
        "model": resolved, "model_resolved": resolved,
        "provider": CC.PROVIDER,
        "reasoning_effort": "high", "reasoning_mode": "standard",
        "reasoning_resolved": {"effort": "high"}, "reasoning_verified": True,
        "zero_tool": True, "attempts": 1, "request_id": request_id,
        "trace_id": trace_id, "response_id": "resp-" + trace_id,
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


def _error(cls, code: str, *, http_status: int | None = None,
           error_class: str = "", upstream_code: str = "",
           retry_after: int | None = None, detail: str = "",
           attempted: bool = True, observed: str = "response_received"):
    error = cls(code, detail or code, http_status=http_status,
                retry_after=retry_after, upstream_code=upstream_code,
                error_class=error_class,
                response_received=http_status is not None)
    error.request_attempted = attempted
    error.send_observed = observed if attempted else "not_attempted"
    # 真实失败都带请求/追踪 ID；两次尝试的 ID 必须能分别读出。
    error.request_id = "req-" + code
    error.trace_id = "trace-" + str(http_status or code)
    return error


def overload_error(**kwargs):
    return _error(CC.CognitionOverloadedError, "cognition_upstream_overloaded",
                  http_status=502, error_class=CC.UPSTREAM_OVERLOADED,
                  upstream_code="server_is_overloaded", retry_after=300,
                  detail="上游瞬时容量不足", **kwargs)


def server_error(**kwargs):
    return _error(CC.CognitionUpstreamError, "cognition_upstream_server_error",
                  http_status=503, error_class=CC.UPSTREAM_SERVER,
                  upstream_code="server_error", detail="上游 503", **kwargs)


def timeout_error(**kwargs):
    return _error(CC.CognitionTimeoutError, "cognition_client_timeout",
                  error_class=CC.CLIENT_TIMEOUT, detail="等待上游超时",
                  observed="unknown", **kwargs)


def model_rate_limit_error(**kwargs):
    return _error(CC.CognitionQuotaError, "cognition_quota",
                  http_status=429, error_class=CC.UPSTREAM_RATE_LIMIT,
                  upstream_code="model_cooldown", detail="模型级冷却", **kwargs)


def account_quota_error(**kwargs):
    return _error(CC.CognitionQuotaError, "cognition_quota",
                  http_status=429, error_class=CC.UPSTREAM_RATE_LIMIT,
                  upstream_code="insufficient_quota", detail="账户额度不足",
                  **kwargs)


def auth_error(status: int = 401, **kwargs):
    return _error(CC.CognitionAuthError, "cognition_upstream_auth",
                  http_status=status, error_class=CC.UPSTREAM_AUTH,
                  detail="认证失败", **kwargs)


def param_error(**kwargs):
    return _error(CC.CognitionUpstreamClientError,
                  "cognition_upstream_client_error", http_status=400,
                  error_class=CC.UPSTREAM_CLIENT, detail="请求参数被拒绝",
                  **kwargs)


def quality_error(**kwargs):
    """回答不可消费（解析/质量问题）：没有 error_class，属于质量而非通道故障。"""
    error = CC.CognitionResponseError(
        "cognition_reply_unusable", "回复不是可消费结构")
    error.request_attempted = True
    error.send_observed = "response_received"
    return error


def identity_error(**kwargs):
    return _error(CC.CognitionIdentityError,
                  "cognition_model_identity_mismatch",
                  detail="上游回显模型与请求不一致")


class FallbackBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="交付可复核结果",
                      contract=F.CONTRACT, risk="medium",
                      task_id="fallback", workspace=self.dir,
                      task_kind="standard")
        self.materials = os.path.join(self.dir, "materials.md")
        with open(self.materials, "w", encoding="utf-8") as fh:
            fh.write("材料正文\n")
        self.calls: list[str] = []

    def state(self) -> dict:
        return G.load_state(self.state_path)

    def _consult(self, *extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = CD.main(["cognition-consult", "--state", self.state_path,
                            "--materials", self.materials,
                            "--question", "本次要判断什么", *extra])
        return code, out.getvalue(), err.getvalue()

    def run_table(self, table: dict, *extra: str) -> tuple[int, str, str]:
        """按模型注入成功/失败：table[model] 是结果或异常，缺省成功。"""

        def fake(_prompt, **kwargs):
            model = str(kwargs.get("model") or CC.DEFAULT_MODEL)
            self.calls.append(model)
            outcome = table.get(model)
            if isinstance(outcome, Exception):
                raise outcome
            if isinstance(outcome, dict):
                return outcome
            return think_result(model, request_id="req-" + model,
                                trace_id="trace-" + model)

        with mock.patch.object(CC, "run_consult", side_effect=fake):
            return self._consult(*extra)

    def records(self) -> list[dict]:
        return [item for item in self.state().get("cognition_calls") or []
                if isinstance(item, dict)]

    def last_record(self) -> dict:
        return self.records()[-1]


class SingleCallTests(FallbackBase):
    def test_default_success_uses_one_request_and_no_fallback(self):
        code, out, _err = self.run_table({}, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [CC.DEFAULT_MODEL])
        record = self.last_record()
        self.assertEqual(record["status"], "completed")
        self.assertFalse(record["fallback_used"])
        self.assertEqual(record["requested_model"], CC.DEFAULT_MODEL)
        self.assertEqual(record["actual_model"], CC.DEFAULT_MODEL)
        self.assertEqual(record["model"], CC.DEFAULT_MODEL)
        self.assertEqual(record["attempt_count"], 1)
        self.assertEqual([row["status"] for row in record["attempt_trail"]],
                         ["completed"])
        self.assertEqual(json.loads(out)["fallback_used"], False)


class AllowedFallbackTests(FallbackBase):
    def _assert_fallback_succeeded(self, first_error) -> dict:
        code, out, err = self.run_table(
            {CC.DEFAULT_MODEL: first_error}, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [CC.DEFAULT_MODEL, CC.FALLBACK_MODEL])
        record = self.last_record()
        self.assertEqual(record["status"], "completed")
        self.assertTrue(record["fallback_used"])
        self.assertTrue(record["fallback_reason"].startswith("allowed:"))
        self.assertEqual(record["requested_model"], CC.DEFAULT_MODEL)
        self.assertEqual(record["actual_model"], CC.FALLBACK_MODEL)
        # `model` 始终是真实产出模型：Astra 的结果不得冒充 SOL。
        self.assertEqual(record["model"], CC.FALLBACK_MODEL)
        self.assertEqual(record["model_key"], CC.model_key(CC.FALLBACK_MODEL))
        self.assertEqual(record["attempt_count"], 2)
        self.assertEqual([row["status"] for row in record["attempt_trail"]],
                         ["failed", "completed"])
        self.assertEqual([row["model"] for row in record["attempt_trail"]],
                         [CC.DEFAULT_MODEL, CC.FALLBACK_MODEL])
        self.assertEqual(record["primary_error"]["model"], CC.DEFAULT_MODEL)
        self.assertEqual(record["primary_error"]["error_class"],
                         first_error.error_class)
        self.assertIsNone(record["fallback_error"])
        # 两次尝试的用量与耗时都保留（不把兜底成本藏起来）。
        self.assertEqual(record["usage_total"]["total_tokens"], 42)
        self.assertIsNotNone(record["latency_total_ms"])
        self.assertIn("接管", err)
        payload = json.loads(out)
        self.assertTrue(payload["fallback_used"])
        return record

    def test_sol_502_astra_takes_over(self):
        self._assert_fallback_succeeded(overload_error())

    def test_sol_503_astra_takes_over(self):
        self._assert_fallback_succeeded(server_error())

    def test_sol_client_timeout_astra_takes_over(self):
        record = self._assert_fallback_succeeded(timeout_error())
        # 超时无法证明上游是否已生成回复：保持 unknown，不宣称“无成本”。
        self.assertEqual(record["primary_error"]["send_observed"], "unknown")

    def test_sol_model_level_429_astra_takes_over(self):
        record = self._assert_fallback_succeeded(model_rate_limit_error())
        self.assertIn("model_cooldown", record["fallback_reason"])

    def test_model_identity_mismatch_is_fallback_eligible(self):
        self._assert_fallback_succeeded(identity_error())


class DeniedFallbackTests(FallbackBase):
    def _assert_no_fallback(self, error, reason_fragment: str) -> dict:
        code, out, _err = self.run_table({CC.DEFAULT_MODEL: error}, "--json")
        # CONSULT 失败只降级：退出码仍为 0，但记录必须如实。
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [CC.DEFAULT_MODEL])
        record = self.last_record()
        self.assertEqual(record["status"], "failed")
        self.assertFalse(record["fallback_used"])
        self.assertIn(reason_fragment, record["fallback_denied"])
        self.assertEqual(record["attempt_count"], 1)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "unavailable")
        self.assertFalse(payload["fallback_used"])
        self.assertIn(reason_fragment, payload["fallback_denied"])
        return record

    def test_401_auth_does_not_fall_back(self):
        self._assert_no_fallback(auth_error(401), "denied:upstream_auth")

    def test_403_auth_does_not_fall_back(self):
        self._assert_no_fallback(auth_error(403), "denied:upstream_auth")

    def test_account_quota_does_not_fall_back(self):
        self._assert_no_fallback(account_quota_error(),
                                 "denied:account-quota-or-unknown-rate-limit")

    def test_request_parameter_error_does_not_fall_back(self):
        self._assert_no_fallback(param_error(), "denied:upstream_client_error")

    def test_reply_quality_problem_does_not_fall_back(self):
        self._assert_no_fallback(quality_error(), "denied:reply-quality-or-request")

    def test_account_quota_wrapped_in_5xx_does_not_fall_back(self):
        # 上游把账户额度错误包进 502：分类必须先认账户问题，不能当瞬时容量回退。
        error_class = CC.classify_upstream_failure(
            502, {"error": {"code": "insufficient_quota",
                            "message": "You exceeded your current quota"}})
        self.assertEqual(error_class, CC.UPSTREAM_RATE_LIMIT)
        error = _error(CC.CognitionQuotaError, "cognition_quota",
                       http_status=502, error_class=error_class,
                       upstream_code="insufficient_quota", detail="上游 502")
        self._assert_no_fallback(
            error, "denied:account-quota-or-unknown-rate-limit")

    def test_auth_wrapped_in_5xx_does_not_fall_back(self):
        error_class = CC.classify_upstream_failure(
            502, {"error": {"code": "invalid_api_key",
                            "message": "Incorrect API key provided"}})
        self.assertEqual(error_class, CC.UPSTREAM_AUTH)
        error = _error(CC.CognitionAuthError, "cognition_upstream_auth",
                       http_status=502, error_class=error_class,
                       detail="上游 502")
        self._assert_no_fallback(error, "denied:upstream_auth")

    def test_safety_refusal_wrapped_in_5xx_does_not_fall_back(self):
        # 安全终止包在 502 外壳里：换模型只会把同一拒绝重复一次。
        error_class = CC.classify_upstream_failure(
            502, {"error": {"code": "content_policy_violation",
                            "message": "request blocked by safety system"}})
        self.assertEqual(error_class, CC.UPSTREAM_CLIENT)
        error = _error(CC.CognitionUpstreamClientError,
                       "cognition_upstream_client_error", http_status=502,
                       error_class=error_class, detail="上游 502")
        self._assert_no_fallback(error, "denied:upstream_client_error")

    def test_user_cancel_does_not_fall_back(self):
        error_class = CC.classify_upstream_failure(
            502, {"error": {"code": "request_canceled"}})
        self.assertEqual(error_class, CC.UPSTREAM_CLIENT)
        error = _error(CC.CognitionUpstreamClientError,
                       "cognition_upstream_client_error", http_status=502,
                       error_class=error_class, detail="请求被取消")
        self._assert_no_fallback(error, "denied:upstream_client_error")


class ClassifierTests(unittest.TestCase):
    """分类顺序：账户/认证优先于 5xx，真 5xx 仍允许回退。"""

    def test_account_and_auth_codes_win_over_5xx_shell(self):
        cases = [
            ({"error": {"code": "insufficient_quota"}}, CC.UPSTREAM_RATE_LIMIT),
            ({"error": {"code": "usage_limit_reached"}}, CC.UPSTREAM_RATE_LIMIT),
            ({"error": {"code": "billing_hard_limit_reached"}},
             CC.UPSTREAM_RATE_LIMIT),
            ({"error": {"code": "invalid_api_key"}}, CC.UPSTREAM_AUTH),
            ({"error": {"code": "account_deactivated"}}, CC.UPSTREAM_AUTH),
            ({"error": {"code": "content_policy_violation"}},
             CC.UPSTREAM_CLIENT),
            ({"error": {"code": "request_canceled"}}, CC.UPSTREAM_CLIENT),
        ]
        for payload, expected in cases:
            self.assertEqual(CC.classify_upstream_failure(502, payload),
                             expected, payload)

    def test_true_transient_errors_stay_fallback_eligible(self):
        for status, payload, expected in (
                (502, {"error": {"code": "server_error"}}, CC.UPSTREAM_SERVER),
                (503, {"error": {"code": "server_is_overloaded"}},
                 CC.UPSTREAM_OVERLOADED),
                (500, {"error": {"message": "internal error"}},
                 CC.UPSTREAM_SERVER),
        ):
            error_class = CC.classify_upstream_failure(status, payload)
            self.assertEqual(error_class, expected)
            self.assertIn(error_class, CC.FALLBACK_ERROR_CLASSES)


class BothModelsFailTests(FallbackBase):
    def test_astra_failure_stops_with_exhausted_record(self):
        code, out, _err = self.run_table(
            {CC.DEFAULT_MODEL: overload_error(),
             CC.FALLBACK_MODEL: server_error()}, "--json")
        self.assertEqual(code, 0)
        # 最多两次：不会找第三个模型。
        self.assertEqual(self.calls, [CC.DEFAULT_MODEL, CC.FALLBACK_MODEL])
        record = self.last_record()
        self.assertEqual(record["status"], "failed")
        self.assertTrue(record["fallback_used"])
        self.assertTrue(record["fallback_exhausted"])
        self.assertEqual(record["attempt_count"], 2)
        self.assertEqual(record["error_class"], CC.UPSTREAM_SERVER)
        # 两次尝试各自的 trace 都保留，不以最后一次覆盖第一次。
        trail = record["attempt_trail"]
        self.assertEqual([row["status"] for row in trail],
                         ["failed", "failed"])
        self.assertTrue(all(row.get("trace_id") for row in trail))
        self.assertNotEqual(trail[0]["trace_id"], trail[1]["trace_id"])
        payload = json.loads(out)
        self.assertTrue(payload["fallback_exhausted"])
        self.assertEqual(payload["attempt_count"], 2)

    def test_primary_backoff_window_blocks_immediate_repeat(self):
        self.run_table({CC.DEFAULT_MODEL: overload_error(),
                        CC.FALLBACK_MODEL: server_error()})
        # 主模型过载带 Retry-After：窗口只属于 SOL，Astra 的失败窗口另存。
        status = G.cognition_backoff_status(
            self.state(), model=CC.model_key(CC.DEFAULT_MODEL))
        self.assertTrue(status["active"])
        self.assertEqual(status["error_class"], CC.UPSTREAM_OVERLOADED)
        # 窗口内再次调用不发送请求、不新增记录。
        self.calls.clear()
        code, _out, err = self.run_table({})
        self.assertEqual(code, 2)
        self.assertEqual(self.calls, [])
        self.assertIn("退避窗口", err)

    def test_astra_in_own_backoff_window_is_skipped(self):
        self.run_table({CC.DEFAULT_MODEL: overload_error(),
                        CC.FALLBACK_MODEL: overload_error()})
        # 清掉 SOL 的窗口，只保留 Astra 的窗口：本次应因 Astra 退避而跳过兜底。
        state = self.state()
        windows = state[G.MODEL_BACKOFF_KEY]["windows"]
        windows.pop(CC.model_key(CC.DEFAULT_MODEL), None)
        G.save_state(self.state_path, state)
        self.calls.clear()
        code, _out, err = self.run_table({CC.DEFAULT_MODEL: overload_error()})
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [CC.DEFAULT_MODEL])
        last = self.last_record()
        self.assertEqual(last["status"], "failed")
        self.assertEqual(last["fallback_skipped"], "backoff-window")
        self.assertFalse(last["fallback_used"])
        self.assertIn("退避窗口", err)


class ModelIsolationTests(FallbackBase):
    def test_explicit_astra_has_no_reverse_fallback(self):
        with mock.patch.dict(os.environ, {CC.MODEL_ENV: CC.FALLBACK_MODEL}):
            code, _out, _err = self.run_table(
                {CC.FALLBACK_MODEL: overload_error()}, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [CC.FALLBACK_MODEL])
        record = self.last_record()
        self.assertEqual(record["status"], "failed")
        self.assertFalse(record["fallback_used"])
        self.assertEqual(record["fallback_denied"], "unavailable:explicit-model")

    def test_next_task_still_starts_from_default_model(self):
        self.run_table({CC.DEFAULT_MODEL: overload_error()})
        second_dir = os.path.join(self.dir, "second")
        os.makedirs(second_dir, exist_ok=True)
        second_state = os.path.join(second_dir, "state.json")
        G.start_state(second_state, goal="第二个任务", contract=F.CONTRACT,
                      risk="medium", task_id="fallback-2",
                      workspace=second_dir, task_kind="standard")
        materials = os.path.join(second_dir, "materials.md")
        with open(materials, "w", encoding="utf-8") as fh:
            fh.write("材料正文\n")
        self.calls.clear()

        def fake(_prompt, **kwargs):
            self.calls.append(str(kwargs.get("model") or ""))
            return think_result(str(kwargs.get("model") or ""))

        with mock.patch.object(CC, "run_consult", side_effect=fake), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = CD.main(["cognition-consult", "--state", second_state,
                            "--materials", materials,
                            "--question", "本次要判断什么"])
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [CC.DEFAULT_MODEL])

    def test_fallback_can_be_disabled_by_env(self):
        with mock.patch.dict(os.environ, {CC.FALLBACK_ENV: "off"}):
            code, _out, _err = self.run_table(
                {CC.DEFAULT_MODEL: overload_error()}, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [CC.DEFAULT_MODEL])
        record = self.last_record()
        self.assertFalse(record["fallback_used"])
        self.assertEqual(record["fallback_denied"], "unavailable:disabled")


class ReuseIsolationTests(FallbackBase):
    def test_fallback_result_is_not_reused_by_next_default_request(self):
        self.run_table({CC.DEFAULT_MODEL: overload_error()})
        first = self.last_record()
        self.assertTrue(first["fallback_used"])
        self.assertEqual(first["requested_model"], CC.DEFAULT_MODEL)
        self.assertEqual(first["actual_model"], CC.FALLBACK_MODEL)
        self.calls.clear()
        # 同材料同问题：兜底结论不进入可复用缓存。主模型仍在退避窗口内，因此本次
        # 不发送请求，也不把 Astra 的结论当作 SOL 的结论返回。
        code, _out, err = self.run_table({})
        self.assertEqual(code, 2)
        self.assertEqual(self.calls, [])
        self.assertIn("退避窗口", err)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.state().get("cognition_reuses") or [], [])
        # 窗口关闭后重新请求：先尝试主模型，而不是复用兜底结论。
        state = self.state()
        state[G.MODEL_BACKOFF_KEY]["windows"].pop(
            CC.model_key(CC.DEFAULT_MODEL), None)
        G.save_state(self.state_path, state)
        self.calls.clear()
        code, _out, _err = self.run_table({})
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [CC.DEFAULT_MODEL])
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(self.last_record()["actual_model"], CC.DEFAULT_MODEL)

    def test_explicit_astra_does_not_reuse_a_sol_record(self):
        # 结论属于产出它的模型：显式 Astra 请求不得复用 SOL 的结论。
        self.run_table({})
        self.assertEqual(self.last_record()["actual_model"], CC.DEFAULT_MODEL)
        self.calls.clear()
        with mock.patch.dict(os.environ, {CC.MODEL_ENV: CC.FALLBACK_MODEL}):
            code, out, _err = self.run_table(
                {}, "--force", "--trigger", "new-route",
                "--reason", "核验显式模型不复用默认模型的结论")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [CC.FALLBACK_MODEL])
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(self.last_record()["actual_model"], CC.FALLBACK_MODEL)
        self.assertNotIn("复用", out)

    def test_explicit_astra_does_not_reuse_a_fallback_record(self):
        # 兜底记录 requested_model=SOL：显式 Astra 请求要求模型一致，不复用。
        self.run_table({CC.DEFAULT_MODEL: overload_error()})
        self.assertTrue(self.last_record()["fallback_used"])
        self.calls.clear()
        with mock.patch.dict(os.environ, {CC.MODEL_ENV: CC.FALLBACK_MODEL}):
            code, _out, _err = self.run_table(
                {}, "--force", "--trigger", "new-route",
                "--reason", "核验兜底记录不被显式模型复用")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [CC.FALLBACK_MODEL])
        self.assertEqual(len(self.records()), 2)

    def test_astra_success_does_not_clear_primary_backoff_window(self):
        # SOL 过载（带 Retry-After），Astra 接管成功。
        self.run_table({CC.DEFAULT_MODEL: overload_error()})
        record = self.last_record()
        self.assertEqual(record["status"], "completed")
        self.assertTrue(record["fallback_used"])
        # 成功只清 Astra 自己的窗口；SOL 的过载窗口仍然有效（不掩盖主模型故障）。
        self.assertFalse(G.cognition_backoff_status(
            self.state(), model=CC.model_key(CC.FALLBACK_MODEL))["active"])
        self.assertTrue(G.cognition_backoff_status(
            self.state(), model=CC.model_key(CC.DEFAULT_MODEL))["active"])
        self.calls.clear()
        code, _out, err = self.run_table({})
        # 兜底记录不复用：主模型窗口内本次不发送请求，也不返回 Astra 的结论。
        self.assertEqual(code, 2)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.records()), 1)
        self.assertIn("退避窗口", err)


class ReviewModeFallbackTests(FallbackBase):
    def _review(self, table: dict) -> tuple[int, str, str]:
        def fake(_prompt, **kwargs):
            model = str(kwargs.get("model") or CC.DEFAULT_MODEL)
            self.calls.append(model)
            outcome = table.get(model)
            if isinstance(outcome, Exception):
                raise outcome
            result = think_result(model, request_id="req-" + model,
                                  trace_id="trace-" + model)
            result.update({"schema": CC.REVIEW_SCHEMA, "mode": "REVIEW",
                           "verdict": "pass", "findings": [],
                           "findings_count": {"blocking": 0, "major": 0,
                                              "minor": 0, "info": 0},
                           "materials_reviewed": [],
                           "verdict_adjusted": []})
            return result

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(CC, "run_review", side_effect=fake), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = CD.main(["cognition-review", "--state", self.state_path,
                            "--materials", self.materials])
        return code, out.getvalue(), err.getvalue()

    def test_review_fallback_success_is_registered_as_astra_result(self):
        code, out, _err = self._review({CC.DEFAULT_MODEL: overload_error()})
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [CC.DEFAULT_MODEL, CC.FALLBACK_MODEL])
        record = self.last_record()
        self.assertEqual(record["mode"], "REVIEW")
        self.assertTrue(record["fallback_used"])
        self.assertEqual(record["actual_model"], CC.FALLBACK_MODEL)
        self.assertEqual(record["verdict"], "pass")
        self.assertIn("兜底", out)

    def test_review_auth_failure_returns_real_code_without_fallback(self):
        error = auth_error(401)
        code, _out, _err = self._review({CC.DEFAULT_MODEL: error})
        self.assertEqual(code, error.exit_code)
        self.assertEqual(self.calls, [CC.DEFAULT_MODEL])
        record = self.last_record()
        self.assertEqual(record["status"], "failed")
        self.assertFalse(record["fallback_used"])
        self.assertEqual(record["fallback_denied"], "denied:upstream_auth")


class FallbackStatsTests(FallbackBase):
    def test_stats_count_fallback_routes(self):
        self.run_table({CC.DEFAULT_MODEL: overload_error()})
        stats = SCH.stats([self.state()])
        route = f"{CC.DEFAULT_MODEL}->{CC.FALLBACK_MODEL}"
        self.assertEqual(stats["fallback"]["used"], 1)
        self.assertEqual(stats["fallback"]["succeeded"], 1)
        self.assertEqual(stats["fallback"]["attempts"], 2)
        self.assertEqual(stats["fallback"]["by_route"][route], 1)

    def test_denied_fallback_is_counted_separately(self):
        self.run_table({CC.DEFAULT_MODEL: auth_error(401)})
        stats = SCH.stats([self.state()])
        self.assertEqual(stats["fallback"]["used"], 0)
        self.assertEqual(stats["fallback"]["denied"], 1)
        self.assertEqual(stats["fallback"]["attempts"], 1)


class FallbackCacheIsolationTests(FallbackBase):
    """v9.15 缓存隔离专项：任何模型的结果都不得被另一模型的请求复用。"""

    def _clear_model_window(self, model: str) -> None:
        state = self.state()
        state[G.MODEL_BACKOFF_KEY]["windows"].pop(CC.model_key(model), None)
        G.save_state(self.state_path, state)

    def test_sol_success_is_reused_by_next_sol_request(self):
        # 场景 1：正常 SOL 结果按现有规则复用，不重复付费。
        self.run_table({})
        self.assertEqual(self.last_record()["actual_model"], CC.DEFAULT_MODEL)
        self.calls.clear()
        code, out, _err = self.run_table({})
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.records()), 1)
        reuse = (self.state().get("cognition_reuses") or [])[-1]
        self.assertEqual(reuse["requested_model"], CC.DEFAULT_MODEL)
        self.assertEqual(reuse["model"], CC.DEFAULT_MODEL)
        self.assertIn("复用", out)

    def test_astra_takeover_does_not_serve_next_sol_request(self):
        # 场景 2：SOL 失败、Astra 成功后再次请求 SOL → 必须重新尝试 SOL。
        self.run_table({CC.DEFAULT_MODEL: overload_error()})
        self.assertTrue(self.last_record()["fallback_used"])
        self.calls.clear()
        code, _out, err = self.run_table({})
        self.assertEqual(code, 2)  # SOL 退避窗口内不发送请求
        self.assertEqual(self.calls, [])
        self.assertIn("退避窗口", err)
        self.assertEqual(self.state().get("cognition_reuses") or [], [])
        # 窗口关闭后第一次请求仍从 SOL 开始，不复用 Astra 的兜底结论。
        self._clear_model_window(CC.DEFAULT_MODEL)
        self.calls.clear()
        code, _out, _err = self.run_table({})
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [CC.DEFAULT_MODEL])
        self.assertEqual(self.last_record()["actual_model"], CC.DEFAULT_MODEL)

    def test_explicit_astra_does_not_reuse_sol_takeover_record(self):
        # 场景 3：兜底记录 requested_model=SOL → 显式 Astra 请求不复用。
        self.run_table({CC.DEFAULT_MODEL: overload_error()})
        fallback = self.last_record()
        self.assertEqual(fallback["requested_model"], CC.DEFAULT_MODEL)
        self.calls.clear()
        with mock.patch.dict(os.environ, {CC.MODEL_ENV: CC.FALLBACK_MODEL}):
            code, _out, _err = self.run_table({})
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [CC.FALLBACK_MODEL])
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(self.last_record()["actual_model"], CC.FALLBACK_MODEL)
        self.assertFalse(self.last_record()["fallback_used"])
        # 兜底记录仍原样保留在审计流中。
        self.assertEqual(self.records()[0]["call_id"], fallback["call_id"])
        self.assertEqual(self.records()[0]["requested_model"], CC.DEFAULT_MODEL)

    def test_explicit_astra_does_not_reuse_sol_record(self):
        # 场景 4：正常 SOL 结果不得被显式 Astra 请求复用（无需 --force 也应是新调用）。
        self.run_table({})
        self.calls.clear()
        with mock.patch.dict(os.environ, {CC.MODEL_ENV: CC.FALLBACK_MODEL}):
            code, _out, _err = self.run_table({})
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [CC.FALLBACK_MODEL])
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(self.last_record()["requested_model"], CC.FALLBACK_MODEL)

    def test_sol_does_not_reuse_astra_record(self):
        # 场景 5：Astra 正常结果后请求 SOL → 不复用 Astra 的结论。
        with mock.patch.dict(os.environ, {CC.MODEL_ENV: CC.FALLBACK_MODEL}):
            self.run_table({})
        self.assertEqual(self.last_record()["actual_model"], CC.FALLBACK_MODEL)
        self.calls.clear()
        code, _out, _err = self.run_table({})
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [CC.DEFAULT_MODEL])
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(self.last_record()["actual_model"], CC.DEFAULT_MODEL)

    def test_fallback_record_keeps_audit_identity(self):
        # 场景 6：兜底结果不参与复用，但审计/追踪/统计信息完整保留。
        self.run_table({CC.DEFAULT_MODEL: overload_error()})
        record = self.last_record()
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["requested_model"], CC.DEFAULT_MODEL)
        self.assertEqual(record["actual_model"], CC.FALLBACK_MODEL)
        self.assertEqual(record["model"], CC.FALLBACK_MODEL)
        self.assertEqual(record["model_key"], CC.model_key(CC.FALLBACK_MODEL))
        self.assertTrue(record["fallback_used"])
        self.assertTrue(record["fallback_reason"])
        self.assertEqual(record["attempt_count"], 2)
        self.assertEqual(record["attempt_trail"][0]["status"], "failed")
        self.assertEqual(record["attempt_trail"][1]["status"], "completed")
        self.assertTrue(record["primary_error"])
        # 再次请求（未命中复用）不会删除或改写这条记录。
        self._clear_model_window(CC.DEFAULT_MODEL)
        self.run_table({})
        kept = [item for item in self.records()
                if item["call_id"] == record["call_id"]][0]
        self.assertEqual(kept["actual_model"], CC.FALLBACK_MODEL)
        self.assertEqual(kept["attempt_count"], 2)

    def test_legacy_record_without_actual_model_is_not_reused(self):
        # 场景 7：缺 actual_model 的旧记录无法确认身份 → 未命中，按新调用处理。
        self.run_table({})
        state = self.state()
        state["cognition_calls"][-1].pop("actual_model", None)
        G.save_state(self.state_path, state)
        self.calls.clear()
        code, _out, _err = self.run_table({})
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [CC.DEFAULT_MODEL])
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(self.state().get("cognition_reuses") or [], [])

    def test_both_models_failing_leaves_no_reusable_record(self):
        # 场景 8：双失败不得产生可复用（成功）缓存。
        self.run_table({CC.DEFAULT_MODEL: overload_error(),
                        CC.FALLBACK_MODEL: server_error()})
        self.assertEqual(
            [item["status"] for item in self.records()], ["failed"])
        self._clear_model_window(CC.DEFAULT_MODEL)
        self._clear_model_window(CC.FALLBACK_MODEL)
        self.calls.clear()
        code, _out, _err = self.run_table({})
        self.assertEqual(code, 0)
        # 未复用失败记录：确实重新走了主模型（本次主模型成功，不再触发兜底）。
        self.assertEqual(self.calls, [CC.DEFAULT_MODEL])
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(self.records()[0]["status"], "failed")
        self.assertEqual(self.last_record()["status"], "completed")
        self.assertEqual(self.state().get("cognition_reuses") or [], [])


if __name__ == "__main__":
    unittest.main()

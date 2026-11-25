#!/usr/bin/env python3
"""SOL 经 CPA 调用的可靠性回归：错误分类、Retry-After、退避窗口、恢复与幂等。

对应 2026-11-25 专项的完成条件 C2–C7：

1. 408 / 502(`server_is_overloaded`) / 503(`auth_unavailable`) / 401 / 400 / 429
   各有稳定、机器可读的分类，并保留原始 HTTP 状态、上游 code 与 Retry-After。
2. `auth_unavailable`（CPA 凭据冷却）永不被登记为认证失效。
3. 保持单次请求：任何失败与退避都不触发自动重试或换 Provider。
4. 客户端超时、传输中断、响应不完整、`max_output_tokens` 截断都拒绝消费，
   不登记为成功，也不产生伪造认知。
5. 失败记录保留可追溯事实；上游要求等待时调度推迟义务（不结算、不改阶段）。
6. 远程成功但本地登记失败不重复计费（pending 恢复）；同阶段去重、不同阶段独立。

所有通道都是替身，本文件不发起任何真实模型请求。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message
from unittest import mock

import v6_support as F

G = F.G
import cognition_call as CC  # noqa: E402  v6_support 已把 scripts/ 加入 sys.path
import cognition_schedule as SCH  # noqa: E402
import copilot_dispatch as CD  # noqa: E402

URL = "http://127.0.0.1:8317/v1/responses"


def _body(code: str = "", message: str = "", **extra) -> bytes:
    payload = {"error": {"code": code, "message": message, **extra}}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _http_error(status: int, body: bytes = b"", headers: dict | None = None):
    return urllib.error.HTTPError(URL, status, "error", headers or {},
                                  io.BytesIO(body))


def think_text() -> str:
    return json.dumps({
        "schema": CC.THINK_SCHEMA,
        "judgment": "先补定向测试再合并",
        "essence": "真正的风险在缺少验证",
        "strategy": "先用定向测试锁定行为",
        "tradeoffs": ["灰度会慢一步"],
        "risks": ["缺少集成测试"],
        "priority": ["P1 验证"],
        "verify": ["定向测试覆盖关键分支"],
        "reconsult": ["出现并发写入"],
        "unverified": [],
    }, ensure_ascii=False)


def think_payload(*, status: str = "completed", http_status: int = 200,
                  text: str | None = None, incomplete: dict | None = None,
                  request_id: str = "", trace_id: str = "") -> dict:
    payload = {
        "model": CC.MODEL,
        "status": status,
        "output": [{"type": "message", "status": "completed",
                    "content": [{"type": "output_text",
                                 "text": think_text() if text is None else text}]}],
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "reasoning": {"effort": "high", "mode": "standard"},
        "_http_status": http_status,
        "_request_id": request_id,
        "_trace_id": trace_id,
    }
    if incomplete is not None:
        payload["incomplete_details"] = incomplete
    return payload


def think_result(mode: str = "CONSULT") -> dict:
    """通道替身返回的成功结果（结构与真实 run_cognition 一致，不联网）。"""
    return {
        "schema": CC.THINK_SCHEMA, "mode": mode,
        "model": CC.MODEL, "model_resolved": CC.MODEL,
        "reasoning_effort": "high", "reasoning_mode": "standard",
        "reasoning_resolved": {"effort": "high"}, "reasoning_verified": True,
        "zero_tool": True, "attempts": 1, "request_id": "req-1",
        "trace_id": "trace-1", "response_id": "resp-1",
        "materials_sha256": "e" * 64, "materials_chars": 10,
        "request_sha256": "r" * 64, "reply_text": think_text(),
        "reply_sha256": "a" * 64, "reply_chars": 10, "usage": {"total_tokens": 42},
        "usage_available": True, "latency_ms": 1,
        "judgment": "先补测试", "essence": "缺少验证", "strategy": "先测后改",
        "tradeoffs": [], "risks": [], "priority": [],
        "verify": [], "reconsult": [], "unverified": [],
        "completed_at": G.now_iso(),
    }


# ── 1. HTTP 失败分类（原始状态/上游 code/Retry-After 全保留） ──────────────
class ClassificationTests(unittest.TestCase):
    def _post(self, status: int, body: bytes = b"",
              headers: dict | None = None):
        error = _http_error(status, body, headers)
        with mock.patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = error
            with self.assertRaises(CC.CognitionError) as ctx:
                CC._post_once(URL, "k", {"model": "m"}, 5)
            # 单次请求：失败不重试、不换 Provider。
            self.assertEqual(build.return_value.open.call_count, 1)
        return ctx.exception

    def test_408_stream_disconnect_is_upstream_stream_timeout(self):
        exc = self._post(408, _body(
            "request_timeout",
            "stream disconnected before completion: "
            "stream closed before response.completed"))
        self.assertIsInstance(exc, CC.CognitionStreamTimeoutError)
        self.assertEqual(exc.error_class, CC.UPSTREAM_STREAM_TIMEOUT)
        self.assertEqual(exc.http_status, 408)
        self.assertEqual(exc.exit_code, CC.EXIT_UPSTREAM)
        self.assertIsNone(exc.retry_after)
        self.assertIn("HTTP 408", exc.detail)
        self.assertIn("upstream_code=request_timeout", exc.detail)

    def test_502_overload_keeps_retry_after_header(self):
        exc = self._post(502, _body("server_is_overloaded",
                                    "The server is overloaded",),
                         {"Retry-After": "322", "X-Request-Id": "req-9",
                          "X-Cpa-Trace-Id": "trace-9"})
        self.assertIsInstance(exc, CC.CognitionOverloadedError)
        self.assertEqual(exc.error_class, CC.UPSTREAM_OVERLOADED)
        self.assertEqual(exc.http_status, 502)
        self.assertEqual(exc.retry_after, 322)
        self.assertEqual(exc.upstream_code, "server_is_overloaded")
        self.assertEqual((exc.request_id, exc.trace_id), ("req-9", "trace-9"))
        self.assertIn("Retry-After=322s", exc.detail)
        self.assertNotEqual(exc.error_class, CC.UPSTREAM_AUTH)

    def test_502_retry_after_header_is_case_insensitive(self):
        headers = Message()
        headers["retry-after"] = "375"
        exc = self._post(502, _body("server_is_overloaded", "overloaded"),
                         headers)
        self.assertEqual(exc.retry_after, 375)

    def test_retry_after_ms_in_body_is_honoured(self):
        body = json.dumps({"error": {
            "code": "server_is_overloaded", "message": "overloaded",
            "headers": {"retry-after-ms": "322000"}}}).encode("utf-8")
        exc = self._post(502, body)
        self.assertEqual(exc.retry_after, 322)

    def test_http_date_retry_after_is_parsed_as_remaining_seconds(self):
        stamp = datetime.now(timezone.utc) + timedelta(hours=2)
        raw = stamp.strftime("%a, %d %b %Y %H:%M:%S GMT")
        exc = self._post(503, _body("auth_unavailable", "no auth available"),
                         {"Retry-After": raw})
        self.assertIsNotNone(exc.retry_after)
        self.assertAlmostEqual(exc.retry_after, 7200, delta=120)
        self.assertEqual(exc.retry_after, int(round(exc.retry_after)))
        self.assertEqual(exc.retry_after_raw, raw)

    def test_expired_http_date_is_not_guessed(self):
        raw = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%a, %d %b %Y %H:%M:%S GMT")
        exc = self._post(408, _body("request_timeout", "stream closed"),
                         {"Retry-After": raw})
        self.assertIsNone(exc.retry_after)
        self.assertEqual(exc.retry_after_raw, raw)
        # 过期的服务端时间不当作“可以立刻重试”，由本地退避策略兜底。
        self.assertEqual(exc.error_class, CC.UPSTREAM_STREAM_TIMEOUT)

    def test_unparseable_retry_after_falls_back(self):
        exc = self._post(502, _body("server_is_overloaded", "overloaded"),
                         {"Retry-After": "soon-ish"})
        self.assertIsNone(exc.retry_after)
        self.assertEqual(exc.retry_after_raw, "soon-ish")

    def test_503_auth_unavailable_is_credential_cooling_not_auth(self):
        exc = self._post(503, _body(
            "auth_unavailable",
            "no auth available; last upstream error: server_is_overloaded"),
            {"Retry-After": "194"})
        self.assertIsInstance(exc, CC.CognitionCredentialCoolingError)
        self.assertEqual(exc.error_class, CC.UPSTREAM_CREDENTIAL_COOLING)
        self.assertNotEqual(exc.error_class, CC.UPSTREAM_AUTH)
        self.assertEqual(exc.retry_after, 194)
        self.assertIn("不是认证失效", exc.detail)

    def test_401_and_403_are_real_auth_failures(self):
        for status in (401, 403):
            with self.subTest(status=status):
                exc = self._post(status, _body("unauthorized", "bad key"))
                self.assertIsInstance(exc, CC.CognitionAuthError)
                self.assertEqual(exc.error_class, CC.UPSTREAM_AUTH)

    def test_4xx_request_errors_are_client_side(self):
        exc = self._post(400, _body("invalid_request", "bad payload"))
        self.assertIsInstance(exc, CC.CognitionUpstreamClientError)
        self.assertEqual(exc.error_class, CC.UPSTREAM_CLIENT)

    def test_plain_5xx_is_server_error(self):
        exc = self._post(500, _body("server_error", "boom"))
        self.assertIsInstance(exc, CC.CognitionUpstreamError)
        self.assertEqual(exc.error_class, CC.UPSTREAM_SERVER)

    def test_429_stays_quota(self):
        exc = self._post(429, _body("usage_limit_reached", "quota"))
        self.assertIsInstance(exc, CC.CognitionQuotaError)
        self.assertEqual(exc.error_class, CC.UPSTREAM_RATE_LIMIT)

    def test_received_error_response_means_request_was_sent(self):
        exc = self._post(502, _body("server_is_overloaded", "overloaded"))
        self.assertTrue(exc.response_received)


# ── 2. 真实调用路径：单次请求、审计事实、不伪造成功 ────────────────────────
class RunCognitionAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit = os.path.join(self.tmp.name, "audit.jsonl")

    def _audit(self) -> dict:
        with open(self.audit, "r", encoding="utf-8") as fh:
            lines = [line for line in fh.read().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        return json.loads(lines[0])

    def _run(self, side_effect=None, return_value=None):
        with mock.patch("urllib.request.build_opener") as build:
            if side_effect is not None:
                build.return_value.open.side_effect = side_effect
            else:
                handle = mock.MagicMock()
                handle.__enter__.return_value.read.return_value = \
                    json.dumps(return_value).encode("utf-8")
                handle.__enter__.return_value.status = 200
                handle.__enter__.return_value.headers = {}
                build.return_value.open.return_value = handle
            return CC.run_consult("材料", api_key="k", audit_log=self.audit)

    def test_408_audit_keeps_class_status_and_ids(self):
        error = _http_error(408, _body("request_timeout", "stream disconnected "
                                       "before completion"),
                            {"X-Request-Id": "req-1", "X-Cpa-Trace-Id": "t-1"})
        with self.assertRaises(CC.CognitionStreamTimeoutError):
            self._run(side_effect=error)
        record = self._audit()
        self.assertFalse(record["ok"])
        self.assertEqual(record["error_class"], CC.UPSTREAM_STREAM_TIMEOUT)
        self.assertEqual(record["http_status"], 408)
        self.assertIsNone(record["retry_after"])
        self.assertEqual(record["upstream_code"], "request_timeout")
        self.assertEqual((record["request_id"], record["trace_id"]),
                         ("req-1", "t-1"))
        self.assertTrue(record["request_attempted"])
        self.assertEqual(record["send_observed"], "response_received")
        self.assertEqual(record["attempts"], 1)

    def test_client_timeout_is_recorded_as_sent_with_unknown_result(self):
        with self.assertRaises(CC.CognitionTimeoutError) as ctx:
            self._run(side_effect=TimeoutError("boom"))
        self.assertEqual(ctx.exception.exit_code, CC.EXIT_TIMEOUT)
        self.assertEqual(ctx.exception.error_class, CC.CLIENT_TIMEOUT)
        record = self._audit()
        self.assertEqual(record["error_class"], CC.CLIENT_TIMEOUT)
        self.assertIsNone(record["http_status"])
        self.assertTrue(record["request_attempted"])
        # 客户端超时无法证明上游是否已生成回复：保持 unknown，不当作未发送。
        self.assertEqual(record["send_observed"], "unknown")

    def test_incomplete_response_is_rejected_not_registered(self):
        with self.assertRaises(CC.CognitionResponseError) as ctx:
            self._run(return_value=think_payload(
                status="incomplete",
                incomplete={"reason": "max_output_tokens"}))
        self.assertEqual(ctx.exception.code, "cognition_response_incomplete")
        self.assertEqual(ctx.exception.error_class, CC.RESPONSE_INCOMPLETE)
        self.assertIn("max_output_tokens", ctx.exception.detail)
        record = self._audit()
        self.assertEqual(record["error_class"], CC.RESPONSE_INCOMPLETE)

    def test_empty_text_is_not_a_success(self):
        with self.assertRaises(CC.CognitionResponseError) as ctx:
            self._run(return_value=think_payload(text="   "))
        self.assertEqual(ctx.exception.code, "cognition_output_empty")

    def test_default_output_budget_is_unchanged(self):
        # 24000 是 v7.0.0 起的既定值；历史 8192 截断修复没有回退。
        self.assertEqual(CC.DEFAULT_MAX_OUTPUT_TOKENS, 24000)
        self.assertEqual(CC.build_payload("材料")["max_output_tokens"], 24000)

    def test_timeout_budget_is_unchanged(self):
        self.assertEqual(CC.DEFAULT_TIMEOUT, 300.0)


# ── 3. 失败登记与退避窗口 ───────────────────────────────────────────────────
class FailureRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="目标", contract=F.CONTRACT,
                      risk="medium", task_id="reliability")

    def state(self) -> dict:
        return G.load_state(self.state_path)

    def _fail(self, **kwargs) -> dict:
        defaults = dict(mode="CONSULT", error_code="cognition_upstream_overloaded",
                        detail="HTTP 502",
                        model=CC.DEFAULT_MODEL, provider=CC.PROVIDER)
        defaults.update(kwargs)
        return G.record_cognition_call_failure(self.state_path, **defaults)

    def window(self, error_class: str = CC.UPSTREAM_OVERLOADED) -> dict:
        state = self.state()
        slots = ((state.get(G.MODEL_BACKOFF_KEY) or {}).get("windows") or {}) \
            .get(CC.model_key(), {})
        return slots.get(error_class, {}) if isinstance(slots, dict) else {}

    def backoff(self, include_model: bool = True) -> dict:
        return G.cognition_backoff_status(
            self.state(),
            model=CC.model_key() if include_model else "")

    def test_failure_record_keeps_raw_upstream_facts(self):
        record = self._fail(http_status=502, retry_after=322,
                            upstream_code="server_is_overloaded",
                            error_class=CC.UPSTREAM_OVERLOADED,
                            request_id="req-1", trace_id="trace-1",
                            stage="planning")
        self.assertEqual(record["error_class"], CC.UPSTREAM_OVERLOADED)
        self.assertEqual(record["http_status"], 502)
        self.assertEqual(record["retry_after"], 322)
        self.assertEqual(record["upstream_code"], "server_is_overloaded")
        self.assertEqual((record["request_id"], record["trace_id"]),
                         ("req-1", "trace-1"))
        stored = self.state()["cognition_calls"][-1]
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["error_class"], CC.UPSTREAM_OVERLOADED)
        self.assertEqual(stored["stage"], "planning")

    def test_historical_failure_infers_error_class(self):
        for code, expected in (
                ("cognition_quota", CC.UPSTREAM_RATE_LIMIT),
                ("cognition_timeout", CC.CLIENT_TIMEOUT),
                ("cognition_transport_error", CC.NETWORK_ERROR),
                ("cognition_response_incomplete", CC.RESPONSE_INCOMPLETE)):
            with self.subTest(code=code):
                self.assertEqual(G._infer_error_class(code), expected)
        record = self._fail(error_code="cognition_quota", http_status=429,
                            retry_after=60)
        self.assertEqual(record["error_class"], CC.UPSTREAM_RATE_LIMIT)

    def test_retry_after_sets_backoff_window(self):
        record = self._fail(http_status=502, retry_after=322,
                            error_class=CC.UPSTREAM_OVERLOADED)
        self.assertEqual(record["backoff"]["seconds"], 322)
        self.assertEqual(record["backoff"]["model_key"], CC.model_key())
        status = self.backoff()
        self.assertTrue(status["active"])
        self.assertEqual(status["reason"], "retry-after")
        self.assertEqual(status["error_class"], CC.UPSTREAM_OVERLOADED)

    def test_server_retry_after_is_never_truncated(self):
        # 服务端 Retry-After 是最短等待时间：3600s 必须原样保留，不被 1800s 上限截断。
        record = self._fail(http_status=502, retry_after=3600,
                            error_class=CC.UPSTREAM_OVERLOADED)
        self.assertEqual(record["backoff"]["seconds"], 3600)
        self.assertEqual(self.window()["seconds"], 3600)
        remaining = self.backoff()["remaining_seconds"]
        self.assertGreater(remaining, G.COGNITION_BACKOFF_MAX_SECONDS)

    def test_env_cap_cannot_shrink_server_wait(self):
        with mock.patch.dict(os.environ, {"COPILOT_COGNITION_BACKOFF_MAX": "60"}):
            self._fail(http_status=502, retry_after=3600,
                       error_class=CC.UPSTREAM_OVERLOADED)
            self.assertEqual(self.window()["seconds"], 3600)

    def test_env_cap_still_bounds_local_fallback(self):
        with mock.patch.dict(os.environ, {"COPILOT_COGNITION_BACKOFF_MAX": "10"}):
            self._fail(http_status=408,
                       error_class=CC.UPSTREAM_STREAM_TIMEOUT)
            self.assertEqual(
                self.window(CC.UPSTREAM_STREAM_TIMEOUT)["seconds"], 10)

    def test_backoff_record_keeps_raw_retry_after(self):
        raw = "Wed, 25 Nov 2026 20:00:00 GMT"
        self._fail(http_status=502, retry_after=900, retry_after_raw=raw,
                   error_class=CC.UPSTREAM_OVERLOADED)
        self.assertEqual(self.window()["retry_after_raw"], raw)
        self.assertEqual(self.state()["cognition_calls"][-1]["retry_after_raw"],
                         raw)

    def test_retryable_class_without_hint_gets_default_window(self):
        self._fail(http_status=408, error_class=CC.UPSTREAM_STREAM_TIMEOUT)
        status = self.backoff()
        self.assertTrue(status["active"])
        self.assertEqual(status["reason"], "error-class")

    def test_auth_and_client_errors_do_not_create_backoff(self):
        self._fail(http_status=401, error_class=CC.UPSTREAM_AUTH)
        self.assertFalse(self.backoff()["active"])
        self._fail(http_status=400, error_class=CC.UPSTREAM_CLIENT)
        self.assertFalse(self.backoff()["active"])

    def test_failure_without_model_identity_writes_no_legacy_window(self):
        # v9.10：没有模型身份的失败只记失败事实，不再写 `sol_backoff`
        # （历史单例只读保留，新写入不得产生旧格式数据）。
        record = self._fail(model="", provider="", http_status=502,
                            retry_after=300,
                            error_class=CC.UPSTREAM_OVERLOADED)
        self.assertEqual(record["backoff_skipped"], "missing-model-identity")
        self.assertNotIn("backoff", record)
        state = self.state()
        self.assertNotIn(G.LEGACY_BACKOFF_KEY, state)
        self.assertFalse(G.cognition_backoff_status(state)["active"])
        self.assertEqual(state["cognition_calls"][-1]["status"], "failed")

    def test_backoff_expires_without_state_change(self):
        # 只读兼容：v9.8 及更早的历史单例仍可读，过期即失效，不改历史状态。
        state = self.state()
        state[G.LEGACY_BACKOFF_KEY] = {
            "schema": G.COGNITION_BACKOFF_SCHEMA,
            "until": (datetime.now().astimezone()
                      - timedelta(seconds=1)).isoformat(timespec="seconds"),
            "reason": "retry-after", "error_class": CC.UPSTREAM_OVERLOADED,
        }
        G.save_state(self.state_path, state)
        status = G.cognition_backoff_status(self.state())
        self.assertFalse(status["active"])
        self.assertEqual(status["remaining_seconds"], 0.0)

    def test_corrupt_backoff_record_does_not_block_forever(self):
        state = self.state()
        state[G.LEGACY_BACKOFF_KEY] = {"until": "not-a-time"}
        G.save_state(self.state_path, state)
        status = G.cognition_backoff_status(self.state())
        self.assertFalse(status["active"])
        self.assertTrue(status["invalid"])

    def test_successful_call_clears_backoff(self):
        self._fail(http_status=502, retry_after=300,
                   error_class=CC.UPSTREAM_OVERLOADED)
        self.assertTrue(self.backoff()["active"])
        G.record_cognition_call(self.state_path, {
            "call_id": "SCok", "mode": "CONSULT", "summary": "ok",
            "materials_sha256": "m" * 64,
            "model": CC.DEFAULT_MODEL, "model_key": CC.model_key(),
            "provider": CC.PROVIDER,
            "completed_at": G.now_iso()},
            mode="CONSULT", materials_sha256="m" * 64)
        state = self.state()
        self.assertNotIn(CC.model_key(),
                         ((state.get(G.MODEL_BACKOFF_KEY) or {})
                          .get("windows") or {}))
        self.assertNotIn(G.LEGACY_BACKOFF_KEY, state)
        self.assertFalse(G.cognition_backoff_status(state)["active"])
        events = [item.get("event") for item in state.get("history") or []]
        self.assertIn("cognition_backoff_cleared", events)

    def test_failure_does_not_change_stage_or_criteria(self):
        before = self.state()
        self._fail(http_status=502, retry_after=300,
                   error_class=CC.UPSTREAM_OVERLOADED)
        after = self.state()
        self.assertEqual(before["mode"], after["mode"])
        self.assertEqual(before["schema"], after["schema"])
        self.assertEqual(before["contract"], after["contract"])
        self.assertEqual(len(after["contract"]["criteria"]),
                         len(before["contract"]["criteria"]))

    def test_backoff_classes_match_cognition_channels(self):
        # 一致性：调用侧认为"上游不可用"的分类必须是被调度层推迟的同一集合。
        self.assertTrue(CC.UPSTREAM_RETRYABLE_CLASSES
                        <= G.COGNITION_BACKOFF_CLASSES)


# ── 4. 调度与手动路径：窗口内不发请求，窗口关闭后继续 ──────────────────────
class BackoffDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="交付可复核结果",
                      contract=F.CONTRACT, risk="medium",
                      task_id="reliability", workspace=self.dir,
                      # 方案类任务＝真实认知价值信号：本组测试需要一个可调度义务；
                      # 默认 standard + risk=medium 不再产生义务。
                      task_kind="architecture")
        self.materials = os.path.join(self.dir, "materials.md")
        with open(self.materials, "w", encoding="utf-8") as fh:
            fh.write("材料正文\n")

    def state(self) -> dict:
        return G.load_state(self.state_path)

    def _set_backoff(self, seconds: float = 322) -> None:
        # 退避窗口按 provider+model+error_class 隔离：只写当前默认模型的窗口，
        # 与真实失败路径（record_cognition_call_failure 带 model）保持一致。
        state = self.state()
        window = G._cognition_backoff_from_failure({
            "error_class": CC.UPSTREAM_OVERLOADED, "retry_after": seconds,
            "http_status": 502, "call_id": "SCfail"})
        G.store_model_backoff(state, {**window, "model": CC.DEFAULT_MODEL,
                                      "provider": CC.PROVIDER,
                                      "model_key": CC.model_key()})
        G.save_state(self.state_path, state)

    def _consult(self, *extra: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return CD.main(["cognition-consult", "--state", self.state_path,
                            "--materials", self.materials,
                            "--question", "本次要判断什么", *extra])

    def _sync(self) -> None:
        state = self.state()
        synced = G.sync_cognition_schedule(state, advice=G.cognition_advice_summary(state))
        if synced["changes"]:
            G.save_state(self.state_path, state)

    def test_schedule_step_defers_inside_window_without_request(self):
        self._sync()
        self.assertTrue(SCH.due_obligations(self.state()))
        self._set_backoff()
        args = mock.Mock(state=self.state_path, cognition="auto")
        calls = {"count": 0}

        def fake(*_a, **_k):
            calls["count"] += 1
            return think_result()

        with mock.patch.dict(os.environ, {"COPILOT_COGNITION_AUTO": "on"}), \
                mock.patch.object(CC, "run_consult", side_effect=fake), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            result = CD._cognition_schedule_step(self.state_path, args)
        self.assertEqual(calls["count"], 0)
        self.assertEqual(result["executed"], "")
        self.assertTrue(result["deferred"]["obligation"])
        row = SCH.obligations(self.state())[0]
        self.assertEqual(row["status"], "due")
        self.assertEqual(row["deferrals"], 1)
        self.assertIn("upstream-backoff", row["last_deferral_reason"])

    def test_schedule_executes_after_window_closes(self):
        self._sync()
        self._set_backoff(1)
        state = self.state()
        window = state[G.MODEL_BACKOFF_KEY]["windows"][CC.model_key()][
            CC.UPSTREAM_OVERLOADED]
        window["until"] = (datetime.now().astimezone()
                           - timedelta(seconds=1)).isoformat(timespec="seconds")
        G.save_state(self.state_path, state)
        args = mock.Mock(state=self.state_path, cognition="auto")
        calls = {"count": 0}

        def fake(*_a, **_k):
            calls["count"] += 1
            return think_result()

        with mock.patch.dict(os.environ, {"COPILOT_COGNITION_AUTO": "on"}), \
                mock.patch.object(CC, "run_consult", side_effect=fake), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            result = CD._cognition_schedule_step(self.state_path, args)
        self.assertEqual(calls["count"], 1)
        self.assertTrue(result["executed"])

    def test_manual_call_is_refused_inside_window(self):
        self._set_backoff()
        with mock.patch.object(CC, "run_consult",
                               side_effect=AssertionError("不应发起请求")):
            code = self._consult()
        self.assertEqual(code, 2)
        failed = [item for item in self.state().get("cognition_calls") or []
                  if item.get("status") == "failed"]
        self.assertEqual(failed, [])

    def test_ignore_backoff_override_sends_exactly_one_request(self):
        self._set_backoff()
        with mock.patch.object(CC, "run_consult",
                               return_value=think_result()) as run:
            code = self._consult("--ignore-backoff")
        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 1)
        state = self.state()
        self.assertEqual(state["cognition_calls"][-1]["status"], "completed")
        events = [item.get("event") for item in state.get("history") or []]
        self.assertIn("cognition_backoff_overridden", events)
        self.assertFalse(G.cognition_backoff_status(
            state, model=CC.model_key())["active"])

    def test_consult_failure_degrades_and_stays_traceable(self):
        error = CC.CognitionOverloadedError(
            "cognition_upstream_overloaded", "上游瞬时容量不足",
            http_status=502, retry_after=300,
            upstream_code="server_is_overloaded",
            error_class=CC.UPSTREAM_OVERLOADED, response_received=True)
        error.request_attempted = True
        error.send_observed = "response_received"
        out = io.StringIO()
        with mock.patch.object(CC, "run_consult", side_effect=error), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = CD.main(["cognition-consult", "--state", self.state_path,
                            "--materials", self.materials,
                            "--question", "本次要判断什么", "--json"])
        # CONSULT 失败只降级：不阻断工程、不返回非零。
        self.assertEqual(code, 0)
        record = self.state()["cognition_calls"][-1]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error_class"], CC.UPSTREAM_OVERLOADED)
        self.assertEqual(record["http_status"], 502)
        self.assertEqual(record["retry_after"], 300)
        self.assertEqual(record["upstream_code"], "server_is_overloaded")
        degraded = json.loads(out.getvalue())
        self.assertEqual(degraded["status"], "unavailable")
        self.assertEqual(degraded["action"], "continue")
        self.assertEqual(degraded["error_class"], CC.UPSTREAM_OVERLOADED)
        self.assertTrue(G.cognition_backoff_status(
            self.state(), model=CC.model_key())["active"])

    def test_review_failure_returns_real_exit_code(self):
        error = CC.CognitionAuthError("cognition_upstream_auth", "认证失败",
                                http_status=401,
                                error_class=CC.UPSTREAM_AUTH,
                                response_received=True)
        error.request_attempted = True
        error.send_observed = "response_received"
        with mock.patch.object(CC, "run_review", side_effect=error), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = CD.main(["cognition-review", "--state", self.state_path,
                            "--materials", self.materials])
        self.assertEqual(code, error.exit_code)
        self.assertEqual(self.state()["cognition_calls"][-1]["error_class"],
                         CC.UPSTREAM_AUTH)


# ── 5. 恢复与幂等：不重复付费、同阶段去重、不同阶段独立 ────────────────────
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="交付可复核结果",
                      contract=F.CONTRACT, risk="medium",
                      task_id="reliability", workspace=self.dir,
                      # 方案类任务＝真实认知价值信号：本组测试需要一个可调度义务；
                      # 默认 standard + risk=medium 不再产生义务。
                      task_kind="architecture")
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

    def test_remote_success_with_registration_failure_is_recovered(self):
        with mock.patch.object(CC, "run_consult",
                               return_value=think_result()) as run, \
                mock.patch.object(G, "record_cognition_call",
                                  side_effect=G.GovernanceError("磁盘失败")):
            code = self._consult()
        self.assertEqual(code, 2)
        self.assertEqual(run.call_count, 1)
        pending_dir = os.path.join(self.dir, "cognition-consult")
        pendings = [name for name in os.listdir(pending_dir)
                    if name.startswith("pending-")]
        self.assertEqual(len(pendings), 1)
        # 第二次同材料调用直接恢复，不再发起请求（不重复计费）。
        with mock.patch.object(CC, "run_consult",
                               side_effect=AssertionError("不应再次请求")):
            code = self._consult()
        self.assertEqual(code, 0)
        record = self.state()["cognition_calls"][-1]
        self.assertEqual(record["status"], "completed")
        self.assertTrue(record["recovered_from_pending"])
        self.assertFalse(os.path.exists(
            os.path.join(pending_dir, pendings[0])))

    def test_same_subject_reuses_without_second_request(self):
        with mock.patch.object(CC, "run_consult",
                               return_value=think_result()) as run:
            self.assertEqual(self._consult(), 0)
            self.assertEqual(self._consult(), 0)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(self.state()["cognition_calls"]), 1)

    def test_different_stage_is_an_independent_call(self):
        trigger = sorted(CC.RETRY_TRIGGERS)[0]
        with mock.patch.object(CC, "run_consult",
                               return_value=think_result()) as run:
            self.assertEqual(self._consult("--stage", "understanding"), 0)
            self.assertEqual(self._consult(
                "--stage", "planning", "--force", "--trigger", trigger,
                "--reason", "阶段目标已变化"), 0)
        self.assertEqual(run.call_count, 2)
        calls = self.state()["cognition_calls"]
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]["stage"], calls[1]["stage"])

    def test_same_subject_without_reason_is_refused(self):
        with mock.patch.object(CC, "run_consult",
                               return_value=think_result()) as run:
            self.assertEqual(self._consult("--stage", "understanding"), 0)
            code = self._consult("--stage", "planning")
        self.assertEqual(code, 2)
        self.assertEqual(run.call_count, 1)

    def test_failure_keeps_obligation_due_instead_of_satisfied(self):
        state = self.state()
        synced = G.sync_cognition_schedule(state, advice=G.cognition_advice_summary(state))
        if synced["changes"]:
            G.save_state(self.state_path, state)
        due = SCH.due_obligations(self.state())
        self.assertTrue(due)
        target = due[0]
        error = CC.CognitionOverloadedError(
            "cognition_upstream_overloaded", "上游过载", http_status=502,
            retry_after=300, upstream_code="server_is_overloaded",
            error_class=CC.UPSTREAM_OVERLOADED, response_received=True)
        error.request_attempted = True
        error.send_observed = "response_received"
        with mock.patch.object(CC, "run_consult", side_effect=error), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = CD.main(["cognition-consult", "--state", self.state_path,
                            "--obligation", target["obligation_id"],
                            "--lock-timeout", "1"])
        self.assertEqual(code, 0)
        row = SCH.find_obligation(self.state(), obligation_id=target["obligation_id"])
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["failure"], "cognition_upstream_overloaded")


# ── 5. 408 跨阶段 / 跨会话退避（follow-up 2026-11-25）───────────────────────
class StreamTimeoutCrossStageTests(unittest.TestCase):
    """408 是上游瞬时流中断：窗口内不同阶段与跨会话都不得连续制造无效请求。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        F.init_git_workspace(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        G.start_state(self.state_path, goal="复核 408 跨阶段退避",
                      contract=F.CONTRACT, risk="medium",
                      task_id="stream-timeout", workspace=self.dir,
                      task_kind="architecture")
        self.materials = os.path.join(self.dir, "materials.md")
        with open(self.materials, "w", encoding="utf-8") as fh:
            fh.write("材料正文\n")

    def state(self) -> dict:
        return G.load_state(self.state_path)

    def _window(self, state: dict | None = None) -> dict:
        state = state if state is not None else self.state()
        return ((state.get(G.MODEL_BACKOFF_KEY) or {}).get("windows", {})
                .get(CC.model_key(), {}).get(CC.UPSTREAM_STREAM_TIMEOUT, {}))

    def _fail_408(self, retry_after=None, raw: str = "") -> dict:
        return G.record_cognition_call_failure(
            self.state_path, mode="CONSULT",
            model=CC.DEFAULT_MODEL, provider=CC.PROVIDER,
            error_code="cognition_upstream_stream_timeout",
            detail="stream closed before response.completed",
            http_status=408, retry_after=retry_after,
            retry_after_raw=raw,
            upstream_code="request_timeout",
            error_class=CC.UPSTREAM_STREAM_TIMEOUT,
            send_observed="response_received")

    def _add_obligation(self, family: str, event: str, mode: str,
                        stage: str, instance: str) -> str:
        state = self.state()
        schedule = SCH.ensure_schedule(state)
        obligation = SCH._new_obligation(state, {
            "family": family, "event": event, "instance": instance,
            "mode": mode, "stage": stage, "level": "recommended",
            "question": "本次要判断什么"}, now=G.now_iso())
        obligation["obligation_id"] = SCH._next_obligation_id(schedule)
        schedule["obligations"].append(obligation)
        G.save_state(self.state_path, state)
        return obligation["obligation_id"]

    def _execute(self, obligation_id: str) -> tuple[int, int]:
        obligation = SCH.find_obligation(self.state(),
                                        obligation_id=obligation_id)
        calls = {"count": 0}

        def fake(*_a, **_k):
            calls["count"] += 1
            return think_result()

        with mock.patch.object(CC, "run_consult", side_effect=fake), \
                mock.patch.object(CC, "run_plan", side_effect=fake), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = CD._execute_obligation(self.state_path, dict(obligation))
        return code, calls["count"]

    def test_408_without_retry_after_creates_local_window(self):
        record = self._fail_408()
        backoff = self._window()
        self.assertEqual(record["backoff"]["reason"], "error-class")
        self.assertEqual(backoff["reason"], "error-class")
        self.assertEqual(backoff["error_class"], CC.UPSTREAM_STREAM_TIMEOUT)
        self.assertEqual(backoff["seconds"], G.COGNITION_BACKOFF_FALLBACK_SECONDS)
        self.assertNotEqual(backoff["error_class"], CC.UPSTREAM_AUTH)
        self.assertNotIn(G.LEGACY_BACKOFF_KEY, self.state())
        self.assertTrue(G.cognition_backoff_status(
            self.state(), model=CC.model_key())["active"])

    def test_408_with_retry_after_uses_server_wait(self):
        self._fail_408(600, raw="600")
        backoff = self._window()
        self.assertEqual(backoff["reason"], "retry-after")
        self.assertEqual(backoff["seconds"], 600)
        self.assertEqual(backoff["retry_after_raw"], "600")

    def test_startup_408_defers_plan_stage(self):
        self._fail_408()
        plan_id = self._add_obligation("B", "plan", "PLAN", "planning",
                                       "plan-sha")
        code, calls = self._execute(plan_id)
        self.assertEqual(code, 0)
        self.assertEqual(calls, 0)
        row = SCH.find_obligation(self.state(), obligation_id=plan_id)
        self.assertEqual(row["status"], "due")
        self.assertEqual(row["deferrals"], 1)
        self.assertIn("upstream-backoff", row["last_deferral_reason"])

    def test_closure_does_not_bypass_backoff(self):
        self._fail_408()
        closure_id = self._add_obligation("E", "closure", "CONSULT",
                                          "closure", "final-digest")
        code, calls = self._execute(closure_id)
        self.assertEqual(code, 0)
        self.assertEqual(calls, 0)
        row = SCH.find_obligation(self.state(), obligation_id=closure_id)
        self.assertEqual(row["status"], "due")
        self.assertEqual(row["deferrals"], 1)

    def test_backoff_survives_process_and_session_restart(self):
        self._fail_408(900)
        # 新进程/新会话只读同一 state.json：退避状态不依赖内存、会话绑定或锁。
        fresh = G.load_state(self.state_path)
        status = G.cognition_backoff_status(fresh, model=CC.model_key())
        self.assertTrue(status["active"])
        self.assertGreater(status["remaining_seconds"], 0)
        plan_id = self._add_obligation("B", "plan", "PLAN", "planning", "p2")
        code, calls = self._execute(plan_id)
        self.assertEqual((code, calls), (0, 0))

    def test_expired_window_resumes_scheduling(self):
        self._fail_408(600)
        state = self.state()
        self._window(state)["until"] = (
            datetime.now().astimezone()
            - timedelta(seconds=1)).isoformat(timespec="seconds")
        G.save_state(self.state_path, state)
        self.assertFalse(G.cognition_backoff_status(
            self.state(), model=CC.model_key())["active"])
        plan_id = self._add_obligation("B", "plan", "PLAN", "planning", "p3")
        code, calls = self._execute(plan_id)
        self.assertEqual(code, 0)
        self.assertEqual(calls, 1)

    def test_manual_override_still_one_single_request(self):
        self._fail_408()
        calls = {"count": 0}

        def fake(*_a, **_k):
            calls["count"] += 1
            return think_result()

        with mock.patch.object(CC, "run_consult", side_effect=fake), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = CD.main(["cognition-consult", "--state", self.state_path,
                            "--materials", self.materials,
                            "--question", "本次要判断什么",
                            "--ignore-backoff"])
        self.assertEqual(code, 0)
        self.assertEqual(calls["count"], 1)
        state = self.state()
        self.assertNotIn(G.LEGACY_BACKOFF_KEY, state)
        events = [item.get("event") for item in state.get("history") or []]
        self.assertIn("cognition_backoff_overridden", events)


# ── 6. CPA 408 来源分类（模拟响应，不触碰真实 CPA 配置）─────────────────────
class Cpa408ClassificationTests(unittest.TestCase):
    def _post(self, status: int, body: bytes = b"",
              headers: dict | None = None):
        error = _http_error(status, body, headers)
        with mock.patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = error
            with self.assertRaises(CC.CognitionError) as ctx:
                CC._post_once(URL, "k", {"model": "m"}, 5)
        return ctx.exception

    def test_upstream_codex_timeout_is_stream_timeout(self):
        exc = self._post(408, _body(
            "request_timeout",
            "stream disconnected before completion: "
            "stream closed before response.completed"))
        self.assertEqual(exc.error_class, CC.UPSTREAM_STREAM_TIMEOUT)
        self.assertNotEqual(exc.error_class, CC.UPSTREAM_AUTH)
        self.assertNotEqual(exc.error_class, CC.UPSTREAM_OVERLOADED)

    def test_bare_408_without_payload_is_still_stream_timeout(self):
        # CPA 代理层/上游在无 body 时透传 408：归类为上游瞬时，不猜成认证问题。
        exc = self._post(408)
        self.assertEqual(exc.error_class, CC.UPSTREAM_STREAM_TIMEOUT)
        self.assertIsNone(exc.retry_after)

    def test_client_deadline_exceeded_code_is_stream_timeout(self):
        exc = self._post(408, _body("deadline_exceeded", "deadline exceeded"))
        self.assertIn(exc.error_class, (CC.UPSTREAM_STREAM_TIMEOUT,
                                        CC.UPSTREAM_SERVER))

    def test_local_client_timeout_has_no_http_status(self):
        with mock.patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = __import__("socket").timeout(
                "read timed out")
            with self.assertRaises(CC.CognitionError) as ctx:
                CC._post_once(URL, "k", {"model": "m"}, 5)
        exc = ctx.exception
        self.assertEqual(exc.error_class, CC.CLIENT_TIMEOUT)
        self.assertIsNone(exc.http_status)
        self.assertEqual(exc.send_observed, "not_attempted")

    def test_503_cooldown_is_not_classified_as_auth(self):
        exc = self._post(503, _body(
            "auth_unavailable", "no auth available; last upstream error: "
            "server_is_overloaded"), {"Retry-After": "194"})
        self.assertEqual(exc.error_class, CC.UPSTREAM_CREDENTIAL_COOLING)
        self.assertEqual(exc.retry_after, 194)


if __name__ == "__main__":
    unittest.main()

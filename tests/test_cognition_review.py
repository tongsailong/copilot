#!/usr/bin/env python3
"""SOL 零工具单轮调用（CONSULT / PLAN / REVIEW）：请求不变量、解析、失败收敛、单轮与边界。

v9.0.0 起 SOL 是默认认知引擎：CONSULT / PLAN 按生命周期按需发出，THINK 作为
历史名称只读兼容并归一为 CONSULT；REVIEW 只在用户明确要求时使用。两者都不
进入 Finding 台账、不影响 G1–G3（REVIEW 的 findings 仍以 advisory 进入台账）。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import v6_support as F  # noqa: E402
import governance as G  # noqa: E402
import review_policy as RP  # noqa: E402
import cognition_call as CC  # noqa: E402


def reply(**overrides) -> str:
    payload = {
        "schema": CC.REVIEW_SCHEMA,
        "verdict": "pass",
        "summary": "总体通过",
        "findings": [],
        "unverified": [],
        "materials_reviewed": ["变更"],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def think(**overrides) -> str:
    payload = {
        "schema": CC.THINK_SCHEMA,
        "judgment": "先补定向测试再合并",
        "essence": "调用链简单，真正的风险在缺少验证",
        "strategy": "先用一次定向测试锁定行为，再小范围合并",
        "tradeoffs": ["先小范围灰度会慢一步，但能限制回滚面"],
        "risks": ["缺少集成测试"],
        "priority": ["P1 验证", "P2 合并"],
        "verify": ["定向测试覆盖关键分支"],
        "reconsult": ["出现并发写入"],
        "unverified": [],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def response(text: str, *, model: str = CC.MODEL, status: str = "completed",
             output_type: str = "message", extra_output: list | None = None,
             http_status: int = 200, reasoning: dict | None = None,
             request_id: str = "", trace_id: str = "") -> dict:
    output = [{"type": output_type, "status": "completed",
               "content": [{"type": "output_text", "text": text}]}]
    output.extend(extra_output or [])
    payload = {"model": model, "status": status, "output": output,
               "usage": {"input_tokens": 10, "output_tokens": 20},
               "_http_status": http_status,
               "_request_id": request_id, "_trace_id": trace_id}
    payload["reasoning"] = ({"effort": "high", "mode": "standard"}
                            if reasoning is None else reasoning)
    return payload


class ZeroToolTests(unittest.TestCase):
    def test_payload_has_no_tool_definitions(self):
        with mock.patch.dict(os.environ, {CC.MODEL_ENV: ""}):
            payload = CC.build_payload("材料")
        self.assertNotIn("tools", payload)
        self.assertNotIn("functions", payload)
        self.assertEqual(payload["tool_choice"], "none")
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(payload["model"], "gpt-6.2-sol")
        # v9.14.0 起默认 SOL；Astra 保留为显式可选，不是自动回退目标。
        self.assertEqual(CC.DEFAULT_MODEL, "gpt-6.2-sol")
        self.assertEqual(CC.MODEL, CC.DEFAULT_MODEL)
        self.assertEqual(CC.SUPPORTED_MODELS, ("gpt-6.2-sol", "gpt-6.2-astra"))
        self.assertFalse(hasattr(CC, "LEGACY_MODEL"))

    def test_explicit_model_selection_is_not_fallback(self):
        # 显式选择：显式参数 > 环境变量 > 默认；未知模型名仍然拒绝发送。
        with mock.patch.dict(os.environ, {CC.MODEL_ENV: ""}):
            self.assertEqual(CC.resolve_model(), "gpt-6.2-sol")
        self.assertEqual(CC.resolve_model("gpt-6.2-astra"), "gpt-6.2-astra")
        with mock.patch.dict(os.environ, {CC.MODEL_ENV: "gpt-6.2-astra"}):
            self.assertEqual(CC.resolve_model(), "gpt-6.2-astra")
            # 显式参数优先于环境变量。
            self.assertEqual(CC.resolve_model("gpt-6.2-sol"), "gpt-6.2-sol")
            self.assertEqual(CC.build_payload("材料")["model"], "gpt-6.2-astra")
        for bad in ("gpt-6.2-sol-x", "gpt-6.1-sol", "gpt-6.2-unknown"):
            with mock.patch.dict(os.environ, {CC.MODEL_ENV: bad}):
                with self.assertRaises(CC.CognitionUsageError):
                    CC.resolve_model()
        # 显式选择不改变默认值，也不构成失败回退。
        self.assertEqual(CC.DEFAULT_MODEL, "gpt-6.2-sol")
        self.assertNotIn("astra", CC.model_key())

    def test_payload_omits_unsupported_sampling_params(self):
        payload = CC.build_payload("材料")
        for key in ("temperature", "top_p", "top_logprobs", "logprobs",
                    "include", "frequency_penalty", "presence_penalty"):
            self.assertNotIn(key, payload)
        with self.assertRaises(CC.CognitionUsageError) as ctx:
            CC.assert_responses_api_compat({"temperature": 0.2})
        self.assertEqual(ctx.exception.code, "cognition_unsupported_param")

    def test_effort_is_pinned_to_high_for_every_mode(self):
        for mode in CC.CALL_MODES:
            with self.subTest(mode=mode):
                payload = CC.build_payload("材料", mode=mode)
                self.assertEqual(payload["reasoning"], {"effort": "high"})
                self.assertEqual(payload["model"], CC.DEFAULT_MODEL)
                self.assertEqual(payload["tool_choice"], "none")
                self.assertNotIn("tools", payload)
        for effort in ("medium", "low", "xhigh", "max", ""):
            with self.subTest(effort=effort):
                with self.assertRaises(CC.CognitionUsageError):
                    CC.build_payload("材料", effort=effort)

    def test_mode_instructions_are_distinct_and_zero_tool(self):
        consult = CC.build_payload("材料", mode="CONSULT")["instructions"]
        plan = CC.build_payload("材料", mode="PLAN")["instructions"]
        review = CC.build_payload("材料", mode="REVIEW")["instructions"]
        self.assertNotEqual(consult, plan)
        self.assertNotEqual(consult, review)
        self.assertIn("CONSULT", consult)
        self.assertIn("PLAN", plan)
        self.assertIn("REVIEW", review)
        for text in (consult, plan, review):
            self.assertIn("没有、也不能使用任何工具", text)
            self.assertIn("zcode", text.lower())
        # THINK 是历史别名：只归一为 CONSULT，不产生第三套指令。
        self.assertEqual(
            CC.build_payload("材料", mode="THINK")["instructions"], consult)
        # 默认模式就是默认认知引擎：不传 mode 时构造 CONSULT 请求。
        self.assertEqual(CC.build_payload("材料")["instructions"], consult)

    def test_unknown_mode_fails_closed(self):
        for mode in ("DECIDE", "decide", "", "REVIEWER"):
            with self.subTest(mode=mode):
                with self.assertRaises(CC.CognitionUsageError):
                    CC.build_payload("材料", mode=mode)
        self.assertNotIn("DECIDE", CC.CALL_MODES)

    def test_injected_tools_fail_closed(self):
        for key in ("tools", "functions", "parallel_tool_calls"):
            with self.subTest(key=key):
                with self.assertRaises(CC.CognitionUsageError):
                    CC.assert_zero_tool({"model": "m", "tool_choice": "none",
                                         key: []})
        with self.assertRaises(CC.CognitionUsageError):
            CC.assert_zero_tool({"model": "m", "tool_choice": "auto"})

    def test_run_review_sends_exactly_one_zero_tool_request(self):
        calls = []

        def fake(url, key, body, timeout):
            calls.append(body)
            return response(reply())

        result = CC.run_review("材料", transport=fake, api_key="k")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("tools", calls[0])
        self.assertEqual(calls[0]["tool_choice"], "none")
        self.assertEqual(calls[0]["reasoning"], {"effort": "high"})
        self.assertTrue(result["zero_tool"])
        self.assertEqual(result["attempts"], 1)

    def test_run_think_sends_exactly_one_zero_tool_high_request(self):
        calls = []

        def fake(url, key, body, timeout):
            calls.append(body)
            return response(think())

        # THINK 是历史别名：请求仍是 CONSULT 语义，结果模式归一为 CONSULT。
        result = CC.run_think("材料", transport=fake, api_key="k")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], CC.DEFAULT_MODEL)
        self.assertEqual(calls[0]["reasoning"], {"effort": "high"})
        self.assertNotIn("tools", calls[0])
        self.assertEqual(calls[0]["tool_choice"], "none")
        self.assertEqual(result["mode"], "CONSULT")
        self.assertTrue(result["zero_tool"])
        self.assertEqual(result["judgment"], "先补定向测试再合并")

    def test_legacy_thinks_normalize_to_consult(self):
        """THINK 是历史名称：只归一为 CONSULT，不再产生第三套流程。"""
        self.assertEqual(CC.normalize_mode("THINK"), "CONSULT")
        self.assertEqual(CC.normalize_mode("think"), "CONSULT")
        self.assertEqual(CC.normalize_mode("CONSULT"), "CONSULT")
        self.assertEqual(CC.normalize_mode("plan"), "PLAN")
        self.assertEqual(CC.normalize_mode("REVIEW"), "REVIEW")
        self.assertNotIn("THINK", CC.CALL_MODES)
        self.assertEqual(CC.COGNITIVE_MODES, ("CONSULT", "PLAN"))


class ParseTests(unittest.TestCase):
    def test_valid_json_is_parsed(self):
        parsed = CC.parse_review_output(reply(
            verdict="partial",
            findings=[
                {"id": "F2", "severity": "minor", "summary": "小问题"},
                {"id": "F1", "severity": "blocking", "file": "a.py",
                 "line": 3, "summary": "严重问题"},
            ]))
        self.assertEqual(parsed["verdict"], "partial")
        self.assertEqual([item["severity"] for item in parsed["findings"]],
                         ["blocking", "minor"])

    def test_single_fence_is_accepted(self):
        parsed = CC.parse_review_output("```json\n" + reply() + "\n```")
        self.assertEqual(parsed["verdict"], "pass")

    def test_surrounding_prose_with_one_schema_object_is_accepted(self):
        parsed = CC.parse_review_output("说明如下：\n" + reply() + "\n以上。")
        self.assertEqual(parsed["verdict"], "pass")

    def test_pass_with_blocking_is_downgraded(self):
        parsed = CC.parse_review_output(reply(findings=[
            {"id": "F1", "severity": "blocking", "summary": "问题"}]))
        self.assertEqual(parsed["verdict"], "partial")
        self.assertIn("pass_with_blocking_or_major", parsed["verdict_adjusted"])

    def test_pass_with_unverified_is_downgraded(self):
        parsed = CC.parse_review_output(reply(unverified=["未看到调用方"]))
        self.assertEqual(parsed["verdict"], "partial")
        self.assertIn("pass_with_unverified", parsed["verdict_adjusted"])

    def test_ambiguous_or_invalid_output_is_rejected(self):
        cases = {
            "ambiguous": reply() + "\n" + reply(),
            "schema": json.dumps({"schema": "other", "verdict": "pass"}),
            "verdict": reply(verdict="ok"),
            "severity": reply(findings=[{"id": "F1", "severity": "huge"}]),
            "empty": "   ",
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(CC.CognitionResponseError):
                    CC.parse_review_output(text)


class ThinkParseTests(unittest.TestCase):
    def test_think_structure_is_parsed(self):
        parsed = CC.parse_think_output(think())
        self.assertEqual(parsed["judgment"], "先补定向测试再合并")
        self.assertEqual(parsed["tradeoffs"],
                         ["先小范围灰度会慢一步，但能限制回滚面"])
        self.assertEqual(parsed["verify"], ["定向测试覆盖关键分支"])
        fenced = CC.parse_think_output("```json\n" + think() + "\n```")
        self.assertEqual(fenced["strategy"],
                         "先用一次定向测试锁定行为，再小范围合并")

    def test_schema_must_match(self):
        with self.assertRaises(CC.CognitionResponseError):
            CC.parse_think_output(json.dumps({"schema": "other",
                                              "judgment": "结论"}))

    def test_think_fields_are_strict(self):
        cases = {
            "risks_string": think(risks="风险"),
            "tradeoff_object": think(tradeoffs=[{"x": 1}]),
            "judgment_number": think(judgment=3),
            "empty": think(judgment="", strategy=""),
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(CC.CognitionResponseError):
                    CC.parse_think_output(text)


class FailureTests(unittest.TestCase):
    def test_identity_mismatch_is_rejected(self):
        with self.assertRaises(CC.CognitionIdentityError):
            CC.run_review("材料", transport=lambda *a: response(
                reply(), model="gpt-other"), api_key="k")

    def test_non_benign_output_is_security_event(self):
        with self.assertRaises(CC.CognitionToolCallError):
            CC.run_review("材料", transport=lambda *a: response(
                reply(), extra_output=[{"type": "function_call",
                                        "name": "run"}]), api_key="k")

    def test_incomplete_response_is_rejected(self):
        with self.assertRaises(CC.CognitionResponseError):
            CC.run_review("材料", transport=lambda *a: response(
                reply(), status="incomplete"), api_key="k")

    def test_reasoning_effort_echo_mismatch_is_rejected(self):
        with self.assertRaises(CC.CognitionReasoningError) as ctx:
            CC.run_review("材料", transport=lambda *a: response(
                reply(), reasoning={"effort": "medium", "mode": "standard"}),
                api_key="k")
        self.assertEqual(ctx.exception.code, "cognition_reasoning_effort_mismatch")

    def test_reasoning_mode_override_is_rejected(self):
        with self.assertRaises(CC.CognitionReasoningError) as ctx:
            CC.run_review("材料", transport=lambda *a: response(
                reply(), reasoning={"effort": "high", "mode": "pro"}),
                api_key="k")
        self.assertEqual(ctx.exception.code, "cognition_reasoning_mode_mismatch")

    def test_reasoning_echo_is_recorded_when_verified(self):
        result = CC.run_review(
            "材料", transport=lambda *a: response(
                reply(), request_id="req-1", trace_id="trace-1"), api_key="k")
        self.assertTrue(result["reasoning_verified"])
        self.assertEqual(result["reasoning_resolved"]["effort"], "high")
        self.assertEqual(result["reasoning_mode"], "standard")
        self.assertEqual(result["reasoning_effort"], "high")
        self.assertEqual(result["request_id"], "req-1")
        self.assertEqual(result["trace_id"], "trace-1")

    def test_echoed_tools_are_a_security_event(self):
        def fake(*args):
            payload = response(reply())
            payload["tools"] = [{"type": "function", "name": "shell"}]
            return payload

        with self.assertRaises(CC.CognitionToolCallError):
            CC.run_review("材料", transport=fake, api_key="k")

    def test_zero_tool_usage_echo_is_not_an_event(self):
        # GPT-6.2 固定回显 tools=[] 与全零 tool_usage；这不是工具使用。
        def fake(*args):
            payload = response(reply())
            payload["tools"] = []
            payload["tool_usage"] = {
                "image_gen": {"input_tokens": 0, "output_tokens": 0,
                              "input_tokens_details": {"text_tokens": 0}},
                "web_search": {"num_requests": 0},
            }
            return payload

        result = CC.run_review("材料", transport=fake, api_key="k")
        self.assertEqual(result["verdict"], "pass")

    def test_nonzero_tool_usage_is_an_event(self):
        def fake(*args):
            payload = response(reply())
            payload["tool_usage"] = {"web_search": {"num_requests": 1}}
            return payload

        with self.assertRaises(CC.CognitionToolCallError):
            CC.run_review("材料", transport=fake, api_key="k")

    def test_failure_is_not_retried(self):
        calls = []

        def failing(url, key, body, timeout):
            calls.append(1)
            raise CC.CognitionUpstreamError("cognition_upstream_error", "boom")

        with tempfile.TemporaryDirectory() as tmp:
            audit = os.path.join(tmp, "audit.jsonl")
            with self.assertRaises(CC.CognitionUpstreamError):
                CC.run_review("材料", transport=failing, api_key="k",
                              audit_log=audit)
            self.assertEqual(len(calls), 1)
            lines = open(audit, encoding="utf-8").read().splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertFalse(record["ok"])
            self.assertEqual(record["error"], "cognition_upstream_error")

    def test_materials_too_large_is_rejected(self):
        with self.assertRaises(CC.CognitionUsageError):
            CC.run_review("x" * (CC.MAX_MATERIALS_CHARS + 1),
                          transport=lambda *a: response(reply()), api_key="k")

    def test_missing_credentials_fail_closed(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(CC, "_read_env_value", return_value=None), \
                mock.patch.object(CC, "_read_zcode_provider_key",
                                  return_value=None):
            os.environ.pop(CC.API_KEY_ENV, None)
            with self.assertRaises(CC.CognitionUsageError):
                CC.run_review("材料", transport=lambda *a: response(reply()))

    def test_http_429_is_quota_not_retry(self):
        error = urllib.error.HTTPError(
            "http://127.0.0.1:8317/v1/responses", 429, "Too Many Requests",
            {}, io.BytesIO(b'{"error": {"code": "usage_limit_reached"}}'))
        with mock.patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = error
            with self.assertRaises(CC.CognitionQuotaError):
                CC._post_once("http://127.0.0.1:8317/v1/responses", "k",
                              {"model": "m"}, 5)
            self.assertEqual(build.return_value.open.call_count, 1)

    def test_timeout_is_not_retried(self):
        with mock.patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = TimeoutError("timeout")
            with self.assertRaises(CC.CognitionTimeoutError):
                CC._post_once("http://127.0.0.1:8317/v1/responses", "k",
                              {"model": "m"}, 5)
            self.assertEqual(build.return_value.open.call_count, 1)


class SingleRoundTests(unittest.TestCase):
    def test_same_materials_reuse_existing_review(self):
        state = {"sol_reviews": [{"status": "completed",
                                  "model": CC.DEFAULT_MODEL,
                                  "materials_sha256": "a" * 64,
                                  "review_id": "SR1"}]}
        decision = CC.single_round_decision(state, "a" * 64, mode="REVIEW")
        self.assertEqual(decision["action"], "reuse")

    def test_first_review_needs_no_reason(self):
        self.assertEqual(
            CC.single_round_decision({}, "a" * 64,
                                     mode="REVIEW")["action"], "call")

    def test_second_review_requires_explicit_reason(self):
        state = {"sol_reviews": [{"status": "completed",
                                  "model": CC.DEFAULT_MODEL,
                                  "materials_sha256": "a" * 64}]}
        refused = CC.single_round_decision(state, "b" * 64, mode="REVIEW")
        self.assertEqual(refused["action"], "refuse")
        self.assertEqual(refused["code"], "cognition_review_requires_reason")
        invalid = CC.single_round_decision(
            state, "b" * 64, force=True, trigger="because", reason="r",
            mode="REVIEW")
        self.assertEqual(invalid["code"], "cognition_call_trigger_invalid")
        missing = CC.single_round_decision(
            state, "b" * 64, force=True, trigger="scope-changed", reason="",
            mode="REVIEW")
        self.assertEqual(missing["code"], "cognition_call_reason_missing")
        allowed = CC.single_round_decision(
            state, "b" * 64, force=True, trigger="scope-changed",
            reason="范围扩大", mode="REVIEW")
        self.assertEqual(allowed["action"], "call")

    def test_modes_do_not_share_or_reuse_records(self):
        review_only = {
            "sol_reviews": [{"status": "completed", "mode": "REVIEW",
                             "model": CC.DEFAULT_MODEL,
                             "materials_sha256": "a" * 64,
                             "review_id": "SR1"}],
            "sol_advisories": [],
        }
        # 同一材料已 REVIEW ≠ THINK 已调用：各自独立首次调用。
        decision = CC.single_round_decision(
            review_only, "a" * 64, records_key="sol_advisories", mode="THINK")
        self.assertEqual(decision["action"], "call")
        state = {
            **review_only,
            "sol_advisories": [{"status": "completed", "mode": "CONSULT",
                                "model": CC.DEFAULT_MODEL,
                                "materials_sha256": "b" * 64,
                                "advisory_id": "SA1"}],
        }
        # 历史 CONSULT 记录就是同一份认知结论：THINK 直接复用，不再重复付费。
        reuse = CC.single_round_decision(
            state, "b" * 64, records_key="sol_advisories", mode="THINK")
        self.assertEqual(reuse["action"], "reuse")
        self.assertEqual(reuse["review"]["advisory_id"], "SA1")
        # 认知结论不能替代独立复核：REVIEW 仍需自己的一次请求。
        self.assertEqual(CC.single_round_decision(
            state, "b" * 64, records_key="sol_advisories",
            mode="REVIEW")["action"], "call")
        review_reuse = CC.single_round_decision(state, "a" * 64,
                                                mode="REVIEW")
        self.assertEqual(review_reuse["action"], "reuse")


class StateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state_path = os.path.join(self.dir, "state.json")
        F.init_git_workspace(self.dir)
        G.start_state(self.state_path, goal="目标", contract=F.CONTRACT,
                      risk="medium", task_id="sol-task")

    def record(self, findings, verdict="partial") -> dict:
        record = {
            "schema": CC.REVIEW_SCHEMA, "review_id": "CC" + "a" * 24,
            "verdict": verdict, "summary": "结论", "findings": findings,
            "unverified": [],
            "findings_count": {"blocking": 0, "major": 0, "minor": 0,
                               "info": 0},
            "completed_at": G.now_iso(),
        }
        return G.record_cognition_call(self.state_path, record, mode="REVIEW",
                                   materials_sha256="b" * 64)

    def test_findings_are_advisory_and_dismissible(self):
        self.record([{"id": "F1", "severity": "major", "file": "a.py",
                      "line": 1, "summary": "可疑"}])
        state = G.load_state(self.state_path)
        self.assertEqual(G.list_findings(state)["blocking"], ["F1"])
        item = G.record_finding_status(
            self.state_path, "F1", "dropped",
            note="SOL 误报：已确认调用方语义正确")
        self.assertEqual(item["status"], "dropped")
        self.assertEqual(item["authority"], "advisory")
        state = G.load_state(self.state_path)
        self.assertEqual(G.list_findings(state)["blocking"], [])

    def test_formal_findings_cannot_be_dismissed(self):
        state = G.load_state(self.state_path)
        state["finding_ledger"] = {"schema": RP.LEDGER_SCHEMA, "next_id": 2,
                                   "items": [{
                                       "finding_id": "F1", "severity": "major",
                                       "status": "open", "authority":
                                       "authoritative"}]}
        G.save_state(self.state_path, state)
        with self.assertRaises(G.GovernanceError):
            G.record_finding_status(self.state_path, "F1", "dropped",
                                    note="想绕过")

    def test_cognition_call_is_advisory_not_a_closure_gate(self):
        self.record([], verdict="pass")
        state = G.load_state(self.state_path)
        state["risk"] = "high"
        check = G.closure_check(state)
        # G4 退役：门禁只有 G1–G3，SOL 记录不改变门禁集合。
        self.assertEqual(set(check["gates"]), {"G1", "G2", "G3"})
        self.assertNotIn("G4", check["gates"])
        self.assertEqual(state["cognition_calls"][0]["status"], "completed")

    def test_duplicate_cognition_record_does_not_double_count(self):
        record = {
            "schema": CC.REVIEW_SCHEMA, "review_id": "CC" + "c" * 24,
            "verdict": "partial", "summary": "结论",
            "reply_sha256": "e" * 64,
            "findings": [{"id": "F1", "severity": "major", "file": "a.py",
                          "line": 1, "summary": "可疑"}],
            "unverified": [], "findings_count": {"major": 1},
            "completed_at": G.now_iso(),
        }
        G.record_cognition_call(self.state_path, record, mode="REVIEW",
                            materials_sha256="b" * 64)
        G.record_cognition_call(self.state_path, record, mode="REVIEW",
                            materials_sha256="b" * 64)
        state = G.load_state(self.state_path)
        item = G.list_findings(state)["items"][0]
        self.assertEqual(item["occurrences"], 1)
        self.assertEqual(item["first_review_sha256"], "e" * 64)

    def test_failure_is_recorded_without_blocking_task(self):
        failure = G.record_cognition_call_failure(
            self.state_path, error_code="cognition_timeout", detail="300s",
            materials_sha256="c" * 64)
        self.assertEqual(failure["status"], "failed")
        state = G.load_state(self.state_path)
        self.assertEqual(state["cognition_calls"][-1]["error"], "cognition_timeout")
        self.assertEqual(G.evaluate(state)["phase"], "PARTIAL")

    def test_review_can_be_recorded_while_blocked(self):
        G.add_block(self.state_path, "等待用户授权", "用户确认")
        record = self.record([], verdict="pass")
        self.assertEqual(record["status"], "completed")
        state = G.load_state(self.state_path)
        self.assertEqual(G.evaluate(state)["phase"], "BLOCKED")
        self.assertTrue(state["cognition_calls"])

    def test_think_is_advisory_only(self):
        record = {
            "schema": CC.THINK_SCHEMA, "call_id": "SC" + "a" * 24,
            "mode": "THINK", "summary": "判断", "judgment": "先补测试",
            "essence": "缺少验证", "strategy": "先测后改",
            "tradeoffs": [], "risks": ["风险"], "priority": ["P1"],
            "verify": [], "reconsult": [], "unverified": [],
            "completed_at": G.now_iso(),
        }
        G.record_cognition_call(self.state_path, record, mode="THINK",
                          materials_sha256="c" * 64)
        state = G.load_state(self.state_path)
        self.assertEqual(state["cognition_calls"][-1]["status"], "completed")
        # 不进入 Finding 台账、不影响阶段、不改变 G1–G3。
        self.assertEqual(G.list_findings(state)["items"], [])
        self.assertEqual(G.evaluate(state)["phase"], "PARTIAL")
        check = G.closure_check(state)
        self.assertEqual(set(check["gates"]), {"G1", "G2", "G3"})

    def test_think_failure_is_recorded_without_blocking(self):
        failure = G.record_cognition_call_failure(
            self.state_path, mode="THINK", error_code="cognition_timeout",
            detail="300s", materials_sha256="d" * 64)
        self.assertEqual(failure["status"], "failed")
        state = G.load_state(self.state_path)
        self.assertEqual(state["cognition_calls"][-1]["error"], "cognition_timeout")
        self.assertEqual(G.evaluate(state)["phase"], "PARTIAL")


class AdmissionTests(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(CC.admission(risk="low")["decision"], "not_needed")
        self.assertEqual(CC.admission(risk="medium")["decision"], "optional")
        self.assertEqual(CC.admission(risk="high")["decision"], "recommended")
        self.assertNotIn("g4_separate", CC.admission(risk="high"))

    def test_trigger_validation(self):
        self.assertEqual(CC.admission(risk="low",
                                      triggers=["security"])["decision"],
                         "recommended")
        with self.assertRaises(CC.CognitionUsageError):
            CC.admission(risk="medium", triggers=["nonsense"])


class HardeningTests(unittest.TestCase):
    """SOL 独立复核发现的问题的回归：权威性、重定向、严格解析、传输异常、凭据。"""

    def test_advisory_does_not_downgrade_legacy_findings(self):
        state = {"finding_ledger": {"schema": RP.LEDGER_SCHEMA, "next_id": 2,
                                    "items": [{
                                        "finding_id": "F1", "severity": "major",
                                        "status": "open", "file": "a.py",
                                        "summary": "历史正式问题"}]}}
        RP.merge_review(state, {
            "result_sha256": "1" * 64, "verdict": "partial",
            "findings": [{"id": "F1", "severity": "major", "file": "a.py",
                          "summary": "历史正式问题"}]}, authoritative=False)
        item = RP.get_item(state, "F1")
        self.assertEqual(item["authority"], "authoritative")
        with self.assertRaises(RP.ReviewPolicyError):
            RP.transition(state, "F1", "dropped", note="想绕过")

    def test_redirects_are_not_followed(self):
        self.assertIsNone(CC._NoRedirect().redirect_request(
            None, None, 302, "Found", {}, "http://evil.example/v1"))
        opened = []

        class Opener:
            def open(self, request, timeout=None):
                opened.append(request.full_url)
                raise urllib.error.HTTPError(
                    request.full_url, 302, "Found",
                    {"Location": "http://evil.example/v1"}, io.BytesIO(b""))

        with mock.patch("urllib.request.build_opener", return_value=Opener()):
            with self.assertRaises(CC.CognitionUpstreamError):
                CC._post_once("http://127.0.0.1:8317/v1/responses", "k",
                              {"model": "m"}, 5)
        self.assertEqual(len(opened), 1)

    def test_partial_read_is_transport_error(self):
        import http.client as http_client

        class Broken:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                raise http_client.IncompleteRead(b"partial")

        class Opener:
            def open(self, request, timeout=None):
                return Broken()

        with mock.patch("urllib.request.build_opener", return_value=Opener()):
            with self.assertRaises(CC.CognitionUpstreamError) as ctx:
                CC._post_once("http://127.0.0.1:8317/v1/responses", "k",
                              {"model": "m"}, 5)
        self.assertEqual(ctx.exception.code, "cognition_transport_error")

    def test_strict_unverified_and_finding_fields(self):
        cases = {
            "unverified_object": reply(unverified=[{"detail": "未验证"}]),
            "unverified_string": reply(unverified="未验证"),
            "materials_object": reply(materials_reviewed=[{"x": 1}]),
            "finding_summary_object": reply(findings=[
                {"id": "F1", "severity": "major", "summary": {"x": 1}}]),
            "finding_id_object": reply(findings=[
                {"id": {"x": 1}, "severity": "major", "summary": "s"}]),
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(CC.CognitionResponseError):
                    CC.parse_review_output(text)

    def test_provider_key_requires_local_cpa_endpoint(self):
        def config(base_url: str) -> str:
            path = tempfile.mktemp(suffix=".json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"config": {"providerConfigRules": {
                    "providerRules": [{"config": {
                        "api": {"baseUrl": base_url},
                        "access": {"apiKey": "k"}}}]}}}, fh)
            self.addCleanup(os.unlink, path)
            return path

        self.assertEqual(CC._read_zcode_provider_key(
            config("http://127.0.0.1:8317/v1")), "k")
        self.assertIsNone(CC._read_zcode_provider_key(
            config("http://remote.example:8317/v1")))
        self.assertIsNone(CC._read_zcode_provider_key(
            config("http://127.0.0.1:9999/v1")))
        self.assertIsNone(CC._read_zcode_provider_key(
            config("http://127.0.0.1:8317@evil.example/v1")))

    def test_base_url_rejects_userinfo_and_remote_hosts(self):
        with mock.patch.dict(os.environ,
                             {"COPILOT_COGNITION_BASE_URL":
                              "http://127.0.0.1:8317@evil.example/v1"}):
            with self.assertRaises(CC.CognitionUsageError):
                CC.resolve_base_url()
        with mock.patch.dict(os.environ,
                             {"COPILOT_COGNITION_BASE_URL":
                              "http://127.0.0.1:9999/v1"}):
            with self.assertRaises(CC.CognitionUsageError):
                CC.resolve_base_url()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state = os.path.join(self.dir, "state.json")
        self.materials = os.path.join(self.dir, "materials.md")
        F.init_git_workspace(self.dir)
        with open(os.path.join(self.dir, "work.py"), "w", encoding="utf-8") as fh:
            fh.write("VALUE = 1\n")
        G.start_state(self.state, goal="目标", contract=F.CONTRACT,
                      risk="medium", task_id="sol-cli")
        with open(self.materials, "w", encoding="utf-8") as fh:
            fh.write("## 变更\n- VALUE = 1\n+ VALUE = 2\n")

    def test_dry_run_is_zero_tool_and_does_not_write(self):
        before = open(self.state, "rb").read()
        proc = F.run_cli("copilot_dispatch.py", "cognition-review", "--state",
                         self.state, "--materials", self.materials,
                         "--dry-run", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["zero_tool"])
        self.assertEqual(payload["tool_keys_present"], [])
        self.assertEqual(payload["model"], CC.DEFAULT_MODEL)
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["unsupported_params_present"], [])
        self.assertEqual(open(self.state, "rb").read(), before)

    def test_think_dry_run_is_zero_tool_and_does_not_write(self):
        before = open(self.state, "rb").read()
        # cognition-consult 是历史别名，语义等同 cognition-consult：dry-run 报 CONSULT。
        proc = F.run_cli("copilot_dispatch.py", "cognition-consult",
                         "--state", self.state,
                         "--materials", self.materials,
                         "--question", "该方案是否可行？",
                         "--dry-run", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["mode"], "CONSULT")
        self.assertEqual(payload["model"], CC.DEFAULT_MODEL)
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertTrue(payload["zero_tool"])
        self.assertEqual(payload["tool_keys_present"], [])
        self.assertEqual(open(self.state, "rb").read(), before)

    def test_think_record_is_stored_outside_findings(self):
        import argparse
        import contextlib
        import io
        import copilot_dispatch as CD
        result = {
            "schema": CC.THINK_SCHEMA, "mode": "CONSULT",
            "model": CC.MODEL, "model_resolved": CC.MODEL,
            "reasoning_effort": "high", "reasoning_mode": "standard",
            "reasoning_resolved": {"effort": "high"}, "reasoning_verified": True,
            "zero_tool": True, "attempts": 1, "request_id": "req-1",
            "trace_id": "trace-1", "response_id": "resp-1",
            "materials_sha256": "e" * 64, "materials_chars": 10,
            "request_sha256": "r" * 64, "reply_text": think(),
            "reply_sha256": "a" * 64, "reply_chars": 10, "usage": {},
            "usage_available": True, "latency_ms": 1,
            "judgment": "先补测试", "essence": "缺少验证", "strategy": "先测后改",
            "tradeoffs": [], "risks": [], "priority": [],
            "verify": [], "reconsult": [], "unverified": [],
            "completed_at": G.now_iso(),
        }
        args = argparse.Namespace(
            state=self.state, materials=self.materials,
            question="该方案是否可行？", stage="", reason="", trigger="",
            force=False, dry_run=False, list=False, json=True, timeout=5,
            max_output_tokens=100, audit_log="", output_dir="",
            knowledge_file="")
        out = io.StringIO()
        with mock.patch("cognition_call.run_consult", return_value=dict(result)), \
                contextlib.redirect_stdout(out):
            self.assertEqual(CD.cmd_cognition_consult(args), 0)
        state = G.load_state(self.state)
        self.assertEqual(state["cognition_calls"][-1]["mode"], "CONSULT")
        self.assertEqual(state["cognition_calls"][-1]["request_id"], "req-1")
        self.assertEqual([r for r in state["cognition_calls"]
                          if r.get("mode") == "REVIEW"], [])
        self.assertEqual(G.list_findings(state)["items"], [])
        self.assertTrue(os.path.isdir(os.path.join(self.dir, "cognition-consult")))

    def test_think_list_is_read_only(self):
        G.record_cognition_call_failure(
            self.state, mode="THINK", error_code="cognition_quota",
            materials_sha256="f" * 64)
        proc = F.run_cli("copilot_dispatch.py", "cognition-consult", "--state",
                         self.state, "--list", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(len(payload["cognition_calls"]), 1)
        self.assertEqual(payload["cognition_calls"][0]["mode"], "THINK")

    def test_same_materials_reuse_without_network(self):
        import argparse
        import contextlib
        import io
        import copilot_dispatch as CD
        state = G.load_state(self.state)
        materials_text = open(self.materials, encoding="utf-8").read()
        digest = CC.review_subject_sha256(state, materials_text, mode="REVIEW")
        G.record_cognition_call(self.state, {
            "schema": CC.REVIEW_SCHEMA, "review_id": "CC" + "b" * 24,
            "model": CC.DEFAULT_MODEL, "model_key": CC.model_key(),
            "provider": CC.PROVIDER,
            "verdict": "pass", "summary": "ok", "findings": [],
            "unverified": [], "findings_count": {}, "completed_at": G.now_iso(),
        }, mode="REVIEW", materials_sha256=digest)
        args = argparse.Namespace(
            state=self.state, materials=self.materials, reason="", trigger="",
            force=False, dry_run=False, list=False, json=True, timeout=5,
            max_output_tokens=100, audit_log="", output_dir="")
        out = io.StringIO()
        with mock.patch("cognition_call.run_review",
                        side_effect=AssertionError("must not call")) as call, \
                contextlib.redirect_stdout(out):
            self.assertEqual(CD.cmd_cognition_review(args), 0)
        self.assertEqual(call.call_count, 0)
        self.assertEqual(json.loads(out.getvalue())["call_id"],
                         "CC" + "b" * 24)

    def test_status_change_does_not_create_new_subject(self):
        state = G.load_state(self.state)
        materials_text = open(self.materials, encoding="utf-8").read()
        before = CC.review_subject_sha256(state, materials_text)
        state["contract"]["criteria"][0]["status"] = "met"
        after = CC.review_subject_sha256(state, materials_text)
        self.assertEqual(before, after)
        self.assertNotEqual(
            before, CC.review_subject_sha256(state, materials_text + "\n+extra"))

    def test_list_shows_completed_and_failed(self):
        G.record_cognition_call_failure(self.state, mode="REVIEW",
                                    error_code="cognition_quota",
                                    materials_sha256="d" * 64)
        proc = F.run_cli("copilot_dispatch.py", "cognition-review", "--state",
                         self.state, "--list", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(len(payload["cognition_calls"]), 1)
        self.assertEqual(payload["cognition_calls"][0]["error"], "cognition_quota")

    def test_audit_has_no_prompt_text_or_key(self):
        text = "SECRET-PROMPT-BODY"
        dummy_credential = "placeholder-credential-value"
        with tempfile.TemporaryDirectory() as tmp:
            audit = os.path.join(tmp, "audit.jsonl")
            CC.run_review(text, transport=lambda *a: response(reply()),
                          api_key=dummy_credential, audit_log=audit)
            content = open(audit, encoding="utf-8").read()
        self.assertNotIn("SECRET-PROMPT-BODY", content)
        self.assertNotIn(dummy_credential, content)
        record = json.loads(content.splitlines()[0])
        self.assertEqual(record["materials_sha256"],
                         hashlib.sha256(text.encode("utf-8")).hexdigest())


class HardeningV801Tests(unittest.TestCase):
    def test_base_url_parameter_cannot_bypass_local_route(self):
        sent = []

        def fake(*args):
            sent.append(args)
            return response(reply())

        with self.assertRaises(CC.CognitionUsageError) as ctx:
            CC.run_review("材料", transport=fake, api_key="k",
                          base_url="https://example.com:8317/v1")
        self.assertEqual(ctx.exception.code, "cognition_route_not_local")
        self.assertEqual(sent, [])

    def test_route_error_never_echoes_userinfo(self):
        with self.assertRaises(CC.CognitionUsageError) as ctx:
            CC.run_review("材料", transport=lambda *a: response(reply()),
                          api_key="k",
                          base_url="http://alice:s3cret@evil.example:8317/v1")
        detail = ctx.exception.detail
        self.assertNotIn("s3cret", detail)
        self.assertNotIn("alice:", detail)

    def test_dry_run_material_validation_is_shared(self):
        with self.assertRaises(CC.CognitionUsageError) as ctx:
            CC.build_payload("x" * (CC.MAX_MATERIALS_CHARS + 1))
        self.assertEqual(ctx.exception.code, "cognition_materials_too_large")
        with self.assertRaises(CC.CognitionUsageError) as empty:
            CC.build_payload("   ")
        self.assertEqual(empty.exception.code, "cognition_materials_empty")

    def test_duplicate_finding_ids_are_normalized_unique(self):
        parsed = CC.normalize_review({
            "schema": CC.REVIEW_SCHEMA, "verdict": "partial",
            "findings": [
                {"id": "F2", "severity": "minor", "summary": "a"},
                {"id": "F2", "severity": "minor", "summary": "b"},
                {"id": "F2", "severity": "minor", "summary": "c"},
            ],
        })
        ids = [item["id"] for item in parsed["findings"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_audit_failure_does_not_discard_successful_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_audit = os.path.join(tmp, "audit-dir")
            os.makedirs(bad_audit)
            result = CC.run_review(
                "材料", transport=lambda *a: response(reply()),
                api_key="k", audit_log=bad_audit)
        self.assertEqual(result["verdict"], "pass")
        self.assertFalse(result["audit_written"])
        self.assertEqual(result["audit_error"], "cognition_audit_write_failed")


class PendingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state_path = os.path.join(self.dir, "state.json")
        F.init_git_workspace(self.dir)
        G.start_state(self.state_path, goal="目标", contract=F.CONTRACT,
                      risk="medium", task_id="pending-task")
        self.materials = "材料正文"
        mat_path = os.path.join(self.dir, "materials.md")
        with open(mat_path, "w", encoding="utf-8") as fh:
            fh.write(self.materials)
        self.mat_path = mat_path

    def test_pending_result_is_recorded_without_new_request(self):
        import copilot_dispatch as D

        state = G.load_state(self.state_path)
        digest = CC.review_subject_sha256(state, self.materials, mode="REVIEW")
        result = {
            "schema": CC.REVIEW_SCHEMA, "model": CC.MODEL,
            "model_resolved": CC.MODEL, "reasoning_effort": "high",
            "zero_tool": True, "attempts": 1,
            # 真实 run_cognition() 记录的是提示词文本哈希，与 subject 摘要不同；
            # pending 恢复必须按 subject_sha256 匹配，不能拿这两个值混用。
            "subject_sha256": digest,
            "materials_sha256": "p" * 64,
            "materials_chars": len(self.materials),
            "request_sha256": "r" * 64, "reply_text": reply(),
            "reply_sha256": "a" * 64, "reply_chars": 10,
            "usage": {}, "usage_available": True, "latency_ms": 1,
            "verdict": "pass", "summary": "ok", "findings": [],
            "unverified": [], "materials_reviewed": [],
            "verdict_adjusted": [],
            "findings_count": {"blocking": 0, "major": 0, "minor": 0,
                               "info": 0},
            "completed_at": G.now_iso(),
        }
        pending = D._pending_result_path(self.dir, digest)
        os.makedirs(os.path.dirname(pending), exist_ok=True)
        with open(pending, "w", encoding="utf-8") as fh:
            json.dump(result, fh)
        with mock.patch.object(CC, "run_review",
                               side_effect=AssertionError("不应再次请求")):
            code = D.main(["cognition-review", "--state", self.state_path,
                           "--materials", self.mat_path])
        self.assertEqual(code, 0)
        record = G.load_state(self.state_path)["cognition_calls"][-1]
        self.assertEqual(record["status"], "completed")
        self.assertTrue(record["recovered_from_pending"])
        self.assertFalse(os.path.exists(pending))

    def test_legacy_pending_is_recovered_by_prompt_hash(self):
        import copilot_dispatch as D

        state = G.load_state(self.state_path)
        digest = CC.review_subject_sha256(state, self.materials, mode="REVIEW")
        prompt = CC.build_prompt(state, self.materials, mode="REVIEW")
        result = {
            "schema": CC.REVIEW_SCHEMA, "model": CC.MODEL,
            "model_resolved": CC.MODEL, "reasoning_effort": "high",
            "zero_tool": True, "attempts": 1,
            # 旧格式没有 subject_sha256，只能靠提示词文本哈希恢复。
            "materials_sha256": hashlib.sha256(
                prompt.encode("utf-8")).hexdigest(),
            "materials_chars": len(prompt),
            "request_sha256": "r" * 64, "reply_text": reply(),
            "reply_sha256": "a" * 64, "reply_chars": 10,
            "usage": {}, "usage_available": True, "latency_ms": 1,
            "verdict": "pass", "summary": "ok", "findings": [],
            "unverified": [], "materials_reviewed": [],
            "verdict_adjusted": [],
            "findings_count": {"blocking": 0, "major": 0, "minor": 0,
                               "info": 0},
            "completed_at": G.now_iso(),
        }
        pending = D._pending_result_path(self.dir, digest)
        os.makedirs(os.path.dirname(pending), exist_ok=True)
        with open(pending, "w", encoding="utf-8") as fh:
            json.dump(result, fh)
        with mock.patch.object(CC, "run_review",
                               side_effect=AssertionError("不应再次请求")):
            code = D.main(["cognition-review", "--state", self.state_path,
                           "--materials", self.mat_path])
        self.assertEqual(code, 0)
        record = G.load_state(self.state_path)["cognition_calls"][-1]
        self.assertTrue(record["recovered_from_pending"])


if __name__ == "__main__":
    unittest.main()

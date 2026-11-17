#!/usr/bin/env python3
"""v7 SOL 零工具单轮复核：请求不变量、解析、失败收敛、单轮与 G4 边界。"""
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
import sol_review as SR  # noqa: E402


def reply(**overrides) -> str:
    payload = {
        "schema": SR.REVIEW_SCHEMA,
        "verdict": "pass",
        "summary": "总体通过",
        "findings": [],
        "unverified": [],
        "materials_reviewed": ["变更"],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def response(text: str, *, model: str = SR.MODEL, status: str = "completed",
             output_type: str = "message", extra_output: list | None = None,
             http_status: int = 200) -> dict:
    output = [{"type": output_type, "status": "completed",
               "content": [{"type": "output_text", "text": text}]}]
    output.extend(extra_output or [])
    return {"model": model, "status": status, "output": output,
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "_http_status": http_status}


class ZeroToolTests(unittest.TestCase):
    def test_payload_has_no_tool_definitions(self):
        payload = SR.build_payload("材料")
        self.assertNotIn("tools", payload)
        self.assertNotIn("functions", payload)
        self.assertEqual(payload["tool_choice"], "none")
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(payload["model"], "gpt-6.1-sol")

    def test_injected_tools_fail_closed(self):
        for key in ("tools", "functions", "parallel_tool_calls"):
            with self.subTest(key=key):
                with self.assertRaises(SR.SolUsageError):
                    SR.assert_zero_tool({"model": "m", "tool_choice": "none",
                                         key: []})
        with self.assertRaises(SR.SolUsageError):
            SR.assert_zero_tool({"model": "m", "tool_choice": "auto"})

    def test_run_review_sends_exactly_one_zero_tool_request(self):
        calls = []

        def fake(url, key, body, timeout):
            calls.append(body)
            return response(reply())

        result = SR.run_review("材料", transport=fake, api_key="k")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("tools", calls[0])
        self.assertEqual(calls[0]["tool_choice"], "none")
        self.assertTrue(result["zero_tool"])
        self.assertEqual(result["attempts"], 1)


class ParseTests(unittest.TestCase):
    def test_valid_json_is_parsed(self):
        parsed = SR.parse_review_output(reply(
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
        parsed = SR.parse_review_output("```json\n" + reply() + "\n```")
        self.assertEqual(parsed["verdict"], "pass")

    def test_surrounding_prose_with_one_schema_object_is_accepted(self):
        parsed = SR.parse_review_output("说明如下：\n" + reply() + "\n以上。")
        self.assertEqual(parsed["verdict"], "pass")

    def test_pass_with_blocking_is_downgraded(self):
        parsed = SR.parse_review_output(reply(findings=[
            {"id": "F1", "severity": "blocking", "summary": "问题"}]))
        self.assertEqual(parsed["verdict"], "partial")
        self.assertIn("pass_with_blocking_or_major", parsed["verdict_adjusted"])

    def test_pass_with_unverified_is_downgraded(self):
        parsed = SR.parse_review_output(reply(unverified=["未看到调用方"]))
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
                with self.assertRaises(SR.SolResponseError):
                    SR.parse_review_output(text)


class FailureTests(unittest.TestCase):
    def test_identity_mismatch_is_rejected(self):
        with self.assertRaises(SR.SolIdentityError):
            SR.run_review("材料", transport=lambda *a: response(
                reply(), model="gpt-other"), api_key="k")

    def test_non_benign_output_is_security_event(self):
        with self.assertRaises(SR.SolToolCallError):
            SR.run_review("材料", transport=lambda *a: response(
                reply(), extra_output=[{"type": "function_call",
                                        "name": "run"}]), api_key="k")

    def test_incomplete_response_is_rejected(self):
        with self.assertRaises(SR.SolResponseError):
            SR.run_review("材料", transport=lambda *a: response(
                reply(), status="incomplete"), api_key="k")

    def test_failure_is_not_retried(self):
        calls = []

        def failing(url, key, body, timeout):
            calls.append(1)
            raise SR.SolUpstreamError("sol_upstream_error", "boom")

        with tempfile.TemporaryDirectory() as tmp:
            audit = os.path.join(tmp, "audit.jsonl")
            with self.assertRaises(SR.SolUpstreamError):
                SR.run_review("材料", transport=failing, api_key="k",
                              audit_log=audit)
            self.assertEqual(len(calls), 1)
            lines = open(audit, encoding="utf-8").read().splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertFalse(record["ok"])
            self.assertEqual(record["error"], "sol_upstream_error")

    def test_materials_too_large_is_rejected(self):
        with self.assertRaises(SR.SolUsageError):
            SR.run_review("x" * (SR.MAX_MATERIALS_CHARS + 1),
                          transport=lambda *a: response(reply()), api_key="k")

    def test_missing_credentials_fail_closed(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(SR, "_read_env_value", return_value=None), \
                mock.patch.object(SR, "_read_zcode_provider_key",
                                  return_value=None):
            os.environ.pop(SR.API_KEY_ENV, None)
            with self.assertRaises(SR.SolUsageError):
                SR.run_review("材料", transport=lambda *a: response(reply()))

    def test_http_429_is_quota_not_retry(self):
        error = urllib.error.HTTPError(
            "http://127.0.0.1:8317/v1/responses", 429, "Too Many Requests",
            {}, io.BytesIO(b'{"error": {"code": "usage_limit_reached"}}'))
        with mock.patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = error
            with self.assertRaises(SR.SolQuotaError):
                SR._post_once("http://127.0.0.1:8317/v1/responses", "k",
                              {"model": "m"}, 5)
            self.assertEqual(build.return_value.open.call_count, 1)

    def test_timeout_is_not_retried(self):
        with mock.patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = TimeoutError("timeout")
            with self.assertRaises(SR.SolTimeoutError):
                SR._post_once("http://127.0.0.1:8317/v1/responses", "k",
                              {"model": "m"}, 5)
            self.assertEqual(build.return_value.open.call_count, 1)


class SingleRoundTests(unittest.TestCase):
    def test_same_materials_reuse_existing_review(self):
        state = {"sol_reviews": [{"status": "completed",
                                  "materials_sha256": "a" * 64,
                                  "review_id": "SR1"}]}
        decision = SR.single_round_decision(state, "a" * 64)
        self.assertEqual(decision["action"], "reuse")

    def test_first_review_needs_no_reason(self):
        self.assertEqual(SR.single_round_decision({}, "a" * 64)["action"],
                         "call")

    def test_second_review_requires_explicit_reason(self):
        state = {"sol_reviews": [{"status": "completed",
                                  "materials_sha256": "a" * 64}]}
        refused = SR.single_round_decision(state, "b" * 64)
        self.assertEqual(refused["action"], "refuse")
        self.assertEqual(refused["code"], "sol_review_requires_reason")
        invalid = SR.single_round_decision(
            state, "b" * 64, force=True, trigger="because", reason="r")
        self.assertEqual(invalid["code"], "sol_review_trigger_invalid")
        missing = SR.single_round_decision(
            state, "b" * 64, force=True, trigger="scope-changed", reason="")
        self.assertEqual(missing["code"], "sol_review_reason_missing")
        allowed = SR.single_round_decision(
            state, "b" * 64, force=True, trigger="scope-changed",
            reason="范围扩大")
        self.assertEqual(allowed["action"], "call")


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
            "schema": SR.REVIEW_SCHEMA, "review_id": "SR" + "a" * 24,
            "verdict": verdict, "summary": "结论", "findings": findings,
            "unverified": [],
            "findings_count": {"blocking": 0, "major": 0, "minor": 0,
                               "info": 0},
            "completed_at": G.now_iso(),
        }
        return G.record_sol_review(self.state_path, record,
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

    def test_sol_review_never_satisfies_g4(self):
        self.record([], verdict="pass")
        state = G.load_state(self.state_path)
        state["risk"] = "high"
        state["native_reviews"] = []
        gate = G._gate_review(state)
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reasons"][0]["code"], "native_review_missing")
        self.assertEqual(state["sol_reviews"][0]["status"], "completed")

    def test_duplicate_sol_record_does_not_double_count(self):
        record = {
            "schema": SR.REVIEW_SCHEMA, "review_id": "SR" + "c" * 24,
            "verdict": "partial", "summary": "结论",
            "reply_sha256": "e" * 64,
            "findings": [{"id": "F1", "severity": "major", "file": "a.py",
                          "line": 1, "summary": "可疑"}],
            "unverified": [], "findings_count": {"major": 1},
            "completed_at": G.now_iso(),
        }
        G.record_sol_review(self.state_path, record,
                            materials_sha256="b" * 64)
        G.record_sol_review(self.state_path, record,
                            materials_sha256="b" * 64)
        state = G.load_state(self.state_path)
        item = G.list_findings(state)["items"][0]
        self.assertEqual(item["occurrences"], 1)
        self.assertEqual(item["first_review_sha256"], "e" * 64)

    def test_failure_is_recorded_without_blocking_task(self):
        failure = G.record_sol_review_failure(
            self.state_path, error_code="sol_timeout", detail="300s",
            materials_sha256="c" * 64)
        self.assertEqual(failure["status"], "failed")
        state = G.load_state(self.state_path)
        self.assertEqual(state["sol_reviews"][-1]["error"], "sol_timeout")
        self.assertEqual(G.evaluate(state)["phase"], "PARTIAL")

    def test_review_can_be_recorded_while_blocked(self):
        G.add_block(self.state_path, "等待用户授权", "用户确认")
        record = self.record([], verdict="pass")
        self.assertEqual(record["status"], "completed")
        state = G.load_state(self.state_path)
        self.assertEqual(G.evaluate(state)["phase"], "BLOCKED")
        self.assertTrue(state["sol_reviews"])


class AdmissionTests(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(SR.admission(risk="low")["decision"], "not_needed")
        self.assertEqual(SR.admission(risk="medium")["decision"], "optional")
        self.assertEqual(SR.admission(risk="high")["decision"], "recommended")
        self.assertTrue(SR.admission(risk="high")["g4_separate"])

    def test_trigger_validation(self):
        self.assertEqual(SR.admission(risk="low",
                                      triggers=["security"])["decision"],
                         "recommended")
        with self.assertRaises(SR.SolUsageError):
            SR.admission(risk="medium", triggers=["nonsense"])


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
        self.assertIsNone(SR._NoRedirect().redirect_request(
            None, None, 302, "Found", {}, "http://evil.example/v1"))
        opened = []

        class Opener:
            def open(self, request, timeout=None):
                opened.append(request.full_url)
                raise urllib.error.HTTPError(
                    request.full_url, 302, "Found",
                    {"Location": "http://evil.example/v1"}, io.BytesIO(b""))

        with mock.patch("urllib.request.build_opener", return_value=Opener()):
            with self.assertRaises(SR.SolUpstreamError):
                SR._post_once("http://127.0.0.1:8317/v1/responses", "k",
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
            with self.assertRaises(SR.SolUpstreamError) as ctx:
                SR._post_once("http://127.0.0.1:8317/v1/responses", "k",
                              {"model": "m"}, 5)
        self.assertEqual(ctx.exception.code, "sol_transport_error")

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
                with self.assertRaises(SR.SolResponseError):
                    SR.parse_review_output(text)

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

        self.assertEqual(SR._read_zcode_provider_key(
            config("http://127.0.0.1:8317/v1")), "k")
        self.assertIsNone(SR._read_zcode_provider_key(
            config("http://remote.example:8317/v1")))
        self.assertIsNone(SR._read_zcode_provider_key(
            config("http://127.0.0.1:9999/v1")))
        self.assertIsNone(SR._read_zcode_provider_key(
            config("http://127.0.0.1:8317@evil.example/v1")))

    def test_base_url_rejects_userinfo_and_remote_hosts(self):
        with mock.patch.dict(os.environ,
                             {"COPILOT_SOL_BASE_URL":
                              "http://127.0.0.1:8317@evil.example/v1"}):
            with self.assertRaises(SR.SolUsageError):
                SR.resolve_base_url()
        with mock.patch.dict(os.environ,
                             {"COPILOT_SOL_BASE_URL":
                              "http://127.0.0.1:9999/v1"}):
            with self.assertRaises(SR.SolUsageError):
                SR.resolve_base_url()


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
        proc = F.run_cli("copilot_dispatch.py", "sol-review", "--state",
                         self.state, "--materials", self.materials,
                         "--dry-run", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["zero_tool"])
        self.assertEqual(payload["tool_keys_present"], [])
        self.assertEqual(payload["model"], "gpt-6.1-sol")
        self.assertEqual(open(self.state, "rb").read(), before)

    def test_same_materials_reuse_without_network(self):
        import argparse
        import contextlib
        import io
        import copilot_dispatch as CD
        state = G.load_state(self.state)
        materials_text = open(self.materials, encoding="utf-8").read()
        digest = SR.review_subject_sha256(state, materials_text)
        G.record_sol_review(self.state, {
            "schema": SR.REVIEW_SCHEMA, "review_id": "SR" + "b" * 24,
            "verdict": "pass", "summary": "ok", "findings": [],
            "unverified": [], "findings_count": {}, "completed_at": G.now_iso(),
        }, materials_sha256=digest)
        args = argparse.Namespace(
            state=self.state, materials=self.materials, reason="", trigger="",
            force=False, dry_run=False, list=False, json=True, timeout=5,
            max_output_tokens=100, audit_log="", output_dir="")
        out = io.StringIO()
        with mock.patch("sol_review.run_review",
                        side_effect=AssertionError("must not call")) as call, \
                contextlib.redirect_stdout(out):
            self.assertEqual(CD.cmd_sol_review(args), 0)
        self.assertEqual(call.call_count, 0)
        self.assertEqual(json.loads(out.getvalue())["review_id"],
                         "SR" + "b" * 24)

    def test_status_change_does_not_create_new_subject(self):
        state = G.load_state(self.state)
        materials_text = open(self.materials, encoding="utf-8").read()
        before = SR.review_subject_sha256(state, materials_text)
        state["contract"]["criteria"][0]["status"] = "met"
        after = SR.review_subject_sha256(state, materials_text)
        self.assertEqual(before, after)
        self.assertNotEqual(
            before, SR.review_subject_sha256(state, materials_text + "\n+extra"))

    def test_list_shows_completed_and_failed(self):
        G.record_sol_review_failure(self.state, error_code="sol_quota",
                                    materials_sha256="d" * 64)
        proc = F.run_cli("copilot_dispatch.py", "sol-review", "--state",
                         self.state, "--list", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(len(payload["sol_reviews"]), 1)
        self.assertEqual(payload["sol_reviews"][0]["error"], "sol_quota")

    def test_audit_has_no_prompt_text_or_key(self):
        text = "SECRET-PROMPT-BODY"
        dummy_credential = "placeholder-credential-value"
        with tempfile.TemporaryDirectory() as tmp:
            audit = os.path.join(tmp, "audit.jsonl")
            SR.run_review(text, transport=lambda *a: response(reply()),
                          api_key=dummy_credential, audit_log=audit)
            content = open(audit, encoding="utf-8").read()
        self.assertNotIn("SECRET-PROMPT-BODY", content)
        self.assertNotIn(dummy_credential, content)
        record = json.loads(content.splitlines()[0])
        self.assertEqual(record["materials_sha256"],
                         hashlib.sha256(text.encode("utf-8")).hexdigest())


if __name__ == "__main__":
    unittest.main()

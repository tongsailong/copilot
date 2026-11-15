#!/usr/bin/env python3
"""v6.3 复核收敛回归：真实误拒、解析变体、相对路径、finding 元数据与指标。

用例只覆盖真实发生过的机制失败（ad-strategy 的 `git rev-list`/`paste`、honor 的
围栏输出与 `cd` 相对路径、review-perf/gromore 的重复 finding 与证据漂移），不为
理论边界扩张。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import v6_support as F

import copilot_auto as AC  # noqa: E402
import governance as G  # noqa: E402
import native_agent as NA  # noqa: E402
import native_bridge as NB  # noqa: E402


class ReviewOutputParsingV63Tests(unittest.TestCase):
    def parse(self, text: str) -> dict:
        return NA._extract_review_json(text, "output.json")

    def test_fenced_variants_are_accepted(self):
        variants = {
            "plain": '{"verdict": "pass"}',
            "newline": '```json\n{"verdict": "pass"}\n```',
            "no newline": '```json{"verdict": "pass"}```',
            "tag spaces": '``` json \n{"verdict": "pass"}\n```',
        }
        for name, text in variants.items():
            with self.subTest(name=name):
                self.assertEqual(self.parse(text)["verdict"], "pass")

    def test_multiple_objects_stay_ambiguous(self):
        text = '```json\n{"a": 1}\n```\n```json\n{"b": 2}\n```'
        with self.assertRaises(NA.AgentRecordError) as ctx:
            self.parse(text)
        self.assertIn("review_output_ambiguous", str(ctx.exception))

    def test_non_json_is_unparsable(self):
        with self.assertRaises(NA.AgentRecordError) as ctx:
            self.parse("no json here")
        self.assertIn("review_output_unparsable", str(ctx.exception))


class FindingMetadataV63Tests(F.BridgeBase):
    def review_with(self, findings) -> dict:
        return super().review(self.execution(), findings=findings,
                              g4_eligible=False, target_sha256="")

    def validate(self, findings):
        state = G.load_state(self.state_path)
        execution = self.execution()
        return NB.validate_review_result(
            self.review_with(findings), self.dispatch, execution, state,
            check_files=False)

    def test_optional_metadata_is_preserved(self):
        record = self.validate([{
            "id": "F1", "severity": "minor", "summary": "小问题",
            "file": self.evidence, "evidence": "观察",
            "root_cause": "同一根因", "category": "evidence",
            "status": "open", "related": ["F2"], "reproduce": "跑一次",
        }, {
            "id": "F2", "severity": "info", "summary": "观察",
            "file": "", "evidence": "",
        }])
        first = record["findings"][0]
        self.assertEqual(first["id"], "F1")
        self.assertEqual(first["root_cause"], "同一根因")
        self.assertEqual(first["category"], "evidence")
        self.assertEqual(first["status"], "open")
        self.assertEqual(first["related"], ["F2"])
        self.assertEqual(first["reproduce"], "跑一次")
        self.assertEqual(record["findings"][1]["status"], "open")

    def test_duplicate_id_is_rejected(self):
        findings = [
            {"id": "F1", "severity": "minor", "summary": "一", "file": "", "evidence": ""},
            {"id": "F1", "severity": "minor", "summary": "二", "file": "", "evidence": ""},
        ]
        with self.assertRaises(NB.BridgeError) as ctx:
            self.validate(findings)
        self.assertEqual(ctx.exception.code, "review_finding_duplicate")

    def test_duplicate_body_is_rejected(self):
        findings = [
            {"severity": "minor", "summary": "同一个", "file": self.evidence,
             "line": 3, "evidence": "相同"},
            {"severity": "minor", "summary": "同一个", "file": self.evidence,
             "line": 3, "evidence": "相同"},
        ]
        with self.assertRaises(NB.BridgeError) as ctx:
            self.validate(findings)
        self.assertEqual(ctx.exception.code, "review_finding_duplicate")

    def test_invalid_id_status_and_self_relation_are_rejected(self):
        cases = {
            "review_finding_invalid": [
                {"id": "X1", "severity": "minor", "summary": "s", "file": "", "evidence": ""}],
            "status": [
                {"id": "F1", "severity": "minor", "summary": "s", "file": "",
                 "evidence": "", "status": "fixed"}],
            "self": [
                {"id": "F1", "severity": "minor", "summary": "s", "file": "",
                 "evidence": "", "related": ["F1"]}],
        }
        for name, findings in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(NB.BridgeError) as ctx:
                    self.validate(findings)
                self.assertEqual(ctx.exception.code, "review_finding_invalid")


class ExecutionDriftV63Tests(unittest.TestCase):
    def test_drift_reports_differs_dropped_and_missing(self):
        state = {"evidence": [
            {"id": "E1", "sha256": "a" * 64},
            {"id": "E2", "sha256": "b" * 64, "dropped": True},
        ]}
        execution = {"evidence": [
            {"id": "E1", "sha256": "c" * 64},
            {"id": "E2", "sha256": "b" * 64},
            {"id": "E3", "sha256": "d" * 64},
        ]}
        drift = NB.execution_evidence_drift(execution, state)
        self.assertEqual([d["id"] for d in drift], ["E1", "E2", "E3"])
        self.assertEqual([d["problem"] for d in drift],
                         ["sha_differs", "dropped", "not_registered"])

    def test_no_drift_when_summaries_match(self):
        state = {"evidence": [{"id": "E1", "sha256": "a" * 64}]}
        execution = {"evidence": [{"id": "E1", "sha256": "a" * 64}]}
        self.assertEqual(NB.execution_evidence_drift(execution, state), [])


class ReviewContextV63Tests(F.BridgeBase):
    def setUp(self):
        super().setUp()
        state = G.load_state(self.state_path)
        state["native_dispatch"] = self.dispatch
        G.save_state(self.state_path, state)
        self.execution_env = self.execution()
        self.execution_path = os.path.join(self.dir, "execution.json")
        with open(self.execution_path, "w", encoding="utf-8") as fh:
            json.dump(self.execution_env, fh, ensure_ascii=False)
        self.dispatch_path = os.path.join(self.dir, "dispatch.json")
        with open(self.dispatch_path, "w", encoding="utf-8") as fh:
            json.dump(self.dispatch, fh, ensure_ascii=False)
        AC._ingest_execution(self.state_path, self.execution_env)

    def args(self, **overrides):
        base = {"state": self.state_path, "dispatch": self.dispatch_path,
                "execution": self.execution_path, "work_item": "A1",
                "previous_review": "", "workspace": self.dir}
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_context_reports_bindings_and_evidence(self):
        payload = NA.build_review_context(self.args())
        self.assertEqual(payload["schema"], "copilot-review-context/v1")
        self.assertEqual(payload["execution_result_sha256"],
                         self.execution_env["result_sha256"])
        self.assertEqual(payload["review_scope"], "full")
        self.assertEqual([e["id"] for e in payload["evidence"]], ["E1"])
        self.assertEqual(payload["execution_evidence_unbound"], [])
        self.assertIn("criteria", payload)

    def test_previous_review_flags_changed_criteria(self):
        # 真实 e2e 复现：复核后核销条件 → 基线目标摘要过期，不能再建议“直接复用”。
        attempt = self.create_attempt(self.execution_env)
        host = F.make_fake_review_host(
            self.dir, execution=self.execution_env, dispatch=self.dispatch,
            evidence=self.evidence, task_id=self.state["task_id"],
            attempt_id=attempt["review_attempt_id"],
            session_created_at=attempt["created_at"])
        env = self.review(self.execution_env, attempt=attempt, host=host)
        AC._ingest_review(self.state_path, env)
        # 阶段九：正式基线必须来自新链；夹具把本轮复核标记为 candidate 并消费
        # attempt，再核销条件以复现“复核后条件变化”的诊断路径。
        state = G.load_state(self.state_path)
        record = state["native_reviews"][-1]
        record["g4_eligible"] = True
        record["g4_source"] = "candidate"
        record["candidate_g4"] = {"eligible": True, "reasons": []}
        bound = G.find_review_attempt(state, attempt["review_attempt_id"])
        bound["consumed"] = True
        bound["consumed_by_result_sha256"] = record["result_sha256"]
        G.save_state(self.state_path, state)
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=["E1"])
        review_path = os.path.join(self.dir, "previous.json")
        with open(review_path, "w", encoding="utf-8") as fh:
            json.dump(env, fh, ensure_ascii=False)
        payload = NA.build_review_context(
            self.args(previous_review=review_path))
        self.assertEqual(payload["review_scope"], "full")
        self.assertIn("完成条件状态已变化", payload["scope_reason"])
        self.assertNotIn("可直接复用", payload["scope_reason"])

    def test_context_warns_when_criteria_still_pending(self):
        # 核销条件会改变目标摘要，收口时本轮复核会被判过期（真实 e2e 已复现）。
        payload = NA.build_review_context(self.args())
        self.assertTrue(any("尚未核销" in w for w in payload["warnings"]))
        self.assertTrue(any("C1" in w for w in payload["warnings"]))

    def test_context_reports_dropped_evidence_drift(self):
        state = G.load_state(self.state_path)
        G.drop_evidence(state, "E1", "替换快照")
        G.save_state(self.state_path, state)
        payload = NA.build_review_context(self.args())
        self.assertEqual(payload["execution_evidence_unbound"][0]["id"], "E1")
        self.assertEqual(
            payload["execution_evidence_unbound"][0]["problem"], "dropped")


class IngestAuditV63Tests(F.BridgeBase):
    def setUp(self):
        super().setUp()
        state = G.load_state(self.state_path)
        state["native_dispatch"] = self.dispatch
        G.save_state(self.state_path, state)

    def test_execution_ingest_writes_history_event(self):
        AC._ingest_execution(self.state_path, self.execution())
        state = G.load_state(self.state_path)
        events = [h["event"] for h in state.get("history") or []]
        self.assertIn("native_result_ingested", events)

    def test_rejected_review_is_logged_as_attempt(self):
        execution = self.execution()
        AC._ingest_execution(self.state_path, execution)
        bad = self.review(execution, verdict="pass", g4_eligible=False,
                          target_sha256="", findings=[{
                              "severity": "blocking", "summary": "阻塞",
                              "file": self.evidence, "evidence": "证据"}])
        with self.assertRaises(AC.AutoError):
            AC._ingest_review(self.state_path, bad)
        path = os.path.join(os.path.dirname(self.state_path), "review-attempts.jsonl")
        self.assertTrue(os.path.isfile(path))
        with open(path, "r", encoding="utf-8") as fh:
            entry = json.loads(fh.readline())
        self.assertEqual(entry["kind"], "review")
        self.assertEqual(entry["code"], "pass_with_blocking_finding")

    def test_review_build_failure_is_logged_as_attempt(self):
        # 生成阶段被拒也属于无效信封：落进同一份 attempts，指标才完整。
        execution = self.execution()
        AC._ingest_execution(self.state_path, execution)
        execution_path = os.path.join(self.dir, "execution.json")
        with open(execution_path, "w", encoding="utf-8") as fh:
            json.dump(execution, fh, ensure_ascii=False)
        dispatch_path = os.path.join(self.dir, "dispatch.json")
        with open(dispatch_path, "w", encoding="utf-8") as fh:
            json.dump(self.dispatch, fh, ensure_ascii=False)
        output = os.path.join(self.dir, "review-output.json")
        with open(output, "w", encoding="utf-8") as fh:
            json.dump({"verdict": "pass"}, fh)
        metadata = os.path.join(self.dir, "review-metadata.json")
        with open(metadata, "w", encoding="utf-8") as fh:
            json.dump({"agentId": "agent-review", "childSessionId": "child-review",
                       "profileId": "reviewer",
                       "profileSnapshot": {"name": "reviewer",
                                           "tools": ["Read", "Grep", "Glob", "Bash"],
                                           "disallowedTools": ["Write", "Edit"]},
                       "status": "completed",
                       "totalToolUseCount": 1, "outputFile": output}, fh)
        rollout = os.path.join(self.dir, "review-rollout.jsonl")
        with open(rollout, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"sessionId": "child-review",
                                 "model": {"providerId": "fake",
                                           "modelId": "reviewer-model"}}) + "\n")
        code = NA.main(["review", "--state", self.state_path,
                        "--dispatch", dispatch_path,
                        "--execution", execution_path,
                        "--work-item", "A1", "--metadata", metadata])
        self.assertEqual(code, 2)
        path = os.path.join(os.path.dirname(self.state_path), "review-attempts.jsonl")
        self.assertTrue(os.path.isfile(path))
        with open(path, "r", encoding="utf-8") as fh:
            entry = json.loads(fh.readline())
        self.assertEqual(entry["kind"], "review_build")
        self.assertEqual(entry["code"], "review_attempt_missing")
        self.assertEqual(entry["work_item_id"], "A1")
        self.assertEqual(entry["execution_result_sha256"],
                         execution["result_sha256"])

    def test_review_ingest_writes_history_event(self):
        execution = self.execution()
        AC._ingest_execution(self.state_path, execution)
        # 非 G4 PASS：新链不会从信封自述取得正式资格。
        review = self.review(execution, g4_eligible=False, target_sha256="")
        AC._ingest_review(self.state_path, review)
        state = G.load_state(self.state_path)
        events = [h["event"] for h in state.get("history") or []]
        self.assertIn("native_review_ingested", events)
        self.assertEqual(state["native_reviews"][-1]["execution_evidence_unbound"], [])


class ReviewMetricsV63Tests(unittest.TestCase):
    def test_metrics_count_rounds_duplicates_and_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = {
                "task_id": "task-metrics", "risk": "high",
                "execution_backend": "native", "created": "2026-11-14T10:00:00+08:00",
                "evidence": [{"id": "E1", "sha256": "a" * 64}],
                "native_reviews": [
                    {"work_item_id": "A1", "verdict": "partial",
                     "received": "2026-11-14T11:00:00+08:00",
                     "review_scope": "full", "g4_eligible": False,
                     "findings": [{"severity": "major", "file": "/x", "line": 1,
                                   "summary": "同一问题"},
                                  {"severity": "minor", "file": "/y", "line": 2,
                                   "summary": "另一问题"}],
                     "commands_run": ["git status"], "files_inspected": ["/x"],
                     "evidence_manifest": []},
                    {"work_item_id": "A1", "verdict": "pass",
                     "received": "2026-11-14T12:00:00+08:00",
                     "review_scope": "incremental", "g4_eligible": True,
                     "findings": [{"severity": "major", "file": "/x", "line": 1,
                                   "summary": "同一问题"}],
                     "commands_run": ["git status"], "files_inspected": ["/x"],
                     "evidence_manifest": ["E1"]},
                ],
                "formal_reviews": [{"verdict": "pass"}],
                "closure": {"closed": True,
                            "closed_at": "2026-11-14T12:30:00+08:00"},
            }
            with open(os.path.join(tmp, "review-attempts.jsonl"), "w",
                      encoding="utf-8") as fh:
                fh.write(json.dumps({"kind": "review",
                                     "code": "review_command_not_readonly"}) + "\n")
                fh.write(json.dumps({"kind": "execution",
                                     "code": "evidence_sha_mismatch"}) + "\n")
            metrics = G.review_metrics(state, task_dir=tmp)
        self.assertEqual(metrics["native_reviews_total"], 2)
        self.assertEqual(metrics["g4_reviews"], 1)
        self.assertEqual(metrics["formal_reviews_total"], 1)
        self.assertEqual(metrics["review_scopes"], {"full": 1, "incremental": 1})
        self.assertEqual(metrics["findings_total"], 3)
        self.assertEqual(metrics["duplicate_findings"], 1)
        self.assertEqual(metrics["duplicate_finding_ratio"], 0.333)
        self.assertEqual(metrics["repeated_command_count"], 1)
        self.assertEqual(metrics["repeated_file_count"], 1)
        self.assertEqual(metrics["invalid_envelopes"],
                         {"execution": 1, "review": 1, "review_build": 0})
        self.assertEqual(metrics["invalid_envelope_codes"],
                         {"evidence_sha_mismatch": 1,
                          "review_command_not_readonly": 1})
        self.assertEqual(metrics["work_items"][0]["rounds"], 2)
        self.assertEqual(metrics["work_items"][0]["pass_round"], 2)
        self.assertEqual(metrics["work_items"][0]["time_to_pass_seconds"], 3600.0)
        self.assertEqual(metrics["avg_rounds_to_pass"], 2.0)
        self.assertEqual(metrics["task_seconds"], 9000.0)


if __name__ == "__main__":
    unittest.main()

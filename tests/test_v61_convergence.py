#!/usr/bin/env python3
"""v6.1 复核收敛：只读策略、宿主派生、增量复核、rerun 与合同提醒。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import copilot_auto as auto  # noqa: E402
import governance as G  # noqa: E402
import native_agent as NA  # noqa: E402
import native_bridge as NB  # noqa: E402
import v6_support as F  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""


class CodeAssertions(unittest.TestCase):
    def assert_code(self, code: str, func, *args, **kwargs) -> None:
        with self.assertRaises(NB.BridgeError) as ctx:
            func(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))


class ReadonlyPolicyTests(CodeAssertions):
    """只读策略按实际写风险判定：检查工具放行，写变体失败关闭。"""

    ALLOWED = (
        "sed -n '1,40p' /abs/file",
        "sed -n '/\\\\w\\\\+/p' /abs/file",
        "awk '/pat/ {print $1}' /abs/file",
        "echo ---- ; printf '%s\\n' a b",
        "jq -r '.a' /abs/file.json",
        "for f in a b; do sha256sum \"$f\"; done",
        "unzip -l /abs/a.apk",
        "apksigner verify --print-certs /abs/a.apk",
        "aapt dump badging /abs/a.apk",
        "aapt2 dump permissions /abs/a.apk",
        "date -u +%s",
        "date -u -d '2020-01-01' +%s",
        "date -ud '2020-01-01' +%s",
        "date +%Y-%m-%d",
        "date -Iseconds",
        "date --iso-8601=seconds",
        "date -d @0 -u +%FT%TZ",
        "test -f /abs/file",
        "git status --short",
        "awk -F: -v n=1 '{print $1}' /abs/file",
        "awk 'NR > 1' /abs/file",
        "awk '$3 > 100 {print $1}' /abs/file",
        "awk '$1==\"a\" || $2==\"b\"' /abs/file",
        "awk 'BEGIN{print \"a>b\"}'",
        "awk 'BEGIN{x=\"print a > b\"; print x}'",
        "awk 'BEGIN{print (1 > 2)}'",
        "awk 'BEGIN{print 1 || 2}'",
        "awk '{print $1==\"a\" || $1==\"b\"}' /abs/file",
        "awk 'BEGIN{print 1 >= 2}'",
        "awk -b '{print}' /abs/file",
        "awk -M '{print}' /abs/file",
        "awk -W version",
        "sed -n -e '1,5p' /abs/file",
        "sed --quiet '1p' /abs/file",
        "sed 'y/a/w/' /abs/file",
        "sed 'a\\welcome' /abs/file",
        "sed -e's/i/x/' /abs/file",
        "sed --line-len=80 '1p' /abs/file",
        "xxd -l 64 /abs/file",
        "file /abs/file",
        "file -m /abs/magic /abs/file",
    )
    REJECTED = (
        "sed -i 's/a/b/' /abs/file",
        "sed -n 'w /abs/out' /abs/file",
        "sed 's/a/b/w' /abs/file",
        "sed -f /abs/script.sed /abs/file",
        "sed -ni 'p' /abs/file",
        "sed --in-pl 'p' /abs/file",
        "sed --fil=/abs/script.sed /abs/file",
        "sed -e 'w /abs/out' /abs/file",
        "awk 'BEGIN{print \"x\" > \"/abs/out\"}'",
        "awk '{print $1 >> \"/abs/out\"}' /abs/file",
        "awk '{print $1 | \"sort\"}' /abs/file",
        "awk '\"date\" | getline x'",
        "awk '\"date\" |& getline x'",
        "awk '{print $1 |& \"sort\"}' /abs/file",
        "awk 'BEGIN{system(\"rm /abs/file\")}'",
        "awk 'BEGIN{system\t(\"rm /abs/file\")}'",
        "awk -f /abs/p.awk /abs/file",
        "awk --fil=/abs/p.awk /abs/file",
        "awk -p /abs/profile.out /abs/file",
        "awk --profile=/abs/out /abs/file",
        "for f in $(ls); do cat $f; done",
        "for f in a b; do rm $f; done",
        "date -s '2020-01-01'",
        "date -snotadate",
        "date 010100002020",
        "date -u 010100002020",
        "date -I 010100002020",
        "date -Idate 010100002020",
        "date --iso-8601 010100002020",
        "date --iso-8601 -s2020-01-01",
        "date --se='2020-01-01'",
        "date --s=202001010000",
        "tree -o /abs/out /abs",
        "unzip /abs/a.apk",
        "unzip -d /abs/out /abs/a.apk",
        "apksigner sign /abs/a.apk",
        "aapt2 link -o /abs/x",
        "cat /abs/file > /abs/out",
        "xxd /abs/file /abs/out",
        "file -C -m /abs/x.magic",
        "file --com -m /abs/x.magic",
        "file --co -m /abs/x.magic",
    )

    def test_allowed_inspection_commands(self):
        for command in self.ALLOWED:
            with self.subTest(command=command):
                self.assertEqual(NB.validate_review_commands([command]), [command])

    def test_write_variants_rejected(self):
        for command in self.REJECTED:
            with self.subTest(command=command):
                self.assert_code("review_command_not_readonly",
                                 NB.validate_review_commands, [command])


class InspectionFieldTests(F.BridgeBase):
    def test_empty_files_inspected_is_allowed(self):
        execution = self.execution()
        env = self.review(execution, files_inspected=[])
        result = NB.validate_review_result(env, self.dispatch, execution, self.state)
        self.assertEqual(result["files_inspected"], [])
        self.assertTrue(result["g4_eligible"])

    def test_declared_but_unverified_files_are_recorded(self):
        execution = self.execution()
        env = self.review(execution,
                          declared_files_unverified=["/abs/not-read.txt"])
        result = NB.validate_review_result(env, self.dispatch, execution, self.state)
        self.assertEqual(result["declared_files_unverified"],
                         ["/abs/not-read.txt"])

    def test_g4_with_unverified_is_rejected(self):
        execution = self.execution()
        env = self.review(execution, unverified=["仍有未验证事项"])
        self.assert_code("g4_with_unverified", NB.validate_review_result,
                         env, self.dispatch, execution, self.state)


class RerunTests(F.BridgeBase):
    def _save_dispatch(self) -> None:
        state = G.load_state(self.state_path)
        state["native_dispatch"] = self.dispatch
        G.save_state(self.state_path, state)

    def test_rerun_allows_new_result_for_same_work_item(self):
        self._save_dispatch()
        first = self.execution(result="旧结果")
        auto._ingest_execution(self.state_path, first)
        second = self.execution(result="修复后的新结果")
        self.assert_code("work_item_terminal", NB.validate_execution_result,
                         second, self.dispatch, G.load_state(self.state_path))
        G.rerun_action(self.state_path, "A1", reason="修复后重新验证")
        state = G.load_state(self.state_path)
        action = next(a for a in state["actions"] if a["id"] == "A1")
        self.assertEqual(action["status"], "pending")
        self.assertEqual(action["rerun_count"], 1)
        self.assertEqual(action["rerun_history"][0]["status"], "done")
        self.assertIn("旧结果", action["rerun_history"][0]["result"] or "")
        auto._ingest_execution(self.state_path, second)
        state = G.load_state(self.state_path)
        action = next(a for a in state["actions"] if a["id"] == "A1")
        self.assertEqual(action["status"], "done")
        self.assertIn("修复后的新结果", action["result"])


class ContractAmendmentTests(unittest.TestCase):
    def test_amend_while_blocked_and_source_drift_is_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            contract_file = os.path.join(tmp, "contract.md")
            with open(contract_file, "w", encoding="utf-8") as fh:
                fh.write(CONTRACT)
            G.start_state(path, goal="目标", contract=CONTRACT,
                          contract_source=contract_file)
            G.add_block(path, reason="等待用户决策", needs="用户确认")
            # BLOCKED 下允许修订合同，但不会解除阻塞。
            G.amend_contract(path, contract=CONTRACT, contract_source=contract_file)
            state = G.load_state(path)
            self.assertEqual(G.evaluate(state)["phase"], "BLOCKED")
            self.assertEqual(G.evaluate(state).get("warnings") or [], [])
            with open(contract_file, "w", encoding="utf-8") as fh:
                fh.write(CONTRACT + "\n- 新增条件\n")
            warnings = G.evaluate(state).get("warnings") or []
            self.assertTrue(any("合同源文件已变更" in item for item in warnings),
                            warnings)
            self.assertEqual(G.evaluate(state)["phase"], "BLOCKED")
            # 重新登记后提醒消失，源摘要更新。
            with open(contract_file, encoding="utf-8") as fh:
                updated = fh.read()
            G.amend_contract(path, contract=updated, contract_source=contract_file)
            self.assertEqual(
                G.evaluate(G.load_state(path)).get("warnings") or [], [])


class IncrementalReviewTests(F.BridgeBase):
    def setUp(self):
        super().setUp()
        state = G.load_state(self.state_path)
        state["native_dispatch"] = self.dispatch
        G.save_state(self.state_path, state)
        self.first = self.execution()
        auto._ingest_execution(self.state_path, self.first)
        state = G.load_state(self.state_path)
        base_env = self.review(
            self.first, target_sha256=G.final_evidence_digest(state))
        base = NB.validate_review_result(base_env, self.dispatch, self.first, state)
        base["host_sha256"] = G._sha256_text(G.canonical_json(base["host"]))
        state["native_reviews"].append(base)
        G.save_state(self.state_path, state)
        self.base = base

    def _second_execution(self) -> dict:
        extra = os.path.join(self.dir, "evidence-2.txt")
        with open(extra, "w", encoding="utf-8") as fh:
            fh.write("fixed\n")
        self.extra = extra
        G.rerun_action(self.state_path, "A1", reason="修复后复核")
        second = self.execution(
            result="修复后的结果",
            evidence=[{"id": "E2", "file": extra,
                       "sha256": NB.sha256_file(extra), "kind": "file"}])
        auto._ingest_execution(self.state_path, second)
        return second

    def _incremental(self, second: dict, **overrides) -> dict:
        state = G.load_state(self.state_path)
        env = self.review(second, target_sha256=G.final_evidence_digest(state),
                          review_scope="incremental",
                          based_on_review_sha256=self.base["result_sha256"],
                          evidence_manifest=["E2"])
        env.update(overrides)
        env["result_sha256"] = NB.envelope_sha256(env)
        return env

    def test_incremental_review_covers_only_new_evidence(self):
        second = self._second_execution()
        state = G.load_state(self.state_path)
        env = self._incremental(second)
        result = NB.validate_review_result(env, self.dispatch, second, state)
        self.assertEqual(result["review_scope"], "incremental")
        self.assertEqual(result["based_on_review_sha256"],
                         self.base["result_sha256"])
        self.assertEqual(result["evidence_manifest"], ["E2"])
        self.assertTrue(result["g4_eligible"], result.get("g4_reasons"))
        record = {**result,
                  "host_sha256": G._sha256_text(G.canonical_json(result["host"]))}
        state["native_reviews"].append(record)
        ok, code = G._eligible_review(record, state)
        self.assertTrue(ok, code)

    def test_incremental_requires_manifest_for_changed_evidence(self):
        second = self._second_execution()
        state = G.load_state(self.state_path)
        env = self._incremental(second, evidence_manifest=[])
        self.assert_code("incremental_manifest_missing",
                         NB.validate_review_result, env, self.dispatch, second, state)

    def test_incremental_rejects_changed_criteria(self):
        second = self._second_execution()
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=["E1"])
        state = G.load_state(self.state_path)
        env = self._incremental(second)
        self.assert_code("incremental_criteria_changed",
                         NB.validate_review_result, env, self.dispatch, second, state)

    def test_incremental_requires_existing_baseline(self):
        second = self._second_execution()
        state = G.load_state(self.state_path)
        env = self._incremental(second, based_on_review_sha256="a" * 64)
        self.assert_code("incremental_base_missing",
                         NB.validate_review_result, env, self.dispatch, second, state)

    def test_dropped_evidence_counts_as_change(self):
        second = self._second_execution()
        state = G.load_state(self.state_path)
        G.drop_evidence(state, "E1", "基线上的证据已归档")
        G.save_state(self.state_path, state)
        state = G.load_state(self.state_path)
        env = self._incremental(second, evidence_manifest=["E2"])
        self.assert_code("incremental_evidence_uncovered",
                         NB.validate_review_result, env, self.dispatch, second,
                         state)

    def test_governance_rejects_uncovered_incremental_record(self):
        second = self._second_execution()
        state = G.load_state(self.state_path)
        env = self._incremental(second, evidence_manifest=[])
        # 绕过 ingest 直接构造记录：收口期仍要用覆盖快照重算并拒绝。
        record = {**env,
                  "target_sha256": G.final_evidence_digest(state),
                  "coverage": NB.review_coverage(state),
                  "reviewer": {"kind": "subagent", "agent_id": "agent-review",
                               "child_session_id": "child-review",
                               "profile_id": "reviewer", "model_reported": "m"},
                  "host": {"metadata_path": "/m", "output_file": "/o",
                           "rollout_path": "/r",
                           "tool_calls": [{"name": "Read", "count": 1}],
                           "agent_id": "agent-review",
                           "child_session_id": "child-review"},
                  "evidence_manifest": []}
        record["host_sha256"] = G._sha256_text(G.canonical_json(record["host"]))
        state["native_reviews"].append(record)
        ok, code = G._eligible_review(record, state)
        self.assertFalse(ok)
        self.assertEqual(code, "native_review_incremental_evidence_uncovered")


class HostOutputTests(unittest.TestCase):
    def test_single_fence_is_accepted(self):
        data = NA._extract_review_json(
            "说明\n```json\n{\"verdict\": \"pass\"}\n```\n结尾", "x")
        self.assertEqual(data, {"verdict": "pass"})

    def test_two_fences_are_rejected(self):
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA._extract_review_json(
                "```json\n{\"verdict\": \"pass\"}\n```\n"
                "```json\n{\"verdict\": \"partial\"}\n```", "x")
        self.assertIn("review_output_ambiguous", str(ctx.exception))

    def test_plain_text_is_rejected(self):
        with self.assertRaises(NA.AgentRecordError) as ctx:
            NA._extract_review_json("review passed", "x")
        self.assertIn("review_output_unparsable", str(ctx.exception))


class DerivationTests(unittest.TestCase):
    def test_commands_and_files_come_from_actual_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "evidence.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x\n")
            calls = [
                ("Read", "", {"file_path": path}),
                ("Bash", "git status --short", {"command": "git status --short"}),
            ]
            self.assertEqual(NA._actual_commands(calls), ["git status --short"])
            self.assertEqual(NA._merge_inspected_files([], calls), [path])
            self.assertEqual(NA._declared_files_unverified([path], calls), [])
            self.assertEqual(
                NA._declared_files_unverified([os.path.join(tmp, "other.txt")], calls),
                [os.path.join(tmp, "other.txt")])


if __name__ == "__main__":
    unittest.main()

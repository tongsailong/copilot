#!/usr/bin/env python3
"""回复协议兼容层测试：只做纯函数校验，不联网、不读写任务状态。"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import copilot_dispatch  # noqa: E402
import reply_protocol  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "third-reply-old-fields.md")


def plan_with(criteria):
    return {"decision": "stop", "verdict": "pass", "summary": "s",
            "criteria": criteria}


class NormalizePlanTests(unittest.TestCase):
    def test_old_fields_are_mapped_and_originals_preserved(self):
        raw = plan_with([{"id": "C1", "status": "met", "evidence": ["E1"],
                          "reason": "r"}])
        result = reply_protocol.normalize_plan(raw)
        criterion = result["plan"]["criteria"][0]
        self.assertEqual(criterion["criterion_id"], "C1")
        self.assertEqual(criterion["evidence_refs"], ["E1"])
        # 原始字段必须保留，便于审计追溯。
        self.assertEqual(criterion["id"], "C1")
        self.assertEqual(criterion["evidence"], ["E1"])
        self.assertEqual(result["version"], reply_protocol.NORMALIZER_VERSION)
        self.assertEqual([(m["from"], m["to"]) for m in result["mapping"]],
                         [("id", "criterion_id"), ("evidence", "evidence_refs")])
        # 入参不能被修改（纯函数）。
        self.assertNotIn("criterion_id", raw["criteria"][0])

    def test_evidence_ids_is_mapped(self):
        raw = plan_with([{"id": "C1", "status": "met",
                          "evidence_ids": ["E1", "E2"]}])
        result = reply_protocol.normalize_plan(raw)
        criterion = result["plan"]["criteria"][0]
        self.assertEqual(criterion["evidence_refs"], ["E1", "E2"])
        self.assertEqual(criterion["evidence_ids"], ["E1", "E2"])
        self.assertEqual([(m["from"], m["to"]) for m in result["mapping"]],
                         [("id", "criterion_id"),
                          ("evidence_ids", "evidence_refs")])

    def test_evidence_and_evidence_ids_equal_is_normal(self):
        raw = plan_with([{"criterion_id": "C1", "status": "met",
                          "evidence": ["E1"], "evidence_ids": ["E1"]}])
        result = reply_protocol.normalize_plan(raw)
        self.assertEqual(result["plan"]["criteria"][0]["evidence_refs"], ["E1"])
        # 两个别名等值：只记录一次映射（取别名表中靠前者），不重复补字段。
        self.assertEqual([(m["from"], m["to"]) for m in result["mapping"]],
                         [("evidence", "evidence_refs")])

    def test_evidence_and_evidence_ids_conflict_is_rejected(self):
        raw = plan_with([{"criterion_id": "C1", "status": "met",
                          "evidence": ["E1"], "evidence_ids": ["E2"]}])
        with self.assertRaises(reply_protocol.ProtocolError) as ctx:
            reply_protocol.normalize_plan(raw)
        self.assertEqual(ctx.exception.code, "field_conflict")

    def test_canonical_and_evidence_ids_conflict_is_rejected(self):
        raw = plan_with([{"criterion_id": "C1", "status": "met",
                          "evidence_refs": ["E1"], "evidence_ids": ["E2"]}])
        with self.assertRaises(reply_protocol.ProtocolError) as ctx:
            reply_protocol.normalize_plan(raw)
        self.assertEqual(ctx.exception.code, "field_conflict")

    def test_evidence_ids_type_error_is_rejected(self):
        raw = plan_with([{"id": "C1", "status": "met",
                          "evidence_ids": "E1"}])
        with self.assertRaises(reply_protocol.ProtocolError) as ctx:
            reply_protocol.normalize_plan(raw)
        self.assertEqual(ctx.exception.code, "field_type_invalid")

    def test_canonical_only_is_normal(self):
        raw = plan_with([{"criterion_id": "C1", "status": "met",
                          "evidence_refs": ["E1"]}])
        result = reply_protocol.normalize_plan(raw)
        self.assertEqual(result["plan"], raw)
        self.assertEqual(result["mapping"], [])

    def test_both_equal_is_normal(self):
        raw = plan_with([{"criterion_id": "C1", "id": "C1", "status": "met",
                          "evidence_refs": ["E1"], "evidence": ["E1"]}])
        result = reply_protocol.normalize_plan(raw)
        self.assertEqual(result["plan"]["criteria"][0]["criterion_id"], "C1")
        self.assertEqual(result["mapping"], [])

    def test_both_conflicting_is_rejected(self):
        raw = plan_with([{"criterion_id": "C1", "id": "C2", "status": "met",
                          "evidence_refs": ["E1"]}])
        with self.assertRaises(reply_protocol.ProtocolError) as ctx:
            reply_protocol.normalize_plan(raw)
        self.assertEqual(ctx.exception.code, "field_conflict")

    def test_conflicting_evidence_is_rejected(self):
        raw = plan_with([{"criterion_id": "C1", "status": "met",
                          "evidence_refs": ["E1"], "evidence": ["E2"]}])
        with self.assertRaises(reply_protocol.ProtocolError) as ctx:
            reply_protocol.normalize_plan(raw)
        self.assertEqual(ctx.exception.code, "field_conflict")

    def test_id_type_error_is_rejected(self):
        raw = plan_with([{"id": 123, "status": "met", "evidence": ["E1"]}])
        with self.assertRaises(reply_protocol.ProtocolError) as ctx:
            reply_protocol.normalize_plan(raw)
        self.assertEqual(ctx.exception.code, "field_type_invalid")

    def test_invalid_id_value_is_rejected(self):
        raw = plan_with([{"id": "X1", "status": "met", "evidence": ["E1"]}])
        with self.assertRaises(reply_protocol.ProtocolError) as ctx:
            reply_protocol.normalize_plan(raw)
        self.assertEqual(ctx.exception.code, "field_value_invalid")

    def test_invalid_status_is_rejected(self):
        raw = plan_with([{"id": "C1", "status": "done", "evidence": ["E1"]}])
        with self.assertRaises(reply_protocol.ProtocolError) as ctx:
            reply_protocol.normalize_plan(raw)
        self.assertEqual(ctx.exception.code, "field_value_invalid")

    def test_met_without_evidence_is_rejected(self):
        raw = plan_with([{"id": "C1", "status": "met"}])
        with self.assertRaises(reply_protocol.ProtocolError) as ctx:
            reply_protocol.normalize_plan(raw)
        self.assertEqual(ctx.exception.code, "field_missing")

    def test_evidence_type_error_is_rejected(self):
        raw = plan_with([{"id": "C1", "status": "met", "evidence": "E1"}])
        with self.assertRaises(reply_protocol.ProtocolError) as ctx:
            reply_protocol.normalize_plan(raw)
        self.assertEqual(ctx.exception.code, "field_type_invalid")

    def test_evidence_objects_are_validated(self):
        sha = "a" * 64
        raw = plan_with([{"id": "C1", "status": "met",
                          "evidence": [{"id": "E1", "sha256": sha}]}])
        result = reply_protocol.normalize_plan(raw)
        self.assertEqual(result["plan"]["criteria"][0]["evidence_refs"],
                         [{"id": "E1", "sha256": sha}])

    def test_bad_evidence_sha_is_rejected(self):
        raw = plan_with([{"id": "C1", "status": "met",
                          "evidence": [{"id": "E1", "sha256": "short"}]}])
        with self.assertRaises(reply_protocol.ProtocolError) as ctx:
            reply_protocol.normalize_plan(raw)
        self.assertEqual(ctx.exception.code, "field_value_invalid")

    def test_unknown_fields_are_preserved(self):
        raw = plan_with([{"id": "C1", "status": "met", "evidence": ["E1"],
                          "custom": {"keep": True}}])
        result = reply_protocol.normalize_plan(raw)
        self.assertEqual(result["plan"]["criteria"][0]["custom"], {"keep": True})

    def test_non_object_plan_is_rejected(self):
        with self.assertRaises(reply_protocol.ProtocolError) as ctx:
            reply_protocol.normalize_plan([])
        self.assertEqual(ctx.exception.code, "plan_not_object")

    def test_explicit_null_criteria_keeps_legacy_missing_behavior(self):
        raw = {"decision": "continue", "criteria": None,
               "summary": "无逐条条件"}
        result = reply_protocol.normalize_plan(raw)
        self.assertEqual(result["mapping"], [])
        self.assertIsNone(result["plan"]["criteria"])

    def test_explicit_null_criteria_parses_like_legacy(self):
        reply = ("```copilot-plan\n"
                 + json.dumps({"decision": "continue", "criteria": None})
                 + "\n```\n")
        plan = copilot_dispatch.parse_plan(reply)
        self.assertEqual(plan["decision"], "continue")
        self.assertIsNone(plan["criteria"])

    def test_idempotent(self):
        raw = plan_with([{"id": "C1", "status": "met", "evidence": ["E1"]}])
        once = reply_protocol.normalize_plan(raw)["plan"]
        twice = reply_protocol.normalize_plan(once)
        self.assertEqual(twice["plan"], once)
        self.assertEqual(twice["mapping"], [])

    def test_does_not_mutate_input_even_on_failure(self):
        raw = plan_with([{"id": "C1", "status": "met", "evidence": ["E1"]}])
        original = copy.deepcopy(raw)
        reply_protocol.normalize_plan(raw)
        self.assertEqual(raw, original)


class ProtocolVersionTests(unittest.TestCase):
    def test_evidence_ids_rule_bumps_normalizer_version(self):
        # v2 是对外可见的恢复审计字段；规则变化必须同步升级版本。
        self.assertEqual(reply_protocol.NORMALIZER_VERSION, "reply-protocol/v2")


class ParsePlanCompatibilityTests(unittest.TestCase):
    def reply(self, plan: dict) -> str:
        return ("结论。\n\n```copilot-plan\n"
                + json.dumps(plan, ensure_ascii=False) + "\n```\n")

    def test_parse_plan_ex_reports_mapping(self):
        plan, mapping, version = copilot_dispatch.parse_plan_ex(self.reply(
            plan_with([{"id": "C1", "status": "met", "evidence": ["E1"]}])))
        self.assertEqual(plan["criteria"][0]["criterion_id"], "C1")
        self.assertEqual(len(mapping), 2)
        self.assertEqual(version, reply_protocol.NORMALIZER_VERSION)
        # wrapper 保持原调用方式与返回类型。
        self.assertEqual(copilot_dispatch.parse_plan(self.reply(
            plan_with([{"criterion_id": "C1", "status": "met",
                        "evidence_refs": ["E1"]}])))["decision"], "stop")

    def test_conflict_is_reported_as_dispatch_error(self):
        with self.assertRaises(copilot_dispatch.DispatchError) as ctx:
            copilot_dispatch.parse_plan_ex(self.reply(plan_with(
                [{"criterion_id": "C1", "id": "C2", "status": "met",
                  "evidence_refs": ["E1"]}])))
        self.assertIn("冲突", str(ctx.exception))

    def test_real_third_reply_fixture_is_normalized(self):
        with open(FIXTURE, encoding="utf-8") as fh:
            reply = fh.read()
        plan, mapping, _version = copilot_dispatch.parse_plan_ex(reply)
        self.assertEqual(plan["decision"], "stop")
        self.assertEqual(plan["verdict"], "pass")
        refs = {item["criterion_id"]: [
            ref if isinstance(ref, str) else ref["id"]
            for ref in item["evidence_refs"]] for item in plan["criteria"]}
        self.assertEqual(refs, {"C1": ["E2", "E3"], "C2": ["E2", "E3"],
                                "C3": ["E1"]})
        self.assertEqual(len(mapping), 6)
        self.assertTrue(all(m["from"] in ("id", "evidence") for m in mapping))


if __name__ == "__main__":
    unittest.main()

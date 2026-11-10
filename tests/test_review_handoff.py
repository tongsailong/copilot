#!/usr/bin/env python3
"""Reviewer 原始报告自动交接：登记、绑定、幂等、证据包失败关闭。

只做本地状态/文件模拟，不调用真实模型，不触发付费请求。
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

from test_native_bridge import NativeBridgeBase  # noqa: E402

import copilot_auto as A  # noqa: E402
import copilot_dispatch as D  # noqa: E402
from lib_common import sha256_file  # noqa: E402

DISPATCH = os.path.join(SCRIPTS, "copilot_dispatch.py")


class ReviewHandoffBase(NativeBridgeBase):
    def cli(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-B", DISPATCH, *args],
            capture_output=True, text=True, env=env)

    def legacy_state(self, task_id: str = "legacy-review-task") -> dict:
        path = os.path.join(self.dir, f"{task_id}.json")
        state = self._make_state(backend="legacy", task_id=task_id, path=path)
        entry = D.add_report(state, "执行报告：T1 已完成")
        D.save_state(path, state)
        return {
            "path": path,
            "state": D.load_state(path),
            "exec_report_sha256": entry["report_sha256"],
        }

    def write(self, name: str, text: str) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def review_file(self, name: str = "reviewer-report.md", *,
                    marker: str = "REVIEWER-RAW-MARKER") -> str:
        return self.write(name, f"# Reviewer 原始报告\n\n{marker}\n结论：pass\n")

    def native_raw(self, name: str = "native-review.json",
                   **payload) -> str:
        data = {"schema": "copilot-native-review/v1", "verdict": "blocked"}
        data.update(payload)
        return self.write(name, json.dumps(data, ensure_ascii=False))


class NativeRawVerdictFidelityTests(ReviewHandoffBase):
    def test_registration_rejects_verdict_that_rewrites_raw_review(self):
        fixture = self.legacy_state("native-raw-mismatch")
        raw = self.native_raw(verdict="blocked")
        before = len(fixture["state"]["evidence"])
        with self.assertRaises(D.DispatchError) as ctx:
            D.register_review_evidence(
                fixture["state"], file_path=raw,
                review_of=fixture["exec_report_sha256"],
                review_of_kind="execution-report", review_source="legacy",
                verdict="partial",
                round_id=fixture["state"]["rounds"][-1]["round_id"])
        self.assertIn("不一致", str(ctx.exception))
        self.assertEqual(len(fixture["state"]["evidence"]), before)

    def test_report_rejects_verdict_that_differs_from_registered_evidence(self):
        fixture = self.legacy_state("native-raw-report-mismatch")
        raw = self.native_raw(verdict="blocked")
        evidence = D.register_review_evidence(
            fixture["state"], file_path=raw,
            review_of=fixture["exec_report_sha256"],
            review_of_kind="execution-report", review_source="legacy",
            verdict="blocked",
            round_id=fixture["state"]["rounds"][-1]["round_id"])
        with self.assertRaises(D.DispatchError) as ctx:
            D.add_report(
                fixture["state"], "把 blocked 改写成 partial", kind="review",
                review_of=fixture["exec_report_sha256"], verdict="partial",
                review_of_kind="execution-report", evidence_id=evidence["id"],
                raw_report_sha256=evidence["sha256"], review_source="legacy")
        self.assertIn("不一致", str(ctx.exception))

    def test_matching_native_raw_verdict_is_accepted(self):
        fixture = self.legacy_state("native-raw-match")
        raw = self.native_raw(verdict="blocked")
        evidence = D.register_review_evidence(
            fixture["state"], file_path=raw,
            review_of=fixture["exec_report_sha256"],
            review_of_kind="execution-report", review_source="legacy",
            verdict="blocked",
            round_id=fixture["state"]["rounds"][-1]["round_id"])
        report = D.add_report(
            fixture["state"], "原始结论为 blocked", kind="review",
            review_of=fixture["exec_report_sha256"], verdict="blocked",
            review_of_kind="execution-report", evidence_id=evidence["id"],
            raw_report_sha256=evidence["sha256"], review_source="legacy")
        self.assertEqual(report["verdict"], "blocked")
        self.assertEqual(evidence["verdict"], "blocked")

    def test_native_schema_without_valid_verdict_is_rejected(self):
        fixture = self.legacy_state("native-raw-no-verdict")
        for payload in ({"verdict": ""}, {"verdict": "unknown"}):
            with self.subTest(payload=payload):
                raw = self.native_raw(name=f"raw-{payload['verdict'] or 'empty'}.json",
                                      **payload)
                with self.assertRaises(D.DispatchError):
                    D.register_review_evidence(
                        fixture["state"], file_path=raw,
                        review_of=fixture["exec_report_sha256"],
                        review_of_kind="execution-report",
                        review_source="legacy", verdict="blocked",
                        round_id=fixture["state"]["rounds"][-1]["round_id"])

    def test_schema_match_ignores_case_and_surrounding_space(self):
        fixture = self.legacy_state("native-raw-schema-case")
        raw = self.write("raw-case.json", json.dumps({
            "schema": "  Copilot-Native-Review/V1 ", "verdict": "blocked"}))
        evidence = D.register_review_evidence(
            fixture["state"], file_path=raw,
            review_of=fixture["exec_report_sha256"],
            review_of_kind="execution-report", review_source="legacy",
            verdict="blocked",
            round_id=fixture["state"]["rounds"][-1]["round_id"])
        self.assertEqual(evidence["verdict"], "blocked")
        # 大写声明同样受原始结论约束，不能借大小写差异绕过一致性检查。
        with self.assertRaises(D.DispatchError):
            D.register_review_evidence(
                fixture["state"], file_path=raw,
                review_of=fixture["exec_report_sha256"],
                review_of_kind="execution-report", review_source="legacy",
                verdict="partial",
                round_id=fixture["state"]["rounds"][-1]["round_id"])

    def test_truncated_native_json_is_rejected(self):
        fixture = self.legacy_state("native-raw-truncated")
        raw = self.write("raw-truncated.json", (
            '{"schema": "copilot-native-review/v1", "verdict": "blocked"'))
        before = len(fixture["state"]["evidence"])
        with self.assertRaises(D.DispatchError) as ctx:
            D.register_review_evidence(
                fixture["state"], file_path=raw,
                review_of=fixture["exec_report_sha256"],
                review_of_kind="execution-report", review_source="legacy",
                verdict="blocked",
                round_id=fixture["state"]["rounds"][-1]["round_id"])
        self.assertIn("不是", str(ctx.exception))
        self.assertEqual(len(fixture["state"]["evidence"]), before)

    def test_truncated_native_json_variants_still_fail_closed(self):
        fixture = self.legacy_state("native-raw-truncated-variants")
        before = len(fixture["state"]["evidence"])
        for name, text in (
                ("raw-truncated-upper.json",
                 '{"schema": "COPILOT-NATIVE-REVIEW/V1", "verdict": "pass"'),
                ("raw-truncated-escaped.json",
                 '{"schema": "copilot-native-review\\/v1", "verdict": "pass"'),
                ("raw-truncated-unicode.json",
                 '{"schema": "copilot-native-review\\u002fv1", "verdict": "pass"'),
                ("raw-truncated-space.json",
                 '{"schema": "copilot-native-review/v1   ", "verdict": "pass"'),
                ("raw-truncated-escaped-value.json",
                 '{"schema": "copilot-native-rev\\u0069ew/v1", '
                 '"verdict": "pass"'),
                ("raw-truncated-escaped-field.json",
                 '{"sch\\u0065ma": "copilot-native-review/v1", '
                 '"verdict": "pass"'),
                ("raw-truncated-json-no-schema.json",
                 '{"verdict": "pass"'),
                ("raw-truncated-fenced.json",
                 '```json\n{"schema": "copilot-native-review/v1", '
                 '"verdict": "pass"'),
                ("raw-truncated-fenced-escaped.json",
                 '```json\n{"schema": "copilot-native-rev\\u0069ew/v1", '
                 '"verdict": "pass"'),
                ("raw-truncated-fenced-escaped-field.json",
                 '```\n{"sch\\u0065ma": "copilot-native-review/v1", '
                 '"verdict": "pass"'),
                ("raw-truncated-fenced-extra-info.json",
                 '```json extra info\n{"schema": '
                 '"copilot-native-rev\\u0069ew/v1", "verdict": "pass"')):
            with self.subTest(name=name):
                raw = self.write(name, text)
                with self.assertRaises(D.DispatchError):
                    D.register_review_evidence(
                        fixture["state"], file_path=raw,
                        review_of=fixture["exec_report_sha256"],
                        review_of_kind="execution-report",
                        review_source="legacy", verdict="pass",
                        round_id=fixture["state"]["rounds"][-1]["round_id"])
                self.assertEqual(len(fixture["state"]["evidence"]), before)

    def test_prose_report_with_json_fence_stays_legacy(self):
        # 整份报告不是纯 JSON/单围栏 → 只有不声明 native schema 时才按 legacy 文本。
        fixture = self.legacy_state("legacy-prose-fence")
        raw = self.write("raw-prose-fence.md", (
            "# 复核结论\n\n结论：不通过。\n\n```json\n"
            '{"verdict": "pass"}\n```\n'))
        evidence = D.register_review_evidence(
            fixture["state"], file_path=raw,
            review_of=fixture["exec_report_sha256"],
            review_of_kind="execution-report", review_source="legacy",
            verdict="blocked",
            round_id=fixture["state"]["rounds"][-1]["round_id"])
        self.assertEqual(evidence["verdict"], "blocked")

    def test_prose_report_declaring_schema_is_rejected(self):
        # 散文里夹了明文 schema 声明：按「声明了 native schema」拒绝，不放成 legacy。
        fixture = self.legacy_state("legacy-prose-literal-schema")
        raw = self.write("raw-prose-literal.md", (
            "# 复核结论\n\n结论：不通过。\n\n```json\n"
            '{"schema": "copilot-native-review/v1", "verdict": "pass"}\n```\n'))
        before = len(fixture["state"]["evidence"])
        with self.assertRaises(D.DispatchError) as ctx:
            D.register_review_evidence(
                fixture["state"], file_path=raw,
                review_of=fixture["exec_report_sha256"],
                review_of_kind="execution-report", review_source="legacy",
                verdict="blocked",
                round_id=fixture["state"]["rounds"][-1]["round_id"])
        self.assertIn("不是合法 JSON", str(ctx.exception))
        self.assertEqual(len(fixture["state"]["evidence"]), before)

    def test_complete_fenced_native_json_is_registered_and_enforced(self):
        # 与宿主输出容忍单围栏的口径一致：完整围栏内是合法 native JSON 时按原始结论处理。
        fixture = self.legacy_state("native-raw-fenced-complete")
        raw = self.write("raw-fenced-complete.json", (
            '```json\n{"schema": "copilot-native-rev\\u0069ew/v1", '
            '"verdict": "blocked"}\n```\n'))
        evidence = D.register_review_evidence(
            fixture["state"], file_path=raw,
            review_of=fixture["exec_report_sha256"],
            review_of_kind="execution-report", review_source="legacy",
            verdict="blocked",
            round_id=fixture["state"]["rounds"][-1]["round_id"])
        self.assertEqual(evidence["verdict"], "blocked")
        # 原始结论仍然约束登记：不能借围栏把 blocked 报成 pass。
        with self.assertRaises(D.DispatchError):
            D.register_review_evidence(
                fixture["state"], file_path=raw,
                review_of=fixture["exec_report_sha256"],
                review_of_kind="execution-report", review_source="legacy",
                verdict="pass",
                round_id=fixture["state"]["rounds"][-1]["round_id"])

    def test_bom_prefixed_native_json_is_registered(self):
        fixture = self.legacy_state("native-raw-bom")
        raw = self.write("raw-bom.json", "\ufeff" + json.dumps({
            "schema": "copilot-native-review/v1", "verdict": "pass"}))
        evidence = D.register_review_evidence(
            fixture["state"], file_path=raw,
            review_of=fixture["exec_report_sha256"],
            review_of_kind="execution-report", review_source="legacy",
            verdict="pass",
            round_id=fixture["state"]["rounds"][-1]["round_id"])
        self.assertEqual(evidence["verdict"], "pass")

    def test_schema_key_is_case_insensitive(self):
        # `{"Schema": ...}` 同样声明 native schema：必须按原始结论约束，不能当 legacy。
        fixture = self.legacy_state("native-raw-schema-key-case")
        for key in ("Schema", "SCHEMA", " Schema "):
            with self.subTest(key=key):
                raw = self.write(f"raw-key-{key.strip().lower()}.json",
                                 json.dumps({key: "copilot-native-review/v1",
                                             "verdict": "blocked"}))
                with self.assertRaises(D.DispatchError):
                    D.register_review_evidence(
                        fixture["state"], file_path=raw,
                        review_of=fixture["exec_report_sha256"],
                        review_of_kind="execution-report",
                        review_source="legacy", verdict="pass",
                        round_id=fixture["state"]["rounds"][-1]["round_id"])
                with self.assertRaises(D.DispatchError):
                    # 同一文件换裁决值重复登记同样被原始结论拒绝。
                    D.register_review_evidence(
                        fixture["state"], file_path=raw,
                        review_of=fixture["exec_report_sha256"],
                        review_of_kind="execution-report",
                        review_source="legacy", verdict="partial",
                        round_id=fixture["state"]["rounds"][-1]["round_id"])

    def test_fenced_native_json_fence_forms(self):
        # 围栏信息串不限、~~~ 与不同长度围栏、收尾围栏后允许空行；BOM 同时容忍。
        fixture = self.legacy_state("native-raw-fence-forms")
        for name, text in (
                ("fence-tilde.md",
                 '~~~json\n{"schema": "copilot-native-review/v1", '
                 '"verdict": "pass"}\n~~~\n'),
                ("fence-four.md",
                 '````json\n{"schema": "copilot-native-review/v1", '
                 '"verdict": "pass"}\n````\n'),
                ("fence-extra-info.md",
                 '```json extra info\n{"schema": "copilot-native-review/v1", '
                 '"verdict": "pass"}\n```\n'),
                ("fence-blank-after.md",
                 '```json\n{"schema": "copilot-native-review/v1", '
                 '"verdict": "pass"}\n```\n\n'),
                ("bom-fence.md",
                 '\ufeff```json\n{"schema": "copilot-native-review/v1", '
                 '"verdict": "pass"}\n```')):
            with self.subTest(name=name):
                raw = self.write(name, text)
                evidence = D.register_review_evidence(
                    fixture["state"], file_path=raw,
                    review_of=fixture["exec_report_sha256"],
                    review_of_kind="execution-report",
                    review_source="legacy", verdict="pass",
                    round_id=fixture["state"]["rounds"][-1]["round_id"])
                self.assertEqual(evidence["verdict"], "pass")

    def test_schema_declaration_cannot_hide_in_odd_shapes(self):
        # 声明了 native schema 就不能落成 legacy：重复键、嵌套、带后缀、散文+转义围栏、
        # 混合围栏都按「声明」拒绝，原始 verdict 始终有约束。
        fixture = self.legacy_state("native-raw-odd-declarations")
        before = len(fixture["state"]["evidence"])
        for name, text in (
                ("dup-keys.json",
                 '{"schema": "junk", "Schema": "copilot-native-review/v1", '
                 '"verdict": "blocked"}'),
                # 带后缀的声明不能被后面的合法声明覆盖：任一含标识的值都必须精确匹配。
                ("schema-suffix-masked.json",
                 '{"schema": "copilot-native-review/v1 extra", '
                 '"Schema": "copilot-native-review/v1", "verdict": "blocked"}'),
                # 完全同名重复键：json.loads 只保留最后一个值（"junk"），声明层必须兜底拒绝。
                ("dup-same-key-native-first.json",
                 '{"schema": "copilot-native-review/v1", "schema": "junk", '
                 '"verdict": "blocked"}'),
                # 同名键折叠不能用后一个合法声明掩盖前一个非法声明（两种排列都拒绝）。
                ("dup-exact-schema-suffix-first.json",
                 '{"schema": "copilot-native-review/v1 extra", "schema": '
                 '"copilot-native-review/v1", "verdict": "blocked"}'),
                ("dup-exact-schema-suffix-last.json",
                 '{"schema": "copilot-native-review/v1", "schema": '
                 '"copilot-native-review/v1 extra", "verdict": "blocked"}'),
                # 同名 verdict 键同样会被折叠，后一个值能改写原始结论，一律拒绝。
                ("dup-verdict-keys.json",
                 '{"schema": "copilot-native-review/v1", "verdict": "blocked", '
                 '"verdict": "pass"}'),
                # 嵌套层的重复键也拒绝：折叠后无法证明哪一份是原始结论。
                ("dup-nested-keys.json",
                 '{"a": {"k": 1, "k": 2}, "schema": '
                 '"copilot-native-review/v1", "verdict": "blocked"}'),
                # 带首尾空白的字段名：解析后按去空白判定，声明层也必须能识别。
                ("padded-key-truncated.md",
                 '# 结论\n\n```json\n{"schema ": '
                 '"copilot-native-review/v1", "verdict": "blocked"'),
                ("nested-schema.json",
                 '{"a": {"schema": "copilot-native-review/v1"}, '
                 '"verdict": "blocked"}'),
                ("schema-suffix.json",
                 '{"schema": "copilot-native-review/v1 extra", '
                 '"verdict": "blocked"}'),
                ("prose-escaped-fence.md",
                 '# 结论\n\n```json\n{"schema": '
                 '"copilot-native-rev\\u0069ew/v1", "verdict": "blocked"}\n```\n'),
                ("prose-escaped-key.md",
                 '# 结论\n\n```json\n{"sch\\u0065ma": '
                 '"copilot-native-review/v1", "verdict": "blocked"}\n```\n'),
                ("multi-fence-mixed.md",
                 '~~~json\n{"schema": "copilot-native-review/v1", '
                 '"verdict": "blocked"}\n~~~\n```json\n{"schema": '
                 '"copilot-native-review/v1", "verdict": "blocked"}\n```\n')):
            with self.subTest(name=name):
                raw = self.write(name, text)
                with self.assertRaises(D.DispatchError):
                    D.register_review_evidence(
                        fixture["state"], file_path=raw,
                        review_of=fixture["exec_report_sha256"],
                        review_of_kind="execution-report",
                        review_source="legacy", verdict="pass",
                        round_id=fixture["state"]["rounds"][-1]["round_id"])
                self.assertEqual(len(fixture["state"]["evidence"]), before)

    def test_multiple_valid_schema_declarations_stay_native(self):
        # 多个合法声明不是冲突：仍按原始 verdict 约束登记（与「任一非法即拒绝」互补）。
        fixture = self.legacy_state("native-raw-multi-valid-schema")
        raw = self.write("multi-valid-schema.json", json.dumps({
            "schema": "copilot-native-review/v1",
            "Schema": "Copilot-Native-Review/V1", "verdict": "pass"}))
        evidence = D.register_review_evidence(
            fixture["state"], file_path=raw,
            review_of=fixture["exec_report_sha256"],
            review_of_kind="execution-report", review_source="legacy",
            verdict="pass",
            round_id=fixture["state"]["rounds"][-1]["round_id"])
        self.assertEqual(evidence["verdict"], "pass")

    def test_duplicate_keys_rejected_even_in_legacy_json(self):
        # 重复键检查不限于 native 声明：任何 JSON 原始结论被折叠后都无法证明哪一份
        # 是 Reviewer 的实际 verdict，因此一律失败关闭且状态不变。
        fixture = self.legacy_state("legacy-raw-duplicate-verdict")
        before = len(fixture["state"]["evidence"])
        raw = self.write("legacy-dup-verdict.json",
                         '{"verdict": "blocked", "verdict": "pass"}')
        with self.assertRaises(D.DispatchError):
            D.register_review_evidence(
                fixture["state"], file_path=raw,
                review_of=fixture["exec_report_sha256"],
                review_of_kind="execution-report", review_source="legacy",
                verdict="pass",
                round_id=fixture["state"]["rounds"][-1]["round_id"])
        self.assertEqual(len(fixture["state"]["evidence"]), before)

    def test_padded_schema_key_parsed_stays_native(self):
        # 声明层放宽到「引号内允许空白」，解析成功时 `_schema_fields` 的去空白口径一致：
        # 合法 native JSON 仍按原始 verdict 登记，不因键名空白被误拒。
        fixture = self.legacy_state("native-raw-padded-key")
        raw = self.write("padded-key.json", json.dumps({
            "schema ": "copilot-native-review/v1", "verdict": "pass"}))
        evidence = D.register_review_evidence(
            fixture["state"], file_path=raw,
            review_of=fixture["exec_report_sha256"],
            review_of_kind="execution-report", review_source="legacy",
            verdict="pass",
            round_id=fixture["state"]["rounds"][-1]["round_id"])
        self.assertEqual(evidence["verdict"], "pass")

    def test_multi_fence_legacy_text_without_declaration_stays_legacy(self):
        # 多围栏只对「声明了 native schema」的报告失败关闭；不带声明的普通评审文本
        # 仍按 legacy 登记（与 docs 的限定口径一致）。
        fixture = self.legacy_state("legacy-multi-fence")
        raw = self.write("legacy-multi-fence.md",
                         '# 结论\n\n```text\ndiff 片段\n```\n\n'
                         '```python\nprint(1)\n```\n')
        evidence = D.register_review_evidence(
            fixture["state"], file_path=raw,
            review_of=fixture["exec_report_sha256"],
            review_of_kind="execution-report", review_source="legacy",
            verdict="pass",
            round_id=fixture["state"]["rounds"][-1]["round_id"])
        self.assertEqual(evidence["verdict"], "pass")

    def test_schema_field_must_be_a_string(self):
        fixture = self.legacy_state("native-raw-schema-list")
        raw = self.write("raw-schema-list.json", json.dumps({
            "schema": ["copilot-native-review/v1"], "verdict": "pass"}))
        with self.assertRaises(D.DispatchError):
            D.register_review_evidence(
                fixture["state"], file_path=raw,
                review_of=fixture["exec_report_sha256"],
                review_of_kind="execution-report", review_source="legacy",
                verdict="pass",
                round_id=fixture["state"]["rounds"][-1]["round_id"])


class LegacyReviewRegistrationTests(ReviewHandoffBase):
    def test_cli_registers_raw_file_and_is_idempotent(self):
        fixture = self.legacy_state()
        raw = self.review_file()
        args = [
            "report", "--state", fixture["path"], "--kind", "review",
            "--file", raw, "--review-of", fixture["exec_report_sha256"],
            "--review-of-kind", "execution-report", "--verdict", "pass",
        ]
        first = self.cli(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        state = D.load_state(fixture["path"])
        self.assertEqual([r["kind"] for r in state["reports"]],
                         ["execution", "review"])
        report = state["reports"][-1]
        self.assertEqual(report["review_source"], "legacy")
        self.assertEqual(report["review_of_kind"], "execution-report")
        self.assertEqual(report["verdict"], "pass")
        self.assertFalse(report["g4_eligible"])
        evidence = D._find_evidence_by_id(state, report["evidence_id"])
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["origin"], "review-report")
        self.assertEqual(evidence["review_source"], "legacy")
        self.assertEqual(evidence["sha256"], sha256_file(raw))
        self.assertEqual(evidence["sha256"], report["raw_report_sha256"])
        self.assertEqual(evidence["task_id"], state["task_id"])
        prompt = A._build_prompt(state, {"phase": "GUIDANCE_NEEDED"}, "")
        self.assertIn("legacy 复核报告", prompt)
        self.assertNotIn("原生独立复核报告", prompt)
        self.assertIn(evidence["id"], prompt)
        self.assertIn(evidence["sha256"], prompt)
        before = {k: v for k, v in state.items() if k != "updated"}

        second = self.cli(*args)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("重复登记", second.stderr)
        after_state = D.load_state(fixture["path"])
        self.assertEqual(len(after_state["evidence"]), len(state["evidence"]))
        self.assertEqual(len(after_state["reports"]), len(state["reports"]))
        after = {k: v for k, v in after_state.items() if k != "updated"}
        self.assertEqual(json.dumps(after, ensure_ascii=False, sort_keys=True),
                         json.dumps(before, ensure_ascii=False, sort_keys=True))

    def test_evidence_target_binds_to_registered_evidence_sha(self):
        fixture = self.legacy_state("legacy-evidence-target")
        source = self.write("artifact.txt", "被复核的产物\n")
        D.register_evidence(fixture["state"], "E5", file_path=source,
                            kind="artifact", note="被复核产物")
        D.save_state(fixture["path"], fixture["state"])
        sha = sha256_file(source)
        raw = self.review_file()
        proc = self.cli(
            "report", "--state", fixture["path"], "--kind", "review",
            "--file", raw, "--review-of", sha,
            "--review-of-kind", "evidence", "--verdict", "partial")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = D.load_state(fixture["path"])
        report = state["reports"][-1]
        self.assertEqual(report["review_of"], sha)
        self.assertEqual(report["review_of_kind"], "evidence")
        self.assertEqual(report["verdict"], "partial")
        self.assertFalse(report["g4_eligible"])
        # 复核原文是普通已登记证据，可被完成条件显式引用；但不会自动判定条件达成。
        self.assertEqual(
            D._validate_evidence_refs(state, [report["evidence_id"]]),
            [report["evidence_id"]])

    def test_missing_file_wrong_target_and_cross_task_are_rejected(self):
        fixture = self.legacy_state("legacy-reject-task")
        raw = self.review_file()
        missing = self.cli(
            "report", "--state", fixture["path"], "--kind", "review",
            "--file", os.path.join(self.dir, "missing-review.md"),
            "--review-of", fixture["exec_report_sha256"],
            "--review-of-kind", "execution-report", "--verdict", "pass")
        self.assertNotEqual(missing.returncode, 0)
        wrong = self.cli(
            "report", "--state", fixture["path"], "--kind", "review",
            "--file", raw, "--review-of", "0" * 64,
            "--review-of-kind", "execution-report", "--verdict", "pass")
        self.assertNotEqual(wrong.returncode, 0)

        source = self.write("cross-task.txt", "跨任务证据\n")
        D.register_evidence(fixture["state"], "E6", file_path=source)
        fixture["state"]["evidence"][-1]["task_id"] = "other-task"
        D.save_state(fixture["path"], fixture["state"])
        cross = self.cli(
            "report", "--state", fixture["path"], "--kind", "review",
            "--file", raw, "--review-of", sha256_file(source),
            "--review-of-kind", "evidence", "--verdict", "pass")
        self.assertNotEqual(cross.returncode, 0)
        self.assertIn("不是本任务", cross.stderr)

    def test_pack_selects_latest_and_criteria_referenced_reviews_only(self):
        fixture = self.legacy_state("legacy-select-task")
        state = fixture["state"]
        old_round = "legacy-select-task-r0"
        state["rounds"].insert(0, {
            "round_id": old_round, "call_id": "legacy-select-call-0",
            "reply_sha256": "0" * 64, "model_identity_ok": True,
            "mode": "PLAN", "decision": "continue", "verdict": "partial",
            "ts": D.now_iso()})
        old_target = self.write("old-target.txt", "旧复核对象\n")
        D.register_evidence(state, "E20", file_path=old_target,
                            kind="artifact")
        unrelated_target = self.write("unrelated-target.txt", "无关复核对象\n")
        D.register_evidence(state, "E21", file_path=unrelated_target,
                            kind="artifact")

        old_raw = self.review_file("old-review.md", marker="OLD-REVIEW-MARKER")
        old_ev = D.register_review_evidence(
            state, file_path=old_raw, review_of=sha256_file(old_target),
            review_of_kind="evidence", review_source="legacy",
            verdict="partial", round_id=old_round)
        D.add_report(
            state, "旧复核", round_id=old_round, kind="review",
            review_of=sha256_file(old_target), verdict="partial",
            review_of_kind="evidence", evidence_id=old_ev["id"],
            raw_report_sha256=old_ev["sha256"], review_source="legacy")

        unrelated_raw = self.review_file(
            "unrelated-review.md", marker="UNRELATED-REVIEW-MARKER")
        unrelated_ev = D.register_review_evidence(
            state, file_path=unrelated_raw, review_of=sha256_file(unrelated_target),
            review_of_kind="evidence", review_source="legacy",
            verdict="pass", round_id=old_round)
        D.add_report(
            state, "无关复核", round_id=old_round, kind="review",
            review_of=sha256_file(unrelated_target), verdict="pass",
            review_of_kind="evidence", evidence_id=unrelated_ev["id"],
            raw_report_sha256=unrelated_ev["sha256"], review_source="legacy")

        latest_raw = self.review_file(
            "latest-review.md", marker="LATEST-REVIEW-MARKER")
        latest_ev = D.register_review_evidence(
            state, file_path=latest_raw,
            review_of=fixture["exec_report_sha256"],
            review_of_kind="execution-report", review_source="legacy",
            verdict="pass", round_id=state["rounds"][-1]["round_id"])
        D.add_report(
            state, "最新复核", round_id=state["rounds"][-1]["round_id"],
            kind="review", review_of=fixture["exec_report_sha256"],
            verdict="pass", review_of_kind="execution-report",
            evidence_id=latest_ev["id"],
            raw_report_sha256=latest_ev["sha256"], review_source="legacy")

        state["contract"]["criteria"][0]["evidence_refs"] = [
            {"id": old_ev["id"], "sha256": old_ev["sha256"]}]
        D.save_state(fixture["path"], state)
        state = D.load_state(fixture["path"])
        pack = A._build_auto_pack(os.path.join(self.dir, "auto-select"),
                                  state, [], "")
        self.assertTrue(pack)
        with open(pack, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("OLD-REVIEW-MARKER", body)
        self.assertIn("LATEST-REVIEW-MARKER", body)
        self.assertNotIn("UNRELATED-REVIEW-MARKER", body)

    def test_same_target_different_raw_report_is_rejected(self):
        fixture = self.legacy_state("legacy-tamper-task")
        raw = self.review_file(marker="第一份原始报告")
        first = self.cli(
            "report", "--state", fixture["path"], "--kind", "review",
            "--file", raw, "--review-of", fixture["exec_report_sha256"],
            "--review-of-kind", "execution-report", "--verdict", "pass")
        self.assertEqual(first.returncode, 0, first.stderr)
        before = D.load_state(fixture["path"])
        with open(raw, "a", encoding="utf-8") as fh:
            fh.write("被替换的结论\n")
        second = self.cli(
            "report", "--state", fixture["path"], "--kind", "review",
            "--file", raw, "--review-of", fixture["exec_report_sha256"],
            "--review-of-kind", "execution-report", "--verdict", "pass")
        self.assertNotEqual(second.returncode, 0)
        after = D.load_state(fixture["path"])
        self.assertEqual(len(after["reports"]), len(before["reports"]))
        self.assertEqual(len(after["evidence"]), len(before["evidence"]))


class NativeReviewHandoffTests(ReviewHandoffBase):
    def setUp(self):
        super().setUp()
        self.dispatch, self.state = self.emit()
        self.result = self.execution(self.state, self.dispatch)
        A._apply_native_execution(self.state_path, self.state, self.result)
        self.state = D.load_state(self.state_path)
        self.review_env = self.review(self.state, self.dispatch, self.result)

    def apply_review(self, *, source_file: str = "") -> dict:
        return A._apply_native_review(
            self.state_path, self.state, self.review_env,
            source_file=source_file)

    def test_native_review_registers_raw_file_and_dedupes(self):
        raw = os.path.join(self.dir, "native-review.json")
        with open(raw, "w", encoding="utf-8") as fh:
            json.dump(self.review_env, fh, ensure_ascii=False, sort_keys=True)
        info = self.apply_review(source_file=raw)
        self.assertFalse(info["duplicate"])
        state = D.load_state(self.state_path)
        self.assertEqual(state["native_reviews"][-1]["evidence_id"],
                         info["evidence_id"])
        evidence = D._find_evidence_by_id(state, info["evidence_id"])
        self.assertEqual(evidence["origin"], "review-report")
        self.assertEqual(evidence["review_source"], "native")
        self.assertEqual(evidence["review_of_kind"], D.NATIVE_REVIEW_OF_KIND)
        self.assertEqual(evidence["path_hint"], os.path.abspath(raw))
        self.assertEqual(evidence["sha256"], sha256_file(raw))
        self.assertEqual(state["reports"][-1]["evidence_id"], evidence["id"])
        self.assertEqual(state["reports"][-1]["raw_report_sha256"],
                         evidence["sha256"])
        self.assertFalse(state["reports"][-1]["g4_eligible"])

        before = json.dumps(state, ensure_ascii=False, sort_keys=True)
        again = A._apply_native_review(
            self.state_path, state, self.review_env, source_file=raw)
        self.assertTrue(again["duplicate"])
        after = D.load_state(self.state_path)
        self.assertEqual(len(after["evidence"]), len(state["evidence"]))
        self.assertEqual(json.dumps(after, ensure_ascii=False, sort_keys=True),
                         before)

    def test_pack_and_prompt_carry_raw_review_not_only_summary(self):
        self.apply_review()
        state = D.load_state(self.state_path)
        workdir = os.path.join(self.dir, "auto")
        pack = A._build_auto_pack(workdir, state, [], "")
        self.assertTrue(pack)
        with open(pack, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("copilot-native-review/v1", body)
        self.assertIn("native-task", body)
        # 摘要只含结构化结论，不应替代原始信封；原文必须已经在包里。
        prompt = A._build_prompt(state, {"phase": "GUIDANCE_NEEDED"}, "")
        evidence_id = state["reports"][-1]["evidence_id"]
        evidence = D._find_evidence_by_id(state, evidence_id)
        self.assertIn(evidence_id, prompt)
        self.assertIn(evidence["sha256"], prompt)
        self.assertIn("review_of_kind", prompt)
        self.assertIn("摘要仅辅助", prompt)

    def test_tampered_or_missing_raw_review_fails_pack_closed(self):
        raw = os.path.join(self.dir, "native-review-tamper.json")
        with open(raw, "w", encoding="utf-8") as fh:
            json.dump(self.review_env, fh, ensure_ascii=False, sort_keys=True)
        self.apply_review(source_file=raw)
        state = D.load_state(self.state_path)
        with open(raw, "a", encoding="utf-8") as fh:
            fh.write("TAMPERED\n")
        with self.assertRaises(A.AutoError) as ctx:
            A._build_auto_pack(os.path.join(self.dir, "auto-tamper"), state, [], "")
        self.assertIn("摘要不一致", str(ctx.exception))
        os.remove(raw)
        with self.assertRaises(A.AutoError) as ctx:
            A._build_auto_pack(os.path.join(self.dir, "auto-missing"), state, [], "")
        self.assertIn("不存在", str(ctx.exception))

    def test_no_review_leaves_normal_pack_and_prompt_behavior(self):
        state = D.load_state(self.state_path)
        self.assertIsNone(A._build_auto_pack(
            os.path.join(self.dir, "auto-none"), state, [], ""))
        prompt = A._build_prompt(state, {"phase": "GUIDANCE_NEEDED"}, "")
        self.assertNotIn("复核原始证据", prompt)


class PackFailClosedTests(ReviewHandoffBase):
    def legacy_with_review(self, raw_text: str) -> tuple[dict, str]:
        fixture = self.legacy_state(f"legacy-pack-{len(raw_text)}")
        raw = self.write("huge-review.md", raw_text)
        evidence = D.register_review_evidence(
            fixture["state"], file_path=raw,
            review_of=fixture["exec_report_sha256"],
            review_of_kind="execution-report", review_source="legacy",
            verdict="pass", round_id=fixture["state"]["rounds"][-1]["round_id"],
            note="大报告")
        D.add_report(
            fixture["state"], "Legacy 复核报告摘要", kind="review",
            review_of=fixture["exec_report_sha256"], verdict="pass",
            review_of_kind="execution-report", evidence_id=evidence["id"],
            raw_report_sha256=evidence["sha256"], review_source="legacy")
        D.save_state(fixture["path"], fixture["state"])
        return D.load_state(fixture["path"]), raw

    def test_truncated_review_report_fails_closed(self):
        state, _ = self.legacy_with_review("这是一行复核原始记录。\n" * 2500)
        with self.assertRaises(A.AutoError) as ctx:
            A._build_auto_pack(os.path.join(self.dir, "auto-truncated"),
                               state, [], "")
        self.assertIn("未能完整进入证据包", str(ctx.exception))

    def test_review_omitted_by_total_budget_fails_closed(self):
        state, _ = self.legacy_with_review("短复核记录。\n" * 20)
        manifest = []
        for i in range(1, 4):
            path = self.write(f"big-{i}.txt", "这是一行大证据。\n" * 2400)
            manifest.append({"id": f"E{20 + i}", "file": path,
                             "note": "大证据", "include": True})
        with self.assertRaises(A.AutoError) as ctx:
            A._build_auto_pack(os.path.join(self.dir, "auto-omitted"),
                               state, manifest, "")
        self.assertIn("未能完整进入证据包", str(ctx.exception))
        self.assertIn("总预算", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

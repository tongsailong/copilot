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

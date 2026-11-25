#!/usr/bin/env python3
"""知识交接块回归：read 摘录必须来自实际卡片，index-only/unverified 不得冒充已读。"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import v6_support as F  # noqa: F401  # 把 scripts/ 加入 sys.path

import copilot_dispatch as CD
import knowledge_handoff as KH


class HandoffBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = os.path.join(self.tmp.name, "vault")
        self.memory = os.path.join(self.tmp.name, "memory")
        os.makedirs(self.vault, exist_ok=True)
        os.makedirs(self.memory, exist_ok=True)
        self.card = self.write_card(
            self.memory, "ad-cooldown.md",
            "# 开屏冷却\n\n开屏长冷却从 60 分钟改为 2 分钟，第 4 次起生效。\n")
        self.old_roots = KH.ALLOWED_ROOTS
        KH.ALLOWED_ROOTS = (self.vault, self.memory)
        self.addCleanup(setattr, KH, "ALLOWED_ROOTS", self.old_roots)

    @staticmethod
    def write_card(root: str, name: str, body: str) -> str:
        path = os.path.join(root, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def entry(self, **overrides) -> dict:
        base = {
            "id": "K1",
            "path": self.card,
            "kind": "memory",
            "status": "read",
            "verified_at": "2026-11-18",
            "decision": "开屏长冷却为 2 分钟",
            "constraints": ["第 4 次起生效"],
            "excerpt": "开屏长冷却从 60 分钟改为 2 分钟，第 4 次起生效。",
            "impact": "改冷却时必须保持 2 分钟基线",
        }
        base.update(overrides)
        return base

    def write_input(self, entries: list[dict]) -> str:
        path = os.path.join(self.tmp.name, "handoff.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"entries": entries}, fh, ensure_ascii=False)
        return path


class ValidationTests(HandoffBase):
    def test_read_requires_excerpt_present_in_card(self):
        entries = KH.load_handoff(self.write_input([self.entry()]))
        text = KH.render(entries, target="cognition")
        self.assertIn("主代理声明已读", text)
        self.assertIn("开屏长冷却从 60 分钟改为 2 分钟", text)
        self.assertIn(self.card, text)

    def test_read_evidence_is_rendered_but_not_verified(self):
        entries = KH.load_handoff(self.write_input([self.entry(
            read_evidence="sess_demo: Read <card> at 15:00")]))
        text = KH.render(entries, target="cognition")
        self.assertIn("阅读证据：sess_demo: Read <card> at 15:00", text)
        self.assertIn("不证明宿主工具调用", text)

    def test_read_rejects_fabricated_excerpt(self):
        path = self.write_input([self.entry(excerpt="卡片里没有这句话")])
        with self.assertRaises(KH.HandoffError) as ctx:
            KH.load_handoff(path)
        self.assertEqual(ctx.exception.code, "EXCERPT_NOT_FOUND")

    def test_read_requires_decision_and_excerpt(self):
        path = self.write_input([self.entry(decision="")])
        with self.assertRaises(KH.HandoffError) as ctx:
            KH.load_handoff(path)
        self.assertEqual(ctx.exception.code, "DECISION_REQUIRED")
        path = self.write_input([self.entry(excerpt="")])
        with self.assertRaises(KH.HandoffError) as ctx:
            KH.load_handoff(path)
        self.assertEqual(ctx.exception.code, "EXCERPT_REQUIRED")

    def test_index_only_cannot_carry_decision_or_excerpt(self):
        for field in ({"excerpt": "正文"}, {"decision": "已确认"}):
            path = self.write_input([self.entry(
                status="index-only", **field)])
            with self.assertRaises(KH.HandoffError) as ctx:
                KH.load_handoff(path)
            self.assertEqual(ctx.exception.code, "INDEX_ONLY_CLAIM")

    def test_index_only_is_labeled(self):
        entries = KH.load_handoff(self.write_input([self.entry(
            status="index-only", decision="", excerpt="")]))
        text = KH.render(entries, target="cognition")
        self.assertIn("index-only（仅索引，未读正文", text)
        self.assertNotIn("历史决策", text)

    def test_unverified_decision_without_excerpt_is_rejected(self):
        path = self.write_input([self.entry(
            status="unverified", decision="可能是 2 分钟", excerpt="")])
        with self.assertRaises(KH.HandoffError) as ctx:
            KH.load_handoff(path)
        self.assertEqual(ctx.exception.code, "UNVERIFIED_CLAIM")

    def test_unverified_decision_is_labeled_as_pending(self):
        entries = KH.load_handoff(self.write_input([self.entry(
            status="unverified", decision="可能是 2 分钟",
            excerpt="待核实的线索")]))
        text = KH.render(entries, target="cognition")
        self.assertIn("待核实线索：可能是 2 分钟", text)
        self.assertIn("未核实摘录", text)
        self.assertNotIn("历史决策", text)

    def test_out_of_scope_and_sensitive_paths_are_rejected(self):
        outside = os.path.join(self.tmp.name, "outside.md")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("outside")
        with self.assertRaises(KH.HandoffError) as ctx:
            KH.load_handoff(self.write_input([self.entry(path=outside)]))
        self.assertEqual(ctx.exception.code, "OUT_OF_SCOPE")
        secret = self.write_card(self.memory, "secret-card.md", "secret")
        with self.assertRaises(KH.HandoffError) as ctx:
            KH.load_handoff(self.write_input([self.entry(path=secret)]))
        self.assertEqual(ctx.exception.code, "SENSITIVE_PATH")

    def test_too_many_entries_and_budget(self):
        entries = [self.entry(id=f"K{i}") for i in range(1, 7)]
        with self.assertRaises(KH.HandoffError) as ctx:
            KH.load_handoff(self.write_input(entries))
        self.assertEqual(ctx.exception.code, "TOO_MANY_ENTRIES")

        long_card = self.write_card(
            self.memory, "long.md", "# long\n\n" + ("x" * 2000) + "\n")
        five = [self.entry(id=f"K{i}", path=long_card,
                           excerpt=("x" * 1500)) for i in range(1, 6)]
        loaded = KH.load_handoff(self.write_input(five))
        with self.assertRaises(KH.HandoffError) as ctx:
            KH.render(loaded, target="subagent")
        self.assertEqual(ctx.exception.code, "EXCERPT_BUDGET")


class CliTests(HandoffBase):
    def test_build_writes_block_and_check_summarizes(self):
        source = self.write_input([self.entry()])
        output = os.path.join(self.tmp.name, "handoff.md")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = KH.main(["build", "--input", source, "--output", output])
        self.assertEqual(rc, 0)
        summary = json.loads(out.getvalue())
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["read"], 1)
        with open(output, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("<!-- copilot-knowledge-handoff -->", text)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = KH.main(["check", "--input", source])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])

    def test_cli_error_is_json_and_exit_2(self):
        source = self.write_input([self.entry(excerpt="not in card")])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = KH.main(["check", "--input", source])
        self.assertEqual(rc, 2)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "EXCERPT_NOT_FOUND")


class DispatchCompositionTests(HandoffBase):
    def test_compose_appends_block_and_metadata(self):
        source = self.write_input([self.entry()])
        materials, meta = CD._compose_materials("## 当前证据\n\n测试通过", source)
        self.assertIn("## 当前证据", materials)
        self.assertIn("<!-- copilot-knowledge-handoff -->", materials)
        self.assertEqual(meta["knowledge_entries"], 1)
        self.assertEqual(len(meta["knowledge_block_sha256"]), 64)

    def test_compose_rejects_duplicate_handoff_block(self):
        source = self.write_input([self.entry()])
        with self.assertRaises(CD.CLIError) as ctx:
            CD._compose_materials(
                "<!-- copilot-knowledge-handoff -->\n已有", source)
        self.assertIn("KNOWLEDGE_BLOCK_DUPLICATE", str(ctx.exception))

    def test_compose_allows_inline_marker_mention(self):
        source = self.write_input([self.entry()])
        materials, meta = CD._compose_materials(
            "说明：输出带 `<!-- copilot-knowledge-handoff -->` 标记", source)
        self.assertIn("说明：输出带", materials)
        self.assertEqual(meta["knowledge_entries"], 1)

    def test_compose_without_knowledge_file_is_noop(self):
        materials, meta = CD._compose_materials("正文", "")
        self.assertEqual(materials, "正文")
        self.assertEqual(meta, {})

    def test_knowledge_block_reaches_cognition_prompt(self):
        import cognition_call as CC
        source = self.write_input([self.entry()])
        block = KH.render_file(source, target="cognition")
        prompt = CC.build_prompt(
            {"goal": "改冷却", "contract": {"done_criteria_text": "C1"},
             "risk": "medium"},
            "当前证据\n\n" + block, mode="REVIEW")
        self.assertIn("开屏长冷却从 60 分钟改为 2 分钟", prompt)
        self.assertIn("知识交接（主 Agent 核验", prompt)


if __name__ == "__main__":
    unittest.main()

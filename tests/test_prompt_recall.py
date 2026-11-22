#!/usr/bin/env python3
"""自动召回回归：分类、预算、去重、跨项目过滤、降级与状态隔离。

这些测试不访问真实 Memory/Vault：用临时 git 仓库 + 临时知识根复现真实目录结构，
因此可以在任何机器上重复运行，也不会把测试内容写进正式知识库。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE) + "/scripts"
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import context_engine as CE  # noqa: E402
import prompt_recall as PR  # noqa: E402

ENGINEERING = "帮我修复 stop hook 的 session 绑定问题，报错在 scripts/guard.py"


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class RecallBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = self.tmp.name
        self.repo = os.path.join(base, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True,
                       capture_output=True)
        self.state_root = os.path.join(base, "state")
        self.memory_root = os.path.join(base, "memories")
        self.vault_root = os.path.join(base, "vault")
        _, hash16 = CE.project_slug(os.path.realpath(self.repo))
        self.memory_dir = os.path.join(self.memory_root, "demo-" + hash16, "memory")
        write(os.path.join(self.memory_dir, "MEMORY.md"), "\n".join([
            "# Memory Index",
            "- [Hook 续跑经验](hook-note.md) — stop hook 续跑与 session 绑定",
            "- [召回链路](pipeline-note.md) — memory 召回 capture 候选去重",
            "",
        ]))
        write(os.path.join(self.memory_dir, "hook-note.md"), "\n".join([
            "---",
            "name: hook-note",
            "description: stop hook 续跑",
            "metadata:",
            "  type: project",
            "---",
            "- stop hook 续跑必须绑定 session；已验证 exit 2 生效。",
            "",
        ]))
        write(os.path.join(self.memory_dir, "pipeline-note.md"), "\n".join([
            "---",
            "name: pipeline-note",
            "description: memory 召回与 capture",
            "metadata:",
            "  type: project",
            "---",
            "- memory 召回 capture 候选需要去重后再写入。",
            "",
        ]))
        write(os.path.join(self.vault_root, "15-Engineering-Memory", "demo",
                           "hook-session.md"), "\n".join([
            "---",
            "type: engineering-memory",
            "status: active",
            "last_verified: 2026-11-20",
            "source_commit: abc1234",
            "---",
            "# stop hook 与 session 绑定",
            "stop hook 续跑：session 绑定后 exit 2 生效，已验证。",
            "",
        ]))
        write(os.path.join(self.vault_root, "15-Engineering-Memory", "other",
                           "unrelated.md"), "\n".join([
            "---",
            "type: engineering-memory",
            "status: active",
            "last_verified: 2026-08-01",
            "---",
            "# 广告与充电测试",
            "test session 复盘：充电广告与温度无关。",
            "",
        ]))

    def recall(self, prompt: str, *, session: str = "s1", now: float = 1000,
               memory_root: str | None = None, vault_root: str | None = None,
               state_root: str | None = None) -> dict:
        return PR.maybe_inject(
            {"prompt": prompt, "cwd": self.repo, "session_id": session},
            memory_root=self.memory_root if memory_root is None else memory_root,
            vault_root=self.vault_root if vault_root is None else vault_root,
            state_root=self.state_root if state_root is None else state_root,
            now=now)


class ClassifyTests(unittest.TestCase):
    def test_social_and_short_prompts_do_not_trigger(self):
        for prompt in ("你好", "只回复 ok", "继续", "谢谢"):
            self.assertFalse(PR.classify(prompt)["engineering"], prompt)

    def test_pure_question_does_not_trigger(self):
        self.assertFalse(PR.classify("什么是二分查找")["engineering"])
        self.assertFalse(PR.classify("解释一下 CAP 定理")["engineering"])

    def test_engineering_prompt_triggers(self):
        verdict = PR.classify(ENGINEERING)
        self.assertTrue(verdict["engineering"])
        self.assertIn("hook", verdict["terms"])
        self.assertTrue(PR.classify("把 README 里的安装步骤更新一下")["engineering"])

    def test_terms_normalize_and_drop_secrets(self):
        terms, strong = PR.extract_terms(
            "修 hook 时看到 token 0123456789abcdef0123456789abcdef0123456789abcdef")
        self.assertIn("hook", terms)
        self.assertTrue(strong)
        self.assertFalse(any("0123456789abcdef" in term for term in terms))


class MaybeInjectTests(RecallBase):
    def test_engineering_prompt_injects_memory_and_vault(self):
        result = self.recall(ENGINEERING)
        self.assertTrue(result["injected"], result)
        text = result["text"]
        self.assertIn("<copilot-recall>", text)
        self.assertIn("Memory/hook-note.md", text)
        self.assertIn("Vault/15-Engineering-Memory/demo/hook-session.md", text)
        self.assertIn("核验 2026-11-20", text)
        self.assertIn("基线 abc1234", text)
        self.assertNotIn("unrelated.md", text, "通用词不得带进无关项目卡片")
        self.assertLessEqual(len(text), PR.INJECT_LIMIT)
        self.assertTrue(os.path.isfile(result["state_path"]))

    def test_same_topic_second_turn_is_deduped(self):
        first = self.recall(ENGINEERING, now=1000)
        self.assertTrue(first["injected"])
        second = self.recall(ENGINEERING, now=1001)
        self.assertFalse(second["injected"])
        self.assertEqual(second["reason"], "repeat")
        self.assertEqual(second["elapsed_ms"] < 50, True)

    def test_new_topic_allows_incremental_recall(self):
        self.recall(ENGINEERING, now=1000)
        second = self.recall("帮我修 memory 召回 capture 的候选去重", now=1001)
        self.assertTrue(second["injected"], second)
        self.assertEqual(second["reason"], "incremental")

    def test_session_cap_limits_total_recalls(self):
        first = self.recall(ENGINEERING, now=1000)
        self.assertTrue(first["injected"])
        second = self.recall("帮我修 memory 召回 capture 的候选去重", now=1001)
        self.assertTrue(second["injected"])
        with open(first["state_path"], encoding="utf-8") as fh:
            state = json.load(fh)
        project = next(iter(state["projects"].values()))
        self.assertEqual(project["recall_count"], 2)
        project["recall_count"] = PR.SESSION_RECALL_CAP
        PR._write_state(first["state_path"], state)
        capped = self.recall("看看 CI workflow 的构建配置", now=1002)
        self.assertFalse(capped["injected"])
        self.assertEqual(capped["reason"], "session-cap")

    def test_non_engineering_prompt_does_not_write_state(self):
        result = self.recall("你好")
        self.assertFalse(result["injected"])
        self.assertEqual(result["reason"], "social")
        self.assertFalse(os.path.exists(os.path.join(self.state_root, "sessions")))
        self.assertFalse(os.path.exists(os.path.join(self.state_root, "events.jsonl")))

    def test_no_project_root_skips_recall(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        result = PR.maybe_inject(
            {"prompt": ENGINEERING, "cwd": outside.name, "session_id": "s1"},
            memory_root=self.memory_root, vault_root=self.vault_root,
            state_root=self.state_root)
        self.assertFalse(result["injected"])
        self.assertEqual(result["reason"], "no-project-root")

    def test_unreadable_sources_degrade_with_visible_event(self):
        broken_memory = os.path.join(self.tmp.name, "not-a-dir")
        write(broken_memory, "x")
        result = self.recall(ENGINEERING, memory_root=broken_memory,
                             vault_root=os.path.join(self.tmp.name, "missing"))
        self.assertFalse(result["injected"])
        self.assertIn(result["reason"], ("no-hits", "zero_hit", "failed"))
        events = os.path.join(self.state_root, "events.jsonl")
        self.assertTrue(os.path.isfile(events))
        with open(events, encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh]
        self.assertTrue(any(r.get("decision") in ("no-hits", "zero_hit", "failed")
                            for r in records))

    def test_secret_never_lands_in_state_or_injection(self):
        secret = "0123456789abcdef0123456789abcdef0123456789abcdef"
        result = self.recall(f"修 hook 时看到 token {secret}")
        if result["injected"]:
            self.assertNotIn(secret, result["text"])
        state_file = result["state_path"]
        if os.path.isfile(state_file):
            with open(state_file, encoding="utf-8") as fh:
                self.assertNotIn(secret, fh.read())

    def test_resume_snapshot_reuses_without_search(self):
        first = self.recall(ENGINEERING)
        self.assertTrue(first["injected"])
        lines = PR.session_recall_lines("s1", self.repo, state_root=self.state_root)
        self.assertTrue(lines)
        joined = "\n".join(lines)
        self.assertIn("Memory/hook-note.md", joined)
        self.assertIn("不重复检索", joined)

    def test_cross_project_state_is_isolated(self):
        self.recall(ENGINEERING, session="s1")
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        os.makedirs(os.path.join(other.name, "repo"))
        subprocess.run(["git", "init", "-q"], cwd=os.path.join(other.name, "repo"),
                       check=True, capture_output=True)
        result = PR.maybe_inject(
            {"prompt": ENGINEERING, "cwd": os.path.join(other.name, "repo"),
             "session_id": "s1"},
            memory_root=self.memory_root, vault_root=self.vault_root,
            state_root=self.state_root)
        self.assertTrue(result["injected"], result)
        self.assertEqual(result["reason"], "initial", "不同项目不能共享召回去重")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""通用只读 Profile 与正式 G4 边界的对齐测试。

v6.6.1 起工作区只有一个通用只读权限载体 `independent-readonly.md`：它只向宿主声明
Read/Grep/Glob，不定义 Reviewer 身份、复核流程或输出契约。G4 按宿主实际生效的工具
策略判定，Profile 名称/路径不参与。本测试锁定文件权限声明，防止后续改动悄悄放宽边界，
并锁定“旧专用 reviewer Profile 不再存在”。
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
AGENTS_DIR = os.path.join(ROOT, ".zcode", "agents")
PROFILE = os.path.join(AGENTS_DIR, "independent-readonly.md")
LEGACY_PROFILE = os.path.join(AGENTS_DIR, "reviewer.md")


def read_profile() -> str:
    with open(PROFILE, encoding="utf-8") as fh:
        return fh.read()


def frontmatter() -> dict[str, str]:
    text = read_profile()
    if not text.startswith("---\n"):
        raise AssertionError("independent-readonly.md 缺少 frontmatter")
    body = text.split("---\n", 2)[1]
    fields: dict[str, str] = {}
    for line in body.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


class WorkspaceProfileTests(unittest.TestCase):
    def test_legacy_reviewer_profile_is_gone(self):
        self.assertFalse(os.path.exists(LEGACY_PROFILE),
                         "旧专用 reviewer Profile 必须已删除")

    def test_generic_profile_exists(self):
        self.assertTrue(os.path.isfile(PROFILE))
        self.assertEqual(frontmatter().get("name"), "independent-readonly")

    def test_agents_dir_has_only_the_generic_profile(self):
        # 旧式只读/含 Bash 的额外 Profile 文件不允许悄悄回到工作区。
        entries = sorted(os.listdir(AGENTS_DIR))
        self.assertEqual(entries, ["independent-readonly.md"], entries)

    def test_profile_tools_are_exactly_read_only(self):
        fields = frontmatter()
        self.assertEqual(fields.get("tools"), "[Read, Grep, Glob]")
        self.assertEqual(fields.get("model"), "inherit")
        self.assertEqual(fields.get("permissionMode"), "default")
        self.assertEqual(fields.get("injectAgentsMd"), "false")

    def test_profile_disallows_bash_and_writes(self):
        fields = frontmatter()
        disallowed = fields.get("disallowedTools", "")
        for tool in ("Write", "Edit", "Bash", "WebFetch", "WebSearch",
                     "Agent", "Task", "TodoWrite", "NotebookEdit"):
            self.assertIn(tool, disallowed, f"{tool} 必须被禁用")
        self.assertNotIn("mcpServers", fields)
        self.assertNotIn("skills", fields)

    def test_profile_prompt_stays_generic(self):
        text = read_profile()
        self.assertIn("Bash is not available", text)
        self.assertIn("Use only Read, Grep and Glob", text)
        # Profile 不再承担 Reviewer 身份、输出契约或复核生命周期。
        for marker in ("copilot-native-review", "review_attempt_id",
                       "verdict", "review_scope", "G4"):
            self.assertNotIn(marker, text, f"通用 Profile 不应包含 {marker}")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""工作区只读 Reviewer Profile 与正式 G4 边界的对齐测试。

旧正式 G4 允许 Reviewer 使用只读 Bash；阶段九退役该机制后，Reviewer 只允许
Read/Grep/Glob，Bash 一律拒绝。本测试锁定工作区 Profile 文件的实际权限声明，
防止后续改动悄悄放宽边界。
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROFILE = os.path.join(ROOT, ".zcode", "agents", "reviewer.md")


def frontmatter() -> dict[str, str]:
    with open(PROFILE, encoding="utf-8") as fh:
        text = fh.read()
    if not text.startswith("---\n"):
        raise AssertionError("reviewer.md 缺少 frontmatter")
    body = text.split("---\n", 2)[1]
    fields: dict[str, str] = {}
    for line in body.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


class WorkspaceProfileTests(unittest.TestCase):
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

    def test_profile_prompt_forbids_shell_commands(self):
        with open(PROFILE, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("Bash is not available", text)
        self.assertIn("Use only Read, Grep and Glob", text)


if __name__ == "__main__":
    unittest.main()

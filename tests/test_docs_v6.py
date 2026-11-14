#!/usr/bin/env python3
"""v6 文档契约：Skill 入口、references、Reviewer 与项目文档与代码一致。

这些断言保护“文档不会把 SOL 重新写回活动路径”，以及 v6 的关键运行契约
（本地治理、PARTIAL 持续推进、正式 REVIEW opt-in、G4 fail-closed、v2 信封）
没有在文档中被静默删除或放宽。
"""
from __future__ import annotations

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SKILL = os.path.join(ROOT, "SKILL.md")
REFERENCES = os.path.join(ROOT, "references")
REVIEWER = os.path.join(ROOT, "agents", "reviewer.md")
ACTIVE_REFS = ("workflow.md", "runtime.md", "review.md", "agent-bridge.md")
HISTORICAL_REFS = ("role.md", "role-consult.md", "role-plan.md", "role-review.md",
                   "role-map.md", "review-ledger.md")
DISPATCH = os.path.join(ROOT, "scripts", "copilot_dispatch.py")


def text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def current_version() -> str:
    """以代码里的版本号为准，避免文档测试钉死某个历史版本。"""
    match = re.search(r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"',
                      text(DISPATCH), re.M)
    if not match:
        raise AssertionError("copilot_dispatch.py 缺少 __version__")
    return match.group(1)


def raw(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def frontmatter(document: str) -> str:
    match = re.search(r"^---\n(.*?)\n---\n", document, re.S)
    if not match:
        raise AssertionError("缺少 frontmatter")
    return match.group(1)


class SkillEntryTests(unittest.TestCase):
    def setUp(self):
        self.doc = text(SKILL)
        self.front = frontmatter(self.doc)

    def test_loader_limits(self):
        self.assertLessEqual(len(raw(SKILL)), 100_000)
        self.assertIn("when_to_use:", self.front)
        self.assertNotIn("whenToUse:", self.front)
        dm = re.search(r'description: "(.*?)"', self.front, re.S)
        self.assertIsNotNone(dm)
        self.assertLessEqual(len(dm.group(1)), 1024)
        self.assertLessEqual(len(re.search(r"when_to_use: >\n(.*?)\n---", self.doc, re.S)
                                 .group(1)), 1024)

    def test_version_and_retirement(self):
        self.assertIn(f"v{current_version()}", self.doc)
        self.assertIn(f"# copilot：轻量协作治理（v{current_version()}）", self.doc)
        self.assertIn("SOL 已退役", self.doc)
        self.assertIn("legacy-status", self.doc)
        self.assertNotIn("唯一正式 GPT/SOL 入口", self.doc)
        # 不得再把 SOl 写成可调用入口：只有在“退役/历史”语境下允许出现模型名。
        for line in self.doc.splitlines():
            if "gpt-6.1-sol" in line:
                self.assertIn("退役", line)

    def test_lightweight_governance_and_partial_persistence(self):
        self.assertIn("不替代技术决策", self.doc)
        self.assertIn("PARTIAL", self.doc)
        self.assertIn("不是任务终点", self.doc)
        self.assertIn("AUTO-REPLAN", self.doc)
        self.assertIn("AUTO-G4", self.doc)
        self.assertIn("人为轮次", self.doc)

    def test_formal_review_is_opt_in_not_gate(self):
        self.assertIn("authorize-review", self.doc)
        self.assertIn("仅在用户明确授权", self.doc)
        self.assertIn("不是普通开发", self.doc)
        self.assertIn("--amend", self.doc)
        self.assertIn("--supersedes", self.doc)

    def test_blocked_controlled_writes_are_documented(self):
        self.assertIn("BLOCKED 下的受控写入", self.doc)
        self.assertIn("不会解除阻塞", self.doc)
        self.assertIn("evidence --drop", self.doc)

    def test_four_gates_and_native_reviewer(self):
        for gate in ("G1", "G2", "G3", "G4"):
            self.assertIn(gate, self.doc)
        self.assertIn("fail-closed", self.doc)
        self.assertIn("g4_eligible", self.doc)
        self.assertIn("copilot-native-review/v2", self.doc)
        self.assertIn("--create-attempt", self.doc)
        self.assertIn("review_attempt_id", self.doc)
        self.assertIn("代码范围快照", self.doc)
        self.assertIn("--workspace", self.doc)
        # 高风险与 native 后端的绑定必须写进入口，否则会留下无法收口的任务。
        self.assertIn("--risk high", self.doc)
        self.assertIn("--backend native", self.doc)

    def test_reference_routing_lists_all_active_docs(self):
        for name in ACTIVE_REFS + ("history.md",):
            self.assertIn(f"references/{name}", self.doc)


class ReferenceTests(unittest.TestCase):
    def test_active_references_exist_and_have_core_contracts(self):
        o = {name: text(os.path.join(REFERENCES, name)) for name in ACTIVE_REFS}
        self.assertIn("legacy-status", o["workflow.md"])
        self.assertIn("evidence_tampered", o["workflow.md"])
        self.assertIn("MAX_DRIVE_STEPS", o["runtime.md"])
        self.assertIn("PARTIAL 不是终点", o["runtime.md"])
        self.assertIn("G4", o["review.md"])
        self.assertIn("在任一门失败时拒绝", o["review.md"])
        self.assertIn("copilot-native-dispatch/v2", o["agent-bridge.md"])
        self.assertIn("host_tool_calls_changed", o["agent-bridge.md"])
        self.assertIn("native_review_host_changed", o["review.md"])
        self.assertIn("--authorized-by user", o["workflow.md"])
        self.assertIn("退出码 0", o["workflow.md"])
        self.assertIn("--amend", o["workflow.md"])
        self.assertIn("--supersedes", o["workflow.md"])
        self.assertIn("BLOCKED 下的受控写入", o["runtime.md"])
        self.assertIn("amendment", o["review.md"])
        self.assertIn("superseded", o["review.md"])
        self.assertIn("--create-attempt", o["review.md"])
        self.assertIn("native_review_attempt_workspace_drift", o["review.md"])
        self.assertIn("native_review_code_scope_drift", o["review.md"])
        self.assertIn("code_snapshot.files", o["agent-bridge.md"] + text(REVIEWER))
        self.assertIn("review_attempt_id", o["agent-bridge.md"])
        self.assertIn("review_attempt_created_at", o["agent-bridge.md"])
        self.assertIn("--create-attempt", o["workflow.md"])
        for name, doc in o.items():
            self.assertNotIn("gpt-6.1-sol", doc, f"{name} 不应把 SOL 写成活动路径")
            self.assertNotIn("call_gpt.py --", doc, name)

    def test_historical_references_are_marked_retired(self):
        for name in HISTORICAL_REFS:
            doc = text(os.path.join(REFERENCES, name))
            self.assertIn("历史文档", doc, f"{name} 缺少退役横幅")
            self.assertIn("退役", doc, f"{name} 未说明退役")

    def test_history_records_v6_retirement(self):
        doc = text(os.path.join(REFERENCES, "history.md"))
        self.assertIn("v6.0.0（2026-11-12）", doc)
        self.assertIn("SOL 决策通道已于 v6.0.0", doc)
        self.assertIn("legacy/", doc)


class ReviewerProfileTests(unittest.TestCase):
    def test_reviewer_uses_v2_bindings_and_no_self_declared_g4(self):
        doc = text(REVIEWER)
        front = frontmatter(doc)
        self.assertIn("copilot-native-review/v2", doc)
        self.assertIn("plan_id", doc)
        self.assertIn("plan_sha256", doc)
        self.assertNotIn("round_id", doc)
        self.assertNotIn("gpt-6.1-sol", doc)
        self.assertIn("Do **not** declare `g4_eligible`", doc)
        self.assertIn("tools: [Read, Grep, Glob, Bash]", front)
        self.assertIn("disallowedTools: [Write, Edit]", front)
        self.assertIn("model: inherit", front)


class ProjectDocTests(unittest.TestCase):
    def test_agents_md_points_to_v6_architecture(self):
        doc = text(os.path.join(ROOT, "AGENTS.md"))
        self.assertIn("SOL 决策通道退役", doc)
        self.assertIn("PARTIAL 不是终点", doc)
        self.assertIn("legacy/tests", doc)
        self.assertNotIn("SOL 固定", doc)
        self.assertNotIn("唯一正式 GPT/SOL 入口", doc)

    def test_charter_and_roadmap_mark_sol_retired(self):
        charter = text(os.path.join(ROOT, "docs", "PROJECT-CHARTER.md"))
        roadmap = text(os.path.join(ROOT, "docs", "ROADMAP.md"))
        self.assertIn("SOL 已于 v6.0.0 退役", charter)
        self.assertIn("唯一工程执行与技术决策主体", charter)
        self.assertIn("已退役", roadmap)

    def test_status_and_decisions_record_v6(self):
        status = text(os.path.join(ROOT, "docs", "STATUS.md"))
        decisions = text(os.path.join(ROOT, "docs", "DECISIONS.md"))
        self.assertIn("v6.0.0", status)
        self.assertIn("SOL 决策通道退役", decisions)
        self.assertIn("本地治理引擎", decisions)

    def test_readme_does_not_present_sol_as_active(self):
        doc = text(os.path.join(ROOT, "README.md"))
        self.assertIn("退役", doc)
        self.assertIn("governance.py", doc)
        self.assertIn("legacy/", doc)


if __name__ == "__main__":
    unittest.main()

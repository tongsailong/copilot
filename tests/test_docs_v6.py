#!/usr/bin/env python3
"""v8.4 文档契约：Skill 入口、references、SOL THINK/REVIEW 与项目文档与代码一致。

这些断言保护“SOL 只提供认知输入、不是决策通道”，以及 v8.4 的关键运行契约
（本地治理、PARTIAL 持续推进、正式 REVIEW opt-in、默认 gpt-6.2-astra + high、零工具单轮、
THINK advisory 不入门禁、REVIEW 仅用户要求、G4 退役）。
"""
from __future__ import annotations

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SKILL = os.path.join(ROOT, "SKILL.md")
REFERENCES = os.path.join(ROOT, "references")
READONLY_PROFILE = os.path.join(ROOT, ".zcode", "agents",
                                "independent-readonly.md")
LEGACY_PROFILE = os.path.join(ROOT, ".zcode", "agents", "reviewer.md")
ACTIVE_REFS = ("workflow.md", "runtime.md", "review.md", "agent-bridge.md")
HISTORICAL_REFS = ("review-ledger.md",)
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

    def test_version_and_sol_review_only(self):
        self.assertIn(f"v{current_version()}", self.doc)
        self.assertIn(f"# copilot：轻量协作治理（v{current_version()}）", self.doc)
        # 单轮复核仍是入口规则；完整的版本沿革迁到 references/history.md，
        # 入口只保留当前摘要、不变量与指针（见 v9.8.0 文档瘦身）。
        self.assertIn("零工具单轮", self.doc)
        self.assertIn("references/history.md", self.doc)
        history = text(os.path.join(REFERENCES, "history.md"))
        self.assertIn("SOL 零工具单轮复核", history)
        self.assertIn("legacy-status", self.doc)
        self.assertNotIn("唯一正式 GPT/SOL 入口", self.doc)
        # SOL 只是建议：不得恢复决策通道或 DECIDE。
        self.assertNotIn("SOL 决策通道已退役。", self.doc)
        self.assertNotIn('"DECIDE"', self.doc)
        for line in self.doc.splitlines():
            if "gpt-6.2-sol" in line:
                self.assertNotIn("决策通道", line)
        # 模型升级必须写进当前版本说明，并且不得残留旧模型为现行默认配置。
        self.assertIn("gpt-6.2-astra", self.doc)
        self.assertIn("gpt-6.2-sol", self.doc)
        self.assertIn("reasoning.effort=\"high\"", self.doc)
        self.assertIn("--cognition", self.doc)
        # 当前命令面是 sol-consult / sol-plan；THINK 只作读取别名，sol-advisory 只允许作为历史说明。
        self.assertIn("sol-consult", self.doc)
        self.assertIn("sol-plan", self.doc)
        self.assertIn("THINK 保留为只读别名", self.doc)
        self.assertIn("THINK 只作读取别名", history)

    def test_think_and_review_are_advisory_not_gates(self):
        self.assertIn("sol_advisories", self.doc)
        self.assertIn("THINK", self.doc)
        self.assertIn("REVIEW", self.doc)
        # 认知判断与复核都不得成为门禁或自动工作项来源。
        self.assertIn("不进入 Finding 台账", self.doc)
        self.assertIn("不影响 G1–G3", self.doc)
        # REVIEW 只在用户明确要求时执行，不因风险或阶段自动派生。
        self.assertIn("只在用户明确要求", self.doc)

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

    def test_gates_and_sol_review(self):
        for gate in ("G1", "G2", "G3"):
            self.assertIn(gate, self.doc)
        self.assertIn("G4", self.doc)
        self.assertIn("已正式退役", self.doc)
        self.assertIn("fail-closed", self.doc)
        # SOL 单轮复核必须写进入口，并明确是 advisory。
        self.assertIn("sol-review", self.doc)
        self.assertIn("单轮", self.doc)
        self.assertIn("零工具", self.doc)
        self.assertIn("advisory", self.doc)
        # 高风险不再绑定 native 后端。
        self.assertIn("--risk high", self.doc)
        self.assertIn("不再绑定 `--backend native`", self.doc)

    def test_reference_routing_lists_all_active_docs(self):
        for name in ACTIVE_REFS + ("history.md",):
            self.assertIn(f"references/{name}", self.doc)


class ReferenceTests(unittest.TestCase):
    def test_active_references_exist_and_have_core_contracts(self):
        o = {name: text(os.path.join(REFERENCES, name)) for name in ACTIVE_REFS}
        self.assertIn("legacy-status", o["workflow.md"])
        self.assertIn("evidence_tampered", o["workflow.md"])
        self.assertIn("sol-review", o["workflow.md"])
        self.assertIn("sol-think", o["workflow.md"])
        self.assertIn("sol_reviews", o["review.md"])
        self.assertIn("sol_advisories", o["review.md"])
        self.assertIn("gpt-6.2-astra", o["review.md"])
        self.assertIn("gpt-6.2-sol", o["review.md"])
        self.assertIn("单轮", o["review.md"])
        self.assertIn("零工具", o["review.md"])
        self.assertIn("已正式退役", o["review.md"])
        self.assertIn("MAX_DRIVE_STEPS", o["runtime.md"])
        self.assertIn("PARTIAL 不是终点", o["runtime.md"])
        self.assertIn("G4", o["review.md"])
        self.assertIn("在任一门失败时拒绝", o["review.md"])
        self.assertIn("copilot-native-dispatch/v2", o["agent-bridge.md"])
        self.assertIn("native_review_retired", o["agent-bridge.md"])
        self.assertIn("--authorized-by user", o["workflow.md"])
        self.assertIn("退出码 0", o["workflow.md"])
        self.assertIn("--amend", o["workflow.md"])
        self.assertIn("--supersedes", o["workflow.md"])
        self.assertIn("BLOCKED 下的受控写入", o["runtime.md"])
        self.assertIn("amendment", o["review.md"])
        self.assertIn("superseded", o["review.md"])
        self.assertIn("code_snapshot", o["agent-bridge.md"])
        self.assertIn("review_attempt", o["agent-bridge.md"])
        for name, doc in o.items():
            self.assertNotIn("call_gpt.py --", doc, name)
        self.assertIn("native_review_retired", o["agent-bridge.md"])

    def test_historical_references_are_marked_retired(self):
        for name in HISTORICAL_REFS:
            doc = text(os.path.join(REFERENCES, name))
            self.assertIn("历史文档", doc, f"{name} 缺少退役横幅")
            self.assertIn("退役", doc, f"{name} 未说明退役")

    def test_design_review_keeps_two_modes_and_knowledge_boundary(self):
        # 设计咨询是可选证据输入：认知模式可无图但不得冒充看过画面。
        doc = text(os.path.join(REFERENCES, "design-review.md"))
        for phrase in ("认知咨询（可无图）", "视觉咨询（必须有图）",
                       "未看画面", "不得评价像素", "跳过是正常结果",
                       "创造性问题", "九问是视觉咨询的通用兜底",
                       "项目视觉 DNA", "不自动成为其他项目的默认审美",
                       "不重复付费"):
            self.assertIn(phrase, doc)
        self.assertIn("不是审批节点", doc)
        self.assertIn("不改变决策主体", doc)
        self.assertIn("references/design-review.md",
                      text(SKILL))

    def test_history_records_v6_and_v8(self):
        doc = text(os.path.join(REFERENCES, "history.md"))
        self.assertIn("v6.0.0（2026-11-12）", doc)
        self.assertIn("SOL 决策通道已于 v6.0.0", doc)
        self.assertIn("v7.0.0（2026-11-17）", doc)
        self.assertIn("v8.1.0", doc)
        self.assertIn("gpt-6.2-sol", doc)
        self.assertIn("SOL 零工具单轮复核", doc)
        self.assertIn("v9.0.0", doc)
        self.assertIn("legacy/", doc)


class ReadonlyProfileTests(unittest.TestCase):
    def test_workspace_profile_is_generic_read_only(self):
        doc = text(READONLY_PROFILE)
        front = frontmatter(doc)
        self.assertIn("tools: [Read, Grep, Glob]", front)
        self.assertIn("Bash", front.split("disallowedTools:", 1)[1].split("\n", 1)[0])
        self.assertIn("model: inherit", front)
        self.assertIn("name: independent-readonly", front)
        # 通用 Profile 只提供宿主权限，不承担复核输出契约或生命周期。
        self.assertNotIn("copilot-native-review/v2", doc)
        self.assertNotIn("plan_id", doc)
        self.assertNotIn("gpt-6.1-sol", doc)
        self.assertNotIn("gpt-6.2-sol", doc)
        self.assertNotIn("round_id", doc)

    def test_legacy_profiles_are_gone(self):
        self.assertFalse(os.path.exists(LEGACY_PROFILE),
                         "旧专用 reviewer 工作区 Profile 已删除")
        self.assertFalse(os.path.exists(os.path.join(ROOT, "agents", "reviewer.md")),
                         "旧 Reviewer 全局 Profile 已退役，不应回到源码")


class ProjectDocTests(unittest.TestCase):
    def test_agents_md_points_to_v8_architecture(self):
        doc = text(os.path.join(ROOT, "AGENTS.md"))
        self.assertIn("唯一远程调用是 SOL（CONSULT / PLAN / REVIEW）", doc)
        self.assertIn("gpt-6.2-astra", doc)
        self.assertIn("gpt-6.2-sol", doc)
        self.assertIn("PARTIAL 不是终点", doc)
        self.assertIn("legacy/tests", doc)
        self.assertIn("native_review_retired", doc)
        self.assertIn("G4 原生只读复核正式退役", doc)
        self.assertIn("G1–G3", doc)
        self.assertNotIn("SOL 固定", doc)
        self.assertNotIn("唯一正式 GPT/SOL 入口", doc)

    def test_charter_and_roadmap_mark_sol_advisory_only(self):
        charter = text(os.path.join(ROOT, "docs", "PROJECT-CHARTER.md"))
        roadmap = text(os.path.join(ROOT, "docs", "ROADMAP.md"))
        self.assertIn("v9.0.0 按生命周期恢复 CONSULT / PLAN", charter)
        self.assertIn("唯一工程执行与技术决策主体", charter)
        self.assertIn("v7.0", roadmap)
        self.assertIn("v8.0", roadmap)
        self.assertIn("v8.1", roadmap)
        self.assertIn("Phase 13（v9.0）", roadmap)

    def test_status_and_decisions_record_versions(self):
        status = text(os.path.join(ROOT, "docs", "STATUS.md"))
        decisions = text(os.path.join(ROOT, "docs", "DECISIONS.md"))
        self.assertIn("v6.0.0", status)
        self.assertIn("SOL 决策通道退役", decisions)
        self.assertIn("本地治理引擎", decisions)
        self.assertIn("v7.0.0", decisions)
        self.assertIn("SOL 零工具单轮", decisions)
        self.assertIn("v8.0.0", decisions)
        self.assertIn("v8.1.0", decisions)
        self.assertIn("gpt-6.2-astra", decisions)
        self.assertIn("gpt-6.2-sol", decisions)
        self.assertIn("G4", decisions)

    def test_readme_presents_sol_review_as_active(self):
        doc = text(os.path.join(ROOT, "README.md"))
        self.assertIn("SOL 零工具单轮复核", doc)
        self.assertIn("governance.py", doc)
        self.assertIn("legacy/", doc)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Context Engine 回归：项目识别、有界召回、上下文包、沉淀草稿与恢复接线。

这些测试只用本地夹具（临时工作区 + 假 Vault + 假 Memory 根），不访问真实知识库、
不调用模型、不写工作区外的状态。重点覆盖两类相反风险：

* 串库/误用历史：同名不同路径的项目、缺失的项目记忆、过期或未核实的卡片不得被
  静默当成当前事实；
* 静默失败：零命中、检索失败、预算截断必须可区分，缺上下文模块时恢复接线要降级
  而不是阻断会话启动。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import context_engine as CE  # noqa: E402
import copilot_stop_guard as GUARD  # noqa: E402
import copilot_dispatch as DISPATCH  # noqa: E402

CARD = """---
last_verified: 2026-11-01
source_commit: {commit}
status: active
project: demo
---

# 部署基线

部署必须走原子替换，失败时保留上一版本并可回滚。
"""


def git(cwd: str, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "-c", "init.defaultBranch=main", *args],
        cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {args} 失败: {proc.stderr}")
    return proc.stdout.strip()


class ContextEngineBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = self.tmp.name
        self.memory_root = os.path.join(self.base, "memories")
        self.vault_root = os.path.join(self.base, "vault")
        os.makedirs(self.memory_root)
        os.makedirs(self.vault_root)

    # -- 夹具 ---------------------------------------------------------------

    def make_workspace(self, name: str = "demo", *, commit: bool = True) -> str:
        ws = os.path.join(self.base, "ws-" + name + "-" + str(len(os.listdir(self.base))))
        os.makedirs(ws)
        git(ws, "init", "-q")
        if commit:
            git(ws, "commit", "-q", "--allow-empty", "-m", "init")
        return ws

    def make_memory(self, workspace: str, *, prefix: str = "",
                    index: str = "# Memory Index\n\n- [部署约定](deploy.md) — 原子替换\n") -> str:
        _slug, hash16 = CE.project_slug(os.path.realpath(workspace))
        base = os.path.basename(os.path.realpath(workspace)).rstrip(os.sep)
        name = f"{prefix}{base}-{hash16}"
        memory = os.path.join(self.memory_root, name, "memory")
        os.makedirs(memory)
        with open(os.path.join(memory, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write(index)
        return memory

    def make_card(self, rel: str, text: str) -> str:
        path = os.path.join(self.vault_root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def resolve(self, workspace: str) -> dict:
        return CE.resolve(workspace, memory_root=self.memory_root,
                          vault_root=self.vault_root)

    def recall(self, workspace: str, keywords: str, **kwargs) -> dict:
        project = self.resolve(workspace)
        terms = [t.strip() for t in re.split(r"[|,]", keywords) if t.strip()]
        return CE.recall(project, terms, scope=kwargs.pop("scope", ["vault", "memory"]),
                         **kwargs)


class ResolveTests(ContextEngineBase):
    def test_project_slug_matches_workspace_realpath(self):
        ws = self.make_workspace("demo")
        info = self.resolve(ws)
        base, hash16 = CE.project_slug(os.path.realpath(ws))
        self.assertEqual(info["schema"], "copilot-context-resolve/v1")
        self.assertEqual(info["project"]["hash16"], hash16)
        self.assertEqual(info["project"]["slug"], base)
        self.assertEqual(info["project"]["basename"], os.path.basename(ws))

    def test_same_basename_different_paths_do_not_share_memory(self):
        first = self.make_workspace("dup")
        second = self.make_workspace("dup")
        self.make_memory(first)
        one = self.resolve(first)
        two = self.resolve(second)
        self.assertTrue(one["memory_available"])
        self.assertEqual(one["memory"][0]["status"], "available")
        self.assertEqual(two["memory"][0]["status"], "absent")
        self.assertFalse(two["memory_available"])
        self.assertNotEqual(one["memory"][0].get("name"), two["memory"][0].get("name"))

    def test_absent_memory_is_not_unreadable(self):
        ws = self.make_workspace("plain")
        info = self.resolve(ws)
        self.assertEqual(info["memory"][0]["status"], "absent")

    def test_unreadable_memory_root_is_reported(self):
        ws = self.make_workspace("plain")
        project = CE.resolve(ws, memory_root=os.path.join(self.base, "missing"),
                             vault_root=self.vault_root)
        self.assertEqual(project["memory"][0]["status"], "unreadable")
        self.assertFalse(project["memory_available"])

    def test_unstable_memory_dirs_are_not_trusted(self):
        ws = self.make_workspace("demo")
        self.make_memory(ws, prefix="draft-")
        info = self.resolve(ws)
        self.assertFalse(info["memory_available"])
        self.assertTrue(all(not m["stable"] for m in info["memory"]))

    def test_git_baseline_is_reported(self):
        ws = self.make_workspace("demo")
        info = self.resolve(ws)
        self.assertTrue(info["git"]["available"])
        self.assertRegex(info["git"]["head"], r"^[0-9a-f]{40}$")
        self.assertEqual(info["git"]["branch"], "main")


class RecallTests(ContextEngineBase):
    def setUp(self):
        super().setUp()
        self.ws = self.make_workspace("demo")
        self.make_memory(self.ws)
        self.card = self.make_card("15-Engineering-Memory/demo/deploy.md",
                                   CARD.format(commit="a" * 40))

    def test_hit_carries_path_line_snippet_and_metadata(self):
        result = self.recall(self.ws, "原子替换")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["hits"])
        hit = next(h for h in result["hits"] if h["source"] == "vault")
        self.assertEqual(hit["rel"], "15-Engineering-Memory/demo/deploy.md")
        self.assertGreaterEqual(hit["line"], 1)
        self.assertIn("原子替换", hit["snippet"])
        self.assertEqual(hit["meta"]["last_verified"], "2026-11-01")
        self.assertEqual(hit["meta"]["source_commit"], "a" * 40)
        self.assertEqual(hit["path"], self.card)

    def test_memory_hits_use_short_relative_path(self):
        result = self.recall(self.ws, "不存在的词", scope=["memory"])
        self.assertEqual(result["status"], "zero_hit")
        memory = self.recall(self.ws, "部署约定", scope=["memory"])
        self.assertEqual(memory["status"], "ok")
        self.assertEqual(memory["hits"][0]["rel"], "memory/MEMORY.md")

    def test_zero_hit_is_not_failure(self):
        result = self.recall(self.ws, "不存在的词")
        self.assertEqual(result["status"], "zero_hit")
        self.assertTrue(result["ok"])
        self.assertFalse(result["hits"])

    def test_only_whitelisted_vault_paths_are_scanned(self):
        self.make_card("01-Inbox/candidate.md", "# 候选\n\n原子替换 的候选草稿\n")
        self.make_card("50-Private/notes.md", "# 私有\n\n原子替换 的私有笔记\n")
        result = self.recall(self.ws, "原子替换", scope=["vault"])
        rels = {hit["rel"] for hit in result["hits"]}
        self.assertNotIn("01-Inbox/candidate.md", rels)
        self.assertNotIn("50-Private/notes.md", rels)
        self.assertIn("15-Engineering-Memory/demo/deploy.md", rels)

    def test_missing_vault_is_absent_not_zero_hit(self):
        result = CE.recall(CE.resolve(self.ws, memory_root=self.memory_root,
                                      vault_root=os.path.join(self.base, "novault")),
                           ["原子替换"], scope=["vault"])
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any("不可用" in note for note in result["notes"]))

    def test_budget_truncation_is_explicit(self):
        lines = "\n".join(f"- 原子替换 第 {i} 行" for i in range(30))
        self.make_card("10-Products/demo.md", "# 产品\n" + lines + "\n")
        result = self.recall(self.ws, "原子替换", scope=["vault"], max_lines=3)
        self.assertEqual(len(result["hits"]), 3)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertTrue(any("预算截断" in note for note in result["notes"]))

    def test_cache_hit_and_invalidation(self):
        cache_dir = os.path.join(self.base, "cache")
        first = self.recall(self.ws, "原子替换", cache_dir=cache_dir)
        self.assertEqual(first["cache"], "miss")
        second = self.recall(self.ws, "原子替换", cache_dir=cache_dir)
        self.assertEqual(second["cache"], "hit")
        os.utime(self.card, (1, 1))
        third = self.recall(self.ws, "原子替换", cache_dir=cache_dir)
        self.assertEqual(third["cache"], "miss")

    def test_ledger_records_query_without_content_dump(self):
        state = self.make_state()
        proc = subprocess.run(
            [sys.executable, "-B", os.path.join(SCRIPTS, "context_engine.py"),
             "recall", "--workspace", self.ws, "--keywords", "原子替换",
             "--memory-root", self.memory_root, "--vault-root", self.vault_root,
             "--state", state, "--json"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ledger = os.path.join(os.path.dirname(state), "context", "recall-ledger.jsonl")
        with open(ledger, "r", encoding="utf-8") as fh:
            row = json.loads(fh.readline())
        self.assertEqual(row["keywords"], ["原子替换"])
        self.assertEqual(row["status"], "ok")
        self.assertIn("hits", row)

    def make_state(self) -> str:
        path = os.path.join(self.base, "task", "state.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        state = {
            "task_id": "task-test",
            "goal": "验证上下文引擎",
            "workspace": self.ws,
            "contract": {"boundaries": "只读", "criteria": []},
            "evidence": [],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        return path


class PackTests(ContextEngineBase):
    def setUp(self):
        super().setUp()
        self.ws = self.make_workspace("demo")
        git(self.ws, "commit", "-q", "--allow-empty", "-m", "second")
        self.head = git(self.ws, "rev-parse", "HEAD")
        self.make_memory(self.ws)

    def pack(self, hits: list[dict] | None = None, *, target: str = "subagent",
             status: dict | None = None, requirement: str = "",
             handoff: str = "") -> tuple[str, dict]:
        project = self.resolve(self.ws)
        return CE.build_pack(project=project, target=target,
                             snapshot={"task_id": "task-test", "goal": "目标",
                                       "boundaries": "不改生产", "criteria": [
                                           {"id": "C1", "description": "实现",
                                            "status": "met", "evidence": ["E1"]},
                                           {"id": "C2", "description": "验证",
                                            "status": "pending", "evidence": []}],
                                       "evidence": [{"id": "E1"}], "phase": "RUNNING",
                                       "updated": ""},
                             hits=hits, recall_status=status, terms=["部署"],
                             requirement=requirement, handoff_text=handoff)

    def test_subagent_pack_within_limit_and_has_identity(self):
        text, summary = self.pack()
        self.assertEqual(summary["schema"], "copilot-context-pack/v1")
        self.assertLessEqual(len(text), CE.PACK_LIMITS["subagent"])
        self.assertEqual(summary["chars"], len(text))
        self.assertIn("项目身份", text)
        self.assertIn("完成条件", text)
        self.assertIn("未闭合 C2", text)
        self.assertIn(self.head[:12], text)

    def test_pack_hits_carry_registry(self):
        hits = [{"source": "vault", "path": "/x/10-Products/a.md", "rel": "10-Products/a.md",
                 "line": 3, "score": 2, "snippet": "L3: 部署走原子替换",
                 "meta": {"last_verified": "2026-11-01", "source_commit": self.head,
                          "status": "active"}}]
        text, _ = self.pack(hits)
        self.assertIn("核验 2026-11-01", text)
        self.assertIn(self.head[:12], text)
        self.assertNotIn("基线漂移", text)

    def test_unverified_and_drifted_cards_are_flagged(self):
        hits = [
            {"source": "vault", "path": "/x/a.md", "rel": "10-Products/a.md", "line": 1,
             "score": 1, "snippet": "L1: 部署旧结论", "meta": {}},
            {"source": "vault", "path": "/x/b.md", "rel": "15-Engineering-Memory/b.md",
             "line": 2, "score": 1, "snippet": "L2: 部署旧基线",
             "meta": {"last_verified": "2026-01-01", "source_commit": "b" * 40}},
        ]
        text, _ = self.pack(hits)
        self.assertIn("可信度与冲突提示", text)
        self.assertIn("未核实", text)
        self.assertIn("基线漂移", text)
        self.assertIn("以现场为准", text)

    def test_zero_hit_pack_says_search_incomplete_or_empty(self):
        text, _ = self.pack(status={"status": "zero_hit", "notes": []})
        self.assertIn("零命中不等于不存在", text)

    def test_handoff_and_requirement_are_included(self):
        text, _ = self.pack(requirement="子 Agent 只做只读调查",
                            handoff="### 知识交接\n\n- 结论 A")
        self.assertIn("验收要求", text)
        self.assertIn("子 Agent 只做只读调查", text)
        self.assertIn("结论 A", text)

    def test_pack_trimming_keeps_within_limit(self):
        hits = [{"source": "vault", "path": f"/x/{i}.md", "rel": f"10-Products/{i}.md",
                 "line": 1, "score": 1, "snippet": "L1: " + "很长的片段" * 40,
                 "meta": {"last_verified": "2026-11-01"}} for i in range(20)]
        text, summary = self.pack(hits, target="subagent",
                                  requirement="要求" * 200)
        self.assertLessEqual(len(text), CE.PACK_LIMITS["subagent"])
        self.assertEqual(summary["hits"], 20)
        self.assertTrue(summary["detail_level"] > 0 or text.count("10-Products/") < 20)


class CaptureTests(ContextEngineBase):
    def setUp(self):
        super().setUp()
        self.ws = self.make_workspace("demo")
        self.state = os.path.join(self.base, "task", "state.json")
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        evidence = {"id": "E1", "kind": "text", "text": "pytest 输出",
                    "sha256": "c" * 64}
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump({
                "task_id": "task-test",
                "goal": "沉淀测试",
                "workspace": self.ws,
                "contract": {
                    "boundaries": "只读",
                    "criteria": [{"criterion_id": "C1", "description": "实现",
                                  "status": "met", "evidence_refs": ["E1"]},
                                 {"criterion_id": "C2", "description": "验证",
                                  "status": "pending", "evidence_refs": []}],
                },
                "evidence": [evidence],
            }, fh)

    def build(self, *, dedupe: dict | None = None) -> tuple[str, dict]:
        project = self.resolve(self.ws)
        snapshot = CE._state_snapshot(self.state)
        return CE.build_capture(project=project, snapshot=snapshot, state_path=self.state,
                                terms=["部署"], title="部署基线", dedupe=dedupe)

    def test_draft_has_task_and_evidence_provenance(self):
        text, summary = self.build()
        self.assertEqual(summary["schema"], "copilot-context-capture/v1")
        self.assertIn("task-test", text)
        self.assertIn("E1", text)
        self.assertIn("source_commit: null", text)
        self.assertIn("未补全不得提交", text)
        self.assertNotIn("unknown", text)

    def test_draft_file_name_matches_candidate_contract(self):
        _text, summary = self.build()
        self.assertRegex(summary["file_name"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-[a-z0-9-]+-candidate\.md$")

    def test_draft_passes_candidate_write_validation(self):
        scripts = os.path.expanduser(
            "~/.zcode/skills/knowledge-manager/scripts")
        if not os.path.isdir(scripts):
            self.skipTest("knowledge-manager 未安装")
        sys.path.insert(0, scripts)
        try:
            import candidate_write as cw
        except ImportError:  # pragma: no cover - 环境差异
            self.skipTest("candidate_write 不可导入")
        finally:
            sys.path.remove(scripts)
        text, summary = self.build()
        cw.validate_content(text, summary["file_name"])

    def test_dedupe_hits_are_recorded_before_action(self):
        self.make_card("15-Engineering-Memory/demo/deploy.md",
                       CARD.format(commit="d" * 40))
        project = self.resolve(self.ws)
        dedupe = CE.recall(project, ["原子替换"], scope=["vault"])
        text, summary = self.build(dedupe=dedupe)
        self.assertEqual(summary["dedupe_hits"], len(dedupe["hits"]))
        self.assertIn("去重前置", text)
        self.assertLess(text.index("去重前置"), text.index("## 建议动作"))

    def test_no_met_criteria_blocks_capture(self):
        with open(self.state, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        state["contract"]["criteria"] = [
            {"criterion_id": "C1", "description": "实现", "status": "pending",
             "evidence_refs": []}]
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        text, summary = self.build()
        self.assertEqual(summary["met_criteria"], [])
        self.assertIn("无已闭合完成条件", text)


class WiringTests(ContextEngineBase):
    def setUp(self):
        super().setUp()
        self.ws = self.make_workspace("demo")
        self.make_memory(self.ws)
        self.state_path = os.path.join(self.base, "task", "state.json")
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump({"task_id": "task-test", "workspace": self.ws,
                       "goal": "g", "contract": {"criteria": []}}, fh)

    def test_session_start_text_has_project_context(self):
        row = {"task_id": "task-test", "phase": "RUNNING", "reason": "继续",
               "unmet_criteria": ["C1"], "next_command": "",
               "state_path": self.state_path,
               "task_dir": os.path.dirname(self.state_path)}
        text = GUARD._session_start_text([row], None, root=self.ws)
        self.assertIn("项目现场", text)
        self.assertIn("原生 Memory", text)
        self.assertIn("context_engine.py", text)
        self.assertLessEqual(len(text), GUARD.SESSION_START_LIMIT)
        self.assertTrue(text.startswith(GUARD.SESSION_START_OPEN))

    def test_session_start_degrades_without_workspace(self):
        row = {"task_id": "task-test", "phase": "RUNNING", "reason": "继续",
               "unmet_criteria": [], "next_command": "",
               "state_path": self.state_path,
               "task_dir": os.path.dirname(self.state_path)}
        text = GUARD._session_start_text([row], None,
                                         root=os.path.join(self.base, "gone"))
        self.assertIn("task-test", text)
        self.assertNotIn("项目现场", text)

    def test_status_hint_is_non_blocking_and_optional(self):
        hint = DISPATCH._context_hint(self.state_path, {"workspace": self.ws})
        self.assertIn("原生 Memory", hint)
        self.assertIn("recall --workspace", hint)
        self.assertEqual(DISPATCH._context_hint(self.state_path, {}), "")

    def test_engine_has_no_network_or_model_calls(self):
        with open(os.path.join(SCRIPTS, "context_engine.py"), "r", encoding="utf-8") as fh:
            source = fh.read()
        for forbidden in ("import socket", "urllib.request", "import requests",
                          "http.client", "sol_review", "openai"):
            self.assertNotIn(forbidden, source)


class HardeningTests(ContextEngineBase):
    """独立只读复核确认的缺陷回归：没有对应修复时每条都必须失败。"""

    def setUp(self):
        super().setUp()
        self.ws = self.make_workspace("demo")

    def test_capture_accepts_governance_evidence_refs(self):
        state = os.path.join(self.base, "task", "state.json")
        os.makedirs(os.path.dirname(state), exist_ok=True)
        with open(state, "w", encoding="utf-8") as fh:
            json.dump({
                "task_id": "task-test", "goal": "沉淀", "workspace": self.ws,
                "contract": {"criteria": [
                    {"criterion_id": "C1", "description": "实现", "status": "met",
                     "evidence_refs": [{"id": "E1", "sha256": "c" * 64}]},
                    {"criterion_id": "C2", "description": "验证", "status": "met",
                     "evidence_refs": ["E9"]}]},
                "evidence": [
                    {"id": "E1", "kind": "file", "path_hint": "/tmp/report.txt",
                     "sha256": "c" * 64},
                    {"id": "E9", "kind": "text", "sha256": "d" * 64}]}, fh)
        project = self.resolve(self.ws)
        snapshot = CE._state_snapshot(state)
        text, summary = CE.build_capture(project=project, snapshot=snapshot,
                                         state_path=state, terms=[], title="x",
                                         dedupe=None)
        self.assertEqual(summary["met_criteria"], ["C1", "C2"])
        self.assertIn("E1", text)
        self.assertIn("E9", text)
        self.assertIn("report.txt", text)

    def test_partial_source_failure_is_not_ok(self):
        self.make_card("15-Engineering-Memory/demo/deploy.md",
                       CARD.format(commit="a" * 40))
        broken = os.path.join(self.base, "memfile")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("x")
        project = CE.resolve(self.ws, memory_root=broken, vault_root=self.vault_root)
        result = CE.recall(project, ["原子替换"], scope=["vault", "memory"])
        self.assertTrue(result["hits"])
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any("不可读" in note or "来源失败" in note
                            for note in result["notes"]))

    def test_truncated_without_hits_is_not_zero_hit(self):
        self.make_card("10-Products/a.md", "# 产品\n\n无关内容\n")
        self.make_card("15-Engineering-Memory/b.md", "# 记忆\n\n原子替换 结论\n")
        result = self.recall(self.ws, "原子替换", scope=["vault"], max_files=1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["status"], "budget_exhausted")

    def test_cache_key_includes_scan_budget(self):
        self.make_card("10-Products/a.md", "# 产品\n\n无关内容\n")
        self.make_card("15-Engineering-Memory/b.md", "# 记忆\n\n原子替换 结论\n")
        cache = os.path.join(self.base, "cache")
        tight = self.recall(self.ws, "原子替换", scope=["vault"], max_files=1,
                            cache_dir=cache)
        self.assertEqual(tight["status"], "budget_exhausted")
        full = self.recall(self.ws, "原子替换", scope=["vault"], cache_dir=cache)
        self.assertEqual(full["cache"], "miss")
        self.assertFalse(full["truncated"])
        self.assertEqual(full["status"], "ok")

    def test_horizontal_rule_does_not_hide_body(self):
        self.make_card("10-Products/rule.md", "# 标题\n\n---\n\n原子替换 结论\n")
        result = self.recall(self.ws, "原子替换", scope=["vault"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["hits"][0]["line"], 5)

    def test_memory_root_unreadable_is_not_absent_in_pack(self):
        broken = os.path.join(self.base, "memfile")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("x")
        project = CE.resolve(self.ws, memory_root=broken, vault_root=self.vault_root)
        text, _summary = CE.build_pack(project=project, target="subagent", snapshot=None,
                                       hits=None, recall_status=None, terms=[])
        self.assertIn("不可读", text)
        self.assertNotIn("未找到稳定项目记忆目录", text)
        self.assertIn("不可读", CE._human_resolve(project))

    def test_cwd_subdirectory_memory_is_not_merged(self):
        sub = os.path.join(self.ws, "sub")
        os.makedirs(sub)
        root_memory = self.make_memory(self.ws)
        sub_memory = self.make_memory(
            sub, index="# 子项目\n\n- [部署约定](deploy.md) — 子项目结论\n")
        project = CE.resolve(self.ws, cwd=sub, memory_root=self.memory_root,
                             vault_root=self.vault_root)
        self.assertEqual({m["role"] for m in project["memory"]}, {"root", "cwd"})
        result = CE.recall(project, ["部署约定"], scope=["memory"])
        self.assertEqual(result["status"], "ok")
        paths = {hit["path"] for hit in result["hits"]}
        self.assertTrue(paths)
        self.assertTrue(all(p.startswith(root_memory) for p in paths))
        self.assertFalse(any(p.startswith(sub_memory) for p in paths))

    def test_pack_reports_hits_omitted_by_length(self):
        hits = [{"source": "vault", "path": f"/x/{i}.md", "rel": f"10-Products/{i}.md",
                 "line": 1, "score": 1, "snippet": "L1: " + "很长的片段" * 40,
                 "meta": {"last_verified": "2026-11-01"}} for i in range(20)]
        project = self.resolve(self.ws)
        text, summary = CE.build_pack(project=project, target="subagent", snapshot=None,
                                      hits=hits, recall_status={"status": "ok", "notes": []},
                                      terms=["部署"], requirement="要求" * 3000)
        self.assertLessEqual(len(text), CE.PACK_LIMITS["subagent"])
        self.assertEqual(summary["detail_level"], 3)
        self.assertIn("全部省略", text)

    def test_recall_snippet_is_redacted(self):
        self.make_card("10-Products/config-notes.md",
                       "# 配置\n\n数据库 password=supersecret123 配置\n")
        result = self.recall(self.ws, "数据库", scope=["vault"])
        self.assertTrue(result["hits"])
        self.assertNotIn("supersecret123", result["hits"][0]["snippet"])
        self.assertIn("<redacted:", result["hits"][0]["snippet"])

    def test_pack_without_target_arguments_exits_two(self):
        proc = subprocess.run(
            [sys.executable, "-B", os.path.join(SCRIPTS, "context_engine.py"), "pack"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["code"], "WORKSPACE_REQUIRED")

    def test_write_failure_exits_two(self):
        blocker = os.path.join(self.base, "afile")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        out = os.path.join(blocker, "pack.md")
        proc = subprocess.run(
            [sys.executable, "-B", os.path.join(SCRIPTS, "context_engine.py"), "pack",
             "--workspace", self.ws, "--memory-root", self.memory_root,
             "--vault-root", self.vault_root, "--out", out],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["code"], "WRITE_FAILED")

    def test_symlink_outside_vault_is_skipped(self):
        outside = os.path.join(self.base, "outside.md")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("# 外部\n\n原子替换 外部结论\n")
        self.make_card("10-Products/anchor.md", "# 锚点\n\n无关键词\n")
        os.symlink(outside, os.path.join(self.vault_root, "10-Products", "linked.md"))
        result = self.recall(self.ws, "原子替换", scope=["vault"])
        self.assertFalse(any(h["path"].startswith(outside) for h in result["hits"]))
        self.assertTrue(any("符号链接" in note for note in result["notes"]))

    def test_session_start_git_unknown_is_not_clean(self):
        fake = {"project": {"root": self.ws, "basename": "demo", "slug": "demo",
                            "hash16": "0" * 16},
                "git": {"available": True, "root": self.ws, "branch": "main",
                        "head": "a" * 40, "dirty": None, "untracked": None},
                "memory": [], "memory_root": self.memory_root,
                "vault": {"root": self.vault_root, "status": "available"}, "rules": []}
        state_path = os.path.join(self.base, "task", "state.json")
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump({"task_id": "task-test", "workspace": self.ws}, fh)
        row = {"task_id": "task-test", "phase": "RUNNING", "reason": "",
               "unmet_criteria": [], "next_command": "",
               "state_path": state_path, "task_dir": os.path.dirname(state_path)}
        with mock.patch.object(CE, "resolve", return_value=fake):
            text = GUARD._session_start_text([row], None, root=self.ws)
        self.assertIn("工作区 未知", text)
        self.assertNotIn("工作区 clean", text)

    def test_status_hint_quotes_workspace_path(self):
        spaced = os.path.join(self.base, "ws with space")
        os.makedirs(spaced)
        state_path = os.path.join(self.base, "task", "state.json")
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump({"task_id": "task-test", "workspace": spaced}, fh)
        hint = DISPATCH._context_hint(state_path, {"workspace": spaced})
        self.assertIn("recall --workspace", hint)
        self.assertIn(f"'{spaced}'", hint)


if __name__ == "__main__":
    unittest.main()

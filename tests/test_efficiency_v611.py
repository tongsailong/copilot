#!/usr/bin/env python3
"""v6.11.0 效率专项：单次命令内的快照/路径复用与同步预编译。

这些测试固定的是“同一次命令内只读结果可以复用、跨命令/库调用不得复用”的边界：
缓存默认关闭，漂移检测与篡改检测仍逐次重算。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import code_scope as CS  # noqa: E402
import governance as G  # noqa: E402
import v6_support as F  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts-sync"))
import sync_runtime as S  # noqa: E402


class SnapshotCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = F.init_git_workspace(os.path.join(self.tmp.name, "ws"))
        self.state_path = os.path.join(self.tmp.name, "state.json")
        G.start_state(self.state_path, goal="效率", contract="## 完成条件\n- 产出\n",
                      workspace=self.ws)
        self.state = G.load_state(self.state_path)

    def test_cache_reuses_snapshot_without_touching_git(self):
        cache: dict = {}
        first = CS.capture_code_snapshot(self.ws, cache=cache)
        with mock.patch.object(CS, "_git", side_effect=AssertionError(
                "缓存命中时不得再调用 git")):
            second = CS.capture_code_snapshot(self.ws, cache=cache)
        self.assertEqual(first["digest"], second["digest"])
        self.assertIsNot(first, second, "命中缓存必须返回深拷贝")
        second["files"].append({"path": "injected"})
        third = CS.capture_code_snapshot(self.ws, cache=cache)
        self.assertEqual(first["digest"], third["digest"])

    def test_no_cache_recomputes_each_call(self):
        first = CS.capture_code_snapshot(self.ws)
        with open(os.path.join(self.ws, "README.md"), "a", encoding="utf-8") as fh:
            fh.write("drift\n")
        second = CS.capture_code_snapshot(self.ws)
        self.assertNotEqual(first["digest"], second["digest"],
                            "默认无缓存：必须看到调用时刻的真实工作区")

    def test_cache_key_includes_exclusions(self):
        cache: dict = {}
        with open(os.path.join(self.ws, "output.txt"), "w", encoding="utf-8") as fh:
            fh.write("artifact\n")
        plain = CS.capture_code_snapshot(self.ws, cache=cache)
        with_exclude = CS.capture_code_snapshot(
            self.ws, exclude_paths=[os.path.join(self.ws, "output.txt")],
            cache=cache)
        self.assertNotEqual(plain["digest"], with_exclude["digest"],
                            "不同排除集必须各自计算，不能共用命中")


class WorkspaceInfoCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = F.init_git_workspace(os.path.join(self.tmp.name, "ws"))
        self.plain = os.path.join(self.tmp.name, "plain")
        os.makedirs(self.plain)

    def test_git_root_cached_per_directory(self):
        calls: list[tuple] = []
        real = CS._git

        def counting(root, *args):
            calls.append(args)
            return real(root, *args)

        with mock.patch.object(CS, "_git", side_effect=counting):
            first = CS.git_root(os.path.join(self.ws, "README.md"))
            second = CS.git_root(self.ws)
        self.assertEqual(first, second)
        self.assertEqual(1, len(calls), "同一目录只解析一次 git 根")

    def test_git_root_does_not_cache_non_repository(self):
        calls: list[tuple] = []
        real = CS._git

        def counting(root, *args):
            calls.append(args)
            return real(root, *args)

        with mock.patch.object(CS, "_git", side_effect=counting):
            self.assertEqual("", CS.git_root(self.plain))
            self.assertEqual("", CS.git_root(self.plain))
        self.assertEqual(2, len(calls), "非仓库结果不缓存，避免新建仓库场景读到旧判断")


class GitCallReductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = F.init_git_workspace(os.path.join(self.tmp.name, "ws"))

    def _calls(self, ws: str) -> list[str]:
        calls: list[str] = []
        real = CS._git

        def counting(root, *args):
            calls.append(" ".join(args))
            return real(root, *args)

        with mock.patch.object(CS, "_git", side_effect=counting):
            CS.capture_code_snapshot(ws)
        return calls

    def test_workspace_root_and_head_share_one_call(self):
        calls = self._calls(self.ws)
        self.assertIn("rev-parse --show-toplevel HEAD", calls)
        self.assertFalse([c for c in calls if c == "rev-parse HEAD"],
                         "HEAD 不再单独调用一次 rev-parse")

    def test_gitlink_without_gitmodules_stays_fail_closed(self):
        """index 里有 gitlink 但缺 .gitmodules 时，快照必须保持不可用。"""
        sub = os.path.join(self.ws, "sub")
        F.init_git_workspace(sub)
        sha = subprocess.run(["git", "-C", sub, "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(["git", "-C", self.ws, "update-index", "--add",
                        "--cacheinfo", f"160000,{sha},sub"], check=True,
                       capture_output=True, text=True)
        snapshot = CS.capture_code_snapshot(self.ws)
        self.assertFalse(snapshot.get("available"))
        self.assertEqual("submodule_status_failed", snapshot.get("reason"))


class GateReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = F.init_git_workspace(os.path.join(self.tmp.name, "ws"))
        self.state_path = os.path.join(self.tmp.name, "state.json")
        G.start_state(self.state_path, goal="效率", contract="## 完成条件\n- 产出\n",
                      workspace=self.ws)
        self.evidence = os.path.join(self.ws, "result.txt")
        with open(self.evidence, "w", encoding="utf-8") as fh:
            fh.write("result\n")
        state = G.load_state(self.state_path)
        G.register_evidence(state, "E1", file_path=self.evidence)
        G.save_state(self.state_path, state)
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=["E1"])
        self.state = G.load_state(self.state_path)

    def test_criteria_evidence_hashed_once_with_cache(self):
        calls: list[str] = []
        real = G._sha256_file

        def counting(path):
            calls.append(path)
            return real(path)

        with mock.patch.object(G, "_sha256_file", side_effect=counting):
            cache: dict = {}
            G.closure_check(self.state, cache=cache)
        self.assertEqual(1, len([c for c in calls if c == self.evidence]),
                         "同一证据文件在一条命令内只重读一次")

    def test_tamper_detected_across_closure_checks(self):
        """缓存只作用于单次 closure_check：下一次调用必须重读文件并发现篡改。"""
        first = G.closure_check(self.state, cache={})
        self.assertTrue(first["passed"], first["reasons"])
        with open(self.evidence, "a", encoding="utf-8") as fh:
            fh.write("tampered\n")
        cache: dict = {}
        second = G.closure_check(self.state, cache=cache)
        codes = {str(reason.get("code")) for reason in second["reasons"]}
        self.assertIn("evidence_tampered", codes,
                      "跨命令不得复用旧摘要，否则篡改检测失效")

    def test_closure_check_result_unchanged_by_cache(self):
        cache: dict = {}
        first = G.closure_check(self.state, cache=cache)
        second = G.closure_check(self.state, cache={})
        self.assertEqual(first["reasons"], second["reasons"])
        self.assertEqual(first["passed"], second["passed"])


class ClosureCheckSnapshotCountTests(unittest.TestCase):
    """接入代码类质量报告后，一次 closure-check 只扫描工作区一次。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = F.init_git_workspace(os.path.join(self.tmp.name, "ws"))
        with open(os.path.join(self.ws, "README.md"), "a", encoding="utf-8") as fh:
            fh.write("change\n")
        self.task_dir = os.path.join(self.tmp.name, "task")
        os.makedirs(self.task_dir)
        self.state_path = os.path.join(self.task_dir, "state.json")
        G.start_state(self.state_path, goal="效率", contract="## 完成条件\n- 产出\n",
                      workspace=self.ws)
        G.plan_state(self.state_path, {"summary": "p", "actions": [
            {"id": "A1", "title": "t", "goal": "g", "expected_evidence": "e"}]})
        self.evidence = os.path.join(self.task_dir, "out.txt")
        with open(self.evidence, "w", encoding="utf-8") as fh:
            fh.write("done\n")
        state = G.load_state(self.state_path)
        G.register_evidence(state, "E1", file_path=self.evidence)
        G.save_state(self.state_path, state)
        G.record_action(self.state_path, "A1", "done", result="完成",
                        evidence_ids=["E1"])
        G.record_criterion(self.state_path, "C1", "met", evidence_ids=["E1"])

    def _ingest_pass_report(self) -> None:
        report = os.path.join(self.task_dir, "report.md")
        excludes = CS.governance_paths(self.state_path) + [os.path.realpath(report)]
        snapshot = CS.capture_code_snapshot(self.ws,
                                           exclude_paths=sorted(set(excludes)))
        self.assertTrue(snapshot.get("available"), snapshot.get("reason"))
        payload = {
            "schema": "copilot-verify-result/v1",
            "report_id": "VR-1",
            "generated_at": "2026-11-16T10:00:00+08:00",
            "verdict": "PASS",
            "warnings": [], "residual_risks": [], "unverified": [],
            "acceptance_required": False,
            "acceptance_status": "not_required",
            "checks": {"required_tests_executed": True,
                       "required_evidence_present": True,
                       "completion_criteria_met": True,
                       "delivery_verifiable": True},
            "subject": {
                "scope": "efficiency",
                "summary": "fixture",
                "commit": snapshot["git_head"],
                "code_scope_sha256": snapshot["digest"],
                "files": [item["path"] for item in snapshot.get("files") or []],
            },
        }
        body = __import__("json").dumps(payload, ensure_ascii=False, indent=2)
        with open(report, "w", encoding="utf-8") as fh:
            fh.write("## 报告\n<!-- copilot-verify-result/v1 -->\n```json\n"
                     + body + "\n```\n")
        G.ingest_quality_report(self.state_path, report, workspace=self.ws)

    def _snapshot_scans(self, *, cache) -> int:
        """统计真正扫描工作区的次数（git 工作区根查询）。"""
        calls: list[str] = []
        real = CS._git

        def counting(root, *args):
            if args[:2] == ("rev-parse", "--show-toplevel"):
                calls.append(" ".join(args))
            return real(root, *args)

        state = G.load_state(self.state_path)
        with mock.patch.object(CS, "_git", side_effect=counting):
            G.closure_check(state, task_dir=self.task_dir, cache=cache)
        return len(calls)

    def test_single_workspace_scan_per_closure_check(self):
        self._ingest_pass_report()
        self.assertEqual(1, self._snapshot_scans(cache={}),
                         "G2 质量门禁与 quality_summary 必须共用同一份快照")
        self.assertEqual(1, self._snapshot_scans(cache=None),
                         "closure_check 内部也只为一次求值扫描一次")


class PrecompileTests(unittest.TestCase):
    def test_precompile_writes_bytecode_for_scripts(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        skill = tmp.name
        scripts = os.path.join(skill, "scripts")
        shutil.copytree(os.path.join(os.path.dirname(HERE), "scripts"), scripts,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.rmtree(os.path.join(scripts, "__pycache__"), ignore_errors=True)
        result = S.precompile_bytecode(skill)
        py_files = [n for n in os.listdir(scripts) if n.endswith(".py")]
        self.assertEqual(len(py_files), result["compiled"])
        self.assertEqual(0, result["failed"])
        self.assertTrue(os.path.isdir(os.path.join(scripts, "__pycache__")))
        self.assertTrue([n for n in os.listdir(os.path.join(scripts, "__pycache__"))
                         if n.endswith(".pyc")])

    def test_precompile_missing_scripts_dir_is_not_an_error(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertEqual({"compiled": 0, "failed": 0}, S.precompile_bytecode(tmp.name))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""sync_runtime.py 回归：分发范围、漂移检测、原子写入、prune、安全边界。

测试只操作临时目录；不读取也不写入真实的 ~/.zcode/skills/copilot。阶段九起旧
Reviewer 全局 Profile 已退役，不再参与分发。CLI 场景通过 --skill-dest 指向临时目标。
"""
from __future__ import annotations

import importlib.util
import os
import stat
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SYNC_PATH = os.path.join(ROOT, "scripts-sync", "sync_runtime.py")


def load_sync():
    spec = importlib.util.spec_from_file_location("copilot_sync_runtime", SYNC_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


S = load_sync()


def write(path: str, text: str, mode: int = 0o664) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, mode)
    return path


def make_repo(root: str) -> None:
    write(os.path.join(root, "SKILL.md"),
          "---\nname: copilot\n---\n\n# test skill\n")
    write(os.path.join(root, "scripts", "a.py"), "print('a')\n", 0o755)
    write(os.path.join(root, "scripts", "b.py"), "print('b')\n")
    write(os.path.join(root, "references", "ref.md"), "# ref\n")
    # 以下都不参与分发
    write(os.path.join(root, "README.md"), "# readme\n")
    write(os.path.join(root, "docs", "X.md"), "# doc\n")
    write(os.path.join(root, "tests", "test_x.py"), "# test\n")
    write(os.path.join(root, "scripts-sync", "sync_runtime.py"), "# sync\n")
    write(os.path.join(root, "manifests", "source-baseline.sha256"), "")
    write(os.path.join(root, "scripts", "__pycache__", "a.pyc"), "pyc")
    write(os.path.join(root, "scripts", ".env"), "TOKEN=secret\n")
    write(os.path.join(root, "scripts", "id_rsa.key"), "KEY\n")


class ManifestScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        make_repo(self.root)

    def test_source_manifest_distributes_only_skill_scripts_references(self):
        manifest = S.source_manifest(self.root)
        self.assertEqual(sorted(manifest), [
            "SKILL.md", "references/ref.md", "scripts/a.py", "scripts/b.py",
        ])
        self.assertNotIn(".env", " ".join(manifest))
        self.assertFalse(any(k.endswith(".key") for k in manifest))

    def test_target_manifest_skips_audit_cache_and_temp_state(self):
        target = os.path.join(self.root, "runtime")
        write(os.path.join(target, "SKILL.md"), "x")
        write(os.path.join(target, "scripts", "a.py"), "x")
        write(os.path.join(target, ".audit", "copilot.jsonl"), "log")
        write(os.path.join(target, "scripts", "__pycache__", "a.pyc"), "pyc")
        write(os.path.join(target, "state.json"), "{}")
        manifest = S.target_manifest(target)
        self.assertEqual(sorted(manifest), ["SKILL.md", "scripts/a.py"])

    def test_compare_reports_missing_different_extra(self):
        target = os.path.join(self.root, "runtime")
        write(os.path.join(target, "SKILL.md"),
              "---\nname: copilot\n---\n\n# test skill\n")
        write(os.path.join(target, "scripts", "b.py"), "changed\n")
        write(os.path.join(target, "junk.txt"), "junk\n")
        src = S.source_manifest(self.root)
        dst = S.target_manifest(target)
        missing, different, extra = S.compare_maps(src, dst)
        self.assertEqual(missing, ["references/ref.md", "scripts/a.py"])
        self.assertEqual(different, ["scripts/b.py"])
        self.assertEqual(extra, ["junk.txt"])


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "repo")
        make_repo(self.root)
        self.target = os.path.join(self.tmp.name, "runtime")

    def test_apply_writes_and_preserves_extra(self):
        write(os.path.join(self.target, "SKILL.md"), "old\n")
        write(os.path.join(self.target, "junk.txt"), "keep\n")
        result = S.apply_tree(self.root, self.target)
        self.assertEqual(result["written"], 4)
        self.assertEqual(result["pruned"], 0)
        self.assertTrue(os.path.exists(os.path.join(self.target, "junk.txt")))
        src = S.source_manifest(self.root)
        dst = S.target_manifest(self.target)
        for rel, digest in src.items():
            self.assertEqual(dst[rel], digest)
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.join(self.target, "scripts", "a.py")).st_mode),
            0o755)
        self.assertFalse(os.path.exists(os.path.join(self.target, "scripts", ".env")))
        self.assertFalse(os.path.exists(os.path.join(self.target, "README.md")))

    def test_apply_creates_missing_destination_tree(self):
        nested = os.path.join(self.target, "deep", "skill")
        result = S.apply_tree(self.root, nested)
        self.assertGreater(result["written"], 0)
        self.assertTrue(os.path.isfile(os.path.join(nested, "SKILL.md")))

    def test_prune_removes_extra_but_never_audit_or_secret_files(self):
        write(os.path.join(self.target, "SKILL.md"), "old\n")
        write(os.path.join(self.target, "junk.txt"), "remove\n")
        write(os.path.join(self.target, ".audit", "copilot.jsonl"), "audit\n")
        write(os.path.join(self.target, "state.json"), "{}\n")
        write(os.path.join(self.target, "scripts", ".env"), "TOKEN=secret\n")
        result = S.apply_tree(self.root, self.target, prune=True)
        self.assertEqual(result["pruned"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.target, "junk.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.target, ".audit", "copilot.jsonl")))
        self.assertTrue(os.path.exists(os.path.join(self.target, "state.json")))
        self.assertTrue(os.path.exists(os.path.join(self.target, "scripts", ".env")))

    def test_apply_rejects_same_or_nested_destination(self):
        with self.assertRaises(S.SyncError):
            S.apply_tree(self.root, self.root)
        with self.assertRaises(S.SyncError):
            S.apply_tree(self.root, os.path.join(self.root, "scripts", "nested"))
        with self.assertRaises(S.SyncError):
            S.apply_tree(self.root, self.tmp.name)


class SymlinkAndPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "repo")
        make_repo(self.root)

    def test_source_symlink_file_is_rejected(self):
        link = os.path.join(self.root, "scripts", "link.py")
        os.symlink(os.path.join(self.root, "scripts", "a.py"), link)
        with self.assertRaises(S.SyncError):
            S.source_manifest(self.root)

    def test_target_symlink_file_is_rejected(self):
        target = os.path.join(self.tmp.name, "runtime")
        write(os.path.join(target, "SKILL.md"), "x")
        os.symlink(os.path.join(self.root, "SKILL.md"),
                   os.path.join(target, "linked.md"))
        with self.assertRaises(S.SyncError):
            S.target_manifest(target)

    def test_symlinked_target_root_is_rejected(self):
        real = os.path.join(self.tmp.name, "real")
        write(os.path.join(real, "SKILL.md"), "x")
        link = os.path.join(self.tmp.name, "runtime-link")
        os.symlink(real, link)
        with self.assertRaises(S.SyncError):
            S.target_manifest(link)

    def test_destination_escape_is_rejected(self):
        target = os.path.join(self.tmp.name, "runtime")
        os.makedirs(target)
        with self.assertRaises(S.SyncError):
            S._safe_dest(target, "../outside.txt")
        with self.assertRaises(S.SyncError):
            S._safe_dest(target, "/etc/passwd")

    def test_symlinked_parent_escaping_root_is_rejected(self):
        target = os.path.join(self.tmp.name, "runtime")
        outside = os.path.join(self.tmp.name, "outside")
        os.makedirs(target)
        os.makedirs(outside)
        os.symlink(outside, os.path.join(target, "scripts"))
        with self.assertRaises(S.SyncError):
            S._safe_dest(target, "scripts/a.py")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_root = self.tmp.name
        self.skill_dest = os.path.join(self.tmp_root, "skill")
        # 用真实源码构造一份完整运行时副本，验证 CLI 的 check/apply 行为。
        for rel in S.source_manifest(ROOT):
            S.copy_atomic(os.path.join(ROOT, rel), os.path.join(self.skill_dest, rel))

    def run_cli(self, *args):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = S.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_check_reports_clean_and_drift(self):
        code, out, _ = self.run_cli("--check", "--skill-dest", self.skill_dest)
        self.assertEqual(code, 0, out)
        write(os.path.join(self.skill_dest, "SKILL.md"), "drifted\n")
        code, out, _ = self.run_cli("--check", "--skill-dest", self.skill_dest)
        self.assertEqual(code, 1, out)
        self.assertIn("SKILL.md", out)

    def test_apply_syncs_target_and_reruns_clean(self):
        write(os.path.join(self.skill_dest, "SKILL.md"), "drifted\n")
        code, out, _ = self.run_cli("--apply", "--skill-dest", self.skill_dest,
                                    "--force")
        self.assertEqual(code, 0, out)
        code, out, _ = self.run_cli("--check", "--skill-dest", self.skill_dest)
        self.assertEqual(code, 0, out)
        self.assertIn("缺失 0，不同 0", out)

    def test_prune_requires_apply(self):
        code, _, err = self.run_cli("--prune", "--skill-dest", self.skill_dest)
        self.assertEqual(code, 2)
        self.assertIn("--prune", err)

    def test_apply_refuses_unknown_target_without_force(self):
        other = os.path.join(self.tmp_root, "other")
        os.makedirs(other)
        code, _, err = self.run_cli("--apply", "--skill-dest", other)
        self.assertEqual(code, 2)
        self.assertIn("不像 Copilot skill", err)


class RealDistributionTests(unittest.TestCase):
    """真实仓库的分发范围：v6 模块进入运行时，历史/测试目录不进入。"""

    def test_v6_modules_are_distributed_and_legacy_is_not(self):
        manifest = S.source_manifest(ROOT)
        for rel in ("SKILL.md", "scripts/governance.py",
                    "scripts/copilot_dispatch.py", "scripts/copilot_auto.py",
                    "scripts/native_bridge.py", "scripts/native_agent.py"):
            self.assertIn(rel, manifest)
        self.assertFalse([k for k in manifest if k.startswith("legacy/")])
        self.assertFalse([k for k in manifest if k.startswith("tests/")])
        self.assertNotIn("agents/reviewer.md", manifest)
        # 工作区只读 Profile 由项目工作区加载，不分发。
        self.assertNotIn("agents/independent-readonly.md", manifest)
        self.assertFalse([k for k in manifest if k.startswith(".zcode/")])
        for retired in ("scripts/call_gpt.py", "scripts/review_ledger.py",
                        "scripts/reply_protocol.py"):
            self.assertNotIn(retired, manifest)


if __name__ == "__main__":
    unittest.main()

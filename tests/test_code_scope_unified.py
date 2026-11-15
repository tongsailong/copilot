#!/usr/bin/env python3
"""真实 Git 仓库上的代码快照统一性矩阵。"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import code_scope as CS  # noqa: E402
import g4_attest as A  # noqa: E402


def run(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True,
                          text=True)


class CodeScopeUnifiedTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "需要 git")
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        run("git", "init", "-q", cwd=self.dir)
        run("git", "config", "user.email", "t@example.invalid", cwd=self.dir)
        run("git", "config", "user.name", "t", cwd=self.dir)
        self.write("README.md", "base\n")
        run("git", "add", "README.md", cwd=self.dir)
        run("git", "commit", "-qm", "base", cwd=self.dir)
        self.base = run("git", "rev-parse", "HEAD",
                        cwd=self.dir).stdout.strip()

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def snap(self, **kwargs):
        snapshot = CS.capture_code_snapshot(self.dir, **kwargs)
        self.assertTrue(snapshot["available"], snapshot)
        self.assertEqual(snapshot["digest"], CS.snapshot_digest(snapshot))
        self.assertEqual(snapshot["digest"], A._snapshot_digest(snapshot))
        return snapshot

    def paths(self, snapshot):
        return {item["path"]: item for item in snapshot["files"]}

    def test_initial_then_add_modify_delete_rename(self):
        initial = self.snap()
        self.assertEqual(initial["files"], [])
        self.write("src/a.js", "A = 1\n")
        added = self.snap()
        self.assertIn("src/a.js", self.paths(added))
        self.assertNotEqual(added["digest"], initial["digest"])
        self.write("src/a.js", "A = 2\n")
        modified = self.snap()
        self.assertNotEqual(modified["digest"], added["digest"])
        run("git", "add", "src/a.js", cwd=self.dir)
        run("git", "commit", "-qm", "a", cwd=self.dir)
        os.remove(os.path.join(self.dir, "src/a.js"))
        deleted = self.snap()
        self.assertEqual(self.paths(deleted)["src/a.js"]["kind"], "missing")
        self.assertNotEqual(deleted["digest"], modified["digest"])
        self.write("src/old.js", "OLD\n")
        run("git", "add", "src/old.js", cwd=self.dir)
        run("git", "commit", "-qm", "old", cwd=self.dir)
        run("git", "mv", "src/old.js", "src/new.js", cwd=self.dir)
        renamed = self.snap()
        self.assertIn("src/new.js", self.paths(renamed))
        self.assertNotIn("src/old.js", self.paths(renamed))

    def test_base_includes_committed_task_code(self):
        self.write("src/committed.js", "X = 1\n")
        run("git", "add", "src/committed.js", cwd=self.dir)
        run("git", "commit", "-qm", "task", cwd=self.dir)
        self.assertEqual(self.snap()["files"], [])
        with_base = self.snap(base=self.base)
        self.assertIn("src/committed.js", self.paths(with_base))
        self.assertEqual(with_base["digest"], CS.snapshot_digest(with_base))

    def test_symlink_uses_link_text(self):
        os.symlink("README.md", os.path.join(self.dir, "link.md"))
        snapshot = self.snap()
        item = self.paths(snapshot)["link.md"]
        self.assertEqual(item["kind"], "symlink")
        self.assertEqual(item["sha256"],
                         hashlib.sha256(b"README.md").hexdigest())

    def test_governance_and_build_files_are_excluded(self):
        self.write(".copilot-tasks/state.json", "{}\n")
        self.write(".audit/log.jsonl", "{}\n")
        self.write("__pycache__/x.pyc", "x")
        self.write("node_modules/pkg/index.js", "x")
        self.write(".state.json.lock", "x")
        snapshot = self.snap()
        self.assertEqual(snapshot["files"], [])

    @unittest.skipUnless(shutil.which("git"), "需要 git")
    def test_submodule_gitlink_change_changes_digest(self):
        sub = os.path.join(self.tmp.name + "-sub")
        os.makedirs(sub)
        self.addCleanup(shutil.rmtree, sub, True)
        run("git", "init", "-q", cwd=sub)
        run("git", "config", "user.email", "t@example.invalid", cwd=sub)
        run("git", "config", "user.name", "t", cwd=sub)
        with open(os.path.join(sub, "v.txt"), "w", encoding="utf-8") as fh:
            fh.write("one\n")
        run("git", "add", "v.txt", cwd=sub)
        run("git", "commit", "-qm", "one", cwd=sub)
        run("git", "-c", "protocol.file.allow=always", "submodule", "add",
            sub, "sub", cwd=self.dir)
        run("git", "add", ".gitmodules", "sub", cwd=self.dir)
        run("git", "commit", "-qm", "sub", cwd=self.dir)
        initial = self.snap()
        self.assertEqual(initial["files"], [])
        with open(os.path.join(sub, "v.txt"), "a", encoding="utf-8") as fh:
            fh.write("two\n")
        run("git", "commit", "-qam", "two", cwd=sub)
        new_sha = run("git", "rev-parse", "HEAD", cwd=sub).stdout.strip()
        run("git", "-c", "protocol.file.allow=always", "fetch", "-q",
            "origin", cwd=os.path.join(self.dir, "sub"))
        run("git", "checkout", "-q", new_sha,
            cwd=os.path.join(self.dir, "sub"))
        changed = self.snap()
        self.assertIn("sub", self.paths(changed))
        self.assertNotEqual(initial["digest"], changed["digest"])
        self.assertIn("+", changed["submodules"][0])



    def _add_submodule(self):
        sub = os.path.join(self.tmp.name + "-sub2")
        os.makedirs(sub)
        self.addCleanup(shutil.rmtree, sub, True)
        run("git", "init", "-q", cwd=sub)
        run("git", "config", "user.email", "t@example.invalid", cwd=sub)
        run("git", "config", "user.name", "t", cwd=sub)
        with open(os.path.join(sub, "v.txt"), "w", encoding="utf-8") as fh:
            fh.write("one\n")
        run("git", "add", "v.txt", cwd=sub)
        run("git", "commit", "-qm", "one", cwd=sub)
        run("git", "-c", "protocol.file.allow=always", "submodule", "add",
            sub, "sub", cwd=self.dir)
        run("git", "add", ".gitmodules", "sub", cwd=self.dir)
        run("git", "commit", "-qm", "sub", cwd=self.dir)
        return os.path.join(self.dir, "sub")

    def test_submodule_dirty_tracked_change_changes_digest(self):
        sub = self._add_submodule()
        clean = self.snap()
        self.assertEqual(clean["submodule_worktrees"][0]["dirty"], False)
        with open(os.path.join(sub, "v.txt"), "a", encoding="utf-8") as fh:
            fh.write("dirty\n")
        dirty = self.snap()
        state = dirty["submodule_worktrees"][0]
        self.assertTrue(state["dirty"])
        self.assertNotEqual(clean["digest"], dirty["digest"])
        item = state["files"][0]
        self.assertEqual(item["path"], "v.txt")
        self.assertEqual(item["kind"], "file")
        self.assertEqual(len(item["sha256"]), 64)

    def test_submodule_untracked_file_changes_digest(self):
        sub = self._add_submodule()
        clean = self.snap()
        with open(os.path.join(sub, "new.txt"), "w", encoding="utf-8") as fh:
            fh.write("new\n")
        dirty = self.snap()
        self.assertTrue(dirty["submodule_worktrees"][0]["dirty"])
        self.assertNotEqual(clean["digest"], dirty["digest"])
        self.assertIn("new.txt",
                      [item["path"] for item in
                       dirty["submodule_worktrees"][0]["files"]])

    def test_nested_submodule_dirty_is_included(self):
        sub = self._add_submodule()
        nested = os.path.join(self.tmp.name + "-nested")
        os.makedirs(nested)
        self.addCleanup(shutil.rmtree, nested, True)
        run("git", "init", "-q", cwd=nested)
        run("git", "config", "user.email", "t@example.invalid", cwd=nested)
        run("git", "config", "user.name", "t", cwd=nested)
        with open(os.path.join(nested, "n.txt"), "w", encoding="utf-8") as fh:
            fh.write("n\n")
        run("git", "add", "n.txt", cwd=nested)
        run("git", "commit", "-qm", "n", cwd=nested)
        run("git", "-c", "protocol.file.allow=always", "submodule", "add",
            nested, "inner", cwd=sub)
        run("git", "add", ".gitmodules", "inner", cwd=sub)
        run("git", "commit", "-qm", "inner", cwd=sub)
        clean = self.snap()
        clean_nested = clean["submodule_worktrees"][0]["nested"]
        self.assertEqual(len(clean_nested), 1)
        self.assertFalse(clean_nested[0]["dirty"])
        with open(os.path.join(sub, "inner", "n.txt"), "a",
                  encoding="utf-8") as fh:
            fh.write("dirty\n")
        dirty = self.snap()
        self.assertTrue(dirty["submodule_worktrees"][0]["dirty"])
        nested_states = dirty["submodule_worktrees"][0]["nested"]
        self.assertTrue(nested_states)
        self.assertTrue(nested_states[0]["dirty"])
        self.assertNotEqual(clean["digest"], dirty["digest"])

    def test_uninitialized_nested_submodule_fails_closed(self):
        sub = self._add_submodule()
        nested = os.path.join(self.tmp.name + "-nested2")
        os.makedirs(nested)
        self.addCleanup(shutil.rmtree, nested, True)
        run("git", "init", "-q", cwd=nested)
        run("git", "config", "user.email", "t@example.invalid", cwd=nested)
        run("git", "config", "user.name", "t", cwd=nested)
        self.write_nested = open(os.path.join(nested, "n.txt"), "w")
        self.write_nested.write("n\n")
        self.write_nested.close()
        run("git", "add", "n.txt", cwd=nested)
        run("git", "commit", "-qm", "n", cwd=nested)
        run("git", "-c", "protocol.file.allow=always", "submodule", "add",
            nested, "inner", cwd=sub)
        run("git", "add", ".gitmodules", "inner", cwd=sub)
        run("git", "commit", "-qm", "inner", cwd=sub)
        shutil.rmtree(os.path.join(sub, "inner"))
        snapshot = CS.capture_code_snapshot(self.dir)
        self.assertFalse(snapshot["available"])
        self.assertEqual(snapshot["reason"], "submodule_state_unreadable")


    def _nested_repo(self, name="vendor/nested"):
        path = os.path.join(self.dir, name)
        os.makedirs(path, exist_ok=True)
        run("git", "init", "-q", cwd=path)
        run("git", "config", "user.email", "t@example.invalid", cwd=path)
        run("git", "config", "user.name", "t", cwd=path)
        with open(os.path.join(path, "n.js"), "w", encoding="utf-8") as fh:
            fh.write("N = 1\n")
        run("git", "add", "n.js", cwd=path)
        run("git", "commit", "-qm", "n", cwd=path)
        return path

    def test_undeclared_nested_repo_dirty_is_included(self):
        nested = self._nested_repo()
        clean = self.snap()
        paths = [item["path"] for item in clean["nested_repos"]]
        self.assertIn("vendor/nested", paths)
        self.assertFalse(clean["nested_repos"][0]["dirty"])
        with open(os.path.join(nested, "n.js"), "a", encoding="utf-8") as fh:
            fh.write("dirty\n")
        dirty = self.snap()
        self.assertTrue(dirty["nested_repos"][0]["dirty"])
        self.assertNotEqual(clean["digest"], dirty["digest"])

    def test_undeclared_nested_repo_untracked_file_changes_digest(self):
        nested = self._nested_repo()
        clean = self.snap()
        with open(os.path.join(nested, "new.js"), "w", encoding="utf-8") as fh:
            fh.write("new\n")
        dirty = self.snap()
        self.assertTrue(dirty["nested_repos"][0]["dirty"])
        self.assertNotEqual(clean["digest"], dirty["digest"])
        self.assertIn("new.js", [item["path"] for item in
                                 dirty["nested_repos"][0]["files"]])

    def test_gitignored_nested_repo_is_still_snapshotted(self):
        with open(os.path.join(self.dir, ".gitignore"), "w",
                  encoding="utf-8") as fh:
            fh.write("hidden/\n")
        run("git", "add", ".gitignore", cwd=self.dir)
        run("git", "commit", "-qm", "ignore", cwd=self.dir)
        nested = self._nested_repo("hidden/repo")
        snapshot = self.snap()
        self.assertIn("hidden/repo",
                      [item["path"] for item in snapshot["nested_repos"]])
        with open(os.path.join(nested, "n.js"), "a", encoding="utf-8") as fh:
            fh.write("dirty\n")
        dirty = self.snap()
        self.assertNotEqual(snapshot["digest"], dirty["digest"])
        state = [item for item in dirty["nested_repos"]
                 if item["path"] == "hidden/repo"][0]
        self.assertTrue(state["dirty"])

    def test_nested_repo_inside_nested_repo_is_included(self):
        nested = self._nested_repo()
        inner = self._nested_repo("vendor/nested/inner")
        snapshot = self.snap()
        outer = [item for item in snapshot["nested_repos"]
                 if item["path"] == "vendor/nested"][0]
        self.assertTrue(any(item["path"] == "vendor/nested/inner"
                            for item in outer["nested"]))
        with open(os.path.join(inner, "n.js"), "a", encoding="utf-8") as fh:
            fh.write("dirty\n")
        dirty = self.snap()
        self.assertNotEqual(snapshot["digest"], dirty["digest"])

    def test_gitignored_nested_repo_inside_submodule_is_included(self):
        sub = self._add_submodule()
        with open(os.path.join(sub, ".gitignore"), "w", encoding="utf-8") as fh:
            fh.write("hidden/\n")
        run("git", "add", ".gitignore", cwd=sub)
        run("git", "commit", "-qm", "ignore", cwd=sub)
        hidden = os.path.join(sub, "hidden", "repo")
        os.makedirs(hidden)
        run("git", "init", "-q", cwd=hidden)
        run("git", "config", "user.email", "t@example.invalid", cwd=hidden)
        run("git", "config", "user.name", "t", cwd=hidden)
        with open(os.path.join(hidden, "h.js"), "w", encoding="utf-8") as fh:
            fh.write("H = 1\n")
        run("git", "add", "h.js", cwd=hidden)
        run("git", "commit", "-qm", "h", cwd=hidden)
        clean = self.snap()
        nested = clean["submodule_worktrees"][0]["nested"]
        self.assertTrue(any(item["path"] == "sub/hidden/repo"
                            for item in nested))
        with open(os.path.join(hidden, "h.js"), "a", encoding="utf-8") as fh:
            fh.write("dirty\n")
        dirty = self.snap()
        self.assertNotEqual(clean["digest"], dirty["digest"])
        state = [item for item in dirty["submodule_worktrees"][0]["nested"]
                 if item["path"] == "sub/hidden/repo"][0]
        self.assertTrue(state["dirty"])

    def test_too_deep_nested_scan_fails_closed(self):
        deep = os.path.join(self.dir, *(["d"] * 9))
        os.makedirs(deep)
        run("git", "init", "-q", cwd=deep)
        run("git", "config", "user.email", "t@example.invalid", cwd=deep)
        run("git", "config", "user.name", "t", cwd=deep)
        with open(os.path.join(deep, "d.js"), "w", encoding="utf-8") as fh:
            fh.write("D = 1\n")
        run("git", "add", "d.js", cwd=deep)
        run("git", "commit", "-qm", "d", cwd=deep)
        snapshot = CS.capture_code_snapshot(self.dir)
        self.assertFalse(snapshot["available"])
        self.assertEqual(snapshot["reason"],
                         "nested_repo_scan_depth_exceeded")

    def test_uninitialized_submodule_fails_closed(self):
        self._add_submodule()
        shutil.rmtree(os.path.join(self.dir, "sub"))
        snapshot = CS.capture_code_snapshot(self.dir)
        self.assertFalse(snapshot["available"])
        self.assertEqual(snapshot["reason"], "submodule_state_unreadable")

if __name__ == "__main__":
    unittest.main()

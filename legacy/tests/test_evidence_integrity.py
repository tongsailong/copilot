#!/usr/bin/env python3
"""证据完整性：截断闸门与代码绑定证据（git attest）。

覆盖两处复盘得到的最小改动：
- 证据包条目被采集器截断时，REVIEW 前失败关闭（或显式理由放行并留痕），
  避免把「没看到」当成「没有」而反复补证；
- `bridge_evidence.py --attest` 用 git 机器生成 HEAD / 状态 / 逐文件 sha256，
  让复核能核对被审代码，而不是只信执行方自述。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LEGACY = os.path.dirname(HERE)
ROOT = os.path.dirname(LEGACY)
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, LEGACY)

import bridge_evidence  # noqa: E402
import call_gpt  # noqa: E402
import lib_common  # noqa: E402
import review_ledger  # noqa: E402


def allow_test_audit(case: unittest.TestCase) -> None:
    """测试显式声明使用隔离的临时审计路径（生产审计路径硬门）。"""
    old = os.environ.get(lib_common.AUDIT_TEST_MODE_ENV)

    def restore() -> None:
        if old is None:
            os.environ.pop(lib_common.AUDIT_TEST_MODE_ENV, None)
        else:
            os.environ[lib_common.AUDIT_TEST_MODE_ENV] = old

    os.environ[lib_common.AUDIT_TEST_MODE_ENV] = "1"
    case.addCleanup(restore)


def isolate_standard_audit(case: unittest.TestCase, path: str) -> None:
    """把「标准审计日志」指向临时路径：新建台账只接受标准日志。"""
    original = review_ledger.DEFAULT_AUDIT_LOG
    review_ledger.DEFAULT_AUDIT_LOG = path
    case.addCleanup(setattr, review_ledger, "DEFAULT_AUDIT_LOG", original)


def write_pack(directory: str, name: str, entries: list[tuple[str, str]],
               *, truncated_chars: int = 0, truncated_entry: int = 1) -> str:
    """最小证据包；`truncated_chars` 模拟采集器写下的条目截断元数据。"""
    lines = ["# 测试证据包", "", "- 证据包格式：2", "", "## 已纳入的证据", ""]
    for idx, (source, body) in enumerate(entries, 1):
        lines += [f"### E{idx} — file: {source}", "", "- 采集方式：全文"]
        if truncated_chars and idx == truncated_entry:
            lines.append(f"- 截断：省略 {truncated_chars} 字符（见正文标注）")
        lines += ["", "```text", body, "```", ""]
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


class TruncationParseTests(unittest.TestCase):
    def test_metadata_truncation_is_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_pack(tmp, "p.md", [("a.txt", "body")], truncated_chars=6214)
            with open(path, encoding="utf-8") as fh:
                items, _ = review_ledger.split_pack(fh.read())
        self.assertTrue(items[0]["truncated"])
        self.assertEqual(items[0]["omitted_chars"], 6214)

    def test_fence_body_line_is_evidence_not_control_info(self):
        """正文里出现同样的截断标注只是被引用的文本，不能当成控制信息。"""
        body = "日志摘录：\n- 截断：省略 9 字符（见正文标注）\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = write_pack(tmp, "p.md", [("a.txt", body)])
            with open(path, encoding="utf-8") as fh:
                items, _ = review_ledger.split_pack(fh.read())
        self.assertFalse(items[0]["truncated"])
        self.assertEqual(items[0]["omitted_chars"], 0)


class MergeTruncationMetaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = os.path.join(self.tmp.name, "review.json")
        self.audit = os.path.join(self.tmp.name, "audit.jsonl")
        open(self.audit, "w", encoding="utf-8").close()
        isolate_standard_audit(self, self.audit)

    def test_new_truncated_item_is_flagged_for_gate(self):
        pack = write_pack(self.tmp.name, "r1.md", [("a.txt", "A")],
                          truncated_chars=100)
        ledger = review_ledger.load_or_init(self.ledger, risk="normal",
                                            subject="integrity", audit_log=self.audit)
        _, meta = review_ledger.merge_and_guard(
            ledger, [pack], audit_log=self.audit, ledger_path=self.ledger)
        self.assertEqual(meta["truncated_new_count"], 1)
        self.assertEqual(meta["truncated_new_chars"], 100)
        self.assertEqual(meta["truncated"], [{
            "id": "E1", "source": "a.txt", "omitted_chars": 100,
            "origin_round": 1, "is_new": True,
        }])

    def test_carried_truncation_is_not_new_and_round_record_is_audited(self):
        """历史轮次的截断只提示，不把后续复核永久锁死；入账时留下放行留痕。"""
        pack1 = write_pack(self.tmp.name, "r1.md", [("a.txt", "A")],
                           truncated_chars=100)
        ledger = review_ledger.load_or_init(self.ledger, risk="normal",
                                            subject="integrity", audit_log=self.audit)
        _, meta = review_ledger.merge_and_guard(
            ledger, [pack1], audit_log=self.audit, ledger_path=self.ledger)
        meta["truncation_accept_reason"] = "单条证据超限，已核对头尾"
        record = review_ledger.record_round(
            ledger, self.ledger, meta, "PARTIAL", "裁决：PARTIAL", "p" * 64,
            call_id="c1",
            identity={"model_requested": "m", "model_resolved": "m",
                      "model_source": "default", "model_identity_ok": True})
        self.assertEqual(record["evidence_truncated_new"], 1)
        self.assertEqual(record["evidence_truncation_accept_reason"],
                         "单条证据超限，已核对头尾")
        self.assertEqual(record["evidence_truncated_items"], ["E1"])

        pack2 = write_pack(self.tmp.name, "r2.md", [("b.txt", "B")])
        ledger = review_ledger.load_or_init(self.ledger, risk=None)
        merged, meta2 = review_ledger.merge_and_guard(
            ledger, [pack2], "同一缺陷仍未闭合，补入新证据",
            audit_log=self.audit, ledger_path=self.ledger)
        self.assertEqual(meta2["truncated_count"], 1)
        self.assertEqual(meta2["truncated_new_count"], 0)
        self.assertFalse(meta2["truncated"][0]["is_new"])
        # 沿用条目的截断标注必须仍留在合并包里，供下一轮自检表复现。
        self.assertIn("截断：省略 100 字符", merged)


class ReviewTruncationGateTests(unittest.TestCase):
    PROMPT = "请复核本轮整改。\n"
    REPLY = "裁决：PARTIAL\n\n缺证：E2。\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        allow_test_audit(self)
        self.ledger = os.path.join(self.tmp.name, "review.json")
        self.audit = os.path.join(self.tmp.name, "audit.jsonl")
        isolate_standard_audit(self, self.audit)
        self.prompt = os.path.join(self.tmp.name, "task.md")
        with open(self.prompt, "w", encoding="utf-8") as fh:
            fh.write(self.PROMPT)
        self.pack = write_pack(self.tmp.name, "evidence.md",
                              [("a.txt", "A"), ("b.txt", "B")],
                              truncated_chars=6214, truncated_entry=2)
        self.calls: list[dict] = []
        self._orig = (call_gpt.post_json, call_gpt.resolve_api_key)

        def fake_post(url, key, payload, timeout):
            self.calls.append(payload)
            return 200, {
                "id": "resp_review", "status": "completed",
                "model": payload.get("model"),
                "output": [{"type": "message",
                            "content": [{"type": "output_text", "text": self.REPLY}]}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }, "{}"

        call_gpt.post_json = fake_post
        call_gpt.resolve_api_key = lambda name: "test-key-not-real"
        self.addCleanup(self._restore)

    def _restore(self):
        call_gpt.post_json, call_gpt.resolve_api_key = self._orig

    def run_main(self, *argv):
        args = ["call_gpt.py", *argv,
                "--mode", "REVIEW", "--review-ledger", self.ledger,
                "--evidence", self.pack, "--risk", "normal", "--subject", "trunc-review",
                "--prompt-file", self.prompt, "--audit-log", self.audit,
                "--out", os.path.join(self.tmp.name, "reply.md"), "--max-retries", "0"]
        old_argv, old_stdout, old_stderr = sys.argv, sys.stdout, sys.stderr
        sys.argv, err = args, io.StringIO()
        sys.stdout, sys.stderr = io.StringIO(), err
        try:
            code = call_gpt.main()
        finally:
            self.err = err.getvalue()
            sys.stdout, sys.stderr, sys.argv = old_stdout, old_stderr, old_argv
        return code

    def records(self) -> list[dict]:
        with open(self.audit, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_truncated_evidence_blocks_review_before_paid_call(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_main()
        self.assertEqual(ctx.exception.code, call_gpt.EXIT_USAGE)
        self.assertEqual(self.calls, [], "截断证据不得触发付费调用")
        self.assertIn("被截断", self.err)
        self.assertIn("--accept-evidence-truncation", self.err)
        # 被拦下的轮次不消耗轮次，也不写审计：台账即使被创建也没有任何入账轮次。
        if os.path.exists(self.ledger):
            ledger = review_ledger.load_or_init(self.ledger, risk=None)
            self.assertEqual(ledger["rounds"], [])
        self.assertFalse(os.path.exists(self.audit),
                         "被拦下的轮次不得写审计或消耗轮次")

    def test_explicit_reason_allows_and_is_audited(self):
        code = self.run_main("--accept-evidence-truncation", "单条证据 26k 字符超单源上限")
        self.assertEqual(code, call_gpt.EXIT_OK)
        self.assertEqual(len(self.calls), 1)
        # 自检表必须把截断显式告诉模型，而不是只留在本地日志里。
        self.assertIn("证据被截断 1 条", self.calls[0]["input"])
        start = [r for r in self.records() if r.get("event") == "call_requested"][-1]
        self.assertEqual(start["review_truncated_items"], ["E2"])
        self.assertEqual(start["review_truncated_chars"], 6214)
        self.assertEqual(start["review_truncation_accept_reason"],
                         "单条证据 26k 字符超单源上限")
        ledger = review_ledger.load_or_init(self.ledger, risk=None)
        self.assertEqual(ledger["rounds"][-1]["evidence_truncation_accept_reason"],
                         "单条证据 26k 字符超单源上限")


class BridgeTruncationSummaryTests(unittest.TestCase):
    def test_manifest_and_summary_report_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "big.log")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write("x" * 5000)
            out = os.path.join(tmp, "pack.md")
            manifest = os.path.join(tmp, "pack.json")
            proc = subprocess.run(
                [sys.executable, "-B", os.path.join(SCRIPTS, "bridge_evidence.py"),
                 "--file", src, "--out", out, "--manifest", manifest,
                 "--per-source-chars", "1000", "--max-chars", "60000"],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(manifest, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual(doc["items_truncated"], 1)
            self.assertGreater(doc["omitted_chars"], 0)
            self.assertIn("条证据被截断", proc.stderr)
            with open(out, encoding="utf-8") as fh:
                body = fh.read()
            self.assertIn("- 截断：省略", body)


@unittest.skipIf(shutil.which("git") is None, "需要 git 才能验证代码绑定证据")
class AttestEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "T")
        self.write("a.txt", "one\n")
        self.git("add", "a.txt")
        self.git("commit", "-qm", "init")

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", "-C", self.repo, *args],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def write(self, name: str, text: str) -> str:
        path = os.path.join(self.repo, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def collect(self) -> bridge_evidence.Item:
        return bridge_evidence.collect_attest(None, f"{self.repo}::被测仓库")

    def test_clean_worktree_binds_head(self):
        item = self.collect()
        head = self.git("rev-parse", "HEAD").strip()
        self.assertEqual(item.kind, "git-attest")
        self.assertIn(head, item.text)
        self.assertIn("clean（无未提交改动）", item.text)
        self.assertIn("被测仓库", item.source)
        self.assertIn("git rev-parse", item.method)

    def test_dirty_worktree_lists_changed_files_with_sha256(self):
        self.write("a.txt", "one\ntwo\n")
        self.write("new.txt", "untracked\n")
        item = self.collect()
        self.assertIn("2 项未提交改动", item.text)
        self.assertIn("a.txt", item.text)
        self.assertIn("new.txt", item.text)
        self.assertIn(lib_common.sha256_file(os.path.join(self.repo, "a.txt")),
                      item.text)
        self.assertIn("diff --stat HEAD", item.text)

    def test_subdirectory_uses_repo_relative_paths(self):
        """在子目录采集时，状态路径仍相对仓库根，否则 sha256 会指向不存在的文件。"""
        os.makedirs(os.path.join(self.repo, "sub"))
        self.write("sub/b.txt", "x\n")
        self.git("add", "sub/b.txt")
        self.git("commit", "-qm", "add sub")
        self.write("sub/b.txt", "x\ny\n")
        item = bridge_evidence.collect_attest(None, os.path.join(self.repo, "sub"))
        self.assertIn("sub/b.txt", item.text)
        self.assertIn(lib_common.sha256_file(os.path.join(self.repo, "sub", "b.txt")),
                      item.text)
        self.assertNotIn("（不是普通文件", item.text)

    def test_non_git_directory_fails_closed(self):
        plain = os.path.join(self.tmp.name, "plain")
        os.makedirs(plain)
        with self.assertRaises(SystemExit) as ctx:
            bridge_evidence.collect_attest(None, plain)
        self.assertIn("不是", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

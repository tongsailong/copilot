#!/usr/bin/env python3
"""历史证据投影：非破坏渲染、原始恢复、确定性、失败关闭。

投影只改「本轮合并包的渲染」：已闭合的背景条目只给编号/轮次/来源/SHA-256/长度记录，
正文不注入。原始证据包与台账一字不改，恢复按 E 编号从原始包重读并校验摘要。
本文件锁定四件事：
1. 只有「通过模型身份验证的 PASS」之后的闭合背景可以投影，当前前沿始终给全文；
2. 最近一轮回复 / 本轮任务文本引用、来源点名、显式 `--full-evidence` 一律恢复全文；
3. 重复组装得到逐字相同的投影结果；原始台账与证据包字节不变；
4. 编号不存在、摘要不符、原始包丢失/改写时失败关闭，不用近似正文替代。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import call_gpt  # noqa: E402
import lib_common  # noqa: E402
import review_ledger  # noqa: E402

IDENTITY = {
    "model_requested": "gpt-6.1-sol",
    "model_resolved": "gpt-6.1-sol",
    "model_source": "default",
    "model_identity_ok": True,
}
PASS_REPLY = "裁决：PASS\n\n结论：本轮条件已确认。\n"

# 投影只在「正文明显大于投影记录」时生效（PROJECTION_MIN_SAVING_RATIO）；测试正文
# 必须足够大，否则会走「保留全文」分支，测不到投影本身。
ALPHA = "alpha body " * 200
BRAVO = "bravo body " * 200
CHARLIE = "charlie body " * 200
DELTA = "delta body " * 200


def allow_test_audit(case: unittest.TestCase) -> None:
    old = os.environ.get(lib_common.AUDIT_TEST_MODE_ENV)

    def restore() -> None:
        if old is None:
            os.environ.pop(lib_common.AUDIT_TEST_MODE_ENV, None)
        else:
            os.environ[lib_common.AUDIT_TEST_MODE_ENV] = old

    os.environ[lib_common.AUDIT_TEST_MODE_ENV] = "1"
    case.addCleanup(restore)


def isolate_standard_audit(case: unittest.TestCase, path: str) -> None:
    original = review_ledger.DEFAULT_AUDIT_LOG
    review_ledger.DEFAULT_AUDIT_LOG = path
    case.addCleanup(setattr, review_ledger, "DEFAULT_AUDIT_LOG", original)


def write_pack(directory: str, name: str, entries: list[tuple[str, str]],
               *, image: bool = False) -> str:
    lines = ["# 测试证据包", "", "- 证据包格式：2", "", "## 已纳入的证据", ""]
    for idx, (source, body) in enumerate(entries, 1):
        if image:
            lines += [f"### E{idx} — image: {source}", "", "- 采集方式：图片元数据",
                      "", '<!-- copilot-image {"id": "E%d", "source": "%s", '
                          '"mime": "image/png", "sha256": "%s"} -->' % (idx, source, "a" * 64),
                      ""]
            continue
        lines += [f"### E{idx} — file: {source}", "", "- 采集方式：全文", "",
                  "```text", body, "```", ""]
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def payload_of(path: str, idx: int) -> tuple[dict, str]:
    """返回证据包第 idx 条的解析结果与正文 payload（含围栏与采集元数据）。"""
    with open(path, encoding="utf-8") as fh:
        items, _ = review_ledger.split_pack(fh.read())
    item = items[idx - 1]
    body = item["body"]
    return item, body[body.find("\n") + 1:]


class ProjectionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        allow_test_audit(self)
        self.audit = os.path.join(self.tmp.name, "audit.jsonl")
        open(self.audit, "w", encoding="utf-8").close()
        isolate_standard_audit(self, self.audit)
        self.ledger_path = os.path.join(self.tmp.name, "review.json")
        self.ledger = review_ledger.load_or_init(
            self.ledger_path, risk="normal", subject="projection", audit_log=self.audit)

    def pack(self, name, entries, **kw):
        return write_pack(self.tmp.name, name, entries, **kw)

    def record(self, meta, verdict="PARTIAL", reply=None, call_id=""):
        call_id = call_id or f"call-{len(self.ledger['rounds']) + 1}"
        return review_ledger.record_round(
            self.ledger, self.ledger_path, meta, verdict,
            PASS_REPLY if reply is None and verdict == "PASS" else (reply or f"裁决：{verdict}"),
            "0" * 64, identity=IDENTITY, call_id=call_id)

    def merge(self, pack, reason="", **kw):
        return review_ledger.merge_and_guard(
            self.ledger, [pack], reason, kw.pop("basis", ""),
            audit_log=self.audit, ledger_path=self.ledger_path, **kw)

    def closed_round(self):
        """第 1 轮：两条已确认证据 + 通过身份验证的 PASS（回复不引用编号）。"""
        pack = self.pack("r1.md", [("a.py", ALPHA), ("b.py", BRAVO)])
        merged, meta = self.merge(pack)
        self.record(meta, "PASS", "裁决：PASS\n\n结论：两条证据已确认。\n")
        return pack

    def assert_ledger_bytes_unchanged(self, snapshot, path):
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), snapshot, "投影不得改写原始台账/证据包")


class ProjectionRenderTests(ProjectionCase):
    def test_closed_background_is_projected_and_current_evidence_is_full(self):
        pack1 = self.closed_round()
        with open(self.ledger_path, "rb") as fh:
            ledger_before = fh.read()
        with open(pack1, "rb") as fh:
            pack_before = fh.read()

        pack2 = self.pack("r2.md", [("c.py", CHARLIE)])
        merged, meta = self.merge(pack2, "新增 c.py 证据", basis="new-evidence")
        proj = meta["projection"]
        item1, payload1 = payload_of(pack1, 1)

        self.assertEqual(proj["version"], review_ledger.PROJECTION_VERSION)
        self.assertTrue(proj["enabled"])
        self.assertEqual(proj["pass_round"], 1)
        self.assertEqual(proj["projected_ids"], ["E1", "E2"])
        self.assertEqual(proj["projected_count"], 2)
        self.assertEqual([m["original_id"] for m in proj["mapping"]], ["E1", "E2"])
        self.assertEqual(proj["mapping"][0]["pack"], os.path.abspath(pack1))
        self.assertEqual(proj["mapping"][0]["digest"], item1["digest"])
        self.assertEqual(proj["mapping"][0]["chars"], len(payload1))
        self.assertEqual(proj["mapping"][0]["origin_round"], 1)

        # 正文不注入，但身份/完整性记录必须在；本轮新证据保持全文。
        self.assertNotIn("alpha body", merged)
        self.assertNotIn("bravo body", merged)
        self.assertIn("charlie body", merged)
        self.assertIn("### E1 — 历史证据投影（非原始证据）", merged)
        self.assertIn("投影不是证据正文", merged)
        self.assertIn(f"包 SHA-256 `{proj['mapping'][0]['pack_sha256'][:12]}…`", merged)
        self.assertIn("闭合依据：第 1 轮 PASS（模型身份已验证）", merged)

        # 非破坏：台账与历史证据包字节不变。
        self.assert_ledger_bytes_unchanged(ledger_before, self.ledger_path)
        self.assert_ledger_bytes_unchanged(pack_before, pack1)

    def test_frontier_items_after_last_pass_stay_full(self):
        pack1 = self.pack("r1.md", [("a.py", ALPHA)])
        _, meta1 = self.merge(pack1)
        self.record(meta1, "PASS", "裁决：PASS\n\n结论：已确认。\n")

        pack2 = self.pack("r2.md", [("b.py", BRAVO)])
        _, meta2 = self.merge(pack2, "补 b.py", basis="new-evidence")
        self.record(meta2, "PARTIAL", "裁决：PARTIAL\n\n缺证：b.py 未闭合。\n")

        pack3 = self.pack("r3.md", [("c.py", CHARLIE)])
        merged, meta3 = self.merge(pack3, "整改 b.py 后补 c.py", basis="remediation")
        # 第 1 轮（PASS 边界内）投影；第 2 轮属于当前前沿，必须给全文。
        self.assertEqual(meta3["projection"]["projected_ids"], ["E1"])
        self.assertIn("E2", meta3["projection"]["full_fallback_ids"])
        self.assertNotIn("alpha body", merged)
        self.assertIn("bravo body", merged)
        self.assertIn("charlie body", merged)

    def test_without_verified_pass_nothing_is_projected(self):
        pack1 = self.pack("r1.md", [("a.py", ALPHA)])
        _, meta1 = self.merge(pack1)
        self.record(meta1, "PARTIAL", "裁决：PARTIAL\n\n缺证：a.py。\n")
        pack2 = self.pack("r2.md", [("b.py", BRAVO)])
        merged, meta2 = self.merge(pack2, "补 b.py", basis="new-evidence")
        self.assertEqual(meta2["projection"]["projected_count"], 0)
        self.assertEqual(meta2["projection"]["reason"], "no_verified_pass_round")
        self.assertIn("alpha body", merged)

        # 无身份凭证的历史 PASS 同样不能作为闭合依据（P0 边界）。
        self.ledger["rounds"][0]["verdict"] = "PASS"
        self.ledger["rounds"][0].pop("model_identity_ok", None)
        with open(self.ledger_path, "w", encoding="utf-8") as fh:
            json.dump(self.ledger, fh, ensure_ascii=False)
        pack3 = self.pack("r3.md", [("c.py", CHARLIE)])
        merged3, meta3 = self.merge(pack3, "再补 c.py", basis="new-evidence")
        self.assertEqual(meta3["projection"]["projected_count"], 0)
        self.assertIn("alpha body", merged3)

    def test_missing_latest_reply_falls_back_to_full_evidence(self):
        self.closed_round()
        self.ledger["rounds"][0]["reply"] = ""
        with open(self.ledger_path, "w", encoding="utf-8") as fh:
            json.dump(self.ledger, fh, ensure_ascii=False)
        pack2 = self.pack("r2.md", [("c.py", CHARLIE)])
        merged, meta2 = self.merge(pack2, "新增 c.py", basis="new-evidence")
        self.assertEqual(meta2["projection"]["projected_count"], 0)
        self.assertEqual(meta2["projection"]["reason"], "no_recorded_reply")
        self.assertIn("alpha body", merged)

    def test_projection_can_be_disabled_for_rollback(self):
        self.closed_round()
        pack2 = self.pack("r2.md", [("c.py", CHARLIE)])
        merged, meta2 = self.merge(pack2, "新增 c.py", basis="new-evidence",
                                   history_projection=False)
        self.assertEqual(meta2["projection"]["projected_count"], 0)
        self.assertEqual(meta2["projection"]["reason"], "projection_disabled")
        self.assertIn("alpha body", merged)

    def test_image_and_truncated_items_are_never_projected(self):
        pack1 = self.pack("r1.md", [("a.py", ALPHA)])
        image_pack = self.pack("r1-img.md", [("/tmp/a.png", "")], image=True)
        _, meta1 = review_ledger.merge_and_guard(
            self.ledger, [pack1, image_pack], audit_log=self.audit,
            ledger_path=self.ledger_path)
        self.record(meta1, "PASS", "裁决：PASS\n\n结论：已确认。\n")
        pack2 = self.pack("r2.md", [("c.py", CHARLIE)])
        merged, meta2 = self.merge(pack2, "新增 c.py", basis="new-evidence")
        self.assertEqual(meta2["projection"]["projected_ids"], ["E1"])
        self.assertIn('"sha256": "' + "a" * 64, merged, "图片条目的机器可读引用必须保留")

    def test_small_bodies_are_kept_full_instead_of_projected(self):
        """正文不比投影记录大时，投影省不下空间却削弱证据，必须保留全文。"""
        pack1 = self.pack("r1.md", [("a.py", "tiny body")])
        _, meta1 = self.merge(pack1)
        self.record(meta1, "PASS", "裁决：PASS\n\n结论：已确认。\n")
        pack2 = self.pack("r2.md", [("c.py", CHARLIE)])
        merged, meta2 = self.merge(pack2, "新增 c.py", basis="new-evidence")
        self.assertEqual(meta2["projection"]["projected_count"], 0)
        self.assertEqual(meta2["projection"]["kept_full_small_ids"], ["E1"])
        self.assertIn("tiny body", merged)

    def test_projection_render_is_deterministic(self):
        self.closed_round()
        pack2 = self.pack("r2.md", [("c.py", CHARLIE)])
        _, meta_a = self.merge(pack2, "新增 c.py", basis="new-evidence")
        _, meta_b = self.merge(pack2, "新增 c.py", basis="new-evidence")
        self.assertEqual(meta_a["projection"]["mapping"], meta_b["projection"]["mapping"])
        record_a = review_ledger._projection_record(
            1, {"origin_round": 1, "pack_path": "/tmp/p.md", "pack_sha256": "f" * 64,
                "digest": "d" * 64, "old_id": "E1", "kind_source": "file: a.py",
                "body": "### E1 — file: a.py\n\n- 采集方式：全文\n\n```text\nalpha\n```\n",
                "content_key": "k", "lines": []},
            self.ledger, 1, 1)
        record_b = review_ledger._projection_record(
            1, {"origin_round": 1, "pack_path": "/tmp/p.md", "pack_sha256": "f" * 64,
                "digest": "d" * 64, "old_id": "E1", "kind_source": "file: a.py",
                "body": "### E1 — file: a.py\n\n- 采集方式：全文\n\n```text\nalpha\n```\n",
                "content_key": "k", "lines": []},
            self.ledger, 1, 1)
        self.assertEqual(record_a, record_b)

        pack3 = self.pack("r3.md", [("d.py", DELTA)])
        text_a, meta_c = self.merge(pack3, "新增 d.py", basis="new-evidence")
        text_b, _ = self.merge(pack3, "新增 d.py", basis="new-evidence")
        strip = lambda t: re.sub(r"^- 生成时间：.*\n", "", t, flags=re.M)
        self.assertEqual(strip(text_a), strip(text_b))
        self.assertEqual(meta_c["projection"]["projected_count"], 2)

    def test_projection_records_are_not_parseable_as_evidence_bodies(self):
        self.closed_round()
        pack2 = self.pack("r2.md", [("c.py", CHARLIE)])
        merged, meta2 = self.merge(pack2, "新增 c.py", basis="new-evidence")
        items, _ = review_ledger.split_pack(merged)
        by_id = {it["old_id"]: it for it in items}
        self.assertFalse(by_id["E1"]["intact"], "投影不能被当作结构完整的证据正文")
        self.assertTrue(by_id["E3"]["intact"])

        text, stats = call_gpt.evidence_selfcheck_text(
            merged, "offline#r2", projected_ids=set(meta2["projection"]["projected_ids"]))
        self.assertEqual(stats["projected"], ["E1", "E2"])
        self.assertIn("历史证据投影 2 条", text)
        self.assertIn("投影不是证据正文", text)


class ProjectionRestoreTests(ProjectionCase):
    def test_current_task_text_citation_restores_full_body(self):
        self.closed_round()
        pack2 = self.pack("r2.md", [("c.py", CHARLIE)])
        merged, meta2 = self.merge(pack2, "复核 E1 的整改", basis="remediation",
                                   focus_text="本轮只复核 E1 的整改是否关闭。")
        self.assertEqual(meta2["projection"]["projected_ids"], ["E2"])
        self.assertIn("alpha body", merged)
        self.assertNotIn("bravo body", merged)

    def test_latest_reply_citation_restores_full_body(self):
        pack1 = self.pack("r1.md", [("a.py", ALPHA), ("b.py", BRAVO)])
        _, meta1 = self.merge(pack1)
        self.record(meta1, "PASS", "裁决：PASS\n\n结论：E1 已确认；E2 也已确认。\n")
        pack2 = self.pack("r2.md", [("c.py", CHARLIE)])
        merged, meta2 = self.merge(pack2, "新增 c.py", basis="new-evidence")
        self.assertEqual(meta2["projection"]["restored_ids"], ["E1", "E2"])
        self.assertIn("alpha body", merged)
        self.assertIn("bravo body", merged)

    def test_source_path_mention_restores_full_body(self):
        self.closed_round()
        pack2 = self.pack("r2.md", [("c.py", CHARLIE)])
        merged, meta2 = self.merge(pack2, "复核 a.py 的改动", basis="remediation",
                                   focus_text="请核对 a.py 的整改。")
        self.assertEqual(meta2["projection"]["projected_ids"], ["E2"])
        self.assertEqual(meta2["projection"]["restored_sources"], ["a.py"])
        self.assertIn("alpha body", merged)

    def test_explicit_full_evidence_restores_and_rejects_unknown_ids(self):
        self.closed_round()
        pack2 = self.pack("r2.md", [("c.py", CHARLIE)])
        merged, meta2 = self.merge(pack2, "新增 c.py", basis="new-evidence",
                                   full_evidence=["E2"])
        self.assertEqual(meta2["projection"]["projected_ids"], ["E1"])
        self.assertIn("bravo body", merged)
        self.assertNotIn("alpha body", merged)
        with self.assertRaises(review_ledger.LedgerError) as ctx:
            self.merge(pack2, "新增 c.py", basis="new-evidence", full_evidence=["E99"])
        self.assertIn("不在本轮合并证据中", str(ctx.exception))
        with self.assertRaises(review_ledger.LedgerError):
            self.merge(pack2, "新增 c.py", basis="new-evidence", full_evidence=["evidence"])


class RecoveryTests(ProjectionCase):
    def test_recover_returns_exact_original_body_by_id_and_sha256(self):
        pack1 = self.closed_round()
        item1, payload1 = payload_of(pack1, 1)
        recovered = review_ledger.recover_evidence(self.ledger, ["E1"])[0]
        self.assertEqual(recovered["id"], "E1")
        self.assertEqual(recovered["origin_round"], 1)
        self.assertEqual(recovered["pack"], os.path.abspath(pack1))
        self.assertEqual(recovered["digest"], item1["digest"])
        self.assertTrue(recovered["body"].startswith("### E1 — file: a.py"))
        self.assertIn("alpha body", recovered["body"])
        self.assertEqual(recovered["chars"], len(payload1))

    def test_recover_unknown_id_fails_closed(self):
        self.closed_round()
        with self.assertRaises(review_ledger.LedgerError) as ctx:
            review_ledger.recover_evidence(self.ledger, ["E9"])
        self.assertIn("不在历史证据中", str(ctx.exception))

    def test_recover_fails_when_original_pack_is_missing_or_tampered(self):
        pack1 = self.closed_round()
        with open(pack1, "a", encoding="utf-8") as fh:
            fh.write("\n篡改\n")
        with self.assertRaises(review_ledger.LedgerError) as ctx:
            review_ledger.recover_evidence(self.ledger, ["E1"])
        self.assertIn("内容已变化", str(ctx.exception))

        os.remove(pack1)
        with self.assertRaises(review_ledger.LedgerError) as ctx2:
            review_ledger.recover_evidence(self.ledger, ["E1"])
        self.assertIn("已不存在", str(ctx2.exception))

    def test_recover_fails_when_ledger_records_no_fingerprints(self):
        self.closed_round()
        for rnd in self.ledger.get("rounds", []):
            rnd.pop("all_item_hashes", None)
        with self.assertRaises(review_ledger.LedgerError) as ctx:
            review_ledger.recover_evidence(self.ledger, ["E1"])
        self.assertIn("all_item_hashes", str(ctx.exception))


class ProjectionRoundRecordTests(ProjectionCase):
    def test_round_record_carries_projection_mapping(self):
        self.closed_round()
        pack2 = self.pack("r2.md", [("c.py", CHARLIE)])
        _, meta2 = self.merge(pack2, "新增 c.py", basis="new-evidence")
        record = self.record(meta2, "PASS")
        self.assertEqual(record["projection"]["projected_ids"], ["E1", "E2"])
        self.assertEqual(len(record["projection"]["mapping"]), 2)
        with open(self.ledger_path, encoding="utf-8") as fh:
            stored = json.load(fh)
        self.assertEqual(stored["rounds"][-1]["projection"]["projected_ids"],
                         ["E1", "E2"])

    def test_rendered_ledger_states_projection_without_weakening_discipline(self):
        self.closed_round()
        pack2 = self.pack("r2.md", [("c.py", CHARLIE)])
        _, meta2 = self.merge(pack2, "新增 c.py", basis="new-evidence")
        text = review_ledger.render_ledger(self.ledger, meta2)
        self.assertIn("历史证据投影：2 条正文未注入", text)
        self.assertIn("投影不是证据正文，不能替代原始证据", text)
        self.assertIn("缺证：E<n> 原始正文未注入", text)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""SOL 图像材料：真实像素进入 input_image，审计只留指纹，不做 OCR 或文字替代。

这些测试锁定三件事：

1. 文本调用的请求体形状不变（`input` 仍是字符串）；
2. 有图片时使用 Responses API 的 content 块数组，顺序与 `--image` 一致；
3. 图片只能以真实文件进入，上限失败关闭；状态/审计永远不写 base64。
"""
from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import sol_review as SR  # noqa: E402


def png_bytes(width: int = 2, height: int = 2,
              rgba: tuple[int, int, int, int] = (160, 79, 29, 255)) -> bytes:
    """构造一张最小合法 PNG，用于验证魔数与尺寸解析。"""
    raw = b""
    for _ in range(height):
        raw += b"\x00" + bytes(rgba) * width

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def cognitive_reply() -> str:
    payload = {
        "schema": SR.THINK_SCHEMA,
        "judgment": "先看真实像素再判断",
        "goal_understanding": "确认设计是否真的改进",
        "essence": "判断依赖实际画面",
        "strategy": "逐屏看构图与层级",
        "alternatives": ["只看代码"],
        "tradeoffs": ["看图像更慢但更可靠"],
        "risks": ["截图不是当前版本"],
        "priority": ["P1 看真实截图"],
        "investigation": ["读取截图"],
        "phase_goals": ["锁定当前版本"],
        "assumptions": ["截图来自当前产物"],
        "omissions": [],
        "decisions": [],
        "verify": ["sha256 与产物一致"],
        "reconsult": [],
        "unverified": [],
        "agenda": [],
    }
    return json.dumps(payload, ensure_ascii=False)


def fake_transport(response_text: str):
    def transport(url, key, body, timeout):
        return {
            "model": SR.MODEL,
            "status": "completed",
            "output": [{"type": "message", "status": "completed",
                        "content": [{"type": "output_text",
                                     "text": response_text}]}],
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "_http_status": 200,
            "_request_id": "req-img",
            "_trace_id": "trace-img",
        }
    return transport


class ImagePayloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name: str, data: bytes) -> str:
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_text_payload_keeps_string_input(self):
        payload = SR.build_payload("only text")
        self.assertIsInstance(payload["input"], str)
        self.assertEqual(payload["tool_choice"], "none")

    def test_image_payload_uses_content_blocks_in_order(self):
        first = self.write("a.png", png_bytes(3, 4))
        second = self.write("b.png", png_bytes(5, 6, (10, 20, 30, 255)))
        images = SR.load_images([first, second], labels=["P1 默认", "P2 空态"])
        payload = SR.build_payload("materials", images=images)
        self.assertIsInstance(payload["input"], list)
        blocks = payload["input"][0]["content"]
        self.assertEqual([b["type"] for b in blocks],
                         ["input_text", "input_image", "input_image"])
        self.assertEqual(blocks[0]["text"], "materials")
        self.assertTrue(blocks[1]["image_url"].startswith("data:image/png;base64,"))
        meta = SR.image_public_metadata(images)
        self.assertEqual([m["label"] for m in meta], ["P1 默认", "P2 空态"])
        self.assertEqual((meta[0]["width"], meta[0]["height"]), (3, 4))
        self.assertEqual((meta[1]["width"], meta[1]["height"]), (5, 6))

    def test_public_metadata_has_no_pixels_or_path(self):
        path = self.write("a.png", png_bytes())
        images = SR.load_images([path])
        meta = SR.image_public_metadata(images)
        blob = json.dumps(meta, ensure_ascii=False)
        self.assertNotIn("base64", blob)
        self.assertNotIn("data_url", blob)
        self.assertNotIn(self.tmp.name, blob)
        self.assertEqual(len(meta[0]["sha256"]), 64)

    def test_digest_changes_with_pixels_and_labels(self):
        one = self.write("a.png", png_bytes(2, 2))
        two = self.write("b.png", png_bytes(3, 3))
        first = SR.load_images([one])
        second = SR.load_images([two])
        self.assertNotEqual(SR.images_digest(first), SR.images_digest(second))
        relabeled = SR.load_images([one], labels=["换了说明"])
        self.assertNotEqual(SR.images_digest(first), SR.images_digest(relabeled))

    def test_rejects_unsupported_format(self):
        path = self.write("fake.gif", b"GIF89a" + b"\x00" * 32)
        with self.assertRaises(SR.SolUsageError) as ctx:
            SR.load_images([path])
        self.assertEqual(ctx.exception.code, "sol_image_format_unsupported")

    def test_rejects_missing_file(self):
        with self.assertRaises(SR.SolUsageError) as ctx:
            SR.load_images([os.path.join(self.tmp.name, "nope.png")])
        self.assertEqual(ctx.exception.code, "sol_image_missing")

    def test_rejects_too_many_images(self):
        paths = [self.write(f"{i}.png", png_bytes()) for i in range(7)]
        with self.assertRaises(SR.SolUsageError) as ctx:
            SR.load_images(paths)
        self.assertEqual(ctx.exception.code, "sol_image_count_exceeded")

    def test_rejects_label_mismatch(self):
        path = self.write("a.png", png_bytes())
        with self.assertRaises(SR.SolUsageError) as ctx:
            SR.load_images([path], labels=["a", "b"])
        self.assertEqual(ctx.exception.code, "sol_image_label_mismatch")

    def test_rejects_invalid_block_structure(self):
        with self.assertRaises(SR.SolUsageError) as ctx:
            SR.build_payload("text", images=[{"mime": "image/png"}])
        self.assertEqual(ctx.exception.code, "sol_image_invalid")


class ImageAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit = os.path.join(self.tmp.name, "sol-audit.jsonl")

    def test_audit_records_fingerprint_not_pixels(self):
        path = os.path.join(self.tmp.name, "shot.png")
        with open(path, "wb") as fh:
            fh.write(png_bytes(4, 5))
        images = SR.load_images([path], labels=["P1 默认 1.0"])
        result = SR.run_sol(
            "materials", mode="CONSULT", audit_log=self.audit,
            transport=fake_transport(cognitive_reply()),
            api_key="test-key", base_url="http://127.0.0.1:8317/v1",
            images=images)
        self.assertEqual(result["image_count"], 1)
        self.assertEqual(result["images"][0]["sha256"],
                         images[0]["sha256"])
        self.assertNotIn("data_url", json.dumps(result, ensure_ascii=False))
        with open(self.audit, encoding="utf-8") as fh:
            record = json.loads(fh.readline())
        blob = json.dumps(record, ensure_ascii=False)
        self.assertNotIn("base64", blob)
        self.assertNotIn(self.tmp.name, blob)
        self.assertEqual(record["image_count"], 1)
        self.assertEqual(record["input_type"], "content_blocks")
        self.assertEqual(record["images"][0]["sha256"], images[0]["sha256"])

    def test_text_call_audit_still_marks_text_input(self):
        result = SR.run_sol(
            "materials", mode="CONSULT", audit_log=self.audit,
            transport=fake_transport(cognitive_reply()),
            api_key="test-key", base_url="http://127.0.0.1:8317/v1")
        self.assertEqual(result["image_count"], 0)
        self.assertEqual(result["images"], [])
        with open(self.audit, encoding="utf-8") as fh:
            record = json.loads(fh.readline())
        self.assertEqual(record["input_type"], "text")


if __name__ == "__main__":
    unittest.main()

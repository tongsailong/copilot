#!/usr/bin/env python3
"""统一 Reviewer 输出解析契约（review-json/v2）。"""
from __future__ import annotations

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import review_output as R  # noqa: E402

GOOD = {"schema": "copilot-native-review/v2", "verdict": "pass",
        "findings": [], "changed_files": [], "unverified": []}


class ReviewOutputTests(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(R.parse_review_output(json.dumps(GOOD))["verdict"],
                         "pass")

    def test_single_fence(self):
        text = "```json\n" + json.dumps(GOOD) + "\n```"
        self.assertEqual(R.parse_review_output(text)["schema"],
                         GOOD["schema"])

    def test_fence_outside_text_rejected(self):
        text = "结论如下：\n```json\n" + json.dumps(GOOD) + "\n```"
        with self.assertRaises(R.ReviewOutputError) as ctx:
            R.parse_review_output(text)
        self.assertEqual(ctx.exception.code, "review_output_outside_text")

    def test_multiple_fences_rejected(self):
        body = "```json\n" + json.dumps(GOOD) + "\n```"
        with self.assertRaises(R.ReviewOutputError) as ctx:
            R.parse_review_output(body + "\n" + body)
        self.assertEqual(ctx.exception.code, "review_output_ambiguous")

    def test_duplicate_keys_rejected(self):
        text = ('{"schema":"copilot-native-review/v2","verdict":"pass",'
                '"verdict":"partial","findings":[]}')
        with self.assertRaises(R.ReviewOutputError) as ctx:
            R.parse_review_output(text)
        self.assertEqual(ctx.exception.code, "review_output_unparseable")

    def test_non_object_rejected(self):
        with self.assertRaises(R.ReviewOutputError) as ctx:
            R.parse_review_output("[]")
        self.assertEqual(ctx.exception.code, "review_output_not_object")

    def test_validate_requires_schema_and_verdict(self):
        with self.assertRaises(R.ReviewOutputError):
            R.validate_envelope({"findings": []})
        with self.assertRaises(R.ReviewOutputError):
            R.validate_envelope({"schema": "x", "verdict": "ok",
                                 "findings": []})
        with self.assertRaises(R.ReviewOutputError):
            R.validate_envelope({"schema": "x", "verdict": "pass",
                                 "findings": "no"})


if __name__ == "__main__":
    unittest.main()

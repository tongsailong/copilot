#!/usr/bin/env python3
"""v6.3 复核收敛回归：真实误拒、解析变体、相对路径、finding 元数据与指标。

用例只覆盖真实发生过的机制失败（ad-strategy 的 `git rev-list`/`paste`、honor 的
围栏输出与 `cd` 相对路径、review-perf/gromore 的重复 finding 与证据漂移），不为
理论边界扩张。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import v6_support as F

import copilot_auto as AC  # noqa: E402
import governance as G  # noqa: E402
import native_agent as NA  # noqa: E402
import native_bridge as NB  # noqa: E402










class IngestAuditV63Tests(F.BridgeBase):
    def setUp(self):
        super().setUp()
        state = G.load_state(self.state_path)
        state["native_dispatch"] = self.dispatch
        G.save_state(self.state_path, state)

    def test_execution_ingest_writes_history_event(self):
        AC._ingest_execution(self.state_path, self.execution())
        state = G.load_state(self.state_path)
        events = [h["event"] for h in state.get("history") or []]
        self.assertIn("native_result_ingested", events)


class ReviewMetricsV63Tests(unittest.TestCase):
    def test_metrics_mark_g4_retired_and_count_sol(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = {
                "task_id": "task-metrics", "risk": "high",
                "execution_backend": "legacy",
                "created": "2026-11-14T10:00:00+08:00",
                "sol_reviews": [
                    {"status": "completed", "verdict": "pass",
                     "findings_count": {"blocking": 0, "major": 0,
                                        "minor": 1, "info": 0}},
                    {"status": "failed", "error": "cognition_timeout"},
                ],
                "review_admissions": [
                    {"tier": "high", "triggers": ["security"]},
                ],
                "review_stops": [{"stop_id": "RST1"}],
                "native_reviews": [{"g4_eligible": True}],
                "formal_reviews": [{"verdict": "pass"}],
                "closure": {"closed": True,
                            "closed_at": "2026-11-14T12:30:00+08:00"},
            }
            with open(os.path.join(tmp, "review-attempts.jsonl"), "w",
                      encoding="utf-8") as fh:
                fh.write(json.dumps({"kind": "execution",
                                     "code": "evidence_sha_mismatch"}) + "\n")
            metrics = G.review_metrics(state, task_dir=tmp)
        self.assertEqual(metrics["cognition_reviews"]["total"], 2)
        self.assertEqual(metrics["cognition_reviews"]["completed"], 1)
        self.assertEqual(metrics["cognition_reviews"]["failed"], 1)
        self.assertEqual(metrics["cognition_reviews"]["verdicts"]["pass"], 1)
        self.assertEqual(metrics["cognition_reviews"]["findings"]["minor"], 1)
        self.assertEqual(metrics["review_admissions_total"], 1)
        self.assertEqual(metrics["review_stops_total"], 1)
        self.assertEqual(metrics["formal_reviews_total"], 1)
        self.assertTrue(metrics["g4"]["retired"])
        self.assertEqual(metrics["g4"]["historical_native_reviews"], 1)
        self.assertNotIn("g4_reviews", metrics)
        self.assertEqual(metrics["invalid_envelopes"],
                         {"execution": 1, "review": 0, "review_build": 0})
        self.assertEqual(metrics["invalid_envelope_codes"],
                         {"evidence_sha_mismatch": 1})
        self.assertEqual(metrics["task_seconds"], 9000.0)


if __name__ == "__main__":
    unittest.main()

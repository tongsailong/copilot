#!/usr/bin/env python3
"""efficiency_report.py 只读效能观测回归。

覆盖：

1. 指标口径标注（measured / proxy / unavailable），缺数据不猜测；
2. 任务台账统计（时长、条件、证据复用、工作项重开、SOL Token/延迟/采纳）；
3. 指令注入面（SKILL.md/AGENTS.md 字节与余量）；
4. 召回事件与宿主库缺失时降级为 unavailable，存在时只读聚合；
5. 只读：运行不创建/修改工作区文件，不写入任何状态。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest

import v6_support as F  # noqa: F401  把 scripts/ 加入 sys.path

import efficiency_report as ER


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def task_state(*, task_id: str, closed: bool, created: str, updated: str,
               closed_at: str = "", verdict: str = "",
               criteria: list | None = None, actions: list | None = None,
               evidence: list | None = None, cognition_calls: list | None = None,
               obligations: list | None = None,
               reopen: int = 0) -> dict:
    return {
        "task_id": task_id,
        "workspace": "/tmp/ws",
        "task_kind": "standard",
        "risk": "medium",
        "created": created,
        "updated": updated,
        "closure": {
            "closed": closed,
            "verdict": verdict,
            "closed_at": closed_at,
            "reopen_history": [{"at": updated} for _ in range(reopen)],
            "quality": {},
        },
        "contract": {"criteria": criteria or []},
        "actions": actions or [],
        "evidence": evidence or [],
        "cognition_calls": cognition_calls or [],
        "cognition_schedule": {"obligations": obligations or []},
        "blocks": [],
    }


class EfficiencyReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.workspace = os.path.join(self.root, "ws")
        self.tasks = os.path.join(self.workspace, ".copilot-tasks")
        write(os.path.join(self.root, "SKILL.md"), "s" * 1000)
        write(os.path.join(self.root, "AGENTS.md"), "a" * 500)

    def _task(self, name: str, state: dict) -> None:
        write(os.path.join(self.tasks, name, "state.json"),
              json.dumps(state, ensure_ascii=False))

    def build(self, **kwargs) -> dict:
        options = dict(workspaces=[self.workspace], repo_root=self.root,
                       recall_events=os.path.join(self.root, "missing.jsonl"))
        options.update(kwargs)
        return ER.build_report(**options)

    def metric(self, report: dict, metric_id: str) -> dict:
        return [m for m in report["metrics"] if m["id"] == metric_id][0]

    def test_instruction_surface_is_measured_with_headroom(self):
        report = self.build()
        skill = self.metric(report, "skill_bytes")
        self.assertEqual(skill["value"], 1000)
        self.assertEqual(skill["basis"], "measured")
        self.assertEqual(self.metric(report, "skill_headroom")["value"],
                         ER.SKILL_LIMIT_BYTES - 1000)
        self.assertEqual(self.metric(report, "agents_md_bytes")["value"], 500)
        self.assertEqual(self.metric(report, "instruction_total_bytes")["value"],
                         1500)

    def test_instruction_surface_without_files_degrades_to_unavailable(self):
        # 真实宿主自检发现：在无 SKILL.md 的目录执行时余量计算 TypeError 崩溃。
        report = self.build(repo_root=os.path.join(self.root, "empty"))
        self.assertEqual(self.metric(report, "skill_bytes")["basis"], "unavailable")
        headroom = self.metric(report, "skill_headroom")
        self.assertIsNone(headroom["value"])
        self.assertEqual(headroom["basis"], "unavailable")
        self.assertEqual(self.metric(report, "instruction_total_bytes")["basis"],
                         "unavailable")

    def test_ledger_metrics_and_proxy_labels(self):
        call = {"usage": {"total_tokens": 1200}, "latency_ms": 4000,
                "acceptance": {"status": "accepted"}}
        pending = {"usage": {"total_tokens": 800}, "latency_ms": 6000,
                   "acceptance": {"status": "pending"}}
        self._task("closed", task_state(
            task_id="t-closed", closed=True, verdict="pass",
            created="2026-11-24T10:00:00+08:00",
            updated="2026-11-24T12:30:00+08:00",
            closed_at="2026-11-24T12:30:00+08:00",
            criteria=[
                {"status": "met", "evidence_refs": [{"id": "E1"}, {"id": "E2"}]},
                {"status": "met", "evidence_refs": [{"id": "E1"}]},
            ],
            actions=[{"status": "done", "evidence_ids": ["E1"],
                      "rerun_count": 2}],
            evidence=[{"id": "E1", "dropped": False},
                      {"id": "E2", "dropped": True}],
            cognition_calls=[call, pending],
            obligations=[{"window": "closed"}, {"window": "open"}],
            reopen=1))
        self._task("open", task_state(
            task_id="t-open", closed=False,
            created="2026-11-23T10:00:00+08:00",
            updated="2026-11-23T11:00:00+08:00"))
        report = self.build()
        self.assertEqual(self.metric(report, "tasks_total")["value"], 2)
        self.assertEqual(self.metric(report, "tasks_closed")["value"], 1)
        self.assertEqual(self.metric(report, "tasks_open")["value"], 1)
        self.assertEqual(self.metric(report, "tasks_with_reopen")["value"], 1)
        self.assertEqual(self.metric(report, "duration_p50")["value"], 2.5)
        self.assertEqual(self.metric(report, "criteria_met")["value"], 2)
        self.assertEqual(self.metric(report, "actions_failed")["value"], 0)
        self.assertEqual(self.metric(report, "action_reruns")["value"], 2)
        self.assertEqual(self.metric(report, "evidence_dropped")["value"], 1)
        # 引用 4 次、唯一 2 个 → 复用率 0.5，且只能标 proxy。
        self.assertEqual(self.metric(report, "evidence_refs")["value"], 4)
        self.assertEqual(self.metric(report, "evidence_reuse_ratio")["value"],
                         0.5)
        self.assertEqual(
            self.metric(report, "evidence_reuse_ratio")["basis"], "proxy")
        self.assertEqual(self.metric(report, "cognition_calls")["value"], 2)
        self.assertEqual(self.metric(report, "cognition_tokens")["value"], 2000)
        self.assertEqual(self.metric(report, "cognition_latency_p50")["value"], 4000.0)
        self.assertEqual(
            self.metric(report, "cognition_acceptance_recorded")["value"], 1)
        self.assertEqual(
            self.metric(report, "cognition_acceptance_pending")["value"], 1)
        self.assertEqual(self.metric(report, "obligations_open")["value"], 1)
        self.assertEqual(self.metric(report, "urging_count")["basis"],
                         "unavailable")

    def test_missing_workspace_is_unavailable_not_zero(self):
        report = ER.build_report(
            workspaces=[os.path.join(self.root, "nope")],
            repo_root=self.root,
            recall_events=os.path.join(self.root, "nope.jsonl"))
        tasks = self.metric(report, "tasks")
        self.assertIsNone(tasks["value"])
        self.assertEqual(tasks["basis"], "unavailable")
        recall = self.metric(report, "recall_events")
        self.assertIsNone(recall["value"])
        self.assertEqual(recall["basis"], "unavailable")
        host = self.metric(report, "host_db")
        self.assertEqual(host["basis"], "unavailable")

    def test_recall_events_distinguish_no_hits_budget(self):
        events = os.path.join(self.root, "events.jsonl")
        rows = [
            {"decision": "injected", "status": "budget_exhausted",
             "injected_chars": 700, "elapsed_ms": 80},
            {"decision": "no-hits", "status": "budget_exhausted",
             "injected_chars": 0, "elapsed_ms": 60},
            {"decision": "no-hits", "status": "ok",
             "injected_chars": 0, "elapsed_ms": 40},
        ]
        write(events, "\n".join(json.dumps(r) for r in rows) + "\n")
        report = self.build(recall_events=events)
        self.assertEqual(self.metric(report, "recall_injected")["value"], 1)
        self.assertEqual(self.metric(report, "recall_no_hits")["value"], 2)
        self.assertEqual(
            self.metric(report, "recall_no_hits_budget")["value"], 1)
        self.assertEqual(
            self.metric(report, "recall_no_hits_budget")["basis"], "proxy")
        self.assertEqual(self.metric(report, "recall_elapsed_p50")["value"], 60.0)

    def test_host_db_is_read_only_and_degrades_on_missing_tables(self):
        db = os.path.join(self.root, "host.sqlite")
        now_ms = int(__import__("time").time() * 1000)
        conn = sqlite3.connect(db)
        conn.executescript(
            "create table turn_usage (started_at integer,"
            " model_request_count integer, tool_call_count integer,"
            " tool_error_count integer, computed_total_tokens integer,"
            " cache_read_input_tokens integer, context_exceeded integer);"
            "create table tool_usage (started_at integer, duration_ms integer);"
            "create table model_usage (started_at integer, model_id text,"
            " computed_total_tokens integer);"
            f"insert into turn_usage values ({now_ms}, 2, 3, 1, 100, 50, 0);"
            f"insert into tool_usage values ({now_ms}, 10);"
            f"insert into model_usage values ({now_ms}, 'm', 100);")
        conn.commit()
        conn.close()
        before = sorted(os.listdir(self.root))
        report = self.build(host_db=db, days=30)
        self.assertEqual(self.metric(report, "host_turns")["value"], 1)
        self.assertEqual(self.metric(report, "host_tool_errors")["value"], 1)
        self.assertEqual(self.metric(report, "host_tokens")["value"], 150)
        self.assertEqual(sorted(os.listdir(self.root)), before)
        # 损坏/空库只降级，不抛异常。
        empty = os.path.join(self.root, "empty.sqlite")
        sqlite3.connect(empty).close()
        degraded = self.build(host_db=empty)
        self.assertEqual(self.metric(degraded, "host_db")["basis"],
                         "unavailable")
        missing = self.build(host_db=os.path.join(self.root, "none.sqlite"))
        self.assertEqual(self.metric(missing, "host_db")["basis"], "unavailable")

    def test_cli_json_contract_and_no_writes(self):
        self._task("closed", task_state(
            task_id="t-closed", closed=True, verdict="pass",
            created="2026-11-24T10:00:00+08:00",
            updated="2026-11-24T11:00:00+08:00",
            closed_at="2026-11-24T11:00:00+08:00"))
        before = {}
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for name in filenames:
                path = os.path.join(dirpath, name)
                before[path] = os.path.getsize(path)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ER.main(["--workspace", self.workspace, "--repo-root",
                            self.root, "--recall-events",
                            os.path.join(self.root, "none.jsonl"), "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["schema"], ER.SCHEMA)
        self.assertTrue(report["metrics"])
        for metric in report["metrics"]:
            self.assertIn(metric["basis"],
                          ("measured", "proxy", "unavailable"))
        after = {}
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for name in filenames:
                path = os.path.join(dirpath, name)
                after[path] = os.path.getsize(path)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()

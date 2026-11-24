#!/usr/bin/env python3
"""efficiency_report.py — 只读开发效能观测（不是门禁、不写状态、不调用模型）。

复用已经存在的事实来源，给“协作治理本身是否划算”提供可复核的参考：

- Copilot 任务台账（`<workspace>/.copilot-tasks/*/state.json`）：任务时长、条件核销、
  证据复用、工作项重开、阻塞/重开、SOL 调用与义务、采纳回填；
- 指令注入面（`SKILL.md` / `AGENTS.md` 字节数）：每次会话/技能调用的固定上下文成本；
- 自动召回事件（`~/.zcode/copilot-runs/context-recall/events.jsonl`，可选）；
- 本机宿主库（`--host-db`，只读聚合 `turn_usage` / `model_usage` / `tool_usage` 的
  数字列，不读取消息内容）。

所有输出都标注口径：`measured`（台账/文件/库里的直接计数或派生）、`proxy`（间接代理，
不能单独证明结论）、`unavailable`（当前没有可靠来源，不猜测）。本工具不产生 PASS、
不改变阶段、不新增监控平台、不发起任何模型调用；缺数据只降级为 `unavailable`。

用法：

    python3 -B scripts/efficiency_report.py
    python3 -B scripts/efficiency_report.py --workspace /media/vdb/projects/rili --json
    python3 -B scripts/efficiency_report.py --host-db ~/.zcode/cli/db/db.sqlite --days 7
    python3 -B scripts/efficiency_report.py --state T/state.json

退出码：0 = 已输出（允许部分指标 unavailable）；2 = 参数错误；1 = 内部错误。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

SCHEMA = "copilot-efficiency-report/v1"
DEFAULT_RECALL = "~/.zcode/copilot-runs/context-recall/events.jsonl"
SKILL_LIMIT_BYTES = 100_000
HOST_DB_DEFAULT = "~/.zcode/cli/db/db.sqlite"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _read_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_jsonl(path: str) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return float(ordered[index])


def _metric(metric_id: str, label: str, value, *, unit: str = "",
            basis: str = "measured", note: str = "") -> dict:
    return {"id": metric_id, "label": label, "value": value, "unit": unit,
            "basis": basis, "note": note}


def _unavailable(metric_id: str, label: str, note: str) -> dict:
    return _metric(metric_id, label, None, basis="unavailable", note=note)


# ── 指令注入面 ───────────────────────────────────────────────────────────
def instruction_surface(repo_root: str) -> dict:
    skill = os.path.join(repo_root, "SKILL.md")
    agents = os.path.join(repo_root, "AGENTS.md")
    metrics: list[dict] = []
    try:
        skill_bytes = os.path.getsize(skill)
    except OSError:
        skill_bytes = None
    try:
        agents_bytes = os.path.getsize(agents)
    except OSError:
        agents_bytes = None
    metrics.append(_metric(
        "skill_bytes", "SKILL.md 字节数", skill_bytes, unit="bytes",
        basis="measured" if skill_bytes is not None else "unavailable",
        note="调用 Skill 时载入的正文（常驻的只有 description）；宿主单文件上限 100000 字节，"
             "超限会静默截断头部。"
        if skill_bytes is not None else "SKILL.md 不存在。"))
    if skill_bytes is not None:
        metrics.append(_metric(
            "skill_headroom", "SKILL.md 余量", SKILL_LIMIT_BYTES - skill_bytes,
            unit="bytes", note="距宿主上限的剩余字节；为负数说明已超限。"))
    else:
        metrics.append(_unavailable(
            "skill_headroom", "SKILL.md 余量", "SKILL.md 不存在，余量不可计算。"))
    metrics.append(_metric(
        "agents_md_bytes", "AGENTS.md 字节数", agents_bytes, unit="bytes",
        basis="measured" if agents_bytes is not None else "unavailable",
        note="会话启动时注入的指令文件。"
        if agents_bytes is not None else "AGENTS.md 不存在。"))
    known = [v for v in (skill_bytes, agents_bytes) if v is not None]
    if known:
        metrics.append(_metric(
            "instruction_total_bytes", "指令文件字节合计", sum(known), unit="bytes",
            note="SKILL.md + AGENTS.md 字节之和；两者注入时机不同，不能当作常驻注入量或 Token。"))
    else:
        metrics.append(_unavailable(
            "instruction_total_bytes", "指令文件字节合计", "SKILL.md 与 AGENTS.md 都不存在。"))
    return {"metrics": metrics}


# ── 任务台账 ─────────────────────────────────────────────────────────────
def _task_roots(workspaces: list[str], explicit: list[str]) -> list[str]:
    roots: list[str] = []
    for path in explicit:
        roots.append(os.path.abspath(os.path.expanduser(path)))
    for workspace in workspaces:
        roots.append(os.path.join(os.path.abspath(
            os.path.expanduser(workspace)), ".copilot-tasks"))
    return list(dict.fromkeys(roots))


def collect_tasks(roots: list[str], state_path: str = "") -> list[dict]:
    paths: list[str] = []
    if state_path:
        paths.append(os.path.abspath(os.path.expanduser(state_path)))
    for root in roots:
        paths.extend(sorted(glob.glob(os.path.join(root, "*", "state.json"))))
    rows: list[dict] = []
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        state = _read_json(path)
        if not state or not state.get("task_id"):
            continue
        rows.append(_task_row(path, state))
    return rows


def _task_row(path: str, state: dict) -> dict:
    closure = state.get("closure") if isinstance(state.get("closure"), dict) else {}
    criteria = ((state.get("contract") or {}).get("criteria") or [])
    actions = [a for a in state.get("actions") or [] if isinstance(a, dict)]
    evidence = [e for e in state.get("evidence") or [] if isinstance(e, dict)]
    calls = [c for c in state.get("sol_calls") or [] if isinstance(c, dict)]
    schedule = state.get("sol_schedule") if isinstance(
        state.get("sol_schedule"), dict) else {}
    obligations = [o for o in schedule.get("obligations") or []
                   if isinstance(o, dict)]
    created = _parse_iso(state.get("created"))
    closed_at = _parse_iso(closure.get("closed_at"))
    updated = _parse_iso(state.get("updated"))
    end = closed_at or updated
    duration_h = ((end - created).total_seconds() / 3600.0
                  if created and end and end >= created else None)
    ref_ids: list[str] = []
    for criterion in criteria:
        for ref in criterion.get("evidence_refs") or []:
            if isinstance(ref, dict) and ref.get("id"):
                ref_ids.append(str(ref["id"]))
    for action in actions:
        for eid in action.get("evidence_ids") or []:
            ref_ids.append(str(eid))
    active_blocks = [b for b in state.get("blocks") or []
                     if isinstance(b, dict) and not b.get("resolved_at")]
    tokens = 0
    usage_calls = 0
    latencies: list[float] = []
    for call in calls:
        usage = call.get("usage") if isinstance(call.get("usage"), dict) else {}
        total = usage.get("total_tokens")
        if isinstance(total, (int, float)) and total >= 0:
            tokens += int(total)
            usage_calls += 1
        latency = call.get("latency_ms")
        if isinstance(latency, (int, float)) and latency > 0:
            latencies.append(float(latency))
    acceptances = []
    for call in calls:
        acceptance = call.get("acceptance")
        if isinstance(acceptance, dict) and acceptance.get("status"):
            acceptances.append(str(acceptance["status"]))
    reopen_history = closure.get("reopen_history") or []
    return {
        "path": path,
        "task_id": str(state.get("task_id")),
        "workspace": str(state.get("workspace") or ""),
        "task_kind": str(state.get("task_kind") or ""),
        "risk": str(state.get("risk") or ""),
        "closed": bool(closure.get("closed")),
        "verdict": str(closure.get("verdict") or ""),
        "created": state.get("created") or "",
        "updated": state.get("updated") or "",
        "closed_at": closure.get("closed_at") or "",
        "duration_hours": duration_h,
        "open_stale_hours": (None if closure.get("closed") or not updated else
                             (_now() - updated).total_seconds() / 3600.0),
        "criteria_total": len(criteria),
        "criteria_met": sum(1 for c in criteria
                            if str(c.get("status")) == "met"),
        "actions_total": len(actions),
        "actions_done": sum(1 for a in actions
                            if str(a.get("status")) == "done"),
        "actions_failed": sum(1 for a in actions
                              if str(a.get("status")) == "failed"),
        "reruns": sum(int(a.get("rerun_count") or 0) for a in actions),
        "evidence_total": len(evidence),
        "evidence_dropped": sum(1 for e in evidence if e.get("dropped")),
        "evidence_refs": len(ref_ids),
        "evidence_unique": len(set(ref_ids)),
        "sol_calls": len(calls),
        "sol_tokens": tokens,
        "sol_usage_calls": usage_calls,
        "sol_latencies": latencies,
        "sol_acceptances": acceptances,
        "obligations": len(obligations),
        "obligations_open": sum(1 for o in obligations
                                if o.get("window") == "open"),
        "blocks_active": len(active_blocks),
        "reopens": len(reopen_history) if isinstance(reopen_history, list) else 0,
        "quality_verdict": str((closure.get("quality") or {}).get("verdict") or "")
        if isinstance(closure.get("quality"), dict) else "",
    }


def summarize_tasks(rows: list[dict], limit: int = 10) -> dict:
    if not rows:
        return {"metrics": [_unavailable(
            "tasks", "任务台账", "未发现可读的 .copilot-tasks/*/state.json。")],
            "recent": []}
    closed = [r for r in rows if r["closed"]]
    open_rows = [r for r in rows if not r["closed"]]
    durations = [r["duration_hours"] for r in closed
                 if r["duration_hours"] is not None]
    stale = [r for r in open_rows
             if r["open_stale_hours"] is not None and r["open_stale_hours"] >= 24]
    latencies = [value for r in rows for value in r["sol_latencies"]]
    tokens = sum(r["sol_tokens"] for r in rows)
    usage_calls = sum(r["sol_usage_calls"] for r in rows)
    calls = sum(r["sol_calls"] for r in rows)
    acceptances = [status for r in rows for status in r["sol_acceptances"]]
    accepted = sum(1 for s in acceptances if s in (
        "accepted", "modified", "rejected"))
    pending = sum(1 for s in acceptances if s == "pending")
    refs = sum(r["evidence_refs"] for r in rows)
    unique = sum(r["evidence_unique"] for r in rows)
    dropped = sum(r["evidence_dropped"] for r in rows)
    total_evidence = sum(r["evidence_total"] for r in rows)
    metrics = [
        _metric("tasks_total", "任务总数", len(rows), unit="tasks"),
        _metric("tasks_closed", "已收口任务", len(closed), unit="tasks"),
        _metric("tasks_open", "未收口任务", len(open_rows), unit="tasks"),
        _metric("tasks_open_stale_24h", "未收口且 24h 无更新", len(stale),
                unit="tasks", basis="proxy",
                note="代理指标：可能只是等待用户授权或外部条件，不等于早停。"),
        _metric("tasks_closed_pass", "收口 verdict=pass", sum(
            1 for r in closed if r["verdict"] == "pass"), unit="tasks"),
        _metric("tasks_with_reopen", "发生重开的任务", sum(
            1 for r in rows if r["reopens"]), unit="tasks"),
        _metric("duration_p50", "收口任务时长 P50",
                round(_percentile(durations, 0.5), 2) if durations else None,
                unit="hours", note="按台账 created→closed_at 计算；"
                "不是模型/工具耗时。"),
        _metric("duration_p95", "收口任务时长 P95",
                round(_percentile(durations, 0.95), 2) if durations else None,
                unit="hours", note="同 duration_p50。"),
        _metric("criteria_met", "完成条件已核销",
                sum(r["criteria_met"] for r in rows), unit="criteria"),
        _metric("criteria_total", "完成条件总数",
                sum(r["criteria_total"] for r in rows), unit="criteria"),
        _metric("actions_total", "工作项总数",
                sum(r["actions_total"] for r in rows), unit="items"),
        _metric("actions_failed", "工作项失败次数",
                sum(r["actions_failed"] for r in rows), unit="items"),
        _metric("action_reruns", "工作项重开次数", sum(
            r["reruns"] for r in rows), unit="times",
            note="代理指标：重开可能是真实返工，也可能是范围调整。"),
        _metric("evidence_total", "证据总数", total_evidence, unit="files"),
        _metric("evidence_dropped", "已丢弃证据", dropped, unit="files",
                note="被新快照取代或失效的证据；不进入收口引用。"),
        _metric("evidence_refs", "证据引用次数", refs, unit="refs"),
        _metric("evidence_reuse_ratio", "证据复用率",
                round((refs - unique) / refs, 3) if refs else None,
                basis="proxy",
                note="同一证据被多个条件/工作项引用；高不等于验证充分。"),
        _metric("sol_calls", "SOL 调用总数", calls, unit="calls"),
        _metric("sol_tokens", "SOL Token 合计", tokens if usage_calls else None,
                unit="tokens", note="" if usage_calls == calls else
                f"仅 {usage_calls}/{calls} 次调用有 usage 记录。"),
        _metric("sol_latency_p50", "SOL 延迟 P50",
                round(_percentile(latencies, 0.5), 1) if latencies else None,
                unit="ms"),
        _metric("sol_latency_p95", "SOL 延迟 P95",
                round(_percentile(latencies, 0.95), 1) if latencies else None,
                unit="ms"),
        _metric("sol_acceptance_recorded", "已回填采纳的调用", accepted,
                unit="calls", note="accepted/modified/rejected 的合计。"),
        _metric("sol_acceptance_pending", "仍 pending 的采纳", pending,
                unit="calls", note="观察值，不阻断收口。"),
        _metric("obligations_open", "仍打开的调度义务",
                sum(r["obligations_open"] for r in rows), unit="obligations",
                note="跨未收口任务；收口后应结算为终态处置。"),
        _metric("blocks_active", "当前活动阻塞",
                sum(r["blocks_active"] for r in rows), unit="blocks"),
        _unavailable("urging_count", "用户催促次数",
                     "需要读取对话文本；本工具默认不读消息内容，"
                     "以免引入隐私与偏差。"),
        _unavailable("early_stop_count", "早停次数",
                     "没有独立日志；tasks_open_stale_24h 只是代理信号。"),
        _unavailable("duplicate_test_runs", "重复测试命令数",
                     "任务台账只记结果不记命令；统计需解析工具输入，"
                     "当前不纳入默认口径。"),
        _unavailable("compaction_count", "上下文压缩次数",
                     "宿主未在任务台账记录；只在 --host-db 下可聚合"
                     " turn_usage.context_exceeded（不是压缩次数）。"),
    ]
    recent = sorted(rows, key=lambda r: r["updated"] or "", reverse=True)[:limit]
    return {"metrics": metrics, "recent": [
        {key: row[key] for key in (
            "task_id", "workspace", "closed", "verdict", "duration_hours",
            "criteria_met", "criteria_total", "sol_calls", "sol_tokens",
            "evidence_total", "reopens")} for row in recent]}


# ── 自动召回事件 ─────────────────────────────────────────────────────────
def summarize_recall(path: str) -> dict:
    target = os.path.abspath(os.path.expanduser(path))
    rows = _read_jsonl(target)
    if not rows:
        return {"path": target, "metrics": [_unavailable(
            "recall_events", "自动召回事件",
            f"未找到可读事件文件：{target}（未启用或尚未发生召回）。")]}
    statuses: dict[str, int] = {}
    decisions: dict[str, int] = {}
    chars: list[float] = []
    elapsed: list[float] = []
    for row in rows:
        status = str(row.get("status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
        decision = str(row.get("decision") or "unknown")
        decisions[decision] = decisions.get(decision, 0) + 1
        for key in ("chars", "display_chars", "injected_chars"):
            value = row.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                chars.append(float(value))
                break
        value = row.get("elapsed_ms")
        if isinstance(value, (int, float)) and value >= 0:
            elapsed.append(float(value))
    injected = decisions.get("injected", 0)
    no_hits = decisions.get("no-hits", 0)
    metrics = [
        _metric("recall_events", "召回事件数", len(rows), unit="events",
                note=f"来源：{target}"),
        _metric("recall_injected", "已注入上下文", injected, unit="events",
                note="decision=injected；包含展示预算耗尽后仍注入的情况。"),
        _metric("recall_no_hits", "零命中", no_hits, unit="events"),
        _metric("recall_no_hits_budget", "零命中且检索预算耗尽",
                sum(1 for row in rows
                    if row.get("decision") == "no-hits"
                    and row.get("status") == "budget_exhausted"),
                unit="events", basis="proxy",
                note="可能是检索预算不足导致的漏召回；需人工抽样复核，"
                "不是确定的失败。"),
        _metric("recall_failed", "召回失败",
                statuses.get("error", 0) + statuses.get("failed", 0),
                unit="events"),
        _metric("recall_chars_p50", "注入字符 P50",
                round(_percentile(chars, 0.5), 0) if chars else None,
                unit="chars"),
        _metric("recall_elapsed_p50", "召回耗时 P50",
                round(_percentile(elapsed, 0.5), 1) if elapsed else None,
                unit="ms"),
    ]
    return {"path": target, "metrics": metrics}


# ── 宿主库只读聚合（可选） ───────────────────────────────────────────────
def summarize_host_db(path: str, days: int) -> dict:
    target = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(target):
        return {"path": target, "metrics": [_unavailable(
            "host_db", "宿主库聚合", f"宿主库不存在：{target}")]}
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=5)
    except Exception as exc:  # noqa: BLE001  只读打开失败按 unavailable
        return {"path": target, "metrics": [_unavailable(
            "host_db", "宿主库聚合", f"无法只读打开宿主库：{type(exc).__name__}")]}
    since_ms = int((_now() - timedelta(days=max(1, days))).timestamp() * 1000)
    metrics: list[dict] = []
    try:
        row = conn.execute(
            "select count(*), coalesce(sum(model_request_count),0),"
            " coalesce(sum(tool_call_count),0),"
            " coalesce(sum(tool_error_count),0),"
            " coalesce(sum(computed_total_tokens),0),"
            " coalesce(sum(cache_read_input_tokens),0),"
            " coalesce(sum(case when context_exceeded=1 then 1 else 0 end),0)"
            " from turn_usage where started_at >= ?", (since_ms,)).fetchone()
    except Exception as exc:  # noqa: BLE001
        conn.close()
        return {"path": target, "metrics": [_unavailable(
            "host_db", "宿主库聚合", f"只读聚合失败：{type(exc).__name__}")]}
    turns, requests, tools, tool_errors, tokens, cache_read, exceeded = row
    metrics.extend([
        _metric("host_turns", f"宿主回合数（{days} 天）", turns, unit="turns",
                note=f"来源：{target}（只读）"),
        _metric("host_model_requests", "模型请求数", requests, unit="requests"),
        _metric("host_tool_calls", "工具调用数", tools, unit="calls"),
        _metric("host_tool_errors", "工具错误数", tool_errors, unit="calls"),
        _metric("host_tokens", "Token 合计（含缓存读）", tokens + cache_read,
                unit="tokens", note="computed_total_tokens + cache_read_input_tokens。"),
        _metric("host_cache_read", "缓存读 Token", cache_read, unit="tokens"),
        _metric("host_context_exceeded", "上下文超限回合", exceeded,
                unit="turns"),
    ])
    try:
        duration, p95 = conn.execute(
            "select avg(duration_ms),"
            " (select duration_ms from tool_usage where started_at >= ?"
            "  order by duration_ms limit 1 offset max(0,"
            "   (select count(*)*19/20 from tool_usage where started_at >= ?)-1))"
            " from tool_usage where started_at >= ?",
            (since_ms, since_ms, since_ms)).fetchone()
    except Exception:  # noqa: BLE001  统计口径失败不影响主指标
        duration, p95 = None, None
    metrics.extend([
        _metric("host_tool_duration_avg", "工具耗时均值",
                round(duration, 1) if isinstance(duration, (int, float)) else None,
                unit="ms"),
        _metric("host_tool_duration_p95", "工具耗时 P95（近似）",
                round(p95, 1) if isinstance(p95, (int, float)) else None,
                unit="ms", basis="proxy",
                note="按排序偏移近似分位，避免一次性拉取全部行。"),
    ])
    try:
        by_model = conn.execute(
            "select model_id, count(*), coalesce(sum(computed_total_tokens),0)"
            " from model_usage where started_at >= ?"
            " group by model_id order by 2 desc limit 5",
            (since_ms,)).fetchall()
    except Exception:  # noqa: BLE001
        by_model = []
    conn.close()
    return {"path": target, "metrics": metrics, "models": [
        {"model": str(row[0]), "requests": int(row[1]),
         "tokens": int(row[2])} for row in by_model]}


# ── 组装与输出 ───────────────────────────────────────────────────────────
def build_report(*, workspaces: list[str], tasks_roots: list[str] | None = None,
                 state_path: str = "", repo_root: str = "",
                 recall_events: str = DEFAULT_RECALL,
                 host_db: str = "", days: int = 7,
                 limit: int = 10) -> dict:
    roots = _task_roots(workspaces, tasks_roots or [])
    rows = collect_tasks(roots, state_path=state_path)
    report = {
        "schema": SCHEMA,
        "generated_at": _now().astimezone().isoformat(timespec="seconds"),
        "sources": {
            "task_roots": roots,
            "state_path": os.path.abspath(state_path) if state_path else "",
            "recall_events": os.path.abspath(
                os.path.expanduser(recall_events)),
            "host_db": os.path.abspath(os.path.expanduser(host_db))
            if host_db else "",
        },
        "basis_legend": {
            "measured": "台账/文件/库中的直接计数或简单派生",
            "proxy": "间接代理，不能单独证明结论",
            "unavailable": "当前没有可靠来源，不猜测",
        },
        "instruction": instruction_surface(repo_root or os.getcwd()),
        "tasks": summarize_tasks(rows, limit=limit),
        "recall": summarize_recall(recall_events),
        "host_db": summarize_host_db(host_db, days) if host_db else {
            "path": "", "metrics": [_unavailable(
                "host_db", "宿主库聚合", "未使用 --host-db；默认不扫描宿主库。")]},
    }
    report["metrics"] = [m for section in (
        "instruction", "tasks", "recall", "host_db")
        for m in report[section]["metrics"]]
    return report


def render(report: dict) -> str:
    lines = [f"Copilot 效能观测（{report['schema']}）",
             f"生成时间：{report['generated_at']}",
             "口径：measured=直接事实；proxy=代理；unavailable=无可靠来源。", ""]
    for section, title in (("instruction", "指令注入面"),
                           ("tasks", "任务台账"),
                           ("recall", "自动召回"),
                           ("host_db", "宿主库（只读聚合）")):
        lines.append(f"## {title}")
        for metric in report[section]["metrics"]:
            value = metric["value"]
            if value is None:
                text = "-"
            else:
                text = f"{value}{metric['unit']}" if metric["unit"] else str(value)
            suffix = f"  [{metric['basis']}]"
            note = f"  # {metric['note']}" if metric["note"] else ""
            lines.append(f"- {metric['label']}：{text}{suffix}{note}")
        lines.append("")
    recent = report["tasks"].get("recent") or []
    if recent:
        lines.append("## 最近任务")
        for row in recent:
            state = "CLOSED" if row["closed"] else "OPEN"
            lines.append(
                f"- {row['task_id']} [{state}/{row['verdict'] or '-'}] "
                f"{row['workspace']} · 条件 {row['criteria_met']}/"
                f"{row['criteria_total']} · SOL {row['sol_calls']} 次/"
                f"{row['sol_tokens']} tokens · 证据 {row['evidence_total']}"
                f" · 重开 {row['reopens']}")
        lines.append("")
    lines.append("说明：本报告只用于观测，不构成门禁、完成证明或质量结论。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读开发效能观测（不写状态、不调用模型、不是门禁）")
    parser.add_argument("--workspace", action="append", default=[],
                        help="扫描 <workspace>/.copilot-tasks（可重复）")
    parser.add_argument("--tasks-root", action="append", default=[],
                        help="直接指定包含任务目录的根（可重复）")
    parser.add_argument("--state", default="", help="只统计单个任务 state.json")
    parser.add_argument("--repo-root", default="",
                        help="SKILL.md/AGENTS.md 所在仓库根（默认当前目录）")
    parser.add_argument("--recall-events", default=DEFAULT_RECALL,
                        help=f"召回事件 JSONL（默认 {DEFAULT_RECALL}）")
    parser.add_argument("--host-db", default="",
                        help="可选：只读聚合宿主库（默认不扫描）")
    parser.add_argument("--days", type=int, default=7,
                        help="宿主库聚合窗口天数（默认 7）")
    parser.add_argument("--limit", type=int, default=10,
                        help="最近任务条数（默认 10）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)
    if not args.workspace and not args.tasks_root and not args.state:
        args.workspace = [os.getcwd()]
    try:
        report = build_report(
            workspaces=args.workspace, tasks_roots=args.tasks_root,
            state_path=args.state, repo_root=args.repo_root,
            recall_events=args.recall_events, host_db=args.host_db,
            days=args.days, limit=args.limit)
    except Exception as exc:  # noqa: BLE001
        print(f"efficiency_report: 内部错误：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

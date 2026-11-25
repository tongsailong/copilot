#!/usr/bin/env python3
"""认知生命周期调度：调度义务、窗口事实、自动材料与覆盖率统计。

本模块只做**确定性策略与投影**，三件事绝对不做：

- 不发网络请求、不导入任何网络客户端（认知通道只存在于 `cognition_call.py`）；
- 不读写状态文件（调用方传入 dict，调用方负责落盘）；
- 不判定工程完成、不改变阶段、不影响 G1–G3。

设计要点（与认知 CONSULT/PLAN 的结论一致）：

1. **义务 vs 复用是两把键**。义务键标识“在哪个生命周期窗口欠了一次认知判断”
   （任务、合同指纹、family、事件、发生实例）；复用键仍由 `cognition_call` 的
   subject 摘要决定“同一份材料与问题是否可以不再付费”。一个义务可以被真实调用
   满足、被复用满足、被显式跳过，或因为窗口关闭而成为漏调。
2. **窗口只按已持久化的事实关闭**。窗口在任务收口、或被更晚窗口的“已满足”取代时
   关闭；不在当前状态上倒推历史，也不把“待执行”当成“已放弃”。
3. **漏调是派生的，不是写死的**。`due` / `claimed` 状态在窗口关闭时即为漏调；
   `in_flight`（已发出请求但未落盘）单列为未决，不冒充成功也不冒充漏调。
4. **命令必须可直接执行**。建议命令一律由 `runnable_command()` 生成完整路径与
   阶段，材料缺省时由 `build_materials()` 从状态生成，不留给调用方补占位符。
"""
from __future__ import annotations

import json
import os
import shlex

import review_policy as RP

OBLIGATION_SCHEMA = "copilot-cognition-obligation/v1"
SCHEDULE_SCHEMA = "copilot-cognition-schedule/v1"
STATS_SCHEMA = "copilot-cognition-stats/v2"
# 材料口径版本。v2：合同正文改读 `contract["done_criteria_text"]`（`body` 兼容回退），
# 正文缺失时显式标注“（未登记）”。该版本参与 `closure_verification_digest`，所以
# 材料内容变化会让旧收口判断停止覆盖新结果，而不是靠“代码改过了”想当然。
MATERIALS_SCHEMA = "copilot-cognition-schedule-materials/v3"

# 义务状态：due 未执行 / claimed 已被本进程领取 / in_flight 已发出请求未落盘 /
# satisfied 已满足（含复用）/ failed 真实失败 / skipped 显式跳过（须有理由）。
OBLIGATION_STATUSES = ("due", "claimed", "in_flight", "satisfied", "failed",
                       "skipped")
# 窗口关闭时这些状态构成漏调：领取过但从未发出请求与“一直没动”等价。
MISSED_STATUSES = ("due", "claimed")
# 终态处置：窗口关闭后每个义务都必须落到一个可复核的结果。处置是**派生的观察
# 事实**，不是完成证明、不是门禁：completed 只说“窗口有结论”，covered/skipped
# 只说“不需要独立调用”并带可查理由，not-executed 只说“关闭时仍未执行”。
DISPOSITION_KINDS = ("completed", "covered", "skipped", "failed",
                     "not-executed", "unsettled")
FAMILY_INDEX = {name: index for index, name in enumerate(RP.COGNITION_FAMILIES)}
DEFAULT_MATERIALS_CHARS = 24_000
# 每周期保留的策略变更历史条数上限：只用于解释“窗口打开时是什么策略”，
# 不参与调度判定，超出上限丢弃最旧记录（不是审计台账）。
POLICY_HISTORY_LIMIT = 50
# 自动调度路径等待任务认知锁的上限（秒）：拿不到锁说明另一个进程正在调用，
# 本次放弃执行、义务保持 due，而不是无限阻塞工程命令。手动调用不设默认上限；
# `COPILOT_COGNITION_LOCK_TIMEOUT` 可覆盖。自动路径的“有界”是双重的：1 条义务、
# 1 次请求，以及这条锁等待上限（不是请求本身的时长上限）。
AUTO_LOCK_WAIT_S = 120.0


def _text(value: object) -> str:
    return str(value or "").strip()


def _contract_body(state: dict) -> str:
    """合同正文的唯一来源：`done_criteria_text`（与合同指纹同源）。

    `body` 不是状态里的键；保留它只作为旧状态的兼容回退。
    """
    contract = state.get("contract") or {}
    if not isinstance(contract, dict):
        return ""
    return _text(contract.get("done_criteria_text")) or _text(contract.get("body"))


def _sha(value: object) -> str:
    return RP.sha256_text(RP.canonical_json(value))


def family_index(family: str) -> int:
    return FAMILY_INDEX.get(_text(family), len(FAMILY_INDEX))


# ── 义务模型 ───────────────────────────────────────────────────────────────
# v9.10.0 之前的任务只有 `sol_schedule`：只读回退，不迁移、不回写。
LEGACY_SCHEDULE_KEY = "sol_schedule"


def schedule_raw(state: dict) -> dict | None:
    """只读取调度结构：当前键优先，旧任务回退到 `sol_schedule`。

    两个键同时存在时以 `cognition_schedule` 为准（v9.10.0 起新写入只落在该键；
    旧键里的同名义务是历史快照，不参与当前判定，也不删除）。
    """
    for key in ("cognition_schedule", LEGACY_SCHEDULE_KEY):
        raw = state.get(key)
        if isinstance(raw, dict):
            return raw
    return None


def empty_schedule() -> dict:
    return {"schema": SCHEDULE_SCHEMA, "next_id": 1, "obligations": []}


def ensure_schedule(state: dict) -> dict:
    """返回可写的调度结构；旧状态没有该字段时按空调度处理（不落盘）。"""
    raw = state.get("cognition_schedule")
    if not isinstance(raw, dict):
        raw = empty_schedule()
        state["cognition_schedule"] = raw
    raw.setdefault("schema", SCHEDULE_SCHEMA)
    if not isinstance(raw.get("obligations"), list):
        raw["obligations"] = []
    try:
        next_id = int(raw.get("next_id") or 1)
    except (TypeError, ValueError):
        next_id = 1
    raw["next_id"] = max(1, next_id)
    return raw


def obligations(state: dict) -> list[dict]:
    """只读视图：旧状态没有调度字段时返回空列表，不在查询路径创建字段。"""
    raw = schedule_raw(state)
    if not isinstance(raw, dict):
        return []
    items = raw.get("obligations")
    return items if isinstance(items, list) else []


def schedule_view(state: dict) -> dict | None:
    """只读调度结构（不创建）：用于读取策略快照等旁证。"""
    return schedule_raw(state)


def obligation_key(state: dict, family: str, event: str, instance: str) -> str:
    """义务键：任务 + 合同指纹 + family + 事件 + 发生实例。

    同一认知窗口在重复求值时只会刷新同一个义务；合同修订会改变指纹，因此旧合同
    下的义务不会被新合同“继承”，但也不会被改写或删除（审计保留）。
    """
    return _sha([_text(state.get("task_id")), _text(state.get("contract_sha256")),
                 _text(family), _text(event), _text(instance)])[:24]


def _next_obligation_id(schedule: dict) -> str:
    used = {_text(item.get("obligation_id"))
            for item in schedule["obligations"] if isinstance(item, dict)}
    index = int(schedule.get("next_id") or 1)
    while f"SO{index}" in used:
        index += 1
    schedule["next_id"] = index + 1
    return f"SO{index}"


def _new_obligation(state: dict, advice: dict, *, now: str) -> dict:
    family = _text(advice.get("family"))
    event = _text(advice.get("event"))
    instance = _text(advice.get("instance"))
    return {
        "schema": OBLIGATION_SCHEMA,
        "obligation_id": "",
        "key": obligation_key(state, family, event, instance),
        "task_id": _text(state.get("task_id")),
        "contract_sha256": _text(state.get("contract_sha256")),
        "family": family,
        "mode": _text(advice.get("mode")),
        "stage": _text(advice.get("stage")),
        "event": event,
        "instance": instance,
        "level": _text(advice.get("level")) or "recommended",
        "question": _text(advice.get("question")),
        "reason": _text(advice.get("reason")),
        # 收口周期：重开后新建的义务属于新周期，旧义务保持原周期与原始事实。
        "cycle": current_cycle(state),
        "opened_at": now,
        "window": "open",
        "closed_at": "",
        "closed_reason": "",
        "status": "due",
        "attempts": 0,
        "source": "",
        "call_id": "",
        "reuse_of": "",
        "skip_reason": "",
        "skip_by": "",
        "failure": "",
        "started_at": "",
        "finished_at": "",
    }


def find_obligation(state: dict, *, family: str = "", event: str = "",
                    instance: str = "", obligation_id: str = "") -> dict | None:
    for item in obligations(state):
        if not isinstance(item, dict):
            continue
        if obligation_id and _text(item.get("obligation_id")) == obligation_id:
            return item
        if family and _text(item.get("family")) != family:
            continue
        if _text(item.get("event")) != _text(event):
            continue
        if _text(item.get("instance")) != _text(instance):
            continue
        if family:
            return item
    return None


def _refresh(obligation: dict, advice: dict, *, now: str) -> bool:
    """刷新同一义务的建议内容；不改变已有状态与窗口事实。"""
    changed = False
    for field, source in (("question", "question"), ("reason", "reason"),
                          ("level", "level"), ("mode", "mode")):
        value = _text(advice.get(source))
        if value and obligation.get(field) != value:
            obligation[field] = value
            changed = True
    if changed:
        obligation["refreshed_at"] = now
    return changed


def due_obligations(state: dict, *, recommended_only: bool = True) -> list[dict]:
    """当前可执行的义务，按窗口先后与打开时间排序（先到先得，不被新事件抢占）。"""
    rows = [item for item in obligations(state)
            if isinstance(item, dict)
            and item.get("window") == "open"
            and _text(item.get("status")) == "due"
            and _text(item.get("mode")) in ("CONSULT", "PLAN")]
    if recommended_only:
        rows = [item for item in rows
                if _text(item.get("level")) == "recommended"]
    rows.sort(key=lambda item: (family_index(_text(item.get("family"))),
                                _text(item.get("opened_at")),
                                _text(item.get("obligation_id"))))
    return rows


def is_missed(obligation: dict) -> bool:
    return (obligation.get("window") == "closed"
            and _text(obligation.get("status")) in MISSED_STATUSES)


def is_unresolved_in_flight(obligation: dict) -> bool:
    return (obligation.get("window") == "closed"
            and _text(obligation.get("status")) == "in_flight")


# ── 收口周期与终态处置 ────────────────────────────────────────────────────
def current_cycle(state: dict) -> int:
    """收口周期编号：1 + 已授权重开次数。

    重开产生新周期；旧周期的义务保持原样（不改写、不重算、不删除），新周期的
    义务带新编号，避免把“重开前的漏调”与“重开后的进展”混成一个统计。
    """
    closure = state.get("closure")
    history = closure.get("reopen_history") if isinstance(closure, dict) else None
    return 1 + (len(history) if isinstance(history, list) else 0)


def obligation_cycle(obligation: dict) -> int:
    """义务所属周期；历史义务没有该字段时按第 1 个周期处理（不落盘改写）。"""
    try:
        value = int(obligation.get("cycle") or 1)
    except (TypeError, ValueError):
        value = 1
    return max(1, value)


def policy_snapshot(schedule: dict | None, cycle: int) -> dict:
    """读取该周期的调度策略快照（`off` / `advise` / `auto`）。

    历史上没有快照的义务返回空：不能拿“现在的配置”倒推“当时为什么没调用”。
    """
    if not isinstance(schedule, dict):
        return {}
    store = schedule.get("policy_snapshots")
    if not isinstance(store, dict):
        return {}
    entry = store.get(str(int(cycle)))
    return dict(entry) if isinstance(entry, dict) else {}


def note_policy(schedule: dict, *, policy: str, auto_execution: bool | None,
                now: str, cycle: int) -> bool:
    """记录本周期的实际调度策略，供事后解释“为什么没有调用”。

    只记录事实（策略、自动执行是否允许），不据此推断历史，也不改变任何义务状态。
    """
    value = _text(policy).lower()
    if not value:
        return False
    store = schedule.get("policy_snapshots")
    if not isinstance(store, dict):
        store = {}
        schedule["policy_snapshots"] = store
    key = str(int(cycle))
    entry = dict(store.get(key) or {})
    auto = None if auto_execution is None else bool(auto_execution)
    if _text(entry.get("value")) == value and entry.get("auto_execution") == auto:
        return False
    entry["first_seen_at"] = _text(entry.get("first_seen_at")) or now
    entry["value"] = value
    entry["auto_execution"] = auto
    entry["at"] = now
    # 策略变更历史：只追加“当时生效了什么”，供义务窗口打开时点回溯。没有它，
    # 事后只能用周期内最后一次策略解释更早打开的窗口（会把 advise 下的漏调
    # 误标成 auto）。
    history = entry.get("history")
    history = list(history) if isinstance(history, list) else []
    history.append({"value": value, "auto_execution": auto, "at": now})
    entry["history"] = history[-POLICY_HISTORY_LIMIT:]
    store[key] = entry
    return True


def note_deferral(obligation: dict, *, now: str, reason: str) -> None:
    """记录“条件成立但本次未能调度”（例如认知锁被占用、参数前置不成立）。

    只计数并留因：义务仍保持 due（调度失败不等于不该调用），也不结算成失败；
    窗口关闭时这条事实会写进终态处置，不会被静默清除。
    """
    if not isinstance(obligation, dict):
        return
    obligation["deferrals"] = int(obligation.get("deferrals") or 0) + 1
    obligation["last_deferral_reason"] = str(reason or "")[:500]
    obligation["last_deferred_at"] = now


def _policy_at_open(obligation: dict, schedule: dict | None) -> dict:
    """推导义务窗口打开时实际生效的调度策略（只读，不写状态）。

    优先用义务自带的 `policy_at_open`（新义务在 `sync` 内记录）。历史义务回退到
    周期策略快照的变更历史：取打开时刻及之前最后一次变更；快照本身不晚于打开
    时刻时它就是打开时的策略。无法确定时返回空 dict（记 unknown），绝不用当前
    配置倒推。
    """
    recorded = obligation.get("policy_at_open")
    if isinstance(recorded, dict) and _text(recorded.get("value")):
        return dict(recorded)
    opened_at = _text(obligation.get("opened_at"))
    snapshot = policy_snapshot(schedule, obligation_cycle(obligation))
    if not _text(snapshot.get("value")):
        return {}
    history = snapshot.get("history")
    best: dict = {}
    if isinstance(history, list):
        for item in sorted(
                (row for row in history if isinstance(row, dict)),
                key=lambda row: _text(row.get("at"))):
            if not _text(item.get("value")):
                continue
            at = _text(item.get("at"))
            if opened_at and at and at > opened_at:
                continue
            best = dict(item)
    if best:
        return best
    at = _text(snapshot.get("at"))
    if not opened_at or not at or at <= opened_at:
        return {"value": _text(snapshot.get("value")),
                "auto_execution": snapshot.get("auto_execution"),
                "at": at}
    return {}


def _policy_suffix(policy: dict) -> str:
    """策略值的可读后缀：明确写“允许/不自动执行”，避免只凭 auto 猜测。"""
    if policy.get("auto_execution") is True:
        return "（允许自动执行）"
    if policy.get("auto_execution") is False:
        return "（不自动执行）"
    return ""


def _disposition_facts(obligation: dict, schedule: dict | None) -> tuple[str, str]:
    """按已持久化的事实推导（终态, 理由）；没有终态时返回 ("", "")。"""
    status = _text(obligation.get("status"))
    closed_reason = _text(obligation.get("closed_reason")) or status
    if status == "satisfied":
        if _text(obligation.get("source")) == "reuse":
            return "completed", ("同一认知对象的既有结论复用满足（"
                                 + (_text(obligation.get("reuse_of"))
                                    or "subject-reuse") + "）")
        mode = _text(obligation.get("mode")) or "认知"
        call_id = _text(obligation.get("call_id"))
        return "completed", (f"已执行 {mode} 调用"
                             + (f"（{call_id}）" if call_id else "（未记 call_id）"))
    if status == "failed":
        detail = _text(obligation.get("failure")) or _text(obligation.get("result_note"))
        return "failed", (f"执行失败：{detail}" if detail
                          else f"执行失败（{closed_reason}）")
    if status == "skipped":
        reason = _text(obligation.get("skip_reason"))
        if _text(obligation.get("skip_by")) == "closure-covered":
            return "covered", reason or "收口综合判断已覆盖该窗口的认知对象"
        return "skipped", reason or f"显式跳过（{closed_reason}）"
    if status in MISSED_STATUSES:
        parts = [f"窗口关闭（{closed_reason}）时仍未执行"]
        opened = _policy_at_open(obligation, schedule)
        opened_value = _text(opened.get("value"))
        if opened_value:
            parts.append(f"当时调度策略={opened_value}" + _policy_suffix(opened))
        else:
            parts.append("当时调度策略未记录（unknown）")
        # 周期内策略后来变过：如实补记，不改变“打开时”的口径，也不让事后看到的
        # 最新配置冒充窗口打开时的策略。
        latest = policy_snapshot(schedule, obligation_cycle(obligation))
        latest_value = _text(latest.get("value"))
        if latest_value and latest_value != opened_value:
            at = _text(latest.get("at"))
            if opened_value:
                parts.append(f"周期内调度策略后改为={latest_value}"
                             + _policy_suffix(latest)
                             + (f"（{at}）" if at else ""))
            else:
                parts.append(f"周期内记录的调度策略={latest_value}"
                             + _policy_suffix(latest)
                             + (f"（{at}）" if at else ""))
        deferrals = int(obligation.get("deferrals") or 0)
        if deferrals:
            parts.append(f"周期内 {deferrals} 次未能调度，最后一次："
                         f"{_text(obligation.get('last_deferral_reason')) or '未记原因'}")
        return "not-executed", "；".join(parts)
    if status == "in_flight":
        return "unsettled", "已发出请求但未落盘：结果未决（既不算完成也不算漏调）"
    return "", ""


def obligation_disposition(obligation: dict, schedule: dict | None = None) -> dict:
    """只读视图：优先用持久化处置；历史义务按窗口事实现算，不改状态。

    历史任务没有 `disposition` 字段也可以得到终态：推导只读当前义务与策略快照，
    不需要回写历史状态，也不需要重跑任何命令。
    """
    stored = _text(obligation.get("disposition"))
    if stored in DISPOSITION_KINDS:
        return {"kind": stored,
                "reason": _text(obligation.get("disposition_reason")),
                "at": _text(obligation.get("disposition_at")),
                "stored": True}
    if obligation.get("window") != "closed":
        return {"kind": "", "reason": "", "at": "", "stored": False}
    kind, reason = _disposition_facts(obligation, schedule)
    return {"kind": kind, "reason": reason,
            "at": _text(obligation.get("closed_at")), "stored": False}


def _stamp_disposition(obligation: dict, *, now: str,
                       schedule: dict | None = None) -> bool:
    """窗口关闭时落一次终态处置；已写过的不覆盖（只增不改）。"""
    if _text(obligation.get("disposition")) in DISPOSITION_KINDS:
        return False
    if obligation.get("window") != "closed":
        return False
    kind, reason = _disposition_facts(obligation, schedule)
    if not kind:
        return False
    obligation["disposition"] = kind
    obligation["disposition_reason"] = reason[:500]
    obligation["disposition_at"] = now
    return True


def settle(obligation: dict, *, status: str, now: str, reason: str = "",
           schedule: dict | None = None) -> None:
    """结算义务：状态一旦落定（成功/失败/跳过/复用），窗口随即关闭。

    结算不是完成证明，只是“这个认知窗口已经有结果”。不关闭窗口会让已执行义务
    永远停在 open，收口时被误算成“关闭前仍未执行”，也会掩盖后续漏调的窗口边界。
    结算同时写一次终态处置（`schedule` 只用于读取当时策略快照）。
    """
    obligation["status"] = status
    if obligation.get("window") == "closed":
        _stamp_disposition(obligation, now=now, schedule=schedule)
        return
    obligation["window"] = "closed"
    obligation["closed_at"] = now
    obligation["closed_reason"] = str(reason or status)
    _stamp_disposition(obligation, now=now, schedule=schedule)


# ── 同步：把生命周期建议物化为义务、按事实关闭窗口 ────────────────────────
def _close(schedule: dict, obligation: dict, *, now: str, reason: str) -> None:
    if obligation.get("window") == "closed":
        return
    obligation["window"] = "closed"
    obligation["closed_at"] = now
    obligation["closed_reason"] = reason
    _stamp_disposition(obligation, now=now, schedule=schedule)


def _advance_windows(schedule: dict, *, now: str) -> None:
    """更晚窗口已经“满足”时，关闭更早仍未满足的窗口（漏调因此可见）。

    只在确实有更晚窗口成功执行/复用后才关闭：刚创建、被提示但未执行、预算耗尽
    都不关闭窗口，避免把“还没来得及做”误判成漏调。比较只在同一收口周期内进行：
    重开后的收口判断覆盖的是重开后的认知对象，不能追溯性地宣布上一周期的窗口
    已被覆盖。
    """
    satisfied = [item for item in schedule["obligations"]
                 if isinstance(item, dict) and item.get("window") == "closed"
                 and _text(item.get("status")) == "satisfied"]
    if not satisfied:
        return
    # 收口综合判断覆盖了规划比较（B）与里程碑复盘（D）的认知对象：目标达成、
    # 方案是否仍成立、遗漏与下一阶段方向都已在收口判断里综合。**只要同周期的
    # E 窗口已满足就成立**，不能用“最早已满足窗口”的门槛去判断——启动 CONSULT
    # （A）几乎总在该周期内先满足，用 min() 会让这条覆盖永远不触发，最后一批
    # 进展刚打开 B/D 就收口时会白跑一次调用。
    closure_cycles = {obligation_cycle(item) for item in satisfied
                      if family_index(_text(item.get("family")))
                      == family_index("E")}
    frontiers: dict[int, int] = {}
    for item in satisfied:
        cycle = obligation_cycle(item)
        index = family_index(_text(item.get("family")))
        frontiers[cycle] = min(index, frontiers.get(cycle, index))
    for item in schedule["obligations"]:
        if not isinstance(item, dict) or item.get("window") != "open":
            continue
        cycle = obligation_cycle(item)
        family = family_index(_text(item.get("family")))
        if cycle in closure_cycles and _text(item.get("family")) in ("B", "D"):
            item["status"] = "skipped"
            item["skip_by"] = "closure-covered"
            item["skip_reason"] = "收口综合判断已覆盖该窗口的认知对象"
            item["finished_at"] = now
            _close(schedule, item, now=now, reason="closure-covered")
            continue
        frontier = frontiers.get(cycle)
        if frontier is None or family >= frontier:
            continue
        _close(schedule, item, now=now, reason="superseded")


def _expire_superseded(schedule: dict, state: dict, *, now: str) -> list[str]:
    """合同修订后，旧合同下未执行的义务不再适用：显式记为跳过（带原因）。

    不静默删除、也不改写成漏调：旧合同的“应调用”事实保留在审计里，但不进入新
    合同的覆盖率分母（分母只统计当前合同下的义务）。
    """
    current = _text(state.get("contract_sha256"))
    changes: list[str] = []
    for item in schedule["obligations"]:
        if not isinstance(item, dict) or item.get("window") != "open":
            continue
        recorded = _text(item.get("contract_sha256"))
        # 缺少合同指纹的历史义务按“覆盖当前合同”处理（宁可不重复付费），
        # 只有明确记录了旧合同指纹的才随修订失效。
        if not recorded or recorded == current:
            continue
        item["status"] = "skipped"
        item["skip_by"] = "contract-amended"
        item["skip_reason"] = "合同已修订：旧合同下的义务不再适用（未执行）"
        item["finished_at"] = now
        _close(schedule, item, now=now, reason="contract-amended")
        changes.append(f"义务 {item['obligation_id']} 因合同修订跳过")
    return changes


REUSE_FAMILIES = ("A", "C", "E")


def _supersede_older(schedule: dict, fresh: dict, *, now: str) -> list[str]:
    """同一 family+event 的旧窗口被新窗口取代：旧窗口记 skipped，而不是漏调。

    里程碑复盘（D）与方案规划（B）的实例随进展变化：如果不收掉旧窗口，每记录一次
    进展就多一个永久打开的义务，收口前会被成批记成“漏调”，也会让自动执行按记录
    次数重复付费。新窗口覆盖的是同一认知对象的最新事实，旧窗口从未执行的事实仍以
    `skip_by=superseded-by-newer` 留在审计里。执行中的窗口（in_flight / claimed）
    不动，避免打断正在跑的请求。
    """
    changes: list[str] = []
    family = _text(fresh.get("family"))
    event = _text(fresh.get("event"))
    if not family or not event:
        return changes
    for item in schedule["obligations"]:
        if not isinstance(item, dict) or item is fresh:
            continue
        if item.get("window") != "open":
            continue
        if _text(item.get("family")) != family or _text(item.get("event")) != event:
            continue
        if _text(item.get("status")) not in ("due", "claimed"):
            continue
        item["status"] = "skipped"
        item["skip_by"] = "superseded-by-newer"
        item["skip_reason"] = "同一认知窗口出现更新的进展：旧窗口未执行即被取代"
        item["finished_at"] = now
        _close(schedule, item, now=now, reason="superseded-by-newer")
        changes.append(f"义务 {item['obligation_id']} 被更新窗口取代（未执行）")
    return changes



def _collapse_duplicate_windows(schedule: dict, *, now: str) -> list[str]:
    """同一 family+event 只保留最新的一个打开窗口（含历史遗留的堆积）。

    新窗口取代旧窗口时已就地标记；这里处理“先于本逻辑产生的多条打开窗口”，
    避免它们在被更晚窗口关闭时成批记成漏调。只处理 due/claimed，in_flight 不动。
    """
    changes: list[str] = []
    groups: dict[tuple[str, str], list[dict]] = {}
    for item in schedule["obligations"]:
        if not isinstance(item, dict) or item.get("window") != "open":
            continue
        if _text(item.get("status")) not in ("due", "claimed"):
            continue
        key = (_text(item.get("family")), _text(item.get("event")))
        if not key[0] or not key[1]:
            continue
        groups.setdefault(key, []).append(item)
    for key, rows in groups.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda item: (_text(item.get("opened_at")),
                                    _text(item.get("obligation_id"))))
        for stale in rows[:-1]:
            stale["status"] = "skipped"
            stale["skip_by"] = "superseded-by-newer"
            stale["skip_reason"] = "同一认知窗口有更新的未执行窗口：旧的被取代"
            stale["finished_at"] = now
            _close(schedule, stale, now=now, reason="superseded-by-newer")
            changes.append(f"义务 {stale['obligation_id']} 被更新窗口取代（未执行）")
    return changes


def sync(schedule: dict, state: dict, advice: dict, *, now: str,
         closed: bool = False, policy: str = "",
         auto_execution: bool | None = None) -> list[str]:
    """把当前建议物化为调度义务，并按已发生的事实关闭窗口。

    返回人类可读的变更说明（供状态历史/输出使用）。函数只改动传入的 schedule。
    `policy` / `auto_execution` 只用于记录**当时的**调度策略事实：有了它，事后
    才能区分“策略关闭所以没调用”与“应当调用但漏了”；没有它就记 unknown，绝不
    用当前配置倒推。
    """
    changes: list[str] = []
    if not isinstance(schedule.get("obligations"), list):
        schedule["obligations"] = []
    cycle = current_cycle(state)
    if note_policy(schedule, policy=policy, auto_execution=auto_execution,
                   now=now, cycle=cycle):
        changes.append(f"认知调度策略（周期 {cycle}）："
                       f"{_text(policy).lower()}"
                       + ("，允许自动执行" if auto_execution else
                          "，不自动执行" if auto_execution is False else ""))
    changes.extend(_expire_superseded(schedule, state, now=now))
    changes.extend(_collapse_duplicate_windows(schedule, now=now))
    action = _text(advice.get("action"))
    family = _text(advice.get("family"))
    event = _text(advice.get("event"))
    instance = _text(advice.get("instance"))
    if action in ("consult", "plan") and family in RP.COGNITION_FAMILIES and event:
        existing = find_obligation(state, family=family, event=event,
                                   instance=instance)
        if existing is None or existing.get("contract_sha256") != \
                _text(state.get("contract_sha256")):
            obligation = _new_obligation(state, advice, now=now)
            obligation["obligation_id"] = _next_obligation_id(schedule)
            # 打开时点策略：note_policy 已在本轮同步开头记录当前策略，这里把
            # “打开这一刻生效什么”固化到义务上，后续策略变更不覆盖它。
            current_policy = policy_snapshot(schedule, cycle)
            if _text(current_policy.get("value")):
                obligation["policy_at_open"] = {
                    "value": _text(current_policy.get("value")),
                    "auto_execution": current_policy.get("auto_execution"),
                    "at": _text(current_policy.get("at")),
                }
            changes.extend(_supersede_older(schedule, obligation, now=now))
            schedule["obligations"].append(obligation)
            changes.append(
                f"新增调度义务 {obligation['obligation_id']}"
                f"（{family}/{event}）")
        else:
            if _refresh(existing, advice, now=now):
                existing["refreshed_at"] = now
    elif action == "reuse":
        # 复用即满足：同一认知对象已有有效结论，不需要再付费。复用只能满足
        # “已被覆盖的模式”对应的义务：CONSULT 的 A/C/E 与 PLAN 的 B。
        covered = advice.get("covered") or {}
        for item in schedule["obligations"]:
            if not isinstance(item, dict):
                continue
            if item.get("window") != "open":
                continue
            row_family = _text(item.get("family"))
            if row_family in REUSE_FAMILIES and covered.get("CONSULT"):
                pass
            elif row_family == "B" and covered.get("PLAN"):
                pass
            else:
                continue
            item["status"] = "satisfied"
            item["source"] = "reuse"
            item["reuse_of"] = _text(advice.get("call_id") or "subject-reuse")
            item["finished_at"] = now
            _close(schedule, item, now=now, reason="reuse")
            changes.append(f"义务 {item['obligation_id']} 由复用满足")
    if closed:
        # 收口前先把“更晚窗口已满足”的事实结算掉：否则更早的里程碑窗口会被
        # 直接 close 成漏调，而它其实已被收口判断覆盖。
        _advance_windows(schedule, now=now)
        for item in schedule["obligations"]:
            if not isinstance(item, dict) or item.get("window") != "open":
                continue
            if _text(item.get("status")) in ("due", "claimed") \
                    and _text(item.get("level")) != "recommended":
                # 非必需窗口（历史 optional 义务）收口时按“合理跳过”结算：
                # 漏调只统计确实应当执行而未执行的窗口。
                item["status"] = "skipped"
                item["skip_by"] = "not-required"
                item["skip_reason"] = "非必需认知窗口：任务收口时按合理跳过结算"
                item["finished_at"] = now
                _close(schedule, item, now=now, reason="not-required")
                changes.append(f"义务 {item['obligation_id']} 非必需，收口时跳过")
                continue
            _close(schedule, item, now=now, reason="task-closed")
    elif action != "none":
        _advance_windows(schedule, now=now)
    return changes


def summary(state: dict) -> dict:
    """给 evaluate/status 的只读摘要：未闭合义务与下一条可执行项。"""
    rows = obligations(state)
    raw = schedule_raw(state)
    pending = [item for item in rows
               if isinstance(item, dict) and item.get("window") == "open"
               and _text(item.get("status")) in ("due", "claimed", "in_flight")]
    missed = [item for item in rows if isinstance(item, dict) and is_missed(item)]
    next_item = due_obligations(state)
    # 终态处置与周期分布只是可观测值：它们不进入 total/pending/missed，也不参与
    # 覆盖率分母；历史义务没有持久化处置时按窗口事实现算，不改历史状态。
    dispositions = {kind: 0 for kind in DISPOSITION_KINDS}
    by_cycle: dict[int, dict] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        cycle = obligation_cycle(item)
        row = by_cycle.setdefault(cycle, {
            "cycle": cycle, "obligations": 0, "open": 0, "missed": 0,
            "dispositions": {kind: 0 for kind in DISPOSITION_KINDS}})
        row["obligations"] += 1
        if item.get("window") == "open":
            row["open"] += 1
        if is_missed(item):
            row["missed"] += 1
        disposition = obligation_disposition(item, raw)
        if disposition["kind"]:
            dispositions[disposition["kind"]] += 1
            row["dispositions"][disposition["kind"]] += 1
    current = current_cycle(state)
    return {
        "schema": SCHEDULE_SCHEMA,
        "total": len(rows),
        "pending": len(pending),
        "missed": len(missed),
        "cycle": current,
        "policy": policy_snapshot(raw, current),
        "dispositions": dispositions,
        "by_cycle": [by_cycle[key] for key in sorted(by_cycle)],
        "pending_obligations": [
            {"obligation_id": item.get("obligation_id"),
             "family": item.get("family"), "event": item.get("event"),
             "stage": item.get("stage"), "level": item.get("level"),
             "status": item.get("status"),
             "cycle": obligation_cycle(item)}
            for item in pending],
        "next": ({"obligation_id": next_item[0].get("obligation_id"),
                  "family": next_item[0].get("family"),
                  "event": next_item[0].get("event"),
                  "mode": next_item[0].get("mode"),
                  "stage": next_item[0].get("stage")}
                 if next_item else None),
    }


# ── 可直接执行的命令 ──────────────────────────────────────────────────────
def dispatch_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "copilot_dispatch.py")


def runnable_command(state_path: str, obligation: dict, *, round_two: bool,
                     reason: str = "") -> str:
    """生成可直接粘贴执行的认知命令：无占位符、材料缺省自动生成。"""
    mode = _text(obligation.get("mode")).upper() or "CONSULT"
    stage = _text(obligation.get("stage")) or "development"
    question = _text(obligation.get("question")) or RP.question_for(
        _text(obligation.get("event")) or stage)
    sub = {"PLAN": "cognition-plan", "REVIEW": "cognition-review"}.get(mode, "cognition-consult")
    parts = ["python3", "-B", dispatch_path(), sub,
             "--state", state_path, "--stage", stage,
             "--question", question]
    if round_two:
        trigger = RP.COGNITION_EVENT_TRIGGERS.get(
            _text(obligation.get("event")), "new-evidence")
        parts += ["--force", "--trigger", trigger,
                  "--reason", reason or _text(obligation.get("reason"))
                  or "同一认知对象出现新的现场事实，需要重新判断"]
    return " ".join(shlex.quote(part) for part in parts)


# ── 自动材料（结构化、确定性、限长）────────────────────────────────────────
def _evidence_rows(state: dict, limit: int = 40) -> list[str]:
    rows = []
    for item in state.get("evidence") or []:
        if not isinstance(item, dict) or item.get("dropped"):
            continue
        rows.append(f"- {item.get('id')} [{_text(item.get('kind')) or 'file'}] "
                    f"{_text(item.get('note'))[:160]}")
    return rows[:limit]


def _action_rows(state: dict) -> list[str]:
    rows = []
    for item in state.get("actions") or []:
        if not isinstance(item, dict):
            continue
        result = _text(item.get("result"))
        rows.append(f"- {item.get('id')} [{_text(item.get('status')) or 'pending'}] "
                    f"{_text(item.get('title'))[:80]}"
                    + (f" → {result[:160]}" if result else ""))
    return rows


def _criteria_rows(state: dict) -> list[str]:
    rows = []
    for item in (state.get("contract") or {}).get("criteria") or []:
        if not isinstance(item, dict):
            continue
        rows.append(f"- {item.get('criterion_id')} "
                    f"[{_text(item.get('status')) or 'pending'}] "
                    f"{_text(item.get('description'))[:200]}")
    return rows


def _delta_rows(state: dict, obligation: dict) -> list[str]:
    """本次相对最后一次认知调用的实质变化（只列可归因事实）。"""
    calls = [item for item in state.get("cognition_calls") or []
             if isinstance(item, dict) and item.get("status") == "completed"]
    if not calls:
        return ["- 本任务尚无已完成的认知调用：这是该窗口的首次判断。"]
    last = max(calls, key=lambda item: _text(item.get("created_at")))
    rows = []
    current = RP.cognition_event_snapshot(state)
    previous = last.get("cognition_event_snapshot") or {}
    if previous:
        new_regressed = [x for x in current["regressed"]
                         if x not in set(previous.get("regressed") or [])]
        invalidated = [x for x in (previous.get("met") or [])
                       if x not in set(current["met"])]
        new_replans = [x for x in current["replans"]
                       if x not in set(previous.get("replans") or [])]
        if new_regressed:
            rows.append("- 修复后复现的 finding：" + "、".join(new_regressed))
        if invalidated:
            rows.append("- 回退为未核销的完成条件：" + "、".join(sorted(invalidated)))
        if new_replans:
            rows.append("- 新增重规划工作项：" + "、".join(new_replans))
        new_decisions = [x for x in current["decisions"]
                         if x not in set(previous.get("decisions") or [])]
        if new_decisions:
            rows.append("- 新增未决决策：" + "、".join(new_decisions))
        previous_blocks = {str(item.get("block_id")): item
                           for item in previous.get("blocks") or []
                           if isinstance(item, dict)}
        changed_blocks = [item for item in current.get("blocks") or []
                          if item.get("active")
                          and previous_blocks.get(str(item.get("block_id"))) != item]
        if changed_blocks:
            rows.append("- 未解除阻塞发生变化：" + "、".join(
                str(item.get("block_id")) for item in changed_blocks))
        if previous.get("contract_sha256") and \
                previous["contract_sha256"] != current["contract_sha256"]:
            rows.append("- 合同指纹已变化：旧结论不覆盖当前合同。")
        if previous.get("risk") != current.get("risk"):
            rows.append(f"- 风险等级：{previous.get('risk') or '-'} → "
                        f"{current.get('risk') or '-'}")
    previous_evidence = set(previous.get("evidence_ids") or [])
    current_evidence = [str(item.get("id")) for item in state.get("evidence") or []
                        if isinstance(item, dict) and not item.get("dropped")]
    added = [eid for eid in current_evidence if eid not in previous_evidence]
    if added:
        rows.append("- 新增证据（仅身份，不自动视为实质变化）：" + "、".join(added))
    if not rows:
        rows.append("- 自上次调用以来没有可归因的实质变化。")
    return rows


def build_materials(state: dict, obligation: dict, *,
                    max_chars: int = DEFAULT_MATERIALS_CHARS,
                    readiness: dict | None = None) -> str:
    """从状态生成认知材料：结构化、确定性排序、限长、不含敏感载荷。

    材料不是状态转储：它必须回答“本次需要判断什么、相对上次变了什么”。信息不足
    时显式标注，而不是用套话制造形式化覆盖。`readiness`（最终工程验证准入事实）
    由调用方算好后传入，收口材料据此写明验证摘要与各项验证状态。
    """
    event = _text(obligation.get("event")) or "startup"
    family = _text(obligation.get("family")) or "C"
    # 历史判断与未处理议程：让认知判断做增量，而不是从零重问同一问题。只取
    # 最近三次判断的核心结论与采纳状态，不注入完整回复。
    prior = RP.prior_cognition(state.get("cognition_calls"), limit=3)
    agenda = RP.open_agenda(state.get("cognition_calls"))
    prior_lines: list[str] = []
    if prior:
        prior_lines = ["", "## 上次认知判断与采纳", ""]
        for item in prior:
            line = (f"- {item['call_id']}（{item['mode']}/{item['stage']}，采纳："
                    f"{item['acceptance']}"
                    + (f"，决策：{item['decision']}" if item["decision"] else "")
                    + f"）：{item['judgment'] or '（无判断文本）'}")
            if item["delta"]:
                line += f"；实际差异：{item['delta']}"
            prior_lines.append(line)
    agenda_lines: list[str] = []
    if agenda:
        agenda_lines = ["", "## 未处理议程（上次认知提出，仍相关才继续判断）", ""]
        agenda_lines += [f"- [{entry['kind']}] {entry['text']}"
                         for entry in agenda]
    lines = [
        f"# 认知{family} 阶段材料（{_text(obligation.get('mode'))} / "
        f"{_text(obligation.get('stage'))}）",
        "",
        f"- 材料口径：{MATERIALS_SCHEMA}",
        "",
        "## 本次需要判断的事项",
        "",
        _text(obligation.get("question")) or RP.question_for(event),
        "",
        "## 触发事实与前后变化",
        "",
        f"- 生命周期窗口：{family}（{event}）",
        f"- 触发原因：{_text(obligation.get('reason')) or RP.COGNITION_EVENTS.get(event, '-')}",
        *_delta_rows(state, obligation),
        *prior_lines,
        *agenda_lines,
        "",
        "## 任务目标与合同约束",
        "",
        f"- 目标：{_text(state.get('goal'))}",
        f"- 类型：{_text(state.get('task_kind')) or RP.DEFAULT_TASK_KIND}"
        f" · 风险：{_text(state.get('risk')) or '-'}",
        # 合同正文存放在 `done_criteria_text`（与合同指纹同源）；`body` 是不存在的
        # 键，读取它会让每份材料都印出 “None”，认知因此看不到合同正文。
        "- 合同正文摘要：" + (_contract_body(state)[:800] or "（未登记）"),
        "- 声明触发：" + ("、".join(
            _text(item.get("name"))
            for item in state.get("admission_triggers") or []
            if isinstance(item, dict) and item.get("name")) or "无"),
        "",
        "## 完成条件",
        "",
        *(_criteria_rows(state) or ["- 无"]),
        "",
        "## 计划与工作项",
        "",
        *(_action_rows(state) or ["- 尚未登记计划"]),
        "",
        "## 关键证据（身份与备注）",
        "",
        *(_evidence_rows(state) or ["- 尚未登记证据"]),
    ]
    policy = (state.get("contract") or {}).get("test_policy") or {}
    lines += ["", "## 必要测试（合同约定）", ""]
    if policy.get("required"):
        commands = [str(item).strip() for item in policy.get("commands") or []
                    if str(item).strip()]
        lines.append("- 合同要求执行：" + ("；".join(commands) or "未声明命令"))
        if isinstance(readiness, dict):
            lines.append("- 验证证据："
                         + ("已登记（来源 "
                            + _text(readiness.get("tests_source"))
                            + "）" if readiness.get("required_tests_completed")
                            else "尚未登记"))
    else:
        lines.append("- 合同未要求执行测试（不适用）")
    if family == "E" or _text(obligation.get("stage")) == "closure":
        lines += ["", "## 最终验证状态（收口准入事实）", ""]
        if isinstance(readiness, dict):
            lines += [
                f"- 最终验证摘要：{_text(readiness.get('digest'))}"
                f"（义务实例 {_text(obligation.get('instance'))}）",
                "- 工作项全部终态："
                + ("是" if readiness.get("all_actions_terminal") else "否"),
                "- 完成条件全部核销且证据齐全："
                + ("是" if readiness.get("criteria_satisfied") else "否"),
                "- 收口门禁（不含用户对质量报告的接受）："
                + ("通过" if readiness.get("verification_passed") else "未通过"),
                "- 验证准入：" + ("已完成" if readiness.get("ready")
                                  else "未完成（" + "；".join(
                                      _text(item) for item in
                                      readiness.get("blockers") or [])
                                  + "）"),
            ]
        else:
            lines.append("- 最终验证摘要：未提供（不得据此假定验证已完成）")
    blocked = [item for item in state.get("blocks") or []
               if isinstance(item, dict) and not item.get("resolved_at")]
    if blocked:
        lines += ["", "## 阻塞", ""]
        lines += [f"- {item.get('block_id')}：{_text(item.get('reason'))[:200]}"
                  f"（需要：{_text(item.get('needs'))[:120]}）" for item in blocked]
    ledger = state.get("finding_ledger") or {}
    items = [item for item in ledger.get("items") or []
             if isinstance(item, dict)
             and _text(item.get("status")) in ("open", "regressed", "fixed")]
    if items:
        lines += ["", "## 未闭合 finding", ""]
        lines += [f"- {item.get('finding_id')} [{_text(item.get('status'))}] "
                  f"{_text(item.get('summary'))[:160]}" for item in items[:20]]
    lines += [
        "",
        "## 输入说明",
        "",
        "以上材料由 Copilot 从任务状态自动生成：只含目标、合同、条件、工作项状态、"
        "证据身份与可归因的变化，不含仓库正文、日志或凭据。若判断所需信息不足，"
        "请在 unverified 中明确列出，不要猜测。",
        "",
    ]
    text = "\n".join(lines)
    if len(text) > max_chars:
        # 截断必须在稳定位置并显式标注，避免静默丢信息。
        text = text[:max_chars] + \
            f"\n\n[材料已按 {max_chars} 字符上限截断：需要更多信息请由主代理补充 --materials]"
    return text


# ── 覆盖率统计 ────────────────────────────────────────────────────────────
def _usage(record: dict) -> dict:
    usage = record.get("usage") or {}
    try:
        return {"tokens": int(usage.get("total_tokens") or 0),
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0)}
    except (TypeError, ValueError):
        return {"tokens": 0, "input_tokens": 0, "output_tokens": 0}


def _call_subject(record: dict) -> str:
    """重复调用判定用的认知对象键：优先显式 subject，退化到上下文/材料摘要。"""
    for name in ("subject", "subject_sha256", "context_sha256",
                 "materials_sha256"):
        value = _text(record.get(name))
        if value:
            return value
    return ""


def _reasoning_tokens(record: dict) -> int:
    usage = record.get("usage") or {}
    try:
        return int(usage.get("reasoning_tokens") or 0)
    except (TypeError, ValueError):
        return 0


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1,
                       int((len(ordered) - 1) * fraction + 0.5)))
    return ordered[index]


def _window_covered(rows: dict) -> int:
    """任务级窗口的覆盖计数：每个任务/窗口最多计 1 次（满足＝执行或复用）。

    分子必须与分母同单位。任务级窗口（A 启动 / B 规划 / E 收口）的分母是
    “该窗口对本任务是否适用”（0/1），因此分子也只能是 0/1；直接取满足义务的
    条数会把多轮 reopen 的多次收口判断算成 `rate=3.0`（3 次收口 ÷ 1 个任务）。
    义务条数仍完整保留在 `by_family[...]["satisfied"]`，用于解释“覆盖了几次”。
    """
    return 1 if rows["satisfied"] else 0


def task_stats(state: dict) -> dict:
    """单个任务的调度覆盖率与口径；分母显式，跳过与漏调分开统计。

    两个口径必须一致：任务级窗口（A/B/E）的分母是“是否适用”（0/1），分子也是
    0/1；事件窗口（C）的分母是适用义务条数，合理跳过的窗口不进入分母。
    """
    rows = [item for item in obligations(state) if isinstance(item, dict)]
    calls = [item for item in state.get("cognition_calls") or []
             if isinstance(item, dict)]
    completed = [item for item in calls if item.get("status") == "completed"]
    failed_calls = [item for item in calls if item.get("status") == "failed"]
    request_attempted = [item for item in calls
                         if item.get("request_attempted") is True]
    response_received = [item for item in calls
                         if item.get("send_observed") == "response_received"]
    send_unknown = [item for item in calls
                    if item.get("request_attempted") is True
                    and item.get("send_observed") == "unknown"]
    send_not_attempted = [item for item in calls
                          if item.get("request_attempted") is False]
    admission = RP.plan_admission(
        state, triggers=[_text(t.get("name"))
                         for t in state.get("admission_triggers") or []
                         if isinstance(t, dict) and t.get("name")],
        workspace=_text(state.get("workspace")))
    cognition_applicable = admission["level"] in ("medium", "high")
    progressed = RP.has_substantive_progress(state)
    # 收口窗口的适用条件必须与建议口径逐字一致（`cognition_advice` 的 E：复杂度
    # 中/高 + 有实质进展）。不沿用任务类别（`non_simple`）：真实信号决定复杂度，
    # 简单类别 + 高风险的真实任务同样会创建 E 义务。两处口径不一致会产生双向
    # 失真——分母要求却永远创建不出义务（假漏调），或义务已满足却不进分母，
    # 让多任务聚合覆盖率超过 100% 或掩盖其它任务的漏调。
    closure_eligible = bool(cognition_applicable and progressed)

    by_family: dict[str, dict] = {}
    for family in RP.COGNITION_FAMILIES:
        subset = [item for item in rows
                  if _text(item.get("family")) == family]
        by_family[family] = {
            "obligations": len(subset),
            "due": len([i for i in subset if _text(i.get("status")) == "due"]),
            "satisfied": len([i for i in subset
                              if _text(i.get("status")) == "satisfied"]),
            "failed": len([i for i in subset
                           if _text(i.get("status")) == "failed"]),
            "skipped": len([i for i in subset
                            if _text(i.get("status")) == "skipped"]),
            "missed": len([i for i in subset if is_missed(i)]),
            "in_flight": len([i for i in subset
                              if is_unresolved_in_flight(i)]),
            "reused": len([i for i in subset
                           if _text(i.get("source")) == "reuse"]),
            "executed": len([i for i in subset
                             if _text(i.get("source")) in ("auto", "manual",
                                                           "executed")]),
        }
    by_event: dict[str, int] = {}
    for item in rows:
        code = _text(item.get("event")) or "-"
        by_event[code] = by_event.get(code, 0) + 1

    # 终态处置与周期分布：只做观测，不进入 coverage 的分子分母，也不改变 missed
    # 口径。历史义务没有持久化处置时按窗口事实现算（只读，不回写历史状态）。
    raw_schedule = schedule_raw(state)
    dispositions = {kind: 0 for kind in DISPOSITION_KINDS}
    by_cycle: dict[int, dict] = {}
    for item in rows:
        cycle = obligation_cycle(item)
        row = by_cycle.setdefault(cycle, {
            "cycle": cycle, "obligations": 0,
            "dispositions": {kind: 0 for kind in DISPOSITION_KINDS}})
        row["obligations"] += 1
        kind = obligation_disposition(item, raw_schedule)["kind"]
        if kind:
            dispositions[kind] += 1
            row["dispositions"][kind] += 1

    # 分母：只统计“适用”的任务/窗口，并显式给出理由；跳过不进入分子，但仍保留
    # 原始义务计数，避免通过缩小分母制造覆盖率。
    coverage = {
        "startup": {
            # 与建议口径一致：启动窗口只对“存在中/高认知价值信号”的任务适用。
            # 用任务类别（非简单）当分母会制造假漏调——普通 standard 任务按设计
            # 不做启动 CONSULT，不能事后算成“应调未执行”。
            "denominator": 1 if cognition_applicable else 0,
            "reason": (f"启动 CONSULT 适用：复杂度 {admission['level']}"
                       "（按任务计 1 次）" if cognition_applicable else
                       f"启动 CONSULT 不适用：复杂度 {admission['level']}"),
            "numerator": _window_covered(by_family["A"]),
            "skipped": by_family["A"]["skipped"],
            "missed": by_family["A"]["missed"],
        },
        "plan": {
            "denominator": 1 if admission["required"] else 0,
            "reason": (f"PLAN 适用：{admission['reason']}（按任务计 1 次）"
                       if admission["required"]
                       else f"PLAN 不适用：{admission['reason']}"),
            "numerator": _window_covered(by_family["B"]),
            "skipped": by_family["B"]["skipped"],
            "missed": by_family["B"]["missed"],
        },
        "closure": {
            "denominator": 1 if closure_eligible else 0,
            "reason": ("有实质进展且中/高认知价值任务（收口 CONSULT 适用；"
                       "按任务计 1 次）" if closure_eligible else
                       "收口 CONSULT 不适用：无实质进展或复杂度不足"),
            "numerator": _window_covered(by_family["E"]),
            "skipped": by_family["E"]["skipped"],
            "missed": by_family["E"]["missed"],
        },
        "events": {
            # 每个事件窗口都是一条义务；合理跳过（合同修订、被更新窗口取代、
            # 非必需、已被收口覆盖）的窗口不进入分母，与 _expire_superseded /
            # sync 的口径一致，避免用“已失效窗口”拉低覆盖率。
            "denominator": max(0, by_family["C"]["obligations"]
                               - by_family["C"]["skipped"]),
            "reason": "已识别的重大事件（每出现一次构成一个适用窗口；"
                      "合理跳过的窗口不进入分母）",
            "numerator": by_family["C"]["satisfied"],
            "skipped": by_family["C"]["skipped"],
            "missed": by_family["C"]["missed"],
        },
    }
    for row in coverage.values():
        denom = int(row["denominator"] or 0)
        row["rate"] = (round(int(row["numerator"]) / denom, 4)
                       if denom else None)

    acceptance = {"accepted": 0, "modified": 0, "rejected": 0, "unrecorded": 0}
    for record in completed:
        verdict = _text((record.get("acceptance") or {}).get("status"))
        if verdict in ("accepted", "modified", "rejected"):
            acceptance[verdict] += 1
        else:
            acceptance["unrecorded"] += 1
    tokens = sum(_usage(item)["tokens"] for item in completed)
    latency = [float(item.get("latency_ms") or 0)
               for item in completed if item.get("latency_ms")]
    ordered_latency = sorted(latency)
    # 议程/采纳/收口后调用是可观测值，不是价值证明：议程条数多不代表判断有效，
    # 采纳率高也不代表结论正确；它们用于发现“判断沉睡”和“收口后重复付费”。
    agenda_calls = [item for item in completed
                    if RP.normalize_agenda(item.get("agenda"))]
    agenda_items = sum(len(RP.normalize_agenda(item.get("agenda")))
                       for item in completed)
    closure_calls = [item for item in completed
                     if _text(item.get("stage")) == "closure"]
    # 收口后调用只在“收口判断之后、下一次授权 reopen 之前”的区间内计数。重开会
    # 合法地重新打开认知窗口，把重开后的 C/D 调用算成“收口后重复付费”会制造
    # 假阳性，让这个观察值失去意义。
    closure_times = sorted(_text(item.get("created_at")) for item in closure_calls)
    reopen_times = sorted(
        _text(entry.get("at"))
        for entry in (state.get("closure") or {}).get("reopen_history") or []
        if isinstance(entry, dict) and _text(entry.get("at")))
    post_closure_calls = []
    for item in completed:
        created = _text(item.get("created_at"))
        if not created or _text(item.get("stage")) == "closure":
            continue
        if _text(item.get("mode")).upper() not in ("CONSULT", "PLAN"):
            continue
        prior = [value for value in closure_times if value < created]
        if not prior:
            continue
        last_closure = prior[-1]
        if any(last_closure < value <= created for value in reopen_times):
            continue
        post_closure_calls.append(item)
    # 模型级调用指标：模型只是观测维度，不是门禁。同一任务可能先后使用不同模型，
    # 因此按 model_key 分别汇总耗时/Token；历史记录没有 model 字段时归到 "-"，
    # 不把旧认知记录冒充成当前默认模型的调用证据。重复调用用同一模型下重复的
    # subject 计：completed − 去重 subject，>0 即存在同材料重复付费。
    by_model: dict[str, dict] = {}
    latency_by_model: dict[str, list[float]] = {}
    subjects_by_model: dict[str, set[str]] = {}
    for item in calls:
        key = (_text(item.get("model_key")) or _text(item.get("model"))
               or "-")
        row = by_model.setdefault(key, {
            "model_key": key,
            "model": _text(item.get("model")),
            "provider": _text(item.get("provider")),
            "calls": 0,
            "completed": 0,
            "failed": 0,
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "subjects": 0,
            "repeat_calls": 0,
        })
        row["calls"] += 1
        if item.get("status") == "completed":
            row["completed"] += 1
            usage = _usage(item)
            row["total_tokens"] += usage["tokens"]
            row["input_tokens"] += usage["input_tokens"]
            row["output_tokens"] += usage["output_tokens"]
            row["reasoning_tokens"] += _reasoning_tokens(item)
            subject = _call_subject(item)
            if subject:
                subjects_by_model.setdefault(key, set()).add(subject)
            if item.get("latency_ms"):
                latency_by_model.setdefault(key, []).append(
                    float(item.get("latency_ms") or 0))
        elif item.get("status") == "failed":
            row["failed"] += 1
    for key, row in by_model.items():
        row["subjects"] = len(subjects_by_model.get(key) or ())
        row["repeat_calls"] = max(0, row["completed"] - row["subjects"])
        latencies = sorted(int(value) for value in latency_by_model.get(key) or [])
        row["latency_ms_p50"] = _percentile(latencies, 0.50)
        row["latency_ms_p95"] = _percentile(latencies, 0.95)
    # 复用命中：只登记“未重复付费”的事实，命中的条目来自 state["cognition_reuses"]，
    # 与 by_family[*]["reused"] 同一口径（义务级复用），两者都不是价值证明。
    # 历史 `sol_reuses` 键只读合并，不迁移、不回写。
    reuse_rows = [item for key in ("cognition_reuses", "sol_reuses")
                  for item in state.get(key) or []
                  if isinstance(item, dict)]
    reuse_by_model: dict[str, int] = {}
    for item in reuse_rows:
        key = (_text(item.get("model_key")) or _text(item.get("model"))
               or "-")
        reuse_by_model[key] = reuse_by_model.get(key, 0) + 1
    reuse = {
        "hits": len(reuse_rows),
        "by_family": {family: by_family[family]["reused"]
                      for family in RP.COGNITION_FAMILIES},
        "by_model": [{"model_key": key, "hits": reuse_by_model[key]}
                     for key in sorted(reuse_by_model)],
    }
    return {
        "task_id": _text(state.get("task_id")),
        # 认知适用＝存在真实的认知价值信号（中/高复杂度），不是“任务类别非
        # 简单”：`risk=medium` 默认值不构成任何适用性。启动窗口先于调查，因此
        # 不看 `plan_admission().required`（那个判定还要求已经调查出事实）。
        "eligible": bool(cognition_applicable),
        "plan_eligible": bool(admission["required"]),
        "progressed": bool(progressed),
        "closure_eligible": closure_eligible,
        "level": admission["level"],
        "task_kind": admission["task_kind"],
        "obligations": len(rows),
        # “应登记而未登记”只适用于本应拥有启动窗口的任务：低认知价值任务
        # （0 信号）按设计要求不产生任何义务，不能算作登记缺口。
        "not_registered": bool(cognition_applicable
                               and not by_family["A"]["obligations"]),
        "by_family": by_family,
        "by_event": by_event,
        "cycle": current_cycle(state),
        "dispositions": dispositions,
        "by_cycle": [by_cycle[key] for key in sorted(by_cycle)],
        "coverage": coverage,
        "calls": {"completed": len(completed), "failed": len(failed_calls),
                  "total": len(calls),
                  "request_attempted": len(request_attempted),
                  "response_received": len(response_received),
                  "send_unknown": len(send_unknown),
                  "not_attempted": len(send_not_attempted),
                  "transport_observation_missing": len(calls) - len(
                      request_attempted) - len(send_not_attempted)},
        "acceptance": acceptance,
        "agenda": {"calls": len(agenda_calls),
                   "items": agenda_items,
                   "open": len(RP.open_agenda(calls))},
        "pending_acceptance": len(RP.pending_acceptance(completed, limit=10000)),
        "post_closure_calls": len(post_closure_calls),
        "usage": {"total_tokens": tokens,
                  "input_tokens": sum(_usage(i)["input_tokens"]
                                      for i in completed),
                  "output_tokens": sum(_usage(i)["output_tokens"]
                                       for i in completed),
                  "reasoning_tokens": sum(_reasoning_tokens(i)
                                          for i in completed),
                  "recorded_calls": len([i for i in completed
                                         if _usage(i)["tokens"]]),
                  "latency_ms_recorded": len(ordered_latency),
                  "latency_ms_p50": _percentile(
                      [int(value) for value in ordered_latency], 0.50),
                  "latency_ms_p95": _percentile(
                      [int(value) for value in ordered_latency], 0.95)},
        "by_model": [by_model[key] for key in sorted(by_model)],
        "reuse": reuse,
    }


def stats(states: list[dict], *, scope: str = "") -> dict:
    """多任务聚合：每个任务一份明细，并给出显式分母的总体覆盖率。"""
    details = [task_stats(state) for state in states]
    totals = {
        "tasks": len(details),
        "eligible_tasks": sum(1 for d in details if d["eligible"]),
        "plan_eligible_tasks": sum(1 for d in details if d["plan_eligible"]),
        "closure_eligible_tasks": sum(1 for d in details
                                      if d["closure_eligible"]),
        "progressed_tasks": sum(1 for d in details
                                if d["eligible"] and d["progressed"]),
        "not_registered_tasks": sum(1 for d in details if d["not_registered"]),
        "obligations": sum(d["obligations"] for d in details),
        "dispositions": {
            kind: sum(d["dispositions"][kind] for d in details)
            for kind in DISPOSITION_KINDS
        },
        "reopened_tasks": sum(1 for d in details if d["cycle"] > 1),
    }
    coverage = {}
    for key in ("startup", "plan", "closure", "events"):
        denominator = sum(int(d["coverage"][key]["denominator"] or 0)
                          for d in details)
        numerator = sum(int(d["coverage"][key]["numerator"] or 0)
                        for d in details)
        missed = sum(int(d["coverage"][key]["missed"] or 0) for d in details)
        skipped = sum(int(d["coverage"][key]["skipped"] or 0) for d in details)
        coverage[key] = {
            "denominator": denominator,
            "numerator": numerator,
            "missed": missed,
            "skipped": skipped,
            "rate": round(numerator / denominator, 4) if denominator else None,
        }
    by_event: dict[str, int] = {}
    for detail in details:
        for code, count in detail["by_event"].items():
            by_event[code] = by_event.get(code, 0) + count
    # 模型级聚合：跨任务按 model_key 合并，耗时用全量样本重算分位，避免“平均值
    # 的平均”掩盖长尾；复用命中同样按模型合并，历史无 model 的记录归 "-"。
    model_rows: dict[str, dict] = {}
    for state in states:
        for item in state.get("cognition_calls") or []:
            if not isinstance(item, dict):
                continue
            key = (_text(item.get("model_key")) or _text(item.get("model"))
                   or "-")
            row = model_rows.setdefault(key, {
                "model_key": key, "model": _text(item.get("model")),
                "provider": _text(item.get("provider")),
                "calls": 0, "completed": 0, "failed": 0,
                "total_tokens": 0, "input_tokens": 0, "output_tokens": 0,
                "reasoning_tokens": 0, "_latency": [],
            })
            row["calls"] += 1
            if item.get("status") == "completed":
                row["completed"] += 1
                usage = _usage(item)
                row["total_tokens"] += usage["tokens"]
                row["input_tokens"] += usage["input_tokens"]
                row["output_tokens"] += usage["output_tokens"]
                row["reasoning_tokens"] += _reasoning_tokens(item)
                if item.get("latency_ms"):
                    row["_latency"].append(int(float(item["latency_ms"])))
            elif item.get("status") == "failed":
                row["failed"] += 1
    reuse_by_model: dict[str, int] = {}
    for state in states:
        for item in state.get("cognition_reuses") or []:
            if not isinstance(item, dict):
                continue
            key = (_text(item.get("model_key")) or _text(item.get("model"))
                   or "-")
            reuse_by_model[key] = reuse_by_model.get(key, 0) + 1
    by_model = []
    for key in sorted(model_rows):
        row = model_rows[key]
        latencies = sorted(row.pop("_latency"))
        row["latency_ms_p50"] = _percentile(latencies, 0.50)
        row["latency_ms_p95"] = _percentile(latencies, 0.95)
        by_model.append(row)
    call_keys = ("completed", "failed", "total", "request_attempted",
                 "response_received", "send_unknown", "not_attempted",
                 "transport_observation_missing")
    return {
        "schema": STATS_SCHEMA,
        "scope": scope,
        "totals": totals,
        "coverage": coverage,
        "by_event": dict(sorted(by_event.items())),
        "calls": {
            key: sum(d["calls"][key] for d in details) for key in call_keys
        },
        "acceptance": {
            key: sum(d["acceptance"][key] for d in details)
            for key in ("accepted", "modified", "rejected", "unrecorded")
        },
        "agenda": {
            "calls": sum(d["agenda"]["calls"] for d in details),
            "items": sum(d["agenda"]["items"] for d in details),
            "open": sum(d["agenda"]["open"] for d in details),
        },
        "pending_acceptance": sum(d["pending_acceptance"] for d in details),
        "post_closure_calls": sum(d["post_closure_calls"] for d in details),
        "usage": {
            "total_tokens": sum(d["usage"]["total_tokens"] for d in details),
            "input_tokens": sum(d["usage"]["input_tokens"] for d in details),
            "output_tokens": sum(d["usage"]["output_tokens"] for d in details),
            "reasoning_tokens": sum(d["usage"]["reasoning_tokens"]
                                    for d in details),
            "recorded_calls": sum(d["usage"]["recorded_calls"]
                                  for d in details),
            "latency_ms_recorded": sum(d["usage"]["latency_ms_recorded"]
                                       for d in details),
            "latency_ms_p50": _percentile([
                int(item.get("latency_ms") or 0)
                for state in states
                for item in state.get("cognition_calls") or []
                if isinstance(item, dict) and item.get("status") == "completed"
                and item.get("latency_ms")], 0.50),
            "latency_ms_p95": _percentile([
                int(item.get("latency_ms") or 0)
                for state in states
                for item in state.get("cognition_calls") or []
                if isinstance(item, dict) and item.get("status") == "completed"
                and item.get("latency_ms")], 0.95),
        },
        "tasks": details,
        "by_model": by_model,
        "reuse": {"hits": sum(d["reuse"]["hits"] for d in details),
                  "by_model": [{"model_key": key, "hits": reuse_by_model[key]}
                               for key in sorted(reuse_by_model)]},
        "notes": [
            "覆盖率分母只含适用任务/窗口：简单任务、无实质方案决策的 PLAN、"
            "未发生的事件、无合格里程碑、尚无实质进展的收口都不进入分母。",
            "跳过（skipped）需有可审计理由并单列：任务级窗口（A/B/E）的跳过不进入"
            "分子、仍计入该窗口的分母（任务只有 0/1 次机会）；事件窗口（C）每出现一次"
            "事件才构成一个适用窗口，因此合理跳过不进入分母，漏调仍单列。",
            "任务级窗口的分子按任务计 1 次（满足＝真实执行或复用）；义务条数与"
            "执行/复用/跳过的分项仍完整保留在 by_family，用于解释“覆盖了几次”。",
            "漏调（missed）＝窗口关闭时仍未执行且无有效复用；in_flight 单列为未决。",
            "not_registered 表示“应当登记却没有义务记录”，与“已登记未执行”不同。",
            "calls 是状态台账记录数，不等价于 HTTP 请求数；request_attempted 只表示进入 transport，"
            "response_received 表示收到了 HTTP 响应，send_unknown 不推断请求字节是否出站；"
            "历史记录缺观测字段单列，不从成功/失败补造。",
            "agenda / pending_acceptance / post_closure_calls 是可观测值而非价值证明："
            "议程条数与采纳率都不说明判断正确；post_closure_calls 是调度缺陷信号"
            "（收口判断之后仍追加非收口认知调用），应为 0。",
            "acceptance.unrecorded 只表示调用已完成但未回填采纳，不等于“被忽略”，"
            "也不影响 G1–G3；认知采纳只是可选审计记录，不是收口门禁。",
            "dispositions / by_cycle 是观察值而非门禁：completed/covered/skipped/"
            "failed/not-executed/unsettled 只描述窗口关闭时的实际结果，不改 coverage "
            "分子分母、不改 missed、不阻止收口；not-executed 的理由里若没有当时的"
            "策略快照，一律记 unknown，不用当前配置倒推。",
        ],
    }

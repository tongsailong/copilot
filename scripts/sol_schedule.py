#!/usr/bin/env python3
"""SOL 生命周期调度：调度义务、窗口事实、自动材料与覆盖率统计。

本模块只做**确定性策略与投影**，三件事绝对不做：

- 不发网络请求、不导入任何网络客户端（SOL 通道只存在于 `sol_review.py`）；
- 不读写状态文件（调用方传入 dict，调用方负责落盘）；
- 不判定工程完成、不改变阶段、不影响 G1–G3。

设计要点（与 SOL CONSULT/PLAN 的结论一致）：

1. **义务 vs 复用是两把键**。义务键标识“在哪个生命周期窗口欠了一次认知判断”
   （任务、合同指纹、family、事件、发生实例）；复用键仍由 `sol_review` 的
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

OBLIGATION_SCHEMA = "copilot-sol-obligation/v1"
SCHEDULE_SCHEMA = "copilot-sol-schedule/v1"
STATS_SCHEMA = "copilot-sol-stats/v1"
# 材料口径版本。v2：合同正文改读 `contract["done_criteria_text"]`（`body` 兼容回退），
# 正文缺失时显式标注“（未登记）”。该版本参与 `closure_verification_digest`，所以
# 材料内容变化会让旧收口判断停止覆盖新结果，而不是靠“代码改过了”想当然。
MATERIALS_SCHEMA = "copilot-sol-schedule-materials/v2"

# 义务状态：due 未执行 / claimed 已被本进程领取 / in_flight 已发出请求未落盘 /
# satisfied 已满足（含复用）/ failed 真实失败 / skipped 显式跳过（须有理由）。
OBLIGATION_STATUSES = ("due", "claimed", "in_flight", "satisfied", "failed",
                       "skipped")
# 窗口关闭时这些状态构成漏调：领取过但从未发出请求与“一直没动”等价。
MISSED_STATUSES = ("due", "claimed")
FAMILY_INDEX = {name: index for index, name in enumerate(RP.SOL_FAMILIES)}
DEFAULT_MATERIALS_CHARS = 24_000
# 自动调度路径等待任务 SOL 锁的上限（秒）：拿不到锁说明另一个进程正在调用，
# 本次放弃执行、义务保持 due，而不是无限阻塞工程命令。手动调用不设默认上限；
# `COPILOT_SOL_LOCK_TIMEOUT` 可覆盖。自动路径的“有界”是双重的：1 条义务、
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
def empty_schedule() -> dict:
    return {"schema": SCHEDULE_SCHEMA, "next_id": 1, "obligations": []}


def ensure_schedule(state: dict) -> dict:
    """返回可写的调度结构；旧状态没有该字段时按空调度处理（不落盘）。"""
    raw = state.get("sol_schedule")
    if not isinstance(raw, dict):
        raw = empty_schedule()
        state["sol_schedule"] = raw
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
    raw = state.get("sol_schedule")
    if not isinstance(raw, dict):
        return []
    items = raw.get("obligations")
    return items if isinstance(items, list) else []


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


def settle(obligation: dict, *, status: str, now: str, reason: str = "") -> None:
    """结算义务：状态一旦落定（成功/失败/跳过/复用），窗口随即关闭。

    结算不是完成证明，只是“这个认知窗口已经有结果”。不关闭窗口会让已执行义务
    永远停在 open，收口时被误算成“关闭前仍未执行”，也会掩盖后续漏调的窗口边界。
    """
    obligation["status"] = status
    if obligation.get("window") == "closed":
        return
    obligation["window"] = "closed"
    obligation["closed_at"] = now
    obligation["closed_reason"] = str(reason or status)


# ── 同步：把生命周期建议物化为义务、按事实关闭窗口 ────────────────────────
def _close(schedule: dict, obligation: dict, *, now: str, reason: str) -> None:
    if obligation.get("window") == "closed":
        return
    obligation["window"] = "closed"
    obligation["closed_at"] = now
    obligation["closed_reason"] = reason


def _advance_windows(schedule: dict, *, now: str) -> None:
    """更晚窗口已经“满足”时，关闭更早仍未满足的窗口（漏调因此可见）。

    只在确实有更晚窗口成功执行/复用后才关闭：刚创建、被提示但未执行、预算耗尽
    都不关闭窗口，避免把“还没来得及做”误判成漏调。
    """
    satisfied = [item for item in schedule["obligations"]
                 if isinstance(item, dict) and item.get("window") == "closed"
                 and _text(item.get("status")) == "satisfied"]
    if not satisfied:
        return
    frontier = min(family_index(_text(item.get("family"))) for item in satisfied)
    for item in schedule["obligations"]:
        if not isinstance(item, dict) or item.get("window") != "open":
            continue
        family = family_index(_text(item.get("family")))
        if family >= frontier:
            continue
        if frontier == family_index("E") and _text(item.get("family")) == "D":
            # 收口综合判断覆盖了里程碑复盘的认知对象（目标达成/遗漏/下一阶段）。
            # 最后一批进展刚打开 D 窗口就收口时，记“合理跳过”而不是漏调：漏调只
            # 统计确实应当独立执行而未执行的窗口。
            item["status"] = "skipped"
            item["skip_by"] = "closure-covered"
            item["skip_reason"] = "收口综合判断已覆盖该里程碑复盘的认知对象"
            item["finished_at"] = now
            _close(schedule, item, now=now, reason="closure-covered")
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
         closed: bool = False) -> list[str]:
    """把当前建议物化为调度义务，并按已发生的事实关闭窗口。

    返回人类可读的变更说明（供状态历史/输出使用）。函数只改动传入的 schedule。
    """
    changes: list[str] = []
    if not isinstance(schedule.get("obligations"), list):
        schedule["obligations"] = []
    changes.extend(_expire_superseded(schedule, state, now=now))
    changes.extend(_collapse_duplicate_windows(schedule, now=now))
    action = _text(advice.get("action"))
    family = _text(advice.get("family"))
    event = _text(advice.get("event"))
    instance = _text(advice.get("instance"))
    if action in ("consult", "plan") and family in RP.SOL_FAMILIES and event:
        existing = find_obligation(state, family=family, event=event,
                                   instance=instance)
        if existing is None or existing.get("contract_sha256") != \
                _text(state.get("contract_sha256")):
            obligation = _new_obligation(state, advice, now=now)
            obligation["obligation_id"] = _next_obligation_id(schedule)
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
    pending = [item for item in rows
               if isinstance(item, dict) and item.get("window") == "open"
               and _text(item.get("status")) in ("due", "claimed", "in_flight")]
    missed = [item for item in rows if isinstance(item, dict) and is_missed(item)]
    next_item = due_obligations(state)
    return {
        "schema": SCHEDULE_SCHEMA,
        "total": len(rows),
        "pending": len(pending),
        "missed": len(missed),
        "pending_obligations": [
            {"obligation_id": item.get("obligation_id"),
             "family": item.get("family"), "event": item.get("event"),
             "stage": item.get("stage"), "level": item.get("level"),
             "status": item.get("status")}
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
    """生成可直接粘贴执行的 SOL 命令：无占位符、材料缺省自动生成。"""
    mode = _text(obligation.get("mode")).upper() or "CONSULT"
    stage = _text(obligation.get("stage")) or "development"
    question = _text(obligation.get("question")) or RP.question_for(
        _text(obligation.get("event")) or stage)
    sub = {"PLAN": "sol-plan", "REVIEW": "sol-review"}.get(mode, "sol-consult")
    parts = ["python3", "-B", dispatch_path(), sub,
             "--state", state_path, "--stage", stage,
             "--question", question]
    if round_two:
        trigger = RP.SOL_EVENT_TRIGGERS.get(
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
    calls = [item for item in state.get("sol_calls") or []
             if isinstance(item, dict) and item.get("status") == "completed"]
    if not calls:
        return ["- 本任务尚无已完成的 SOL 调用：这是该窗口的首次判断。"]
    last = max(calls, key=lambda item: _text(item.get("created_at")))
    rows = []
    current = RP.sol_event_snapshot(state)
    previous = last.get("sol_event_snapshot") or {}
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
    """从状态生成 SOL 材料：结构化、确定性排序、限长、不含敏感载荷。

    材料不是状态转储：它必须回答“本次需要判断什么、相对上次变了什么”。信息不足
    时显式标注，而不是用套话制造形式化覆盖。`readiness`（最终工程验证准入事实）
    由调用方算好后传入，收口材料据此写明验证摘要与各项验证状态。
    """
    event = _text(obligation.get("event")) or "startup"
    family = _text(obligation.get("family")) or "C"
    lines = [
        f"# SOL {family} 阶段材料（{_text(obligation.get('mode'))} / "
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
        f"- 触发原因：{_text(obligation.get('reason')) or RP.SOL_EVENTS.get(event, '-')}",
        *_delta_rows(state, obligation),
        "",
        "## 任务目标与合同约束",
        "",
        f"- 目标：{_text(state.get('goal'))}",
        f"- 类型：{_text(state.get('task_kind')) or RP.DEFAULT_TASK_KIND}"
        f" · 风险：{_text(state.get('risk')) or '-'}",
        # 合同正文存放在 `done_criteria_text`（与合同指纹同源）；`body` 是不存在的
        # 键，读取它会让每份材料都印出 “None”，SOL 因此看不到合同正文。
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
    calls = [item for item in state.get("sol_calls") or []
             if isinstance(item, dict)]
    completed = [item for item in calls if item.get("status") == "completed"]
    failed_calls = [item for item in calls if item.get("status") == "failed"]
    admission = RP.plan_admission(
        state, triggers=[_text(t.get("name"))
                         for t in state.get("admission_triggers") or []
                         if isinstance(t, dict) and t.get("name")],
        workspace=_text(state.get("workspace")))
    non_simple = admission["non_simple"]
    progressed = RP.has_substantive_progress(state)
    # 收口窗口的适用条件必须与建议口径一致（complexity medium/high 或高风险），
    # 否则会出现“分母要求却永远不可能创建义务”的假漏调。
    closure_eligible = bool(
        non_simple and progressed
        and (_text(state.get("risk")).lower() == "high"
             or admission["level"] in ("medium", "high")))

    by_family: dict[str, dict] = {}
    for family in RP.SOL_FAMILIES:
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

    # 分母：只统计“适用”的任务/窗口，并显式给出理由；跳过不进入分子，但仍保留
    # 原始义务计数，避免通过缩小分母制造覆盖率。
    coverage = {
        "startup": {
            "denominator": 1 if non_simple else 0,
            "reason": "非简单任务（启动 CONSULT 适用；按任务计 1 次）",
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
            "reason": "有实质进展的非简单任务（收口 CONSULT 适用；按任务计 1 次）",
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
    latency = [float((item.get("acceptance") or {}).get("latency_s") or 0)
               for item in completed]
    return {
        "task_id": _text(state.get("task_id")),
        "eligible": bool(non_simple),
        "plan_eligible": bool(admission["required"]),
        "progressed": bool(progressed),
        "closure_eligible": closure_eligible,
        "level": admission["level"],
        "task_kind": admission["task_kind"],
        "obligations": len(rows),
        "not_registered": bool(non_simple and not by_family["A"]["obligations"]),
        "by_family": by_family,
        "by_event": by_event,
        "coverage": coverage,
        "calls": {"completed": len(completed), "failed": len(failed_calls),
                  "total": len(calls)},
        "acceptance": acceptance,
        "usage": {"total_tokens": tokens,
                  "recorded_calls": len([i for i in completed
                                         if _usage(i)["tokens"]]),
                  "latency_ms_recorded": len([v for v in latency if v])},
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
    return {
        "schema": STATS_SCHEMA,
        "scope": scope,
        "totals": totals,
        "coverage": coverage,
        "by_event": dict(sorted(by_event.items())),
        "calls": {
            "completed": sum(d["calls"]["completed"] for d in details),
            "failed": sum(d["calls"]["failed"] for d in details),
            "total": sum(d["calls"]["total"] for d in details),
        },
        "acceptance": {
            key: sum(d["acceptance"][key] for d in details)
            for key in ("accepted", "modified", "rejected", "unrecorded")
        },
        "usage": {
            "total_tokens": sum(d["usage"]["total_tokens"] for d in details),
            "recorded_calls": sum(d["usage"]["recorded_calls"]
                                  for d in details),
        },
        "tasks": details,
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
        ],
    }

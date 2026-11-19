#!/usr/bin/env python3
"""复核效率策略：风险准入、Finding 台账、停止条件与重入判断。

本模块只做纯计算，不读写任务状态、不调用模型、不访问网络。治理层在
`governance.py` 中负责加锁、持久化与门禁；`native_agent.py` 只用它生成
派发前的准入/停止建议。所有判断都必须能从状态里的真实记录重算，不接受
复核方或调用方自述作为唯一证据。

设计约束
--------
* 一次完整复核 + 必要的定向复验：只有出现实质触发因素才允许重新完整复核。
* Finding 使用稳定 ID 与 `open → fixed → verified → closed` 生命周期；同一
  根因换一种说法仍是同一条，已关闭问题没有新证据不得重复登记为新问题。
* 停止条件是状态事实：PASS 的目标/代码范围与当前一致时，不因 Reviewer 还能
  提出建议而重开；minor/info 不阻断，也不触发复核。
* 纯文档/格式调整不需要独立复核；高风险任务建议一次 SOL 零工具单轮复核
  （advisory，不是收口门禁）。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re

LEDGER_SCHEMA = "copilot-finding-ledger/v1"
COMPLEXITY_SCHEMA = "copilot-task-complexity/v1"
SOL_ADVICE_SCHEMA = "copilot-sol-advice/v2"

# 任务认知复杂度只使用状态里已有的确定性事实：不引入第二个模型，也不引入
# 评分模型。每个信号都对应一条可复核的现场证据；0 个信号即低复杂度。
COMPLEXITY_LEVELS = ("low", "medium", "high")
COMPLEXITY_SIGNALS = {
    "risk-high": "高风险或不可逆影响",
    "declared-trigger": "声明了架构/安全/数据链路类风险",
    "multi-module": "证据跨越多个模块或目录",
    "multi-action": "工作项数量明显超过普通局部修改",
    "interdependent": "多个工作项相互依赖",
    "replan-history": "曾出现方案失败并重新规划",
    "reopened": "任务曾重开",
    "user-request": "用户明确要求 SOL 判断或独立复核",
}

# 只有“升级型”信号才算认知对象发生实质变化、允许第二次调用。结构型信号
# （工作项数量、依赖、目录、声明的触发因素）只用于判断复杂度：开发过程中证据
# 目录或计划条目增加不代表需要重新判断；范围变化走合同修订（合同指纹变化即
# 旧结论不再覆盖）。
SOL_ESCALATION_SIGNALS = ("risk-high", "replan-history", "reopened")

# 台账状态：fixed 只是“已改”，verified 才表示修复被定向验证；只有
# verified/closed/dropped 不再阻断。regressed 表示已修复问题又复现。
FINDING_STATUSES = ("open", "fixed", "verified", "closed", "regressed", "dropped")
RESOLVED_STATUSES = {"verified", "closed", "dropped"}
BLOCKING_SEVERITIES = {"blocking", "major"}
SEVERITY_ORDER = {"blocking": 0, "major": 1, "minor": 2, "info": 3}

# 复核层级：按任务影响、失败后果、变更范围与不确定性划分，不引入评分模型。
# `normal` 是历史风险标签，语义等同于 `medium`；读取旧状态时按 medium 处理。
ADMISSION_TRIGGERS = {
    "security": "安全/隐私/凭据处理",
    "permissions": "权限或访问控制",
    "core-logic": "核心业务逻辑或关键数据链路",
    "data-integrity": "事务、并发或数据一致性",
    "architecture": "关键架构或共享基础设施",
    "global-execution": "全局执行机制或调度",
    "production": "生产环境或数据迁移",
    "release": "高风险发布或不可逆操作",
}

class ReviewPolicyError(ValueError):
    """策略输入不合法；调用方必须显式报告，不得静默降级。"""


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now(now: str = "") -> str:
    if now:
        return now
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _text(value: object) -> str:
    return str(value or "").strip()


def _normalize(text: object) -> str:
    """归一化文本：大小写、空白、标点都不应影响同一问题的识别。"""
    raw = _text(text).lower()
    raw = re.sub(r"\s+", " ", raw)
    raw = re.sub(r"[`'\"“”‘’()\[\]{}<>《》「」『』，。；：、,.;:!?！？/\\|_-]+",
                 " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _similarity(first: str, second: str) -> float:
    if not first or not second:
        return 0.0
    if first == second:
        return 1.0
    return difflib.SequenceMatcher(None, first, second).ratio()


def _severity_rank(value: object) -> int:
    return SEVERITY_ORDER.get(_text(value).lower(), 9)


def _max_severity(first: object, second: object) -> str:
    left, right = _text(first).lower(), _text(second).lower()
    if not left:
        return right
    if not right:
        return left
    return left if _severity_rank(left) <= _severity_rank(right) else right


def _ledger(state: dict) -> dict:
    """返回可写的台账结构；不保存。旧状态没有台账时按空台账处理。"""
    raw = state.get("finding_ledger")
    if not isinstance(raw, dict):
        raw = {"schema": LEDGER_SCHEMA, "next_id": 1, "items": []}
        state["finding_ledger"] = raw
    raw.setdefault("schema", LEDGER_SCHEMA)
    if not isinstance(raw.get("items"), list):
        raw["items"] = []
    try:
        next_id = int(raw.get("next_id") or 1)
    except (TypeError, ValueError):
        next_id = 1
    raw["next_id"] = max(1, next_id)
    return raw


def _items(state: dict) -> list[dict]:
    """只读视图：旧状态没有台账时返回空列表，不在查询路径创建字段。"""
    raw = state.get("finding_ledger")
    if not isinstance(raw, dict):
        return []
    items = raw.get("items")
    return items if isinstance(items, list) else []


def _new_finding_id(ledger: dict) -> str:
    used = {_text(item.get("finding_id")) for item in ledger["items"]
            if isinstance(item, dict)}
    index = int(ledger.get("next_id") or 1)
    while f"F{index}" in used:
        index += 1
    ledger["next_id"] = index + 1
    return f"F{index}"


def _identity_text(item: dict) -> str:
    root = _normalize(item.get("root_cause"))
    if root:
        return root
    return _normalize(item.get("summary"))


def _match(items: list[dict], finding: dict) -> dict | None:
    """按稳定 ID / 根因 / 文件+摘要相似度匹配已有 Finding。

    显式 ID 只在身份兼容时复用；同一 ID 被用于不同根因时宁可新建一条，
    也不能把两个缺陷合并掉。
    """
    reported = _text(finding.get("id")).upper()
    if reported:
        for item in items:
            aliases = {_text(alias).upper()
                       for alias in item.get("reported_ids") or []}
            aliases.add(_text(item.get("finding_id")).upper())
            if reported in aliases:
                current = _identity_text(item)
                incoming = _normalize(finding.get("root_cause")) or \
                    _normalize(finding.get("summary"))
                if not current or not incoming or \
                        _similarity(current, incoming) >= 0.6:
                    return item
                # ID 被复用到另一条根因：忽略这个 ID，继续按根因/摘要匹配，
                # 而不是只因为命中了第一条 item 就放弃全部匹配。
                break
    root = _normalize(finding.get("root_cause"))
    if root:
        for item in items:
            roots = {_normalize(item.get("root_cause"))}
            roots |= {_normalize(alias)
                      for alias in item.get("root_cause_aliases") or []}
            if root in roots:
                return item
    file = _text(finding.get("file"))
    incoming_summary = _normalize(finding.get("summary")) or \
        _normalize(finding.get("root_cause"))
    best, score = None, 0.0
    for item in items:
        item_file = _text(item.get("file"))
        if file and item_file and file != item_file:
            continue
        current = _normalize(item.get("summary")) or \
            _normalize(item.get("root_cause"))
        value = _similarity(current, incoming_summary)
        if file and item_file and file == item_file and \
                finding.get("line") == item.get("line"):
            value = min(1.0, value + 0.08)
        if value > score:
            best, score = item, value
    return best if score >= 0.82 else None


def _append_history(item: dict, status: str, *, at: str, review_sha: str = "",
                   note: str = "", evidence_ids: list[str] | None = None) -> None:
    item.setdefault("history", []).append({
        "status": status,
        "at": at,
        "review_sha256": review_sha,
        "note": note[:500],
        "evidence_ids": list(evidence_ids or []),
    })


def merge_review(state: dict, review: dict, *, at: str = "",
                 authoritative: bool = True) -> dict:
    """把一条复核的 findings 合并进稳定台账。

    `authoritative=False`（advisory 复核，例如 SOL）只能新增或重复报告 finding，
    不能把已有问题改成 verified；否则一条参考性结论就能清掉 blocking/major。
    若参考性复核把已关闭的 minor/info 提级为 blocking/major，则允许标为
    regressed（重新阻断，不是清除）。真正的生命周期推进必须来自 ZCode 用定向
    测试证据执行的 `finding` 命令；v7 及更早的正式 G4 记录仍按 authoritative
    读取，保持历史保护级别。

    返回 `refs` 供复核记录固化为 `finding_refs`；同一复核重复合并必须幂等
    （按 review sha 不重复累计 occurrences）。
    """
    ledger = _ledger(state)
    items = ledger["items"]
    review_sha = _text(review.get("result_sha256"))
    stamp = at or _text(review.get("received")) or _now()
    refs: list[dict] = []
    created: list[str] = []
    matched: list[str] = []
    rereported: list[str] = []
    regressed: list[str] = []
    for finding in review.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        reported = _text(finding.get("id")).upper()
        review_status = _text(finding.get("status") or "open").lower()
        if review_status not in ("open", "resolved", "regressed"):
            review_status = "open"
        if not authoritative:
            # 参考性复核不能推进生命周期；它的 resolved/regressed 只作为观察，
            # 由 ZCode 的定向验证重新判断。
            review_status = "open"
        item = _match(items, finding)
        action = "matched"
        if item is None:
            item = {
                "finding_id": _new_finding_id(ledger),
                "root_cause": _text(finding.get("root_cause")),
                "severity": _text(finding.get("severity") or "info").lower(),
                "file": _text(finding.get("file")),
                "line": finding.get("line"),
                "summary": _text(finding.get("summary")),
                "status": "open",
                # SOL 复核只提供建议（advisory）；v7 及更早的正式复核记录
                # （authoritative）仍按原保护级别读取。advisory 的 blocking/major
                # 可以由主代理在给出反证后 dismiss；authoritative 的只能修复并
                # 定向验证。
                "authority": "authoritative" if authoritative else "advisory",
                "first_review_sha256": review_sha,
                "last_review_sha256": review_sha,
                "occurrences": 0,
                "reported_ids": [],
                "last_evidence": "",
                "history": [],
            }
            items.append(item)
            created.append(item["finding_id"])
            action = "created"
        # 同一复核重复合并：完全幂等——不重复累计 occurrence，不重放生命周期，
        # 也不覆盖之后由 ZCode 人工推进的 fixed/verified 状态。
        already = any(entry.get("review_sha256") == review_sha
                      for entry in item.get("history") or []
                      if isinstance(entry, dict))
        if already:
            if item["finding_id"] not in matched:
                matched.append(item["finding_id"])
            refs.append({
                "reported_id": reported,
                "finding_id": item["finding_id"],
                "status": item.get("status"),
                "severity": item.get("severity"),
                "action": "duplicate",
                "id_conflict": False,
            })
            continue
        id_conflict = False
        aliases = {_text(alias).upper()
                   for other in items if other is not item
                   for alias in other.get("reported_ids") or []}
        aliases |= {_text(other.get("finding_id")).upper()
                    for other in items if other is not item}
        if reported and reported not in item.setdefault("reported_ids", []):
            if reported in aliases:
                # 同一 Reviewer ID 被用于另一条根因：保留新条目，但不把该 ID
                # 登记为别名，避免下一轮把两个不同缺陷合并。
                id_conflict = True
            else:
                item["reported_ids"].append(reported)
        previous = _text(item.get("status")) or "open"
        incoming_severity = _text(finding.get("severity")).lower()
        resolved_severity = _text(item.get("resolved_severity")).lower()
        if (not authoritative
                and previous in RESOLVED_STATUSES
                and incoming_severity in BLOCKING_SEVERITIES
                and resolved_severity not in BLOCKING_SEVERITIES
                and _text(item.get("authority")) == "authoritative"):
            # advisory 复核不能把已解决的 authoritative finding 单方面提级为
            # 不可驳回的阻断：单独登记一条可驳回的 advisory 提级条目，原权威
            # 记录的 verified/closed 状态与严重度保持不变。
            existing_id = _text(item.get("advisory_escalation_id"))
            existing = next(
                (other for other in items
                 if _text(other.get("finding_id")) == existing_id), None)
            if existing is not None and \
                    _text(existing.get("status")) not in RESOLVED_STATUSES:
                # 同一提级再次出现：更新已有 advisory 条目，不重复登记。
                existing["severity"] = _max_severity(
                    existing.get("severity"), incoming_severity)
                if finding.get("file"):
                    existing["file"] = _text(finding.get("file"))
                if finding.get("line") is not None:
                    existing["line"] = finding.get("line")
                if finding.get("summary"):
                    existing["summary"] = _text(finding.get("summary"))
                if finding.get("evidence"):
                    existing["last_evidence"] = _text(finding.get("evidence"))
                duplicate_review = any(
                    isinstance(entry, dict)
                    and entry.get("review_sha256") == review_sha
                    for entry in existing.get("history") or [])
                if not duplicate_review:
                    existing["occurrences"] = int(
                        existing.get("occurrences") or 0) + 1
                    _append_history(existing, _text(existing.get("status")),
                                    at=stamp, review_sha=review_sha,
                                    note="advisory escalation repeated")
                existing["last_review_sha256"] = (
                    review_sha or existing.get("last_review_sha256"))
                if existing["finding_id"] not in matched:
                    matched.append(existing["finding_id"])
                refs.append({
                    "reported_id": reported,
                    "finding_id": existing["finding_id"],
                    "status": existing.get("status"),
                    "severity": existing.get("severity"),
                    "action": "advisory-escalation",
                    "id_conflict": False,
                })
                continue
            escalation = {
                "finding_id": _new_finding_id(ledger),
                "root_cause": ("advisory-escalation:"
                               + item["finding_id"] + ":"
                               + _normalize(finding.get("summary"))[:200]),
                "severity": incoming_severity,
                "file": _text(finding.get("file")) or _text(item.get("file")),
                "line": (finding.get("line")
                         if finding.get("line") is not None
                         else item.get("line")),
                "summary": (_text(finding.get("summary"))
                            or _text(item.get("summary"))),
                "status": "open",
                "authority": "advisory",
                "escalation_of": item["finding_id"],
                "first_review_sha256": review_sha,
                "last_review_sha256": review_sha,
                "occurrences": 1,
                "reported_ids": [],
                "last_evidence": _text(finding.get("evidence")),
                "history": [{
                    "status": "open",
                    "at": stamp,
                    "review_sha256": review_sha,
                    "note": "advisory escalation of " + item["finding_id"],
                    "evidence_ids": [],
                }],
            }
            item["advisory_escalation_id"] = escalation["finding_id"]
            items.append(escalation)
            created.append(escalation["finding_id"])
            matched.append(escalation["finding_id"])
            refs.append({
                "reported_id": reported,
                "finding_id": escalation["finding_id"],
                "status": "open",
                "severity": incoming_severity,
                "action": "advisory-escalation",
                "id_conflict": False,
            })
            continue
        previous_evidence = _text(item.get("last_evidence"))
        if authoritative:
            # 权威复核把同一 finding 升级为 authoritative，之后不再允许仅凭
            # 执行方一句说明 dismiss（历史 G4 记录与显式权威调用仍适用）。
            item["authority"] = "authoritative"
        else:
            # 参考性（SOL）复核不能降低已有条目的保护级别。v7 之前的台账条目
            # 没有 authority 字段，它们来自正式复核，必须保守按 authoritative
            # 处理；只有 advisory 复核新建的条目才带 advisory。
            item.setdefault("authority", "authoritative")
        item["severity"] = _max_severity(item.get("severity"),
                                        finding.get("severity"))
        if finding.get("file"):
            item["file"] = _text(finding.get("file"))
        if finding.get("line") is not None:
            item["line"] = finding.get("line")
        if finding.get("summary"):
            item["summary"] = _text(finding.get("summary"))
        if finding.get("root_cause"):
            value = _text(finding.get("root_cause"))
            if not _text(item.get("root_cause")):
                item["root_cause"] = value
            elif _normalize(value) != _normalize(item.get("root_cause")):
                aliases = item.setdefault("root_cause_aliases", [])
                if value not in aliases:
                    aliases.append(value)
        evidence = _text(finding.get("evidence"))
        escalated = bool(
            previous in RESOLVED_STATUSES
            and incoming_severity in BLOCKING_SEVERITIES
            and resolved_severity not in BLOCKING_SEVERITIES)
        next_status = previous
        if escalated:
            # 关闭时只是 minor/info，后续复核认为是 blocking/major：不能继续
            # 以“已关闭”跳过门禁。无论复核是否正式，都重新进入 regressed；
            # 参考性复核仍然不能把它改成 verified（那才是清除阻断）。
            next_status = "regressed"
            action = "regressed"
        elif review_status == "resolved":
            next_status = "verified"
        elif review_status == "regressed":
            next_status = "regressed"
        elif previous in RESOLVED_STATUSES:
            # 已关闭/已验证的问题没有新证据时只记为重复报告，不重开。
            if authoritative and evidence and evidence != previous_evidence:
                next_status = "regressed"
                action = "regressed"
            else:
                next_status = previous
                action = "rereported"
        else:
            next_status = "open"
        if evidence:
            item["last_evidence"] = evidence
        if action == "matched" and previous in RESOLVED_STATUSES and \
                next_status == previous:
            action = "rereported"
        if next_status != previous:
            item["status"] = next_status
            _append_history(item, next_status, at=stamp,
                            review_sha=review_sha,
                            note="复核报告 " + review_status,
                            evidence_ids=[])
            if next_status == "regressed":
                action = "regressed"
        if next_status in RESOLVED_STATUSES:
            if next_status != previous:
                item["resolved_severity"] = item.get("severity")
        else:
            item.pop("resolved_severity", None)
        item["occurrences"] = int(item.get("occurrences") or 0) + 1
        if next_status == previous:
            _append_history(item, _text(item.get("status")) or "open",
                            at=stamp, review_sha=review_sha,
                            note="review " + _text(review.get("verdict")))
        item["last_review_sha256"] = review_sha or item.get("last_review_sha256")
        if action == "rereported":
            if item["finding_id"] not in rereported:
                rereported.append(item["finding_id"])
        elif action == "regressed":
            if item["finding_id"] not in regressed:
                regressed.append(item["finding_id"])
        if item["finding_id"] not in matched:
            matched.append(item["finding_id"])
        refs.append({
            "reported_id": reported,
            "finding_id": item["finding_id"],
            "status": item.get("status"),
            "severity": item.get("severity"),
            "action": action,
            "id_conflict": id_conflict,
        })
    return {"refs": refs, "created": created, "matched": matched,
            "rereported": rereported, "regressed": regressed,
            "review_sha256": review_sha}


def get_item(state: dict, finding_id: str) -> dict | None:
    target = _text(finding_id).upper()
    if not target:
        return None
    for item in _items(state):
        if isinstance(item, dict) and \
                _text(item.get("finding_id")).upper() == target:
            return item
    return None


def transition(state: dict, finding_id: str, status: str, *,
               evidence_ids: list[str] | None = None,
               note: str = "", at: str = "") -> dict:
    """推进 Finding 生命周期；不合法的跳转直接拒绝。

    * `fixed` 需要修复证据（文件/日志/测试输出），`verified` 需要定向验证证据；
    * `closed` 只能从 `verified` 进入；`regressed` 需要说明与证据；
    * 不要求重新派发完整复核——定向测试与证据就足够验证局部修复。
    """
    value = _text(status).lower()
    if value not in FINDING_STATUSES:
        raise ReviewPolicyError(
            f"finding status 只能是 {', '.join(FINDING_STATUSES)}（实际 {value!r}）。")
    item = get_item(state, finding_id)
    if item is None:
        raise ReviewPolicyError(f"finding 台账中没有 {finding_id!r}。")
    previous = _text(item.get("status")) or "open"
    evidence = [str(eid).strip() for eid in (evidence_ids or [])
                if str(eid).strip()]
    if value == "fixed":
        if previous not in ("open", "regressed", "fixed"):
            raise ReviewPolicyError(
                f"{finding_id} 当前为 {previous}，不能直接标记 fixed。")
        if not evidence:
            raise ReviewPolicyError(
                f"{finding_id} 标记 fixed 需要至少一条修复证据（--evidence）。")
    elif value == "verified":
        if previous not in ("fixed", "regressed"):
            raise ReviewPolicyError(
                f"{finding_id} 当前为 {previous}，只能从 fixed/regressed 进入 "
                "verified；先登记修复（fixed）再运行定向验证。")
        if not evidence:
            raise ReviewPolicyError(
                f"{finding_id} 标记 verified 需要定向验证证据（测试输出/日志）。")
    elif value == "closed":
        if previous != "verified":
            raise ReviewPolicyError(
                f"{finding_id} 只能从 verified 关闭（当前 {previous}）。")
    elif value == "regressed":
        if previous == "dropped":
            raise ReviewPolicyError(f"{finding_id} 已归档，不能标记 regressed。")
        if not evidence or not _text(note):
            raise ReviewPolicyError(
                f"{finding_id} 标记 regressed 需要证据与原因说明。")
    elif value == "dropped":
        severity = _text(item.get("severity")).lower()
        authority = _text(item.get("authority")) or "authoritative"
        if severity in BLOCKING_SEVERITIES and authority != "advisory":
            # 执行方不能用一句 note 把权威复核的 blocking/major 从台账里删掉：
            # 那等于绕过台账门禁。必须先修复并定向验证，或由新的权威记录标记
            # resolved。
            raise ReviewPolicyError(
                f"{finding_id} 是 {severity}，不能由执行方直接标记 dropped；"
                "修复后用证据标记 verified，或由新的独立复核标记 resolved。"
                "如认为 finding 不成立，保留 open 并在执行结果/证据中给出反证，"
                "交由复核重新判断。")
        if not _text(note):
            raise ReviewPolicyError(f"{finding_id} 标记 dropped 需要说明原因。")
    elif value == "open":
        if not _text(note):
            raise ReviewPolicyError(f"{finding_id} 重开为 open 需要说明原因。")
    stamp = at or _now()
    item["status"] = value
    if value in RESOLVED_STATUSES:
        item["resolved_severity"] = item.get("severity")
    else:
        item.pop("resolved_severity", None)
    if value == "verified":
        item["verified_at"] = stamp
    if value == "closed":
        item["closed_at"] = stamp
    _append_history(item, value, at=stamp, note=note, evidence_ids=evidence)
    return item


def blocking_findings(state: dict) -> list[dict]:
    """未解决的 blocking/major：open、regressed、fixed（尚未验证）。

    已 resolved 的条目如果后来被复核提级为 blocking/major（例如先按 minor 关闭，
    后续发现实际是 major），不能因为“状态是 verified/dropped”就跳过；用
    `resolved_severity` 记录关闭时的严重度，提级后重新进入阻断。
    """
    out = []
    for item in _items(state):
        if not isinstance(item, dict):
            continue
        severity = _text(item.get("severity")).lower()
        if severity not in BLOCKING_SEVERITIES:
            continue
        status = _text(item.get("status"))
        if status in RESOLVED_STATUSES:
            resolved = _text(item.get("resolved_severity")).lower()
            if not resolved:
                # 旧台账没有记录关闭时严重度：按当前严重度视为当时已知。
                resolved = severity
            if resolved in BLOCKING_SEVERITIES:
                continue
            # 关闭时只是 minor/info，后来被提级为 blocking/major：重新阻断。
        out.append(item)
    out.sort(key=lambda item: (
        _severity_rank(item.get("severity")),
        _text(item.get("finding_id"))))
    return out


def pending_verification(state: dict) -> list[dict]:
    return [item for item in blocking_findings(state)
            if _text(item.get("status")) == "fixed"]


def ledger_summary(state: dict) -> dict:
    items = [item for item in _items(state)
             if isinstance(item, dict)]
    by_status: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for item in items:
        status = _text(item.get("status")) or "open"
        by_status[status] = by_status.get(status, 0) + 1
        severity = _text(item.get("severity")) or "info"
        by_severity[severity] = by_severity.get(severity, 0) + 1
    return {
        "schema": LEDGER_SCHEMA,
        "total": len(items),
        "by_status": dict(sorted(by_status.items())),
        "by_severity": dict(sorted(by_severity.items())),
        "blocking": [item["finding_id"] for item in blocking_findings(state)],
        "pending_verification": [
            item["finding_id"] for item in pending_verification(state)],
        "rereported": [item["finding_id"] for item in items
                       if int(item.get("occurrences") or 0) > 1],
        "items": items,
    }


# ── SOL 主动介入：任务认知复杂度与调用建议 ──────────────────────────────────
#
# 目标不是增加调用量，而是让真正值得认知推理的任务在**足够了解现场之后**主动获得
# 一次 SOL 帮助，同时让简单任务保持 0 调用。判断只使用状态里已有的事实：
# 风险、声明的触发因素、计划结构、证据目录、重规划与重开历史。SOL 仍是 advisory，
# 这里的任何建议都不参与 G1–G3，也不产生阻塞性工作项。
def _action_list(state: dict, *, plan_only: bool = False) -> list[dict]:
    """工作项视图；`plan_only` 只保留 ZCode 登记的计划项（排除治理派生项）。"""
    out = []
    for item in state.get("actions") or []:
        if not isinstance(item, dict):
            continue
        ident = _text(item.get("id"))
        origin = _text(item.get("origin"))
        if origin == "g4-review":
            continue  # v7 历史高风险任务遗留的工作项，不参与新判断
        if plan_only and (ident.startswith("AUTO-") or not _text(item.get("plan_id"))):
            continue
        out.append(item)
    return out


def _dependency_edges(actions: list[dict]) -> int:
    total = 0
    known = {_text(item.get("id")) for item in actions}
    for item in actions:
        for dep in item.get("depends_on") or []:
            if _text(dep) in known:
                total += 1
    return total


def _module_dirs(state: dict, workspace: str = "") -> list[str]:
    """证据所在的模块目录：优先相对任务工作区，无法归属的不计入。"""
    root = ""
    if _text(workspace):
        root = os.path.realpath(os.path.expanduser(workspace))
    dirs = set()
    for entry in state.get("evidence") or []:
        if not isinstance(entry, dict) or entry.get("dropped"):
            continue
        hint = _text(entry.get("path_hint"))
        if not hint or hint.startswith("<"):
            continue
        folder = os.path.dirname(os.path.abspath(hint))
        if root:
            folder = os.path.relpath(folder, root)
        if folder in (".", "") or folder.startswith(".."):
            continue
        dirs.add(folder)
    return sorted(dirs)


def task_complexity(state: dict, *, triggers: list[str] | None = None,
                    workspace: str = "") -> dict:
    """确定性的任务认知复杂度：只用状态事实，不调用任何模型。

    低复杂度（0 个信号）＝简单/局部改动：SOL 默认 0 调用。
    1 个信号＝中等复杂度：值得一次主动认知辅助。
    2 个及以上＝高复杂度：优先在策略/方案阶段用一次 THINK 收敛判断。
    """
    codes: list[str] = []
    details: dict[str, str] = {}

    def add(code: str, detail: str) -> None:
        if code not in codes:
            codes.append(code)
            details[code] = detail

    risk = _text(state.get("risk")).lower()
    risk = "medium" if risk == "normal" else risk
    if risk == "high":
        add("risk-high", "task.risk=high")
    requested = []
    for item in triggers or []:
        value = _text(item).lower()
        if value and value not in requested and value in ADMISSION_TRIGGERS:
            requested.append(value)
    if requested:
        add("declared-trigger", "触发因素：" + "、".join(requested))

    actions = _action_list(state, plan_only=True)
    edges = _dependency_edges(actions)
    modules = _module_dirs(state, workspace)
    if len(actions) >= 4:
        add("multi-action", f"计划工作项 {len(actions)} 个")
    if edges >= 2:
        add("interdependent", f"工作项依赖边 {edges} 条")
    if len(modules) >= 2:
        add("multi-module", "证据目录：" + "、".join(modules[:4]))
    if any(_text(item.get("origin")) == "replan"
           or _text(item.get("id")).startswith("AUTO-REPLAN")
           for item in _action_list(state)):
        add("replan-history", "存在 AUTO-REPLAN 记录")
    if (state.get("closure") or {}).get("reopen_history"):
        add("reopened", "closure.reopen_history 非空")

    count = len(codes)
    level = "low" if count == 0 else ("medium" if count == 1 else "high")
    understood = bool(actions) or bool(state.get("plans")) or \
        bool([item for item in state.get("evidence") or []
              if isinstance(item, dict) and not item.get("dropped")])
    return {
        "schema": COMPLEXITY_SCHEMA,
        "level": level,
        "signals": [{"code": code, "detail": details[code]} for code in codes],
        "signal_codes": codes,
        "counts": {
            "plan_actions": len(actions),
            "dependency_edges": edges,
            "modules": len(modules),
            "criteria": len((state.get("contract") or {}).get("criteria") or []),
            "evidence": len([item for item in state.get("evidence") or []
                             if isinstance(item, dict) and not item.get("dropped")]),
        },
        "readiness": {"understood": understood},
    }


def _record_mode(record: dict) -> str:
    """记录的认知模式：CONSULT / PLAN 是历史名称，统一归入 THINK。"""
    mode = _text(record.get("mode")).upper()
    if mode == "REVIEW":
        return "REVIEW"
    if mode in ("THINK", "CONSULT", "PLAN"):
        return "THINK"
    return "THINK" if record.get("advisory_id") else "REVIEW"


def _covers_contract(record: dict, contract_sha256: str) -> bool:
    """记录是否覆盖当前合同；历史记录缺少合同时按覆盖处理（宁可不重复付费）。"""
    recorded = _text(record.get("contract_sha256"))
    if not recorded:
        return True
    return recorded == _text(contract_sha256)


def _retry_trigger(new_codes: list[str]) -> str:
    if "replan-history" in new_codes or "risk-high" in new_codes:
        return "new-risk"
    return "scope-changed"


def _advice_command(mode: str, *, second_round: bool, trigger: str) -> str:
    if mode == "REVIEW":
        command = ("copilot_dispatch.py sol-review --state <state> "
                   "--materials <materials.md>")
    else:
        command = ("copilot_dispatch.py sol-think --state <state> "
                   "--materials <materials.md> "
                   "--question <本次真正需要 SOL 判断的事项>")
    if second_round:
        command += f" --force --trigger {trigger} --reason <为什么需要再判断>"
    return command


def sol_advice(state: dict, *, calls: list[dict] | None = None,
               triggers: list[str] | None = None, gates_passed: bool = False,
               user_request: bool = False, review_requested: bool = False,
               workspace: str = "") -> dict:
    """SOL 主动介入建议（只读建议，不是门禁，也不派生工作项）。

    SOL 是默认认知引擎，但不是按阶段运行的流程：

    * 简单/局部任务（0 信号）→ 0 调用，即使进入 PASS/CLOSED 也不因阶段调用；
    * 中高复杂任务在“已形成初步理解”（有计划或证据）后建议一次 THINK；
    * 同一合同已有有效认知结论且没有升级型新信号 → 复用，不重复付费；
    * 只有出现升级型信号（风险升级、方案失败重规划、任务重开）才允许追加；
    * REVIEW 只在用户明确要求时建议一次，不由阶段或风险自动派生。
    """
    profile = task_complexity(state, triggers=triggers, workspace=workspace)
    if user_request:
        # 用户显式要求是最高优先级事实：不受复杂度判断限制，但仍只是 advisory。
        codes = list(profile["signal_codes"])
        if "user-request" not in codes:
            codes.append("user-request")
            profile["signals"].append({"code": "user-request",
                                       "detail": COMPLEXITY_SIGNALS["user-request"]})
        profile["signal_codes"] = codes
        profile["level"] = "high"

    completed = [item for item in calls or []
                 if isinstance(item, dict) and item.get("status") == "completed"]
    contract_sha = _text(state.get("contract_sha256"))
    same = [item for item in completed if _covers_contract(item, contract_sha)]
    covered = {mode: any(_record_mode(item) == mode for item in same)
               for mode in ("THINK", "REVIEW")}
    # CLI 的 single_round_decision 按“同模式是否有任何已完成记录”要求 --force，
    # 不按合同过滤。建议命令必须与真实准入一致，否则合同修订后给出的命令会被
    # 直接拒绝（sol_review_requires_reason）。
    attempted = {mode: any(_record_mode(item) == mode for item in completed)
                 for mode in ("THINK", "REVIEW")}
    last = None
    if same:
        last = max(same, key=lambda item: _text(item.get("created_at")))
    # 旧记录没有信号快照时不做差异判断：按“已覆盖”处理，避免升级后重复付费。
    new_codes: list[str] = []
    if last is not None and "complexity_signals" in last:
        recorded = {_text(code) for code in last.get("complexity_signals") or []}
        new_codes = [code for code in profile["signal_codes"]
                     if code in SOL_ESCALATION_SIGNALS and code not in recorded]
    risk = _text(state.get("risk")).lower()
    level = profile["level"]
    base = {
        "schema": SOL_ADVICE_SCHEMA,
        "complexity": profile,
        "covered": covered,
        "new_signals": new_codes,
        "second_round": bool(last is not None),
        "trigger": _retry_trigger(new_codes) if last is not None else "",
        "action": "none",
        "recommended_action": "not_needed",
        "mode": "",
        "phase": "none",
        "reason": "",
        "command": "",
    }
    if bool((state.get("closure") or {}).get("closed")):
        base["reason"] = "任务已收口：不再建议新的 SOL 调用（需继续时先由用户授权 reopen）。"
        return base
    if any(not block.get("resolved_at") and not block.get("work_items")
           for block in state.get("blocks") or [] if isinstance(block, dict)):
        # 任务级阻塞只能由用户补齐授权/决策/资源解除；SOL 不提供本地事实，也不能
        # 解除阻塞。项级阻塞只冻结对应工作项，其余工作照常推进，不因此停用 SOL。
        base["reason"] = ("任务处于 BLOCKED：先解除授权/决策/资源缺口；SOL 不提供本地事实，"
                          "也不能替代用户决定。")
        return base
    if level == "low" and not user_request and not review_requested:
        base["reason"] = ("简单/局部任务（无中高复杂信号）：主代理自检、diff 与必要"
                          "测试闭环，SOL 保持 0 调用。")
        return base
    if not profile["readiness"]["understood"]:
        base["reason"] = ("尚未形成可判断的现场理解（没有计划或证据）；先调查并登记"
                          "计划/证据，再决定是否调用 SOL。")
        return base
    if review_requested:
        if covered["REVIEW"] and not new_codes:
            base.update({
                "action": "reuse", "mode": "REVIEW",
                "reason": "用户要求的独立复核已覆盖当前合同：直接复用，不重复调用。",
            })
            return base
        # 只有已有 REVIEW 才是第二轮；已有 THINK 不要求 REVIEW 带 --force。
        base["second_round"] = attempted["REVIEW"]
        if attempted["REVIEW"]:
            base["trigger"] = "new-risk" if new_codes else "user-request"
        base.update({
            "action": "review", "recommended_action": "recommended",
            "mode": "REVIEW", "phase": "review",
            "reason": ("用户明确要求独立复核：执行一次 SOL REVIEW（advisory）；"
                       "结论仍是建议，不构成收口门禁。"),
            "command": _advice_command("REVIEW", second_round=attempted["REVIEW"],
                                       trigger="new-risk" if new_codes
                                       else "user-request"),
        })
        return base
    if covered["THINK"]:
        if new_codes and not user_request:
            base.update({
                "action": "think", "recommended_action": "recommended",
                "mode": "THINK", "phase": "development",
                "reason": ("出现新的认知信号（" + "、".join(new_codes)
                           + "）：允许追加一次 SOL 判断；其余情况复用已有结论。"),
                "command": _advice_command("THINK", second_round=True,
                                           trigger=base["trigger"]),
            })
            return base
        if user_request:
            base.update({
                "action": "think", "recommended_action": "recommended",
                "mode": "THINK", "phase": "development",
                "reason": ("用户明确要求 SOL 判断：直接调用一次（advisory）；"
                           "即使已有结论也按用户要求执行。"),
                "command": _advice_command("THINK", second_round=True,
                                           trigger="user-request"),
            })
            return base
        base.update({
            "action": "reuse", "mode": "THINK",
            "reason": ("同一合同已有有效 SOL 认知结论且没有新的升级型信号：直接复用，"
                       "不重复调用。"),
        })
        return base
    if gates_passed:
        if risk == "high" or level == "high":
            base["second_round"] = attempted["THINK"]
            if attempted["THINK"]:
                base["trigger"] = base["trigger"] or "scope-changed"
            base.update({
                "action": "think", "recommended_action": "optional",
                "mode": "THINK", "phase": "closure",
                "reason": ("G1–G3 已通过；高风险或高复杂任务可在收口前做一次整体"
                           "判断（advisory，可选，不改变 PASS 判定）。"),
                "command": _advice_command(
                    "THINK", second_round=attempted["THINK"],
                    trigger=base["trigger"] if attempted["THINK"] else ""),
            })
            return base
        base["reason"] = ("G1–G3 已通过且没有新的认知不确定性：不因阶段自动调用 "
                          "SOL。")
        return base
    base["second_round"] = attempted["THINK"]
    if attempted["THINK"]:
        base["trigger"] = base["trigger"] or "scope-changed"
    base.update({
        "action": "think",
        "recommended_action": "recommended",
        "mode": "THINK",
        "phase": "development",
        "reason": ("中高复杂任务已形成初步理解：建议调用一次 SOL 做整体判断"
                   "（问题本质、策略、取舍、风险与验证信号）；是否采纳由主代理判断。"),
        "command": _advice_command(
            "THINK", second_round=attempted["THINK"],
            trigger=base["trigger"] if attempted["THINK"] else ""),
    })
    if user_request:
        base["reason"] = ("用户明确要求 SOL 判断：直接调用一次（advisory）；"
                          "不受复杂度判断限制。")
    return base

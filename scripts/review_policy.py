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
ADMISSION_SCHEMA = "copilot-review-admission/v1"
STOP_SCHEMA = "copilot-review-stop/v1"

# 台账状态：fixed 只是“已改”，verified 才表示修复被定向验证；只有
# verified/closed/dropped 不再阻断。regressed 表示已修复问题又复现。
FINDING_STATUSES = ("open", "fixed", "verified", "closed", "regressed", "dropped")
RESOLVED_STATUSES = {"verified", "closed", "dropped"}
BLOCKING_SEVERITIES = {"blocking", "major"}
SEVERITY_ORDER = {"blocking": 0, "major": 1, "minor": 2, "info": 3}

# 复核层级：按任务影响、失败后果、变更范围与不确定性划分，不引入评分模型。
# `normal` 是历史风险标签，语义等同于 `medium`；读取旧状态时按 medium 处理。
REVIEW_TIERS = ("low", "medium", "high")
RISK_TIER = {"low": "low", "medium": "medium", "normal": "medium", "high": "high"}

# 中风险任务按需复核的触发因素；没有触发因素时默认不派 Reviewer。
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

# 已经有一次 PASS 之后，允许重新完整复核的实质触发因素。
REENTRY_TRIGGERS = {
    "blocking-fix": "修复了 blocking finding",
    "major-fix": "修复了 major finding",
    "code-changed": "关键代码范围实质变化",
    "scope-expanded": "变更范围实质扩大",
    "evidence-changed": "最终证据或完成条件发生实质变化",
    "risk-changed": "风险判断发生变化",
    "security-fix": "安全/权限修复改变了整体结论",
    "concurrency-fix": "并发/事务修复改变了整体结论",
}

# 准入触发因素 → 重入触发因素：同一风险在首次与重入阶段使用不同词汇，
# 不因叫法差异把合法的重入请求判成非法。
TRIGGER_ALIASES = {
    "security": "security-fix",
    "permissions": "security-fix",
    "core-logic": "code-changed",
    "data-integrity": "concurrency-fix",
    "architecture": "scope-expanded",
    "global-execution": "scope-expanded",
    "production": "risk-changed",
    "release": "risk-changed",
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
        # 同一复核重复合并：不重复累计 occurrence，也不重复写历史。
        already = any(entry.get("review_sha256") == review_sha
                      for entry in item.get("history") or []
                      if isinstance(entry, dict))
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
        previous = _text(item.get("status")) or "open"
        incoming_severity = _text(finding.get("severity")).lower()
        resolved_severity = _text(item.get("resolved_severity")).lower()
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
        if not already:
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


def tier_for(risk: object) -> str:
    """把风险标签归一化为复核层级；缺失或未知标签失败关闭，不猜测。"""
    value = _text(risk).lower()
    tier = RISK_TIER.get(value)
    if tier is None:
        raise ReviewPolicyError(
            f"风险等级 {value!r} 缺失或未知；只能是 {' / '.join(REVIEW_TIERS)}"
            "（历史 normal 等价于 medium）。")
    return tier


def admission(*, risk: str = "medium", triggers: list[str] | None = None,
              reason: str = "") -> dict:
    """风险准入：按 low/medium/high 三级决定是否需要独立复核。

    * `low`：文档/文案/样式/简单修复/小范围既有模式改动——主代理自检闭环，
      默认不派 Reviewer；显式声明的实质触发因素仍然优先。
    * `medium`（含历史 `normal`）：普通开发/多页面 UI/常规重构——主代理完成
      开发与验证；只有声明实质触发因素时才建议一次定向只读复核。
    * `high`：建议执行一次 SOL 零工具单轮复核；它是 advisory，不是收口门禁，
      但高风险任务仍须完成 G1–G3 与台账闭合。

    “针对性调查”不是正式复核：普通子 Agent 咨询、`Explore` 调查不会创建
    复核记录或台账。
    """
    tier = tier_for(risk)
    risk_value = _text(risk).lower()
    requested = []
    for item in triggers or []:
        value = _text(item).lower()
        if value and value not in requested:
            requested.append(value)
    unknown = [item for item in requested
               if item not in ADMISSION_TRIGGERS and item not in REENTRY_TRIGGERS]
    if unknown:
        raise ReviewPolicyError(
            "未知复核触发因素：" + ", ".join(unknown) + "；可选："
            + ", ".join(sorted(set(ADMISSION_TRIGGERS) | set(REENTRY_TRIGGERS)))
            + "。")
    base = {"schema": ADMISSION_SCHEMA, "risk": risk_value, "tier": tier,
            "triggers": requested, "reason": _text(reason)[:1000]}
    if tier == "high":
        return {**base, "decision": "recommended", "scope": "sol",
                "detail": "高风险任务建议执行一次 SOL 零工具单轮独立复核（advisory）；"
                          "SOL 不构成收口门禁，也不证明宿主只读权限。"}
    if requested:
        return {**base, "decision": "required", "scope": "advisory",
                "detail": "声明了实质风险或重入触发因素，建议派发一次定向只读复核；"
                          "针对性调查本身不等于正式复核。"}
    if tier == "low":
        return {**base, "decision": "not_required", "scope": "none",
                "detail": "低风险任务（文档/文案/样式/简单修复/小范围既有模式改动）："
                          "由主代理自检、diff 与必要测试闭环，不派发 Reviewer。"}
    return {**base, "decision": "not_required", "scope": "none",
            "detail": "中风险普通开发任务：由主代理完成开发与验证，只在声明实质"
                      "触发因素时才派发一次定向只读复核；不强制完整复核。"}


def last_stop(state: dict) -> dict | None:
    stops = [item for item in state.get("review_stops") or []
             if isinstance(item, dict)]
    if not stops:
        return None
    return max(stops, key=lambda item: _text(item.get("created_at")))


def record_stop(state: dict, review: dict, *, code_scope_sha256: str = "",
                at: str = "", authoritative: bool = True) -> dict | None:
    """为一次 PASS 记录停止标记；同一复核只记录一次（幂等）。"""
    if _text(review.get("verdict")).lower() != "pass":
        return None
    findings = [item for item in review.get("findings") or []
                if isinstance(item, dict)]
    if any(_text(item.get("severity")).lower() == "blocking"
           for item in findings):
        return None
    if review.get("unverified"):
        return None
    review_sha = _text(review.get("result_sha256"))
    stops = state.setdefault("review_stops", [])
    if not isinstance(stops, list):
        stops = []
        state["review_stops"] = stops
    for item in stops:
        if isinstance(item, dict) and \
                _text(item.get("review_sha256")) == review_sha:
            return item
    refs = [item for item in review.get("finding_refs") or []
            if isinstance(item, dict)]
    stop = {
        "schema": STOP_SCHEMA,
        "stop_id": "RST" + sha256_text(
            review_sha + "|" + _text(review.get("target_sha256")))[:20],
        "task_id": _text(review.get("task_id")),
        "work_item_id": _text(review.get("work_item_id")),
        "review_sha256": review_sha,
        "target_sha256": _text(review.get("target_sha256")),
        "code_scope_sha256": _text(code_scope_sha256),
        "verdict": "pass",
        "authoritative": bool(authoritative),
        "findings": {
            "blocking": sum(1 for item in findings
                            if _text(item.get("severity")) == "blocking"),
            "major": sum(1 for item in findings
                         if _text(item.get("severity")) == "major"),
            "minor": sum(1 for item in findings
                         if _text(item.get("severity")) == "minor"),
            "info": sum(1 for item in findings
                        if _text(item.get("severity")) == "info"),
        },
        "minor_ids": [item.get("finding_id") for item in refs
                      if _text(item.get("severity")) in ("minor", "info")],
        "created_at": at or _text(review.get("received")) or _now(),
    }
    stops.append(stop)
    return stop


def reentry_decision(state: dict, *, target: str,
                     triggers: list[str] | None = None,
                     reason: str = "") -> dict:
    """判断已有 PASS 之后是否允许再次独立复核。

    * 目标摘要未变且没有声明触发因素：直接复用上轮结论（`review_not_needed`）。
    * 目标实质变化：必须声明 `REENTRY_TRIGGERS` 之一并写入原因，避免用
      “再来一轮”掩盖非收敛。局部修复优先用定向测试/证据验证。
    """
    requested = []
    for item in triggers or []:
        value = _text(item).lower()
        if value and value not in requested:
            requested.append(value)
    effective = [TRIGGER_ALIASES.get(item, item) for item in requested]
    unknown = [item for item in effective if item not in REENTRY_TRIGGERS]
    stop = last_stop(state)
    if stop is None:
        # 首次复核：triggers 属于风险准入词汇（security/core-logic…），不是
        # 重入触发因素，不能拿 REENTRY_TRIGGERS 去拒绝。
        return {"allowed": True, "code": "", "detail": "首次复核，无停止标记。",
                "triggers": requested, "stop_id": ""}
    if unknown:
        return {"allowed": False, "code": "review_reentry_trigger_invalid",
                "detail": "未知重入触发因素：" + ", ".join(unknown)
                          + "；可选：" + ", ".join(sorted(REENTRY_TRIGGERS)) + "。",
                "triggers": requested, "stop_id": stop.get("stop_id")}
    same_target = _text(stop.get("target_sha256")) == _text(target)
    if same_target and not requested:
        return {"allowed": False, "code": "review_not_needed",
                "detail": "当前目标摘要与上轮 PASS 完全相同；直接复用该结论，"
                          "不重新派发复核。如确有新的实质风险，用 --trigger "
                          "显式声明重入原因。",
                "triggers": requested, "stop_id": stop.get("stop_id")}
    if not requested:
        return {"allowed": False, "code": "review_reentry_requires_trigger",
                "detail": "已有 PASS 且目标发生变化；重新完整复核前必须声明实质"
                          "触发因素（--trigger）与原因（--reason）。局部修复优先用"
                          "定向测试/证据验证，不要默认重跑完整复核。",
                "triggers": requested, "stop_id": stop.get("stop_id")}
    return {"allowed": True, "code": "",
            "detail": "允许重新复核：" + "、".join(
                REENTRY_TRIGGERS[item] for item in effective),
            "triggers": requested, "stop_id": stop.get("stop_id"),
            "reason": _text(reason)[:1000]}

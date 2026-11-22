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
* 纯文档/格式调整不需要独立复核；高风险任务在 CONSULT 建议里体现，REVIEW 只在
  用户明确要求时执行（两者都是 advisory，不是收口门禁）。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re

LEDGER_SCHEMA = "copilot-finding-ledger/v1"
COMPLEXITY_SCHEMA = "copilot-task-complexity/v2"
SOL_ADVICE_SCHEMA = "copilot-sol-advice/v4"

# 任务认知复杂度只使用状态里已有的确定性事实：不引入第二个模型，也不引入
# 评分模型。每个信号都对应一条可复核的现场证据；0 个信号即低复杂度。
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
# 任务类型不是“复杂度信号”，而是默认路由前提：非简单类型始终进入 SOL 认知
# 流程（至少一次 CONSULT），简单类型只有出现真实升级信号才进入。这样默认
# standard 任务不会仅凭类型被抬到 high，也不会默认跳过复杂任务。

# 只有“升级型”信号才算认知对象发生实质变化、允许第二次调用。结构型信号
# （工作项数量、依赖、目录、声明的触发因素）只用于判断复杂度：开发过程中证据
# 目录或计划条目增加不代表需要重新判断；范围变化走合同修订（合同指纹变化即
# 旧结论不再覆盖）。
SOL_ESCALATION_SIGNALS = ("risk-high", "replan-history", "reopened")

# ── 生命周期事件（v9.6 主动调度）──────────────────────────────────────────
# 事件是“认知窗口”的稳定身份，不是复杂度信号。每个事件必须有一条可归因的现场
# 判据：要么来自状态里已存在的确定性事实（工作项/条件/台账/合同/重开/风险），
# 要么来自主代理显式登记的影响判断。普通证据追加、备注措辞变化、常规验收进展
# 都不构成事件，避免把“有变化”误当成“需要重新判断”。
SOL_EVENTS = {
    "startup": "任务启动：理解目标、隐含需求、问题本质与主要风险",
    "plan-needed": "调查已形成现场事实且存在实质方案决策",
    "material-evidence-change": "实质证据变化：已验证结论被推翻或修复复现",
    "decision-point": "出现需要取舍的决策点",
    "risk-escalation": "风险等级实质上升",
    "replan-history": "方案失效并重新规划",
    "scope-changed": "目标、范围或合同实质变化",
    "reopened": "任务重新打开",
    "milestone": "medium/high 任务的重要阶段里程碑",
    "closure": "最终收口综合判断",
}
# 事件到生命周期窗口的归属由生命周期判断自身给出（义务里带 family）；
# 六类动态事件在执行期按新增事实追加，不与启动/规划/里程碑/收口重复计数。
SOL_FAMILIES = ("A", "B", "C", "D", "E")
# 事件 → 追加调用时使用的 --trigger（与 single_round 的触发口径一致）。
SOL_EVENT_TRIGGERS = {
    "material-evidence-change": "new-evidence",
    "decision-point": "new-evidence",
    "risk-escalation": "new-risk",
    "replan-history": "new-risk",
    "scope-changed": "scope-changed",
    "reopened": "scope-changed",
    "startup": "new-evidence",
    "plan-needed": "new-evidence",
    "milestone": "stage-result",
    "closure": "stage-result",
}

# 台账状态：fixed 只是“已改”，verified 才表示修复被定向验证；只有
# verified/closed/dropped 不再阻断。regressed 表示已修复问题又复现。
FINDING_STATUSES = ("open", "fixed", "verified", "closed", "regressed", "dropped")
TERMINAL_STATUSES = {"done", "dropped"}
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

# 任务类型只用于判断“是否默认进入 SOL 认知流程”，不是工作量估算，也不是
# 新的门禁。只有同时满足目标明确、范围很小、实现方式明确、没有重要取舍、
# 风险低、不需要复杂分析的任务才允许归入简单类型；未声明的任务按 standard
# 处理（非简单），复杂度不确定时默认调用 SOL。
TASK_KINDS = {
    "trivial": "单个文字、注释或符号修改",
    "mechanical": "已知原因的简单机械修复或常规维护",
    "docs": "文档、说明或格式调整",
    "format": "纯格式、排版或样式调整",
    "standard": "未声明的普通任务（默认非简单）",
    "feature": "多步骤功能开发",
    "bugfix": "缺陷调查与复杂修复",
    "refactor": "多模块代码改造或重构",
    "architecture": "架构调整或共享基础设施",
    "performance": "性能优化",
    "research": "技术方案研究或调查",
    "product-design": "产品/交互设计",
    "data-infra": "数据链路、数据库或基础设施调整",
    "governance": "工程治理、规则或 Skill 升级",
    "release": "发布、迁移或不可逆操作",
    "multi-option": "多方案比较与选型",
}
SIMPLE_TASK_KINDS = ("trivial", "mechanical", "docs", "format")
DEFAULT_TASK_KIND = "standard"
# 这些任务类型天然需要“调查后规划”，但 PLAN 仍要求已出现现场事实。
PLAN_KINDS = ("architecture", "data-infra", "governance", "product-design",
              "multi-option", "release", "refactor", "performance", "research")


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


def task_kind(state: dict) -> str:
    """任务类型：未声明时按 standard（非简单）处理，未知类型同样按非简单。"""
    value = _text(state.get("task_kind")).lower()
    return value if value in TASK_KINDS else DEFAULT_TASK_KIND


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
    2 个及以上＝高复杂度：优先在策略/方案阶段用一次判断收敛。

    未声明或声明为标准类型时按非简单处理：复杂度不确定时默认调用 SOL，而不是
    默认跳过。简单类型也必须由风险、触发因素、计划结构与证据范围共同确认。
    """
    codes: list[str] = []
    details: dict[str, str] = {}

    def add(code: str, detail: str) -> None:
        if code not in codes:
            codes.append(code)
            details[code] = detail

    kind = task_kind(state)
    non_simple = kind not in SIMPLE_TASK_KINDS
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
        "task_kind": kind,
        "non_simple": non_simple,
        "counts": {
            "plan_actions": len(actions),
            "done_actions": len([item for item in actions
                                 if _text(item.get("status")) in TERMINAL_STATUSES]),
            "dependency_edges": edges,
            "modules": len(modules),
            "criteria": len((state.get("contract") or {}).get("criteria") or []),
            "evidence": len([item for item in state.get("evidence") or []
                             if isinstance(item, dict) and not item.get("dropped")]),
        },
        "readiness": {"understood": understood},
    }


def _record_mode(record: dict) -> str:
    """记录的模式：THINK 归入 CONSULT；CONSULT / PLAN / REVIEW 各自独立。"""
    mode = _text(record.get("mode")).upper()
    if mode == "REVIEW":
        return "REVIEW"
    if mode == "PLAN":
        return "PLAN"
    if mode == "CONSULT" or mode == "THINK":
        return "CONSULT"
    return "CONSULT" if record.get("advisory_id") else "REVIEW"


def _covers_contract(record: dict, contract_sha256: str) -> bool:
    """记录是否覆盖当前合同；历史记录缺少合同时按覆盖处理（宁可不重复付费）。"""
    recorded = _text(record.get("contract_sha256"))
    if not recorded:
        return True
    return recorded == _text(contract_sha256)


def _call_stage(record: dict) -> str:
    """记录所处的生命周期阶段；旧记录没有阶段时按 development 处理。"""
    return _text(record.get("stage")) or _text(record.get("phase")) \
        or "development"


def sol_progress_key(state: dict) -> str:
    """阶段复盘的进度键：工作项状态、证据身份与条件状态的最小投影。

    同一进度下不重复建议阶段复盘；新增证据、工作项推进或条件变化后，才构成
    新的阶段成果。它只用于去重，不是证据摘要，也不进入门禁。
    """
    rows = {
        "actions": sorted([[str(item.get("id") or ""),
                            str(item.get("status") or "")]
                           for item in state.get("actions") or []
                           if isinstance(item, dict)]),
        "evidence": sorted(str(item.get("id") or "")
                           for item in state.get("evidence") or []
                           if isinstance(item, dict) and not item.get("dropped")),
        "criteria": sorted([[str(item.get("criterion_id") or ""),
                             str(item.get("status") or "")]
                            for item in
                            (state.get("contract") or {}).get("criteria") or []
                            if isinstance(item, dict)]),
    }
    return sha256_text(canonical_json(rows))


def _retry_trigger(new_codes: list[str]) -> str:
    if "risk-high" in new_codes or "replan-history" in new_codes:
        return "new-risk"
    if "reopened" in new_codes:
        return "scope-changed"
    return "new-evidence"


def has_substantive_progress(state: dict) -> bool:
    """是否存在实质工程进展：终态工作项、已核销条件或已登记证据。

    收口综合判断与覆盖率分母都用它，避免“刚建任务就谈收口”的空转咨询，也让
    “有实质进展的非简单任务”有一个可复核的口径。
    """
    if any(_text(item.get("status")) in TERMINAL_STATUSES
           for item in state.get("actions") or [] if isinstance(item, dict)):
        return True
    if any(_text(item.get("status")) == "met"
           for item in (state.get("contract") or {}).get("criteria") or []
           if isinstance(item, dict)):
        return True
    return bool([item for item in state.get("evidence") or []
                 if isinstance(item, dict) and not item.get("dropped")])


def declared_triggers(state: dict) -> list[str]:
    """任务声明的准入触发因素（唯一读取口径，避免建议与统计各取一次）。"""
    return [_text(item.get("name"))
            for item in state.get("admission_triggers") or []
            if isinstance(item, dict) and _text(item.get("name"))]


def plan_admission(state: dict, *, triggers: list[str] | None = None,
                   workspace: str = "") -> dict:
    """PLAN 准入的唯一判定口径（建议与覆盖率统计共用）。

    不再由工作项数量单独决定：方案类任务类型、中/高复杂、多工作项都构成
    “存在实质方案决策”。单工作项任务只要命中任一条件也要规划；只有既无信号、
    又无第二项工作、也不是方案类任务时才跳过。
    """
    if triggers is None:
        # 调用方未显式传入时读取任务自己声明的触发因素：准入只有一个口径，
        # 不能因为调用方忘了传就漏掉“声明触发”这条信号。
        triggers = declared_triggers(state)
    profile = task_complexity(state, triggers=triggers, workspace=workspace)
    actions = _action_list(state, plan_only=True)
    planned = len(actions)
    done = len([item for item in actions
                if _text(item.get("status")) in TERMINAL_STATUSES])
    understood = bool(profile["readiness"]["understood"])
    kind = profile["task_kind"]
    required = (profile["level"] in ("medium", "high") or planned >= 2
                or kind in PLAN_KINDS)
    return {
        "required": bool(required and understood),
        "understood": understood,
        "level": profile["level"],
        "task_kind": kind,
        "planned": planned,
        "done": done,
        "non_simple": bool(profile.get("non_simple")),
        "reason": ("方案类任务、中/高复杂或多工作项" if required
                   else "既无方案类信号也没有第二项工作"),
    }


def sol_event_snapshot(state: dict) -> dict:
    """事件判据的确定性快照，用于与上次认知调用比较是否出现新的认知对象。

    只使用状态里已有的可归因事实：台账中复现的 finding、曾经 met 又被回退的
    条件、重规划工作项、显式登记的决策（新增即为新的决策窗口）、合同指纹、风险
    等级、重开次数。它不读 history、不解析自由文本，也不把证据数量当作认知价值。
    """
    ledger = state.get("finding_ledger") or {}
    regressed = sorted(
        str(item.get("finding_id") or "")
        for item in (ledger.get("items") or [])
        if isinstance(item, dict) and _text(item.get("status")) == "regressed")
    met = sorted(
        str(item.get("criterion_id") or "")
        for item in (state.get("contract") or {}).get("criteria") or []
        if isinstance(item, dict) and _text(item.get("status")) == "met")
    replans = sorted(
        str(item.get("id") or "")
        for item in state.get("actions") or []
        if isinstance(item, dict)
        and (_text(item.get("origin")) == "replan"
             or str(item.get("id") or "").startswith("AUTO-REPLAN")))
    decisions = sorted(
        str(item.get("decision_id") or f"D{index + 1}")
        for index, item in enumerate(state.get("decisions") or [])
        if isinstance(item, dict))
    reopen_seq = len((state.get("closure") or {}).get("reopen_history") or [])
    risk = _text(state.get("risk")).lower()
    return {
        "regressed": regressed,
        "met": met,
        "replans": replans,
        "decisions": decisions,
        "contract_sha256": _text(state.get("contract_sha256")),
        "risk": "medium" if risk == "normal" else risk,
        "reopen_seq": reopen_seq,
    }


def closure_key(state: dict) -> str:
    """收口判断的去重键：合同指纹 + 重开次数（一次收口周期一个键）。

    合同修订或重开会开启新的收口周期，旧收口结论不再覆盖新周期；除此之外不
    因证据增删或时间推移重复付费。调用记录与建议使用同一个函数，避免两处口径
    漂移导致“刚收口完又建议收口”。
    """
    snapshot = sol_event_snapshot(state)
    return sha256_text(canonical_json(
        [snapshot["contract_sha256"], snapshot["reopen_seq"]]))[:16]


def _new_items(current: list[str], previous: list[str]) -> list[str]:
    known = set(previous or [])
    return [item for item in current if item not in known]


def sol_events(state: dict, snapshot: dict, last: dict | None) -> list[dict]:
    """当前成立的生命周期事件及其“是否新增”。

    `last` 是上一次认知调用保存的事件快照。没有快照的旧记录退回复杂度信号口径，
    避免升级后对同一认知对象重复付费。
    """
    rows: list[dict] = []
    previous = last if isinstance(last, dict) else None
    if previous is None:
        return rows
    new_regressed = _new_items(snapshot["regressed"],
                               list(previous.get("regressed") or []))
    # 曾经 met 的条件回退：既有结论被推翻，这是实质证据变化，不是普通进展。
    invalidated = [cid for cid in (previous.get("met") or [])
                   if cid not in set(snapshot["met"])]
    if new_regressed or invalidated:
        parts = []
        if new_regressed:
            parts.append("修复后复现的 finding：" + "、".join(new_regressed))
        if invalidated:
            parts.append("回退为未核销的完成条件：" + "、".join(sorted(invalidated)))
        rows.append({
            "code": "material-evidence-change",
            "instance": sha256_text(canonical_json(
                [new_regressed, sorted(invalidated)]))[:16],
            "detail": "；".join(parts),
            "facts": {"regressed": new_regressed,
                      "invalidated": sorted(invalidated)},
        })
    new_decisions = _new_items(snapshot["decisions"],
                               list(previous.get("decisions") or []))
    if new_decisions:
        rows.append({
            "code": "decision-point",
            "instance": sha256_text(canonical_json(new_decisions))[:16],
            "detail": "新出现的未决决策窗口：" + "、".join(new_decisions),
            "facts": {"decision_ids": new_decisions},
        })
    if snapshot["risk"] == "high" and previous.get("risk") != "high":
        rows.append({
            "code": "risk-escalation", "instance": "high",
            "detail": "任务风险等级上升到 high",
            "facts": {"previous_risk": previous.get("risk") or ""},
        })
    new_replans = _new_items(snapshot["replans"],
                             list(previous.get("replans") or []))
    if new_replans:
        rows.append({
            "code": "replan-history",
            "instance": sha256_text(canonical_json(new_replans))[:16],
            "detail": "出现重规划工作项：" + "、".join(new_replans),
            "facts": {"replans": new_replans},
        })
    if snapshot["contract_sha256"] and previous.get("contract_sha256") \
            and snapshot["contract_sha256"] != previous["contract_sha256"]:
        rows.append({
            "code": "scope-changed",
            "instance": snapshot["contract_sha256"][:16],
            "detail": "合同指纹变化：旧结论不再覆盖当前合同",
            "facts": {"contract_sha256": snapshot["contract_sha256"]},
        })
    if int(snapshot["reopen_seq"]) > int(previous.get("reopen_seq") or 0):
        rows.append({
            "code": "reopened",
            "instance": f"reopen-{int(snapshot['reopen_seq'])}",
            "detail": f"任务第 {int(snapshot['reopen_seq'])} 次重开",
            "facts": {"reopen_seq": int(snapshot["reopen_seq"])},
        })
    return rows


def _signals_to_events(codes: list[str]) -> list[dict]:
    """旧记录没有事件快照时的兼容口径：把升级型信号映射成同义事件。"""
    rows = []
    for code in codes:
        event = {"risk-high": "risk-escalation",
                 "replan-history": "replan-history",
                 "reopened": "reopened"}.get(code)
        if event:
            rows.append({"code": event, "instance": code,
                         "detail": SOL_EVENTS[event], "facts": {}})
    return rows


def _advice_command(mode: str, *, second_round: bool, trigger: str,
                    stage: str = "") -> str:
    if mode == "REVIEW":
        command = ("copilot_dispatch.py sol-review --state <state> "
                   "--materials <materials.md>")
    elif mode == "PLAN":
        command = ("copilot_dispatch.py sol-plan --state <state> "
                   "--materials <materials.md> "
                   "--question <本次需要规划判断的事项>")
    else:
        stage_value = stage or "development"
        command = ("copilot_dispatch.py sol-consult --state <state> "
                   "--materials <materials.md> --stage " + stage_value +
                   " --question <本次真正需要 SOL 判断的事项>")
    if second_round:
        command += f" --force --trigger {trigger} --reason <为什么需要再判断>"
    return command


def sol_advice(state: dict, *, calls: list[dict] | None = None,
               triggers: list[str] | None = None, gates_passed: bool = False,
               closure_ready: dict | None = None,
               user_request: bool = False, review_requested: bool = False,
               workspace: str = "") -> dict:
    """SOL 默认认知生命周期建议（只读建议，不是门禁，也不派生工作项）。

    生命周期与调用策略（v9.6 事件驱动）：

    1. 简单/机械任务（无信号且类型明确简单）→ 0 调用；复杂度不确定时按非简单处理。
    2. A 启动：非简单任务 → 一次 CONSULT（目标理解、问题本质、约束与主要风险）。
    3. B 规划：已形成现场事实且存在实质方案决策 → 一次 PLAN；准入不再由工作项
       数量单独决定（方案类任务、声明触发、中/高复杂、多工作项任一即可）。
    4. C 执行期：出现可归因的新事件（实质证据变化、决策点、风险上升、重规划、
       合同修订、重开）→ 追加 CONSULT；普通证据追加与常规进展不触发。
    5. D 里程碑：medium/high 任务完成实质工作后 → 可选阶段复盘（按 progress_key
       去重，不按时间或证据条数）。
    6. E 收口：有实质进展的非简单任务在最终验证后 → 收口综合判断（按收口周期
       去重）；SOL 不拥有 PASS 批准权，也不是门禁。
    7. REVIEW 只由用户明确要求，不由阶段或风险自动派生。

    输出中的 `family` / `event` / `instance` / `level` 是调度层持久化“义务”的稳定
    身份：同一 (合同, family, event, instance) 只应存在一个义务，重复评估只会
    刷新同一个义务，不会产生新的付费对象。这里不设调用次数上限。
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
    consult_calls = [item for item in same if _record_mode(item) == "CONSULT"]
    plan_calls = [item for item in same if _record_mode(item) == "PLAN"]
    review_calls = [item for item in same if _record_mode(item) == "REVIEW"]
    covered = {
        "CONSULT": bool(consult_calls),
        "PLAN": bool(plan_calls),
        "REVIEW": bool(review_calls),
    }
    # 兼容旧输出：历史 THINK 记录视为 CONSULT 已覆盖。
    covered["THINK"] = covered["CONSULT"]
    # CLI 的 single_round_decision 按“同模式是否有任何已完成记录”要求 --force，
    # 不按合同过滤。建议命令必须与真实准入一致，否则合同修订后给出的命令会被
    # 直接拒绝（sol_review_requires_reason）。
    attempted = {mode: any(_record_mode(item) == mode for item in completed)
                 for mode in ("CONSULT", "PLAN", "REVIEW")}
    last_consult = None
    if consult_calls:
        last_consult = max(consult_calls,
                           key=lambda item: _text(item.get("created_at")))
    # 事件基线必须取“最近一次真实判断”，即使它是在旧合同下做的：合同修订正是
    # 靠与旧基线比较才能判为 scope-changed。只按覆盖过滤会让修订后的第一次判断
    # 退回“启动 CONSULT”，事件永远无法识别。
    baseline = last_consult
    if baseline is None:
        everything = [item for item in completed
                      if _record_mode(item) == "CONSULT"]
        if everything:
            baseline = max(everything,
                           key=lambda item: _text(item.get("created_at")))
    same_signals: set[str] = set()
    if baseline is not None and "complexity_signals" in baseline:
        same_signals = {_text(code)
                        for code in baseline.get("complexity_signals") or []}
    # 旧记录没有信号快照时不做差异判断：按“已覆盖”处理，避免升级后重复付费。
    new_codes: list[str] = []
    if same_signals:
        new_codes = [code for code in profile["signal_codes"]
                     if code in SOL_ESCALATION_SIGNALS
                     and code not in same_signals]
    # 事件是调度层与复用层的共同依据：快照只取可归因事实；旧记录缺快照时按
    # 升级型信号映射同义事件，保持升级后不重复付费。
    snapshot = sol_event_snapshot(state)
    events = sol_events(state, snapshot,
                        (baseline or {}).get("sol_event_snapshot"))
    if not events and new_codes:
        events = _signals_to_events(new_codes)
    new_events = [str(item.get("code") or "") for item in events]
    risk = _text(state.get("risk")).lower()
    level = profile["level"]
    kind = profile["task_kind"]
    actions = _action_list(state, plan_only=True)
    planned = len(actions)
    done = len([item for item in actions
                if _text(item.get("status")) in TERMINAL_STATUSES])
    all_done = planned > 0 and done == planned
    understood = bool(profile["readiness"]["understood"])
    # PLAN 准入走唯一口径（plan_admission），覆盖率统计复用同一函数。
    plan_required = plan_admission(
        state, triggers=triggers, workspace=workspace)["required"]
    # 收口综合判断只对“有实质工程进展”的非简单任务有认知价值。
    progressed = has_substantive_progress(state)
    progress_key = sol_progress_key(state)
    closure_cycle = closure_key(state)
    # E 阶段准入＝最终工程验证完成（统一由 governance.closure_readiness 判定）。
    # 缺省时沿用旧的 `gates_passed` 口径，只为历史调用点兼容；调度链路一律传入。
    readiness = closure_ready if isinstance(closure_ready, dict) else {}
    final_verified = (bool(readiness.get("ready")) if readiness
                      else bool(gates_passed))
    verification_digest = _text(readiness.get("digest"))
    # 收口判断按“最终验证摘要”去重：同一份最终验证不重复付费；验证事实变化
    # （新增条件证据、重跑测试报告、工作项结果变化）打开新的收口判断窗口。
    closure_consulted = any(
        _call_stage(item) == "closure"
        and (
            (_text(item.get("final_verification_digest")) and verification_digest
             and _text(item.get("final_verification_digest")) == verification_digest)
            or (not _text(item.get("final_verification_digest"))
                and _text(item.get("closure_key") or closure_cycle) == closure_cycle))
        for item in consult_calls)
    stage_consulted_now = any(
        _call_stage(item) == "stage"
        and _text(item.get("progress_digest")) == progress_key
        for item in consult_calls)
    base = {
        "schema": SOL_ADVICE_SCHEMA,
        "task_kind": kind,
        "complexity": profile,
        "covered": covered,
        "new_signals": new_codes,
        "new_events": new_events,
        "events": events,
        "second_round": bool(last_consult is not None),
        "trigger": _retry_trigger(new_codes) if last_consult is not None else "",
        "action": "none",
        "recommended_action": "not_needed",
        "mode": "",
        "phase": "none",
        "stage": "",
        "family": "",
        "event": "",
        "instance": "",
        "level": "",
        "final_verification_digest": "",
        "question": "",
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
    if level == "low" and not profile.get("non_simple") \
            and not user_request and not review_requested:
        base["reason"] = ("简单/机械任务（无中高复杂信号）：主代理自检、diff 与必要"
                          "测试闭环，SOL 保持 0 调用。")
        return base

    def command_for(mode: str, stage: str, trigger: str) -> str:
        return _advice_command(
            mode, second_round=attempted[mode],
            trigger=trigger or "new-evidence", stage=stage)

    if review_requested:
        if covered["REVIEW"] and not new_codes:
            base.update({
                "action": "reuse", "mode": "REVIEW",
                "reason": "用户要求的独立复核已覆盖当前合同：直接复用，不重复调用。",
            })
            return base
        # 只有已有 REVIEW 才是第二轮；已有 CONSULT 不要求 REVIEW 带 --force。
        base["second_round"] = attempted["REVIEW"]
        base["trigger"] = ("new-risk" if new_codes else "user-request") \
            if attempted["REVIEW"] else ""
        base.update({
            "action": "review", "recommended_action": "recommended",
            "mode": "REVIEW", "phase": "review", "stage": "review",
            "reason": ("用户明确要求独立复核：执行一次 SOL REVIEW（advisory）；"
                       "结论仍是建议，不构成收口门禁。"),
            "command": _advice_command(
                "REVIEW", second_round=attempted["REVIEW"],
                trigger="new-risk" if new_codes else "user-request"),
        })
        return base

    # ── 阶段 E：收口前综合判断 ──────────────────────────────────────────
    # 准入只有一条：最终工程验证真的完成（`closure_readiness`）。工作项刚好全部
    # 终态、条件尚未核销或必要测试没有验证痕迹时，E 保持待执行而不是拿不完整的
    # 材料去问收口问题。仍未处理的动态事件优先于收口判断。
    if final_verified:
        if not closure_consulted and progressed \
                and (risk == "high" or level in ("high", "medium")):
            # 收口综合判断对非简单有进展任务是必需窗口：medium 也必须是 recommended，
            # 否则调度层按 recommended 取义务时永远取不到，收口 CONSULT 结构性不可达。
            recommended = "recommended"
            event = "closure"
            trigger = _retry_trigger(new_codes) if new_codes else "stage-result"
            base["second_round"] = True
            base["trigger"] = trigger
            base.update({
                "action": "consult", "recommended_action": recommended,
                "mode": "CONSULT", "phase": "closure", "stage": "closure",
                "family": "E", "event": event, "level": recommended,
                "instance": verification_digest or closure_cycle,
                "final_verification_digest": verification_digest,
                "question": question_for(event, base),
                "reason": ("最终工程验证已完成（工作项终态、条件核销、证据与必要"
                           "测试齐备）：在收口前做一次 SOL 收口综合判断（目标达成"
                           "情况、重要遗漏与剩余风险）；工程验收仍由 ZCode 依据真实"
                           "证据完成。"),
                "command": command_for("CONSULT", "closure", trigger),
            })
            return base
        if closure_consulted:
            base.update({
                "action": "reuse", "mode": "CONSULT",
                "reason": ("当前最终验证摘要已有收口判断且没有新的升级型事件：直接"
                           "复用，不重复调用。"),
            })
            return base
        base["reason"] = ("任务没有实质工程进展或复杂度不足：不因阶段自动调用 "
                          "SOL，继续按本地证据收口。")
        return base

    # ── 阶段 A：非简单任务启动默认 CONSULT ──────────────────────────────
    # 已有可归因的新事件时走 C（合同修订会让旧结论不再“覆盖”，但语境已经从
    # 启动变成范围变化，问题也应问“哪些旧结论仍成立”而不是“重新理解任务”）。
    if not covered["CONSULT"] and not events:
        stage = "understanding" if not understood else "development"
        if attempted["CONSULT"]:
            trigger = "user-request" if user_request else "scope-changed"
        else:
            trigger = ""
        base["second_round"] = attempted["CONSULT"]
        base["trigger"] = trigger
        base.update({
            "action": "consult", "recommended_action": "recommended",
            "mode": "CONSULT", "phase": stage, "stage": stage,
            "family": "A", "event": "startup", "level": "recommended",
            "instance": sha256_text(canonical_json(
                [snapshot["contract_sha256"], "startup"]))[:16],
            "question": question_for("startup", base),
            "reason": ("非简单任务从启动阶段默认获得一次 SOL CONSULT："
                       "确认目标理解、问题判断、关键矛盾、初步策略与主要风险；"
                       "随后由 ZCode 自主调查并核实。"),
            "command": command_for("CONSULT", stage, trigger),
        })
        if user_request:
            base["reason"] = ("用户明确要求 SOL 判断：直接调用一次 CONSULT；"
                              "不受复杂度判断限制。")
        return base
    if user_request:
        base["second_round"] = attempted["CONSULT"]
        base["trigger"] = "user-request"
        base.update({
            "action": "consult", "recommended_action": "recommended",
            "mode": "CONSULT", "phase": "development", "stage": "development",
            "family": "C", "event": "decision-point", "level": "recommended",
            "instance": "user-request",
            "question": question_for("decision-point", base),
            "reason": ("用户明确要求再次 SOL 判断：按用户要求调用一次（advisory）；"
                       "结论不构成门禁。"),
            "command": command_for("CONSULT", "development", "user-request"),
        })
        return base

    # ── 阶段 C：执行期按新增事件动态追加 CONSULT ────────────────────────
    # 事件来自可归因事实（结论被推翻、复现、决策窗口、风险上升、重规划、
    # 合同修订、重开）。普通证据追加、备注变化、常规验收进展不在这里出现。
    # 事件非空本身就要求“已有一次真实判断作为基线”，因此不再额外要求当前合同
    # 已被覆盖：合同修订后的第一次判断正属于这一类。
    if events:
        row = events[0]
        trigger = SOL_EVENT_TRIGGERS.get(row["code"], "new-evidence")
        base["second_round"] = True
        base["trigger"] = trigger
        base.update({
            "action": "consult", "recommended_action": "recommended",
            "mode": "CONSULT", "phase": "development", "stage": "development",
            "family": "C", "event": row["code"], "level": "recommended",
            "instance": str(row.get("instance") or ""),
            "question": question_for(str(row["code"]), base),
            "reason": ("出现新的认知事件（" + str(row.get("detail") or row["code"])
                       + "）：建议追加一次动态 CONSULT，判断是否调整方案、优先级"
                       "或风险策略。"),
            "command": command_for("CONSULT", "development", trigger),
        })
        return base

    # ── 阶段 B：调查后默认 PLAN（方案比较 / 重要取舍 / 实施优先级）──────
    # 已到收口或全部工作项完成时不再补规划：此时收口综合判断价值更高。
    if not covered["PLAN"] and plan_required and understood \
            and not (gates_passed or all_done):
        trigger = "scope-changed" if attempted["PLAN"] else ""
        base["second_round"] = attempted["PLAN"]
        base["trigger"] = trigger
        base.update({
            "action": "plan", "recommended_action": "recommended",
            "mode": "PLAN", "phase": "planning", "stage": "planning",
            "family": "B", "event": "plan-needed", "level": "recommended",
            "instance": sha256_text(canonical_json(
                [snapshot["contract_sha256"], "plan-needed"]))[:16],
            "question": question_for("plan-needed", base),
            "reason": ("已形成现场事实且任务包含多步骤、多方案或重要决策："
                       "建议调用一次 SOL PLAN 比较方案、确定优先级与验收关注点；"
                       "最终技术方案仍由 ZCode 决定。"),
            "command": command_for("PLAN", "planning", trigger),
        })
        return base

    # ── 阶段 D：medium/high 任务在重要里程碑后可做一次复盘 ───────────────
    # 里程碑以“已完成的实质工作”计量，不用证据条数或时间：medium/high 任务在
    # 完成 ≥2 个工作项（或 ≥1 个工作项且已核销条件）后构成可归因的里程碑。
    # 同一 progress_key 只复盘一次，普通进度推进不会反复触发。
    milestone_done = done >= 2 or (done >= 1 and any(
        _text(item.get("status")) == "met"
        for item in (state.get("contract") or {}).get("criteria") or []
        if isinstance(item, dict)))
    if level in ("medium", "high") and milestone_done and not all_done \
            and not stage_consulted_now:
        base["second_round"] = True
        base["trigger"] = "stage-result"
        base.update({
            "action": "consult", "recommended_action": "recommended",
            "mode": "CONSULT", "phase": "stage", "stage": "stage",
            "family": "D", "event": "milestone", "level": "recommended",
            "instance": progress_key[:16],
            "question": question_for("milestone", base),
            "reason": (f"{level} 复杂任务已完成 {done} 个工作项：在阶段边界做一次"
                       "成果复盘（是否偏离目标、未解决问题与下一阶段方向）；"
                       "同一进度只复盘一次，进度前进后旧窗口被新窗口取代。"),
            "command": command_for("CONSULT", "stage", "stage-result"),
        })
        return base

    base.update({
        "action": "reuse", "mode": "CONSULT",
        "reason": ("同一合同已有有效 SOL 认知结论且没有新的升级型事件：直接复用，"
                   "不重复调用。"),
    })
    return base


def question_for(event: str, advice: dict | None = None) -> str:
    """按生命周期窗口给出本次真正需要判断的事项（确定性模板，不含自由文本）。

    它只是调用时的默认问题：主代理可以覆盖。问题必须落在“需要判断什么”上，
    而不是要求 SOL 复述已知事实。
    """
    code = _text(event) or "startup"
    return {
        "startup": ("在开始实施前，请判断：任务目标与隐含需求是否理解完整？问题本质与"
                    "关键矛盾是什么？有哪些重要业务约束、初步策略与主要风险？调查应"
                    "从哪些重点与优先级入手？"),
        "plan-needed": ("调查已形成现场事实。请比较可行方案与关键取舍，给出实施顺序、"
                        "阶段优先级与验收关注点，并指出最容易被忽略的失败模式。"),
        "material-evidence-change": ("出现可能改变原有判断的新事实（见材料中的实质变化"
                                     "与触发事实）。请判断它是否推翻原方案或既有结论，"
                                     "需要怎样调整方案、优先级或风险策略。"),
        "decision-point": ("出现了需要取舍的决策点。请比较可选方案的收益、代价与风险，"
                           "说明取舍依据、不可逆性，以及需要用户决定的事项。"),
        "risk-escalation": ("任务风险等级实质上升。请判断新的风险来源与影响范围，"
                            "以及是否应调整方案、验证强度或暂停某些动作。"),
        "replan-history": ("原方案已失效并进入重新规划。请判断失败的根因、必须放弃的"
                           "假设，以及实质不同的替代路线与验收方式。"),
        "scope-changed": ("目标、范围或合同发生实质变化。请判断旧结论哪些仍成立、哪些"
                          "已被推翻，以及新范围下的优先顺序与主要风险。"),
        "reopened": ("任务已重新打开。请判断重开的真实原因、旧结论的适用范围，"
                     "以及为避免再次返工应补充的验证。"),
        "milestone": ("重要阶段成果已完成。请复盘：是否偏离原目标？原方案是否仍成立？"
                      "是否有遗漏或新风险？下一阶段的重点与优先级是什么？"),
        "closure": ("最终工程验证已完成，进入收口综合判断。请回答六点："
                    "1) 目标是否真正达成？2) 是否有重要遗漏？"
                    "3) 是否仍存在未解决的关键风险或限制？"
                    "4) 证据与结论是否一致？5) 是否有必要补充验证？"
                    "6) 与之前 SOL 的判断相比是否有实质变化或被推翻的部分？"),
    }.get(code, SOL_EVENTS.get(code) or "请判断本次最需要澄清的事项。")

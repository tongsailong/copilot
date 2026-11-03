#!/usr/bin/env python3
"""copilot 自动协作驱动（阶段 4）：把多轮机械串联收敛为一个入口。

定位
----
主代理（继承当前会话模型）仍是唯一执行方：本脚本不执行工作项、不派子代理、不伪造结果。
它只做原来必须人工串联的机械部分：

    读取状态 -> 八阶段求值 -> （需要时）自动发起一轮 GPT 调用
             -> 把执行结果/证据/报告原子回填 -> 再次求值 -> 自动四门收口

一次 `drive` 至多发起一次真实 GPT 调用；执行动作与 GPT 判断分离。主代理在
`status=execute` 时用原生工具完成工作项，把结果写成 manifest，再用一次
`ingest` 纯本地回填，之后显式 `drive` 进入下一轮——不需要用户逐轮转发报告
或重写 Prompt。

`execution_backend=native` 是显式 opt-in：EXECUTE 额外返回绑定已验证 SOL 轮次的
`native_dispatch`；执行结果与独立复核分别用
`copilot_auto.py ingest --native-result` 回填，报告通过 `kind=execution` /
`kind=review` 分开保存。执行回填后阶段停在 REVIEW_PENDING，复核回填并通过绑定
校验后才允许 SOL 做最终判断；CLOSED 前必须确认复核流程完成。native 状态拒绝
`--result-file`，legacy 状态拒绝 `--native-result`。Bridge 的普通复核不改变 G4，
也不自动派发 Reviewer。

native 默认预算：每任务 6 次请求（按审计 call_requested 在出站前判定，硬上限）
与 500k 累计 Token（按审计 usage 核算；usage 响应后才知道，单次调用最多超出一次，
另用上一次调用的总量做保守预估拦截）。预算状态从审计重建，进程重启不归零。

复用边界
--------
- 状态机、四门、PASS 优先级、pass_pending、CLOSED 锁全部复用 copilot_dispatch.py。
- 调用、模型身份校验、pending/recover、审计、配额熔断全部复用 call_gpt.py。
- 证据包复用 bridge_evidence.py（自动收集 manifest 中标记 include 的证据）。
- 不递归启动 ZCode CLI，不读写宿主数据库，不依赖未公开接口。

失败关闭
--------
CLOSED / RECOVER / BLOCKED / QUOTA_WAIT / 无进展 / 8 轮检查点 / 轮次上限
一律不发起新调用；结果未知的 pending 只能人工 recover 或显式 abandon。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import copilot_dispatch as D  # noqa: E402
import native_bridge as NB  # noqa: E402

CALL = os.path.join(HERE, "call_gpt.py")
BRIDGE = os.path.join(HERE, "bridge_evidence.py")
DISPATCH = os.path.join(HERE, "copilot_dispatch.py")

EXIT_OK, EXIT_USAGE = 0, 2
EXIT_EXECUTE = 10   # 主代理需要执行工作项
EXIT_WAIT = 11      # 等待用户/人工处理（恢复、阻塞、配额、检查点、无进展）

MAX_MANIFEST_BYTES = 2 << 20
MAX_RESULT_CHARS = 200_000
MAX_REPORT_CHARS = 8000
MAX_REPORT_ITEM_CHARS = 1200
EXECUTABLE_STATUSES = ("done", "failed", "blocked", "dropped")


class AutoError(Exception):
    """可预期失败：用法、manifest、状态或前置条件。"""


def _load_json(path: str) -> object:
    if not os.path.exists(path):
        raise AutoError(f"文件不存在：{path}")
    if os.path.getsize(path) > MAX_MANIFEST_BYTES:
        raise AutoError(f"文件过大（>{MAX_MANIFEST_BYTES} 字节）：{path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise AutoError(f"无法解析 JSON {path}：{exc}") from None


def _read_text(path: str) -> str:
    if not os.path.exists(path):
        raise AutoError(f"文件不存在：{path}")
    if os.path.getsize(path) > MAX_RESULT_CHARS:
        raise AutoError(f"结果文件过大（>{MAX_RESULT_CHARS} 字节）：{path}；请先精简")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        raise AutoError(f"无法读取文件 {path}：{exc}") from None


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原长 {len(text)} 字符）"


def _verified_rounds(state: dict) -> list[dict]:
    return [r for r in state.get("rounds") or []
            if isinstance(r, dict) and r.get("model_identity_ok") is True]


def _latest_round_id(state: dict) -> str:
    rounds = _verified_rounds(state)
    return str((rounds[-1].get("round_id") if rounds else "") or "")


def _pending_call_id(state: dict) -> str:
    pending = state.get("pending_call")
    return str(pending.get("call_id") or "") if isinstance(pending, dict) else ""


def _snapshot(state: dict) -> dict:
    return {"round_seq": state.get("round_seq", 0),
            "pending_call_id": _pending_call_id(state)}


# ── Agent Bridge：原生 dispatch / 执行结果 / 独立复核 ───────────────────────

def _native_round_info(state: dict) -> dict:
    rounds = _verified_rounds(state)
    if not rounds:
        raise AutoError("没有已验证 SOL 轮次：原生 dispatch 必须来自已验证安排。")
    rnd = rounds[-1]
    rid = str(rnd.get("round_id") or "").strip()
    call_id = str(rnd.get("call_id") or "").strip()
    reply_sha = str(rnd.get("reply_sha256") or "").strip()
    if not rid or not call_id or not reply_sha:
        raise AutoError(
            "最近已验证轮次缺少 round_id / call_id / reply_sha256；"
            "无法生成可绑定的原生 dispatch。")
    return {"round_id": rid, "plan_call_id": call_id,
            "plan_reply_sha256": reply_sha}


def _emit_native_dispatch(path: str, state: dict, *, persist: bool) -> tuple[dict | None, dict]:
    """按当前 ready actions 生成/复用 dispatch；native 后端才调用。"""
    actions = D.ready_actions(state)
    if not actions:
        return None, state
    info = _native_round_info(state)
    try:
        envelope = NB.build_dispatch(
            state, actions, info, existing=state.get("native_dispatch"))
        next_dispatch = NB.dispatch_state(envelope, state.get("native_dispatch"))
    except NB.BridgeError as exc:
        raise AutoError(f"原生 dispatch 生成失败 [{exc.code}]：{exc}") from None
    if not persist:
        return envelope, state
    snapshot = _snapshot(state)
    state["native_dispatch"] = next_dispatch
    D.save_state(path, state, expect=snapshot)
    return envelope, D.load_state(path)


def _native_result_by_sha(state: dict, result_sha: str) -> dict | None:
    for entry in state.get("native_results") or []:
        if isinstance(entry, dict) and str(entry.get("result_sha256") or "") == result_sha:
            return entry
    return None


def _native_review_by_sha(state: dict, result_sha: str) -> dict | None:
    for entry in state.get("native_reviews") or []:
        if isinstance(entry, dict) and str(entry.get("result_sha256") or "") == result_sha:
            return entry
    return None


def _execution_report_text(result: dict) -> str:
    executor = result.get("executor") or {}
    usage = result.get("usage") or {}
    lines = [
        "原生执行结果（Agent Bridge；这是执行方原始回传，不是独立复核，也不是证据本体）",
        "",
        f"- 工作项：{result['work_item_id']}；状态：{result['status']}",
        f"- dispatch_id：{result['dispatch_id']}",
        f"- result_sha256：{result['result_sha256']}",
        f"- 执行方：{executor.get('kind') or '-'} / {executor.get('profile_id') or '-'}；"
        f"Agent ID：{executor.get('agent_id') or '-'}；"
        f"Child Session：{executor.get('child_session_id') or '-'}",
        f"- 计时：{(result.get('timing') or {}).get('duration_ms', '-')} ms；"
        f"调用统计：{usage or '未提供'}",
        "",
        "执行摘要：",
        result["summary"],
        "",
        "原始结果：",
        result["result"],
    ]
    return _clip("\n".join(lines), MAX_REPORT_CHARS - 120)


def _review_report_text(review: dict) -> str:
    reviewer = review.get("reviewer") or {}
    findings = []
    for item in review.get("findings") or []:
        location = item.get("file") or "-"
        if item.get("line"):
            location += f":{item['line']}"
        findings.append(
            f"- [{item.get('severity')}] {item.get('summary')} "
            f"（{location}；证据：{item.get('evidence') or '-'}）")
    lines = [
        "原生独立复核报告（Reviewer 独立报告；普通 PASS 不等于 G4；"
        "g4_eligible=false）",
        "",
        f"- 工作项：{review['work_item_id']}",
        f"- 复核对象 execution_result_sha256：{review['execution_result_sha256']}",
        f"- Reviewer：{reviewer.get('profile_id') or '-'}；"
        f"Agent ID：{reviewer.get('agent_id') or '-'}；"
        f"Child Session：{reviewer.get('child_session_id') or '-'}",
        f"- 结论：{review['verdict']}",
        f"- 检查文件：{', '.join(review.get('files_inspected') or []) or '-'}",
        f"- 执行命令：{'; '.join(review.get('commands_run') or []) or '（无）'}",
        f"- 未验证事项：{'; '.join(review.get('unverified') or []) or '（无）'}",
        f"- 残余风险：{'; '.join(review.get('residual_risks') or []) or '（无）'}",
        "",
        "Findings：",
    ]
    lines += findings or ["- （无 finding）"]
    return _clip("\n".join(lines), MAX_REPORT_CHARS - 120)


def _apply_native_execution(path: str, state: dict, envelope: dict) -> dict:
    dispatch = state.get("native_dispatch")
    try:
        result = NB.validate_execution_result(envelope, dispatch, state)
    except NB.BridgeError as exc:
        raise AutoError(f"原生执行结果无效 [{exc.code}]：{exc}") from None
    existing = _native_result_by_sha(state, result["result_sha256"])
    if existing is not None:
        return {"kind": "execution", "duplicate": True,
                "work_item_id": existing.get("work_item_id"),
                "result_sha256": result["result_sha256"]}
    action = D._by_id(state, result["work_item_id"])
    snapshot = _snapshot(state)
    try:
        for evidence in result["evidence"]:
            D.register_evidence(
                state, evidence["id"], kind=evidence["kind"],
                file_path=evidence["file"], note=evidence["note"])
        D.record(state, result["work_item_id"], result["status"],
                 result["result"], [e["id"] for e in result["evidence"]])
        blocked = result["status"] in ("failed", "blocked")
        entry = D.add_report(
            state, _execution_report_text(result), round_id=result["round_id"],
            blocked=blocked,
            reason=result["summary"] if blocked else "",
            kind="execution", work_item_id=result["work_item_id"],
            dispatch_id=result["dispatch_id"],
            result_sha256=result["result_sha256"])
        state["native_results"].append({
            "ts": D.now_iso(), "kind": "execution",
            "round_id": result["round_id"],
            "contract_sha256": result["contract_sha256"],
            "plan_call_id": result["plan_call_id"],
            "plan_reply_sha256": result["plan_reply_sha256"],
            "dispatch_id": result["dispatch_id"],
            "idempotency_key": result["idempotency_key"],
            "work_item_id": result["work_item_id"],
            "status": result["status"],
            "summary": result["summary"],
            "evidence_ids": [e["id"] for e in result["evidence"]],
            "executor": result["executor"],
            "timing": result["timing"],
            "usage": result["usage"],
            "result_sha256": result["result_sha256"],
            "report_sha256": entry["report_sha256"],
        })
        native = state["native_dispatch"]
        item = (native.get("items") or {}).get(result["work_item_id"])
        if isinstance(item, dict):
            item["status"] = result["status"]
            item.setdefault("results", []).append(result["result_sha256"])
            item["latest_result_sha256"] = result["result_sha256"]
            item["updated_at"] = D.now_iso()
        D.save_state(path, state, expect=snapshot)
    except D.DispatchError as exc:
        raise AutoError(f"原生执行结果回填失败：{exc}") from None
    return {"kind": "execution", "duplicate": False,
            "work_item_id": result["work_item_id"],
            "status": result["status"],
            "result_sha256": result["result_sha256"],
            "report_sha256": entry["report_sha256"],
            "action": {"id": action["id"], "status": action["status"]}}


def _apply_native_review(path: str, state: dict, envelope: dict) -> dict:
    dispatch = state.get("native_dispatch")
    if not isinstance(dispatch, dict):
        raise AutoError("状态没有 native_dispatch：复核不能先于执行出现。")
    exec_sha = str(envelope.get("execution_result_sha256") or "").strip().lower()
    execution = _native_result_by_sha(state, exec_sha)
    if execution is None:
        raise AutoError(
            "复核绑定的执行结果尚未入账；必须先回填对应的 .execution 结果，"
            "不能凭空复核或只复核执行方自评。")
    try:
        review = NB.validate_review_result(envelope, dispatch, execution, state)
    except NB.BridgeError as exc:
        raise AutoError(f"原生复核结果无效 [{exc.code}]：{exc}") from None
    existing = _native_review_by_sha(state, review["result_sha256"])
    if existing is not None:
        return {"kind": "review", "duplicate": True,
                "work_item_id": existing.get("work_item_id"),
                "result_sha256": review["result_sha256"]}
    consumed = {str(x) for x in state.get("review_consumed") or []}
    if review["execution_result_sha256"] in consumed:
        raise AutoError(
            "该执行结果已有原生复核记录；同一轮 native Reviewer 与 GPT REVIEW "
            "不得重复拥有复核权威，也不能用第二份复核覆盖第一份。")
    channel = state.get("review_channel")
    if channel not in (None, "native"):
        raise AutoError(
            f"当前 review_channel={channel!r}，与原生 Reviewer 冲突；"
            "同一轮不得同时使用原生与 GPT REVIEW 复核权威。")
    snapshot = _snapshot(state)
    blocked = review["verdict"] != "pass"
    reason = "native Review verdict=" + review["verdict"]
    if blocked and review.get("unverified"):
        reason += "；未验证：" + "；".join(review["unverified"])
    try:
        entry = D.add_report(
            state, _review_report_text(review), round_id=review["round_id"],
            blocked=blocked, reason=reason if blocked else "",
            kind="review", work_item_id=review["work_item_id"],
            dispatch_id=review["dispatch_id"],
            result_sha256=review["result_sha256"],
            review_of=review["execution_result_sha256"],
            verdict=review["verdict"], g4_eligible=False)
        state["native_reviews"].append({
            "ts": D.now_iso(), "kind": "review",
            "round_id": review["round_id"],
            "contract_sha256": review["contract_sha256"],
            "dispatch_id": review["dispatch_id"],
            "work_item_id": review["work_item_id"],
            "execution_result_sha256": review["execution_result_sha256"],
            "verdict": review["verdict"],
            "findings": review["findings"],
            "files_inspected": review["files_inspected"],
            "commands_run": review["commands_run"],
            "changed_files": [],
            "unverified": review["unverified"],
            "residual_risks": review["residual_risks"],
            "reviewer": review["reviewer"],
            "g4_eligible": False,
            "result_sha256": review["result_sha256"],
            "report_sha256": entry["report_sha256"],
        })
        state["review_channel"] = "native"
        state["review_round"] = review["round_id"]
        state["review_consumed"].append(review["execution_result_sha256"])
        native = state["native_dispatch"]
        item = (native.get("items") or {}).get(review["work_item_id"])
        if isinstance(item, dict):
            item["review_result_sha256"] = review["result_sha256"]
            item["review_verdict"] = review["verdict"]
            item["updated_at"] = D.now_iso()
        D.save_state(path, state, expect=snapshot)
    except D.DispatchError as exc:
        raise AutoError(f"原生复核结果回填失败：{exc}") from None
    return {"kind": "review", "duplicate": False,
            "work_item_id": review["work_item_id"],
            "verdict": review["verdict"],
            "execution_result_sha256": review["execution_result_sha256"],
            "result_sha256": review["result_sha256"],
            "report_sha256": entry["report_sha256"],
            "g4_eligible": False}


def _validate_native_result(state: dict, envelope: dict) -> dict:
    """--dry-run 的只读校验：不写状态、不登记证据、不产生报告。"""
    try:
        kind = NB.classify_native_result(envelope)
        dispatch = state.get("native_dispatch")
        if kind == "execution":
            result = NB.validate_execution_result(envelope, dispatch, state)
            return {"kind": kind, "valid": True,
                    "work_item_id": result["work_item_id"],
                    "result_sha256": result["result_sha256"]}
        exec_sha = str(envelope.get("execution_result_sha256") or "").strip().lower()
        execution = _native_result_by_sha(state, exec_sha)
        if execution is None:
            raise NB.BridgeError(
                "review_before_execution",
                "复核绑定的执行结果尚未入账；必须先回填执行结果。")
        review = NB.validate_review_result(envelope, dispatch, execution, state)
        return {"kind": kind, "valid": True,
                "work_item_id": review["work_item_id"],
                "verdict": review["verdict"],
                "result_sha256": review["result_sha256"]}
    except NB.BridgeError as exc:
        raise AutoError(f"原生结果校验失败 [{exc.code}]：{exc}") from None


def _apply_native_result(path: str, state: dict, envelope: dict) -> dict:
    try:
        kind = NB.classify_native_result(envelope)
    except NB.BridgeError as exc:
        raise AutoError(f"原生结果校验失败 [{exc.code}]：{exc}") from None
    if kind == "execution":
        return _apply_native_execution(path, state, envelope)
    return _apply_native_review(path, state, envelope)


# ── manifest 校验与回填 ────────────────────────────────────────────────────

def _manifest_evidence(manifest: dict) -> list[dict]:
    raw = manifest.get("evidence") or []
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise AutoError("manifest.evidence 必须是数组。")
    out: list[dict] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise AutoError(f"evidence[{idx}] 不是对象。")
        eid = str(item.get("id") or "").strip()
        if not D.EVIDENCE_ID_RE.match(eid):
            raise AutoError(f"evidence[{idx}] 的 id {eid!r} 非法（应形如 E1）。")
        if eid in seen:
            raise AutoError(f"manifest 内证据 id 重复：{eid}。")
        seen.add(eid)
        has_file = bool(str(item.get("file") or "").strip())
        has_text = item.get("text") is not None
        if has_file == has_text:
            raise AutoError(f"{eid} 必须且只能提供 file 或 text 之一。")
        entry = {
            "id": eid,
            "kind": str(item.get("kind") or ("file" if has_file else "inline")).strip(),
            "note": str(item.get("note") or "").strip(),
            "include": bool(item.get("include", True)),
        }
        if has_file:
            path = os.path.abspath(str(item.get("file")).strip())
            if not os.path.exists(path):
                raise AutoError(f"{eid} 的证据文件不存在：{path}")
            entry["file"] = path
            rng = str(item.get("range") or "").strip()
            if rng and not (re.match(r"^\d+(?:/\d+)?$", rng)
                            or (rng.startswith("/") and rng.endswith("/") and len(rng) > 2)):
                raise AutoError(
                    f"{eid} 的 range={rng!r} 非法；应为 A/B 行区间或 /REGEX/。")
            entry["range"] = rng
        else:
            entry["text"] = str(item.get("text") or "")
        out.append(entry)
    return out


def _manifest_actions(manifest: dict, state: dict,
                      evidence: list[dict]) -> list[dict]:
    raw = manifest.get("actions")
    if not isinstance(raw, list) or not raw:
        raise AutoError("manifest.actions 必须是非空数组。")
    latest = _latest_round_id(state)
    if not latest:
        raise AutoError("还没有已验证轮次：执行结果必须对应一次已提交的 GPT 安排。")
    declared = str(manifest.get("round_id") or "").strip()
    if declared and declared != latest:
        raise AutoError(
            f"manifest.round_id={declared} 不是最近已验证轮次 {latest}；"
            "拒绝把旧轮执行结果记到当前状态。")
    if D.pass_pending(state):
        raise AutoError(
            "最近一轮是显式 PASS（pass_pending）：执行面已锁定，不能继续回填普通工作项；"
            "先完成四门核销或由 GPT 重新判断。")
    if D._effective_decision(state) in ("stop", "blocked"):
        raise AutoError(
            "最近一轮 decision 为 stop/blocked：不能继续执行工作项，"
            "需要 GPT 给出新的判断或用户决策。")
    by_id = {str(a.get("id")): a for a in state.get("actions") or []}
    manifest_evidence_ids = {e["id"] for e in evidence}
    known_evidence = {str(e.get("id")) for e in state.get("evidence") or []}
    out: list[dict] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise AutoError(f"actions[{idx}] 不是对象。")
        aid = str(item.get("id") or "").strip()
        if not aid:
            raise AutoError(f"actions[{idx}] 缺少 id。")
        if aid in seen:
            raise AutoError(f"manifest 内工作项 id 重复：{aid}。")
        seen.add(aid)
        action = by_id.get(aid)
        if action is None:
            raise AutoError(f"不存在工作项 {aid}。")
        if action.get("status") in D.TERMINAL:
            raise AutoError(
                f"{aid} 已终结（{action.get('status')}）；续做请由 GPT 新建工作项。")
        status = str(item.get("status") or "").strip().lower()
        if status not in EXECUTABLE_STATUSES:
            raise AutoError(
                f"{aid} 的 status={status!r} 不被自动驱动接受；"
                f"只能是 {', '.join(EXECUTABLE_STATUSES)}（自动驱动要求同步执行完成）。")
        result = str(item.get("result") or "")
        result_file = str(item.get("result_file") or "").strip()
        if result and result_file:
            raise AutoError(f"{aid} 不能同时提供 result 与 result_file。")
        if result_file:
            result = _read_text(result_file)
        if not result.strip():
            raise AutoError(f"{aid} 缺少执行结果（result / result_file）；"
                            "done/failed/blocked/dropped 都必须说明实际发生了什么。")
        refs = item.get("evidence_ids") or []
        if not isinstance(refs, list):
            raise AutoError(f"{aid} 的 evidence_ids 必须是数组。")
        clean_refs: list[str] = []
        for ref in refs:
            rid = str(ref or "").strip()
            if not rid:
                continue
            if rid not in known_evidence and rid not in manifest_evidence_ids:
                raise AutoError(
                    f"{aid} 引用了未登记证据 {rid}；请先在 manifest.evidence 登记，"
                    "或使用本任务已登记的证据 id。")
            if rid not in clean_refs:
                clean_refs.append(rid)
        out.append({"id": aid, "status": status, "result": result,
                    "evidence_ids": clean_refs})
    return out


def _auto_report(actions: list[dict], note: str) -> str:
    lines = ["自动驱动执行回传（由 copilot_auto.py 生成；这是执行方原始结果，"
             "不是 GPT 判断，也不是证据本体）", ""]
    for action in actions:
        lines.append(f"- {action['id']} [{action['status']}] "
                     + _clip(action["result"].strip(), MAX_REPORT_ITEM_CHARS))
    note = (note or "").strip()
    if note:
        lines += ["", "执行方补充说明：", _clip(note, 3000)]
    text = "\n".join(lines)
    if len(text) > MAX_REPORT_CHARS:
        text = _clip(text, MAX_REPORT_CHARS - 80)
    return text


def _apply_manifest(path: str, state: dict, manifest: dict) -> dict:
    """校验、回填并原子保存。任一校验失败都不写盘。"""
    evidence = _manifest_evidence(manifest)
    actions = _manifest_actions(manifest, state, evidence)
    snapshot = _snapshot(state)
    for entry in evidence:
        D.register_evidence(
            state, entry["id"], kind=entry["kind"],
            text=entry.get("text") if "text" in entry else None,
            file_path=entry.get("file"), note=entry["note"])
    for action in actions:
        D.record(state, action["id"], action["status"], action["result"],
                 action["evidence_ids"])
    report = _auto_report(actions, str(manifest.get("report_note") or ""))
    entry = D.add_report(state, report)
    D.save_state(path, state, expect=snapshot)
    return {"actions": actions, "evidence": evidence,
            "report": {"round_id": entry["round_id"],
                       "report_sha256": entry["report_sha256"],
                       "chars": len(report)}}


# ── 证据包 ─────────────────────────────────────────────────────────────────

def _build_auto_pack(workdir: str, state: dict, manifest_evidence: list[dict],
                     question: str) -> str | None:
    """把 manifest 中 include=true 的证据整理成 call_gpt 可读的证据包。

    复用 bridge_evidence 的脱敏、指纹、预算与省略标注；不在驱动层重新实现。
    """
    selected = [e for e in manifest_evidence if e.get("include", True)]
    if not selected:
        return None
    os.makedirs(workdir, exist_ok=True)
    round_no = int(state.get("round_seq") or 0) + 1
    pack = os.path.join(workdir, f"round-{round_no}-evidence.md")
    manifest_path = os.path.join(workdir, f"round-{round_no}-evidence.json")
    cmd = [sys.executable, "-B", BRIDGE,
           "--goal", f"第 {round_no} 轮执行证据（自动驱动；任务 {state.get('task_id')}）"
                     + (f"\n本轮问题：{_clip(question, 1200)}" if question else ""),
           "--out", pack, "--manifest", manifest_path,
           "--max-chars", "60000"]
    mapping = []
    for entry in selected:
        if "text" in entry:
            tmp = os.path.join(workdir, f"evidence-{entry['id']}.txt")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(entry["text"])
            spec = tmp
        else:
            spec = entry["file"]
            if entry.get("range"):
                spec += "::" + entry["range"]
        cmd += ["--file", spec]
        label = f"{entry['id']}={entry.get('file') or '<inline>'}"
        if entry.get("note"):
            # 证据用途说明随包带给 GPT；否则只有路径/编号，无法判断该证据对应哪项工作。
            label += f"（{_clip(entry['note'], 200)}）"
        mapping.append(label)
    cmd += ["--note", "自动证据包编号与任务状态证据索引是两套编号；"
                      "任务状态编号对应关系：" + "；".join(mapping)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise AutoError("自动证据包生成失败（bridge_evidence.py）："
                        + (proc.stderr.strip() or proc.stdout.strip())[-1500:])
    return pack


# ── 自动 Prompt ────────────────────────────────────────────────────────────

def _gate_reasons(state: dict) -> list[str]:
    gate = (state.get("closure") or {}).get("gate") or {}
    reasons = []
    for reason in gate.get("reasons") or []:
        if isinstance(reason, dict):
            reasons.append(
                f"{reason.get('gate') or '-'} {reason.get('code') or '-'}："
                f"{reason.get('detail') or ''}")
        else:
            reasons.append(str(reason))
    return reasons


def _build_prompt(state: dict, loop: dict, question: str) -> str:
    phase = loop.get("phase") or ""
    rid = _latest_round_id(state)
    criteria = (state.get("contract") or {}).get("criteria") or []
    met = sum(1 for c in criteria if c.get("status") == "met")
    lines = [
        "## 自动协作驱动请求（copilot_auto.py）",
        "",
        f"- 任务：{state.get('task_id') or '-'}",
        f"- 当前阶段：{phase}",
        f"- 已完成轮次：{state.get('round_seq') or 0}"
        f"（最近已验证轮次 {rid or '无'}）",
        f"- 完成条件：{met}/{len(criteria)} 已核销"
        f"（{((state.get('contract') or {}).get('criteria_state') or 'draft')}）",
        f"- PASS 挂起：{'是' if D.pass_pending(state) else '否'}",
        "",
    ]
    if phase == "REJUDGE":
        lines += ["### 上一轮 PASS 未通过本地四门 / native 复核门槛", ""]
        reasons = list(_gate_reasons(state))
        reasons += [f"{item.get('code')}：{item.get('detail')}"
                    for item in (loop.get("native_review") or {}).get("reasons") or []]
        lines += [f"- {r}" for r in reasons] or ["- （失败原因未记录）"]
        lines += ["", "这不是要求你重读证据；请基于当前证据与失败原因重新给出三态判断。",
                  "native 复核的 blocking finding 不能绕过：请明确决定修复还是继续。", ""]
    elif not rid:
        lines += ["### 初始规划", "",
                  "尚未有已验证轮次。请先审定结构化完成条件（需要时给出 "
                  "`criteria_confirmed: true`），并给出可执行工作项。", ""]
    else:
        lines += ["### 执行回传后继续", "",
                  "执行方已回填上一轮可见工作项的实际结果、证据与报告；结果可能是完成、"
                  "失败、阻塞或放弃，以报告与交接状态为准。原始材料见本轮证据包（如有）。"
                  "请判断下一步。", ""]
    if question.strip():
        lines += ["### 执行方本轮问题", "", _clip(question.strip(), 4000), ""]
    reports = state.get("reports") or []
    execution_reports = [r for r in reports
                         if isinstance(r, dict) and r.get("kind", "execution") == "execution"]
    review_reports = [r for r in reports
                      if isinstance(r, dict) and r.get("kind") == "review"]
    if execution_reports:
        latest = execution_reports[-1]
        lines += ["### 最近一次执行报告（执行方自动登记；是执行反馈，不是证据本体）", "",
                  f"- 绑定轮次：{latest.get('round_id') or '-'}；"
                  f"登记时间：{latest.get('ts') or '-'}",
                  "",
                  _clip(str(latest.get("summary") or ""), 5000), ""]
    if review_reports:
        latest = review_reports[-1]
        lines += ["### 最近一次原生独立复核报告（独立报告；PASS 不等于 G4）", "",
                  f"- 绑定轮次：{latest.get('round_id') or '-'}；"
                  f"复核对象 SHA-256：{latest.get('review_of') or '-'}",
                  f"- g4_eligible：{latest.get('g4_eligible')}（固定为 false）",
                  "",
                  _clip(str(latest.get("summary") or ""), 5000), ""]
    lines += [
        "### 输出要求（自动驱动契约）",
        "",
        "1. 必须明确给出 `verdict`：`pass` / `partial` / `blocked`；缺失不等于 PASS。",
        "2. `partial` 时用 ```copilot-plan``` 给最小可执行工作项；"
        "工作项要写清目标、约束和需要返回的原始证据。",
        "3. `pass` 时必须恰好覆盖全部完成条件各一次，每条 `met` 且引用已登记证据；"
        "不能只用自然语言总结代替逐条核销。",
        "4. 你的回复会被自动写入任务状态并直接进入下一轮，不需要用户转发、复制或重新组织。",
        "5. 你仍不能读取本机文件或执行命令；判断只依据本消息中的交接状态与证据包。",
        ("6. 原生复核报告是独立报告，不等于 G4；不得把执行方自报或复核报告合并。"
         if state.get("execution_backend") == "native" else
         "6. 不要声称已完成独立复核；独立复核由执行方按宿主规范另行完成。"),
        "",
    ]
    return "\n".join(lines)


# ── 调用 ───────────────────────────────────────────────────────────────────

def _call_gpt(args, state: dict, prompt: str, evidence: list[str]) -> dict:
    workdir = os.path.abspath(args.work_dir)
    os.makedirs(workdir, exist_ok=True)
    round_no = int(state.get("round_seq") or 0) + 1
    prompt_path = os.path.join(workdir, f"round-{round_no}-prompt.md")
    reply_path = os.path.join(workdir, f"round-{round_no}-reply.md")
    with open(prompt_path, "w", encoding="utf-8") as fh:
        fh.write(prompt + "\n")
    mode = str(state.get("mode") or "PLAN").strip().upper() or "PLAN"
    if mode not in ("PLAN", "CONSULT"):
        raise AutoError(
            f"自动驱动只承接 PLAN / CONSULT 任务（当前 {mode}）；"
            "REVIEW 必须走复核台账路径，不能由自动循环代替。")
    cmd = [sys.executable, "-B", CALL,
           "--mode", mode,
           "--dispatch-state", os.path.abspath(args.state),
           "--prompt-file", prompt_path,
           "--audit-log", args.audit_log,
           "--out", reply_path,
           "--timeout", str(args.timeout),
           "--max-output-tokens", str(args.max_output_tokens)]
    if args.max_retries is not None:
        if args.max_retries < 0:
            raise AutoError("--max-retries 不能为负数")
        retries = int(args.max_retries)
    elif state.get("execution_backend") == "native":
        # native 任务默认只尝试一次：不能让 call_gpt 的 2 次重试默认值意外恢复。
        retries = 0
    else:
        retries = None
    if retries is not None:
        cmd += ["--max-retries", str(retries)]
    if args.base_url:
        cmd += ["--base-url", args.base_url]
    for pack in evidence:
        cmd += ["--evidence", pack]
    outer_timeout = max(60.0, float(args.timeout) + 120.0)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=outer_timeout)
    except subprocess.TimeoutExpired:
        return {"exit": 4, "stderr": "call_gpt.py 进程超时（请求结果未知）",
                "stdout": "", "prompt_file": prompt_path,
                "reply_file": reply_path, "evidence": evidence}
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)
    return {"exit": proc.returncode, "stderr": proc.stderr,
            "stdout": proc.stdout, "prompt_file": prompt_path,
            "reply_file": reply_path, "evidence": evidence}


def _run_recover(args) -> None:
    cmd = [sys.executable, "-B", DISPATCH, "recover",
           "--state", os.path.abspath(args.state),
           "--audit-log", args.audit_log,
           "--reply-file", args.recover_reply]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)
    if proc.returncode != 0:
        raise AutoError("恢复挂起轮次失败：" + (proc.stderr.strip() or proc.stdout.strip()))


# ── 结果包装 ───────────────────────────────────────────────────────────────

def _result(status: str, state: dict, loop: dict, **extra) -> dict:
    out = {
        "status": status,
        "phase": (loop or {}).get("phase"),
        "task_id": state.get("task_id"),
        "round_seq": state.get("round_seq"),
        "round_id": _latest_round_id(state),
        "pass_pending": D.pass_pending(state),
        "criteria": (loop or {}).get("criteria"),
        "checkpoint": (loop or {}).get("checkpoint"),
        "no_progress": (loop or {}).get("no_progress"),
        "gate": (loop or {}).get("gate"),
        "native_review": (loop or {}).get("native_review"),
        "notes": list((loop or {}).get("notes") or []),
    }
    out.update(extra)
    return out


def _action_packet(state: dict) -> list[dict]:
    return [{
        "id": a.get("id"),
        "title": a.get("title"),
        "assignee": a.get("assignee"),
        "goal": a.get("goal"),
        "constraints": a.get("constraints"),
        "expected_evidence": a.get("expected_evidence"),
        "depends_on": a.get("depends_on") or [],
        "status": a.get("status"),
    } for a in D.ready_actions(state)]


def _emit(payload: dict, code: int) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


# ── 主流程 ─────────────────────────────────────────────────────────────────

def _stop_reason(loop: dict, state: dict) -> str:
    if loop.get("checkpoint"):
        return "checkpoint"
    if loop.get("no_progress"):
        return "no_progress"
    limit = (state.get("budget") or {}).get("rounds_limit")
    if limit is not None:
        try:
            if int(state.get("round_seq") or 0) >= int(limit):
                return "rounds_limit"
        except (TypeError, ValueError):
            pass
    return ""


def drive(args) -> int:
    path = os.path.abspath(args.state)
    state = D.load_state(path)
    if not D.task_credential(state):
        raise AutoError(
            "交接状态没有任务级凭证（task_id / contract_sha256）：旧格式状态只能人工补录，"
            "不能进入自动协作通道。请用 `copilot_dispatch.py start --contract-file` 重建。")
    # 注意：不能写 str(value) 判断——str(False) == "False" 为真，会把未收口
    # 的新任务误判成 CLOSED。
    if bool((state.get("closure") or {}).get("closed")):
        return _emit(_result("closed", state, {"phase": "CLOSED"},
                             state_path=path), EXIT_OK)

    # 中断恢复：给了回复文件且确认是同一 pending 时，先按审计凭证恢复；否则只报告。
    if isinstance(state.get("pending_call"), dict):
        if args.recover_reply and not args.dry_run:
            _run_recover(args)
            state = D.load_state(path)
        if isinstance(state.get("pending_call"), dict):
            pending = state["pending_call"]
            return _emit(_result(
                "recover", state, {"phase": "RECOVER"},
                pending={"call_id": pending.get("call_id"),
                         "round_id": pending.get("round_id")},
                reason="存在未结算的调用；先 recover，不要重新发起"), EXIT_WAIT)

    if args.native_result:
        raise AutoError(
            "--native-result 已从 drive 拆分：先用 `copilot_auto.py ingest --state "
            f"{path} --native-result <json>` 纯本地回填（0 请求、不改轮次），"
            "再显式运行 `drive` 让 SOL 依据执行/复核报告判断。")
    if args.result_file and state.get("execution_backend") == "native":
        raise AutoError(
            "当前任务 execution_backend=native，不能用 --result-file 回填；"
            "请使用 `copilot_auto.py ingest --native-result`，且执行与复核报告分别提交。")

    manifest_info = None
    manifest_evidence: list[dict] = []
    if args.result_file:
        manifest = _load_json(args.result_file)
        if not isinstance(manifest, dict):
            raise AutoError("manifest 顶层必须是对象。")
        if args.dry_run:
            manifest_evidence = _manifest_evidence(manifest)
            actions = _manifest_actions(manifest, state, manifest_evidence)
            return _emit(_result(
                "manifest_valid", state, {"phase": "DRY_RUN"},
                dry_run=True,
                manifest={"actions": [a["id"] for a in actions],
                          "evidence": [e["id"] for e in manifest_evidence]}), EXIT_OK)
        manifest_info = _apply_manifest(path, state, manifest)
        manifest_evidence = manifest_info["evidence"]
        state = D.load_state(path)

    loop = D.loop_step(path, persist=not args.dry_run)
    phase = loop.get("phase")
    common = {"dry_run": bool(args.dry_run), "manifest": manifest_info,
              "state_path": path}

    if phase == "CLOSED":
        return _emit(_result("closed", state, loop, **common), EXIT_OK)
    if phase == "RECOVER":
        pending = state.get("pending_call") or {}
        return _emit(_result(
            "recover", state, loop, **common,
            pending={"call_id": pending.get("call_id"),
                     "round_id": pending.get("round_id")},
            reason="存在未结算的调用；先 recover，不要重新发起"), EXIT_WAIT)
    if phase == "BLOCKED":
        return _emit(_result("blocked", state, loop, **common,
                             reason="最近判断为 blocked 或高风险 G4 缺少原生复核"),
                     EXIT_WAIT if not args.dry_run else EXIT_OK)
    if phase == "QUOTA_WAIT":
        return _emit(_result("quota_wait", state, loop, **common,
                             reason="配额/冷却未结束；不调用、不执行"), 
                     EXIT_WAIT if not args.dry_run else EXIT_OK)
    if phase == "REVIEW_PENDING":
        # 执行已回填、复核未回填：不得进入最终判断或收口，也不消耗付费请求。
        return _emit(_result("review_pending", state, loop, **common,
                             reason="native_review_pending",
                             review=loop.get("native_review")),
                     EXIT_WAIT if not args.dry_run else EXIT_OK)
    if phase == "CLOSURE_CHECK":
        if args.dry_run:
            return _emit(_result("would_close", state, loop, **common), EXIT_OK)
        closure = D.close_task(path)
        state = D.load_state(path)
        return _emit(_result("closed", state,
                             {"phase": "CLOSED", "criteria": loop.get("criteria"),
                              "gate": loop.get("gate")},
                             closure={"round_id": closure.get("round_id"),
                                      "closed_by": closure.get("closed_by"),
                                      "verdict": closure.get("verdict")},
                             **common), EXIT_OK)
    if phase == "EXECUTE":
        native_extra = {}
        if state.get("execution_backend") == "native":
            native_dispatch, state = _emit_native_dispatch(
                path, state, persist=not args.dry_run)
            common["execution_backend"] = "native"
            native_extra["native_dispatch"] = native_dispatch
        return _emit(_result("execute", state, loop, **common,
                             actions=_action_packet(state),
                             round_id=_latest_round_id(state), **native_extra),
                     EXIT_OK if args.dry_run else EXIT_EXECUTE)

    # GUIDANCE_NEEDED / REJUDGE：只在没有停摆/检查点/轮次上限时发起调用。
    reason = _stop_reason(loop, state)
    if reason:
        return _emit(_result("wait_user", state, loop, **common, reason=reason),
                     EXIT_OK if args.dry_run else EXIT_WAIT)

    if phase == "REJUDGE" and not args.dry_run:
        # 按 SKILL 的 REJUDGE 流程先留痕失败原因，再发起重新判断。
        reasons = list(_gate_reasons(state))
        reasons += [f"{item.get('code')}：{item.get('detail')}"
                    for item in (loop.get("native_review") or {}).get("reasons") or []]
        summary = "自动驱动：上一轮 PASS 未通过本地四门或 native 复核门槛，转入重新判断。"
        if reasons:
            summary += "\n" + "\n".join(f"- {r}" for r in reasons)
        snapshot = _snapshot(state)
        D.add_report(state, summary, blocked=True,
                     reason="; ".join(reasons) or "四门核验失败")
        D.save_state(path, state, expect=snapshot)
        state = D.load_state(path)
        loop = D.loop_step(path, persist=True)

    question = ""
    if args.question_file:
        question = _read_text(args.question_file)
    elif args.question:
        question = args.question
    prompt = _build_prompt(state, loop, question)
    if args.dry_run:
        return _emit(_result("call", state, loop, **common, prompt=prompt,
                             would_call=True), EXIT_OK)

    # 出站前按审计重算预算：请求数是硬上限；Token 是「已上报 usage」阈值 +
    # 保守预估（usage 只有响应后才知道，不能声称严格上界）。
    budget = D.budget_status(state, args.audit_log)
    if not budget["allowed"]:
        return _emit(_result("budget_exhausted", state, loop, **common,
                             reason=budget["reason"], detail=budget["detail"],
                             budget=budget), EXIT_WAIT)

    evidence = [os.path.abspath(p) for p in args.evidence]
    auto_pack = None
    if not args.no_auto_evidence:
        auto_pack = _build_auto_pack(os.path.abspath(args.work_dir), state,
                                     manifest_evidence, question)
    if auto_pack:
        evidence.append(auto_pack)

    # 本轮无进展指纹在真实调用前落盘。若这次指纹使 streak 达到停摆阈值，
    # 必须在出站前停止——否则「先检查后调用」会多刷一轮付费调用。
    try:
        stamp = D.stamp_progress(path, prompt)
    except (OSError, D.DispatchError) as exc:
        raise AutoError(f"无法记录本轮无进展指纹：{exc}") from None
    if stamp.get("no_progress"):
        state = D.load_state(path)
        loop = D.loop_step(path, persist=True)
        return _emit(_result("wait_user", state, loop, **common,
                             reason="no_progress",
                             stamp=stamp), EXIT_WAIT)

    call = _call_gpt(args, state, prompt, evidence)
    call_info = {"exit": call["exit"], "prompt_file": call["prompt_file"],
                 "reply_file": call["reply_file"], "evidence": evidence}
    if call["exit"] != 0:
        state = D.load_state(path)
        loop = D.loop_step(path, persist=True)
        pending = state.get("pending_call")
        if call["exit"] == 12:
            return _emit(_result(
                "budget_exhausted", state, loop, **common, call=call_info,
                reason="call_gpt 拒绝出站：请求数/Token 预算已耗尽"),
                EXIT_WAIT)
        return _emit(_result(
            "failed", state, loop, **common, call=call_info,
            reason=f"GPT 调用失败（退出码 {call['exit']}）；"
                   + ("存在 pending，下一轮先 recover。" if isinstance(pending, dict)
                      else "未写入任务状态。")), call["exit"])

    state = D.load_state(path)
    loop = D.loop_step(path, persist=True)
    phase = loop.get("phase")
    if phase == "CLOSURE_CHECK":
        closure = D.close_task(path)
        state = D.load_state(path)
        return _emit(_result(
            "closed", state,
            {"phase": "CLOSED", "criteria": loop.get("criteria"), "gate": loop.get("gate")},
            closure={"round_id": closure.get("round_id"),
                     "closed_by": closure.get("closed_by"),
                     "verdict": closure.get("verdict")},
            call=call_info, **common), EXIT_OK)
    if phase == "EXECUTE":
        native_extra = {}
        if state.get("execution_backend") == "native":
            native_dispatch, state = _emit_native_dispatch(
                path, state, persist=not args.dry_run)
            common["execution_backend"] = "native"
            native_extra["native_dispatch"] = native_dispatch
        return _emit(_result("execute", state, loop, call=call_info,
                             actions=_action_packet(state),
                             round_id=_latest_round_id(state),
                             **native_extra, **common),
                     EXIT_EXECUTE)
    if phase in ("BLOCKED", "QUOTA_WAIT", "RECOVER", "REVIEW_PENDING"):
        status = {"BLOCKED": "blocked", "QUOTA_WAIT": "quota_wait",
                  "RECOVER": "recover",
                  "REVIEW_PENDING": "review_pending"}[phase]
        return _emit(_result(status, state, loop, call=call_info, **common),
                     EXIT_WAIT)
    if phase == "REJUDGE":
        return _emit(_result("wait_user", state, loop, call=call_info, **common,
                             reason="rejudge_required"), EXIT_WAIT)
    return _emit(_result("wait_user", state, loop, call=call_info, **common,
                         reason="guidance_without_actions"), EXIT_WAIT)


def ingest(args) -> int:
    """纯本地回填：校验并写入执行结果/证据/复核报告。

    与 drive 的边界：
    - 不调用 call_gpt.py，不产生任何 HTTP 请求；
    - 不递增 round_seq、不创建/释放 pending_call、不推进 GPT 轮次；
    - 重复回填同一信封按 `result_sha256` 幂等，状态不变；
    - CLOSED / 存在 pending 时拒绝写入（收口后不得再改任务结果）。
    SOL 的最终判断只能由随后显式触发的 `drive` 发起。
    """
    path = os.path.abspath(args.state)
    state = D.load_state(path)
    if not D.task_credential(state):
        raise AutoError(
            "交接状态没有任务级凭证（task_id / contract_sha256）：旧格式状态只能人工补录，"
            "不能进入自动协作通道。")
    if bool((state.get("closure") or {}).get("closed")):
        raise AutoError(
            "任务已收口（CLOSED）：不能再回填执行/复核结果；"
            "如需继续请先 `copilot_dispatch.py closure --state <state> --reopen --reason <原因>`。")
    if isinstance(state.get("pending_call"), dict):
        raise AutoError(
            "存在未结算的调用（pending_call）：先 recover 确定该次调用结果，再回填执行/复核。")
    if bool(args.native_result) == bool(args.result_file):
        raise AutoError("必须且只能提供 --native-result 或 --result-file 之一。")

    round_before = int(state.get("round_seq") or 0)
    pending_before = state.get("pending_call")
    if args.native_result:
        if state.get("execution_backend") != "native":
            raise AutoError(
                "当前任务 execution_backend=legacy，不能使用 --native-result；"
                "需要原生执行时请用 `copilot_dispatch.py start --execution-backend native` 重建。")
        envelope = _load_json(args.native_result)
        if not isinstance(envelope, dict):
            raise AutoError("native result 顶层必须是对象。")
        if args.dry_run:
            info = _validate_native_result(state, envelope)
            return _emit({
                "status": "ingest_valid", "kind": info.get("kind"),
                "native_result": info, "dry_run": True,
                "round_seq": round_before, "state_path": path,
                "http_requests": 0,
            }, EXIT_OK)
        info = _apply_native_result(path, state, envelope)
    else:
        if state.get("execution_backend") == "native":
            raise AutoError(
                "当前任务 execution_backend=native，不能用 --result-file 回填；"
                "请使用 --native-result，且执行与复核报告分别提交。")
        manifest = _load_json(args.result_file)
        if not isinstance(manifest, dict):
            raise AutoError("manifest 顶层必须是对象。")
        if args.dry_run:
            manifest_evidence = _manifest_evidence(manifest)
            actions = _manifest_actions(manifest, state, manifest_evidence)
            return _emit({
                "status": "ingest_valid", "kind": "legacy_manifest", "dry_run": True,
                "manifest": {"actions": [a["id"] for a in actions],
                             "evidence": [e["id"] for e in manifest_evidence]},
                "round_seq": round_before, "state_path": path,
                "http_requests": 0,
            }, EXIT_OK)
        info = _apply_manifest(path, state, manifest)

    state = D.load_state(path)
    # 结构性不变式：回填只改执行/复核留痕，不得动轮次或挂起调用。
    if int(state.get("round_seq") or 0) != round_before:
        raise AutoError(
            f"内部错误：回填改变了 round_seq（{round_before} -> "
            f"{state.get('round_seq')}）；请检查 ingest 路径。")
    if state.get("pending_call") != pending_before:
        raise AutoError("内部错误：回填改变了 pending_call；请检查 ingest 路径。")
    loop = D.loop_step(path, persist=False)
    budget = D.budget_status(state, args.audit_log)
    return _emit({
        "status": "ingested",
        "kind": info.get("kind") or "legacy_manifest",
        "duplicate": bool(info.get("duplicate")),
        "native_result": info,
        "round_seq": int(state.get("round_seq") or 0),
        "round_id": _latest_round_id(state),
        "phase": loop.get("phase"),
        "pass_pending": D.pass_pending(state),
        "native_review": loop.get("native_review"),
        "budget": {"allowed": budget["allowed"], "reason": budget["reason"],
                   "spend": budget["spend"], "limits": budget["limits"]},
        "state_path": path,
        "http_requests": 0,
        "notes": ["ingest 是纯本地回填：未调用 GPT、未改动轮次；"
                  "SOL 判断需显式运行 drive。"],
    }, EXIT_OK)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="copilot 自动协作驱动：一次推进一个机械步骤，至多一次 GPT 调用")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("drive", help="推进任务：执行包 / 自动调用 / 自动收口 / 等待")
    p.add_argument("--state", required=True)
    p.add_argument("--audit-log", default=D.DEFAULT_AUDIT_LOG)
    p.add_argument("--work-dir", default="",
                   help="Prompt/回复/自动证据包目录（默认 <state 目录>/auto-<task_id>）")
    p.add_argument("--result-file", default="",
                   help="legacy 后端：主代理执行完工作项后的结果 manifest（JSON）")
    p.add_argument("--native-result", default="",
                   help="已停用：native 结果请用 `copilot_auto.py ingest --native-result` "
                        "纯本地回填，再显式 drive 让 SOL 判断")
    p.add_argument("--evidence", action="append", default=[],
                   help="显式证据包（可多次）；与自动证据包同时提供时由 GPT 一并读取")
    p.add_argument("--no-auto-evidence", action="store_true",
                   help="不自动把 manifest 中 include 的证据整理成证据包")
    p.add_argument("--question", default="", help="本轮额外问题（可省略）")
    p.add_argument("--question-file", default="")
    p.add_argument("--recover-reply", default="",
                   help="存在 pending 时用于恢复的回复文件；不给则只报告 RECOVER")
    p.add_argument("--base-url", default="",
                   help="仅测试/自建上游使用；正式运行留空走 CPA 默认")
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--max-output-tokens", type=int, default=8192)
    p.add_argument("--max-retries", type=int, default=None,
                   help="透传给 call_gpt.py 的最大重试次数；0 表示只尝试一次"
                        "（native 默认 0，legacy 默认沿用 call_gpt 的 2）")
    p.add_argument("--dry-run", action="store_true",
                   help="只求值与校验，不写状态、不写文件、不调用 GPT")

    pi = sub.add_parser(
        "ingest", help="纯本地回填执行/复核结果（0 请求、不改轮次、幂等）")
    pi.add_argument("--state", required=True)
    pi.add_argument("--audit-log", default=D.DEFAULT_AUDIT_LOG)
    pi.add_argument("--native-result", default="",
                    help="native 后端：copilot-native-result/v1 或 "
                         "copilot-native-review/v1 信封（JSON）；执行与复核分别提交")
    pi.add_argument("--result-file", default="",
                    help="legacy 后端：主代理执行完工作项后的结果 manifest（JSON）")
    pi.add_argument("--dry-run", action="store_true",
                    help="只校验信封/manifest，不写状态")
    args = ap.parse_args(argv)

    if args.cmd == "drive":
        path = os.path.abspath(args.state)
        if not args.work_dir:
            state_dir = os.path.dirname(path) or "."
            try:
                task_id = str(D.load_state(path).get("task_id") or "task")
            except (OSError, D.DispatchError):
                task_id = "task"
            safe = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id)[:40] or "task"
            args.work_dir = os.path.join(state_dir, f"auto-{safe}")
        try:
            return drive(args)
        except (AutoError, D.DispatchError, OSError) as exc:
            print(f"copilot-auto: {exc}", file=sys.stderr)
            return EXIT_USAGE
    if args.cmd == "ingest":
        try:
            return ingest(args)
        except (AutoError, D.DispatchError, OSError) as exc:
            print(f"copilot-auto: {exc}", file=sys.stderr)
            return EXIT_USAGE
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())

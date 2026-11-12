#!/usr/bin/env python3
"""copilot 自动协作驱动（阶段 4）：把多轮机械串联收敛为一个入口。

定位
----
主代理（继承当前会话模型）仍是唯一执行方：本脚本不执行工作项、不派子代理、不伪造结果。
它只做原来必须人工串联的机械部分：

    读取状态 -> 九阶段求值（含 REVIEW_PENDING）-> （需要时）自动发起一轮 GPT 调用
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

预算与输出：默认**不设请求数/Token 上限**，用量按审计重建并写入状态，只用于观察与
成本审计（进程重启不归零），不阻断 drive。只有调用方显式给出 max_requests /
max_total_tokens 时才按显式运营上限拒绝出站。输出预算默认不发送 max_output_tokens
（由服务商/模型使用真实上限）；被截断时由 call_gpt.py 在同一任务/证据/轮次内自适应
升档重发，不消耗正式 REVIEW 轮次。

PARTIAL 自动推进：最近一轮 verdict=partial 且没有可执行安排时，本地派生一个
origin=partial-without-plan 工作项（0 请求）并进入 EXECUTE；该轮已有执行/复核/报告
材料时不再派生，必须把新材料交回 SOL 判断。循环持续到 PASS 收口或真实 BLOCKED。

复用边界
--------
- 状态机、四门、PASS 优先级、pass_pending、CLOSED 锁全部复用 copilot_dispatch.py。
- 调用、模型身份校验、pending/recover、审计、配额熔断全部复用 call_gpt.py。
- 证据包复用 bridge_evidence.py（自动收集 manifest 中标记 include 的证据）。
- 不递归启动 ZCode CLI，不读写宿主数据库，不依赖未公开接口。

失败关闭
--------
CLOSED / RECOVER / BLOCKED / QUOTA_WAIT / 无进展（六维 streak ≥2）一律不发起新调用；
8 轮检查点与轮次上限只作观察信号，不停止循环。结果未知的 pending 只能人工 recover
或显式 abandon。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

import copilot_dispatch as D  # noqa: E402
import native_bridge as NB  # noqa: E402
from lib_common import sha256_file, sha256_text  # noqa: E402

CALL = os.path.join(HERE, "call_gpt.py")
BRIDGE = os.path.join(os.path.dirname(HERE), "scripts", "bridge_evidence.py")
DISPATCH = os.path.join(HERE, "copilot_dispatch.py")

EXIT_OK, EXIT_USAGE = 0, 2
EXIT_EXECUTE = 10   # 主代理需要执行工作项
EXIT_WAIT = 11      # 等待人工处理（恢复、阻塞、配额、无进展、真实阻塞）

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


def _run_local(cmd: list[str], timeout: float, label: str):
    """本地子进程调用：超时转成可读的 AutoError，不让 TimeoutExpired 逃成 traceback。

    本地脚本超时表示结果未知（例如 recover 可能已经部分写盘）：这里只报错，
    不重试、不清 pending——恢复语义仍由调用方按「结果未知」处理。
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AutoError(
            f"{label} 超时（{timeout:.0f}s）：结果可能未知；不要自动重试，"
            "先检查审计与交接状态（pending 保留）后人工确认。") from None


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


def _review_report_text(review: dict, evidence: dict | None = None) -> str:
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
    ]
    if evidence:
        lines += [
            f"- 原始报告证据：{evidence['id']}（SHA-256 {evidence['sha256']}）",
            f"- 原始报告文件：{evidence.get('path_hint') or '<inline>'}",
            "",
        ]
    lines += [
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


def _stage_native_review_raw(path: str, review: dict, envelope: dict,
                             source_file: str = "") -> str:
    """保留 Reviewer 原始报告文件。

    优先使用调用方给出的原始文件；直接调用 API（没有来源文件）时，在状态目录下
    写一份内容确定、可复核的信封快照。快照已存在但内容不同则失败关闭，不覆盖。
    """
    if source_file:
        raw_path = os.path.abspath(source_file)
        if not os.path.isfile(raw_path):
            raise AutoError(f"复核原始报告文件不存在：{raw_path}")
        return raw_path
    root = os.path.join(os.path.dirname(os.path.abspath(path)), "review-evidence")
    os.makedirs(root, exist_ok=True)
    raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    digest = sha256_text(raw)
    target = os.path.join(root, f"{review['result_sha256']}.json")
    if os.path.exists(target):
        if sha256_file(target) != digest:
            raise AutoError(
                f"复核快照 {target} 已存在但内容与本次信封不同；拒绝覆盖历史原始报告。")
        return target
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    return target


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


def _apply_native_review(path: str, state: dict, envelope: dict, *,
                         source_file: str = "") -> dict:
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
    raw_path = _stage_native_review_raw(path, review, envelope, source_file)
    snapshot = _snapshot(state)
    blocked = review["verdict"] != "pass"
    reason = "native Review verdict=" + review["verdict"]
    if blocked and review.get("unverified"):
        reason += "；未验证：" + "；".join(review["unverified"])
    try:
        evidence = D.register_review_evidence(
            state, file_path=raw_path,
            review_of=review["execution_result_sha256"],
            review_of_kind=D.NATIVE_REVIEW_OF_KIND,
            review_source="native", verdict=review["verdict"],
            round_id=review["round_id"],
            note=f"native Reviewer 原始报告（{review['work_item_id']}）")
        entry = D.add_report(
            state, _review_report_text(review, evidence),
            round_id=review["round_id"],
            blocked=blocked, reason=reason if blocked else "",
            kind="review", work_item_id=review["work_item_id"],
            dispatch_id=review["dispatch_id"],
            result_sha256=review["result_sha256"],
            review_of=review["execution_result_sha256"],
            review_of_kind=D.NATIVE_REVIEW_OF_KIND,
            evidence_id=evidence["id"],
            raw_report_sha256=evidence["sha256"],
            review_source="native",
            verdict=review["verdict"], g4_eligible=False)
        state["native_reviews"].append({
            "ts": D.now_iso(), "kind": "review",
            "round_id": review["round_id"],
            "contract_sha256": review["contract_sha256"],
            "dispatch_id": review["dispatch_id"],
            "work_item_id": review["work_item_id"],
            "execution_result_sha256": review["execution_result_sha256"],
            "review_of_kind": D.NATIVE_REVIEW_OF_KIND,
            "review_source": "native",
            "evidence_id": evidence["id"],
            "raw_report_path": evidence.get("path_hint") or raw_path,
            "raw_report_sha256": evidence["sha256"],
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
            "evidence_id": evidence["id"],
            "raw_report_sha256": evidence["sha256"],
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


def _apply_native_result(path: str, state: dict, envelope: dict, *,
                         source_file: str = "") -> dict:
    try:
        kind = NB.classify_native_result(envelope)
    except NB.BridgeError as exc:
        raise AutoError(f"原生结果校验失败 [{exc.code}]：{exc}") from None
    if kind == "execution":
        return _apply_native_execution(path, state, envelope)
    return _apply_native_review(path, state, envelope, source_file=source_file)


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
        include = item.get("include", True)
        if not isinstance(include, bool):
            # JSON 里 "false"/0/null 都不是布尔。bool("false") == True 会把
            # 明确要求排除的敏感证据静默放进 SOL 上下文。
            raise AutoError(
                f"{eid} 的 include={include!r} 不是布尔值；请写 true 或 false。")
        entry = {
            "id": eid,
            "kind": str(item.get("kind") or ("file" if has_file else "inline")).strip(),
            "note": str(item.get("note") or "").strip(),
            "include": include,
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

def _review_handoff_entries(state: dict, *, verify_files: bool) -> list[dict]:
    """选出本轮明确相关的复核原始报告。

    默认只取最近一次复核轮次（同一轮可有多条工作项复核）以及完成条件显式引用的
    复核证据；不把全部历史复核塞进上下文。verify_files=True 时重新核对文件存在
    与 SHA-256，正文包必须按登记时的原文进入，不能悄悄用摘要替代。
    """
    reports = [r for r in state.get("reports") or []
               if isinstance(r, dict) and r.get("kind") == "review"]
    if not reports:
        return []
    latest_round = str(reports[-1].get("round_id") or "")
    wanted = {str(r.get("evidence_id")) for r in reports
              if str(r.get("round_id") or "") == latest_round
              and r.get("evidence_id")}
    for criterion in (state.get("contract") or {}).get("criteria") or []:
        for ref in criterion.get("evidence_refs") or []:
            eid = str(ref.get("id") or "") if isinstance(ref, dict) else ""
            if eid:
                wanted.add(eid)
    by_id = {str(e.get("id") or ""): e for e in state.get("evidence") or []
             if isinstance(e, dict)}
    out: list[dict] = []
    seen_sha: set[str] = set()
    for report in reports:
        eid = str(report.get("evidence_id") or "")
        if eid not in wanted:
            continue
        evidence = by_id.get(eid)
        if evidence is None:
            raise AutoError(f"复核报告引用的原始证据 {eid} 不存在；拒绝用摘要替代。")
        if evidence.get("dropped"):
            raise AutoError(f"复核原始证据 {eid} 已归档；不能作为本轮证据。")
        if str(evidence.get("task_id") or "") != str(state.get("task_id") or ""):
            raise AutoError(f"复核原始证据 {eid} 不属于当前任务；拒绝跨任务引用。")
        sha = str(evidence.get("sha256") or "")
        if (evidence.get("origin") != "review-report"
                or sha != str(report.get("raw_report_sha256") or "")
                or str(evidence.get("review_of") or "") != str(report.get("review_of") or "")
                or str(evidence.get("review_of_kind") or "")
                != str(report.get("review_of_kind") or "")
                or str(evidence.get("review_source") or "")
                != str(report.get("review_source") or "")):
            raise AutoError(
                f"复核原始证据 {eid} 与报告绑定不一致；拒绝用不可信摘要替代原文。")
        path = str(evidence.get("path_hint") or "")
        if verify_files:
            if not path or path == "<inline>":
                raise AutoError(f"复核原始证据 {eid} 没有可读文件；不能进入证据包。")
            if not os.path.isfile(path):
                raise AutoError(f"复核原始报告文件不存在：{path}（证据 {eid}）。")
            actual = sha256_file(path)
            if actual != sha:
                raise AutoError(
                    f"复核原始报告摘要不一致：证据 {eid} 登记 {sha[:12]}，"
                    f"当前文件 {actual[:12]}；拒绝把被改动的内容当作原文。")
        if sha in seen_sha:
            continue
        seen_sha.add(sha)
        out.append({
            "id": eid,
            "sha256": sha,
            "file": path,
            "review_of": str(report.get("review_of") or ""),
            "review_of_kind": str(report.get("review_of_kind") or ""),
            "review_source": str(report.get("review_source") or ""),
            "verdict": str(report.get("verdict") or ""),
            "round_id": str(report.get("round_id") or ""),
        })
    return out


def _build_auto_pack(workdir: str, state: dict, manifest_evidence: list[dict],
                     question: str, *, include_manifest: bool = True) -> str | None:
    """把 manifest 证据与相关复核原文整理成 call_gpt 可读的证据包。

    复用 bridge_evidence 的脱敏、指纹、预算与省略标注；复核原文被截断、整体
    未纳入或摘要不符时失败关闭，绝不退化成只用摘要。

    `include_manifest=False`（`--no-auto-evidence`）只关闭可选的 manifest 证据
    打包；已有复核原文仍必须完整入包——那是正式 REVIEW 的证据链要求，不能用
    开关关闭。
    """
    manifest_selected = ([e for e in manifest_evidence if e.get("include", True)]
                         if include_manifest else [])
    review_selected = _review_handoff_entries(state, verify_files=True)
    if not manifest_selected and not review_selected:
        return None
    os.makedirs(workdir, exist_ok=True)
    round_no = int(state.get("round_seq") or 0) + 1
    pack = os.path.join(workdir, f"round-{round_no}-evidence.md")
    manifest_path = os.path.join(workdir, f"round-{round_no}-evidence.json")

    sources: list[dict] = []
    for entry in manifest_selected:
        if "text" in entry:
            digest = sha256_text(entry["text"])
            spec = None
            text = entry["text"]
        else:
            path = entry["file"]
            try:
                digest = sha256_file(path)
            except OSError as exc:
                raise AutoError(f"自动证据包读取失败：{path}：{exc}") from None
            text = None
            spec = path + ("::" + entry["range"] if entry.get("range") else "")
        label = f"{entry['id']}={entry.get('file') or '<inline>'}"
        if entry.get("note"):
            # 证据用途说明随包带给 GPT；否则只有路径/编号，无法判断该证据对应哪项工作。
            label += f"（{_clip(entry['note'], 200)}）"
        sources.append({"digest": digest, "spec": spec, "text": text,
                        "range": entry.get("range") or "", "labels": [label],
                        "critical": False})
    for entry in review_selected:
        attached = None
        for source in sources:
            if source["digest"] != entry["sha256"]:
                continue
            if source["range"]:
                raise AutoError(
                    f"复核原始报告 {entry['id']} 与 manifest 中同摘要的行区间证据"
                    "无法同时保证完整原文；请改用整文件证据或去掉区间。")
            attached = source
            break
        label = (f"{entry['id']}={entry['file']}（{entry['review_source']} 复核原文；"
                 f"review_of_kind={entry['review_of_kind']}；结论={entry['verdict']}；"
                 f"SHA-256={entry['sha256']}）")
        if attached is not None:
            attached["labels"].append(label)
            attached["critical"] = True
            continue
        sources.append({"digest": entry["sha256"], "spec": entry["file"],
                        "text": None, "range": "", "labels": [label],
                        "critical": True})

    cmd = [sys.executable, "-B", BRIDGE,
           "--goal", f"第 {round_no} 轮执行与复核证据（自动驱动；任务 {state.get('task_id')}）"
                     + (f"\n本轮问题：{_clip(question, 1200)}" if question else ""),
           "--out", pack, "--manifest", manifest_path,
           "--max-chars", "60000"]
    mapping = []
    for index, source in enumerate(sources, 1):
        if source["text"] is not None:
            tmp = os.path.join(workdir, f"evidence-{round_no}-{index}.txt")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(source["text"])
            spec = tmp
        else:
            spec = source["spec"]
        cmd += ["--file", spec]
        mapping.append("；".join(source["labels"]))
    cmd += ["--note", "自动证据包编号与任务状态证据索引是两套编号；"
                      "任务状态编号对应关系：" + "；".join(mapping)]
    proc = _run_local(cmd, 180, "自动证据包生成（bridge_evidence.py）")
    if proc.returncode != 0:
        raise AutoError("自动证据包生成失败（bridge_evidence.py）："
                        + (proc.stderr.strip() or proc.stdout.strip())[-1500:])
    manifest = _load_json(manifest_path)
    items = manifest.get("items") if isinstance(manifest, dict) else None
    if not isinstance(items, list):
        raise AutoError("自动证据包清单缺少 items；无法确认复核原文是否完整。")
    for source in sources:
        if not source["critical"]:
            continue
        matches = [item for item in items
                   if isinstance(item, dict)
                   and str(item.get("sha256") or "") == source["digest"]]
        complete = [item for item in matches
                    if item.get("included") and not item.get("truncated")
                    and not int(item.get("omitted_chars") or 0)]
        if complete:
            continue
        detail = "；".join(
            f"{item.get('reason') or item.get('method') or '未纳入'}"
            for item in matches) or "清单中没有对应来源"
        raise AutoError(
            "复核原始报告未能完整进入证据包（"
            + "；".join(source["labels"]) + "）：" + detail
            + "。拒绝用摘要或截断内容代替原文。")
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


def _no_progress_material(state: dict) -> dict:
    """无进展重新分析的结构化材料（稳定、可重复、不含时间戳）。

    只放「已经发生的事实」：失败工作项、未闭合条件、证据索引、复核报告与最近
    裁决。执行报告与轮次摘要不进入指纹——否则每次重新分析新增的报告会让指纹
    自己变化，无法判断是否真的出现了新信息。
    """
    actions = []
    for action in state.get("actions") or []:
        if not isinstance(action, dict):
            continue
        actions.append({
            "id": str(action.get("id") or ""),
            "title": str(action.get("title") or ""),
            "status": str(action.get("status") or ""),
            "fail_streak": int(action.get("fail_streak") or 0),
            "result": _clip(str(action.get("result") or ""), 2000),
            "result_sha256": sha256_text(str(action.get("result") or "")),
            "origin": str(action.get("origin") or ""),
        })
    criteria = []
    for item in (state.get("contract") or {}).get("criteria") or []:
        if not isinstance(item, dict):
            continue
        criteria.append({
            "id": str(item.get("criterion_id") or ""),
            # 完成条件的正文在契约里叫 `description`；读 `text` 会渲染成空串，
            # SOL 看到的未闭合条件就全是空描述。
            "text": _clip(str(item.get("description")
                              or item.get("text") or ""), 1200),
            "status": str(item.get("status") or ""),
            "evidence_refs": sorted(str(r.get("id") or "")
                                    for r in item.get("evidence_refs") or []),
        })
    evidence = sorted(
        ({"id": str(e.get("id") or ""), "sha256": str(e.get("sha256") or ""),
          "note": _clip(str(e.get("note") or ""), 500)}
         for e in state.get("evidence") or [] if isinstance(e, dict)),
        key=lambda e: e["id"])
    reviews = []
    for report in state.get("reports") or []:
        if not isinstance(report, dict) or report.get("kind") != "review":
            continue
        reviews.append({
            "round_id": str(report.get("round_id") or ""),
            "verdict": str(report.get("verdict") or ""),
            "review_of": str(report.get("review_of") or ""),
            "review_of_kind": str(report.get("review_of_kind") or ""),
            "review_source": str(report.get("review_source") or ""),
            "raw_report_sha256": str(report.get("raw_report_sha256") or ""),
            "summary": _clip(str(report.get("summary") or ""), 3000),
        })
    rounds = []
    for rnd in state.get("rounds") or []:
        if not isinstance(rnd, dict):
            continue
        rounds.append({
            "round_id": str(rnd.get("round_id") or ""),
            "verdict": str(rnd.get("verdict") or ""),
            "summary": _clip(str(rnd.get("summary") or ""), 2000),
        })
    decisions = [{"ts": str(d.get("ts") or ""),
                  "text": _clip(str(d.get("text") or ""), 1000)}
                 for d in state.get("decisions") or [] if isinstance(d, dict)]
    material = {
        "task_id": str(state.get("task_id") or ""),
        "mode": str(state.get("mode") or ""),
        "round_seq": int(state.get("round_seq") or 0),
        "actions": actions,
        "criteria": criteria,
        "evidence": evidence,
        "review_reports": reviews,
        "rounds": rounds[-6:],
        "decisions": decisions[-8:],
    }
    # 指纹只看「实质事实」：`fail_streak` 是同一失败的计数，只增计数不构成新信息；
    # 若把它算进取指纹，同一份材料每轮都会被判为「有新进展」，付费重析闸门失效。
    digest_actions = [{k: v for k, v in action.items() if k != "fail_streak"}
                      for action in actions]
    material["digest"] = sha256_text(
        json.dumps({**{k: material[k] for k in
                       ("task_id", "mode", "criteria", "evidence",
                        "review_reports", "decisions")},
                    "actions": digest_actions},
                   ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return material


def _no_progress_prompt(material: dict) -> str:
    failed = [a for a in material["actions"]
              if a["status"] == "failed" or a["fail_streak"] > 0]
    unmet = [c for c in material["criteria"] if c["status"] != "met"]
    lines = [
        "### 连续无进展：必须重新分析，不能重复原方案",
        "",
        "六维进展指纹已连续多轮没有实质变化；同一失败工作项或未闭合条件反复出现。"
        "请把它当作**方案失效**的信号，而不是继续重复原来的建议。",
        "",
        f"- 失败工作项：{('、'.join(a['id'] for a in failed) or '（无记录）')}",
        f"- 未闭合条件：{('、'.join(c['id'] for c in unmet) or '（无）')}",
        "",
        "你必须逐项回答（缺项视为无效回复）：",
        "1. 无进展的直接原因：之前哪条执行路径/建议失效，证据是什么；"
        "不要把「继续补证」当作原因。",
        "2. 之前方案为什么失效：约束冲突、证据不可得、授权不足、方案本身不可执行，"
        "或其他具体原因；引用上面的失败记录/证据索引。",
        "3. 是否存在其他可行路径：至少给出一个与已失败方案**实质不同**的替代方案；"
        "若没有，给出可验证的理由。",
        "4. 下一步具体执行指令：用 ```copilot-plan``` 给出可执行工作项，写清目标、约束、"
        "需要返回的原始证据、完成条件。",
        "5. 继续执行的授权边界：哪些动作在现有授权内，哪些必须用户另行授权；"
        "超出授权时必须停下并说明，不得默认放行。",
        "",
        "禁止事项：重复上一轮完全相同的建议；为了得到 PASS 降低验收标准；"
        "声称已执行未执行的动作；把这份诊断上下文当作完成条件的核销证据。",
        "只有在确实不存在安全、可执行的替代方案，或缺少用户业务决策/必要授权/"
        "无法取得的资源/不可绕过的服务商限制时，才允许 `blocked`，并给出具体原因与证据。",
        "",
    ]
    if failed:
        lines += ["失败明细（来自任务状态）：", ""]
        for a in failed:
            lines.append(f"- {a['id']}（{a['status']}，连续失败 {a['fail_streak']} 次）："
                         f"{a['result'] or '未记录失败原因'}")
        lines.append("")
    if unmet:
        lines += ["未闭合条件：", ""]
        for c in unmet:
            lines.append(f"- {c['id']}：{c['text']}（状态 {c['status']}；"
                         f"已引用证据 {('、'.join(c['evidence_refs']) or '无')}）")
        lines.append("")
    return "\n".join(lines)


def _build_no_progress_pack(workdir: str, material: dict) -> str:
    """生成无进展诊断证据包；内容变化才产生新包，不发送完全相同的材料。"""
    os.makedirs(workdir, exist_ok=True)
    digest = material["digest"]
    stem = os.path.join(workdir, f"no-progress-{digest[:12]}")
    context_path = stem + ".md"
    pack_path = stem + "-evidence.md"
    manifest_path = stem + "-manifest.json"
    failed = [a for a in material["actions"]
              if a["status"] == "failed" or a["fail_streak"] > 0]
    unmet = [c for c in material["criteria"] if c["status"] != "met"]
    lines = [
        "# 无进展诊断上下文（自动生成）",
        "",
        "这不是新的交付物证据，也不能用于核销完成条件；它只记录为什么自动循环"
        "没有继续推进，供 SOL 重新分析失效方案与替代路径。",
        "",
        f"- 任务：{material['task_id']}",
        f"- 模式：{material['mode']}",
        f"- 当前轮次：{material['round_seq']}",
        f"- 诊断指纹：{digest}",
        "",
        "## 失败工作项",
        "",
    ]
    lines += ([f"- {a['id']}（{a['status']}，连续失败 {a['fail_streak']} 次）："
               f"{a['result'] or '未记录失败原因'}" for a in failed]
              or ["- （无失败工作项记录）"])
    lines += ["", "## 未闭合条件", ""]
    lines += ([f"- {c['id']}：{c['text']}（{c['status']}）" for c in unmet]
              or ["- （无未闭合条件）"])
    lines += ["", "## 已有证据索引（只列编号与指纹，不重复正文）", ""]
    lines += ([f"- {e['id']}：SHA-256 {e['sha256']}；{e['note']}" for e in material["evidence"]]
              or ["- （无已登记证据）"])
    lines += ["", "## 最近复核报告", ""]
    lines += ([f"- 轮次 {r['round_id']}：verdict={r['verdict']}；"
               f"review_of={r['review_of']}；来源={r['review_source']}\n  {r['summary']}"
               for r in material["review_reports"][-3:]]
              or ["- （无复核报告）"])
    lines += ["", "## 最近裁决与决策", ""]
    lines += ([f"- {r['round_id']}：{r['verdict']}；{r['summary']}"
               for r in material["rounds"][-3:]] or ["- （无往轮摘要）"])
    lines += ([f"- {d['ts']}：{d['text']}" for d in material["decisions"][-3:]] or [])
    lines += ["", "## 约束", "",
              "- 同一失败方案不得机械重复；没有新信息时不得无限重复相同 SOL 请求。",
              "- 不得降低验收标准，不得伪造 progress_streak 或执行结果。",
              "- 正式 REVIEW 的授权门槛不变；超出授权必须 BLOCKED 并说明。", ""]
    with open(context_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    cmd = [sys.executable, "-B", BRIDGE,
           "--goal", "连续无进展诊断：分析原方案失效原因与替代路径",
           "--out", pack_path, "--manifest", manifest_path,
           "--max-chars", "60000", "--file", context_path,
           "--note", "无进展诊断上下文；不是交付物证据，不用于核销完成条件"]
    proc = _run_local(cmd, 180, "无进展诊断证据包生成（bridge_evidence.py）")
    if proc.returncode != 0:
        raise AutoError("无进展诊断证据包生成失败："
                        + (proc.stderr.strip() or proc.stdout.strip())[-1500:])
    return pack_path


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
        source = str(latest.get("review_source") or "")
        if source == "native":
            title = "### 最近一次原生独立复核报告（Reviewer 独立报告；PASS 不等于 G4）"
        elif source == "legacy":
            title = ("### 最近一次 legacy 复核报告（未经 native 身份校验；"
                     "PASS 不等于 G4）")
        else:
            title = "### 最近一次复核报告（独立报告；PASS 不等于 G4）"
        lines += [title, "",
                  f"- 绑定轮次：{latest.get('round_id') or '-'}；"
                  f"复核对象 SHA-256：{latest.get('review_of') or '-'}"
                  f"（{latest.get('review_of_kind') or '类型未记录'}）",
                  f"- 复核来源：{source or '未记录'}；"
                  f"结论：{latest.get('verdict') or '-'}",
                  f"- g4_eligible：{latest.get('g4_eligible')}（固定为 false）"]
        handoff = _review_handoff_entries(state, verify_files=False)
        if handoff:
            lines += ["", "已登记的复核原始证据（自动证据包启用时会随包发送；"
                          "摘要仅辅助，判定以原文为准）："]
            for entry in handoff:
                lines.append(
                    f"- {entry['id']}：SHA-256 {entry['sha256']}；"
                    f"来源 {entry['review_source'] or '未记录'}；"
                    f"review_of_kind {entry['review_of_kind'] or '未记录'}；"
                    f"结论 {entry['verdict'] or '-'}；轮次 {entry['round_id'] or '-'}")
            lines += ["- 自动证据包启用且原文未能完整进入时，驱动会失败关闭，"
                      "不会把摘要或截断内容当成已核验原文。"]
        lines += ["", _clip(str(latest.get("summary") or ""), 5000), ""]
    lines += [
        "### 输出要求（自动驱动契约）",
        "",
        "1. 必须明确给出 `verdict`：`pass` / `partial` / `blocked`；缺失不等于 PASS。",
        "2. `partial` 时用 ```copilot-plan``` 给最小可执行工作项；"
        "工作项要写清目标、约束和需要返回的原始证据。",
        "3. `pass` 时必须恰好覆盖全部完成条件各一次，每条 `met` 且引用已登记证据；"
        "不能只用自然语言总结代替逐条核销。若完成条件仍为 draft，须在同一 "
        "```copilot-plan``` 中给出 `criteria_confirmed: true`，否则本地 G2 无法核销。",
        "4. 你的回复会被自动写入任务状态并直接进入下一轮，不需要用户转发、复制或重新组织。",
        "5. 你仍不能读取本机文件或执行命令；判断只依据本消息中的交接状态与证据包。",
        ("6. 原生复核报告是独立报告，不等于 G4；不得把执行方自报或复核报告合并。"
         if state.get("execution_backend") == "native" else
         "6. 不要声称已完成独立复核；独立复核由执行方按宿主规范另行完成。"),
        "",
    ]
    return "\n".join(lines)


# ── 调用 ───────────────────────────────────────────────────────────────────

def _review_ledger_path(args, workdir: str) -> str:
    """REVIEW 台账路径：显式优先，否则放在本轮 work-dir 下保持跨轮稳定。"""
    if args.review_ledger:
        return os.path.abspath(args.review_ledger)
    return os.path.join(workdir, "review-ledger.json")


def _review_round_count(path: str) -> int:
    """台账已入账的 REVIEW 轮数；读不到按 0 处理（由 review_ledger 自己初始化）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0
    rounds = data.get("rounds") if isinstance(data, dict) else None
    return len(rounds) if isinstance(rounds, list) else 0


def _review_subject(path: str, state: dict) -> str:
    """台账的交付物标识：已存在则沿用其 subject；新建时才用 task_id。

    已存在的台账若没有 subject，不在这里静默补一个（那会把「未知归属」说成
    「属于本任务」），交给 review_ledger 失败关闭，要求人工显式声明。
    """
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return ""
        if isinstance(data, dict):
            return str(data.get("subject") or "").strip()
        return ""
    return str(state.get("task_id") or "").strip()


def _auto_extra_round_reason(state: dict, ledger_path: str,
                             new_ids: list[str]) -> tuple[str, str]:
    """首轮之外自动生成 `--extra-round-reason`；返回 (理由, 依据标签)。

    只陈述本轮**实际提交**的事实：第几轮、上一轮裁决、本轮提交了哪些 manifest
    证据、仍未闭合的条件。是否真的构成「新增证据」由台账按内容哈希判定，这里
    不预先断言「新增/哈希变化」——自动生成的文字不能替调用方取得新增证据资格。
    """
    used = _review_round_count(ledger_path)
    if used < 1:
        return "", ""
    rounds = [r for r in state.get("rounds") or []
              if isinstance(r, dict) and r.get("model_identity_ok") is True]
    verdict = str((rounds[-1] if rounds else {}).get("verdict") or "未记录")
    unmet = [str(c.get("criterion_id") or "")
             for c in D.unmet_criteria(state) if c.get("criterion_id")]
    # evidence id 在 manifest 里通常已带 E 前缀；不要重复拼接成 EE1。
    evidence = "、".join(
        str(i) if str(i).upper().startswith("E") else f"E{i}" for i in new_ids
    )
    if evidence:
        reason = (f"自动驱动第 {used + 1} 轮 REVIEW：上一轮裁决 {verdict}；"
                  f"本轮提交 manifest 证据 {evidence}（是否构成新增由台账按内容哈希"
                  f"判定），用于继续核验未闭合条件 {('、'.join(unmet) or '（无）')}。")
        return reason, "new-evidence"
    return (f"自动驱动第 {used + 1} 轮 REVIEW：上一轮裁决 {verdict}；"
            f"本轮未附新的 manifest 证据，仅提交任务状态当前证据与复核原文，"
            f"继续核验未闭合条件 {('、'.join(unmet) or '（无）')}；"
            "是否构成新增证据交由台账判定。"), ""


def _call_gpt(args, state: dict, prompt: str, evidence: list[str],
              extra_round_reason: str = "", extra_round_basis: str = "") -> dict:
    workdir = os.path.abspath(args.work_dir)
    os.makedirs(workdir, exist_ok=True)
    round_no = int(state.get("round_seq") or 0) + 1
    prompt_path = os.path.join(workdir, f"round-{round_no}-prompt.md")
    reply_path = os.path.join(workdir, f"round-{round_no}-reply.md")
    with open(prompt_path, "w", encoding="utf-8") as fh:
        fh.write(prompt + "\n")
    mode = str(state.get("mode") or "PLAN").strip().upper() or "PLAN"
    if mode not in ("PLAN", "CONSULT", "REVIEW"):
        raise AutoError(
            f"自动驱动只承接 PLAN / CONSULT / REVIEW 任务（当前 {mode}）。")
    if mode == "REVIEW" and not getattr(args, "auto_review", False):
        raise AutoError(
            "REVIEW 自动闭环需要显式 --auto-review（表示用户已明确要求连续正式 "
            "REVIEW）；未显式授权时 REVIEW 仍走复核台账人工路径，不能由自动循环代替。")
    cmd = [sys.executable, "-B", CALL,
           "--mode", mode,
           "--dispatch-state", os.path.abspath(args.state),
           "--prompt-file", prompt_path,
           "--audit-log", args.audit_log,
           "--out", reply_path,
           "--timeout", str(args.timeout)]
    if mode == "REVIEW":
        ledger_path = _review_ledger_path(args, workdir)
        cmd += ["--review-ledger", ledger_path]
        subject = _review_subject(ledger_path, state)
        if subject:
            cmd += ["--subject", subject]
        if extra_round_reason.strip():
            cmd += ["--extra-round-reason", extra_round_reason.strip()]
            if extra_round_basis.strip():
                cmd += ["--extra-round-basis", extra_round_basis.strip()]
    # 输出预算默认不发送：不在代码里设置低于服务商真实上限的人为上限。
    if args.max_output_tokens is not None:
        cmd += ["--max-output-tokens", str(args.max_output_tokens)]
    if args.max_output_tokens_ceiling is not None:
        cmd += ["--max-output-tokens-ceiling", str(args.max_output_tokens_ceiling)]
    if getattr(args, "max_output_retries", None) is not None:
        if args.max_output_retries < 0:
            raise AutoError("--max-output-retries 不能为负数")
        cmd += ["--max-output-retries", str(args.max_output_retries)]
    segmentation_plan = os.path.join(workdir, f"round-{round_no}-segmentation.json")
    cmd += ["--segmentation-plan", segmentation_plan]
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
                "reply_file": reply_path, "evidence": evidence,
                "segmentation_plan": segmentation_plan}
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)
    return {"exit": proc.returncode, "stderr": proc.stderr,
            "stdout": proc.stdout, "prompt_file": prompt_path,
            "reply_file": reply_path, "evidence": evidence,
            "segmentation_plan": segmentation_plan}


def _run_recover(args) -> None:
    cmd = [sys.executable, "-B", DISPATCH, "recover",
           "--state", os.path.abspath(args.state),
           "--audit-log", args.audit_log,
           "--reply-file", args.recover_reply]
    proc = _run_local(cmd, 120, "恢复挂起轮次（copilot_dispatch.py recover）")
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)
    if proc.returncode != 0:
        raise AutoError("恢复挂起轮次失败：" + (proc.stderr.strip() or proc.stdout.strip()))


# ── 结果包装 ───────────────────────────────────────────────────────────────

def _latest_post_failure(audit_log: str) -> dict | None:
    """取审计里最后一条 call_post_failure；读不到时返回 None。"""
    try:
        records = D._audit_records(audit_log)
    except D.DispatchError:
        return None
    for record in reversed(records):
        if record.get("event") == "call_post_failure":
            return record
    return None


def _auto_recover_rejected(args, path: str, state: dict,
                           call: dict) -> dict:
    """对刚发生的 dispatch_plan_rejected 尝试恢复原回复；不新增模型调用。

    只处理「最后一个轮次已是 rejected、审计最后一条失败正是该轮
    dispatch_plan_rejected」的情形。先 dry-run 验证，再正式应用；任何
    语义错误、字段冲突、身份/凭证不符都由 recover_consumed_reply 抛错，
    此处原样返回失败原因，不猜测、不重试。
    """
    rounds = state.get("rounds") or []
    if not rounds:
        return {"status": "skipped", "reason": "状态没有轮次记录"}
    target = rounds[-1]
    if not isinstance(target, dict) or not target.get("rejected"):
        return {"status": "skipped", "reason": "最后一个轮次不是拒绝轮次"}
    call_id = str(target.get("call_id") or "").strip()
    round_id = str(target.get("round_id") or "").strip()
    if not call_id or not round_id:
        return {"status": "skipped", "reason": "拒绝轮次缺少 call_id/round_id"}
    failure = _latest_post_failure(args.audit_log)
    if failure is None:
        return {"status": "skipped", "reason": "审计中没有 call_post_failure 记录"}
    # 必须是「这一次」失败，而不是历史遗留的拒绝轮次；否则一个无关的
    # 凭证失败可能顺手恢复更早的回复。
    if failure.get("error") != "dispatch_plan_rejected":
        return {"status": "skipped",
                "reason": f"最近一次失败不是 dispatch_plan_rejected："
                          f"{failure.get('error') or 'unknown'}"}
    if (str(failure.get("call_id") or "") != call_id
            or str(failure.get("round_id") or "") != round_id):
        return {"status": "skipped",
                "reason": "最近一次拒绝与状态中的最后轮次不匹配"}

    reply_path = str(call.get("reply_file") or "")
    if not reply_path or not os.path.isfile(reply_path):
        return {"status": "rejected", "reason": "回复文件不存在，无法恢复"}
    try:
        reply = _read_text(reply_path)
    except AutoError as exc:
        return {"status": "rejected", "reason": str(exc)}

    try:
        preview = D.recover_consumed_reply(
            path, state, args.audit_log, call_id=call_id, reply=reply,
            dry_run=True)
    except D.DispatchError as exc:
        return {"status": "rejected", "reason": f"dry-run 未通过：{exc}"}
    if preview.get("status") == "already_applied":
        return {"status": "already_applied",
                "round_id": preview.get("round_id") or round_id,
                "call_id": call_id}
    if preview.get("status") != "preview":
        return {"status": "rejected",
                "reason": f"dry-run 返回未知状态：{preview.get('status')!r}"}

    try:
        applied = D.recover_consumed_reply(
            path, state, args.audit_log, call_id=call_id, reply=reply,
            dry_run=False)
    except D.DispatchError as exc:
        return {"status": "rejected", "reason": f"恢复失败：{exc}"}
    return {"status": "applied", "round_id": round_id, "call_id": call_id,
            "mapping": applied.get("mapping") or [],
            "normalizer_version": applied.get("normalizer_version"),
            "before_state_sha256": applied.get("before_state_sha256"),
            "after_state_sha256": applied.get("after_state_sha256")}


NEXT_STEPS = {
    "execute": "主代理按 actions[].work_order 执行并回填 manifest，然后再次 drive；"
               "不需要用户转发或逐轮授权",
    "closed": "已收口，停止调用与执行",
    "wait_user": "按 reason 处理；PARTIAL 不在停止状态里",
    "blocked": "真实阻塞：说明缺口与所需授权，等待用户决定",
    "quota_wait": "配额/冷却中：不调用、不执行，恢复后再继续",
    "recover": "先按 pending_call 恢复，不重新发起请求",
    "review_pending": "先回填独立复核，再显式 drive",
    "failed": "调用失败：按退出码处理，同一请求不在调用内自动重发。有 pending 先按 "
              "pending_call 恢复；退出码 8 等配额恢复；3/4 且无 pending 时核对上游错误后可显式"
              "再次 drive；7 不得重试；9 须先修正 CPA 路由，再由人工决定是否重新发起",
    "segmentation_required": "输出预算达到真实上限：按 segmentation_plan 逐段处理证据，"
                             "再提交新的复核；不要重发完全相同请求",
    "budget_exhausted": "仅当显式运营上限被触及时出现；不是默认预算",
}


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
        "loop_stats": _loop_stats(state),
        "next_step": NEXT_STEPS.get(status, ""),
    }
    out.update(extra)
    return out


def _work_order(state: dict, action: dict) -> dict:
    """把内部工作项翻译成宿主可直接执行的工单。

    只读：不改写 SOL 安排的目标/约束/证据要求，也不把内部字段换成结论。
    除 SOL 原字段外补三类信息供主代理执行与回传：
    - 完成条件：本任务未闭合条件（以及该工作项引用的条件）；
    - 授权边界：原合同边界 + 「超出原始授权必须 BLOCKED」的硬约束；
    - 回传要求：manifest 路径与必须登记的原始证据。
    """
    assignee = str(action.get("assignee") or "zcode")
    unmet = D.unmet_criteria(state)
    criteria = [{
        "criterion_id": c.get("criterion_id"),
        "description": c.get("description") or "",
        "status": c.get("status") or "pending",
    } for c in unmet]
    boundaries = str((state.get("contract") or {}).get("boundaries") or "").strip()
    complete = bool(str(action.get("goal") or "").strip()
                    and str(action.get("expected_evidence") or "").strip())
    return {
        "id": action.get("id"),
        "title": action.get("title"),
        "assignee": assignee,
        # 执行主体：zcode 由主代理直接做；deepseek 由主代理派发宿主原生子代理。
        # 两者都由主代理负责回填结果与证据，SOL 不执行。
        "executed_by": ("main-agent" if assignee in ("zcode", "deepseek", "reasonix")
                        else "unknown"),
        "goal": action.get("goal"),
        "constraints": action.get("constraints"),
        "expected_evidence": action.get("expected_evidence"),
        "depends_on": action.get("depends_on") or [],
        "status": action.get("status"),
        "origin": action.get("origin") or "sol-plan",
        "derived_from_round_id": action.get("derived_from_round_id") or "",
        "directive_complete": complete,
        "completion_criteria": criteria,
        "authorization": (boundaries or "仅限原任务授权范围") +
                         "；不得自行扩大授权（生产部署/发布、真实数据写入、Git push、"
                         "外部消息、权限或全局配置修改、新增业务规则），"
                         "超出时按 BLOCKED 汇报并请求用户授权。",
        "return_contract": ("执行后用 manifest 回填：actions[].id/status/result/evidence_ids + "
                            "evidence[].id/file/kind/note/include，再运行 "
                            "`copilot_auto.py drive --result-file <manifest>`；"
                            "只登记摘要不算证据，必须给出可重读的原始产物。"),
    }


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
        # 宿主执行衔接：work_order 是同一工作项的完整可执行视图（不改写 SOL 字段）。
        "work_order": _work_order(state, a),
    } for a in D.ready_actions(state)]


def _latest_verified_round(state: dict) -> dict | None:
    rounds = [r for r in state.get("rounds") or []
              if isinstance(r, dict) and r.get("model_identity_ok") is True]
    return rounds[-1] if rounds else None


def _round_material(state: dict, round_id: str) -> list[str]:
    """该轮之后已经产生了哪些新材料（执行结果 / 复核 / 报告 / 已完成派工）。

    新材料意味着「下一步不是再派活，而是把结果交给 SOL 判断」：本地派生工作项
    只在那一轮确实什么都没发生时才成立。
    """
    rid = str(round_id or "").strip()
    if not rid:
        return []
    found = []
    for entry in state.get("native_results") or []:
        if isinstance(entry, dict) and str(entry.get("round_id") or "") == rid:
            found.append("execution")
    for entry in state.get("native_reviews") or []:
        if isinstance(entry, dict) and str(entry.get("round_id") or "") == rid:
            found.append("review")
    for entry in state.get("reports") or []:
        if isinstance(entry, dict) and str(entry.get("round_id") or "") == rid:
            found.append(str(entry.get("kind") or "report"))
    for entry in state.get("actions") or []:
        # `dropped` 也是终态：派生项被放弃并写明原因后，本轮已有事实需要交回 SOL，
        # 不能因为状态不是 done/failed 就再派生一条同义 AUTO 项无限空转。
        if (isinstance(entry, dict)
                and str(entry.get("derived_from_round_id") or "") == rid
                and str(entry.get("status") or "") in ("done", "failed", "blocked",
                                                       "dropped")):
            found.append("derived-work")
    return found


def _partial_without_actions(state: dict) -> dict | None:
    """最近一轮 PARTIAL 是否「说了没做好，但没给可执行工作」。

    只认最近已验证轮次：`verdict=partial` 且该轮没有新增任何安排、也没有任何
    新材料。已经给过安排、执行后再次进入 GUIDANCE_NEEDED 的情形不算——那需要把
    执行结果交给 SOL 判断，不是再派一次活；同样的道理，派生工作项执行完并登记
    证据/报告之后，也必须把新材料交回 SOL，不能本地再派一轮。
    """
    latest = _latest_verified_round(state)
    if latest is None:
        return None
    if str(latest.get("verdict") or "").strip().lower() != "partial":
        return None
    if latest.get("actions"):
        return None
    if D.ready_actions(state):
        return None
    if _round_material(state, str(latest.get("round_id") or "")):
        return None
    return latest


def _reply_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _derive_partial_work(path: str, state: dict, reply_text: str) -> dict | None:
    """PARTIAL 没有可执行安排时，本地派生下一步工作项（0 请求）。"""
    if _partial_without_actions(state) is None:
        return None
    snapshot = _snapshot(state)
    action = D.derive_work(state, reply_text)
    D.save_state(path, state, expect=snapshot)
    return action


def _loop_stats(state: dict) -> dict:
    """轮次与用量观察值：只记录，不作为停止条件。"""
    budget = state.get("budget") or {}
    usage = budget.get("usage") or {}
    return {
        "round_seq": int(state.get("round_seq") or 0),
        "rounds": len(state.get("rounds") or []),
        "requests": int(usage.get("requests") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "unknown_usage_calls": int(usage.get("unknown_usage_calls") or 0),
        "limits_enforced": False,
        "checkpoint": int(state.get("round_seq") or 0) > 0
                      and int(state.get("round_seq") or 0)
                      % int(budget.get("checkpoint_every") or D.CHECKPOINT_EVERY) == 0,
        "note": "轮次/Token 只用于审计与观察，不构成停止条件",
    }


def _maybe_advance_partial(path: str, state: dict, loop: dict,
                           common: dict, workdir: str) -> tuple[dict, dict, dict]:
    """PARTIAL 但没有可执行安排时，推进到「下一步工作」而不是停下等用户。

    这一步是纯本地的：不发请求、不改轮次、不改 SOL 的判断。派生动作明确标注
    `origin=partial-without-plan`，目标与证据要求来自未闭合条件与回复原文摘录。
    """
    if loop.get("phase") != "GUIDANCE_NEEDED":
        return state, loop, common
    # loop_step(persist=True) 可能刚写过四门记录：调用方手上的 state 已过期，
    # 派生前必须重读，否则保存会覆盖（或被 save_seq 拒绝）那次写入。
    state = D.load_state(path)
    latest = _partial_without_actions(state)
    if latest is None:
        return state, loop, common
    reply_path = os.path.join(
        workdir, f"round-{int(state.get('round_seq') or 0)}-reply.md")
    try:
        action = _derive_partial_work(path, state, _reply_text(reply_path))
    except (D.DispatchError, OSError) as exc:
        common["partial_bridge_error"] = str(exc)
        return state, loop, common
    if action is None:
        return state, loop, common
    state = D.load_state(path)
    loop = D.loop_step(path, persist=True)
    common["derived_work"] = {
        "id": action.get("id"),
        "title": action.get("title"),
        "origin": action.get("origin"),
        "derived_from_round_id": action.get("derived_from_round_id"),
        "reason": "最近一轮 PARTIAL 没有给出可执行安排；按未闭合条件本地派生，"
                  "不是 SOL 直接安排",
    }
    return state, loop, common


def _emit(payload: dict, code: int) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


# ── 主流程 ─────────────────────────────────────────────────────────────────

def _stop_reason(loop: dict, state: dict) -> str:
    """保留接口但不再因无进展计数直接停止。

    2026-11-08 起连续无进展由 `drive` 转入 SOL 重新分析（见 `_no_progress_material`）；
    只有没有新信息、缺少正式 REVIEW 授权或 SOL 给出有证据的 BLOCKED 时才停。
    轮次数字、检查点与累计用量仍然只观察、不停止。
    """
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
        # 已收口任务不接受执行回填：否则 --result-file 会被静默忽略而 exit 0，调用方
        # 会误以为本轮执行已被采纳。仅纯只读调用（无回填参数）才直接返回 CLOSED。
        if args.result_file or args.native_result:
            raise AutoError(
                "任务已 CLOSED，不接受执行回填（--result-file / --native-result）："
                "本次未写入任何状态。如需继续，先按新目标重建任务合同。")
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
        manifest_evidence = _manifest_evidence(manifest)
        actions = _manifest_actions(manifest, state, manifest_evidence)
        if args.dry_run:
            preview_loop = D.loop_step(path, persist=False)
            preview = _result(
                "manifest_valid", state, preview_loop, dry_run=True,
                manifest={"actions": [a["id"] for a in actions],
                          "evidence": [e["id"] for e in manifest_evidence]})
            # 不改变状态；只把阶段的只读预演结果标成 DRY_RUN。
            preview["state_phase"] = preview.get("phase")
            preview["phase"] = "DRY_RUN"
            return _emit(preview, EXIT_OK)
        # 阶段门禁必须先于回填：待复核（REVIEW_PENDING）、配额等待、BLOCKED(G4)
        # 等状态下，磁盘不能前进——否则被复核的执行快照会被新证据/新报告污染，
        # 用户看到「停止」，状态却在走。预检只读（persist=False），不写盘。
        gate = D.loop_step(path, persist=False)
        gate_phase = str(gate.get("phase") or "")
        if gate_phase in ("CLOSED", "RECOVER", "BLOCKED", "QUOTA_WAIT",
                          "REVIEW_PENDING"):
            raise AutoError(
                f"当前阶段 {gate_phase} 不接受执行回填：先处理该阶段（recover / 复核 / "
                "等待冷却 / 用户决策），再提交新一轮执行结果；本次未写入任何状态。")
        manifest_info = _apply_manifest(path, state, manifest)
        manifest_evidence = manifest_info["evidence"]
        state = D.load_state(path)

    loop = D.loop_step(path, persist=not args.dry_run)
    # loop_step 可能刚写入四门记录（persist=True）：重新加载，避免后续用旧快照
    # 保存时覆盖或被 save_seq 拒绝。
    state = D.load_state(path)
    phase = loop.get("phase")
    common = {"dry_run": bool(args.dry_run), "manifest": manifest_info,
              "state_path": path}

    # PARTIAL 但 SOL 没给可执行安排：先用本地派生把循环推进到 EXECUTE，
    # 不用同一组证据再刷一次付费调用，也不把决定权交回用户。
    if not args.dry_run:
        state, loop, common = _maybe_advance_partial(
            path, state, loop, common, os.path.abspath(args.work_dir))
        phase = loop.get("phase")

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

    # GUIDANCE_NEEDED / REJUDGE：无进展不再直接终止。真正的判定放在
    # stamp_progress 之后（streak 可能在本轮才达到阈值），见下方出站前分支。

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
        if loop.get("no_progress"):
            prompt += "\n" + _no_progress_prompt(_no_progress_material(state))
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
    # 复核原文入包不受 --no-auto-evidence 影响：该开关只关闭可选的 manifest
    # 证据打包，不能关闭复核证据链的完整性要求。
    auto_pack = _build_auto_pack(os.path.abspath(args.work_dir), state,
                                 manifest_evidence, question,
                                 include_manifest=not args.no_auto_evidence)
    if auto_pack:
        evidence.append(auto_pack)
    # 本轮无进展指纹在真实调用前落盘。连续无进展不再直接停止：先确认是否有
    # 新信息，再把失败项/未闭合条件/证据提交 SOL 重新分析；没有新信息才停。
    reanalysis = None
    try:
        stamp = D.stamp_progress(path, prompt)
    except (OSError, D.DispatchError) as exc:
        raise AutoError(f"无法记录本轮无进展指纹：{exc}") from None
    state = D.load_state(path)
    if bool(loop.get("no_progress")) or bool(stamp.get("no_progress")):
        mode = str(state.get("mode") or "").strip().upper()
        if mode == "REVIEW" and not getattr(args, "auto_review", False):
            # 无进展恢复不能绕过正式 REVIEW 的显式授权门槛。
            return _emit(_result("wait_user", state, loop, **common,
                                 reason="no_progress_reanalysis_requires_auto_review"),
                         EXIT_WAIT)
        material = _no_progress_material(state)
        last_digest = str((state.get("budget") or {}).get(
            "last_no_progress_digest") or "")
        if last_digest and last_digest == material["digest"]:
            # 相同失败/未闭合项/证据：不再发完全相同的新请求。
            state = D.load_state(path)
            loop = D.loop_step(path, persist=True)
            return _emit(_result(
                "wait_user", state, loop, **common,
                reason="no_progress_no_new_information",
                no_progress={"digest": material["digest"],
                             "failed_actions": [a["id"] for a in material["actions"]
                                                if a["status"] == "failed"
                                                or a["fail_streak"] > 0],
                             "unmet_criteria": [c["id"] for c in material["criteria"]
                                                if c["status"] != "met"]}),
                EXIT_WAIT)
        reanalysis = {"material": material, "digest": material["digest"]}
        prompt += "\n" + _no_progress_prompt(material)
        pack = _build_no_progress_pack(os.path.abspath(args.work_dir), material)
        evidence.append(pack)
        reanalysis["pack"] = pack
        common["no_progress_reanalysis"] = {
            "digest": material["digest"],
            "failed_actions": [a["id"] for a in material["actions"]
                               if a["status"] == "failed" or a["fail_streak"] > 0],
            "unmet_criteria": [c["id"] for c in material["criteria"]
                               if c["status"] != "met"],
            "pack": pack,
        }
    if stamp.get("no_progress") and not reanalysis:
        state = D.load_state(path)
        loop = D.loop_step(path, persist=True)
        return _emit(_result("wait_user", state, loop, **common,
                             reason="no_progress",
                             stamp=stamp), EXIT_WAIT)

    extra_round_reason = ""
    extra_round_basis = ""
    if str(state.get("mode") or "").strip().upper() == "REVIEW":
        extra_round_reason, extra_round_basis = _auto_extra_round_reason(
            state, _review_ledger_path(args, os.path.abspath(args.work_dir)),
            [str(e.get("id") or "") for e in manifest_evidence
             if isinstance(e, dict)])
        if reanalysis:
            extra_round_reason += (
                "；本轮为连续无进展重新分析：提交失败工作项、未闭合条件与已有证据"
                f"诊断包 {reanalysis['digest'][:12]}，用于分析原方案为何失效；"
                "该诊断包不是交付物证据，不用于核销完成条件。")
            extra_round_basis = extra_round_basis or "new-evidence"
    call = _call_gpt(args, state, prompt, evidence, extra_round_reason,
                     extra_round_basis)
    call_info = {"exit": call["exit"], "prompt_file": call["prompt_file"],
                 "reply_file": call["reply_file"], "evidence": evidence,
                 "extra_round_reason": extra_round_reason,
                 "segmentation_plan": call.get("segmentation_plan")}
    if call["exit"] != 0:
        state = D.load_state(path)
        pending = state.get("pending_call")
        plan_path = str(call.get("segmentation_plan") or "")
        if plan_path and os.path.exists(plan_path):
            # 输出预算已无法继续提高：转入分段证据处理，不把同一失败请求
            # 当作普通失败重试。正式 REVIEW 轮次没有被消耗。
            loop = D.loop_step(path, persist=True)
            return _emit(_result(
                "segmentation_required", state, loop, **common, call=call_info,
                segmentation_plan=plan_path,
                reason="输出预算达到真实上限；按分段计划处理证据后重新提交"), EXIT_EXECUTE)
        if call["exit"] == 12:
            loop = D.loop_step(path, persist=True)
            return _emit(_result(
                "budget_exhausted", state, loop, **common, call=call_info,
                reason="call_gpt 拒绝出站：请求数/Token 预算已耗尽"),
                EXIT_WAIT)
        recovery = None
        if call["exit"] == 2:
            # 只对「回复已取得、轮次已消费、但 plan 解析/应用失败」的拒绝轮次
            # 自动尝试恢复；凭证/归属失败没有可恢复回复，仍走失败关闭路径。
            recovery = _auto_recover_rejected(args, path, state, call)
            call_info["recovery"] = recovery
        if recovery and recovery.get("status") in ("applied", "already_applied"):
            # 恢复已复用原 call_id 完成入账；继续走与成功调用相同的阶段求值，
            # 但本次 drive 不会再发起任何 GPT 请求。
            common["auto_recovered"] = recovery.get("status")
        else:
            loop = D.loop_step(path, persist=True)
            reason = f"GPT 调用失败（退出码 {call['exit']}）；"
            if recovery and recovery.get("status") == "rejected":
                reason += "自动恢复未通过：" + str(recovery.get("reason") or "-")
            elif isinstance(pending, dict):
                reason += "存在 pending，下一轮先 recover。"
            else:
                reason += "本轮未入账：轮次与工作项未变（调用前的无进展指纹已落盘）。"
            return _emit(_result(
                "failed", state, loop, **common, call=call_info,
                reason=reason), call["exit"])

    state = D.load_state(path)
    if reanalysis:
        # 本轮重新分析已被 SOL 受理：记录诊断指纹，下一轮只有在失败项/
        # 未闭合条件/证据真正变化时才再次发起，避免无限重复相同请求。
        snapshot = _snapshot(state)
        budget = state.setdefault("budget", {})
        budget["last_no_progress_digest"] = reanalysis["digest"]
        budget["last_no_progress_round_seq"] = int(state.get("round_seq") or 0)
        D.save_state(path, state, expect=snapshot)
        state = D.load_state(path)
    loop = D.loop_step(path, persist=True)
    # 与出站前同理：loop_step 可能落盘四门记录，后续写入必须基于重读后的状态。
    state = D.load_state(path)
    phase = loop.get("phase")
    # 本轮 SOL 若给出 PARTIAL 但没有可执行安排，同样立刻派生下一步工作项，
    # 让循环继续到 EXECUTE，而不是以 guidance_without_actions 停下。
    state, loop, common = _maybe_advance_partial(
        path, state, loop, common, os.path.abspath(args.work_dir))
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
        info = _apply_native_result(path, state, envelope,
                                    source_file=args.native_result)
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
    p.add_argument("--max-output-tokens", type=int, default=None,
                   help="可选初始输出预算；默认不传，由服务商/模型使用真实默认上限。"
                        "截断时 call_gpt 会自适应升档重发（不消耗正式 REVIEW 轮次）")
    p.add_argument("--max-output-tokens-ceiling", type=int, default=None,
                   help="可选输出预算显式上界；缺省不设人为上限")
    p.add_argument("--max-output-retries", type=int, default=None,
                   help="可选输出升档重发次数上限；默认不限制。只有显式传入时才"
                        "作为运营限制透传给 call_gpt.py（0 表示不升档）")
    p.add_argument("--max-retries", type=int, default=None,
                   help="透传给 call_gpt.py 的最大重试次数；0 表示只尝试一次"
                        "（native 默认 0，legacy 默认沿用 call_gpt 的 2）")
    p.add_argument("--auto-review", action="store_true",
                   help="显式声明用户已要求连续正式 REVIEW，允许自动驱动承接 "
                        "REVIEW 任务；未给出时 REVIEW 仍只能走人工复核台账路径")
    p.add_argument("--review-ledger", default="",
                   help="REVIEW 复核台账路径；缺省为 <work-dir>/review-ledger.json，"
                        "跨轮保持同一台账以合并历史证据与往轮裁决")
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

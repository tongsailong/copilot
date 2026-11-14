#!/usr/bin/env python3
"""Copilot v6 持续推进器：本地阶段机 + 工作单 + 原生信封回填。

它**不调用任何模型**，也不等待外部判断：

* `drive`：读取状态 → 本地求值 → 命中 PASS 就收口；命中 BLOCKED/CLOSED 就停下
  报告；有可领取工作项就输出工作单并以 EXIT_EXECUTE 退出；PARTIAL 则本地派生
  下一步工作项后继续（不是任务终点）。
* `ingest`：把 `native_agent.py` 生成的执行/复核信封校验后回填状态，包括证据
  登记、工作项结果、原生复核记录。

退出码
------
* 0  ：任务已 CLOSED 或本次已收口；
* 10 ：有工作项需要 ZCode 执行（工作单已打印）；
* 11 ：等待用户（真实 BLOCKED 或任务已 CLOSED 需授权重开）；
* 12 ：本地内部错误（状态不一致、信封被拒、派生上限等），需人工检查。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import governance as G  # noqa: E402
import native_agent as NA  # noqa: E402
import native_bridge as NB  # noqa: E402

EXIT_DONE = 0
EXIT_EXECUTE = 10
EXIT_WAIT = 11
EXIT_ERROR = 12

MAX_DRIVE_STEPS = 32


class AutoError(Exception):
    pass


def _load_json(path: str) -> object:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as exc:
        raise AutoError(f"无法读取 {path}：{exc}") from None
    except ValueError as exc:
        raise AutoError(f"无法解析 JSON {path}：{exc}") from None


def _state_path(args) -> str:
    value = str(getattr(args, "state", "") or os.environ.get("COPILOT_STATE", "")).strip()
    if not value:
        raise AutoError("缺少 --state（或用 COPILOT_STATE 指定任务状态文件）。")
    return value


# ── 工作单 ─────────────────────────────────────────────────────────────────
def work_order(state: dict, action: dict, state_path: str,
               next_eid: str = "E1") -> str:
    constraints = str(action.get("constraints") or "").strip()
    match = re.match(r"^AUTO-(C[1-9][0-9]{0,2})", str(action.get("id") or ""))
    criterion_hint = match.group(1) if match else "C1"
    lines = [
        f"# 工作项 {action.get('id')} · {action.get('title')}",
        "",
        f"- 任务：{state.get('task_id')}",
        f"- 目标：{state.get('goal')}",
        f"- 执行方：{action.get('assignee')}（由 ZCode 主代理执行或派发原生子代理）",
        f"- 风险：{state.get('risk')} · 后端：{state.get('execution_backend')}",
        "",
        "## 要做的事",
        str(action.get("goal") or "").strip(),
        "",
        "## 约束",
        constraints or "（无额外约束；仍须遵守用户原始授权与项目规则）",
        "",
        "## 期望证据",
        str(action.get("expected_evidence") or "").strip(),
        "",
        "## 回填方式",
        f"1. 登记原始证据：`copilot_dispatch.py evidence --state {state_path} "
        f"--add --id {next_eid} --file <绝对路径>`（或 `--capture-cmd` 采集命令输出）",
        f"2. 回填工作项：`copilot_dispatch.py record --state {state_path} "
        f"--id {action.get('id')} --status done --result \"实际结果\" --evidence {next_eid}`",
        f"3. 若完成条件因此闭合：`copilot_dispatch.py criterion --state ... "
        f"--id {criterion_hint} --status met --evidence {next_eid}`",
        "4. 继续推进：`copilot_auto.py drive --state ...`",
        "",
        "## 授权提醒",
        "工作单不是授权：不得越过用户原始授权；commit / push / 发布 / 部署 / 付费 / "
        "生产数据 / Provider 或安全策略修改仍需用户明确授权。",
    ]
    return "\n".join(lines)


def _print_work_orders(state: dict, actions: list[dict], state_path: str,
                       dispatch_path: str = "") -> None:
    next_eid = G.next_evidence_id(state)
    for index, action in enumerate(actions):
        if index:
            print("\n---\n")
        print(work_order(state, action, state_path,
                         next_eid if index == 0 else f"E{int(next_eid[1:]) + index}"))
    if dispatch_path:
        print(f"\n原生派发已写入：{dispatch_path}")
        print("执行：用 Executor 子代理完成工作项后，运行")
        print("  native_agent.py execution --state ... --dispatch ... --work-item ...")
        print("  copilot_auto.py ingest --state ... --result-file <execution.json>")


def _write_dispatch(state: dict, ready: list[dict], path: str,
                    args) -> str:
    # 只有「计划内」工作项走原生派发；PARTIAL 本地派生的恢复/复核工作项由
    # ZCode 主代理直接执行并用 CLI 回填，不属于任何计划。
    plan_id = str(ready[0].get("plan_id") or "")
    batch = [a for a in ready if str(a.get("plan_id") or "") == plan_id]
    plan = NB._plan_record(state, plan_id) if plan_id else None
    existing = state.get("native_dispatch")
    env = NB.build_dispatch(state, batch, plan=plan, existing=existing)
    state = G.load_state(path)
    state["native_dispatch"] = env
    G.save_state(path, state)
    out = args.dispatch_out or (os.path.splitext(path)[0] + ".dispatch.json")
    out = os.path.abspath(out)
    directory = os.path.dirname(out) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{out}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(env, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, out)
    return out


# ── drive ──────────────────────────────────────────────────────────────────
def drive(args) -> int:
    path = _state_path(args)
    for step in range(MAX_DRIVE_STEPS):
        state = G.load_state(path)
        evaluation = G.evaluate(state)
        phase = evaluation.get("phase")
        print(f"[{step}] {state.get('task_id')} → {phase}：{evaluation.get('reason')}")
        if phase == "CLOSED":
            print(G.render_status(state, evaluation))
            return EXIT_DONE
        if phase == "BLOCKED":
            print(G.render_status(state, evaluation))
            return EXIT_WAIT
        if phase == "PASS":
            closure = G.close_task(path)
            print(f"四门通过，任务已收口：iteration={closure['iteration_id']} "
                  f"by={closure['closed_by']}")
            return EXIT_DONE
        if phase == "RUNNING":
            ready = G.ready_actions(state)
            if not ready:
                print("阶段为 RUNNING 但没有可领取工作项；重新求值。")
                continue
            chosen = ready[: int(args.max_actions or 1)]
            dispatch_path = ""
            dispatchable = [a for a in chosen if str(a.get("plan_id") or "")]
            if state.get("execution_backend") == "native" and dispatchable:
                try:
                    dispatch_path = _write_dispatch(state, dispatchable, path, args)
                except NB.BridgeError as exc:
                    raise AutoError(f"生成原生派发失败 [{exc.code}]：{exc}") from None
            _print_work_orders(state, chosen, path, dispatch_path)
            return EXIT_EXECUTE
        # PARTIAL：本地派生下一步，不等待任何外部判断。
        before = G.progress_digest(state)
        created = G.derive_work(state)
        if not created:
            print("PARTIAL 但无法派生新的工作项（派生上限或状态异常）；"
                  "请检查未闭合条件与停摆项。", file=sys.stderr)
            return EXIT_ERROR
        G.begin_iteration(state, note=str(evaluation.get("reason") or ""))
        G.save_state(path, state)
        print("已派生工作项：" + ", ".join(a["id"] for a in created))
        after = G.progress_digest(G.load_state(path))
        if after == before and not any(a["origin"] == "partial-next"
                                       for a in created):
            print("警告：本次派生没有改变进展摘要；下一轮若重复，将记录替代方案要求。")
    print(f"连续 {MAX_DRIVE_STEPS} 步仍未收敛；状态已保留，请检查工作项是否被反复"
          "派生。", file=sys.stderr)
    return EXIT_ERROR


# ── ingest ─────────────────────────────────────────────────────────────────
def _existing_result(state: dict, sha: str) -> dict | None:
    for rec in state.get("native_results") or []:
        if isinstance(rec, dict) and str(rec.get("result_sha256") or "") == sha:
            return rec
    return None


def _existing_review(state: dict, sha: str) -> dict | None:
    for rec in state.get("native_reviews") or []:
        if isinstance(rec, dict) and str(rec.get("result_sha256") or "") == sha:
            return rec
    return None


def _record_attempt(path: str, kind: str, envelope: dict, code: str,
                    message: str) -> None:
    """把被拒的信封尝试追加到任务目录的 review-attempts.jsonl（只记元数据）。

    实现复用 `governance.log_attempt`，让 ingest 被拒与 `native_agent.py review`
    生成被拒落在同一份尝试记录里，`review-metrics` 口径一致。
    """
    G.log_attempt(path, kind, code, message, envelope)


def _ingest_execution(path: str, envelope: dict) -> int:
    state = G.load_state(path)
    dispatch = state.get("native_dispatch")
    try:
        normalized = NB.validate_execution_result(envelope, dispatch, state)
    except NB.BridgeError as exc:
        _record_attempt(path, "execution", envelope, exc.code, exc.message)
        raise AutoError(f"执行信封被拒绝 [{exc.code}]：{exc}") from None
    sha = normalized["result_sha256"]
    if _existing_result(state, sha):
        print(f"执行结果已回填（{sha[:16]}…），幂等跳过。")
        return EXIT_DONE
    for item in normalized["evidence"]:
        try:
            G.register_evidence(state, item["id"], kind=item.get("kind") or "file",
                                file_path=item["file"], note=item.get("note") or "")
        except G.GovernanceError as exc:
            raise AutoError(f"证据 {item.get('id')} 登记失败：{exc}") from None
    G.save_state(path, state)
    result_text = f"{normalized['summary']}\n\n{normalized['result']}"
    try:
        G.record_action(path, normalized["work_item_id"], normalized["status"],
                        result=result_text,
                        evidence_ids=[item["id"] for item in normalized["evidence"]])
    except G.GovernanceError as exc:
        raise AutoError(f"工作项回填失败：{exc}") from None
    state = G.load_state(path)
    state.setdefault("native_results", []).append({
        "result_sha256": sha,
        "work_item_id": normalized["work_item_id"],
        "dispatch_id": normalized["dispatch_id"],
        "plan_id": normalized["plan_id"],
        "plan_sha256": normalized["plan_sha256"],
        "iteration_id": normalized["iteration_id"],
        "status": normalized["status"],
        "summary": normalized["summary"],
        "result": normalized["result"][:G.MAX_RESULT_CHARS],
        "evidence": normalized["evidence"],
        "executor": normalized["executor"],
        "timing": normalized["timing"],
        "usage": normalized["usage"],
        "received": G.now_iso(),
    })
    G._history(state, "native_result_ingested", {
        "work_item_id": normalized["work_item_id"],
        "result_sha256": sha,
        "dispatch_id": normalized["dispatch_id"],
        "status": normalized["status"],
        "evidence_ids": [item["id"] for item in normalized["evidence"]],
    })
    G.save_state(path, state)
    print(f"执行结果已回填：{normalized['work_item_id']} → {normalized['status']} "
          f"sha256={sha[:16]}… 证据 {', '.join(i['id'] for i in normalized['evidence'])}")
    return EXIT_DONE


def _ingest_review(path: str, envelope: dict) -> int:
    state = G.load_state(path)
    dispatch = state.get("native_dispatch")
    bound = str(envelope.get("execution_result_sha256") or "")
    execution = _existing_result(state, bound)
    if execution is None:
        raise AutoError("复核绑定的执行结果尚未回填；先 ingest 执行信封。")
    try:
        normalized = NB.validate_review_result(envelope, dispatch, execution, state)
    except NB.BridgeError as exc:
        _record_attempt(path, "review", envelope, exc.code, exc.message)
        raise AutoError(f"复核信封被拒绝 [{exc.code}]：{exc}") from None
    sha = normalized["result_sha256"]
    if _existing_review(state, sha):
        print(f"复核结果已回填（{sha[:16]}…），幂等跳过。")
        return EXIT_DONE
    if normalized["g4_eligible"]:
        problems = NA.verify_review_host_record(envelope)
        if problems:
            _record_attempt(path, "review", envelope, "review_host_record_changed",
                            ", ".join(problems))
            raise AutoError(
                "G4 复核的宿主记录复核未通过：" + ", ".join(problems) +
                "；不得把该结论当作 G4 依据。")
    state = G.load_state(path)
    state.setdefault("native_reviews", []).append({
        **normalized,
        # 宿主记录摘要随记录固化：G4 收口时用它检测记录被改写，避免只信任
        # g4_eligible 布尔值（首次核对已在上面按 metadata/rollout 完成）。
        "host_sha256": G._sha256_text(G.canonical_json(normalized.get("host") or {})),
        "received": G.now_iso(),
    })
    findings = normalized.get("findings") or []
    G._history(state, "native_review_ingested", {
        "work_item_id": normalized["work_item_id"],
        "result_sha256": sha,
        "verdict": normalized["verdict"],
        "g4_eligible": normalized["g4_eligible"],
        "review_scope": normalized["review_scope"],
        "based_on_review_sha256": normalized["based_on_review_sha256"],
        "evidence_manifest": normalized["evidence_manifest"],
        "finding_count": len(findings),
        "blocking_count": sum(1 for f in findings
                              if str(f.get("severity") or "") == "blocking"),
        "execution_evidence_unbound": normalized.get(
            "execution_evidence_unbound") or [],
    })
    G.save_state(path, state)
    print(f"复核结果已回填：{normalized['work_item_id']} → {normalized['verdict']}"
          f"（g4_eligible={normalized['g4_eligible']}）")
    return EXIT_DONE


def ingest(args) -> int:
    path = _state_path(args)
    if not args.result_file and not args.review_file:
        raise AutoError("请提供 --result-file 和/或 --review-file。")
    if args.result_file:
        envelope = _load_json(args.result_file)
        kind = NB.classify_native_result(envelope)
        if kind != "execution":
            raise AutoError("--result-file 不是执行结果信封。")
        _ingest_execution(path, envelope)
    if args.review_file:
        envelope = _load_json(args.review_file)
        kind = NB.classify_native_result(envelope)
        if kind != "review":
            raise AutoError("--review-file 不是复核结果信封。")
        _ingest_review(path, envelope)
    return EXIT_DONE


# ── CLI ────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="copilot_auto.py",
        description="Copilot v6 持续推进器（不调用任何模型）")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("drive", help="推进任务：收口 / 输出工作单 / 派生下一步")
    p.add_argument("--state", required=True)
    p.add_argument("--dispatch-out", default="",
                   help="native 后端的 dispatch 输出路径")
    p.add_argument("--max-actions", type=int, default=1,
                   help="一次输出的工作单数量（默认 1，避免并行改写冲突）")
    p.set_defaults(func=drive)

    p = sub.add_parser("ingest", help="回填原生执行/复核信封")
    p.add_argument("--state", required=True)
    p.add_argument("--result-file", default="")
    p.add_argument("--review-file", default="")
    p.set_defaults(func=ingest)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except (AutoError, G.GovernanceError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

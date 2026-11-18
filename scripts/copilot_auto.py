#!/usr/bin/env python3
"""Copilot v8 持续推进器：本地阶段机 + 工作单 + 原生执行信封回填。

它**不调用任何模型**，也不等待外部判断：

* `drive`：读取状态 → 本地求值 → 命中 PASS 就收口；命中 BLOCKED/CLOSED 就停下
  报告；有可领取工作项就输出工作单并以 EXIT_EXECUTE 退出；PARTIAL 则本地派生
  下一步工作项后继续（不是任务终点）。
* `ingest`：把 `native_agent.py` 生成的执行信封校验后回填状态。SOL 认知与用户要求的
  独立复核由 `sol_review.py` 承担；G4 原生复核信封已随 G4 退役，本模块不再接受。

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
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import governance as G  # noqa: E402
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
    if not batch:
        raise AutoError("原生派发没有可派发的工作项。")
    plan = NB._plan_record(state, plan_id) if plan_id else None
    existing = state.get("native_dispatch")
    env = NB.build_dispatch(state, batch, plan=plan, existing=existing)
    state = G.load_state(path)
    # 求值与写入之间另一进程可能已收口；CLOSED 状态不允许再写入派发。
    if bool((state.get("closure") or {}).get("closed")):
        raise AutoError("任务已收口（CLOSED），不能写入原生派发。")
    state["native_dispatch"] = env
    G.save_state(path, state)
    out = args.dispatch_out or (os.path.splitext(path)[0] + ".dispatch.json")
    out = os.path.abspath(out)
    directory = os.path.dirname(out) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dispatch-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(env, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return out


# ── drive ──────────────────────────────────────────────────────────────────
def drive(args) -> int:
    path = _state_path(args)
    # 单次驱动内工作区不会被本进程改写：代码快照与证据摘要只算一次，
    # 后续求值/派生/收口复用同一份，避免同一命令里重复扫描 git 工作区。
    cache: dict = {}
    for step in range(MAX_DRIVE_STEPS):
        state = G.load_state(path)
        evaluation = G.evaluate(state, cache=cache)
        phase = evaluation.get("phase")
        print(f"[{step}] {state.get('task_id')} → {phase}：{evaluation.get('reason')}")
        sol = evaluation.get("sol") or {}
        # PASS 阶段 drive 会立即收口，此时再提示 SOL 已无法执行；收口前的建议
        # 由 `status` / `review-admission` 在收口前给出（见 SKILL.md 顺序）。
        if phase != "PASS" and sol.get("action") in ("think", "review"):
            print(f"SOL {sol.get('mode')} 建议（{sol.get('recommended_action')}，"
                  f"复杂度 {(sol.get('complexity') or {}).get('level') or '-'}）："
                  f"{sol.get('reason')}")
            if sol.get("command"):
                print(f"  可选调用：{sol['command']}")
            print("  说明：SOL 只是 advisory；不影响阶段、门禁与收口判定。")
        if phase == "CLOSED":
            print(G.render_status(state, evaluation, cache=cache))
            return EXIT_DONE
        if phase == "BLOCKED":
            print(G.render_status(state, evaluation, cache=cache))
            return EXIT_WAIT
        if phase == "PASS":
            closure = G.close_task(path, cache=cache)
            print(f"G1–G3 通过，任务已收口：verdict={closure.get('verdict')} "
                  f"iteration={closure['iteration_id']} by={closure['closed_by']}")
            quality = closure.get("quality") or {}
            if quality.get("verdict") == "pass_with_warnings":
                print("质量结论："
                      f"{str(quality.get('raw_verdict') or 'PASS_WITH_WARNINGS').upper()}"
                      "（原始结论保留，不改写为 PASS）")
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
                # 原生 dispatch 一次只覆盖一个计划；工作单必须与 dispatch 同集合，
                # 不能出现“打印了工作项但 dispatch 文件里没有”的情况。
                anchor = str(dispatchable[0].get("plan_id") or "")
                chosen = [a for a in chosen
                          if not str(a.get("plan_id") or "")
                          or str(a.get("plan_id") or "") == anchor]
                dispatchable = [a for a in chosen
                                if str(a.get("plan_id") or "") == anchor]
                try:
                    dispatch_path = _write_dispatch(state, dispatchable, path, args)
                except NB.BridgeError as exc:
                    raise AutoError(f"生成原生派发失败 [{exc.code}]：{exc}") from None
            _print_work_orders(state, chosen, path, dispatch_path)
            return EXIT_EXECUTE
        # PARTIAL：本地派生下一步，不等待任何外部判断。
        before = G.progress_digest(state)
        created = G.derive_work(state, cache=cache)
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


def _record_attempt(path: str, kind: str, envelope: dict, code: str,
                    message: str) -> None:
    """把被拒的信封尝试追加到任务目录的 review-attempts.jsonl（只记元数据）。

    实现复用 `governance.log_attempt`，让执行 ingest 被拒与信封生成被拒落在同一份
    尝试记录里，`review-metrics` 口径一致。
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
    result_text = f"{normalized['summary']}\n\n{normalized['result']}"
    # 证据、工作项回填、native_results 与 binding 消费在同一次 save_state 落盘：
    # 任一步失败都不留下“action=done 但无结果/未消费”的单边成功。
    try:
        G.record_action_state(
            state, normalized["work_item_id"], normalized["status"],
            result=result_text,
            evidence_ids=[item["id"] for item in normalized["evidence"]])
    except G.GovernanceError as exc:
        raise AutoError(f"工作项回填失败：{exc}") from None
    binding = dict(normalized.get("execution_binding") or {})
    if binding:
        stored = G.find_execution_binding(state, str(binding.get("binding_id") or ""))
        if stored is None:
            raise AutoError(
                f"执行 binding {binding.get('binding_id')} 不存在；不能回填该执行结果。")
        if stored.get("consumed"):
            raise AutoError(
                "执行 binding 已被消费；同一 binding 不能重放另一份执行结果。")
        if str(stored.get("nonce") or "") != str(binding.get("nonce") or ""):
            raise AutoError("执行 binding nonce 与状态记录不一致。")
        stored["consumed"] = True
        stored["consumed_by_result_sha256"] = sha
        stored["consumed_at"] = G.now_iso()
    state.setdefault("native_results", []).append({
        "result_sha256": sha,
        "task_id": normalized.get("task_id") or "",
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
        "executor_metadata_path": normalized.get("executor_metadata_path") or "",
        "timing": normalized["timing"],
        "usage": normalized["usage"],
        "test_commands": normalized.get("test_commands") or [],
        "test_artifacts": normalized.get("test_artifacts") or [],
        "code_scope_sha256": normalized.get("code_scope_sha256") or "",
        "code_scope_excluded_paths": normalized.get(
            "code_scope_excluded_paths") or [],
        "execution_binding": binding,
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


def ingest(args) -> int:
    path = _state_path(args)
    if not args.result_file:
        raise AutoError("请提供 --result-file（执行信封）。原生复核信封已随 G4 "
                        "退役；独立复核请用 copilot_dispatch.py sol-review。")
    envelope = _load_json(args.result_file)
    kind = NB.classify_native_result(envelope)
    if kind != "execution":
        raise AutoError("--result-file 不是执行结果信封。")
    _ingest_execution(path, envelope)
    return EXIT_DONE


# ── CLI ────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="copilot_auto.py",
        description="Copilot 持续推进器（不调用任何模型）")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("drive", help="推进任务：收口 / 输出工作单 / 派生下一步")
    p.add_argument("--state", required=True)
    p.add_argument("--dispatch-out", default="",
                   help="native 后端的 dispatch 输出路径")
    p.add_argument("--max-actions", type=int, default=1,
                   help="一次输出的工作单数量（默认 1，避免并行改写冲突）")
    p.set_defaults(func=drive)

    p = sub.add_parser("ingest", help="回填原生执行信封")
    p.add_argument("--state", required=True)
    p.add_argument("--result-file", default="")
    p.set_defaults(func=ingest)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except (AutoError, G.GovernanceError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"ERROR: 状态写入/文件操作失败：{exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

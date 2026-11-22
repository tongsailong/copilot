#!/usr/bin/env python3
"""Copilot 持续推进器：本地阶段机 + 工作单 + 原生执行信封回填。

它不做远程判断，也不等待外部裁决；唯一可能发起模型调用的是 SOL 生命周期
调度：默认 `--sol auto` 下每条命令最多消费 1 条 recommended 义务（1 次请求，
失败只记录、不阻断），`--sol off|advise` 或测试进程不调用。

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
    unmet = [str(item.get("criterion_id") or "")
             for item in (state.get("contract") or {}).get("criteria") or []
             if isinstance(item, dict)
             and str(item.get("status") or "pending") != "met"]
    # 条件提示必须来自本工作项的真实归属，不能一律写 C1：计划动作可用
    # target_criteria 声明条件，AUTO-C<n> 天然承载 C<n>，其余动作才回退到
    # 第一条未闭合条件（仍可能与本项无关，故用占位说明而非假装确定）。
    targets = [str(cid) for cid in (action.get("target_criteria") or [])
               if str(cid).strip()]
    target_unmet = [cid for cid in targets if cid in unmet]
    if target_unmet:
        criterion_hint = target_unmet[0]
        criterion_extra = target_unmet[1:]
    elif match:
        criterion_hint = match.group(1)
        criterion_extra = []
    elif unmet:
        criterion_hint = unmet[0]
        criterion_extra = []
    else:
        criterion_hint = "<C?>"
        criterion_extra = []
    blocked = G._blocked_items_summary(state)
    claim = G.active_claims(state).get(str(action.get("id") or ""))
    field = []
    if unmet:
        field.append("- 未闭合完成条件：" + "、".join(unmet))
    if blocked:
        field.append("- 已阻塞工作项：" + "；".join(
            f"{item['id']}（{item['block_id']}）原因 {item['reason']}；需要 "
            f"{item['needs']}" for item in blocked))
    if claim:
        handoff = int(claim.get("handoff_count") or 0)
        field.append(f"- 本次调度已领取：{claim.get('claim_id')} · 领取方 "
                     f"{claim.get('owner')} · 第 {handoff} 次交办 · 租约到期 "
                     f"{claim.get('expires_at')}")
        if handoff >= 3:
            field.append("- 注意：同一工作项已多次交办仍未回填。先判断是否真的受阻；"
                         "受阻就 block 写明原因/所需条件，路线失败就 record failed "
                         "让运行时派生 AUTO-REPLAN，不要原样重复。")
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
    ]
    if field:
        lines.extend(["## 任务现场", *field, ""])
    lines.extend([
        "## 期望证据",
        str(action.get("expected_evidence") or "").strip(),
        "",
        "## 回填方式",
        f"1. 登记原始证据：`copilot_dispatch.py evidence --state {state_path} "
        f"--add --id {next_eid} --file <绝对路径>`（或 `--capture-cmd` 采集命令输出）",
        f"2. 回填工作项：`copilot_dispatch.py record --state {state_path} "
        f"--id {action.get('id')} --status done --result \"实际结果\" --evidence {next_eid}`",
        f"3. 若完成条件因此闭合：`copilot_dispatch.py criterion --state ... "
        f"--id {criterion_hint} --status met --evidence {next_eid}`"
        + (f"（本工作项还对应 {'、'.join(criterion_extra)}，分别执行同一命令）"
           if criterion_extra else ""),
        "4. 继续推进：`copilot_auto.py drive --state ...`",
        "",
        "## 授权提醒",
        "工作单不是授权：不得越过用户原始授权；commit / push / 发布 / 部署 / 付费 / "
        "生产数据 / Provider 或安全策略修改仍需用户明确授权。",
    ])
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
def _dispatch_module():
    """惰性导入调度执行器：模块级不引入网络客户端（sol_review）。

    `copilot_dispatch` 会导入 `sol_review`（唯一允许访问网络与凭据的模块）；
    `copilot_auto` 的模块级依赖必须保持零网络，所以只在真正调度时才导入。
    """
    import copilot_dispatch as D
    return D


def _drive_sol_step(path: str, args, *, gates_passed: bool = False,
                    prefer_family: str = "",
                    closure_ready: dict | None = None,
                    cache: dict | None = None) -> dict:
    """drive 内的有界调度步：失败只打印，不改变 drive 的退出码与阶段。"""
    try:
        D = _dispatch_module()
        return D._sol_schedule_step(
            path, args, gates_passed=gates_passed,
            execute=(D._sol_policy(args) == "auto"),
            prefer_family=prefer_family,
            closure_ready=closure_ready, cache=cache)
    except Exception as exc:  # noqa: BLE001 — SOL 不可用不阻断工程推进
        print(f"SOL 调度未完成（{exc}）；按本地证据继续，不阻断。", file=sys.stderr)
        return {"executed": "", "error": str(exc)}


def drive(args) -> int:
    path = _state_path(args)
    # 单次驱动内工作区不会被本进程改写：代码快照与证据摘要只算一次，
    # 后续求值/派生/收口复用同一份，避免同一命令里重复扫描 git 工作区。
    cache: dict = {}
    # 生命周期调度（v9.6）：drive 是有界消费点，每条命令最多执行 1 个义务。
    # 收口前的窗口（family E）在 PASS 分支单独处理，不占用这个额度。
    sol_used = False
    for step in range(MAX_DRIVE_STEPS):
        state = G.load_state(path)
        evaluation = G.evaluate(state, cache=cache)
        phase = evaluation.get("phase")
        print(f"[{step}] {state.get('task_id')} → {phase}：{evaluation.get('reason')}")
        sol = evaluation.get("sol") or {}
        # 非 PASS 阶段：把建议显示为调度义务（auto 时由调度步真正执行）。
        if phase != "PASS" and sol.get("action") in ("consult", "plan", "review"):
            print(f"SOL {sol.get('mode')} 建议（{sol.get('recommended_action')}，"
                  f"阶段 {sol.get('stage') or '-'}，"
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
            # continuation() 对 BLOCKED 恒给出 required=False；若这里仍要求继续，
            # 说明求值口径不一致（例如阻塞范围标记错误），报错而不是空转到上限。
            if (evaluation.get("continuation") or {}).get("required"):
                raise AutoError(
                    "阶段为 BLOCKED 但续跑判定仍要求继续："
                    "请检查阻塞范围（--work-item）与完成条件归属。")
            return EXIT_WAIT
        if phase == "PASS":
            # 收口顺序固定：最终工程验证（PASS + 验证完成）→ SOL 收口综合判断 →
            # 收口。验证缺口（合同要求的测试没有执行痕迹）时两步都不做：交给
            # continuation 的 verify 动作，由主代理先补齐验证。
            readiness = evaluation.get("closure_readiness")
            if not isinstance(readiness, dict):
                readiness = G.closure_readiness(state, cache=cache)
            if not readiness.get("ready"):
                print("G1–G3 已通过，但最终工程验证尚未完成："
                      "暂不执行 SOL 收口判断、暂不收口。")
                for blocker in G.closure_blockers_text(readiness):
                    print(f"- {blocker}")
                print("先执行合同要求的必要测试并登记验证结果，再重新 drive。")
                return EXIT_EXECUTE
            # 这里必须消费调度，否则收口 CONSULT 在 drive 路径上永远不可达。
            _drive_sol_step(path, args, gates_passed=True, prefer_family="E",
                            closure_ready=readiness, cache=cache)
            # 顺序：最终验证 → SOL 收口判断 → ZCode 独立验收 → G1–G3 收口。刚执行完
            # E 就收口会跳过 ZCode 的判断（收口后又不能再登记，已被 CLOSED 拒绝），
            # 所以这里停下来把判断交回主代理；采纳/修改/否决任一条都能继续。
            pending_acceptance = G.closure_acceptance_pending(G.load_state(path))
            if pending_acceptance:
                print("SOL 收口判断已完成，等待 ZCode 独立验收后再收口：")
                print(f"- 调用：{pending_acceptance['call_id']}"
                      f"（stage=closure，最终验证摘要 "
                      f"{pending_acceptance['digest']}）")
                print("下一步：copilot_dispatch.py sol-accept --state <state> "
                      f"--call-id {pending_acceptance['call_id']} "
                      "--status accepted|modified|rejected --rationale <依据>")
                print("登记判断后再 drive：此时才执行 G1–G3 收口。")
                return EXIT_EXECUTE
            closure = G.close_task(path, cache=cache, sol_entry="drive")
            print(f"G1–G3 通过，任务已收口：verdict={closure.get('verdict')} "
                  f"iteration={closure['iteration_id']} by={closure['closed_by']}")
            quality = closure.get("quality") or {}
            if quality.get("verdict") == "pass_with_warnings":
                print("质量结论："
                      f"{str(quality.get('raw_verdict') or 'PASS_WITH_WARNINGS').upper()}"
                      "（原始结论保留，不改写为 PASS）")
            return EXIT_DONE
        if phase == "RUNNING":
            cont = evaluation.get("continuation") or {}
            if cont.get("required"):
                print(f"续跑：{cont.get('action')} —— {cont.get('reason')}")
            if not sol_used:
                # 有界消费：先兑现当前的认知窗口（最多 1 次），再交付工作单，
                # 这样 SOL 的判断能影响本次执行而不是事后补记。
                sol_used = True
                before_seq = state.get("save_seq")
                _drive_sol_step(path, args)
                if G.load_state(path).get("save_seq") != before_seq:
                    # 调度步可能已写入调用记录：重新读取，避免领取被 CAS 拒绝。
                    state = G.load_state(path)
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
                # 原生派发已改写状态（save_seq 递增），必须重新读取再领取，
                # 否则领取写入会被 CAS 拒绝。
                state = G.load_state(path)
            # 调度领取落盘：同一工作项不会同时交给两个会话；租约到期自动失效，
            # 崩溃不会永久锁死工作。领取只是交办事实，不是执行完成。
            try:
                claimed = G.claim_actions(state, [a["id"] for a in chosen])
                if claimed["claimed"]:
                    G.save_state(path, state)
            except G.GovernanceError as exc:
                raise AutoError(
                    "状态在求值与领取之间被其它会话推进；请重新运行 drive："
                    f"{exc}") from None
            if not claimed["claimed"]:
                # 另一个会话刚领取了这些项：回到求值，不要重复打印。
                print("工作项已被其它会话领取；重新求值。")
                continue
            print(f"调度领取：{', '.join(claimed['claimed'])}（领取方 "
                  f"{claimed['owner']}，租约 {claimed['ttl']}s；回填后自动释放）")
            if claimed["busy"]:
                print("未领取（其它会话持有有效租约）：" + "、".join(claimed["busy"]))
            _print_work_orders(state, chosen, path, dispatch_path)
            return EXIT_EXECUTE
        # PARTIAL：本地派生下一步，不等待任何外部判断。
        cont = evaluation.get("continuation") or {}
        if not cont.get("required") and cont.get("action") == "wait":
            # 唯一可能：工作项已被其它会话领取且没有其它可派生缺口。等待对方的
            # 回填或租约到期即可，这不是整体 BLOCKED，也不需要用户授权。
            print(f"等待其它会话回填：{cont.get('reason')}")
            return EXIT_WAIT
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
        description="Copilot 持续推进器（--sol auto 时每条命令最多 1 次建议性调用）")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("drive", help="推进任务：收口 / 输出工作单 / 派生下一步")
    p.add_argument("--state", required=True)
    p.add_argument("--dispatch-out", default="",
                   help="native 后端的 dispatch 输出路径")
    p.add_argument("--sol", default="",
                   choices=("", "off", "advise", "auto"),
                   help="SOL 生命周期调度：auto（默认，有界执行 1 个义务）/ "
                        "advise（只给命令）/ off（只记录义务）")
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
        code = args.func(args) or 0
    except (AutoError, G.GovernanceError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"ERROR: 状态写入/文件操作失败：{exc}", file=sys.stderr)
        return EXIT_ERROR
    # 会话绑定：drive/ingest 成功推进（0/10/11）后才表示本会话负责该任务。
    # 出错（12）时状态未推进，不留下会把 Stop 误触发到本会话的绑定。
    if code != EXIT_ERROR:
        try:
            G.touch_session(str(getattr(args, "state", "") or ""))
        except Exception:  # noqa: BLE001 - 旁路绑定失败不能改变命令结论
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())

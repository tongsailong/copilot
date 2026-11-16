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
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import g4_shadow as SH  # noqa: E402
import host_records as HR  # noqa: E402
import governance as G  # noqa: E402
import native_bridge as NB  # noqa: E402
import review_output  # noqa: E402
import review_policy as RP  # noqa: E402

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
            print(f"四门通过，任务已收口：verdict={closure.get('verdict')} "
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


def _review_output_bytes(state: dict, normalized: dict,
                         shadow_record: dict) -> tuple[bytes | None, str]:
    """取 Reviewer 原始输出字节：outputFile 优先，否则宿主 message/part。"""
    host = normalized.get("host") or {}
    path = str(host.get("output_file") or "").strip()
    if path and os.path.isfile(path):
        with open(path, "rb") as fh:
            return fh.read(), "outputFile"
    child = str((normalized.get("reviewer") or {}).get("child_session_id") or "")
    if child:
        # 信封生成时的宿主库路径随 host 记录固化；老信封没有该字段时回退默认库。
        db = str(host.get("host_db") or "").strip() or os.path.expanduser(
            "~/.zcode/cli/db/db.sqlite")
        output = HR.assistant_output(db, child)
        if output:
            return output["text"].encode("utf-8"), "db.message.part"
    return None, ""


def _finalize_candidate(path: str, state: dict, normalized: dict,
                        envelope: dict, shadow_record: dict, execution: dict,
                        review_sha: str) -> dict:
    """候选 G4 闭环：校验 shadow/TE/binding/attempt/原始输出并一次性标记。

    只写候选字段与 raw output 文件，不改变正式 `g4_eligible` 或正式消费语义。
    """
    reasons: list[str] = []
    attestation = shadow_record.get("attestation") or {}
    test_execution = shadow_record.get("test_execution") or {}
    if shadow_record.get("status") != "attested":
        reasons.append("shadow_not_attested")
    if shadow_record.get("new_g4_eligible") is not True:
        reasons.append("shadow_not_eligible")
    binding_id = str(test_execution.get("execution_binding_id") or "")
    binding = G.find_execution_binding(state, binding_id) if binding_id else None
    te_status = str(test_execution.get("status") or "")
    if te_status == "not_applicable":
        # 只有当前合同显式声明“不要求测试”时才接受 N/A，且 binding 不能带
        # 测试要求；amend 后旧 binding 的 N/A 不再有效。
        if not G.contract_no_test_binding(binding, state):
            reasons.append("test_execution_not_applicable_invalid")
    elif te_status != "verified":
        reasons.append("test_execution_not_verified")
    if binding is None:
        reasons.append("execution_binding_missing")
    elif str(binding.get("consumed_by_result_sha256") or "") != \
            str(execution.get("result_sha256") or ""):
        reasons.append("execution_binding_not_bound_to_execution")
    if binding is not None and str(binding.get("contract_sha256") or "") != \
            str(state.get("contract_sha256") or ""):
        reasons.append("execution_binding_contract_changed")
    attempt = G.find_review_attempt(
        state, str(normalized.get("review_attempt_id") or ""))
    if attempt is None:
        reasons.append("review_attempt_missing")
    else:
        previous = str(attempt.get("candidate_consumed_by_result_sha256") or "")
        if previous and previous != review_sha:
            reasons.append("candidate_attempt_consumed_by_other")
        formal_by = str(attempt.get("consumed_by_result_sha256") or "")
        if attempt.get("consumed") and formal_by and formal_by != review_sha:
            reasons.append("attempt_consumed_by_other")
    # 只有其余条件全绿才读取/持久化原始输出；避免留下“未通过却已落盘”的
    # 半成品证据。宿主原文与信封结论必须一致（防止信封改写结论）。
    raw = None
    source = ""
    if not reasons:
        raw, source = _review_output_bytes(state, normalized, shadow_record)
        if raw is None:
            reasons.append("raw_output_missing")
        else:
            raw_sha = hashlib.sha256(raw).hexdigest()
            expected = str((normalized.get("host") or {}).get("output_sha256")
                           or attestation.get("output_sha256") or "")
            if expected and raw_sha != expected.lower():
                reasons.append("raw_output_sha_mismatch")
            else:
                try:
                    parsed = review_output.parse_review_output(
                        raw.decode("utf-8", errors="replace"))
                except review_output.ReviewOutputError:
                    reasons.append("raw_output_unparseable")
                    parsed = None
                if isinstance(parsed, dict):
                    # 与信封的“原始字段”比对（而非 validate 后的规范化结构）：
                    # findings 会被补齐六个键并小写 severity，verdict 会被小写，
                    # 直接比 normalized 会把带 findings 的合法复核误判为不一致。
                    if str(parsed.get("verdict") or "").strip().lower() != \
                            str(envelope.get("verdict") or "").strip().lower():
                        reasons.append("envelope_host_field_mismatch:verdict")
                    for field in ("findings", "unverified", "changed_files"):
                        if G.canonical_json(parsed.get(field) or []) != \
                                G.canonical_json(envelope.get(field) or []):
                            reasons.append(
                                f"envelope_host_field_mismatch:{field}")
                if not reasons and isinstance(
                        shadow_record.get("attestation"), dict):
                    try:
                        stored = G.store_review_output(path, raw, raw_sha)
                    except (OSError, G.GovernanceError) as exc:
                        reasons.append(
                            f"raw_output_persist_failed:{type(exc).__name__}")
                    else:
                        attestation["raw_output"] = {
                            "path": stored["path"], "sha256": stored["sha256"],
                            "bytes": stored["bytes"], "source": source,
                            "parser": review_output.PARSER_VERSION,
                        }
                elif not reasons:
                    reasons.append("attestation_missing")
    candidate = {
        "eligible": not reasons,
        "reasons": sorted(set(reasons)),
        "shadow_id": str(shadow_record.get("shadow_id") or ""),
        "binding_id": binding_id,
        "test_execution_status": str(test_execution.get("status") or ""),
        "raw_output_sha256": str(
            (attestation.get("raw_output") or {}).get("sha256") or ""),
        "review_attempt_id": str(
            normalized.get("review_attempt_id") or ""),
        "review_sha256": review_sha,
        "decided_at": G.now_iso(),
    }
    if candidate["eligible"] and isinstance(attempt, dict):
        attempt["candidate_consumed_by_result_sha256"] = review_sha
        attempt["candidate_consumed_at"] = candidate["decided_at"]
    shadow_record["attestation"] = attestation
    shadow_record["candidate"] = candidate
    return candidate


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
    shadow_attempt = G.find_review_attempt(
        state, str(normalized.get("review_attempt_id") or "")) \
        if normalized.get("review_attempt_id") else None
    # 影子取证在正式 G4 宿主复核/attempt 判定之前运行；不写 consume marker，
    # 任何异常只记 g4_attestations，不影响下面的正式放行与退出码。
    shadow_record = SH.build(
        normalized, state, shadow_attempt, state_path=path,
        db=str((normalized.get("host") or {}).get("host_db") or ""),
        expected_target=G.final_evidence_digest(state))
    # 正式 G4 由新链决定：高风险任务只有 attested + 新链 eligible 才是正式
    # eligible；旧 `g4_eligible` 只作为信封结构预检，不参与正式放行。
    risk_high = str(state.get("risk") or "").lower() == "high"
    formal_g4 = bool(risk_high
                     and shadow_record.get("status") == "attested"
                     and shadow_record.get("new_g4_eligible") is True)
    reasons = list(shadow_record.get("new_reason") or [])
    shadow_error = shadow_record.get("error") or {}
    if isinstance(shadow_error, dict) and shadow_error.get("code"):
        reasons.append(f"shadow:{shadow_error['code']}")
    incremental = shadow_record.get("incremental") or {}
    if isinstance(incremental, dict):
        reasons.extend(f"shadow:{item}" for item in incremental.get("problems") or [])
    reasons.extend(f"shadow:{item}" for item in shadow_record.get("differences") or [])
    test_execution = shadow_record.get("test_execution") or {}
    if test_execution.get("status") not in ("verified", "not_applicable"):
        reasons.extend("test_execution:" + str(item)
                       for item in test_execution.get("problems") or [])
    normalized["g4_eligible"] = formal_g4
    normalized["g4_source"] = "candidate" if formal_g4 else ""
    normalized["g4_reasons"] = sorted(set(str(item) for item in reasons
                                          if str(item)))
    if formal_g4:
        # attempt 是派发前固化的不可变快照；ingest 只接受仍与当前状态一致的
        # attempt，并把它消费给这一份结果，防止旧输出换新 target 重放。
        attempt_problems = G.review_attempt_problems(
            normalized, state, check_current=True)
        if attempt_problems:
            _record_attempt(path, "review", envelope, "review_attempt_rejected",
                            ", ".join(attempt_problems))
            raise AutoError(
                "G4 复核的 review attempt 校验未通过：" + ", ".join(attempt_problems)
                + "；旧 Reviewer 输出不能绑定到新快照，请新建 attempt 重做复核。")
        attempt = G.find_review_attempt(
            state, str(normalized.get("review_attempt_id") or ""))
        if attempt is None:
            raise AutoError(
                "G4 复核引用的 review attempt 不存在；请先创建 attempt 再回填。")
        if attempt.get("consumed"):
            _record_attempt(path, "review", envelope, "review_attempt_consumed",
                            "attempt already consumed")
            raise AutoError(
                "G4 复核引用的 review attempt 已被消费；不能用同一 attempt "
                "重放另一份结果，请新建 attempt。")
    state = G.load_state(path)
    # 候选 G4 评估先于 attempt 消费：评估本身抛异常时不能留下“复核已入账、
    # attempt 已消费、candidate 永远失败”的半成品；此时直接拒绝，状态不变，
    # 修正后可重试同一 attempt。
    candidate = {"eligible": False, "reasons": ["candidate_not_evaluated"]}
    try:
        candidate = _finalize_candidate(
            path, state, normalized, envelope, shadow_record, execution, sha)
    except Exception as exc:
        _record_attempt(path, "review", envelope, "candidate_internal_error",
                        f"{type(exc).__name__}: {exc}")
        raise AutoError(
            f"G4 候选评估内部错误（{type(exc).__name__}）：{exc}；"
            "本次复核未入账、attempt 未消费，修正后可重试。") from None
    authoritative = bool(normalized["g4_eligible"] and candidate.get("eligible"))
    if normalized["g4_eligible"]:
        # 校验后重新加载，确保消费与复核登记基于同一份状态；任何一边变化都
        # 失败关闭，而不是把“校验通过”当成可写入。
        attempt = G.find_review_attempt(
            state, str(normalized.get("review_attempt_id") or ""))
        late_problems = G.review_attempt_problems(
            normalized, state, check_current=True)
        if attempt is None or late_problems or attempt.get("consumed"):
            codes = late_problems or (["review_attempt_consumed"]
                                      if attempt and attempt.get("consumed")
                                      else ["review_attempt_foreign"])
            _record_attempt(path, "review", envelope, "review_attempt_rejected",
                            ", ".join(codes))
            raise AutoError(
                "G4 复核的 review attempt 在回填前发生变化："
                + ", ".join(codes) + "；请重新创建 attempt 并复核。")
        attempt["consumed"] = True
        attempt["consumed_by_result_sha256"] = sha
        attempt["consumed_at"] = G.now_iso()
        attempt["reviewer"] = {
            "agent_id": str((normalized.get("reviewer") or {}).get("agent_id") or ""),
            "child_session_id": str(
                (normalized.get("reviewer") or {}).get("child_session_id") or ""),
            "profile_id": str(
                (normalized.get("reviewer") or {}).get("profile_id") or ""),
            "bound_at": G.now_iso(),
        }
    # Finding 台账在正式记录写入前合并：稳定 ID、跨轮去重、生命周期状态都从
    # 正式 G4（且候选可用）重算；普通/参考性复核只能新增或重复报告，不能把
    # 已有 blocking/major 改成 verified，避免 g4_eligible=false 清掉台账门禁。
    ledger = RP.merge_review(state, normalized, authoritative=authoritative)
    normalized["finding_refs"] = ledger["refs"]
    stop_attempt = G.find_review_attempt(
        state, str(normalized.get("review_attempt_id") or ""))
    stop = None
    if str(normalized.get("verdict") or "").lower() == "pass" and \
            (authoritative or not risk_high):
        stop = RP.record_stop(
            state, normalized,
            code_scope_sha256=str(
                (stop_attempt or {}).get("code_scope_sha256") or ""),
            authoritative=authoritative)
    if stop is not None:
        normalized["review_stop_id"] = stop["stop_id"]
    state.setdefault("native_reviews", []).append({
        **normalized,
        "candidate_g4": candidate,
        # 宿主记录摘要随记录固化：G4 收口时用它检测记录被改写，避免只信任
        # g4_eligible 布尔值（首次核对已在上面按 metadata/rollout 完成）。
        "host_sha256": G._sha256_text(G.canonical_json(normalized.get("host") or {})),
        "received": G.now_iso(),
    })
    try:
        SH.append_attestation(state, shadow_record)
    except ValueError as exc:
        # 影子字段损坏不应反向阻断正式复核；把它修复为只含本条记录的新列表，
        # 并在 history 保留原错误，避免下一轮继续阻断。
        state["g4_attestations"] = [shadow_record]
        G._history(state, "g4_shadow_field_repaired", {
            "shadow_id": shadow_record.get("shadow_id") or "",
            "error": str(exc),
        })
    findings = normalized.get("findings") or []
    G._history(state, "native_review_ingested", {
        "work_item_id": normalized["work_item_id"],
        "result_sha256": sha,
        "verdict": normalized["verdict"],
        "g4_eligible": normalized["g4_eligible"],
        "g4_source": normalized.get("g4_source") or "",
        "review_scope": normalized["review_scope"],
        "based_on_review_sha256": normalized["based_on_review_sha256"],
        "evidence_manifest": normalized["evidence_manifest"],
        "finding_count": len(findings),
        "blocking_count": sum(1 for f in findings
                              if str(f.get("severity") or "") == "blocking"),
        "finding_refs": normalized.get("finding_refs") or [],
        "finding_rereported": ledger.get("rereported") or [],
        "finding_regressed": ledger.get("regressed") or [],
        "review_stop_id": str(normalized.get("review_stop_id") or ""),
        "execution_evidence_unbound": normalized.get(
            "execution_evidence_unbound") or [],
        "review_attempt_id": normalized.get("review_attempt_id") or "",
        "shadow_id": shadow_record.get("shadow_id") or "",
        "shadow_status": shadow_record.get("status") or "",
        "shadow_agreement": shadow_record.get("agreement") or "",
        "shadow_new_g4_eligible": shadow_record.get("new_g4_eligible"),
        "candidate_g4_eligible": bool(candidate.get("eligible")),
        "candidate_g4_reasons": candidate.get("reasons") or [],
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
    except OSError as exc:
        print(f"ERROR: 状态写入/文件操作失败：{exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

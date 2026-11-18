#!/usr/bin/env python3
"""Copilot 任务治理 CLI（ZCode Autonomous Development Edition）。

Copilot 做轻量协作治理：任务合同、工作项、证据、审计与本地 G1–G3。
除 `sol-review`（一次性零工具复核）与 `sol-advisory`（一次性零工具
CONSULT / PLAN 辅助分析）外，所有命令只读写任务状态，不调用模型；
SOL 的结论只是建议，工程执行与技术决策始终由 ZCode 主代理完成。

常用命令::

    copilot_dispatch.py start --state S --goal G --contract-file C.md
    copilot_dispatch.py plan --state S --plan P.json
    copilot_dispatch.py next --state S
    copilot_dispatch.py evidence --state S --add --id E1 --text "..."
    copilot_dispatch.py record --state S --id W1 --status done --result "..." --evidence E1
    copilot_dispatch.py criterion --state S --id C1 --status met --evidence E1
    copilot_dispatch.py closure-check --state S
    copilot_dispatch.py closure --state S --close
    copilot_dispatch.py sol-review --state S --materials M.md
    copilot_dispatch.py sol-advisory --state S --mode CONSULT --materials M.md
    copilot_dispatch.py legacy-status --state old.json --audit-log old.jsonl --ledger ledger.json

历史 v1/v2 SOL 状态与审计只能通过 `legacy-status` 只读查看，v6+ 不会驱动它们。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import governance as G  # noqa: E402
import agent_policy as AP  # noqa: E402
import review_policy as RP  # noqa: E402
import sol_review as SR  # noqa: E402

__version__ = "8.1.0"


class CLIError(Exception):
    pass


def _read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise CLIError(f"无法读取 {path}：{exc}") from None


def _load_json_doc(text: str, name: str):
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 2 and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise CLIError(f"{name} 不是合法 JSON：{exc}") from None


def _load_test_policy(args):
    """合同测试策略：--test-policy JSON 或 --test-policy-file；两者都空则不设置。"""
    inline = str(getattr(args, "test_policy", "") or "").strip()
    path = str(getattr(args, "test_policy_file", "") or "").strip()
    if inline and path:
        raise CLIError("--test-policy 与 --test-policy-file 只能给一个。")
    if not inline and not path:
        return None
    text = _read_text(path) if path else inline
    value = _load_json_doc(text, "测试策略")
    if not isinstance(value, dict):
        raise CLIError("测试策略必须是 JSON 对象。")
    return value


def _load_criteria(path: str):
    text = _read_text(path)
    try:
        return json.loads(text)
    except ValueError:
        items = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line = line.lstrip("-*+ ").strip()
            if line:
                items.append(line)
        return items


def _evidence_ids(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        for part in str(value).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _state_path(args) -> str:
    value = getattr(args, "state", "") or os.environ.get("COPILOT_STATE", "")
    value = str(value).strip()
    if not value:
        raise CLIError("缺少 --state（或用 COPILOT_STATE 指定任务状态文件）。")
    return value


def _print(obj) -> None:
    if isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def _brief(state: dict) -> str:
    cache: dict = {}
    ev = G.evaluate(state, cache=cache)
    return G.render_status(state, ev, cache=cache)


# ── 命令实现 ───────────────────────────────────────────────────────────────
def cmd_start(args) -> int:
    contract = _read_text(args.contract_file) if args.contract_file else ""
    criteria = _load_criteria(args.criteria_file) if args.criteria_file else None
    state_path = _state_path(args)
    workspace = args.workspace or G._git_workspace(
        os.path.dirname(os.path.abspath(state_path)))
    state = G.start_state(
        state_path, args.goal, contract=contract, criteria=criteria,
        risk=args.risk, execution_backend=args.backend,
        task_id=args.task_id or "", mode=args.mode or "", note=args.note or "",
        contract_source=args.contract_file or "",
        workspace=workspace,
        test_policy=_load_test_policy(args))
    print(f"任务已创建：{state['task_id']}")
    print(f"状态文件：{os.path.abspath(state_path)}")
    if state.get("workspace"):
        print(f"任务工作区：{state['workspace']}")
    print(f"完成条件：{len(state['contract']['criteria'])} 条 "
          f"（{', '.join(c['criterion_id'] for c in state['contract']['criteria'])}）")
    print(f"风险：{state['risk']} · 执行后端：{state['execution_backend']}")
    return 0


def cmd_next(args) -> int:
    state = G.load_state(_state_path(args))
    cache: dict = {}
    ev = G.evaluate(state, cache=cache)
    if args.json:
        _print({"task_id": state.get("task_id"), **ev})
    else:
        print(G.render_status(state, ev, cache=cache))
    return 0


def cmd_plan(args) -> int:
    text = _read_text(args.plan) if args.plan else sys.stdin.read()
    plan = _load_json_doc(text, "计划")
    result = G.plan_state(_state_path(args), plan, source=args.source or "zcode")
    if result["idempotent"]:
        print(f"计划已存在（{result['plan']['plan_id']}），未重复登记。")
        return 0
    print(f"计划已登记：{result['plan']['plan_id']}")
    for action in result["actions"]:
        print(f"- {action['id']} [{action['assignee']}] {action['title']}")
    return 0


def cmd_record(args) -> int:
    action = G.record_action(
        _state_path(args), args.id, args.status, result=args.result or "",
        evidence_ids=_evidence_ids(args.evidence))
    print(f"{action['id']} → {action['status']}"
          + (f"；证据 {', '.join(action['evidence_ids'])}"
             if action["evidence_ids"] else ""))
    return 0


def cmd_rerun(args) -> int:
    action = G.rerun_action(_state_path(args), args.id, reason=args.reason or "")
    print(f"{action['id']} → pending（第 {action['rerun_count']} 次重开）；"
          "旧结果已保存在 rerun_history。")
    return 0


def cmd_criterion(args) -> int:
    criterion = G.record_criterion(
        _state_path(args), args.id, args.status,
        evidence_ids=_evidence_ids(args.evidence))
    print(f"{criterion['criterion_id']} → {criterion['status']}"
          + (f"；证据 {', '.join(r['id'] for r in criterion['evidence_refs'])}"
             if criterion["evidence_refs"] else ""))
    return 0


def cmd_judgment(args) -> int:
    entry = G.record_judgment(
        _state_path(args), args.status, args.summary,
        evidence_ids=_evidence_ids(args.evidence))
    print(f"判断已记录：{entry['judgment_id']}（{entry['status']}）")
    return 0


def cmd_report(args) -> int:
    path = _state_path(args)
    verify_file = str(getattr(args, "verify_report", "") or "").strip()
    summary = str(getattr(args, "summary", "") or "").strip()
    if not verify_file and not summary:
        raise CLIError("report 需要 --summary 或 --verify-report 之一。")
    if verify_file:
        result = G.ingest_quality_report(path, verify_file, note=args.reason or "",
                                         summary=summary,
                                         workspace=getattr(args, "workspace", "") or "")
        report = result["report"]
        if result["idempotent"]:
            print(f"质量报告已接入（幂等，未重复登记）：{report.get('report_id')} "
                  f"verdict={report.get('raw_verdict')} "
                  f"acceptance={report.get('acceptance_status')}")
        else:
            print(f"质量报告已接入：{report.get('report_id')} "
                  f"verdict={report.get('raw_verdict')} "
                  f"acceptance={report.get('acceptance_status')}")
        if report.get("supersedes_report_id"):
            supersede = report.get("supersede") or {}
            print(f"替代关系：{report.get('supersedes_report_id')} → "
                  f"{report.get('report_id')}；继承 "
                  f"{'、'.join(supersede.get('carried_ids') or []) or '无'}；"
                  f"解决 {'、'.join(supersede.get('resolved_ids') or []) or '无'}")
        if report.get("code_scope_sha256"):
            print(f"代码范围已绑定：commit={report.get('code_scope', {}).get('git_head')} "
                  f"sha256={report.get('code_scope_sha256')[:16]}…")
        required = [entry.get("id") for entry in G._report_unresolved(report)]
        if required:
            criterion = result.get("criterion") or {}
            if not report.get("binding_sha256"):
                print("警告：报告绑定缺失；请重新接入后再接受。")
            state = G.load_state(path)
            if G.quality_summary(state).get("binding_current") is False:
                print("警告：该报告与当前目标/合同/条件不一致（绑定已过期）；"
                      "请重新验收并接入新报告，旧接受不继承。")
            print(f"已自动登记用户接受条件：{criterion.get('criterion_id')} "
                  f"（待接受 {'、'.join(required)}）")
            print("用户明确接受后运行：copilot_dispatch.py accept --state <state> "
                  f"--report {report.get('report_id')} --accept "
                  f"{','.join(required)} --by user --quote \"<用户原话>\"")
            print("未接受前 G2 拒绝收口；verify/执行方不得代替用户接受。")
        elif report.get("acceptance_required"):
            print("警告：PWW 报告没有可接受的 W/R；按 fail-closed 处理。")
        if summary:
            print("报告已登记：已随质量报告一并写入（同一保存轮次）。")
        return 0
    entry = G.add_report(path, summary,
                         blocked=bool(args.blocked), reason=args.reason or "")
    print(f"报告已登记：sha256={entry['report_sha256'][:16]}…")
    return 0


def cmd_accept(args) -> int:
    result = G.accept_pww(
        _state_path(args), report_id=args.report or "",
        ids=_evidence_ids(args.accept), accepted_by=args.by or "",
        quote=args.quote or "", note=args.note or "")
    report = result["report"]
    print(f"用户接受已登记：{report.get('report_id')} → {result['status']}")
    print(f"已接受：{'、'.join((report.get('acceptance') or {}).get('accepted_ids') or []) or '无'}")
    if result["pending_ids"]:
        print(f"仍未接受：{'、'.join(result['pending_ids'])}；G2 继续拒绝收口。")
    else:
        print("全部警告/残余风险已接受；原始 verdict 保持 "
              f"{report.get('raw_verdict')}，不改写为 PASS。")
    return 0


def cmd_quality(args) -> int:
    state = G.load_state(_state_path(args))
    summary = G.quality_summary(state)
    if args.json:
        _print({"task_id": state.get("task_id"), **summary,
                "reports": state.get("quality_reports") or []})
        return 0
    print(f"任务 {state.get('task_id')} 质量结论："
          f"{str(summary.get('raw_verdict') or summary.get('verdict')).upper()}")
    if summary.get("verdict") == "unrecorded":
        print("- 未接入 verify 结构化质量报告；不推断接受状态。")
    else:
        print(f"- 报告：{summary.get('report_id')}")
        if summary.get("supersedes_report_id"):
            print(f"- 替代：{summary.get('supersedes_report_id')} → "
                  f"{summary.get('report_id')}")
        print(f"- 接受状态：{summary.get('acceptance_status')}")
        if summary.get("accepted_ids"):
            print(f"- 已接受：{'、'.join(summary.get('accepted_ids'))}")
        if summary.get("unresolved_ids"):
            print(f"- 未解决：{'、'.join(summary.get('unresolved_ids'))}")
        if summary.get("code_scope_declared"):
            if summary.get("code_scope_sha256"):
                print(f"- 代码范围：{summary.get('code_scope_sha256')[:16]}…"
                      f"（{summary.get('code_scope_status') or 'unknown'}；"
                      "G2 收口时重算）")
                if summary.get("workspace"):
                    print(f"- 工作区：{summary.get('workspace')}"
                          f"（{summary.get('workspace_source') or 'unknown'}）")
                if summary.get("code_scope_captured_at"):
                    print("- 快照来源：Copilot ingest 重算 "
                          f"{summary.get('code_scope_captured_at')}；"
                          "不是报告生成时刻证明")
            else:
                print("- 代码范围：历史报告未绑定快照")
        elif summary.get("workspace"):
            print(f"- 工作区：{summary.get('workspace')}"
                  f"（{summary.get('workspace_source') or 'unknown'}）")
        if not summary.get("binding_current"):
            print("- 绑定已过期：目标/合同/条件/工作区变化，原接受不继承。")
    return 0


def cmd_decide(args) -> int:
    entry = G.add_decision(_state_path(args), args.text, source=args.source)
    print(f"决策已记录（{entry['source']}）：{entry['text'][:120]}")
    return 0


def cmd_block(args) -> int:
    block = G.add_block(_state_path(args), args.reason, args.needs)
    print(f"已进入 BLOCKED：{block['block_id']}")
    print(f"原因：{block['reason']}")
    print(f"所需条件：{block['needs']}")
    return 0


def cmd_unblock(args) -> int:
    result = G.resolve_block(_state_path(args), args.reason,
                             block_id=args.block_id or "")
    print(f"已解除 {result['resolved']} 条阻塞：{result['reason']}")
    if result.get("block_ids"):
        print("block_id：" + ", ".join(result["block_ids"]))
    return 0


def cmd_evidence(args) -> int:
    path = _state_path(args)
    state = G.load_state(path)
    if args.list:
        if not state.get("evidence"):
            print("（尚无证据）")
            return 0
        for entry in state["evidence"]:
            mark = "已归档" if entry.get("dropped") else "有效"
            print(f"{entry['id']} [{mark}] {entry['kind']} "
                  f"sha256={entry['sha256'][:16]}… {entry.get('path_hint')}")
        return 0
    if args.next_id:
        print(G.next_evidence_id(state))
        return 0
    if args.add and args.drop:
        raise CLIError("--add 与 --drop 只能选一个。")
    if args.drop:
        if not args.id:
            raise CLIError("--drop 需要 --id。")
        G.drop_evidence(state, args.id, args.reason or "")
        G.save_state(path, state)
        print(f"{args.id} 已归档。")
        return 0
    if not args.add:
        raise CLIError("请指定 --add / --drop / --list / --next-id。")
    eid = (args.id or "").strip() or G.next_evidence_id(state)
    existed = any(str(e.get("id") or "") == eid for e in state.get("evidence") or [])
    if args.capture_cmd and existed:
        # 采集会先写 <evidence>/<id>.md 再校验摘要；同 id 重采会覆盖已登记文件，
        # 即使随后报“摘要不同”，磁盘上的证据也已经被改写（G2 会判篡改）。
        # 已归档（--drop）的证据文件仍作为历史材料保留，同样不能被覆盖，所以
        # 这里的处置只能是换新 id。
        raise CLIError(f"证据 {eid} 已登记或已归档；--capture-cmd 会覆盖证据文件。"
                       "请使用新的 id 采集（归档证据的文件不会删除）。")
    if args.capture_cmd:
        if args.file or args.text is not None:
            raise CLIError("--capture-cmd 不能与 --file/--text 同时使用。")
        capture_dir = os.path.join(os.path.dirname(os.path.abspath(path)), "evidence")
        os.makedirs(capture_dir, exist_ok=True)
        out_path = os.path.join(capture_dir, f"{eid}.md")
        cmd = [sys.executable, "-B", os.path.join(HERE, "bridge_evidence.py"),
               "--cmd", args.capture_cmd, "--out", out_path,
               "--cwd", os.getcwd(), "--goal", f"任务 {state.get('task_id')} 证据 {eid}"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise CLIError(f"证据采集失败（exit {proc.returncode}）："
                           f"{(proc.stderr or proc.stdout).strip()[:1000]}")
        entry = G.register_evidence(state, eid, kind="capture",
                                    file_path=out_path, note=args.note or "")
    elif args.file:
        entry = G.register_evidence(state, eid, kind=args.kind or "file",
                                    file_path=args.file, note=args.note or "")
    elif args.text is not None:
        entry = G.register_evidence(state, eid, kind=args.kind or "text",
                                    text=args.text, note=args.note or "")
    else:
        raise CLIError("--add 需要 --file、--text 或 --capture-cmd 之一。")
    G.save_state(path, state)
    if existed:
        # 幂等登记不新增记录：明确说清楚，避免误以为又登记了一份证据。
        peers = [str(e.get("id")) for e in state.get("evidence") or []
                 if str(e.get("id") or "") != entry["id"]
                 and not e.get("dropped")
                 and str(e.get("sha256") or "") == entry["sha256"]]
        extra = f"；与 {'/'.join(peers)} 内容相同" if peers else ""
        print(f"{entry['id']} 已存在且摘要一致（未新增，sha256={entry['sha256'][:16]}…{extra}）")
    else:
        print(f"{entry['id']} 已登记：{entry['kind']} size={entry['size']} "
              f"sha256={entry['sha256'][:16]}…")
    return 0


def cmd_closure_check(args) -> int:
    path = _state_path(args)
    state = G.load_state(path)
    check = G.closure_check(
        state, task_dir=os.path.dirname(os.path.abspath(path)))
    if args.json:
        _print({"task_id": state.get("task_id"), **check})
        return 0
    print(f"任务 {state.get('task_id')} 三道门检查："
          f"{'PASS' if check['passed'] else 'FAIL'}")
    for name in ("G1", "G2", "G3"):
        gate = check["gates"][name]
        print(f"- {name}: {'PASS' if gate.get('passed') else 'FAIL'}")
        for reason in gate.get("reasons") or []:
            print(f"    {reason.get('code')}: {reason.get('detail')}")
    quality = check.get("quality") or {}
    if quality.get("verdict") and quality.get("verdict") != "unrecorded":
        print(f"- 质量结论：{str(quality.get('raw_verdict') or quality.get('verdict')).upper()}"
              f"（接受状态 {quality.get('acceptance_status')}；"
              f"已接受 {'、'.join(quality.get('accepted_ids') or []) or '无'}）")
    for warning in check.get("warnings") or []:
        print(f"- 提醒（不阻断）：{warning}")
    return 0


def cmd_review_metrics(args) -> int:
    path = _state_path(args)
    state = G.load_state(path)
    metrics = G.review_metrics(
        state, task_dir=os.path.dirname(os.path.abspath(path)))
    _print(metrics)
    return 0


def cmd_finding(args) -> int:
    path = _state_path(args)
    if args.list or not args.id:
        state = G.load_state(path)
        summary = G.list_findings(state)
        if args.json:
            _print(summary)
        else:
            items = summary.get("items") or []
            print(f"Finding 台账：{summary.get('total', 0)} 条；"
                  f"未闭合 blocking/major：{len(summary.get('blocking') or [])}")
            for item in items:
                where = str(item.get("file") or "")
                if item.get("line") is not None:
                    where += f":{item.get('line')}"
                print(f"- {item.get('finding_id')} [{item.get('severity')}] "
                      f"{item.get('status')} {where} {item.get('summary')}")
        return 0
    if not args.status:
        raise CLIError("更新 finding 需要 --status。")
    item = G.record_finding_status(
        path, args.id, args.status,
        evidence_ids=args.evidence, note=args.note)
    print(f"Finding {item['finding_id']} → {item['status']}"
          f"（{item.get('severity')}）")
    if item["status"] == "fixed":
        print("下一步：运行定向测试/检查并登记证据，再标记 verified；"
              "不需要重新派发完整复核。")
    return 0


def cmd_review_admission(args) -> int:
    """SOL 复核准入建议（只读，不是门禁）。"""
    path = _state_path(args)
    state = G.load_state(path)
    try:
        decision = G.review_admission(
            state, triggers=args.trigger, reason=args.reason)
    except (RP.ReviewPolicyError, SR.SolUsageError) as exc:
        raise CLIError(str(exc)) from None
    if args.json:
        _print(decision)
    else:
        print(f"SOL 复核准入：{decision['decision']}"
              f"（tier={decision.get('tier') or decision['risk']}）")
        print(decision.get("detail") or "")
        if decision.get("triggers"):
            print("触发因素：" + ", ".join(decision["triggers"]))
        print("说明：SOL 是建议性独立审查（advisory），不是收口门禁；单轮调用，不自动二审。")
    return 0


def _write_atomic(path: str, text: str) -> str:
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise CLIError(f"无法写入 {path}：{exc}") from None
    return path


def _pending_result_path(task_dir: str, digest: str,
                         subdir: str = "sol-reviews") -> str:
    return os.path.join(task_dir, subdir, f"pending-{digest}.json")


def _load_pending_result(task_dir: str, digest: str,
                         subdir: str = "sol-reviews") -> dict | None:
    """读取已取得响应但尚未登记完成的 SOL 结果，避免再次付费。"""
    path = _pending_result_path(task_dir, digest, subdir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    if str(value.get("materials_sha256") or "") != digest:
        return None
    if not str(value.get("reply_sha256") or ""):
        return None
    return value


def _invoke_sol_once(*, state_path: str, task_dir: str, prompt: str,
                     digest: str, mode: str, args, audit_log: str,
                     audit_subdir: str) -> tuple[dict | None, int]:
    """共享的一次调用路径：pending 恢复 → 一次请求 → 落可恢复结果。

    返回 `(result, exit_code)`；result 为 None 表示失败且已打印真实原因。
    只有 REVIEW 走 `run_review`，CONSULT/PLAN 走 `run_advisory`。
    """
    recovered = False
    result = _load_pending_result(task_dir, digest, audit_subdir)
    if result is not None:
        recovered = True
        print("发现已取得、尚未登记完成的 SOL 结果：直接恢复登记，"
              "不重复发起请求。", file=sys.stderr)
    else:
        try:
            if mode == "REVIEW":
                result = SR.run_review(
                    prompt, audit_log=audit_log,
                    max_output_tokens=args.max_output_tokens,
                    timeout=args.timeout)
            else:
                result = SR.run_advisory(
                    prompt, mode, audit_log=audit_log,
                    max_output_tokens=args.max_output_tokens,
                    timeout=args.timeout)
        except SR.SolError as exc:
            if mode == "REVIEW":
                G.record_sol_review_failure(
                    state_path, error_code=exc.code, detail=exc.detail,
                    materials_sha256=digest, model=SR.MODEL)
            else:
                G.record_sol_advisory_failure(
                    state_path, mode=mode, error_code=exc.code,
                    detail=exc.detail, materials_sha256=digest,
                    model=SR.MODEL)
            print(f"SOL {mode} 失败 [{exc.code}]：{exc.detail or ''}",
                  file=sys.stderr)
            print("已记录真实失败原因，未自动重试、未切换 Provider；"
                  "主代理可继续开发，或按需人工重新发起。", file=sys.stderr)
            return None, exc.exit_code
        # 远程已成功：先落一份可恢复结果，再写状态。状态/审计落盘失败时，
        # 下一次同材料调用直接恢复这份结果，不会再次付费。
        try:
            _write_atomic(
                _pending_result_path(task_dir, digest, audit_subdir),
                json.dumps(result, ensure_ascii=False, sort_keys=True))
        except CLIError as exc:
            print(f"警告：无法写入待登记结果（不影响本次登记）：{exc}",
                  file=sys.stderr)
    result["recovered_from_pending"] = recovered
    return result, 0


def _sol_channel_config(mode: str) -> dict:
    """REVIEW 与 CONSULT/PLAN 的通道差异（记录键、目录、锁与文案）。"""
    if mode == "REVIEW":
        return {
            "records_key": "sol_reviews",
            "subdir": "sol-reviews",
            "audit_name": "sol-review-audit.jsonl",
            "lock_name": ".sol-review.lock",
            "id_prefix": "SR",
            "label": "复核",
            "dry_run_schema": "copilot-sol-review-dry-run/v1",
            "closed_note": "任务已收口；新复核需先由用户授权 reopen",
        }
    return {
        "records_key": "sol_advisories",
        "subdir": "sol-advisories",
        "audit_name": "sol-advisory-audit.jsonl",
        "lock_name": ".sol-advisory.lock",
        "id_prefix": "SA",
        "label": "辅助分析",
        "dry_run_schema": "copilot-sol-advisory-dry-run/v1",
        "closed_note": "任务已收口；新的辅助分析需先由用户授权 reopen",
    }


def _sol_list(args, state: dict, mode: str, cfg: dict) -> int:
    records = (G.list_sol_reviews(state) if mode == "REVIEW"
               else G.list_sol_advisories(state))
    if args.json:
        _print({"task_id": state.get("task_id"), cfg["records_key"]: records})
    else:
        print(f"SOL {cfg['label']}记录：{len(records)} 条")
        for item in records:
            ident = item.get("review_id") or item.get("advisory_id")
            detail = (f"verdict={item.get('verdict')}"
                      if mode == "REVIEW"
                      else f"mode={item.get('mode')}")
            print(f"- {ident} [{item.get('status')}] {detail} "
                  f"{item.get('created_at') or ''}")
    return 0


def _sol_reuse_or_refuse(decision: dict, args, mode: str, cfg: dict) -> int | None:
    if decision["action"] == "refuse":
        raise CLIError(f"[{decision['code']}] {decision['note']}")
    if decision["action"] != "reuse":
        return None
    existing = decision["review"]
    if args.json:
        _print(existing)
    else:
        ident = existing.get("review_id") or existing.get("advisory_id")
        detail = (f"verdict={existing.get('verdict')}" if mode == "REVIEW"
                  else f"mode={existing.get('mode')}")
        print(f"SOL {cfg['label']}已完成：{ident} {detail}")
        print(decision["note"])
    return 0


def _run_sol_channel(args, mode: str) -> int:
    """SOL 三模式共用的一次调用路径；模式差异只在提示词与记录结构。"""
    path = _state_path(args)
    state = G.load_state(path)
    if G.is_legacy_state(state):
        raise CLIError("v1/v2 SOL 历史状态只读；请为新任务创建 v6+ 状态。")
    cfg = _sol_channel_config(mode)
    if args.list:
        return _sol_list(args, state, mode, cfg)
    if not args.materials:
        raise CLIError(
            ("sol-review" if mode == "REVIEW" else "sol-advisory")
            + " 需要 --materials（或 --list）。")
    materials = _read_text(args.materials)
    if not materials.strip():
        raise CLIError("材料为空。")
    question = str(getattr(args, "question", "") or "")

    def prepare(current: dict) -> tuple[str, str, str, dict]:
        if mode == "REVIEW":
            prompt_text = SR.build_materials(current, materials)
            subject = SR.review_subject_sha256(current, materials)
        else:
            prompt_text = SR.build_advisory_prompt(
                current, materials, mode=mode, question=question)
            subject = SR.review_subject_sha256(
                current, materials, mode=mode, question=question)
        if len(prompt_text) > SR.WARN_MATERIALS_CHARS:
            print(f"提示：材料 {len(prompt_text)} 字符，较大；"
                  f"上限 {SR.MAX_MATERIALS_CHARS}，超限会拒绝而不是静默截断。",
                  file=sys.stderr)
        context = SR.review_digest(prompt_text)
        decision = SR.single_round_decision(
            current, subject, force=bool(args.force),
            reason=args.reason, trigger=args.trigger,
            records_key=cfg["records_key"], mode=mode)
        return prompt_text, subject, context, decision

    if args.dry_run:
        prompt, digest, _context, decision = prepare(state)
        outcome = _sol_reuse_or_refuse(decision, args, mode, cfg)
        if outcome is not None:
            return outcome
        payload = SR.build_payload(
            prompt, mode=mode, max_output_tokens=args.max_output_tokens)
        info = {
            "schema": cfg["dry_run_schema"],
            "mode": mode,
            "model": SR.MODEL,
            "reasoning_effort": payload["reasoning"]["effort"],
            "endpoint": SR.resolve_base_url() + SR.ENDPOINT,
            "materials_sha256": digest,
            "materials_chars": len(prompt),
            "zero_tool": True,
            "payload_keys": sorted(payload),
            "tool_keys_present": [key for key in payload
                                  if key in SR.TOOL_KEYS],
            "unsupported_params_present": [
                key for key in payload if key in SR.UNSUPPORTED_PARAMS],
            "max_output_tokens": payload.get("max_output_tokens"),
        }
        if args.json:
            _print(info)
        else:
            print(f"SOL {mode} 预检：model={info['model']} "
                  f"effort={info['reasoning_effort']} "
                  f"materials={info['materials_chars']} 字符 sha={digest[:16]}…")
            print("零工具：请求体不含 tools/functions；tool_choice=none")
        return 0

    task_dir = os.path.dirname(os.path.abspath(path))
    # 跨进程互斥：同一任务同一通道只允许一个调用走“检查—调用—登记”临界区，
    # 避免并发进程在完成记录写入前各自发起一次付费请求。
    with G.task_lock(task_dir, cfg["lock_name"]):
        state = G.load_state(path)
        prompt, digest, context_sha, decision = prepare(state)
        outcome = _sol_reuse_or_refuse(decision, args, mode, cfg)
        if outcome is not None:
            return outcome
        if (state.get("closure") or {}).get("closed"):
            raise CLIError(cfg["closed_note"] + "（--list 仍可只读查看）。")
        audit_log = args.audit_log or os.path.join(
            task_dir, cfg["audit_name"])
        result, code = _invoke_sol_once(
            state_path=path, task_dir=task_dir, prompt=prompt,
            digest=digest, mode=mode, args=args, audit_log=audit_log,
            audit_subdir=cfg["subdir"])
        if result is None:
            return code
        reply_text = result.pop("reply_text", "")
        result["reply_sha256"] = result.get("reply_sha256") or ""
        output_dir = args.output_dir or os.path.join(task_dir, cfg["subdir"])
        record_id = cfg["id_prefix"] + secrets.token_hex(12)
        raw_path = ""
        raw_error = ""
        try:
            raw_path = _write_atomic(
                os.path.join(output_dir, f"{record_id}.raw.txt"),
                reply_text + "\n")
        except CLIError as exc:
            raw_error = str(exc)
            print(f"警告：原始输出写入失败（结构化结果仍登记）：{exc}",
                  file=sys.stderr)
        common = {
            "task_id": state.get("task_id") or "",
            "contract_sha256": state.get("contract_sha256") or "",
            "status": "completed",
            "mode": mode,
            "model": result["model"],
            "model_resolved": result.get("model_resolved"),
            "reasoning_effort": result.get("reasoning_effort"),
            "reasoning_mode": result.get("reasoning_mode"),
            "reasoning_resolved": result.get("reasoning_resolved"),
            "reasoning_verified": result.get("reasoning_verified"),
            # 只保留路由摘要：不回显 userinfo、查询参数或凭据。
            "endpoint": SR.resolve_base_url() + SR.ENDPOINT,
            "materials_sha256": result["materials_sha256"],
            "context_sha256": context_sha,
            "materials_chars": result["materials_chars"],
            "request_sha256": result.get("request_sha256"),
            "request_id": result.get("request_id") or "",
            "trace_id": result.get("trace_id") or "",
            "response_id": result.get("response_id") or "",
            "reply_sha256": result.get("reply_sha256"),
            "reply_chars": result.get("reply_chars"),
            "reply_path": raw_path,
            "raw_write_error": raw_error,
            "usage": result.get("usage"),
            "usage_available": result.get("usage_available"),
            "latency_ms": result.get("latency_ms"),
            "summary": result.get("summary"),
            "unverified": result.get("unverified") or [],
            "audit_written": result.get("audit_written", True),
            "audit_error": result.get("audit_error") or "",
            "recovered_from_pending": result.get("recovered_from_pending", False),
            "zero_tool": True,
            "attempts": 1,
            "completed_at": result.get("completed_at"),
        }
        if mode == "REVIEW":
            record = {
                **common,
                "schema": SR.REVIEW_SCHEMA,
                "review_id": record_id,
                "work_item_id": "",
                "verdict": result["verdict"],
                "findings": result.get("findings") or [],
                "findings_count": result.get("findings_count") or {},
                "materials_reviewed": result.get("materials_reviewed") or [],
                "verdict_adjusted": result.get("verdict_adjusted") or [],
            }
            record = G.record_sol_review(
                path, record, materials_sha256=digest,
                trigger=decision.get("trigger") or "",
                reason=decision.get("reason") or "")
        else:
            record = {
                **common,
                "schema": SR.ADVISORY_SCHEMA,
                "advisory_id": record_id,
                "analysis": result.get("analysis") or [],
                "options": result.get("options") or [],
                "steps": result.get("steps") or [],
                "risks": result.get("risks") or [],
                "alternatives": result.get("alternatives") or [],
                "recommendation": result.get("recommendation") or "",
            }
            record = G.record_sol_advisory(
                path, record, materials_sha256=digest,
                trigger=decision.get("trigger") or "",
                reason=decision.get("reason") or "")
        try:
            os.unlink(_pending_result_path(task_dir, digest, cfg["subdir"]))
        except OSError:
            pass
        if args.json:
            _print(record)
        elif mode == "REVIEW":
            counts = record.get("findings_count") or {}
            print(f"SOL 复核完成：{record_id} verdict={record['verdict']}")
            print("findings：" + "，".join(
                f"{key}={counts.get(key, 0)}"
                for key in ("blocking", "major", "minor", "info")))
            if record.get("unverified"):
                print(f"未验证事项：{len(record['unverified'])} 条（见结果 JSON）")
            print(f"原始输出：{raw_path or '(写入失败，见结构化结果)'}")
            if not record.get("audit_written", True):
                print("警告：审计日志写入失败；复核结果已登记，"
                      f"错误 {record.get('audit_error') or '-'}。", file=sys.stderr)
            print("说明：SOL 只提供建议；主代理核实、修复并用定向测试验证，"
                  "不自动发起第二轮复核。")
        else:
            print(f"SOL {mode} 完成：{record_id}")
            print(f"摘要：{record.get('summary') or '(无)'}")
            print("产出：" + "，".join([
                f"analysis={len(record.get('analysis') or [])}",
                f"options={len(record.get('options') or [])}",
                f"steps={len(record.get('steps') or [])}",
                f"risks={len(record.get('risks') or [])}"]))
            if record.get("unverified"):
                print(f"未验证事项：{len(record['unverified'])} 条（见结果 JSON）")
            print(f"原始输出：{raw_path or '(写入失败，见结构化结果)'}")
            if not record.get("audit_written", True):
                print("警告：审计日志写入失败；辅助分析结果已登记，"
                      f"错误 {record.get('audit_error') or '-'}。", file=sys.stderr)
            print("说明：SOL 只提供建议；是否采纳、如何执行由主代理决定。")
        return 0


def cmd_sol_review(args) -> int:
    """SOL 零工具单轮复核：材料 → 一次请求 → 结构化结果 → 建议性 findings。"""
    return _run_sol_channel(args, "REVIEW")


def cmd_sol_advisory(args) -> int:
    """SOL 零工具单轮辅助分析（CONSULT / PLAN）：建议性认知辅助，不入门禁。"""
    mode = SR.normalize_mode(getattr(args, "mode", "CONSULT"))
    if mode not in SR.ADVISORY_MODES:
        raise CLIError("sol-advisory 只接受 CONSULT 或 PLAN；REVIEW 用 sol-review。")
    return _run_sol_channel(args, mode)


def cmd_agent_policy(args) -> int:
    """只读调度建议：主代理默认执行，Explore/general-purpose 按需。

    这是决策支持，不是调度器：不创建任务、不派发子代理、不设调用配额。
    """
    decision = AP.dispatch_advice(
        needs_independent_context=bool(args.needs_independent_context),
        parallelizable=bool(args.parallel),
        independent_deliverable=bool(args.independent_deliverable),
        write_conflict=bool(args.write_conflict),
        saves_time=bool(args.saves_time),
        research_only=bool(args.research_only),
        subagent_unavailable=bool(args.subagent_unavailable),
    )
    decision["schema"] = "copilot-agent-dispatch-advice/v1"
    decision["note"] = (
        "建议而非门禁；主代理可按现场证据否决，但不得为“体现协作”强制调用子代理。"
        "内置 Explore 不注入 AGENTS.md，派发时必须提供项目背景与范围；"
        "general-purpose 拥有完整工具与 MCP，不能当作只读复核者。")
    if args.json:
        _print(decision)
    else:
        print(f"建议执行方：{decision['executor']}")
        print(f"理由：{decision['reason']}")
        for item in decision.get("prompt_requirements") or []:
            print(f"- {item}")
        print("说明：" + decision["note"])
    return 0


def cmd_closure(args) -> int:
    path = _state_path(args)
    if not args.close:
        return cmd_closure_check(args)
    closure = G.close_task(path, by=args.by)
    quality = closure.get("quality") or {}
    print(f"任务已收口：verdict={closure['verdict']} "
          f"iteration={closure['iteration_id']} by={closure['closed_by']}")
    if quality.get("verdict") == "pass_with_warnings":
        print(f"质量结论：{str(quality.get('raw_verdict') or 'PASS_WITH_WARNINGS').upper()}"
              "（原始结论保留，不改写为 PASS；已接受 "
              f"{'、'.join(quality.get('accepted_ids') or []) or '无'}）")
    return 0


def cmd_reopen(args) -> int:
    G.reopen_task(_state_path(args), args.reason,
                  authorized_by=args.authorized_by)
    print("任务已重开；历史收口记录保留在 closure.reopen_history。")
    return 0


# ── 交付清单（只读） ──────────────────────────────────────────────────────
_HANDOFF_ARTIFACT_EXTS = frozenset({
    ".apk", ".aab", ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".log", ".txt", ".mp4", ".webm", ".har", ".trace", ".hprof",
})


def _git_read(cwd: str, *args: str) -> str | None:
    """只读执行 git 查询；禁用可选锁，失败返回 None（不抛异常、不写工作区）。

    `errors="replace"`：`git status -z` 原样输出路径字节，非 UTF-8 文件名在严格
    解码下会抛 UnicodeDecodeError 并让整个清单崩溃；替换字符只影响展示。
    """
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    try:
        proc = subprocess.run(["git", "--no-optional-locks", *args], cwd=cwd,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env)
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _parse_git_status(raw: str) -> list[dict]:
    """解析 `git status --porcelain=v1 -z`；重命名/复制记录的第二段是原路径。"""
    entries: list[dict] = []
    tokens = (raw or "").split("\0")
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        idx += 1
        if len(token) < 4:
            continue
        code, path = token[:2], token[3:]
        entry = {"code": code, "path": path}
        if code[0] in "RC" or code[1] in "RC":
            if idx < len(tokens) and tokens[idx]:
                entry["from"] = tokens[idx]
                idx += 1
        entries.append(entry)
    return entries


def _handoff_bucket(path: str, *, evidence: set[str], task_root: str) -> str:
    """把未跟踪文件归入可解释的类别；Copilot 只分类，不删除、不 ignore。"""
    if task_root and (path == task_root or path.startswith(task_root + "/")):
        return "治理记录（任务状态/证据；通常由忽略规则保留）"
    if path in evidence:
        return "任务证据（已登记；按证据链保留）"
    ext = os.path.splitext(path)[1].lower()
    if ext in _HANDOFF_ARTIFACT_EXTS:
        return "验收产物（截图/日志/安装包；确认是否交付或保留）"
    return "未分类（需 ZCode / 用户判断）"


def _resolve_handoff_cwd(state_dir: str) -> str:
    """优先使用任务所在仓库根目录；否则回落到当前目录。

    从任务目录本身开始向上探测，并用 `git rev-parse --show-toplevel` 判断——
    直接检查 `.git` 目录会漏掉 worktree/submodule（`.git` 是文件）以及状态文件
    直接放在仓库根的情况。
    """
    if state_dir:
        candidate = state_dir
        for _ in range(3):
            if not candidate:
                break
            top = (_git_read(candidate, "rev-parse", "--show-toplevel") or "").strip()
            if top:
                return top
            parent = os.path.dirname(candidate)
            if parent == candidate:
                break
            candidate = parent
    return os.getcwd()


def cmd_handoff(args) -> int:
    state = None
    state_path = os.path.abspath(args.state) if args.state else ""
    if state_path:
        state = G.load_state(state_path)
    task_dir = os.path.dirname(state_path) if state_path else ""
    cwd = os.path.abspath(args.cwd) if args.cwd else _resolve_handoff_cwd(task_dir)

    top = (_git_read(cwd, "rev-parse", "--show-toplevel") or "").strip()
    git_ok = bool(top)
    branch = head = upstream = divergence = ""
    entries: list[dict] = []
    if git_ok:
        branch = (_git_read(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
        head = (_git_read(cwd, "log", "-1", "--format=%h %s") or "").strip()
        upstream = (_git_read(
            cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
            "@{upstream}") or "").strip()
        divergence = (_git_read(
            cwd, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
            or "").strip()
        entries = _parse_git_status(
            _git_read(cwd, "status", "--porcelain=v1", "-z",
                      "--untracked-files=all") or "")

    staged, unstaged, untracked = [], [], []
    for entry in entries:
        code, path = entry["code"], entry["path"]
        if code == "??":
            untracked.append(entry)
            continue
        if code[0] != " ":
            staged.append(entry)
        if code[1] != " ":
            unstaged.append(entry)

    task_root = ""
    if task_dir and git_ok:
        relative = os.path.relpath(task_dir, top)
        if not relative.startswith(".."):
            task_root = relative.replace(os.sep, "/")
    evidence_paths: set[str] = set()
    evidence_warnings: list[str] = []
    missing_evidence: list[str] = []
    task_summary: dict = {}
    if isinstance(state, dict):
        for item in state.get("evidence") or []:
            hint = str(item.get("path_hint") or "")
            if not hint or hint == "<inline>":
                continue
            if git_ok:
                relative = os.path.relpath(hint, top)
                if not relative.startswith(".."):
                    evidence_paths.add(relative.replace(os.sep, "/"))
            # 已归档证据的文件被清理是正常收尾，不再报“缺失（需处理）”。
            if not item.get("dropped") and not os.path.exists(hint):
                missing_evidence.append(f"{item.get('id')}：{hint}")
        evidence_warnings = G.evidence_warnings(state)
        closed = bool((state.get("closure") or {}).get("closed"))
        task_summary = {
            "task_id": state.get("task_id"),
            "goal": str(state.get("goal") or "")[:200],
            "phase": "CLOSED" if closed else G.evaluate(state).get("phase"),
            "risk": state.get("risk"),
            "closed_by": (state.get("closure") or {}).get("closed_by"),
            "evidence_live": len([e for e in state.get("evidence") or []
                                  if not e.get("dropped")]),
            "evidence_dropped": len([e for e in state.get("evidence") or []
                                     if e.get("dropped")]),
        }

    buckets: dict[str, list[str]] = {}
    for entry in untracked:
        bucket = _handoff_bucket(entry["path"], evidence=evidence_paths,
                                 task_root=task_root)
        buckets.setdefault(bucket, []).append(entry["path"])

    behind = ahead = None
    if divergence:
        parts = divergence.split()
        if len(parts) == 2:
            behind, ahead = parts[0], parts[1]

    report = {
        "cwd": cwd,
        "repo": top.strip(),
        "git_available": git_ok,
        "branch": branch,
        "head": head,
        "upstream": upstream,
        "behind": behind,
        "ahead": ahead,
        "staged": [{"code": e["code"], "path": e["path"],
                    **({"from": e["from"]} if e.get("from") else {})}
                   for e in staged],
        "unstaged": [{"code": e["code"], "path": e["path"],
                      **({"from": e["from"]} if e.get("from") else {})}
                     for e in unstaged],
        "untracked": [{"path": e["path"],
                       "bucket": _handoff_bucket(e["path"], evidence=evidence_paths,
                                                 task_root=task_root)}
                      for e in untracked],
        "untracked_buckets": {name: sorted(paths) for name, paths in buckets.items()},
        "task": task_summary,
        "evidence_warnings": evidence_warnings,
        "missing_evidence_files": missing_evidence,
        "note": ("只读清单：Copilot 不提交、不删除、不改写 .gitignore；"
                 "已提交内容不在清单里，未列出的忽略项也不受影响。"),
    }

    if args.json:
        _print(report)
        return 0

    if git_ok:
        sync = ""
        if behind is not None and ahead is not None:
            sync = f"，落后上游 {behind} / 领先 {ahead}"
        print(f"## 工作区交付清单（只读）\n")
        print(f"- 仓库：{report['repo']}")
        print(f"- 分支：{branch}（HEAD {head}）{sync}")
        if upstream:
            print(f"- 上游：{upstream}")
        total = len(staged) + len(unstaged) + len(untracked)
        print(f"- 变更：已暂存 {len(staged)} / 未暂存 {len(unstaged)} / "
              f"未跟踪 {len(untracked)}（共 {total}）")
        for title, items in (("已暂存（下次提交会包含）", staged),
                             ("已修改未暂存", unstaged)):
            print(f"\n### {title}")
            if not items:
                print("- （无）")
            for entry in items:
                origin = f" ← {entry['from']}" if entry.get("from") else ""
                print(f"- `{entry['code']}` {entry['path']}{origin}")
        print("\n### 未跟踪文件（按类别）")
        if not buckets:
            print("- （无）")
        for name in sorted(buckets):
            print(f"- {name}：{len(buckets[name])} 项")
            for path in sorted(buckets[name])[:args.limit]:
                print(f"    - {path}")
            if len(buckets[name]) > args.limit:
                print(f"    - …（其余 {len(buckets[name]) - args.limit} 项省略）")
    else:
        print(f"## 工作区交付清单（只读）\n\n- 目录 {cwd} 不是可用 git 仓库，"
              "仅列出任务状态。")
    if task_summary:
        print(f"\n### 任务状态")
        print(f"- {task_summary['task_id']}：{task_summary['phase']} "
              f"（风险 {task_summary['risk']}，收口人 {task_summary['closed_by'] or '-'}）")
        print(f"- 目标：{task_summary['goal']}")
        print(f"- 证据：有效 {task_summary['evidence_live']} / "
              f"已归档 {task_summary['evidence_dropped']}")
    if missing_evidence:
        print("\n### 证据文件缺失（需处理）")
        for item in missing_evidence[:args.limit]:
            print(f"- {item}")
    if evidence_warnings:
        print("\n### 证据提醒（不阻断）")
        for item in evidence_warnings[:args.limit]:
            print(f"- {item}")
    print(f"\n{report['note']}")
    return 0

def cmd_contract(args) -> int:
    path = _state_path(args)
    state = G.load_state(path)
    if args.show:
        contract = state.get("contract") or {}
        print(f"合同指纹：{state.get('contract_sha256')}")
        print(f"条件状态：{contract.get('criteria_state')}")
        if state.get("workspace"):
            print(f"任务工作区：{state['workspace']}（{state.get('workspace_source') or 'unknown'}）")
        print("完成条件：")
        for criterion in contract.get("criteria") or []:
            refs = ", ".join(r.get("id") for r in criterion.get("evidence_refs") or [])
            print(f"- {criterion['criterion_id']} [{criterion['status']}] "
                  f"{criterion['description']}"
                  + (f" ← {refs}" if refs else ""))
        if contract.get("boundaries"):
            print("边界：")
            print(contract["boundaries"])
        return 0
    if not args.amend:
        raise CLIError("请指定 --show 或 --amend。")
    contract_text = (_read_text(args.contract_file)
                     if args.contract_file else "")
    criteria = _load_criteria(args.criteria_file) if args.criteria_file else None
    result = G.amend_contract(path, contract=contract_text, criteria=criteria,
                              contract_source=args.contract_file or "",
                              test_policy=_load_test_policy(args))
    print(f"合同已修订：criteria_sha256={result['criteria_sha256'][:16]}…")
    for criterion in result["criteria"]:
        print(f"- {criterion['criterion_id']} [{criterion['status']}] "
              f"{criterion['description']}")
    return 0


def cmd_authorize_review(args) -> int:
    entry = G.authorize_formal_review(
        _state_path(args), authorized_by=args.authorized_by,
        reason=args.reason, scope=args.scope or "")
    print(f"已记录用户对正式 REVIEW 的授权：{entry['reason'][:120]}")
    print("授权只登记用户要求；正式 REVIEW 不是普通开发的强制门禁。")
    return 0


def cmd_formal_review(args) -> int:
    entry = G.record_formal_review(
        _state_path(args), verdict=args.verdict or "", summary=args.summary or "",
        review_ref=args.review_ref or "", reviewer=args.reviewer,
        amend=args.amend or "", supersedes=args.supersedes or "",
        reason=args.reason or "")
    if "amendment" in entry:
        amendment = entry["amendment"]
        print(f"正式 REVIEW amendment 已登记：{entry['review_id']} → "
              f"{amendment['amendment_id']}；原 {entry['verdict']} 结论未覆盖。")
        print(f"原因：{amendment['reason'][:160]}")
    elif entry.get("supersedes"):
        print(f"正式 REVIEW 新结论已登记：{entry['review_id']} → {entry['verdict']}")
        print(f"已关联 supersedes={entry['supersedes']}；旧记录原样保留。")
    else:
        print(f"正式 REVIEW 已登记：{entry['review_id']} → {entry['verdict']}")
    return 0


def cmd_legacy_status(args) -> int:
    if not (args.state or args.audit_log or args.ledger):
        raise CLIError("至少提供 --state / --audit-log / --ledger 之一。")
    data = G.legacy_status(args.state or "", args.audit_log or "",
                           args.ledger or "")
    if args.json:
        _print(data)
        return 0
    print("历史 SOL 通道（只读；v6 不再调用）")
    if data["state"]:
        item = data["state"]
        print(f"状态：{item['path']}")
        print(f"任务：{item.get('task_id')} · v{item.get('version')} · "
              f"backend={item.get('execution_backend')}")
        print(f"轮次：{len(item['rounds'])}；证据：{item['evidence_count']}；"
              f"工作项：{item['action_count']}")
        for round_ in item["rounds"]:
            print(f"- {round_.get('round_id')} verdict={round_.get('verdict')} "
                  f"call={round_.get('call_id')}")
        print(f"说明：{item['note']}")
    if data["audit"]:
        item = data["audit"]
        print(f"审计：{item['path']} requests={item['requests']} "
              f"results={item['results']} models={item['models']}")
        print(f"说明：{item['note']}")
    if data["ledger"]:
        item = data["ledger"]
        print(f"REVIEW 台账：{item['path']} rounds={len(item['rounds'])}")
        for round_ in item["rounds"]:
            print(f"- {round_.get('round_id')} verdict={round_.get('verdict')}")
        print(f"说明：{item['note']}")
    return 0


# ── CLI ────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="copilot_dispatch.py",
        description="Copilot 本地任务治理（不调用任何模型）")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="创建任务合同与状态")
    p.add_argument("--state", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--contract-file", default="", help="合同 Markdown；`## 完成条件` 列表")
    p.add_argument("--criteria-file", default="", help="条件 JSON 数组或逐行文本")
    p.add_argument("--test-policy", default="",
                   help="合同测试策略 JSON（required/commands/report/artifacts/require_*）")
    p.add_argument("--test-policy-file", default="",
                   help="合同测试策略 JSON 文件")
    p.add_argument("--risk", default="medium", choices=G.RISKS,
                   help="low/medium 默认不派 Reviewer；high 建议一次 SOL 单轮复核"
                        "（advisory，不阻塞收口）")
    p.add_argument("--backend", default="legacy", choices=G.BACKENDS)
    p.add_argument("--task-id", default="")
    p.add_argument("--mode", default="")
    p.add_argument("--note", default="")
    p.add_argument("--workspace", default="",
                   help="任务实际 git 工作区；省略时取状态文件所在目录的 git 根")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("plan", help="登记 ZCode 本地计划（JSON）")
    p.add_argument("--state", required=True)
    p.add_argument("--plan", default="", help="计划 JSON 路径；默认读 stdin")
    p.add_argument("--source", default="zcode")
    p.set_defaults(func=cmd_plan)

    for name in ("next", "brief", "render", "status"):
        p = sub.add_parser(name, help="查看阶段、工作项、G1–G3 与下一步")
        p.add_argument("--state", required=True)
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=cmd_next)

    p = sub.add_parser("record", help="回填工作项状态与证据")
    p.add_argument("--state", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--status", required=True, choices=G.STATUSES)
    p.add_argument("--result", default="")
    p.add_argument("--evidence", action="append", default=[])
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("rerun", help="把已终结工作项重开为 pending（修复后重新验证）")
    p.add_argument("--state", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_rerun)

    p = sub.add_parser("criterion", help="核销完成条件")
    p.add_argument("--state", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--status", required=True, choices=G.CRITERION_STATUSES)
    p.add_argument("--evidence", action="append", default=[])
    p.set_defaults(func=cmd_criterion)

    p = sub.add_parser("judgment", help="记录 ZCode 自我判断（仅审计）")
    p.add_argument("--state", required=True)
    p.add_argument("--status", required=True, choices=G.VERDICTS)
    p.add_argument("--summary", required=True)
    p.add_argument("--evidence", action="append", default=[])
    p.set_defaults(func=cmd_judgment)

    p = sub.add_parser("evidence", help="登记 / 归档 / 列出证据")
    p.add_argument("--state", required=True)
    p.add_argument("--add", action="store_true")
    p.add_argument("--drop", action="store_true")
    p.add_argument("--list", action="store_true")
    p.add_argument("--next-id", action="store_true")
    p.add_argument("--id", default="")
    p.add_argument("--file", default="")
    p.add_argument("--text", default=None)
    p.add_argument("--capture-cmd", default="",
                   help="经 bridge_evidence 采集命令输出（脱敏/截断/清单）")
    p.add_argument("--kind", default="")
    p.add_argument("--note", default="")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser("report", help="登记执行报告或接入 verify 质量报告")
    p.add_argument("--state", required=True)
    p.add_argument("--summary", default="",
                   help="执行报告摘要；单独使用或与 --verify-report 同用")
    p.add_argument("--verify-report", default="",
                   help="verify 结构化质量报告文件；PWW 自动登记用户接受条件")
    p.add_argument("--workspace", default="",
                   help="代码类质量报告的实际工作区；纯文档报告可省略")
    p.add_argument("--blocked", action="store_true")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("accept", help="登记用户对 PWW 的明确接受")
    p.add_argument("--state", required=True)
    p.add_argument("--report", default="", help="质量报告 report_id；仅一份时可省略")
    p.add_argument("--accept", action="append", default=[],
                   help="用户接受的 W/R id，逗号分隔或重复传入")
    p.add_argument("--by", required=True,
                   help="必须是 user；执行方/子代理/verify 不得代为接受")
    p.add_argument("--quote", required=True,
                   help="用户接受的原话（原样记录，不能只写调用方结论）")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_accept)

    p = sub.add_parser("quality", help="查看质量结论与 PWW 接受状态")
    p.add_argument("--state", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_quality)

    p = sub.add_parser("decide", help="记录用户/任务决策")
    p.add_argument("--state", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--source", default="user")
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("block", help="记录真实阻塞（缺少授权/决策/资源/外部限制）")
    p.add_argument("--state", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--needs", required=True)
    p.set_defaults(func=cmd_block)

    p = sub.add_parser("unblock", help="解除阻塞")
    p.add_argument("--state", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--block-id", default="",
                   help="存在多条未解除阻塞时必填，只解除指定 block")
    p.set_defaults(func=cmd_unblock)

    p = sub.add_parser("closure-check", help="检查 G1–G3")
    p.add_argument("--state", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_closure_check)

    p = sub.add_parser("review-metrics",
                       help="统计真实复核轮次/重复 finding/无效信封等效率指标")
    p.add_argument("--state", required=True)
    p.add_argument("--json", action="store_true", help="输出 JSON（默认即为 JSON）")
    p.set_defaults(func=cmd_review_metrics)

    p = sub.add_parser("review-admission",
                       help="SOL 复核准入建议（只读；不是门禁、不写状态）")
    p.add_argument("--state", required=True)
    p.add_argument("--trigger", action="append", default=[],
                   help="实质风险触发因素（如 security/core-logic/data-integrity）")
    p.add_argument("--reason", default="", help="准入原因")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_review_admission)

    p = sub.add_parser(
        "sol-review",
        help="SOL 零工具单轮复核：一次调用、不自动重试、不自动二审")
    p.add_argument("--state", required=True)
    p.add_argument("--materials", default="",
                   help="主代理整理的审查材料文件（- 表示 stdin）；--list 时可省略")
    p.add_argument("--reason", default="", help="再次复核的具体原因")
    p.add_argument("--trigger", default="",
                   choices=("", *sorted(SR.RETRY_TRIGGERS)),
                   help="再次复核的实质原因类型")
    p.add_argument("--force", action="store_true",
                   help="已有结论且材料不同时，允许按 --trigger/--reason 再调用一次")
    p.add_argument("--dry-run", action="store_true",
                   help="只构建并校验请求（零工具/材料大小），不发请求、不写状态")
    p.add_argument("--list", action="store_true", help="只读列出生效的 SOL 复核记录")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeout", type=float, default=SR.DEFAULT_TIMEOUT,
                   help="单次请求超时秒数（不重试）")
    p.add_argument("--max-output-tokens", type=int,
                   default=SR.DEFAULT_MAX_OUTPUT_TOKENS)
    p.add_argument("--audit-log", default="",
                   help="审计日志路径；默认任务目录 sol-review-audit.jsonl")
    p.add_argument("--output-dir", default="",
                   help="原始输出目录；默认任务目录 sol-reviews/")
    p.set_defaults(func=cmd_sol_review)

    p = sub.add_parser(
        "sol-advisory",
        help="SOL 零工具单轮辅助分析（CONSULT / PLAN）：建议性，不入门禁")
    p.add_argument("--state", required=True)
    p.add_argument("--mode", default="CONSULT",
                   choices=SR.ADVISORY_MODES,
                   help="CONSULT=问题分析/方案比较/风险识别；PLAN=规划与依赖建议")
    p.add_argument("--materials", default="",
                   help="主代理整理的材料文件（- 表示 stdin）；--list 时可省略")
    p.add_argument("--question", default="",
                   help="主代理要 SOL 回答的具体问题（可选）")
    p.add_argument("--reason", default="", help="再次调用的具体原因")
    p.add_argument("--trigger", default="",
                   choices=("", *sorted(SR.RETRY_TRIGGERS)),
                   help="再次调用的实质原因类型")
    p.add_argument("--force", action="store_true",
                   help="已有同模式结论且材料不同时，允许按 --trigger/--reason 再调用一次")
    p.add_argument("--dry-run", action="store_true",
                   help="只构建并校验请求（零工具/模型/推理强度），不发请求、不写状态")
    p.add_argument("--list", action="store_true", help="只读列出辅助分析记录")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeout", type=float, default=SR.DEFAULT_TIMEOUT,
                   help="单次请求超时秒数（不重试）")
    p.add_argument("--max-output-tokens", type=int,
                   default=SR.DEFAULT_MAX_OUTPUT_TOKENS)
    p.add_argument("--audit-log", default="",
                   help="审计日志路径；默认任务目录 sol-advisory-audit.jsonl")
    p.add_argument("--output-dir", default="",
                   help="原始输出目录；默认任务目录 sol-advisories/")
    p.set_defaults(func=cmd_sol_advisory)

    p = sub.add_parser("agent-policy",
                       help="只读调度建议：主代理默认，Explore/general-purpose 按需")
    p.add_argument("--needs-independent-context", action="store_true",
                   help="调查需要独立上下文（跨模块/调用链/独立复核）")
    p.add_argument("--parallel", action="store_true",
                   help="存在可真正并行的独立工作")
    p.add_argument("--independent-deliverable", action="store_true",
                   help="有边界清晰、可独立验收的交付物")
    p.add_argument("--write-conflict", action="store_true",
                   help="工作范围与主代理正在修改的文件冲突；冲突时建议主代理")
    p.add_argument("--saves-time", action="store_true",
                   help="并行收益明显高于上下文与集成成本（覆盖第 4、5 问）")
    p.add_argument("--research-only", action="store_true",
                   help="只读调查，不修改工程文件；只建议 Explore，不交给全工具代理")
    p.add_argument("--subagent-unavailable", action="store_true",
                   help="Profile/类型不可用或权限不满足；建议主代理继续")
    p.add_argument("--plan-mode", action="store_true",
                   help="父会话处于 plan 模式（planEnabled）：宿主会给子代理注入 Bash")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_agent_policy)

    p = sub.add_parser("finding",
                       help="查看/更新 Finding 台账（修复与定向验证，不触发复核）")
    p.add_argument("--state", required=True)
    p.add_argument("--list", action="store_true")
    p.add_argument("--id", default="")
    p.add_argument("--status", choices=RP.FINDING_STATUSES)
    p.add_argument("--evidence", action="append", default=[],
                   help="修复/定向验证证据 id（fixed/verified/regressed 必填）")
    p.add_argument("--note", default="", help="状态变更说明（regressed/dropped/open 必填）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_finding)

    p = sub.add_parser("closure", help="收口任务（必须 G1–G3 通过）")
    p.add_argument("--state", required=True)
    p.add_argument("--close", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--by", default="zcode-native")
    p.set_defaults(func=cmd_closure)

    p = sub.add_parser("reopen", help="用户授权后重开已收口任务")
    p.add_argument("--state", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--authorized-by", required=True,
                   help="必须显式写 user：重开需要用户授权")
    p.set_defaults(func=cmd_reopen)

    p = sub.add_parser("handoff", help="只读列出生效交付清单与未提交/未跟踪文件")
    p.add_argument("--state", default="", help="任务状态文件（可选）")
    p.add_argument("--cwd", default="", help="仓库目录（默认取任务所在仓库）")
    p.add_argument("--limit", type=int, default=20, help="每类最多列出的条目数")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_handoff)

    p = sub.add_parser("contract", help="查看或修订合同与完成条件")
    p.add_argument("--state", required=True)
    p.add_argument("--show", action="store_true")
    p.add_argument("--amend", action="store_true")
    p.add_argument("--contract-file", default="")
    p.add_argument("--criteria-file", default="")
    p.add_argument("--test-policy", default="",
                   help="修订合同测试策略 JSON（省略则保留原策略）")
    p.add_argument("--test-policy-file", default="")
    p.set_defaults(func=cmd_contract)

    p = sub.add_parser("authorize-review", help="记录用户对正式 REVIEW 的明确授权")
    p.add_argument("--state", required=True)
    p.add_argument("--authorized-by", required=True,
                   help="必须显式写 user：该字段是调用方对用户要求的声明，不是凭证")
    p.add_argument("--reason", required=True)
    p.add_argument("--scope", default="")
    p.set_defaults(func=cmd_authorize_review)

    p = sub.add_parser("formal-review",
                       help="登记正式 REVIEW 结论，或对既有结论追加 amendment")
    p.add_argument("--state", required=True)
    p.add_argument("--verdict", choices=G.VERDICTS,
                   help="新结论必填；--amend 时继承原结论，不要传")
    p.add_argument("--summary", default="",
                   help="新结论必填；amendment 可只改引用或写修订说明")
    p.add_argument("--review-ref", default="")
    p.add_argument("--reviewer", default="native")
    p.add_argument("--amend", default="",
                   help="对既有 review_id 追加非实质修订（目标摘要必须未变）")
    p.add_argument("--supersedes", default="",
                   help="实质变化后登记新结论，并关联旧 review_id")
    p.add_argument("--reason", default="",
                   help="amendment / supersede 必填，说明修订或失效原因")
    p.set_defaults(func=cmd_formal_review)

    p = sub.add_parser("legacy-status", help="只读查看 v1/v2 SOL 历史状态与台账")
    p.add_argument("--state", default="")
    p.add_argument("--audit-log", default="")
    p.add_argument("--ledger", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_legacy_status)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except (G.GovernanceError, CLIError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except SR.SolError as exc:
        # SOL 前置校验（材料大小、本机路由、凭据）失败也必须显式退出，
        # 不能把 traceback 或未脱敏详情抛给调用方。
        print(f"ERROR: [{exc.code}] {exc.detail}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())

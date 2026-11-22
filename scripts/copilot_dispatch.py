#!/usr/bin/env python3
"""Copilot 任务治理 CLI（ZCode Autonomous Development Edition）。

Copilot 做轻量协作治理：任务合同、工作项、证据、审计、本地 G1–G3，以及 SOL
生命周期调度（A 启动 / B 规划 / C 事件 / D 里程碑 / E 收口）。
`sol-consult` / `sol-plan` / `sol-review` 是显式模型调用入口；其余生命周期命令
（`start` / `plan` / `record` / `criterion` / `closure` 等）和 `drive` 在默认
`--sol auto` 下会顺带执行**至多 1 条** recommended 调度义务（1 次请求，失败只
记录、不阻断），因此“不调用模型”只对 `--sol off` / `advise`、读命令与测试进程
成立。SOL 的结论只是认知输入，工程执行与技术决策始终由 ZCode 主代理完成。

常用命令::

    copilot_dispatch.py start --state S --goal G --contract-file C.md
    copilot_dispatch.py plan --state S --plan P.json
    copilot_dispatch.py next --state S
    copilot_dispatch.py evidence --state S --add --id E1 --text "..."
    copilot_dispatch.py record --state S --id W1 --status done --result "..." --evidence E1
    copilot_dispatch.py criterion --state S --id C1 --status met --evidence E1
    copilot_dispatch.py sol-schedule --state S
    copilot_dispatch.py sol-consult --state S --materials M.md --question "..."
    copilot_dispatch.py sol-plan --state S --materials M.md --question "..."
    copilot_dispatch.py sol-review --state S --materials M.md   # 仅用户明确要求
    copilot_dispatch.py sol-accept --state S --call-id SC... --status accepted
    copilot_dispatch.py sol-stats --state S
    copilot_dispatch.py closure-check --state S
    copilot_dispatch.py closure --state S --close
    copilot_dispatch.py legacy-status --state old.json --audit-log old.jsonl --ledger ledger.json

历史 v1/v2 SOL 状态与审计只能通过 `legacy-status` 只读查看，v6+ 不会驱动它们；
`sol-think` 是 v8.4–v8.5 的只读别名，读取时归一为 CONSULT。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shlex
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import governance as G  # noqa: E402
import agent_policy as AP  # noqa: E402
import review_policy as RP  # noqa: E402
import sol_review as SR  # noqa: E402
import sol_schedule as SS  # noqa: E402

__version__ = "9.6.2"


class CLIError(Exception):
    pass


def _knowledge_module():
    """知识交接是本地可选扩展：仓库不跟踪，只在用到 `--knowledge-file` 时才需要。"""
    try:
        import knowledge_handoff as module
    except ImportError:
        return None
    return module


def _read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise CLIError(f"无法读取 {path}：{exc}") from None


def _compose_materials(materials: str, knowledge_file: str) -> tuple[str, dict]:
    """把已校验的知识交接块追加到 SOL materials；路径不能替代正文。"""
    if not knowledge_file:
        return materials, {}
    module = _knowledge_module()
    if module is None:
        raise CLIError("[KNOWLEDGE_EXTENSION_MISSING] 知识交接扩展未安装（"
                       "缺少 scripts/knowledge_handoff.py）；去掉 --knowledge-file，"
                       "或恢复该文件后重试。")
    try:
        block = module.render_file(knowledge_file, target="sol")
    except module.HandoffError as exc:
        raise CLIError(f"[{exc.code}] {exc.message}") from None
    if any(line.strip() == "<!-- copilot-knowledge-handoff -->"
           for line in materials.splitlines()):
        raise CLIError("[KNOWLEDGE_BLOCK_DUPLICATE] materials 已包含知识交接块，"
                       "不要再传 --knowledge-file。")
    composed = materials.rstrip() + "\n\n" + block
    return composed, {
        "knowledge_file": os.path.abspath(knowledge_file),
        "knowledge_block_sha256": module.block_sha256(block),
        "knowledge_entries": sum(1 for line in block.splitlines()
                                 if line.startswith("### ")),
    }


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


EXIT_CONTINUE = 10

# 只读命令不写任何文件（含 session.json 会话绑定）：它们的契约是“只看不改”。
READ_ONLY_COMMANDS = frozenset({
    "closure-check", "review-metrics", "review-admission", "agent-policy",
    "handoff", "legacy-status", "next", "brief", "render", "status", "quality",
    "sol-stats",
})


def _is_read_only(args) -> bool:
    """按实际动作判定只读：查看旧任务不能被当成本会话“负责”这个任务。

    `evidence --list/--next-id` 与 `contract --show` 是纯读取；`--add/--drop/--amend`
    才写状态。按命令名一刀切会让查看动作留下会话绑定，进而触发 Stop 误拦。
    """
    command = str(getattr(args, "command", "") or "")
    if command in READ_ONLY_COMMANDS:
        return True
    if command == "evidence":
        return not (getattr(args, "add", None) or getattr(args, "drop", None))
    if command == "contract":
        return not getattr(args, "amend", None)
    if command == "sol-schedule":
        # 只读查看调度；--execute 会发起一次真实调用，属于写动作。
        return not getattr(args, "execute", False)
    return False


# ── SOL 生命周期调度（v9.6）───────────────────────────────────────────────
# 调度不是新的调用通道：义务只是「应调用」的持久化事实，执行仍然走
# `_run_sol_channel`（同一 sol_review 通道、同一 sol_calls 台账、同一把锁）。
# 三条硬边界：
#   1. 每条命令最多执行 1 个义务、最多 1 次请求（有界）；
#   2. 只自动执行 recommended 级；optional 级只提示；
#   3. 读命令永不执行；SOL 失败只记录，不改变命令退出码。
SOL_POLICIES = ("off", "advise", "auto")
DEFAULT_SOL_POLICY = "auto"


def _sol_policy(args) -> str:
    raw = str(getattr(args, "sol", "") or os.environ.get("COPILOT_SOL")
              or DEFAULT_SOL_POLICY)
    value = raw.strip().lower()
    if value not in SOL_POLICIES:
        print(f"提示：未知 SOL 调度策略 {raw!r}；按 {DEFAULT_SOL_POLICY} 处理。",
              file=sys.stderr)
        return DEFAULT_SOL_POLICY
    return value


def _auto_execution_allowed() -> bool:
    """是否允许自动执行（会真实计费）。

    `COPILOT_SOL_AUTO=off` 是显式离线开关；测试进程（`unittest` 已加载）默认
    只记录义务、不发起付费请求——单元测试不得因为调度而访问模型。需要验证自动
    执行的测试显式设 `COPILOT_SOL_AUTO=on` 并替换 SOL 通道。
    """
    value = str(os.environ.get("COPILOT_SOL_AUTO") or "").strip().lower()
    if value in ("off", "0", "false", "no"):
        return False
    if value in ("on", "1", "true", "yes"):
        return True
    return "unittest" not in sys.modules


def _obligation_round_two(state: dict, obligation: dict) -> bool:
    """同一模式已有已完成记录时，再调用必须声明 --force/--trigger/--reason。"""
    mode = str(obligation.get("mode") or "CONSULT").upper()
    return any(isinstance(item, dict) and G.sol_ref_mode(item) == mode
               and str(item.get("status") or "") == "completed"
               for item in G.iter_sol_calls(state))


def _auto_lock_wait() -> float:
    """自动调度路径等待任务 SOL 锁的上限（秒）。

    `COPILOT_SOL_LOCK_TIMEOUT` 可覆盖；非法/非正值回落到 `SS.AUTO_LOCK_WAIT_S`。
    拿不到锁时 `_execute_obligation` 把义务保持 `due` 并退出 0（不付费、不阻断），
    而不是让工程命令无限等待另一个进程的请求。
    """
    raw = str(os.environ.get("COPILOT_SOL_LOCK_TIMEOUT") or "").strip()
    try:
        value = float(raw) if raw else SS.AUTO_LOCK_WAIT_S
    except ValueError:
        value = SS.AUTO_LOCK_WAIT_S
    return value if value > 0 else SS.AUTO_LOCK_WAIT_S


def _obligation_argv(path: str, obligation: dict, *,
                     round_two: bool) -> list[str]:
    mode = str(obligation.get("mode") or "CONSULT").upper()
    sub = {"PLAN": "sol-plan", "REVIEW": "sol-review"}.get(mode, "sol-consult")
    argv = [sub, "--state", path,
            "--obligation", str(obligation.get("obligation_id") or ""),
            "--lock-timeout", f"{_auto_lock_wait():g}"]
    if round_two:
        trigger = RP.SOL_EVENT_TRIGGERS.get(
            str(obligation.get("event")), "new-evidence")
        argv += ["--force", "--trigger", trigger, "--reason",
                 str(obligation.get("reason")
                     or "同一认知对象出现新的现场事实，需要重新判断")]
    return argv


def _execute_obligation(path: str, obligation: dict) -> int:
    """执行一个调度义务：复用既有通道，一次请求，失败只记录不阻断。"""
    state = G.load_state(path)
    obligation_id = str(obligation.get("obligation_id") or "")
    mode = str(obligation.get("mode") or "CONSULT").upper()
    argv = _obligation_argv(path, obligation,
                            round_two=_obligation_round_two(state, obligation))
    try:
        ns = build_parser().parse_args(argv)
    except SystemExit:
        print(f"SOL 调度：义务 {obligation_id} 的参数不合法；保持待执行。",
              file=sys.stderr)
        return 0
    print(f"SOL 调度：执行义务 {obligation_id}"
          f"（{obligation.get('family')}/{obligation.get('event')} → "
          f"{mode} stage={obligation.get('stage')}）", file=sys.stderr)
    try:
        return _run_sol_channel(ns, mode)
    except G.LockBusyError as exc:
        print(f"SOL 调度：{exc}；本次不执行，义务保持待执行。", file=sys.stderr)
        return 0
    except CLIError as exc:
        # 前置条件不成立（材料超限、任务已收口等）：记一次尝试；连续失败后
        # 终止该义务，避免每条命令都重复打印同一条错误。
        current = G.load_state(path)
        row = G.record_sol_obligation_error(current, obligation_id, str(exc))
        if row is not None:
            G.save_state(path, current)
        print(f"SOL 调度：义务 {obligation_id} 未能执行（{exc}）；"
              "工程进展不受影响。", file=sys.stderr)
        return 0


def _sol_schedule_step(path: str, args, *, gates_passed: bool = False,
                       execute: bool = True, prefer_family: str = "",
                       closure_ready: dict | None = None,
                       cache: dict | None = None) -> dict:
    """生命周期命令的统一调度步：物化义务 → 显示 →（auto 时）有界执行 1 个。

    `closure_ready` 是 E 阶段（收口 CONSULT）的唯一准入事实，这里只算一次并原样
    交给建议层：最后一个工作项关闭不等于最终工程验证完成，因此不能由工作项耗尽
    触发收口判断。未给出时按当前状态现算（只读、可缓存）。

    `prefer_family` 用于收口：收口前的认知窗口是 E（收口综合判断），不能被更早
    的未执行窗口挤掉；更早的窗口仍按窗口事实记为漏调。
    """
    policy = _sol_policy(args)
    state = G.load_state(path)
    if bool((state.get("closure") or {}).get("closed")):
        return {"policy": policy, "closed": True, "executed": ""}
    readiness = closure_ready if isinstance(closure_ready, dict) \
        else G.closure_readiness(state, cache=cache)
    advice = G.sol_advice_summary(state, gates_passed=gates_passed,
                                  closure_ready=readiness, cache=cache)
    synced = G.sync_sol_schedule(state, advice=advice)
    if synced["changes"]:
        G.save_state(path, state)
    due = SS.due_obligations(state)
    if prefer_family:
        preferred = [item for item in due
                     if str(item.get("family") or "") == prefer_family]
        if preferred:
            due = preferred
    optional = [item for item in SS.obligations(state)
                if isinstance(item, dict) and item.get("window") == "open"
                and str(item.get("status") or "") == "due"
                and str(item.get("level") or "") != "recommended"]
    result = {"policy": policy, "advice": advice.get("action"),
              "schedule": synced["schedule"], "executed": "", "command": "",
              "closure_ready": readiness.get("ready"),
              "closure_blockers": list(readiness.get("blockers") or []),
              "optional": [str(item.get("obligation_id")) for item in optional]}
    if not due:
        if optional and policy != "off":
            print("SOL 调度：可选义务 " + "、".join(result["optional"])
                  + "（`sol-schedule` 查看命令）")
        return result
    target = due[0]
    if policy == "auto" and execute and _auto_execution_allowed():
        result["executed"] = str(target.get("obligation_id"))
        result["exit"] = _execute_obligation(path, target)
        return result
    if policy == "off":
        return result
    command = "python3 -B " + shlex.join(
        [os.path.abspath(__file__)]
        + _obligation_argv(path, target,
                           round_two=_obligation_round_two(state, target)))
    result["command"] = command
    print(f"SOL 调度：义务 {target.get('obligation_id')}"
          f"（{target.get('family')}/{target.get('event')}）待执行")
    print("可直接执行：" + command)
    print("提示：不执行也可以继续工程推进；该义务会按窗口事实计入覆盖率。")
    return result


def _print_continuation(path: str, args=None) -> int:
    """回填类命令的统一续跑判定：仍有可执行工作时返回 10。

    阶段报告与工作项回填都是进度记录，不是任务终点。退出码 10 沿用 `drive`
    的口径（有待执行工作），调用方必须继续而不是收束回合。
    """
    if args is not None:
        # 调度步放在续跑判定之前：先给出（或执行完）当前的 SOL 认知窗口。
        _sol_schedule_step(path, args)
    state = G.load_state(path)
    ev = G.evaluate(state)
    cont = ev.get("continuation") or {}
    if cont.get("required"):
        print(f"续跑：必须继续（{cont.get('action')}）—— {cont.get('reason')}")
        if cont.get("executable"):
            print("可执行工作项：" + "、".join(cont["executable"]))
        if cont.get("derivable"):
            print("可派生工作项：" + "、".join(cont["derivable"]))
        if cont.get("unattributed_criteria"):
            print("未归属完成条件：" + "、".join(cont["unattributed_criteria"])
                  + "（若由已阻塞工作项承载，先 block --criterion 归属，"
                    "不要反复派生执行不了的 AUTO-C）")
        if cont.get("next_command"):
            print(f"下一步：{cont.get('next_command')}")
        print("提示：该动作未完成前不要结束回合；报告与阶段结论都不是任务终点。")
        return EXIT_CONTINUE
    if cont.get("action") == "wait":
        print(f"续跑：等待外部条件 —— {cont.get('reason')}")
        return 0
    print(f"续跑：无需继续（{cont.get('reason')}）")
    return 0


# ── 命令实现 ───────────────────────────────────────────────────────────────
def _under_tasks_dir(path: str) -> bool:
    parts = os.path.abspath(path).split(os.sep)
    return ".copilot-tasks" in parts


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
        test_policy=_load_test_policy(args),
        task_kind=args.task_kind or "",
        triggers=args.trigger or [])
    print(f"任务已创建：{state['task_id']}")
    print(f"状态文件：{os.path.abspath(state_path)}")
    if not _under_tasks_dir(state_path):
        print("提示：状态文件不在 .copilot-tasks/ 下。Stop/SessionStart/UserPromptSubmit 只扫描"
              " <workspace>/.copilot-tasks/*/state.json，该任务不会被钩子发现、也不会自动"
              "续跑或恢复；建议移到 .copilot-tasks/<task>/state.json。")
    if state.get("workspace"):
        print(f"任务工作区：{state['workspace']}")
    print(f"完成条件：{len(state['contract']['criteria'])} 条 "
          f"（{', '.join(c['criterion_id'] for c in state['contract']['criteria'])}）")
    print(f"风险：{state['risk']} · 类型：{state['task_kind']} "
          f"· 执行后端：{state['execution_backend']}")
    # 生命周期调度（v9.6）：启动认知窗口在 start 自身事务内兑现——`--sol auto` 时真的
    # 执行一次 CONSULT，而不是只打印命令等主代理记得跑；`advise` 给无占位符的可执行
    # 命令；`off` 明确静默。
    step = _sol_schedule_step(state_path, args)
    advice = G.sol_advice_summary(state)
    if step.get("executed"):
        print(f"SOL 调度：已执行义务 {step['executed']}（启动认知判断，记录见 sol_calls）")
    elif step.get("command"):
        print(f"SOL 默认流程：{advice['action'].upper()}（{advice.get('stage') or '-'}）")
        print(f"理由：{advice.get('reason')}")
    elif step.get("policy") == "off":
        print("SOL 默认流程：跳过（--sol off）")
    elif advice.get("action") in ("consult", "plan", "review"):
        print(f"SOL 默认流程：{advice['action'].upper()}（{advice.get('stage') or '-'}）")
        print(f"理由：{advice.get('reason')}")
        if advice.get("command"):
            print(f"下一步：{advice['command']}")
    else:
        print(f"SOL 默认流程：跳过（{advice.get('reason')}）")
    return 0


def _context_hint(state_path: str, state: dict) -> str:
    """status 的人工输出附一行上下文提示（非阻断、可选能力）。

    JSON 契约不变：结构化输出里不混入提示。Context Engine 缺失或任何异常都返回空串，
    status 的可用性不依赖它。
    """
    try:
        import context_engine as CE
        workspace = str(state.get("workspace") or "")
        if not workspace:
            return ""
        info = CE.resolve(workspace)
    except Exception:  # noqa: BLE001 - 可选能力，失败即不提示
        return ""
    try:
        entries = [m for m in info.get("memory") or [] if m.get("role") == "root"]
        memory = next((m for m in entries if m.get("stable")), {})
        mem = memory.get("status") or "未知"
        if mem == "available":
            mem = f"available（索引 {memory.get('index_lines') or 0} 行）"
        elif not memory and any(m.get("status") == "unreadable" for m in entries):
            mem = "unreadable（不是 absent）"
        git = info.get("git") or {}
        head = (git.get("head") or "")[:12] if git.get("available") else ""
        where = info["project"]["root"] + (f" · {git.get('branch')}@{head}" if head else "")
        engine = os.path.join(HERE, "context_engine.py")
        return ("上下文（可选）：" + where + f" · 原生 Memory {mem}\n"
                "  需要历史证据或子 Agent 交接包时：python3 -B " + shlex.quote(engine)
                + f" recall --workspace {shlex.quote(info['project']['root'])}"
                  " --keywords '词1|词2' --state " + shlex.quote(state_path))
    except Exception:  # noqa: BLE001
        return ""


def cmd_next(args) -> int:
    state = G.load_state(_state_path(args))
    cache: dict = {}
    ev = G.evaluate(state, cache=cache)
    if args.json:
        _print({"task_id": state.get("task_id"), **ev})
    else:
        print(G.render_status(state, ev, cache=cache))
        hint = _context_hint(_state_path(args), state)
        if hint:
            print(hint)
    return 0


def cmd_plan(args) -> int:
    text = _read_text(args.plan) if args.plan else sys.stdin.read()
    plan = _load_json_doc(text, "计划")
    result = G.plan_state(_state_path(args), plan, source=args.source or "zcode")
    if result["idempotent"]:
        print(f"计划已存在（{result['plan']['plan_id']}），未重复登记。")
        return _print_continuation(_state_path(args), args)
    print(f"计划已登记：{result['plan']['plan_id']}")
    for action in result["actions"]:
        print(f"- {action['id']} [{action['assignee']}] {action['title']}")
    return _print_continuation(_state_path(args), args)


def cmd_record(args) -> int:
    action = G.record_action(
        _state_path(args), args.id, args.status, result=args.result or "",
        evidence_ids=_evidence_ids(args.evidence))
    print(f"{action['id']} → {action['status']}"
          + (f"；证据 {', '.join(action['evidence_ids'])}"
             if action["evidence_ids"] else ""))
    return _print_continuation(_state_path(args), args)


def cmd_rerun(args) -> int:
    action = G.rerun_action(_state_path(args), args.id, reason=args.reason or "")
    print(f"{action['id']} → pending（第 {action['rerun_count']} 次重开）；"
          "旧结果已保存在 rerun_history。")
    return _print_continuation(_state_path(args), args)


def cmd_criterion(args) -> int:
    criterion = G.record_criterion(
        _state_path(args), args.id, args.status,
        evidence_ids=_evidence_ids(args.evidence))
    print(f"{criterion['criterion_id']} → {criterion['status']}"
          + (f"；证据 {', '.join(r['id'] for r in criterion['evidence_refs'])}"
             if criterion["evidence_refs"] else ""))
    return _print_continuation(_state_path(args), args)


def cmd_judgment(args) -> int:
    entry = G.record_judgment(
        _state_path(args), args.status, args.summary,
        evidence_ids=_evidence_ids(args.evidence))
    print(f"判断已记录：{entry['judgment_id']}（{entry['status']}）")
    _sol_schedule_step(_state_path(args), args)
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
        return _print_continuation(path, args)
    entry = G.add_report(path, summary,
                         blocked=bool(args.blocked), reason=args.reason or "")
    print(f"报告已登记：sha256={entry['report_sha256'][:16]}…")
    print("注意：报告是进度记录，不是任务终点；以续跑判定为准。")
    return _print_continuation(path, args)


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
    return _print_continuation(_state_path(args), args)


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
    # 用户决策是 decision-point 的可归因事实：这里同步一次调度窗口。
    _sol_schedule_step(_state_path(args), args)
    return 0


def cmd_block(args) -> int:
    items = list(getattr(args, "work_item", None) or [])
    scope = str(getattr(args, "scope", "") or "").strip()
    if not items and scope != "task":
        raise CLIError(
            "block 必须指定作用范围：--work-item <id>（推荐，只阻塞该工作项）"
            " 或 --scope task（仅当目标/资源整体不可推进时才允许）。"
            "单条授权缺口（commit / push / 部署 / 生产数据 / 付费 / 用户决策）"
            "不得冻结整个任务。")
    if items and scope == "task":
        raise CLIError("--work-item 与 --scope task 不能同时使用。")
    if scope == "task" and not getattr(args, "force", False):
        # 任务级阻塞会把仍有可执行路径的工作一起冻结。只有在目标/资源整体不可
        # 推进时才允许；仍有可领取项时必须显式 --force，并优先改写为项级阻塞。
        state = G.load_state(_state_path(args))
        ready = [str(a.get("id") or "") for a in
                 G.ready_actions(state, include_claimed=True)]
        if ready:
            raise CLIError(
                "仍有可执行工作项（" + "、".join(ready) + "），不得直接用 "
                "--scope task 冻结整个任务。请改成 --work-item <id> 只阻塞受阻项；"
                "只有确认目标/资源整体不可推进时，才加 --force 登记任务级阻塞。")
    block = G.add_block(_state_path(args), args.reason, args.needs,
                        work_items=items,
                        criteria=list(getattr(args, "criterion", None) or []),
                        force=bool(getattr(args, "force", False)))
    if block.get("work_items"):
        print(f"已阻塞工作项：{', '.join(block['work_items'])}（{block['block_id']}）")
    else:
        print(f"已进入任务级 BLOCKED：{block['block_id']}")
    if block.get("criteria"):
        print(f"同时卡住的完成条件：{', '.join(block['criteria'])}")
    elif block.get("work_items"):
        # 条件归属缺口：本项被阻塞却没有声明它承载哪些条件，未闭合条件会继续被
        # 派生为 AUTO-C<n>，任务永远到不了 BLOCKED。这里把缺口直接说清楚。
        state = G.load_state(_state_path(args))
        pending = [str(c.get("criterion_id") or "")
                   for c in (state.get("contract") or {}).get("criteria") or []
                   if str(c.get("status") or "pending") != "met"]
        if pending:
            print(f"警告：本次阻塞未归属完成条件；仍未闭合：{', '.join(pending)}")
            print("若这些条件正由本工作项承载，请补登记归属："
                  f"copilot_dispatch.py block --state <state> --work-item "
                  f"{block['work_items'][0]} --criterion {pending[0]} "
                  "--reason \"<为何由该项承载>\" --needs \"<解除条件>\"；"
                  "否则每一轮都会派生执行不了的 AUTO-C。")
    print(f"原因：{block['reason']}")
    print(f"所需条件：{block['needs']}")
    print("提示：未阻塞的工作项继续执行；不要因单项阻塞结束回合。")
    return _print_continuation(_state_path(args), args)


def cmd_unblock(args) -> int:
    result = G.resolve_block(_state_path(args), args.reason,
                             block_id=args.block_id or "",
                             by=args.by or "zcode",
                             quote=args.quote or "")
    print(f"已解除 {result['resolved']} 条阻塞：{result['reason']}")
    print(f"解除方：{result.get('resolved_by') or 'zcode'}"
          + ("（调用方声明；原话已记录）" if result.get("resolved_by") == "user"
             else ""))
    if result.get("block_ids"):
        print("block_id：" + ", ".join(result["block_ids"]))
    if result.get("resumed"):
        print("已恢复工作项：" + ", ".join(result["resumed"]) + "（重新进入可执行）")
    return _print_continuation(_state_path(args), args)


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
        _sol_schedule_step(path, args)
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
                                    file_path=out_path, note=args.note or "",
                                    capture_cmd=args.capture_cmd)
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
    # 证据登记是最密集的生命周期动作：在这里给出（或执行）当前认知窗口，
    # 但普通证据追加本身不会产生新义务（是否触发由事件判据决定）。
    _sol_schedule_step(path, args)
    return 0


def cmd_closure_check(args) -> int:
    path = _state_path(args)
    state = G.load_state(path)
    check = G.closure_check(
        state, task_dir=os.path.dirname(os.path.abspath(path)))
    # 门禁结果必须体现在退出码上：FAIL 仍返回 0 会让调用方（和管道后的 shell）
    # 把“检查过”误当“通过了”。PASS=0，FAIL=1。
    exit_code = 0 if check.get("passed") else 1
    if args.json:
        _print({"task_id": state.get("task_id"), **check})
        return exit_code
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
    if exit_code:
        print("门禁未通过：这不是任务终点；按 drive 的下一步继续处理缺口，"
              "不要以本次检查结果收束回合。")
    return exit_code


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
        evidence_ids=_evidence_ids(args.evidence), note=args.note)
    print(f"Finding {item['finding_id']} → {item['status']}"
          f"（{item.get('severity')}）")
    if item["status"] == "fixed":
        print("下一步：运行定向测试/检查并登记证据，再标记 verified；"
              "不需要重新派发完整复核。")
    return _print_continuation(path, args)


def cmd_review_admission(args) -> int:
    """SOL 主动介入建议（只读，不是门禁；复杂度 + 覆盖 + 门禁事实）。"""
    path = _state_path(args)
    state = G.load_state(path)
    try:
        decision = G.review_admission(
            state, triggers=args.trigger, reason=args.reason,
            user_request=bool(getattr(args, "user_request", False)),
            review_requested=bool(getattr(args, "review_request", False)))
    except (RP.ReviewPolicyError, SR.SolUsageError) as exc:
        raise CLIError(str(exc)) from None
    if args.json:
        _print(decision)
    else:
        advice = decision.get("advice") or {}
        complexity = advice.get("complexity") or {}
        signals = [item.get("code") for item in complexity.get("signals") or []]
        print(f"任务类型：{advice.get('task_kind') or decision.get('task_kind') or '-'}")
        print(f"任务复杂度：{complexity.get('level') or '-'}"
              f"（信号：{', '.join(signals) if signals else '无'}）")
        print(f"SOL 建议：{advice.get('action') or '-'}"
              f"（mode={advice.get('mode') or '-'}，"
              f"stage={advice.get('stage') or advice.get('phase') or '-'}）")
        print(advice.get("reason") or "")
        if advice.get("new_signals"):
            print("新增信号：" + ", ".join(advice["new_signals"]))
        if advice.get("second_round") and advice.get("trigger"):
            print(f"第二轮触发：{advice['trigger']}")
        if advice.get("command"):
            print("调用：" + advice["command"])
        if decision.get("recommended_action"):
            print(f"最终建议：{decision['recommended_action']}")
        print(f"风险策略：{decision['decision']}"
              f"（tier={decision.get('tier') or decision['risk']}）"
              f" — {decision.get('detail') or ''}")
        if decision.get("triggers"):
            print("触发因素：" + ", ".join(decision["triggers"]))
        by_mode = decision.get("sol_calls_by_mode") or {}
        print(f"已有 SOL 调用：{decision.get('sol_calls_total', 0)} 次"
              f"（CONSULT {by_mode.get('CONSULT', 0)} / "
              f"PLAN {by_mode.get('PLAN', 0)} / "
              f"REVIEW {by_mode.get('REVIEW', 0)}）")
        print("说明：CONSULT/PLAN 是默认认知输入（advisory），REVIEW 仅在用户明确"
              "要求时执行；都不构成收口门禁，单轮调用、不自动二审。")
    return 0


def _write_atomic(path: str, text: str) -> str:
    path = os.path.abspath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    # mkstemp 使用不可预测文件名与 O_EXCL，避免同机攻击者预置符号链接截断目标。
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
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
                         subdir: str = "sol-reviews",
                         prompt: str = "") -> dict | None:
    """读取已取得响应但尚未登记完成的 SOL 结果，避免再次付费。"""
    path = _pending_result_path(task_dir, digest, subdir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    # pending 文件名使用 subject 摘要；`materials_sha256` 是 run_sol() 对实际
    # 提示词文本的哈希，二者不是同一个值。
    if value.get("subject_sha256"):
        if str(value["subject_sha256"]) != digest:
            return None
    else:
        # 旧格式没有 subject_sha256：只有能重算出同一提示词哈希时才恢复，
        # 不能仅凭文件名接受一份可能属于其他材料的 pending。
        recorded = str(value.get("materials_sha256") or "")
        if not prompt or recorded != hashlib.sha256(
                prompt.encode("utf-8")).hexdigest():
            return None
    if not str(value.get("reply_sha256") or ""):
        return None
    return value


def _invoke_sol_once(*, state_path: str, task_dir: str, prompt: str,
                     digest: str, mode: str, args, audit_log: str,
                     audit_subdir: str,
                     stage: str = "") -> tuple[dict | None, int]:
    """共享的一次调用路径：pending 恢复 → 一次请求 → 落可恢复结果。

    返回 `(result, exit_code)`；result 为 None 表示失败且已打印真实原因。

    CONSULT / PLAN 是默认认知引擎（`THINK` 只是归一为 CONSULT 的历史别名）：
    失败时只记录并降级（返回 0），主代理按已有信息继续，SOL 不可用不构成工程
    阻断。REVIEW 是用户明确要求的独立结论：失败仍返回真实非零退出码，不伪造
    PASS。
    """
    recovered = False
    result = _load_pending_result(task_dir, digest, audit_subdir, prompt)
    if result is not None:
        recovered = True
        print("发现已取得、尚未登记完成的 SOL 结果：直接恢复登记，"
              "不重复发起请求。", file=sys.stderr)
    else:
        try:
            call = {
                "REVIEW": SR.run_review,
                "PLAN": SR.run_plan,
                "CONSULT": SR.run_consult,
                "THINK": SR.run_consult,
            }.get(mode, SR.run_consult)
            result = call(
                prompt, audit_log=audit_log,
                max_output_tokens=args.max_output_tokens,
                timeout=args.timeout)
        except SR.SolError as exc:
            G.record_sol_call_failure(
                state_path, mode=mode, error_code=exc.code, detail=exc.detail,
                materials_sha256=digest, model=SR.MODEL,
                obligation_id=str(getattr(args, "obligation_id", "") or ""),
                stage=stage)
            print(f"SOL {mode} 失败 [{exc.code}]：{exc.detail or ''}",
                  file=sys.stderr)
            if mode == "REVIEW":
                print("已记录真实失败原因，未自动重试、未切换 Provider；"
                      "独立复核未完成，如实报告，不伪造结论。",
                      file=sys.stderr)
                return None, exc.exit_code
            print("已记录真实失败原因，未自动重试、未切换 Provider；"
                  "SOL 不可用不阻断工程推进：主代理按现有判断继续，"
                  "新增关键信息后可人工重新发起。", file=sys.stderr)
            if getattr(args, "json", False):
                _print({
                    "schema": "copilot-sol-cognitive-degraded/v1",
                    "status": "unavailable",
                    "action": "continue",
                    "mode": mode,
                    "error": exc.code,
                    "detail": exc.detail,
                    "materials_sha256": digest,
                })
            return None, 0
        # 远程已成功：先落一份可恢复结果，再写状态。状态/审计落盘失败时，
        # 下一次同材料调用直接恢复这份结果，不会再次付费。
        # pending 以 subject 摘要命名，必须显式保存该键；`materials_sha256`
        # 仍保留提示词文本摘要用于审计，两者不能混用。
        pending = {**result, "subject_sha256": digest}
        try:
            _write_atomic(
                _pending_result_path(task_dir, digest, audit_subdir),
                json.dumps(pending, ensure_ascii=False, sort_keys=True))
        except CLIError as exc:
            print(f"警告：无法写入待登记结果（不影响本次登记）：{exc}",
                  file=sys.stderr)
    result["recovered_from_pending"] = recovered
    return result, 0


def _sol_channel_config(mode: str) -> dict:
    """SOL 各模式的少量差异（原始输出目录、文案与预检 schema）。

    记录、审计与互斥锁已统一：所有模式写入同一 `sol_calls` 流、同一
    `sol-audit.jsonl` 与同一 `.sol.lock`，不再按模式复制一套通道。
    """
    if mode == "REVIEW":
        return {
            "subdir": "sol-reviews",
            "label": "复核",
            "dry_run_schema": "copilot-sol-review-dry-run/v1",
            "closed_note": "任务已收口；新复核需先由用户授权 reopen",
        }
    if mode == "PLAN":
        return {
            "subdir": "sol-plans",
            "label": "方案规划",
            "dry_run_schema": "copilot-sol-plan-dry-run/v1",
            "closed_note": "任务已收口；新的方案规划需先由用户授权 reopen",
        }
    return {
        "subdir": "sol-consult",
        "label": "认知咨询",
        "dry_run_schema": "copilot-sol-consult-dry-run/v1",
        "closed_note": "任务已收口；新的认知咨询需先由用户授权 reopen",
    }


def _sol_list(args, state: dict, mode: str, cfg: dict) -> int:
    records = G.list_sol_calls(state, mode=mode)
    if args.json:
        _print({"task_id": state.get("task_id"), "mode": mode,
                "sol_calls": records})
    else:
        print(f"SOL {cfg['label']}记录：{len(records)} 条")
        for item in records:
            detail = (f"verdict={item.get('verdict')}"
                      if mode == "REVIEW"
                      else f"mode={item.get('mode')}")
            print(f"- {G.sol_call_id(item)} [{item.get('status')}] {detail} "
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
        detail = (f"verdict={existing.get('verdict')}" if mode == "REVIEW"
                  else f"mode={existing.get('mode')}")
        print(f"SOL {cfg['label']}已完成：{G.sol_call_id(existing)} {detail}")
        print(decision["note"])
    return 0


def _run_sol_channel(args, mode: str) -> int:
    """SOL 两模式共用的一次调用路径；模式差异只在提示词、记录结构与失败语义。"""
    path = _state_path(args)
    state = G.load_state(path)
    # 单次调用内复用证据摘要：收口准入在锁外预检、锁内复核都读同一份缓存。
    readiness_cache: dict = {}
    if G.is_legacy_state(state):
        raise CLIError("v1/v2 SOL 历史状态只读；请为新任务创建 v6+ 状态。")
    cfg = _sol_channel_config(mode)
    if args.list:
        return _sol_list(args, state, mode, cfg)
    obligation = None
    obligation_id = str(getattr(args, "obligation_id", "") or "").strip()
    if obligation_id:
        obligation = SS.find_obligation(state, obligation_id=obligation_id)
        if obligation is None:
            raise CLIError(
                f"当前任务的调度义务中不存在 {obligation_id}；"
                "用 `sol-schedule` 查看可执行义务。")
    if not args.materials:
        if obligation is None:
            raise CLIError(
                {"REVIEW": "sol-review", "PLAN": "sol-plan"}.get(mode, "sol-consult")
                + " 需要 --materials（或 --list）。")
        # 调度执行时材料由状态确定性生成：主代理不必先写材料文件，也不会因为
        # 懒于写材料而放弃一次真实调用。手动调用仍以 --materials 为准。
        # 材料里的验证事实与收口准入同源：非收口窗口只给必要测试的验证状态
        # （便宜、不重算证据摘要），收口窗口给完整的最终验证准入。
        tests_ok, tests_source = G.test_verification_evidence(state)
        facts: dict = {"required_tests_completed": tests_ok,
                       "tests_source": tests_source}
        if str(obligation.get("family") or "") == "E" \
                or str(obligation.get("stage") or "") == "closure":
            facts = G.closure_readiness(state)
        materials = SS.build_materials(state, obligation, readiness=facts)
        print(f"材料：按调度义务 {obligation_id} 自动生成 "
              f"（{len(materials)} 字符）", file=sys.stderr)
    else:
        materials = _read_text(args.materials)
    materials, knowledge_meta = _compose_materials(
        materials, str(getattr(args, "knowledge_file", "") or ""))
    if not materials.strip():
        raise CLIError("材料为空。")
    question = str(getattr(args, "question", "") or "").strip()
    stage = str(getattr(args, "stage", "") or "").strip().lower()
    if obligation is not None:
        question = question or str(obligation.get("question") or "")
        stage = stage or str(obligation.get("stage") or "")
    if mode in ("CONSULT", "PLAN"):
        stage = SR.normalize_stage(stage, mode=mode)
        # 收口阶段的准入先于其它参数校验：材料与问题都齐也不能拿未完成的最终
        # 验证去问收口问题。锁内会用同一份缓存复核一次，避免重复重读证据文件。
        if mode == "CONSULT" and stage == "closure":
            early = G.closure_readiness(state, cache=readiness_cache)
            if not early.get("ready"):
                raise CLIError(
                    "最终工程验证尚未完成，不能执行收口 CONSULT："
                    + "；".join(G.closure_blockers_text(early))
                    + "。先完成必要测试、证据登记与条件核销。")
        if not question:
            raise CLIError(
                f"SOL {mode} 需要 --question：必须写明本次真正需要判断的事项；"
                "没有明确问题时不要调用 SOL。")

    def prepare(current: dict) -> tuple[str, str, str, dict]:
        # 各模式共用同一上下文构造：合同、完成条件、工作项与证据登记自动注入，
        # 主代理只需提供本次增量的材料与具体问题。
        prompt_text = SR.build_prompt(current, materials, mode=mode,
                                      question=question, stage=stage)
        subject = SR.review_subject_sha256(
            current, materials, mode=mode, question=question, stage=stage)
        # 上限口径与真实调用一致（认知模式比 REVIEW 更紧）：超限拒绝而非截断。
        limit = SR.MATERIALS_LIMITS[mode]
        if len(prompt_text) > SR.MATERIALS_WARN[mode]:
            print(f"提示：材料 {len(prompt_text)} 字符，较大；"
                  f"上限 {limit}，超限会拒绝而不是静默截断。",
                  file=sys.stderr)
        context = SR.review_digest(prompt_text)
        decision = SR.single_round_decision(
            current, subject, force=bool(args.force),
            reason=args.reason, trigger=args.trigger,
            records_key="sol_calls", mode=mode)
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
            "question": question,
            "stage": stage,
            # 缓存/复用口径：这些成分变化即不再复用旧结论。
            "prompt_version": SR.PROMPT_VERSION,
            "cache_key_components": ["prompt_version", "model",
                                     "reasoning_effort", "goal", "contract",
                                     "criteria", "evidence_digest",
                                     "materials", "mode", "question", "stage"],
            "evidence_digest": SR.evidence_digest(state),
            "evidence_entries": len(state.get("evidence") or []),
            "action_entries": len(state.get("actions") or []),
            "knowledge_file": knowledge_meta.get("knowledge_file") or "",
            "knowledge_block_sha256": knowledge_meta.get(
                "knowledge_block_sha256") or "",
            "knowledge_entries": knowledge_meta.get("knowledge_entries") or 0,
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
            print(f"共享上下文：提示词 {info['prompt_version']}，"
                  f"证据 {info['evidence_entries']} 条，"
                  f"工作项 {info['action_entries']} 个")
            print("零工具：请求体不含 tools/functions；tool_choice=none")
        return 0

    task_dir = os.path.dirname(os.path.abspath(path))
    # 跨进程互斥：同一任务只允许一个 SOL 调用走“检查—调用—登记”临界区，
    # 避免并发进程在完成记录写入前各自发起一次付费请求（三模式共用一把锁）。
    # 生命周期命令使用 `--lock-timeout` 有界等待：拿不到锁说明另一个调用正在
    # 进行，本次放弃执行（义务保持待执行），而不是阻塞工程命令。
    with G.task_lock(task_dir, ".sol.lock",
                     timeout=getattr(args, "lock_timeout", None)):
        state = G.load_state(path)
        # 收口阶段（E 窗口）的唯一执行前提：最终工程验证真的完成，且待执行义务
        # 对应的就是当前这份验证摘要。两条都不满足时不得付费调用，也不把旧摘要
        # 的结论当成新结果的结论。
        final_digest = ""
        if mode == "CONSULT" and stage == "closure":
            readiness = G.closure_readiness(state, cache=readiness_cache)
            if not readiness.get("ready"):
                raise CLIError(
                    "最终工程验证尚未完成，不能执行收口 CONSULT："
                    + "；".join(G.closure_blockers_text(readiness))
                    + "。先完成必要测试、证据登记与条件核销。")
            final_digest = str(readiness.get("digest") or "")
            expected = str((obligation or {}).get("instance") or "")
            if expected and expected != final_digest:
                raise CLIError(
                    f"调度义务 {obligation_id} 对应的最终验证摘要已变化"
                    f"（义务 {expected} / 当前 {final_digest}）：旧摘要的判断不能"
                    "覆盖新结果；重新执行生命周期命令以刷新义务后再调用。")
        prompt, digest, context_sha, decision = prepare(state)
        outcome = _sol_reuse_or_refuse(decision, args, mode, cfg)
        if outcome is not None:
            if outcome == 0 and obligation is not None:
                # 同一认知对象已有有效结论：义务由复用满足，不再付费。
                current = G.load_state(path)
                existing = decision.get("review") or {}
                G.resolve_sol_obligation(
                    current, obligation_id, status="satisfied", source="reuse",
                    call_id=(G.sol_call_id(existing)
                             if isinstance(existing, dict) else ""))
                G.save_state(path, current)
            return outcome
        if (state.get("closure") or {}).get("closed"):
            raise CLIError(cfg["closed_note"] + "（--list 仍可只读查看）。")
        audit_log = args.audit_log or os.path.join(task_dir, "sol-audit.jsonl")
        result, code = _invoke_sol_once(
            state_path=path, task_dir=task_dir, prompt=prompt,
            digest=digest, mode=mode, args=args, audit_log=audit_log,
            audit_subdir=cfg["subdir"], stage=stage)
        if result is None:
            return code
        reply_text = result.pop("reply_text", "")
        result["reply_sha256"] = result.get("reply_sha256") or ""
        output_dir = args.output_dir or os.path.join(task_dir, cfg["subdir"])
        record_id = "SC" + secrets.token_hex(12)
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
        # 统一的调用记录：各模式共享同一状态流与同一字段口径；模式差异只在
        # 结果字段（REVIEW 的 findings / 认知模式的判断与方案结构）。
        # `complexity_signals` 是调用时刻的复杂度快照：后续判断“是否出现新的
        # 认知信号”只需要比较快照，不需要重新推理。
        complexity = RP.task_complexity(
            state,
            triggers=[str(t.get("name") or "")
                      for t in state.get("admission_triggers") or []
                      if isinstance(t, dict) and t.get("name")],
            workspace=str(state.get("workspace") or ""))
        record = {
            # 原始输出文件名与统一记录 ID 必须一致，审计才能按 call_id 直接定位。
            "call_id": record_id,
            "task_id": state.get("task_id") or "",
            "contract_sha256": state.get("contract_sha256") or "",
            "risk": state.get("risk") or "",
            "task_kind": str(state.get("task_kind") or RP.DEFAULT_TASK_KIND),
            "complexity": complexity["level"],
            "complexity_signals": complexity["signal_codes"],
            "status": "completed",
            "mode": mode,
            "stage": stage if mode in ("CONSULT", "PLAN") else "",
            # 收口判断的身份：E 的去重与失效都以这份最终验证摘要为准。
            "final_verification_digest": final_digest,
            "question": question,
            "progress_digest": (RP.sol_progress_key(state)
                                if mode in ("CONSULT", "PLAN") else ""),
            "prompt_version": SR.PROMPT_VERSION,
            "evidence_digest": SR.evidence_digest(state),
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
            "knowledge_file": knowledge_meta.get("knowledge_file") or "",
            "knowledge_block_sha256": knowledge_meta.get(
                "knowledge_block_sha256") or "",
            "knowledge_entries": knowledge_meta.get("knowledge_entries") or 0,
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
            "summary": (result.get("summary")
                        or result.get("judgment") or ""),
            "unverified": result.get("unverified") or [],
            "audit_written": result.get("audit_written", True),
            "audit_error": result.get("audit_error") or "",
            "recovered_from_pending": result.get("recovered_from_pending", False),
            "zero_tool": True,
            "attempts": 1,
            "completed_at": result.get("completed_at"),
        }
        if mode == "REVIEW":
            record.update({
                "schema": SR.REVIEW_SCHEMA,
                "verdict": result["verdict"],
                "findings": result.get("findings") or [],
                "findings_count": result.get("findings_count") or {},
                "materials_reviewed": result.get("materials_reviewed") or [],
                "verdict_adjusted": result.get("verdict_adjusted") or [],
            })
        else:
            record.update({
                "schema": SR.THINK_SCHEMA,
                "judgment": result.get("judgment") or "",
                "goal_understanding": result.get("goal_understanding") or "",
                "essence": result.get("essence") or "",
                "strategy": result.get("strategy") or "",
                "contradictions": result.get("contradictions") or [],
                "alternatives": result.get("alternatives") or [],
                "tradeoffs": result.get("tradeoffs") or [],
                "risks": result.get("risks") or [],
                "priority": result.get("priority") or [],
                "investigation": result.get("investigation") or [],
                "phase_goals": result.get("phase_goals") or [],
                "acceptance_focus": result.get("acceptance_focus") or [],
                "assumptions": result.get("assumptions") or [],
                "omissions": result.get("omissions") or [],
                "decisions": result.get("decisions") or [],
                "verify": result.get("verify") or [],
                "reconsult": result.get("reconsult") or [],
            })
        record = G.record_sol_call(
            path, record, mode=mode, materials_sha256=digest,
            trigger=decision.get("trigger") or "",
            reason=decision.get("reason") or "",
            obligation_id=obligation_id)
        try:
            os.unlink(_pending_result_path(task_dir, digest, cfg["subdir"]))
        except OSError:
            pass
        record_id = G.sol_call_id(record)
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
            label = cfg["label"]
            print(f"SOL {mode}（{label}）完成：{record_id}"
                  + (f" stage={record.get('stage')}" if record.get("stage") else ""))
            print(f"核心判断：{record.get('judgment') or '(无)'}")
            if record.get("goal_understanding"):
                print(f"目标理解：{record['goal_understanding']}")
            if record.get("strategy"):
                print(f"推荐策略：{record['strategy']}")
            print("产出：" + "，".join([
                f"alternatives={len(record.get('alternatives') or [])}",
                f"tradeoffs={len(record.get('tradeoffs') or [])}",
                f"risks={len(record.get('risks') or [])}",
                f"priority={len(record.get('priority') or [])}",
                f"omissions={len(record.get('omissions') or [])}",
                f"verify={len(record.get('verify') or [])}"]))
            if record.get("unverified"):
                print(f"未验证事项：{len(record['unverified'])} 条（见结果 JSON）")
            print(f"原始输出：{raw_path or '(写入失败，见结构化结果)'}")
            if not record.get("audit_written", True):
                print("警告：审计日志写入失败；认知结果已登记，"
                      f"错误 {record.get('audit_error') or '-'}。", file=sys.stderr)
            print("说明：SOL 只提供认知输入；是否采纳、如何实现与验证由主代理决定。"
                  f"采纳结论用 sol-accept --call-id {record_id} 登记。")
        return 0


def cmd_sol_review(args) -> int:
    """SOL 零工具单轮复核：材料 → 一次请求 → 结构化结果 → 建议性 findings。"""
    return _run_sol_channel(args, "REVIEW")


def cmd_sol_consult(args) -> int:
    """SOL 默认认知咨询：目标理解、问题本质、方案取舍与风险；失败只降级。"""
    return _run_sol_channel(args, "CONSULT")


def cmd_sol_plan(args) -> int:
    """SOL 方案规划：调查后比较方案、优先级与验收关注点；失败只降级。"""
    return _run_sol_channel(args, "PLAN")


def cmd_sol_think(args) -> int:
    """历史命令：v9 起等价于 `sol-consult`。"""
    return _run_sol_channel(args, "CONSULT")


def cmd_sol_accept(args) -> int:
    """回填主代理对 SOL 建议的采纳结论（事实记录，不改变任务状态与门禁）。"""
    path = _state_path(args)
    result = G.record_sol_acceptance(
        path, args.call_id, status=args.status,
        note=args.note or "", reason=args.reason or "",
        decision=args.decision or "", rationale=args.rationale or "",
        delta=args.delta or "",
        evidence_refs=args.evidence_ref or [],
        source_refs=args.source_ref or [],
        evidence_note=args.evidence_note or "",
        decided_by=args.decided_by or "zcode")
    if args.json:
        _print(result)
    else:
        entry = result["acceptance"]
        print(f"SOL 采纳结论已登记：{result['call_id']} → {entry['status']}"
              + (f"（{entry['note']}）" if entry.get("note") else ""))
        print("记录：" + "，".join([
            f"实际决策={'有' if entry.get('decision') else '缺'}",
            f"理由={'有' if G.acceptance_rationale(entry) else '缺'}",
            f"差异={'有' if entry.get('delta') else '缺'}"]))
        resolution = entry.get("evidence_resolution") or {}
        print("证据关联：" + "，".join([
            "已登记 " + ("、".join(resolution.get("linked") or []) or "无"),
            "待核 " + ("、".join(resolution.get("unresolved") or []) or "无"),
            "已归档 " + ("、".join(resolution.get("archived") or []) or "无"),
            "来源定位 " + str(len(entry.get("source_refs") or [])) + " 条"]))
        if result["hints"]:
            print("提示（非阻断，不影响登记与任务推进）：")
            for hint in result["hints"]:
                print(f"- {hint}")
        print("说明：这是主代理对建议的事实判断记录；不是用户授权，也不改变 G1–G3。")
    return 0


def _sol_schedule_view(state: dict, path: str) -> dict:
    """调度视图（只读）：义务清单、下一条可执行项与可直接执行的命令。"""
    due = SS.due_obligations(state)
    rows = []
    for item in SS.obligations(state):
        if not isinstance(item, dict):
            continue
        rows.append({
            "obligation_id": item.get("obligation_id"),
            "family": item.get("family"),
            "event": item.get("event"),
            "mode": item.get("mode"),
            "stage": item.get("stage"),
            "level": item.get("level"),
            "status": item.get("status"),
            "window": item.get("window"),
            "missed": SS.is_missed(item),
            "source": item.get("source"),
            "call_id": item.get("call_id"),
            "opened_at": item.get("opened_at"),
            "closed_reason": item.get("closed_reason"),
            "skip_reason": item.get("skip_reason"),
            "failure": item.get("failure"),
        })
    command = ""
    if due:
        command = "python3 -B " + shlex.join(
            [os.path.abspath(__file__)]
            + _obligation_argv(path, due[0],
                               round_two=_obligation_round_two(state, due[0])))
    return {"schema": SS.SCHEDULE_SCHEMA, "summary": SS.summary(state),
            "obligations": rows,
            "next": (due[0].get("obligation_id") if due else ""),
            "command": command}


def cmd_sol_schedule(args) -> int:
    """SOL 生命周期调度：显示（或执行）当前应做的认知窗口。"""
    path = _state_path(args)
    state = G.load_state(path)
    advice = G.sol_advice_summary(state)
    synced = G.sync_sol_schedule(state, advice=advice)
    if args.execute:
        # 显式执行：先落盘义务事实，再执行一个（有界），便于脚本按需推进。
        if synced["changes"]:
            G.save_state(path, state)
        due = SS.due_obligations(state)
        if not due:
            print("SOL 调度：当前没有 recommended 级待执行义务。")
        else:
            _execute_obligation(path, due[0])
        state = G.load_state(path)
    view = _sol_schedule_view(state, path)
    if args.json:
        _print({**view, "advice": advice})
        return 0
    summary = view["summary"]
    print(f"SOL 调度：义务 {summary['total']} 个"
          f"（未闭合 {summary['pending']}，漏调 {summary['missed']}）")
    for row in view["obligations"]:
        mark = "漏调" if row["missed"] else row["status"]
        extra = ""
        if row.get("call_id"):
            extra = f" call={row['call_id']}"
        elif row.get("skip_reason"):
            extra = f"（{row['skip_reason']}）"
        elif row.get("failure"):
            extra = f"（失败：{row['failure']}）"
        print(f"- {row['obligation_id']} [{mark}] {row['family']}/{row['event']} "
              f"{row['mode']} {row['level']} stage={row['stage'] or '-'}{extra}")
    if view["command"]:
        print("下一条可执行：" + str(view["next"]))
        print("可直接执行：" + view["command"])
    else:
        print("当前没有待执行的 recommended 级义务。")
    print("说明：调度义务是「应调用」的事实记录；SOL 仍是 advisory，不构成门禁。")
    return 0


def _iter_task_states(root: str) -> list[tuple[str, dict]]:
    tasks_dir = os.path.join(root, ".copilot-tasks")
    found: list[tuple[str, dict]] = []
    if not os.path.isdir(tasks_dir):
        return found
    for name in sorted(os.listdir(tasks_dir)):
        candidate = os.path.join(tasks_dir, name, "state.json")
        if not os.path.isfile(candidate):
            continue
        try:
            found.append((candidate, G.load_state(candidate)))
        except G.GovernanceError:
            continue
    return found


def cmd_sol_stats(args) -> int:
    """SOL 调度覆盖率（显式分母）：建议/执行/复用/跳过/失败/漏调分开统计。"""
    if args.workspace:
        root = os.path.abspath(args.workspace)
        pairs = _iter_task_states(root)
        if not pairs:
            raise CLIError(f"{root} 下没有找到可读的任务状态（.copilot-tasks/*/state.json）。")
        scoped = [state for _path, state in pairs]
        scope = f"workspace:{root}"
    else:
        path = _state_path(args)
        scoped = [G.load_state(path)]
        scope = f"state:{os.path.abspath(path)}"
    result = SS.stats(scoped, scope=scope)
    _print(result)
    return 0


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
    # 收口顺序固定为：最终工程验证（G1–G3）→ SOL 收口综合判断 → 收口。
    # SOL 没有 PASS 批准权，也不阻断收口：advise/off 都会照常收口，未执行的
    # 收口窗口按窗口事实记为漏调并写入 closure.sol_schedule。
    state = G.load_state(path)
    cache: dict = {}
    gates = G.closure_check(state,
                            task_dir=os.path.dirname(os.path.abspath(path)),
                            cache=cache)
    if gates.get("passed"):
        # 收口前的统一准入：只有最终工程验证真的完成（工作项终态、条件核销、
        # 证据与必要测试齐备）才允许执行 E，也才允许直接收口。`--close` 因此
        # 不能绕过验证与 E 阶段调度。
        readiness = G.closure_readiness(state, cache=cache)
        if not readiness.get("ready"):
            print("最终工程验证尚未完成，不能收口：")
            for blocker in G.closure_blockers_text(readiness):
                print(f"- {blocker}")
            print("先补齐上述验证（测试执行结果、证据登记或条件核销），"
                  "再执行 closure --close；收口 CONSULT 会在验证完成后自动调度。")
            return 1
        _sol_schedule_step(path, args, gates_passed=True,
                           execute=(_sol_policy(args) == "auto"),
                           prefer_family="E", closure_ready=readiness,
                           cache=cache)
        # 顺序：最终验证 → 收口 CONSULT → ZCode 独立验收 → G1–G3。刚执行完 E
        # 就直接收口，等于让 `--close` 绕过 ZCode 对 SOL 结论的判断（收口后
        # 状态已 CLOSED，再登记会被拒绝）。这里停在等待验收，退出码与回填类
        # 命令一致（10 = 仍有必须继续的下一步）。
        pending_acceptance = G.closure_acceptance_pending(G.load_state(path))
        if pending_acceptance:
            print("SOL 收口判断已完成，等待 ZCode 独立验收后再收口：")
            print(f"- 调用：{pending_acceptance['call_id']}"
                  f"（stage=closure，最终验证摘要 "
                  f"{pending_acceptance['digest']}）")
            print("下一步：copilot_dispatch.py sol-accept --state <state> "
                  f"--call-id {pending_acceptance['call_id']} "
                  "--status accepted|modified|rejected --rationale <依据>")
            print("登记判断后再执行 closure --close：此时才执行 G1–G3 收口。")
            return EXIT_CONTINUE
    closure = G.close_task(path, by=args.by, cache=cache,
                           sol_entry="closure-command")
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
    return _print_continuation(_state_path(args), args)


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
                              test_policy=_load_test_policy(args),
                              task_kind=args.task_kind or "",
                              triggers=args.trigger or [],
                              authorized_by=args.authorized_by or "",
                              authorization_note=args.authorization_note or "")
    print(f"合同已修订：criteria_sha256={result['criteria_sha256'][:16]}…")
    for criterion in result["criteria"]:
        print(f"- {criterion['criterion_id']} [{criterion['status']}] "
              f"{criterion['description']}")
    return _print_continuation(path, args)


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
        description="Copilot 本地任务治理（SOL 调度默认 auto：每条命令最多 1 次建议性调用）")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    def _add_sol_policy(parser) -> None:
        parser.add_argument(
            "--sol", default="", choices=("", *SOL_POLICIES),
            help="SOL 生命周期调度策略：auto（默认，有界执行 1 个 recommended 义务）/ "
                 "advise（只给出可执行命令）/ off（只记录义务，不调用）")

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
                   help="low/medium/high 决定认知复杂度：high 在启动/规划窗口给出"
                        "CONSULT 准入建议（advisory）；REVIEW 只在用户明确要求时执行")
    p.add_argument("--backend", default="legacy", choices=G.BACKENDS)
    p.add_argument("--task-id", default="")
    p.add_argument("--task-kind", default="",
                   choices=sorted(RP.TASK_KINDS),
                   help="任务类型；省略按 standard（非简单，默认进入 SOL 认知流程）；"
                        "trivial/mechanical/docs/format 明确声明才跳过")
    p.add_argument("--trigger", action="append", default=[],
                   help="声明触发因素（可多次）：new-evidence/new-risk/scope-changed 等")
    p.add_argument("--mode", default="")
    p.add_argument("--note", default="")
    p.add_argument("--workspace", default="",
                   help="任务实际 git 工作区；省略时取状态文件所在目录的 git 根")
    p.set_defaults(func=cmd_start)
    _add_sol_policy(p)

    p = sub.add_parser("plan", help="登记 ZCode 本地计划（JSON）")
    p.add_argument("--state", required=True)
    p.add_argument("--plan", default="", help="计划 JSON 路径；默认读 stdin")
    p.add_argument("--source", default="zcode")
    p.set_defaults(func=cmd_plan)
    _add_sol_policy(p)

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
    _add_sol_policy(p)

    p = sub.add_parser("rerun", help="把已终结工作项重开为 pending（修复后重新验证）")
    p.add_argument("--state", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_rerun)
    _add_sol_policy(p)

    p = sub.add_parser("criterion", help="核销完成条件")
    p.add_argument("--state", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--status", required=True, choices=G.CRITERION_STATUSES)
    p.add_argument("--evidence", action="append", default=[])
    p.set_defaults(func=cmd_criterion)
    _add_sol_policy(p)

    p = sub.add_parser("judgment", help="记录 ZCode 自我判断（仅审计）")
    p.add_argument("--state", required=True)
    p.add_argument("--status", required=True, choices=G.VERDICTS)
    p.add_argument("--summary", required=True)
    p.add_argument("--evidence", action="append", default=[])
    p.set_defaults(func=cmd_judgment)
    _add_sol_policy(p)

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
    _add_sol_policy(p)

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
    _add_sol_policy(p)

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
    _add_sol_policy(p)

    p = sub.add_parser("quality", help="查看质量结论与 PWW 接受状态")
    p.add_argument("--state", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_quality)

    p = sub.add_parser("decide", help="记录用户/任务决策")
    p.add_argument("--state", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--source", default="user")
    p.set_defaults(func=cmd_decide)
    _add_sol_policy(p)

    p = sub.add_parser("block", help="记录真实阻塞（缺少授权/决策/资源/外部限制）")
    p.add_argument("--state", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--needs", required=True)
    p.add_argument("--work-item", action="append", default=[],
                   help="只阻塞指定工作项（可重复）；授权缺口默认用它")
    p.add_argument("--criterion", action="append", default=[],
                   help="同时卡住的完成条件（可重复）；避免该条件被反复派生")
    p.add_argument("--scope", choices=["task"], default="",
                   help="任务级阻塞；仅当目标/资源整体不可推进时使用")
    p.add_argument("--force", action="store_true",
                   help="仍存在可执行工作项时强制登记任务级阻塞（需先确认整体不可推进）")
    p.set_defaults(func=cmd_block)
    _add_sol_policy(p)

    p = sub.add_parser("unblock", help="解除阻塞")
    p.add_argument("--state", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--block-id", default="",
                   help="存在多条未解除阻塞时必填，只解除指定 block")
    p.add_argument("--by", choices=["user", "zcode", "agent"], default="zcode",
                   help="解除方；只有用户授权解除时用 user，并须给出 --quote")
    p.add_argument("--quote", default="",
                   help="用户授权解除的原话（--by user 时必填）")
    p.set_defaults(func=cmd_unblock)
    _add_sol_policy(p)

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
                       help="SOL 主动介入建议（只读；不是门禁、不写状态）")
    p.add_argument("--state", required=True)
    p.add_argument("--trigger", action="append", default=[],
                   help="实质风险触发因素（如 security/core-logic/data-integrity）")
    p.add_argument("--reason", default="", help="准入原因")
    p.add_argument("--user-request", action="store_true",
                   help="用户明确要求 SOL 判断：建议一次 CONSULT 调用")
    p.add_argument("--review-request", action="store_true",
                   help="用户明确要求独立复核：建议一次 REVIEW（不由阶段或风险自动派生）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_review_admission)

    p = sub.add_parser(
        "sol-review",
        help="SOL 零工具单轮独立复核（用户明确要求时）：一次调用、不自动重试")
    p.add_argument("--state", required=True)
    p.add_argument("--materials", default="",
                   help="主代理整理的审查材料文件（- 表示 stdin）；--list 时可省略")
    p.add_argument("--knowledge-file", default="",
                   help="知识交接 JSON（由 knowledge_handoff.py 校验）；摘录追加到 materials")
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
    # 与认知命令同一把锁、同一等待口径：显式调用默认一直等待，调度路径带上限。
    p.add_argument("--lock-timeout", type=float, default=None,
                   help="等待任务 SOL 锁的秒数；省略则一直等待（手动调用）。"
                        "调度路径固定带上限（默认 "
                        f"{SS.AUTO_LOCK_WAIT_S:g}s，COPILOT_SOL_LOCK_TIMEOUT 覆盖），"
                        "拿不到锁时义务保持待执行、不付费")
    p.add_argument("--timeout", type=float, default=SR.DEFAULT_TIMEOUT,
                   help="单次请求超时秒数（不重试）")
    p.add_argument("--max-output-tokens", type=int,
                   default=SR.DEFAULT_MAX_OUTPUT_TOKENS)
    p.add_argument("--audit-log", default="",
                   help="审计日志路径；默认任务目录 sol-audit.jsonl（三模式共用）")
    p.add_argument("--output-dir", default="",
                   help="原始输出目录；默认任务目录 sol-reviews/")
    p.set_defaults(func=cmd_sol_review)

    def _add_sol_cognitive_args(parser, *, subdir: str) -> None:
        parser.add_argument("--state", required=True)
        parser.add_argument("--materials", default="",
                            help="主代理整理的材料文件（- 表示 stdin）；--list 时可省略；"
                                 "按 --obligation 调度执行时省略则从状态自动生成")
        parser.add_argument("--obligation", dest="obligation_id", default="",
                            help="按调度义务执行（sol-schedule 给出编号）：材料/问题/"
                                 "阶段缺省时从义务补齐，并把结果结算到该义务")
        parser.add_argument("--lock-timeout", type=float, default=None,
                            help="等待任务 SOL 锁的秒数；省略则一直等待（手动调用）。"
                                 "生命周期调度的自动路径固定带上限"
                                 f"（默认 {SS.AUTO_LOCK_WAIT_S:g}s，"
                                 "COPILOT_SOL_LOCK_TIMEOUT 覆盖），拿不到锁时"
                                 "义务保持待执行、不付费")
        parser.add_argument("--knowledge-file", default="",
                            help="知识交接 JSON（由 knowledge_handoff.py 校验）；"
                                 "摘录追加到 materials")
        parser.add_argument("--question", default="",
                            help="本次真正需要 SOL 判断的事项（必填；越具体越好）")
        parser.add_argument("--stage", default="",
                            choices=("", *SR.SOL_STAGES),
                            help="生命周期阶段：understanding/planning/development/"
                                 "stage/closure；省略按模式默认")
        parser.add_argument("--reason", default="", help="再次调用的具体原因")
        parser.add_argument("--trigger", default="",
                            choices=("", *sorted(SR.RETRY_TRIGGERS)),
                            help="再次调用的实质原因类型")
        parser.add_argument("--force", action="store_true",
                            help="已有同模式结论且材料不同时，按 --trigger/--reason "
                                 "再调用一次（声明决策价值，不是次数上限）")
        parser.add_argument("--dry-run", action="store_true",
                            help="只构建并校验请求（零工具/材料预算/推理强度），"
                                 "不发请求、不写状态")
        parser.add_argument("--list", action="store_true", help="只读列出记录")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--timeout", type=float, default=SR.DEFAULT_TIMEOUT,
                            help="单次请求超时秒数（不重试）")
        parser.add_argument("--max-output-tokens", type=int,
                            default=SR.DEFAULT_MAX_OUTPUT_TOKENS)
        parser.add_argument("--audit-log", default="",
                            help="审计日志路径；默认任务目录 sol-audit.jsonl")
        parser.add_argument("--output-dir", default="",
                            help=f"原始输出目录；默认任务目录 {subdir}/")

    p = sub.add_parser(
        "sol-consult",
        help="SOL 默认认知咨询：目标理解、问题本质、方案取舍与风险；失败只降级")
    _add_sol_cognitive_args(p, subdir="sol-consult")
    p.set_defaults(func=cmd_sol_consult)

    p = sub.add_parser(
        "sol-plan",
        help="SOL 方案规划：调查后比较方案、优先级与验收关注点；失败只降级")
    _add_sol_cognitive_args(p, subdir="sol-plans")
    p.set_defaults(func=cmd_sol_plan)

    p = sub.add_parser(
        "sol-think",
        help="历史命令：v9 起等价于 sol-consult")
    _add_sol_cognitive_args(p, subdir="sol-consult")
    p.set_defaults(func=cmd_sol_think)

    p = sub.add_parser(
        "sol-accept",
        help="回填 SOL 建议的采纳结论（accepted/modified/rejected；事实记录）")
    p.add_argument("--state", required=True)
    p.add_argument("--call-id", required=True, help="SOL 记录的 call_id")
    p.add_argument("--status", required=True,
                   choices=("accepted", "modified", "rejected"))
    p.add_argument("--note", default="", help="实际采用范围或补充说明")
    p.add_argument("--reason", default="",
                   help="拒绝或修改的理由（--rationale 省略时作为理由）")
    p.add_argument("--decision", default="",
                   help="ZCode 实际采取的做法（最终决策，不是 SOL 原建议）")
    p.add_argument("--rationale", default="",
                   help="采纳/修改/拒绝的依据（省略时回落 --reason）")
    p.add_argument("--delta", default="",
                   help="modified 时相对原建议的差异")
    p.add_argument("--evidence-ref", action="append", default=[],
                   help="关联本任务已登记证据编号；可重复或逗号分隔（E1,E3）")
    p.add_argument("--source-ref", action="append", default=[],
                   help="报告章节/文件路径等来源定位（只记录，不做证据核验）")
    p.add_argument("--evidence-note", default="", help="证据补充说明")
    p.add_argument("--decided-by", default="zcode")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sol_accept)

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
    _add_sol_policy(p)

    p = sub.add_parser("closure", help="收口任务（必须 G1–G3 通过）")
    p.add_argument("--state", required=True)
    p.add_argument("--close", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--by", default="zcode-native")
    p.set_defaults(func=cmd_closure)
    _add_sol_policy(p)

    p = sub.add_parser("reopen", help="用户授权后重开已收口任务")
    p.add_argument("--state", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--authorized-by", required=True,
                   help="必须显式写 user：重开需要用户授权")
    p.set_defaults(func=cmd_reopen)
    _add_sol_policy(p)

    p = sub.add_parser("sol-schedule",
                       help="SOL 生命周期调度：当前义务、漏调与可直接执行的命令")
    p.add_argument("--state", default="",
                   help="任务状态文件（与 --workspace 二选一）")
    p.add_argument("--execute", action="store_true",
                   help="执行一个 recommended 义务（一次请求；默认只显示）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sol_schedule)

    p = sub.add_parser("sol-stats",
                       help="SOL 调度覆盖率：应调用/执行/复用/跳过/失败/漏调（显式分母）")
    p.add_argument("--state", default="", help="单个任务状态文件")
    p.add_argument("--workspace", default="",
                   help="工作区根目录：聚合 .copilot-tasks/*/state.json")
    p.add_argument("--json", action="store_true", help="输出 JSON（默认即为 JSON）")
    p.set_defaults(func=cmd_sol_stats)

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
    p.add_argument("--task-kind", default="", choices=("", *sorted(RP.TASK_KINDS)),
                   help="修订任务类型（影响 SOL 默认认知路由）")
    p.add_argument("--trigger", action="append", default=[],
                   help="追加剧情的触发因素（可多次）")
    p.add_argument("--authorized-by", default="",
                   help="破坏性修订（删除条件/放松测试要求）必须写 user；"
                        "该字段是调用方自报声明，不是可验证凭证")
    p.add_argument("--authorization-note", default="",
                   help="破坏性修订必填：用户原话或明确要求，写入合同历史供审计核对")
    p.set_defaults(func=cmd_contract)
    _add_sol_policy(p)

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
    # G4 native 复核已退役：默认标签只标注“由 ZCode 在用户要求下登记”，
    # 不再暗示存在原生 Reviewer 通道。
    p.add_argument("--reviewer", default="zcode")
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
        code = args.func(args) or 0
    except (G.GovernanceError, CLIError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except SR.SolError as exc:
        # SOL 前置校验（材料大小、本机路由、凭据）失败也必须显式退出，
        # 不能把 traceback 或未脱敏详情抛给调用方。
        print(f"ERROR: [{exc.code}] {exc.detail}", file=sys.stderr)
        return exc.exit_code
    # 会话绑定：只在实际推进成功（0/10）之后写。命令失败、参数错误或用户拒绝工具调用
    # 时状态没有变化，不应把任务算到本会话名下，否则 Stop 会强制续跑一个没人推进的任务。
    # 只读命令（含 --list/--show）不写绑定；绑定失败也不影响命令结果。
    if code in (0, EXIT_CONTINUE) and not _is_read_only(args):
        try:
            G.touch_session(str(getattr(args, "state", "") or ""))
        except Exception:  # noqa: BLE001 - 旁路绑定失败不能改变命令结论
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())

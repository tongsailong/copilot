#!/usr/bin/env python3
"""Copilot v6 任务治理 CLI（ZCode Autonomous Development Edition）。

Copilot 只做轻量协作治理：任务合同、工作项、证据、审计与本地四门。它**不调用
任何模型**，也不把任何远程判断当作前进条件。所有判定由 `governance.py` 在本地
机械计算；ZCode 原生 Agent 负责调查、计划、实施、测试与修复。

常用命令::

    copilot_dispatch.py start --state S --goal G --contract-file C.md
    copilot_dispatch.py plan --state S --plan P.json
    copilot_dispatch.py next --state S
    copilot_dispatch.py evidence --state S --add --id E1 --text "..."
    copilot_dispatch.py record --state S --id W1 --status done --result "..." --evidence E1
    copilot_dispatch.py criterion --state S --id C1 --status met --evidence E1
    copilot_dispatch.py closure-check --state S
    copilot_dispatch.py closure --state S --close
    copilot_dispatch.py legacy-status --state old.json --audit-log old.jsonl --ledger ledger.json

历史 v1/v2 SOL 状态与审计只能通过 `legacy-status` 只读查看，v6 不会驱动它们。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import governance as G  # noqa: E402

__version__ = "6.4.0"


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
    ev = G.evaluate(state)
    return G.render_status(state, ev)


# ── 命令实现 ───────────────────────────────────────────────────────────────
def cmd_start(args) -> int:
    contract = _read_text(args.contract_file) if args.contract_file else ""
    criteria = _load_criteria(args.criteria_file) if args.criteria_file else None
    state = G.start_state(
        _state_path(args), args.goal, contract=contract, criteria=criteria,
        risk=args.risk, execution_backend=args.backend,
        task_id=args.task_id or "", mode=args.mode or "", note=args.note or "",
        contract_source=args.contract_file or "")
    print(f"任务已创建：{state['task_id']}")
    print(f"状态文件：{os.path.abspath(_state_path(args))}")
    print(f"完成条件：{len(state['contract']['criteria'])} 条 "
          f"（{', '.join(c['criterion_id'] for c in state['contract']['criteria'])}）")
    print(f"风险：{state['risk']} · 执行后端：{state['execution_backend']}")
    return 0


def cmd_next(args) -> int:
    state = G.load_state(_state_path(args))
    ev = G.evaluate(state)
    if args.json:
        _print({"task_id": state.get("task_id"), **ev})
    else:
        print(G.render_status(state, ev))
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
    entry = G.add_report(_state_path(args), args.summary,
                         blocked=bool(args.blocked), reason=args.reason or "")
    print(f"报告已登记：sha256={entry['report_sha256'][:16]}…")
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
    print(f"任务 {state.get('task_id')} 四门检查："
          f"{'PASS' if check['passed'] else 'FAIL'}")
    for name in ("G1", "G2", "G3", "G4"):
        gate = check["gates"][name]
        print(f"- {name}: {'PASS' if gate.get('passed') else 'FAIL'}")
        for reason in gate.get("reasons") or []:
            print(f"    {reason.get('code')}: {reason.get('detail')}")
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


def cmd_closure(args) -> int:
    path = _state_path(args)
    if not args.close:
        return cmd_closure_check(args)
    closure = G.close_task(path, by=args.by)
    print(f"任务已收口：verdict={closure['verdict']} "
          f"iteration={closure['iteration_id']} by={closure['closed_by']}")
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
                              contract_source=args.contract_file or "")
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
        description="Copilot v6 本地任务治理（不调用任何模型）")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="创建任务合同与状态")
    p.add_argument("--state", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--contract-file", default="", help="合同 Markdown；`## 完成条件` 列表")
    p.add_argument("--criteria-file", default="", help="条件 JSON 数组或逐行文本")
    p.add_argument("--risk", default="normal", choices=G.RISKS,
                   help="high 需要 G4，只能配 --backend native")
    p.add_argument("--backend", default="legacy", choices=G.BACKENDS)
    p.add_argument("--task-id", default="")
    p.add_argument("--mode", default="")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("plan", help="登记 ZCode 本地计划（JSON）")
    p.add_argument("--state", required=True)
    p.add_argument("--plan", default="", help="计划 JSON 路径；默认读 stdin")
    p.add_argument("--source", default="zcode")
    p.set_defaults(func=cmd_plan)

    for name in ("next", "brief", "render", "status"):
        p = sub.add_parser(name, help="查看阶段、工作项、四门与下一步")
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

    p = sub.add_parser("report", help="登记执行报告")
    p.add_argument("--state", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--blocked", action="store_true")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_report)

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

    p = sub.add_parser("closure-check", help="检查 G1–G4")
    p.add_argument("--state", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_closure_check)

    p = sub.add_parser("review-metrics",
                       help="统计真实复核轮次/重复 finding/无效信封等效率指标")
    p.add_argument("--state", required=True)
    p.set_defaults(func=cmd_review_metrics)

    p = sub.add_parser("closure", help="收口任务（必须四门通过）")
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


if __name__ == "__main__":
    sys.exit(main())

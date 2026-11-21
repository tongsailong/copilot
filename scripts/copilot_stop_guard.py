#!/usr/bin/env python3
"""Copilot Stop 续跑守卫：把本地续跑判定接到 ZCode 的 Stop 钩子上。

ZCode 的 Stop 钩子返回退出码 2 会让宿主继续当前回合（单回合最多自动续跑 3 次）。
Copilot 的 `EXIT_CONTINUE=10` 只是命令退出码，模型可以忽略它并直接结束回合；
本守卫补上这一层机械约束：Stop 时读取工作区 `.copilot-tasks/*/state.json`，
只要仍有 `continuation.required=true` 的任务，就以退出码 2 要求继续。

绑定关系（避免“该拦不拦”与“永久误拦”两种相反风险）：

* 工作区：从钩子事件里的 `cwd`（回退 `ZCODE_PROJECT_DIR`）向上有限层找
  `.copilot-tasks`，只判定该工作区的任务；其它工作区的任务不参与。
* 会话：Copilot CLI 每次处理任务时把当前 `ZCODE_SESSION_ID` 记到任务目录的
  `session.json`；守卫只对“本会话真正处理过”的任务生效。上一会话遗留的旧任务
  不会被本会话拦下——不是靠时间过期，而是靠会话归属。旧任务仍可在 `status`
  中看到；要继续它，先在本会话跑一次 `drive` 或其它会改写状态的命令建立绑定
  （`status` 等只读命令不写绑定）。
* 显式放行：`COPILOT_STOP_GUARD=0`、工作区 `.copilot-tasks/.stop-guard-off`、
  任务目录 `.stop-guard-off`、任务 state 里 `stop_guard=false`，以及
  `disable` 子命令（用户明确要求停止时由主代理执行）。

异常（state 损坏、绑定文件损坏、任务目录不可读、钩子输入非法）不做静默放行，
也不伪造 PASS：一律以退出码 2 给出可恢复的处理命令（修复或 `disable`）。

只读任务状态、不调用模型。退出码：0 放行，2 要求续跑。

恢复注入（SessionStart）区分两类事件：`source=resume|compact` 给绑定本会话且仍有可执行
工作的任务一段只读快照（阶段、未闭合条件、可粘贴的续接命令）；`source=startup` 或会话
尚未绑定任何任务时只给“存在未闭合任务”的只读指针（含阻塞原因/所需条件），不注入任务正文、
不绑定、不写状态、不消费 Stop 次数。任何异常一律退出 0 且不注入，不阻断会话启动。

用法：
  copilot_stop_guard.py                     # Stop 钩子模式（stdin 读事件 JSON）
  copilot_stop_guard.py session-start       # SessionStart 恢复注入（stdin 读事件 JSON）
  copilot_stop_guard.py status [--workspace DIR] [--session ID]
  copilot_stop_guard.py config [--workspace DIR]
  copilot_stop_guard.py install --workspace DIR [--apply]
  copilot_stop_guard.py install --user [--apply]        # 用户级 ~/.zcode/cli/config.json
  copilot_stop_guard.py install --config PATH [--apply]
  copilot_stop_guard.py uninstall (--workspace DIR | --user | --config PATH) [--apply]
  copilot_stop_guard.py disable (--state PATH | --task-dir DIR | --workspace DIR) --reason TEXT
  copilot_stop_guard.py enable  (--state PATH | --task-dir DIR | --workspace DIR)

工作区级 Stop 钩子需要宿主授予 workspaceHookAdmission；未获授权时钩子不会触发，
此时应用 `--user` 装到用户级配置（对全部工作区生效，用户级配置不需要该授权）。
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import governance as G  # noqa: E402

TASKS_DIR = ".copilot-tasks"
OFF_MARKER = ".stop-guard-off"
SESSION_FILE = "session.json"
USER_CONFIG_ENV = "ZCODE_USER_CONFIG"
TRUTHY_OFF = {"0", "off", "false", "no", "disabled"}
SESSION_ENV_KEYS = ("ZCODE_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")
# 宿主单回合最多自动续跑 3 次（zcode.cjs: xmc=3，硬编码、不可配置）；守卫拿不到
# 已用/剩余次数，只在注入文本里陈述上限，不推测剩余次数，也不持久化计数器。
MAX_CONTINUATIONS = 3
# 宿主把 stderr 截断到 4000 字符作为继续原因。
REASON_LIMIT = 3900
# SessionStart 只在这些事件注入恢复快照：resume/compact 是上下文真正丢失的时点。
# startup 不推断旧任务的归属（避免把无关会话引向旧任务），clear 尊重用户的清空动作。
SESSION_START_SOURCES = ("resume", "compact")
# startup 也必须在 matcher 内：宿主在新会话启动时只发 source=startup，
# 排除它会让“全新会话发现未闭合任务”这条路径根本触发不到钩子。
SESSION_START_MATCHER = "startup|resume|compact"
# 新会话（startup）与换了会话标识的 resume/compact 只拿到“有未闭合任务”的指针，
# 不注入任务正文、不绑定、不强制续跑；由用户/主代理决定是否接管。
POINTER_SOURCES = ("startup", "resume", "compact")
SESSION_START_LIMIT = 1600
POINTER_LIMIT = 1200
CONTEXT_HINT_MAX = 720
SESSION_START_MAX_TASKS = 2
SESSION_START_OPEN = "<copilot-session-recovery>"
SESSION_START_CLOSE = "</copilot-session-recovery>"
POINTER_OPEN = "<copilot-task-pointer>"
POINTER_CLOSE = "</copilot-task-pointer>"
# UserPromptSubmit：用户每回合都能带进新授权/决策/条件，因此逐回合重新提示
# 未闭合任务（含 BLOCKED 的原因与需要）。只在有未闭合任务时注入，且不把用户
# 输入或快照当作用户授权。
USER_PROMPT_EVENT = "UserPromptSubmit"
USER_PROMPT_LIMIT = 1800
USER_PROMPT_MAX_TASKS = 3


def _env_off() -> bool:
    return str(os.environ.get("COPILOT_STOP_GUARD", "")).strip().lower() in TRUTHY_OFF


def _read_payload() -> tuple[dict, str]:
    """返回 (payload, error)；stdin 为空表示手工调用，不是异常。"""
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    raw = (raw or "").strip()
    if not raw:
        return {}, ""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        return {}, f"钩子输入不是合法 JSON（{type(exc).__name__}）"
    if not isinstance(data, dict):
        return {}, "钩子输入不是 JSON 对象"
    return data, ""


def _session_from(payload: dict) -> str:
    for key in ("session_id", "sessionId"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    for key in SESSION_ENV_KEYS:
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def _cwd_from_payload(payload: dict) -> str:
    for key in ("cwd", "workspace", "project_dir"):
        value = str(payload.get(key) or "").strip()
        if value and os.path.isdir(value):
            return value
    for key in ("ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR"):
        value = str(os.environ.get(key) or "").strip()
        if value and os.path.isdir(value):
            return value
    return os.getcwd()


def find_tasks_root(start: str, *, max_up: int = 4) -> str:
    """从 cwd 向上有限层查找 `.copilot-tasks`；不扫全盘，也不越过 Git 根。

    Git 根是项目边界：再往上找到的 `.copilot-tasks` 属于别的项目（例如共享父目录
    里的任务集）。把它们算进当前工作区会让 Stop 误拦、SessionStart 误注入。
    """
    current = os.path.abspath(start)
    seen = 0
    while current and seen <= max_up:
        if os.path.isdir(os.path.join(current, TASKS_DIR)):
            return current
        if os.path.isdir(os.path.join(current, ".git")):
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
        seen += 1
    return ""


def _parse_time(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _task_disabled(state: dict) -> bool:
    value = state.get("stop_guard")
    if isinstance(value, dict):
        if value.get("off") is True:
            return True
        value = value.get("enabled", True)
    return value is False


def _recorded_sessions(task_dir: str) -> tuple[set[str], str]:
    """读取任务目录的会话绑定；返回 (session id 集合, 错误描述)。"""
    path = os.path.join(task_dir, SESSION_FILE)
    if not os.path.isfile(path):
        return set(), ""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return set(), f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return set(), "session.json 不是 JSON 对象"
    ids: set[str] = set()
    raw = data.get("sessions")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                sid = str(item.get("session_id") or "").strip()
            else:
                sid = str(item or "").strip()
            if sid:
                ids.add(sid)
    single = str(data.get("session_id") or "").strip()
    if single:
        ids.add(single)
    return ids, ""


def _unmet_criteria(state: dict) -> list[str]:
    """未闭合完成条件（只读，用于恢复快照；不改写条件状态）。"""
    criteria = (state.get("contract") or {}).get("criteria") or []
    return [str(item.get("criterion_id") or "") for item in criteria
            if isinstance(item, dict)
            and str(item.get("status") or "") != "met"
            and str(item.get("criterion_id") or "")]


def _open_blocks(state: dict) -> list[dict]:
    """未解除阻塞（只读）：恢复/提示时给出原因与所需条件，便于用户补齐授权。"""
    out: list[dict] = []
    for block in state.get("blocks") or []:
        if not isinstance(block, dict) or block.get("resolved_at"):
            continue
        out.append({
            "id": str(block.get("id") or block.get("block_id") or ""),
            "reason": str(block.get("reason") or ""),
            "needs": str(block.get("needs") or block.get("need") or ""),
            "work_items": [str(item) for item in block.get("work_items") or []
                           if str(item or "").strip()],
            "criteria": [str(item) for item in block.get("criteria") or []
                         if str(item or "").strip()],
            "scope": str(block.get("scope") or ""),
        })
    return out


def collect(root: str, *, session: str = "",
            include_open: bool = False) -> tuple[list[dict], list[dict]]:
    """返回 (要求续跑的任务, 异常)。

    任务是否“要求续跑”只由 Copilot 的本地判定决定；这里的会话绑定只决定
    本会话是否为该任务的负责方。未绑定（旧任务）与其它会话的任务不拦截。

    `include_open=True` 额外纳入“没有续跑动作但仍有未解除阻塞”的任务（BLOCKED），
    用于恢复提示与用户回合提示；不改变 Stop 的拦截口径。
    """
    rows: list[dict] = []
    anomalies: list[dict] = []
    base = os.path.join(root, TASKS_DIR)
    try:
        names = sorted(os.listdir(base))
    except OSError as exc:
        anomalies.append({"kind": "tasks-dir", "path": base,
                          "detail": f"{type(exc).__name__}: {exc}"})
        return rows, anomalies
    for name in names:
        task_dir = os.path.join(base, name)
        if not os.path.isdir(task_dir):
            continue
        path = os.path.join(task_dir, "state.json")
        if not os.path.isfile(path):
            continue
        if os.path.exists(os.path.join(task_dir, OFF_MARKER)):
            continue
        # 先判归属再读 state：会话模式下，不能归属本会话的任务根本不参与判定。
        # 否则其它会话/旧任务的 state 损坏会被当成本工作区的异常，误拦所有会话；
        # 同时省掉无关任务的 state 解析与合同校验开销（Stop 每次都会跑）。
        ids, bind_error = _recorded_sessions(task_dir)
        if bind_error:
            # 绑定文件坏了，无法证明该任务属于谁：按本会话负责处理（fail-closed），
            # 同时把修复命令给出来，而不是悄悄跳过这条任务。
            binding = "corrupt"
        elif not ids:
            binding = "unbound"
        elif session and session in ids:
            binding = "bound"
        elif not session:
            binding = "unknown-session"
        else:
            binding = "foreign"
        if session and binding not in ("bound", "corrupt"):
            continue
        if bind_error:
            anomalies.append({"kind": "binding", "task_id": name,
                              "task_dir": task_dir, "state_path": path,
                              "binding": binding, "detail": bind_error})
        try:
            state = G.load_state(path)
        except (G.GovernanceError, OSError, ValueError, TypeError) as exc:
            anomalies.append({"kind": "state", "task_id": name,
                              "task_dir": task_dir, "state_path": path,
                              "binding": binding,
                              "detail": f"{type(exc).__name__}: {exc}"})
            continue
        if _task_disabled(state):
            continue
        try:
            cont = G.continuation(state)
        except Exception as exc:  # noqa: BLE001 - 任何异常都不能变成静默放行
            anomalies.append({"kind": "evaluate", "task_id": name,
                              "task_dir": task_dir, "state_path": path,
                              "binding": binding,
                              "detail": f"{type(exc).__name__}: {exc}"})
            continue
        if not cont.get("required"):
            # BLOCKED 只在显式要求提示时纳入：Stop 不能因“等用户授权”反复续跑。
            blocks = _open_blocks(state)
            if not include_open or not blocks:
                continue
        rows.append({
            "task_id": str(state.get("task_id") or name),
            "task_dir": task_dir,
            "state_path": path,
            "phase": cont.get("phase"),
            "action": cont.get("action"),
            "reason": cont.get("reason") or "",
            "executable": list(cont.get("executable") or []),
            "derivable": list(cont.get("derivable") or []),
            "blocked_items": list(cont.get("blocked_items") or []),
            "unattributed_criteria": list(cont.get("unattributed_criteria") or []),
            "unmet_criteria": _unmet_criteria(state),
            "blocks": _open_blocks(state),
            "next_command": cont.get("next_command") or "",
            "updated": str(state.get("updated") or ""),
            "binding": binding,
            "sessions": sorted(ids),
        })
    rows.sort(key=lambda row: _parse_time(row["updated"]), reverse=True)
    return rows, anomalies


def _binding_note(row: dict) -> str:
    if row["binding"] == "bound":
        return "本会话"
    if row["binding"] == "corrupt":
        return "绑定文件损坏，无法核对归属（fail-closed 拦截）；修好或 disable 后重试"
    if row["binding"] == "unknown-session":
        return "会话标识不可用，按最近更新判定"
    return "未绑定"


def _resume_command(row: dict) -> str:
    """可原样粘贴的续接命令：用满自动续跑后由用户在新回合执行。"""
    script = os.path.join(HERE, "copilot_auto.py")
    return (f"python3 -B {shlex.quote(script)} drive --state "
            f"{shlex.quote(row['state_path'])}")


def render(row: dict, *, total: int = 1, anomalies: list[dict] | None = None,
           continued: bool = False) -> str:
    lines = [
        f"[COPILOT 续跑守卫] 任务 {row['task_id']} 仍在 {row['phase']}："
        f"{row['reason'] or '仍有必须继续的工作'}",
        f"- 归属：{_binding_note(row)}",
        f"- 宿主限制：ZCode 每回合最多接受 {MAX_CONTINUATIONS} 次自动续跑（硬编码、"
        "不可配置）；守卫拿不到已用次数与剩余次数，因此不声明“还剩几次”，也不断言"
        "这是最后一次。",
    ]
    detail = []
    if row["executable"]:
        detail.append("可执行：" + "、".join(row["executable"]))
    if row["derivable"]:
        detail.append("可派生：" + "、".join(row["derivable"]))
    if detail:
        lines.append(f"- 下一步动作：{row['action']}（" + "；".join(detail) + "）")
    if row["unattributed_criteria"]:
        lines.append("- 未归属完成条件：" + "、".join(row["unattributed_criteria"])
                     + "；若确由已阻塞工作项承载，先用 block --work-item ... "
                       "--criterion ... 归属")
    for block in row.get("blocks") or []:
        scope = "、".join(block["work_items"] or block["criteria"]) or "任务级"
        lines.append(f"- 阻塞 {block['id'] or '（未编号）'}（{scope}）："
                     f"{block['reason'] or '原因未记录'}；需要："
                     f"{block['needs'] or '未写明'}")
    if row["next_command"]:
        lines.append("- 下一步命令：" + row["next_command"].replace(
            "<state>", row["state_path"]))
    lines.append("- 可原样粘贴的续接命令：" + _resume_command(row)
                 + "（自动续跑用满后由用户在新回合执行）")
    if total > 1:
        lines.append(f"- 本工作区还有 {total - 1} 个任务同时要求续跑；先处理最近更新的这个。")
    if anomalies:
        lines.append(f"- 另有 {len(anomalies)} 处状态异常；先按 fix 提示处理，"
                     "不得忽略，也不得伪造 PASS。")
    for item in (anomalies or [])[:1]:
        where = item.get("state_path") or item.get("path") or ""
        lines.append(f"- 异常（{item['kind']}）：{item.get('task_id') or ''} {item['detail']}".strip())
        if where:
            lines.append(f"  fix：检查/恢复 {where}；该任务已废弃则执行 "
                         f"disable --state {where} --reason \"<原因>\"")
    lines.append("- 现在就用工具执行，优先完成一个最小可验证工作单元（一个工作项或一条"
                 "完成条件）：领取 → 实施改动 → 跑定向测试 → 登记证据并 criterion met "
                 "→ 再跑 drive 重新求值。不要用只读核对、重复计划或长篇说明代替实际进展。")
    lines.append("- 每次续跑结束前必须落盘：进展与结论用 record/report 写入，证据先 "
                 "evidence --add 再引用，未完成工作项保留 pending/blocked 并写明原因，"
                 "使下一回合能凭状态文件与上面的续接命令继续。")
    lines.append("- 不得为结束回合把 PARTIAL 改写成 BLOCKED 或 PASS 来规避续跑上限："
                 "BLOCKED 只在真实外部阻塞（每条有 reason/needs）时登记，PASS 只在 G1–G3 "
                 "全部通过后由收口产生；改状态规避等同于伪造结论。")
    lines.append("- 阶段报告、计划、只给命令不执行、单个工具失败、子代理返回 PARTIAL "
                 "都不是终点；工具失败先分析原因并换一条合法路径，不要原样重试；"
                 "无进展按 AUTO-REPLAN 换路线，不得降低验收标准。")
    lines.append("- 新回合续接：先 status --json 核实阶段、已完成工作项与证据，只继续真正"
                 "未完成的部分，不重做已完成工作、不覆盖既有证据。")
    lines.append("- 只有 PASS 收口，或所有剩余工作真实 BLOCKED（每条都有 reason/needs）"
                 "才可停止；需要用户授权/决策时先 block 该工作项再 drive。")
    if continued:
        lines.append("- 本回合 Stop 已被强制续跑过至少一次；剩余次数未知，不要假设还有"
                     "机会，也不要假设这就是最后一次。请立即执行，不要重复输出计划。")
    lines.append("- 用户明确要求停止或紧急退出：先执行 disable（--state/--workspace 加 "
                 "--reason \"用户原话\"）再如实报告；守卫不应覆盖用户的明确决定。")
    text = "\n".join(lines)
    return text if len(text) <= REASON_LIMIT else text[:REASON_LIMIT - 20] + "\n...（已截断）"


def _anomaly_reason(anomalies: list[dict]) -> str:
    head = anomalies[0]
    where = head.get("state_path") or head.get("path") or ""
    lines = [
        "[COPILOT 续跑守卫] 无法确认任务是否还有可执行工作，本次不放行（fail-closed）：",
        f"- 异常（{head['kind']}）：{head.get('task_id') or ''} {head['detail']}".strip(),
    ]
    if where:
        lines.append(f"- fix：检查并恢复 {where}")
        lines.append(f"- 若该任务已废弃：disable --state {where} --reason \"<原因>\"")
    lines.append("- 若整个工作区都有问题：disable --workspace <工作区> --reason \"<原因>\"")
    lines.append(f"- 共 {len(anomalies)} 处异常；这不是任务完成，也不代表 PASS。")
    text = "\n".join(lines)
    return text if len(text) <= REASON_LIMIT else text[:REASON_LIMIT - 20] + "\n...（已截断）"


def _log(exit_code: int, root: str, task_id: str = "", note: str = "",
         session: str = "") -> None:
    """可选调试日志（COPILOT_STOP_GUARD_LOG）：记录每次判定，默认不写任何文件。"""
    path = str(os.environ.get("COPILOT_STOP_GUARD_LOG", "")).strip()
    if not path:
        return
    row = {
        "at": datetime.now(timezone.utc).isoformat(),
        "exit": exit_code,
        "root": root,
        "task_id": task_id,
        "session": session,
        "note": note,
    }
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _block(text: str) -> int:
    # 退出码 2：ZCode 取 stderr 作为继续原因；stdout 必须保持非 JSON 文本。
    sys.stderr.write(text + "\n")
    return 2


def cmd_check() -> int:
    """Stop 钩子入口：钩子契约只有 0/2，任何内部异常都按 fail-closed 返回 2。"""
    try:
        return _check()
    except Exception as exc:  # noqa: BLE001 - traceback + 退出码 1 会破坏 0/2 契约
        _log(2, "", note=f"internal-error:{type(exc).__name__}")
        return _block(
            "[COPILOT 续跑守卫] 内部异常，本次不放行（fail-closed）：\n"
            f"- {type(exc).__name__}: {exc}\n"
            "- fix：检查 .copilot-tasks 下的状态与绑定文件；确认无误后重试。\n"
            "- 临时退出：COPILOT_STOP_GUARD=0 或创建 .copilot-tasks/.stop-guard-off"
        )


def _check() -> int:
    if _env_off():
        _log(0, "", note="env-off")
        return 0
    payload, payload_error = _read_payload()
    if payload_error:
        _log(2, "", note="payload-error")
        return _block(
            "[COPILOT 续跑守卫] 钩子输入异常，无法判定任务状态，本次不放行：\n"
            f"- {payload_error}\n"
            "- fix：检查 Stop 钩子配置（命令必须是 copilot_stop_guard.py，"
            "且 stdin 由宿主提供）\n"
            "- 临时退出：COPILOT_STOP_GUARD=0 或创建 .copilot-tasks/.stop-guard-off"
        )
    session = _session_from(payload)
    continued = bool(payload.get("stop_hook_active"))
    start = _cwd_from_payload(payload)
    root = find_tasks_root(start)
    if not root:
        _log(0, start, note="no-tasks-dir", session=session)
        return 0
    base = os.path.join(root, TASKS_DIR)
    if os.path.exists(os.path.join(base, OFF_MARKER)):
        _log(0, root, note="marker-off", session=session)
        return 0
    rows, anomalies = collect(root, session=session)

    if session:
        enforced = [row for row in rows if row["binding"] in ("bound", "corrupt")]
        if not enforced:
            # 没有本会话负责的续跑任务：不拦截。其它会话/旧任务的责任不转嫁给
            # 当前会话，否则旧任务会永久堵住工作区。
            if anomalies:
                _log(2, root, note="anomaly", session=session)
                return _block(_anomaly_reason(anomalies))
            _log(0, root, note=f"out-of-scope:{len(rows)}", session=session)
            return 0
    else:
        # 手工调用或宿主未提供会话标识：无法判定归属，只对最近更新的任务生效，
        # 避免把工作区里所有历史任务一次性当成当前任务。
        enforced = rows[:1]
        if not enforced:
            if anomalies:
                _log(2, root, note="anomaly", session="")
                return _block(_anomaly_reason(anomalies))
            _log(0, root, note="no-continuation", session="")
            return 0
        _log(2, root, task_id=enforced[0]["task_id"], session="",
             note=f"{enforced[0]['phase']}/{enforced[0]['action']}"
                  f"/{enforced[0]['binding']}")
        return _block(render(enforced[0], total=len(rows), anomalies=anomalies,
                             continued=continued))
    _log(2, root, task_id=enforced[0]["task_id"], session=session,
         note=f"{enforced[0]['phase']}/{enforced[0]['action']}"
              f"/{enforced[0]['binding']}")
    return _block(render(enforced[0], total=len(enforced), anomalies=anomalies,
                         continued=continued))


def _project_context(root: str, row: dict) -> list[str]:
    """恢复快照里的项目现场片段：只读、确定、可失败省略。

    原生 Memory 每轮注入只发生在主会话启动时；compaction/resume 之后会话容易只剩
    任务状态而丢失“这是哪个项目、基线是什么、记忆在哪”。这里用 Context Engine 的
    只读定位补齐这三点，并给出召回/交接命令。任何异常都返回空列表：恢复注入的
    可用性优先于附加信息，不得因为缺少上下文模块而阻断会话启动。
    """
    try:
        import context_engine as CE  # 同目录模块，惰性导入
    except Exception:  # noqa: BLE001 - 可选能力
        return []
    try:
        # SessionStart 钩子有 10s 超时：单次 git 调用收紧到 1s，最坏 4s 仍留足余量。
        info = CE.resolve(root, cwd=root, git_timeout=1.0)
    except Exception:  # noqa: BLE001 - 可选能力
        return []
    lines: list[str] = []
    git = info.get("git") or {}
    if git.get("available"):
        head = (git.get("head") or "")[:12] or "未知"
        dirty, untracked = git.get("dirty"), git.get("untracked")
        if dirty is None or untracked is None:
            state = "未知"  # git status 失败不是 clean
        else:
            state = "clean" if not dirty and not untracked else f"已修改 {dirty} · 未跟踪 {untracked}"
        lines.append(f"- 项目现场：{info['project']['root']} · "
                     f"{git.get('branch') or '未知'}@{head} · 工作区 {state}")
    entries = [m for m in info.get("memory") or [] if m.get("role") == "root"]
    memory = next((m for m in entries if m.get("stable")), {})
    if memory.get("status") == "available":
        mem_text = (f"available（索引 {memory.get('index_lines') or 0} 行 / "
                    f"{memory.get('index_pointers') or 0} 条指针）")
    elif memory.get("status"):
        mem_text = f"{memory.get('status')}（不是零命中）"
    elif any(m.get("status") == "unreadable" for m in entries):
        mem_text = "unreadable（不是 absent）"
    else:
        mem_text = "absent（不是零命中）"
    vault_text = (info.get("vault") or {}).get("status") or "未知"
    lines.append(f"- 原生 Memory：{mem_text} · Vault：{vault_text}")
    engine = os.path.join(HERE, "context_engine.py")
    state_path = shlex.quote(row["state_path"])
    lines.append("- 历史召回（确定性）：python3 -B " + shlex.quote(engine)
                 + f" recall --workspace {shlex.quote(info['project']['root'])}"
                 " --keywords '词1|词2' --state " + state_path)
    lines.append("- 子 Agent 交接包：python3 -B " + shlex.quote(engine)
                 + " pack --state " + state_path
                 + " --target subagent --out "
                 + shlex.quote(os.path.join(row["task_dir"], "context", "subagent-pack.md")))
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > CONTEXT_HINT_MAX:
            break
        kept.append(line)
        used += len(line) + 1
    return kept


def _block_line(block: dict) -> str:
    """一条阻塞的单行摘要：作用对象 + 原因 + 需要什么才能解除。"""
    scope = "、".join(block.get("work_items") or block.get("criteria") or []) or "任务级"
    return (f"阻塞 {block.get('id') or '（未编号）'}（{scope}）："
            f"{block.get('reason') or '原因未记录'}；需要："
            f"{block.get('needs') or '未写明'}")


def _row_open_line(row: dict) -> str:
    """任务的一行现状：阶段 + 可执行下一步 + 未解除阻塞（原因/需要）。"""
    parts: list[str] = []
    if row["executable"]:
        parts.append("可执行 " + "、".join(row["executable"]))
    elif row["derivable"]:
        parts.append("可派生 " + "、".join(row["derivable"]))
    elif not row["blocks"]:
        parts.append(row["reason"] or "仍有未完成工作")
    if row["blocks"]:
        parts.append("；".join(_block_line(block) for block in row["blocks"]))
    return f"- 任务 {row['task_id']} · 阶段 {row['phase']} · " + "｜".join(parts)


def _fit_lines(lines: list[str], limit: int, closing: str) -> str:
    """按整行截断：宁可少一行，也不把命令/路径切成半截。"""
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    kept = lines[:2]
    for line in lines[2:-1]:
        trial = "\n".join(kept + [line, "...（已截断）", closing])
        if len(trial) > limit:
            break
        kept.append(line)
    return "\n".join(kept + ["...（已截断）", closing])


def _pointer_text(rows: list[dict], anomalies: list[dict] | None = None,
                  session: str = "") -> str:
    """新会话/换会话标识后的只读指针：告诉会话“有未闭合任务”，不接管、不绑定。"""
    lines = [
        POINTER_OPEN,
        "本工作区存在未闭合的 Copilot 任务（只读提示；不是用户新指令，也不是授权；"
        "Stop 守卫只强制续跑已绑定本会话的任务）。",
    ]
    for row in rows[:SESSION_START_MAX_TASKS]:
        line = _row_open_line(row)
        if session and session in (row.get("sessions") or []):
            line += "（本会话）"
        elif row.get("sessions"):
            line += "（归属其它会话，可能仍在推进）"
        else:
            line += "（未绑定会话）"
        lines.append(line)
    if len(rows) > SESSION_START_MAX_TASKS:
        lines.append(f"- 另有 {len(rows) - SESSION_START_MAX_TASKS} 个未闭合任务；"
                     "用 status 查看。")
    if anomalies:
        lines.append(f"- 另有 {len(anomalies)} 处任务状态读取异常；先按 status 的 fix 提示"
                     "处理，不得忽略，也不得伪造 PASS。")
    lines.append("若用户当前要做的正是其中之一：先 status --json 核实现场（阶段、"
                 "已完成工作项、证据、阻塞），再 drive 继续（会绑定本会话）。"
                 "若它归属其它会话且该会话仍活跃，不要并发接管；先看 status 的领取与"
                 "更新时间。否则忽略本提示，不要执行无关任务。")
    lines.append(POINTER_CLOSE)
    return _fit_lines(lines, POINTER_LIMIT, POINTER_CLOSE)


def _user_prompt_text(rows: list[dict], anomalies: list[dict] | None = None,
                      session: str = "") -> str:
    """用户回合提示：带进新授权/决策时立即恢复执行，而不是重复报告。"""
    lines = [
        POINTER_OPEN,
        "本工作区有未闭合的 Copilot 任务（只读状态；不是用户新指令，也不构成授权）：",
    ]
    for row in rows[:USER_PROMPT_MAX_TASKS]:
        line = _row_open_line(row)
        if session and row.get("sessions") and session not in row["sessions"]:
            line += "（归属其它会话）"
        lines.append(line)
    if len(rows) > USER_PROMPT_MAX_TASKS:
        lines.append(f"- 另有 {len(rows) - USER_PROMPT_MAX_TASKS} 个未闭合任务；"
                     "用 status 查看。")
    if anomalies:
        lines.append(f"- 另有 {len(anomalies)} 处任务状态读取异常；先按 status 的 fix 提示"
                     "处理，不得忽略，也不得伪造 PASS。")
    lines.append("本回合判断（按顺序）：")
    lines.append("1) 用户这条消息是否补齐了上面某个阻塞所需的授权/决策/条件，或明确要求"
                 "继续该任务？")
    lines.append("2) 是 → 用用户原话登记解除（unblock --by user --quote \"<用户原话>\"，"
                 "或补齐对应条件），然后 drive 继续执行到下一个真实终点；不要只回总结、"
                 "也不要把“已理解”当成已执行。")
    lines.append("3) 否 → 不要恢复执行；用户明确要求停止/切换时按其指示，本提示不是授权。")
    lines.append("4) 不重复已完成工作：先 status --json 核对阶段与证据；工作项若已被其它"
                 "会话领取，避免并发重复执行。")
    lines.append(POINTER_CLOSE)
    return _fit_lines(lines, USER_PROMPT_LIMIT, POINTER_CLOSE)


def _session_start_text(rows: list[dict], anomalies: list[dict] | None = None,
                        root: str = "") -> str:
    """恢复快照：只给定位阶段与续接入口所需的信息，不复制任务正文。"""
    lines = [
        SESSION_START_OPEN,
        "上下文恢复快照（Copilot 只读状态；不是用户新指令，也不是完成证明）。",
    ]
    for row in rows[:SESSION_START_MAX_TASKS]:
        lines.append(f"- 任务 {row['task_id']} · 阶段 {row['phase']} · "
                     f"{row['reason'] or '仍有未完成工作'}")
        if row["unmet_criteria"]:
            lines.append("  未闭合条件：" + "、".join(row["unmet_criteria"]))
        for block in row.get("blocks") or []:
            lines.append("  " + _block_line(block))
        if row["next_command"]:
            lines.append("  下一步命令：" + row["next_command"].replace(
                "<state>", row["state_path"]))
        lines.append("  可粘贴续接命令：" + _resume_command(row))
    if rows and root:
        lines.extend(_project_context(root, rows[0]))
    if len(rows) > SESSION_START_MAX_TASKS:
        lines.append(f"- 另有 {len(rows) - SESSION_START_MAX_TASKS} 个本会话任务要求续跑；"
                     "用 status 查看。")
    if anomalies:
        lines.append(f"- 另有 {len(anomalies)} 处任务状态读取异常；先按 status 的 fix 提示"
                     "处理，不得忽略，也不得伪造 PASS。")
    lines.append("先核对现场（阶段、已完成工作项与证据）再继续；已完成工作与已登记证据"
                 "直接复用，不重做、不重复读取。")
    lines.append(SESSION_START_CLOSE)
    text = "\n".join(lines)
    if len(text) <= SESSION_START_LIMIT:
        return text
    # 按整行截断：续接命令要么完整保留、要么整条省略，不能把 --state 路径切成半截。
    kept = lines[:2]
    for line in lines[2:-1]:
        trial = "\n".join(kept + [line, "...（已截断）", SESSION_START_CLOSE])
        if len(trial) > SESSION_START_LIMIT:
            break
        kept.append(line)
    return "\n".join(kept + ["...（已截断）", SESSION_START_CLOSE])


def cmd_session_start() -> int:
    """SessionStart 钩子模式：只读恢复快照；任何异常都放行且不注入。

    resume/compact 且任务绑定本会话：注入完整恢复快照。
    startup，或没有任何任务绑定本会话：只注入“存在未闭合任务”的只读指针，
    不绑定、不接管、不强制续跑（换会话标识后的重启也能重新发现现场）。
    """
    try:
        if _env_off():
            _log(0, "", note="session-start-env-off")
            return 0
        payload, payload_error = _read_payload()
        if payload_error:
            _log(0, "", note="session-start-payload-error")
            return 0
        source = str(payload.get("source") or payload.get("hook_source") or "").strip().lower()
        if source not in POINTER_SOURCES:
            _log(0, "", note=f"session-start-skip:{source or 'unknown'}")
            return 0
        session = _session_from(payload)
        if not session:
            # 无法判定归属时不注入：不把旧任务引向无关会话，也不自动绑定。
            _log(0, "", note="session-start-no-session")
            return 0
        start = _cwd_from_payload(payload)
        root = find_tasks_root(start)
        if not root:
            _log(0, start, note="session-start-no-tasks-dir", session=session)
            return 0
        if os.path.exists(os.path.join(root, TASKS_DIR, OFF_MARKER)):
            _log(0, root, note="session-start-marker-off", session=session)
            return 0
        rows, anomalies = collect(root, session=session)
        bound = [row for row in rows if row["binding"] == "bound"]
        # 只披露本会话任务目录的异常：其它任务的异常不属于本会话，不应诱导它越界处理。
        # 本会话任务自己的状态/绑定损坏时仍要提示修复——静默跳过会让会话以为没有
        # 待续跑任务，从而丢掉真正需要处理的那个任务。
        bound_anomalies = [item for item in anomalies
                           if item.get("binding") in ("bound", "corrupt")]
        if bound or bound_anomalies:
            if source not in SESSION_START_SOURCES:
                # startup 不重复注入：本回合已有会话级上下文，避免启动噪音。
                _log(0, root, note=f"session-start-startup-bound:{len(bound)}",
                     session=session)
                return 0
            text = _session_start_text(bound, bound_anomalies, root=root)
            _log(0, root, task_id=bound[0]["task_id"] if bound else "",
                 session=session, note=f"session-start-inject:{source}")
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": text,
            }}, ensure_ascii=False))
            return 0
        # 没有本会话任务：给只读指针（含 BLOCKED 的原因/需要），不自动绑定。
        # 归属其它会话的任务也列出并标注，否则重启换了会话标识就再也发现不了现场；
        # 是否接管由用户/主代理判断，Stop 仍只强制绑定本会话的任务。
        all_rows, all_anomalies = collect(root, session="", include_open=True)
        if not all_rows:
            _log(0, root, note="session-start-no-open-task", session=session)
            return 0
        text = _pointer_text(all_rows, all_anomalies, session=session)
        _log(0, root, task_id=all_rows[0]["task_id"], session=session,
             note=f"session-start-pointer:{source}:{len(all_rows)}")
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }}, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001 - 恢复注入失败不得阻断会话启动
        _log(0, "", note=f"session-start-error:{type(exc).__name__}")
        return 0


def cmd_user_prompt() -> int:
    """UserPromptSubmit 钩子模式：逐回合提示未闭合任务；任何异常都放行。"""
    try:
        if _env_off():
            _log(0, "", note="user-prompt-env-off")
            return 0
        payload, payload_error = _read_payload()
        if payload_error:
            _log(0, "", note="user-prompt-payload-error")
            return 0
        start = _cwd_from_payload(payload)
        root = find_tasks_root(start)
        if not root:
            _log(0, start, note="user-prompt-no-tasks-dir")
            return 0
        if os.path.exists(os.path.join(root, TASKS_DIR, OFF_MARKER)):
            _log(0, root, note="user-prompt-marker-off")
            return 0
        rows, anomalies = collect(root, session="", include_open=True)
        if not rows:
            _log(0, root, note="user-prompt-no-open-task")
            return 0
        text = _user_prompt_text(rows, anomalies, session=_session_from(payload))
        _log(0, root, task_id=rows[0]["task_id"], note=f"user-prompt-inject:{len(rows)}")
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": USER_PROMPT_EVENT,
            "additionalContext": text,
        }}, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001 - 提示注入失败不得阻断用户回合
        _log(0, "", note=f"user-prompt-error:{type(exc).__name__}")
        return 0


def cmd_status(args) -> int:
    start = args.workspace or os.getcwd()
    root = find_tasks_root(start)
    if not root:
        print(f"未找到 {TASKS_DIR}：{start}")
        return 0
    session = str(getattr(args, "session", "") or "") or _session_from({})
    rows, anomalies = collect(root, session=session)
    print(f"工作区：{root}")
    print(f"会话：{session or '（不可用；按最近更新任务判定）'}")
    if not rows:
        print("没有要求续跑的任务。")
    for row in rows:
        print(render(row, total=len(rows)))
        print(f"- 阶段：{row['phase']} · 绑定：{row['binding']} · 更新：{row['updated']}\n")
    if anomalies:
        print(_anomaly_reason(anomalies))
    return 0


def _runtime_script() -> str:
    return os.path.join(os.path.expanduser("~"), ".zcode", "skills", "copilot",
                        "scripts", "copilot_stop_guard.py")


def _print_runtime_hint() -> None:
    target = _runtime_script()
    if os.path.isfile(target):
        print(f"钩子目标：{target}")
    else:
        print(f"警告：运行时脚本不存在：{target}\n"
              "      先执行 scripts-sync/sync_runtime.py --apply 下发运行时，否则钩子会失败。")


def _hook_command(*, subcommand: str = "") -> str:
    suffix = f" {subcommand}" if subcommand else ""
    return ('python3 -B "$HOME/.zcode/skills/copilot/scripts/copilot_stop_guard.py"'
            + suffix)


def _hook_group(*, subcommand: str = "", matcher: str = "") -> dict:
    group = {"hooks": [{"type": "command",
                        "command": _hook_command(subcommand=subcommand),
                        "timeout": 10}]}
    if matcher:
        group = dict({"matcher": matcher}, **group)
    return group


def _install_targets() -> tuple[tuple[str, dict], ...]:
    """三个事件各一条：Stop 管回合内续跑，SessionStart 管恢复快照，
    UserPromptSubmit 管用户新回合带来的授权/决策重新评估。"""
    return (
        ("Stop", _hook_group()),
        ("SessionStart", _hook_group(subcommand="session-start",
                                     matcher=SESSION_START_MATCHER)),
        (USER_PROMPT_EVENT, _hook_group(subcommand="user-prompt")),
    )


def _config_payload() -> dict:
    return {"hooks": {"enabled": True, "events": {
        event: [group] for event, group in _install_targets()}}}


def _config_path(workspace: str) -> str:
    candidate = os.path.join(workspace, ".zcode", "config.json")
    legacy = os.path.join(workspace, "zcode.json")
    if os.path.isfile(candidate):
        return candidate
    if os.path.isfile(legacy):
        return legacy
    return candidate


def _user_config_path() -> str:
    """用户级配置：ZCode 的整个 CLI 配置（hooks/mcp/plugins 同一文件）。"""
    override = str(os.environ.get(USER_CONFIG_ENV) or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".zcode", "cli", "config.json")


def _target_config_path(args) -> str:
    """--config 显式路径 > --user 用户级 > 工作区默认。"""
    explicit = str(getattr(args, "config", "") or "").strip()
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    if getattr(args, "user", False):
        return _user_config_path()
    workspace = os.path.abspath(getattr(args, "workspace", "") or os.getcwd())
    return _config_path(workspace)


def _scope_note(path: str) -> str:
    user_path = _user_config_path()
    return "用户级（对所有工作区生效）" if os.path.abspath(path) == user_path else "工作区级"


def cmd_config(args) -> int:
    print(json.dumps(_config_payload(), ensure_ascii=False, indent=2))
    return 0


def _load_config(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _write_config(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _require_target(args) -> str:
    """校验已给出安装目标；缺失时返回空串由调用方报错。"""
    if getattr(args, "config", "") or getattr(args, "user", False):
        return _target_config_path(args)
    if getattr(args, "workspace", ""):
        return _target_config_path(args)
    return ""


def _is_guard_group(group: object) -> bool:
    return "copilot_stop_guard.py" in json.dumps(group, ensure_ascii=False)


def cmd_install(args) -> int:
    path = _require_target(args)
    if not path:
        sys.stderr.write("需要指定安装目标：--workspace DIR（工作区级）、--user（用户级）"
                         "或 --config PATH。\n")
        return 2
    data = _load_config(path)
    hooks = data.setdefault("hooks", {})
    hooks["enabled"] = True
    events = hooks.setdefault("events", {})
    changed: list[str] = []
    removed = 0
    for event, wanted in _install_targets():
        groups = events.setdefault(event, [])
        existing = [group for group in groups if _is_guard_group(group)]
        if len(existing) == 1 and existing[0] == wanted:
            continue
        # 幂等：先移除本守卫在该事件下的旧条目（可能指向过期路径或重复多份），再写一条。
        kept = [group for group in groups if not _is_guard_group(group)]
        removed += len(groups) - len(kept)
        kept.append(wanted)
        events[event] = kept
        changed.append(event)
    if not changed:
        # 重复安装不产生重复 Hook，也不重复备份/改写配置。
        print(f"已存在 Copilot 续跑守卫与恢复注入：{path}（{_scope_note(path)}）")
        _print_runtime_hint()
        return 0
    if not args.apply:
        if removed:
            print(f"{path} 已有 {removed} 条 Copilot 守卫记录，将被收敛为每事件 1 条"
                  "（未加 --apply，不做修改）")
        else:
            print(f"将写入 {path}（{_scope_note(path)}；未加 --apply，不做修改）：")
            print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    backup = ""
    if os.path.isfile(path):
        backup = path + ".bak-" + datetime.now().strftime("%Y%m%d%H%M%S")
        with open(path, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
    _write_config(path, data)
    print(f"已安装 Stop 续跑守卫与 SessionStart 恢复注入"
          f"（{SESSION_START_MATCHER}）：{path}（{_scope_note(path)}）")
    if removed:
        print(f"已收敛 {removed} 条重复/过期记录为每事件 1 条")
    if backup:
        print(f"原配置备份：{backup}")
    _print_runtime_hint()
    print("新会话生效（配置在会话启动时加载）；可用 status 子命令验证判定。")
    return 0


def cmd_uninstall(args) -> int:
    path = _require_target(args)
    if not path:
        sys.stderr.write("需要指定卸载目标：--workspace DIR、--user 或 --config PATH。\n")
        return 2
    data = _load_config(path)
    hooks = data.get("hooks") or {}
    events = hooks.get("events") or {}
    removed = 0
    for event in ("Stop", "SessionStart", USER_PROMPT_EVENT):
        groups = events.get(event) or []
        kept = [group for group in groups if not _is_guard_group(group)]
        removed += len(groups) - len(kept)
        if event in events:
            events[event] = kept
    if not removed:
        print(f"未安装 Copilot 续跑守卫：{path}")
        return 0
    if not args.apply:
        print(f"将从 {path} 移除 Copilot 续跑守卫与恢复注入（未加 --apply，不做修改）")
        return 0
    _write_config(path, data)
    print(f"已移除 Copilot 续跑守卫与恢复注入：{path}")
    return 0


def _marker_dir(args) -> str:
    """disable/enable 的目标目录：state 所在任务目录，或工作区 .copilot-tasks。"""
    state = str(getattr(args, "state", "") or "").strip()
    if state:
        return os.path.dirname(os.path.abspath(os.path.expanduser(state)))
    task_dir = str(getattr(args, "task_dir", "") or "").strip()
    if task_dir:
        return os.path.abspath(os.path.expanduser(task_dir))
    workspace = str(getattr(args, "workspace", "") or "").strip()
    if workspace:
        root = find_tasks_root(os.path.abspath(os.path.expanduser(workspace)))
        base = os.path.join(root or os.path.abspath(os.path.expanduser(workspace)),
                            TASKS_DIR)
        return base
    return ""


def _write_marker(directory: str, reason: str, by: str) -> str:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, OFF_MARKER)
    payload = {"reason": reason, "by": by, "at": datetime.now(timezone.utc).isoformat()}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


def cmd_disable(args) -> int:
    directory = _marker_dir(args)
    if not directory:
        sys.stderr.write("需要指定目标：--state PATH、--task-dir DIR 或 --workspace DIR。\n")
        return 2
    reason = str(args.reason or "").strip()
    if not reason:
        sys.stderr.write("disable 必须写明原因（--reason），便于审计为什么放行。\n")
        return 2
    path = _write_marker(directory, reason, str(args.by or "agent"))
    print(f"已临时放行 Stop 守卫：{path}")
    print("该目录下的任务不再拦截回合；恢复用 enable。")
    return 0


def cmd_enable(args) -> int:
    directory = _marker_dir(args)
    if not directory:
        sys.stderr.write("需要指定目标：--state PATH、--task-dir DIR 或 --workspace DIR。\n")
        return 2
    path = os.path.join(directory, OFF_MARKER)
    if not os.path.exists(path):
        print(f"本来就没有放行标记：{path}")
        return 0
    os.unlink(path)
    print(f"已恢复 Stop 守卫：{path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copilot Stop 续跑守卫")
    sub = parser.add_subparsers(dest="command")
    p_status = sub.add_parser("status", help="只读列出要求续跑的任务")
    p_status.add_argument("--workspace", default="")
    p_status.add_argument("--session", default="",
                          help="按指定会话判定归属（默认读 ZCODE_SESSION_ID）")
    sub.add_parser("session-start",
                   help="SessionStart 恢复注入（resume|compact 快照；startup 只给指针）")
    sub.add_parser("user-prompt",
                   help="UserPromptSubmit 逐回合提示未闭合任务（只读、失败放行）")
    p_config = sub.add_parser("config", help="打印 Stop/SessionStart/UserPromptSubmit 钩子配置片段")
    p_config.add_argument("--workspace", default="")
    p_install = sub.add_parser("install", help="把守卫写入工作区/用户级配置")
    p_install.add_argument("--workspace", default="", help="工作区级：写入 <ws>/.zcode/config.json")
    p_install.add_argument("--user", action="store_true",
                           help="用户级：写入 ~/.zcode/cli/config.json（可用 ZCODE_USER_CONFIG 覆盖）")
    p_install.add_argument("--config", default="", help="显式指定配置文件路径")
    p_install.add_argument("--apply", action="store_true")
    p_uninstall = sub.add_parser("uninstall", help="从工作区/用户级配置移除守卫")
    p_uninstall.add_argument("--workspace", default="")
    p_uninstall.add_argument("--user", action="store_true")
    p_uninstall.add_argument("--config", default="")
    p_uninstall.add_argument("--apply", action="store_true")
    for name, help_text in (("disable", "临时放行（用户明确要求停止/紧急退出）"),
                            ("enable", "恢复拦截")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--state", default="", help="任务 state.json 路径")
        p.add_argument("--task-dir", default="", help="任务目录")
        p.add_argument("--workspace", default="", help="整个工作区")
        if name == "disable":
            p.add_argument("--reason", default="", help="为什么放行（必填，审计用）")
            p.add_argument("--by", default="agent", help="谁决定的（默认 agent）")
    args = parser.parse_args(argv)
    if args.command == "session-start":
        return cmd_session_start()
    if args.command == "user-prompt":
        return cmd_user_prompt()
    if args.command == "status":
        return cmd_status(args)
    if args.command == "config":
        return cmd_config(args)
    if args.command == "install":
        return cmd_install(args)
    if args.command == "uninstall":
        return cmd_uninstall(args)
    if args.command == "disable":
        return cmd_disable(args)
    if args.command == "enable":
        return cmd_enable(args)
    return cmd_check()


if __name__ == "__main__":
    sys.exit(main())

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

用法：
  copilot_stop_guard.py                     # Stop 钩子模式（stdin 读事件 JSON）
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
    except (ValueError, TypeError) as exc:
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
    """从 cwd 向上有限层查找 `.copilot-tasks`；不扫全盘。"""
    current = os.path.abspath(start)
    seen = 0
    while current and seen <= max_up:
        if os.path.isdir(os.path.join(current, TASKS_DIR)):
            return current
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


def collect(root: str, *, session: str = "") -> tuple[list[dict], list[dict]]:
    """返回 (要求续跑的任务, 异常)。

    任务是否“要求续跑”只由 Copilot 的本地判定决定；这里的会话绑定只决定
    本会话是否为该任务的负责方。未绑定（旧任务）与其它会话的任务不拦截。
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
        try:
            state = G.load_state(path)
        except (G.GovernanceError, OSError, ValueError, TypeError) as exc:
            anomalies.append({"kind": "state", "task_id": name,
                              "task_dir": task_dir, "state_path": path,
                              "detail": f"{type(exc).__name__}: {exc}"})
            continue
        if _task_disabled(state):
            continue
        try:
            cont = G.continuation(state)
        except Exception as exc:  # noqa: BLE001 - 任何异常都不能变成静默放行
            anomalies.append({"kind": "evaluate", "task_id": name,
                              "task_dir": task_dir, "state_path": path,
                              "detail": f"{type(exc).__name__}: {exc}"})
            continue
        if not cont.get("required"):
            continue
        ids, bind_error = _recorded_sessions(task_dir)
        if bind_error:
            # 状态明确要求续跑，但绑定文件坏了：按本会话负责处理（fail-closed），
            # 同时把修复命令给出来，而不是悄悄跳过这条任务。
            anomalies.append({"kind": "binding", "task_id": name,
                              "task_dir": task_dir, "state_path": path,
                              "detail": bind_error})
            binding = "corrupt"
        elif not ids:
            binding = "unbound"
        elif session and session in ids:
            binding = "bound"
        elif not session:
            binding = "unknown-session"
        else:
            binding = "foreign"
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


def _hook_command() -> str:
    return 'python3 -B "$HOME/.zcode/skills/copilot/scripts/copilot_stop_guard.py"'


def _config_payload() -> dict:
    return {"hooks": {"enabled": True, "events": {"Stop": [
        {"hooks": [{"type": "command", "command": _hook_command(), "timeout": 10}]}
    ]}}}


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
    stop = events.setdefault("Stop", [])
    command = _hook_command()
    wanted = {"hooks": [{"type": "command", "command": command, "timeout": 10}]}
    existing = [group for group in stop if _is_guard_group(group)]
    if len(existing) == 1 and existing[0] == wanted:
        # 重复安装不产生重复 Hook，也不重复备份/改写配置。
        print(f"已存在 Copilot 续跑守卫：{path}（{_scope_note(path)}）")
        _print_runtime_hint()
        return 0
    # 幂等：先移除本守卫的旧条目（可能指向过期路径或重复多份），再写一条。
    kept = [group for group in stop if not _is_guard_group(group)]
    removed = len(stop) - len(kept)
    kept.append(wanted)
    events["Stop"] = kept
    if not args.apply:
        if removed:
            print(f"{path} 已有 {removed} 条 Copilot 续跑守卫记录，将被收敛为 1 条"
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
    print(f"已安装 Copilot 续跑守卫：{path}（{_scope_note(path)}）")
    if removed:
        print(f"已收敛 {removed} 条重复/过期记录为 1 条")
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
    stop = events.get("Stop") or []
    kept = [group for group in stop if not _is_guard_group(group)]
    if len(kept) == len(stop):
        print(f"未安装 Copilot 续跑守卫：{path}")
        return 0
    events["Stop"] = kept
    if not args.apply:
        print(f"将从 {path} 移除 Copilot 续跑守卫（未加 --apply，不做修改）")
        return 0
    _write_config(path, data)
    print(f"已移除 Copilot 续跑守卫：{path}")
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
    p_config = sub.add_parser("config", help="打印 Stop 钩子配置片段")
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

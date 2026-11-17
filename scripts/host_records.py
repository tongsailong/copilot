#!/usr/bin/env python3
"""宿主持久记录的只读访问层：session / 模型选择 / Reviewer 最终输出。

设计边界：

- 只以 `mode=ro` 打开 ZCode 宿主 SQLite，不写库、不调用模型、不执行命令。
- 最终 Reviewer 输出取 `message.role=assistant` 的最后一条非空消息，按 `part`
  顺序拼接 `type=text` 的文本；这与宿主 `outputFile` 字节一致（真实会话验证）。
- 每条输出都带 message/part id、时间、模型与文本 SHA-256，供取证记录溯源。
- 本机 DB 无密码学签名：一致性不等于防伪造，调用方必须保留这一限制说明。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
from urllib.parse import quote

# 子会话 runtime/execution_state 的准入结论。plan 模式会让宿主给子代理注入
# Bash（真实记录：planEnabled=true 的独立只读子会话执行过 `git status`），
# 因此“精确 Read/Grep/Glob”不能只看 profileSnapshot —— 必须同时证明子会话不是
# plan 模式。未知/缺失一律 fail-closed。
HOST_SESSION_STATE_MISSING = "host_session_state_missing"
HOST_SESSION_STATE_UNKNOWN = "host_session_state_unknown"
HOST_SESSION_PLAN_MODE = "host_session_plan_mode"


def connect_ro(db: str) -> sqlite3.Connection:
    if not db or not os.path.isfile(db):
        raise OSError(f"宿主数据库不存在：{db or '-'}")
    uri = "file:" + quote(os.path.abspath(db), safe="/") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only=ON")
    return con


def _json(raw: object) -> dict:
    try:
        data = json.loads(str(raw or ""))
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# 宿主明确拒绝且确认没有执行的错误类型。分类只依据宿主记录：错误类型必须
# 属于策略/权限/工具缺失，且执行时长、输出、退出码都为空或零；任何一项不满足
# 都按 executed_failed（不能证明未执行）处理，只有状态本身未知才走
# execution_unknown（同样 fail-closed）。
DENIED_ERROR_TYPES = frozenset({
    "permission_denied", "policy_denied", "tool_not_allowed", "tool_not_found",
})
# 同一违规工具被反复拒绝的次数达到该值时，视为无效重试循环。
REPEATED_DENIAL_THRESHOLD = 3
TOOL_USAGE_OPTIONAL_COLUMNS = (
    "error_type", "error_code", "duration_ms", "output_bytes",
    "stdout_bytes", "stderr_bytes", "exit_code", "error_message",
)


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    """读取表列名；宿主版本不同可能没有可选诊断列。"""
    try:
        return {str(row[1]) for row in con.execute(f"pragma table_info({table})")}
    except sqlite3.Error:
        return set()


def tool_execution_state(record: dict) -> str:
    """把一次工具记录归类为 completed / denied_not_executed /
    executed_failed / execution_unknown。

    “请求了工具”不等于“工具执行了”。只有宿主明确给出策略拒绝错误码、且诊断列
    （`duration_ms`/`output_bytes`）实际存在并**显式为零**、无退出码时，才返回
    denied_not_executed；诊断列缺失或为 NULL 时无法证明未执行，返回
    execution_unknown（fail-closed）。
    """
    status = str(record.get("status") or "")
    if status == "completed":
        return "completed"
    if status == "error":
        error_type = str(record.get("error_type") or "").strip().lower()
        if error_type not in DENIED_ERROR_TYPES:
            return "executed_failed"
        if any(key not in record for key in ("duration_ms", "output_bytes")):
            return "execution_unknown"
        duration = record.get("duration_ms")
        output = record.get("output_bytes")
        if duration in (None, "") or output in (None, ""):
            # 诊断列存在但值为 NULL：宿主没有给出“零执行”证据。
            return "execution_unknown"
        if duration not in (0, "0") or output not in (0, "0"):
            return "executed_failed"
        for key in ("stdout_bytes", "stderr_bytes"):
            if record.get(key) not in (None, 0, "0"):
                return "executed_failed"
        if record.get("exit_code") not in (None, ""):
            return "executed_failed"
        return "denied_not_executed"
    # running / cancelled / 未知状态：无法证明未执行，保持 fail-closed。
    return "execution_unknown"


def session_row(db: str, session_id: str) -> dict | None:
    """读取 session 基本行；不存在或表不可用时返回 None。"""
    try:
        con = connect_ro(db)
    except (OSError, sqlite3.Error):
        return None
    try:
        row = con.execute(
            "select id, parent_id, task_type, trace_id "
            "from session where id=?", (str(session_id or ""),)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if not row:
        return None
    return {"id": str(row[0] or ""), "parent_id": str(row[1] or ""),
            "task_type": str(row[2] or ""), "trace_id": str(row[3] or "")}


def model_selection(db: str, session_id: str) -> dict:
    """从 `session_entry(runtime/model_selection)` 读取持久模型记录。"""
    try:
        con = connect_ro(db)
    except (OSError, sqlite3.Error):
        return {}
    try:
        rows = con.execute(
            "select data from session_entry where session_id=? "
            "and type='runtime/model_selection' order by time_created",
            (str(session_id or ""),)).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    for (raw,) in reversed(rows):
        data = _json(raw)
        selection = data.get("modelSelection")
        if not isinstance(selection, dict):
            continue
        provider = str(selection.get("providerId") or "").strip()
        model = str(selection.get("modelId") or "").strip()
        if provider and model:
            options = selection.get("options")
            return {
                "provider_id": provider,
                "model_id": model,
                "reasoning": str((options or {}).get("reasoningLevel") or "")
                if isinstance(options, dict) else "",
                "source": "session_entry/runtime/model_selection",
            }
    return {}


def _assistant_text(con: sqlite3.Connection, message_id: str) -> tuple[str, list[str]]:
    parts = con.execute(
        "select id, data from part where message_id=? order by sequence, rowid",
        (message_id,)).fetchall()
    texts: list[str] = []
    part_ids: list[str] = []
    for part_id, raw in parts:
        data = _json(raw)
        if data.get("type") != "text":
            continue
        text = data.get("text")
        if not isinstance(text, str) or not text:
            continue
        texts.append(text)
        part_ids.append(str(part_id or ""))
    return "".join(texts), part_ids


def assistant_output(db: str, session_id: str) -> dict | None:
    """重建该 session 最后一条非空 assistant 文本输出。

    返回 None 表示 DB 缺少可用输出（调用方必须 fail-closed，不能猜测）。
    """
    try:
        con = connect_ro(db)
    except (OSError, sqlite3.Error):
        return None
    try:
        try:
            rows = con.execute(
                "select id, sequence, time_created, data from message "
                "where session_id=? order by sequence desc, time_created desc",
                (str(session_id or ""),)).fetchall()
        except sqlite3.Error:
            return None
        for message_id, sequence, created, raw in rows:
            message = _json(raw)
            if str(message.get("role") or "") != "assistant":
                continue
            text, part_ids = _assistant_text(con, str(message_id))
            if not text:
                continue
            model = {
                "provider_id": str(message.get("providerId") or ""),
                "model_id": str(message.get("modelId") or ""),
            }
            return {
                "session_id": str(session_id or ""),
                "message_id": str(message_id or ""),
                "part_ids": part_ids,
                "sequence": sequence,
                "created_at_ms": created,
                "finish": str(message.get("finish") or ""),
                "text": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "bytes": len(text.encode("utf-8")),
                "model": model,
                "source": "db.message.part",
            }
    finally:
        con.close()
    return None


def session_execution_state(db: str, session_id: str) -> dict:
    """读取子会话的 `runtime/execution_state`（mode / planEnabled）。

    宿主在会话启动时写入该行；`planEnabled=true` 意味着父会话处于 plan 模式，
    宿主会给子代理注入 Bash（`git status` 等只读命令可真实执行）。G4 要求精确
    `Read/Grep/Glob`，所以必须在工具执行前用该记录排除 plan 模式，而不能只在
    事后看有没有调用 Bash。

    返回 `found=False` 表示没有记录/库不可读；`plan_enabled=None` 表示字段缺失
    或不是布尔值。两者都不能证明“非 plan 模式”，调用方必须 fail-closed。
    """
    session_id = str(session_id or "").strip()
    base = {"found": False, "mode": "", "plan_enabled": None,
            "source": "session_entry/runtime/execution_state"}
    if not session_id:
        base["reason"] = "session_missing"
        return base
    try:
        con = connect_ro(db)
    except (OSError, sqlite3.Error) as exc:
        base["reason"] = f"host_db_unavailable: {exc}"
        return base
    try:
        try:
            rows = con.execute(
                "select data from session_entry where session_id=? "
                "and type='runtime/execution_state' "
                "order by time_created desc limit 1",
                (session_id,)).fetchall()
        except sqlite3.Error as exc:
            base["reason"] = f"session_entry_unavailable: {exc}"
            return base
    finally:
        con.close()
    if not rows:
        base["reason"] = "execution_state_missing"
        return base
    return classify_execution_payload(rows[0][0])


def classify_execution_payload(raw: object) -> dict:
    """把一条 `runtime/execution_state` 原始 data 归一化为准入输入。

    供 `g4_attest`（已持有只读连接）与 `session_execution_state` 共用，避免两处
    对“行存在但 JSON 非法/不是对象”给出不同结论。
    """
    data = _json(raw)
    plan = data.get("planEnabled")
    return {
        "found": True,
        "mode": str(data.get("mode") or ""),
        "plan_enabled": plan if isinstance(plan, bool) else None,
        "source": "session_entry/runtime/execution_state",
    }


def execution_state_problems(state: object) -> list[tuple[str, str]]:
    """把 execution_state 归类为 G4 准入偏离项；空列表表示可继续核验。

    只回答“子会话是否可能被宿主动态放宽工具集”。Profile 策略、工具调用与
    原始输出仍由 g4_attest 独立核验。
    """
    data = state if isinstance(state, dict) else {}
    if not data.get("found"):
        return [(HOST_SESSION_STATE_MISSING,
                 "宿主没有子会话 runtime/execution_state 记录：无法证明不是 plan 模式")]
    plan = data.get("plan_enabled")
    if plan is True:
        return [(HOST_SESSION_PLAN_MODE,
                 "子会话 planEnabled=true：plan 模式会给子代理注入 Bash，"
                 "有效工具策略不再精确等于 Read/Grep/Glob")]
    if not isinstance(plan, bool):
        return [(HOST_SESSION_STATE_UNKNOWN,
                 "runtime/execution_state 缺少布尔 planEnabled：无法证明非 plan 模式")]
    if not str(data.get("mode") or "").strip():
        return [(HOST_SESSION_STATE_UNKNOWN,
                 "runtime/execution_state 缺少 mode：无法证明子会话执行模式")]
    return []


def host_version(asar_path: str = "") -> dict:
    """探测本机 ZCode 版本：环境变量 → app.asar package.json → 未知。

    只用于能力诊断的“当前宿主”记录，不参与授权判定；每次调用都重新读取，
    不缓存，宿主升级后再次探测即可得到新结果。
    """
    env = os.environ.get("ZCODE_VERSION")
    if env and env.strip():
        return {"version": env.strip(), "source": "env:ZCODE_VERSION"}
    path = asar_path or "/opt/ZCode/resources/app.asar"
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
            if len(head) != 16:
                raise OSError("asar header too short")
            header_size = struct.unpack("<I", head[4:8])[0]
            json_len = struct.unpack("<I", head[12:16])[0]
            meta = json.loads(fh.read(json_len).decode("utf-8"))
            if not isinstance(meta, dict):
                raise OSError("asar header is not an object")
            files = meta.get("files")
            entry = files.get("package.json") \
                if isinstance(files, dict) else None
            if not isinstance(entry, dict):
                raise OSError("asar package.json entry missing")
            # 不同 Electron/asar 版本把 offset 写成整数或十进制字符串；合法的
            # 整数 0 不能因为 `0 or -1` 被当成缺失。
            raw_offset = entry.get("offset")
            raw_size = entry.get("size")
            offset = int(raw_offset) if raw_offset not in (None, "") else -1
            size = int(raw_size) if raw_size not in (None, "") else -1
            if offset < 0 or size <= 0:
                raise OSError("asar package.json entry missing")
            fh.seek(8 + header_size + offset)
            payload = json.loads(fh.read(size).decode("utf-8"))
    except (OSError, ValueError, TypeError, AttributeError, struct.error,
            json.JSONDecodeError):
        return {"version": "", "source": "unavailable", "path": path}
    if not isinstance(payload, dict):
        return {"version": "", "source": "unavailable", "path": path}
    version = str(payload.get("version") or "").strip()
    if not version:
        return {"version": "", "source": "unavailable", "path": path}
    return {"version": version, "source": "app.asar:package.json",
            "path": path, "name": str(payload.get("name") or "")}

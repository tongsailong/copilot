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
from urllib.parse import quote


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

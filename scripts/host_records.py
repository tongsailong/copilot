#!/usr/bin/env python3
"""宿主持久记录的只读访问层：模型选择记录。

设计边界：

- 只以 `mode=ro` 打开 ZCode 宿主 SQLite，不写库、不调用模型、不执行命令。
- 只读取执行方回填执行信封所需的模型记录（rollout 被清理时的回退）。
- 本机 DB 无密码学签名：一致性不等于防伪造，调用方必须保留这一限制说明。

v8 起 G4 原生只读复核已退役：本模块不再读取 Reviewer 工具调用、最终输出或
子会话执行模式等 G4 专用取证数据。
"""
from __future__ import annotations

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


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    """读取表列名；宿主版本不同可能没有可选诊断列。"""
    try:
        return {str(row[1]) for row in con.execute(f"pragma table_info({table})")}
    except sqlite3.Error:
        return set()


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


__all__ = ["connect_ro", "table_columns", "model_selection"]

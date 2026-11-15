#!/usr/bin/env python3
"""Reviewer 原始输出的唯一解析契约（v2，严格 JSON）。

规则（新版本，不再保留“围栏外可带叙述”的旧兼容）：

- 整段文本必须是一个 JSON 对象；或恰好一个 Markdown 围栏，围栏内是一个 JSON
  对象，围栏外只允许空白。
- 多个对象/多个围栏、围栏外叙述、重复键、非对象顶层一律拒收。
- 不做文案修正、不猜测结论；原始文本 SHA-256 由调用方单独保存。

旧版本若输出前言/多围栏，会被 fail-closed：纯 JSON 要求写在复核派发 prompt 里
（通用只读 Profile 不定义输出契约），这是明确的版本边界（`PARSER_VERSION`）。
"""
from __future__ import annotations

import json
import re

PARSER_VERSION = "review-json/v2"
_FENCE = re.compile(r"```[ \t]*[A-Za-z0-9_+.-]*[ \t]*\r?\n?(.*?)```", re.S)


class ReviewOutputError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def _no_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen[key] = value
    return seen


def _loads(text: str) -> dict:
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicates)
    except ValueError as exc:
        raise ReviewOutputError("review_output_unparseable", str(exc)) from None
    if not isinstance(value, dict):
        raise ReviewOutputError("review_output_not_object",
                                f"top level is {type(value).__name__}")
    return value


def parse_review_output(text: str) -> dict:
    """解析 Reviewer 原始输出；失败抛 ReviewOutputError（fail-closed）。"""
    cleaned = str(text or "").lstrip("\ufeff").strip()
    if not cleaned:
        raise ReviewOutputError("review_output_empty", "输出为空")
    try:
        return _loads(cleaned)
    except ReviewOutputError as direct:
        if direct.code == "review_output_not_object":
            raise
        first_error = direct
    matches = list(_FENCE.finditer(cleaned))
    if not matches:
        raise ReviewOutputError("review_output_unparseable", first_error.detail)
    if len(matches) > 1:
        raise ReviewOutputError(
            "review_output_ambiguous", f"{len(matches)} fences")
    outside = _FENCE.sub("", cleaned)
    if outside.strip():
        raise ReviewOutputError(
            "review_output_outside_text",
            "围栏外存在非空白文本；拒绝从叙述中猜结论")
    return _loads(matches[0].group(1).strip())


def validate_envelope(data: dict) -> dict:
    """公共字段结构校验；返回原对象，失败抛错（语义检查仍由调用方完成）。"""
    if not isinstance(data.get("schema"), str) or not data["schema"].strip():
        raise ReviewOutputError("review_schema_missing")
    verdict = data.get("verdict")
    if not isinstance(verdict, str) or verdict.strip().lower() not in (
            "pass", "partial", "blocked"):
        raise ReviewOutputError("review_verdict_invalid", repr(verdict))
    findings = data.get("findings")
    if not isinstance(findings, list) or any(
            not isinstance(item, dict) for item in findings):
        raise ReviewOutputError("review_findings_invalid")
    for key in ("changed_files", "unverified", "residual_risks",
                "files_inspected", "commands_run"):
        value = data.get(key)
        if value is not None and (
                not isinstance(value, list)
                or any(not isinstance(item, str) for item in value)):
            raise ReviewOutputError(f"review_{key}_invalid")
    return data

#!/usr/bin/env python3
"""G4 宿主取证器：只读校验一次原生复核的宿主记录是否可采信。

设计边界（本模块不改变现有 G4 正式路径）：

- 只读 metadata.json、宿主 SQLite（`mode=ro`）、attempt JSON、证据文件与
  Reviewer 的 outputFile；不调用模型、不联网、不执行 Bash、不写宿主数据库。
- 不依赖 Reviewer 专属 Profile 名称、来源或路径，也不依赖 rollout 长期留存。
  只读边界按宿主 `profileSnapshot` 的有效工具策略判定（工具精确等于
  Read/Grep/Glob、禁用 Write/Edit/Bash、无 skills/MCP），并按 Profile 文件内容
  交叉核对快照；rollout 只在存在时做第三路交叉核对；模型身份取
  `session_entry` 的持久模型记录。
- 输出确定性取证记录（`copilot-g4-attestation/v1`），可在 ingest 时保存，供
  rollout 与本地 DB 被清理后审计。

这不是抗篡改证明：metadata.json 与本地 SQLite 没有密码学签名，本模块只能证明
“多个宿主记录在取证时彼此一致、且快照文件未被改动”，不能证明同用户进程无法
伪造它们；也不是 OS 隔离：Read 可以访问工作区外文件，本模块只能拒绝 G4，不能
阻止内容已经进入模型上下文。attempt 之后新增的未跟踪文件与 submodule 提交仍由
G2 `code_scope` 负责；本模块不重新执行 git。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import quote

import code_scope
import host_records
import native_bridge as NB
import review_output
from lib_common import is_sensitive_path, sha256_file, sha256_text

SCHEMA = "copilot-g4-attestation/v1"
# 只读复核的宿主有效权限契约：工具精确等于只读集合、显式禁用写入/执行工具。
# 判定依据是 metadata.profileSnapshot（宿主派发时固化），不依赖 Profile 名称、
# 来源或所在路径；`tools=[]` 在宿主语义里等于继承父工具，必须拒绝。
READONLY_TOOLS = frozenset({"Read", "Grep", "Glob"})
REQUIRED_DISALLOWED = frozenset({"Write", "Edit", "Bash"})
ALLOWED_PERMISSION_MODES = frozenset({"default", "plan"})
REVIEW_SCHEMAS = frozenset({"copilot-native-review/v2"})
# 同一违规工具被宿主反复拒绝的次数达到该值时，视为无效重试循环，记录残余告警。
REPEATED_DENIAL_THRESHOLD = host_records.REPEATED_DENIAL_THRESHOLD
METADATA_FIELDS = ("agentId", "childSessionId", "parentSessionId", "profileId",
                   "profileSnapshot", "prompt", "status", "createdAt",
                   "workspaceRoot", "outputFile", "totalToolUseCount")
ATTEMPT_ID_RE = re.compile(r"^RA[0-9a-f]{24}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON_BYTES = 4 * 1024 * 1024


class AttestError(Exception):
    """取证前置条件不成立（文件缺失、JSON 非法、参数为空等）。"""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}")


# ── 基础工具 ────────────────────────────────────────────────────────────────
def _canonical_json(value: object) -> bytes:
    """确定性 JSON（bytes），与 code_scope 共用同一序列化口径。"""
    return code_scope.canonical_json(value).encode("utf-8")


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _strict_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"重复键：{key}")
        out[key] = value
    return out


def _parse_json(text: str):
    return json.loads(text.lstrip("﻿"), object_pairs_hook=_strict_object)


def _load_json_with_sha(path: str) -> tuple[object, str]:
    """一次读取并哈希，避免检查与记录之间文件被替换。"""
    if not os.path.isfile(path):
        raise AttestError("record_missing", f"文件不存在：{path}")
    if os.path.getsize(path) > MAX_JSON_BYTES:
        raise AttestError("record_too_large", f"文件过大：{path}")
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8")
        return _parse_json(text), sha256_text(text)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise AttestError("record_invalid", f"无法解析 {path}：{exc}") from None


def _load_json(path: str) -> object:
    return _load_json_with_sha(path)[0]


def _metadata_path(raw: str) -> str:
    path = os.path.abspath(os.path.expanduser(raw))
    if os.path.isdir(path):
        path = os.path.join(path, "metadata.json")
    return path


def _under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _literal_and_real(raw: str, workspace: str) -> tuple[str, str]:
    """返回 (字面绝对路径, 真实路径)，用于识别符号链接/路径穿越越界。"""
    text = str(raw or "").strip()
    if not text:
        return "", ""
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        expanded = os.path.join(workspace, expanded)
    literal = os.path.abspath(expanded)
    return literal, os.path.realpath(literal)


def _resolve(raw: str, workspace: str) -> str:
    return _literal_and_real(raw, workspace)[1]


def _mentions(text: str, tokens: set[str]) -> bool:
    """完整路径或文件名的词边界命中；文件:行号 通过文件名 token 覆盖。"""
    for token in tokens:
        if len(token) < 2:
            continue
        # 边界只按 ASCII 标识符判断：Python 的 `\w` 含 CJK，会把 `未核对a.js`
        # 当成一个词而漏判。句末句点仍算命中（`a.js.`），`b.a.js`/`a.js.bak`
        # 视为不同名字。
        pattern = (r"(?<![A-Za-z0-9_\-])(?<!\.)" + re.escape(token)
                   + r"(?![A-Za-z0-9_\-])(?!\.[A-Za-z0-9])")
        if re.search(pattern, text):
            return True
    return False


def _snapshot_digest(snapshot: dict) -> str:
    """调用 code_scope 的唯一权威摘要实现。"""
    return code_scope.snapshot_digest(snapshot)


def _submodule_paths(lines: list) -> set[str]:
    """从 `git submodule status` 行解析路径；只接受带 40/64 位提交的行。"""
    paths: set[str] = set()
    for line in lines:
        match = re.match(r"^\s*[+-U]?([0-9a-f]{40}|[0-9a-f]{64})\s+(\S+)", str(line))
        if match:
            paths.add(match.group(2))
    return paths


def _read_review(text: str) -> dict | None:
    """按统一 review_output 契约解析；任何不合规输出 fail-closed。"""
    try:
        # 语法契约统一由 review_output 负责；schema/verdict/findings 的
        # 语义检查仍在本模块执行，以保留既有拒绝码。
        return review_output.parse_review_output(text)
    except review_output.ReviewOutputError:
        return None


# ── 宿主数据库 ──────────────────────────────────────────────────────────────
def _db_connect(path: str) -> sqlite3.Connection:
    if not path or not os.path.isfile(path):
        raise AttestError("host_db_unavailable", f"找不到宿主数据库：{path or '-'}")
    uri = "file:" + quote(os.path.abspath(path), safe="/") + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        con.execute("PRAGMA query_only=ON")
        return con
    except sqlite3.Error as exc:
        raise AttestError("host_db_unavailable", f"{path}: {exc}") from None


def _db_rows(con: sqlite3.Connection, sql: str, params: tuple,
             code: str = "host_db_query_failed") -> list[tuple]:
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        raise AttestError(code, f"{sql.split()[0]}: {exc}") from None


def _call_label(record: dict) -> str:
    """拒绝记录的可审计摘要：只保留工具、call id 与宿主错误码/短消息。"""
    return {
        "call_id": str(record.get("call_id") or ""),
        "tool": str(record.get("name") or ""),
        "status": str(record.get("status") or ""),
        "error_type": str(record.get("error_type") or ""),
        "error_code": str(record.get("error_code") or ""),
        "error_message": str(record.get("error_message") or "")[:200],
    }


def _db_fingerprint(path: str) -> dict:
    try:
        stat = os.stat(path)
    except OSError:
        return {"path": path, "size": None, "mtime": "", "note": "unavailable"}
    return {"path": path, "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            .isoformat(timespec="seconds"),
            "note": "size+mtime only; not a content fingerprint"}


# ── 取证主流程 ──────────────────────────────────────────────────────────────
def attest(*, metadata: str, attempt: str, task_id: str, workspace: str,
           expected_target: str = "", evidence: list[str] | None = None,
           db: str = "", executor_agent: str = "", executor_session: str = "",
           task_dir: str = "",
           require_model_selection: bool = True,
           require_target: bool = True,
           require_executor_identity: bool = True,
           outside_reads: str = "reject", consume_marker: str = "",
           expected_output_sha256: str = "", checked_at: str = "") -> dict:
    """校验一次复核的宿主记录；返回确定性取证记录。

    `expected_target` 传当前 `final_evidence_digest(state)`；默认必填，
    否则无法证明 attempt 仍对应当前最终证据摘要（`target_unverified`）。
    `consume_marker` 只用于检测重放；写入由 `write_consume_marker()` 单独完成，
    避免出现“已消费但取证记录未落盘”。
    """
    if outside_reads not in ("reject", "record"):
        raise AttestError("bad_argument", "outside_reads 只能是 reject / record")
    db = db or os.path.expanduser("~/.zcode/cli/db/db.sqlite")
    if not str(task_id or "").strip():
        raise AttestError("bad_argument", "task_id 不能为空")
    if not str(workspace or "").strip():
        raise AttestError("bad_argument", "workspace 不能为空")

    meta_path = _metadata_path(metadata)
    if isinstance(attempt, dict):
        # 影子集成直接传状态里的 attempt 快照，避免为取证额外落盘；
        # attempt_sha256 用规范化 JSON 计算，仍可复现。
        att = attempt
        attempt_path = ""
        attempt_sha = sha256_text(json.dumps(
            attempt, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")))
    else:
        attempt_path = os.path.abspath(os.path.expanduser(attempt))
        att, attempt_sha = _load_json_with_sha(attempt_path)
    meta, meta_sha = _load_json_with_sha(meta_path)
    if not isinstance(meta, dict):
        raise AttestError("record_invalid", "metadata 不是 JSON 对象")
    if not isinstance(att, dict):
        raise AttestError("record_invalid", "attempt 不是 JSON 对象")

    checks: list[dict] = []
    residual: list[str] = []
    evidence_paths: list[str] = []
    observed: dict = {"attempt_sha": attempt_sha}

    def check(code: str, ok: bool, detail: str = "") -> bool:
        checks.append({"code": code, "passed": bool(ok),
                       "detail": detail if not ok else ""})
        return ok

    workspace = os.path.realpath(os.path.abspath(
        os.path.expanduser(workspace or "")))
    task_root = os.path.realpath(os.path.abspath(
        os.path.expanduser(task_dir))) if task_dir else ""
    if task_root:
        # `--task-dir` 会扩大证据与读取的允许根，必须限定在 workspace 内，或
        # 恰为本次 attempt/metadata 所在目录；否则传入 "/" 即可绕过越界检查。
        attempt_parent = os.path.dirname(os.path.realpath(attempt_path)) \
            if attempt_path else ""
        meta_parent = os.path.dirname(os.path.realpath(meta_path))
        task_ok = _under(task_root, workspace) or task_root in (
            attempt_parent, meta_parent)
        check("task_dir_outside_workspace", task_ok,
              f"task_dir={task_root} 不在 workspace 或 attempt/metadata 目录")
        if not task_ok:
            task_root = ""
    allowed_roots = [workspace] + ([task_root] if task_root else [])

    # 1. metadata 必备字段与类型
    missing = [key for key in METADATA_FIELDS
               if key not in meta or meta.get(key) in (None, "")]
    check("metadata_field_missing", not missing, ",".join(missing))
    if missing:
        return _finish(checks, residual, meta, att, observed, evidence_paths,
                       meta_path, meta_sha, attempt_path, db, checked_at,
                       eligible=False)
    snap = meta.get("profileSnapshot")
    if not isinstance(snap, dict):
        check("metadata_field_invalid", False, "profileSnapshot 不是对象")
        return _finish(checks, residual, meta, att, observed, evidence_paths,
                       meta_path, meta_sha, attempt_path, db, checked_at,
                       eligible=False)

    # 2. attempt 绑定
    attempt_id = str(att.get("review_attempt_id") or "").strip()
    check("attempt_id_invalid", bool(ATTEMPT_ID_RE.match(attempt_id)),
          f"review_attempt_id={attempt_id!r}")
    if not check("attempt_task_mismatch",
                 str(att.get("task_id") or "") == task_id,
                 f"attempt={att.get('task_id')!r} task={task_id!r}"):
        return _finish(checks, residual, meta, att, observed, evidence_paths,
                       meta_path, meta_sha, attempt_path, db, checked_at,
                       eligible=False)
    consumed = bool(att.get("consumed")) or bool(att.get("consumed_at"))
    check("attempt_consumed", not consumed, "attempt 已被消费")
    if consume_marker and os.path.exists(consume_marker):
        check("attempt_replayed", False, f"consume marker 已存在：{consume_marker}")
    prompt = str(meta.get("prompt") or "")
    prompt_bound = bool(attempt_id) and bool(re.search(
        r"(?<![\w.\-])" + re.escape(attempt_id) + r"(?![\w.\-])", prompt))
    check("attempt_prompt_unbound", prompt_bound, "宿主 prompt 未携带 attempt id")
    created = _parse_time(meta.get("createdAt"))
    bound = _parse_time(att.get("created_at"))
    if check("attempt_time_invalid", created is not None and bound is not None,
             f"createdAt={meta.get('createdAt')!r} attempt={att.get('created_at')!r}"):
        check("attempt_session_precedes", created >= bound,
              f"会话 {created.isoformat()} 早于 attempt {bound.isoformat()}")
    check("attempt_workspace_mismatch",
          os.path.realpath(os.path.abspath(str(meta.get("workspaceRoot") or "")))
          == workspace,
          str(meta.get("workspaceRoot")))
    target = str(att.get("target_sha256") or "").strip().lower()
    if expected_target:
        check("target_stale", target == str(expected_target).strip().lower(),
              f"{target[:16]} != {str(expected_target)[:16]}")
    elif require_target:
        check("target_unverified", False,
              "未提供 expected_target，无法证明 attempt 仍是当前最终证据摘要")
    else:
        residual.append("未提供 expected_target：未核对复核目标是否为当前最终证据摘要")

    scope = str(att.get("review_scope") or "full").strip().lower()
    check("review_scope_invalid", scope in ("full", "incremental"), scope)
    manifest = [str(item).strip() for item in att.get("evidence_manifest") or []
                if str(item).strip()]
    if scope == "incremental":
        check("review_scope_manifest_missing", bool(manifest),
              "incremental 复核缺少 evidence_manifest")
        check("review_scope_baseline_missing",
              bool(str(att.get("based_on_review_sha256") or "").strip()),
              "incremental 复核缺少 based_on_review_sha256")

    # 3. Profile 边界
    tools = snap.get("tools")
    if not check("profile_tools_empty",
                 isinstance(tools, list) and bool(tools),
                 "空工具集会被宿主解释为继承父工具"):
        tools = []
    check("profile_tools_not_exact",
          isinstance(tools, list) and set(map(str, tools)) == READONLY_TOOLS,
          f"tools={tools} 必须精确等于 {sorted(READONLY_TOOLS)}")
    raw_disallowed = snap.get("disallowedTools")
    check("profile_disallowed_invalid",
          raw_disallowed is None or isinstance(raw_disallowed, list),
          f"disallowedTools={type(raw_disallowed).__name__}")
    disallowed = {str(item) for item in (raw_disallowed or [])
                  if isinstance(item, (str, int))}
    check("profile_disallowed_missing",
          REQUIRED_DISALLOWED <= disallowed,
          ",".join(sorted(REQUIRED_DISALLOWED - disallowed)))
    mode = snap.get("permissionMode")
    check("profile_permission_mode",
          mode in (None, "") or str(mode) in ALLOWED_PERMISSION_MODES,
          f"permissionMode={mode!r}")
    check("profile_skills_not_allowed", not (snap.get("skills") or []),
          f"skills={snap.get('skills')}")
    check("profile_mcp_servers", not (snap.get("mcpServers") or []),
          f"mcpServers={snap.get('mcpServers')}")
    # Profile 名称/来源/路径只作为记录事实：只读边界由上面的有效工具策略
    # 决定，G4 不再要求任何固定名称、固定目录或固定来源。
    source = str(snap.get("source") or "")
    profile_name = str(snap.get("name") or "")
    profile_path = str(snap.get("path") or "")
    # 原定架构只允许 ZCode 内置子代理；项目/用户自定义 Profile 即使工具集精确
    # 也不是正式 G4 载体（source 由宿主按加载目录赋值，不是 frontmatter 可改字段）。
    check("profile_source_not_builtin", source == NB.G4_CARRIER_SOURCE,
          f"source={source!r}；G4 只接受宿主内置 Agent")
    profile_real = _resolve(profile_path, workspace)
    profile_sha = ""
    if check("profile_path_missing",
             bool(profile_path) and os.path.isfile(profile_real), profile_path):
        profile_sha = sha256_file(profile_real)
        try:
            with open(profile_real, "r", encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
            body = raw
            if raw.startswith("---"):
                parts = raw.split("---", 2)
                if len(parts) == 3:
                    body = parts[2]
        except OSError:
            body = ""
        expected_body = str(snap.get("systemPrompt") or "").strip()
        check("profile_system_prompt_missing", bool(expected_body),
              "profileSnapshot.systemPrompt 为空，无法核对 Profile 文件内容")
        if expected_body:
            check("profile_content_mismatch", body.strip() == expected_body,
                  profile_path)

    # 4. 会话完成与独立性
    check("reviewer_not_completed", str(meta.get("status") or "") == "completed",
          str(meta.get("status")))
    if require_executor_identity:
        check("executor_identity_missing", bool(executor_agent or executor_session),
              "未提供执行方 agentId/childSessionId，无法证明非自审")
    if executor_agent:
        check("reviewer_not_independent",
              executor_agent != str(meta.get("agentId") or ""),
              "同一 agentId 自审")
    if executor_session:
        check("reviewer_not_independent",
              executor_session != str(meta.get("childSessionId") or ""),
              "同一 childSessionId 自审")

    # 5. 宿主记录交叉核对
    child = str(meta.get("childSessionId") or "")
    usage: list[tuple] = []
    call_ids: list[str] = []
    names: list[str] = []
    failed_calls: list[str] = []
    model: dict = {}
    session_trace = ""
    only_trace = ""
    turn_id = ""
    completed_reads: list[tuple[str, str]] = []
    partial_reads: list[str] = []
    opened_dirs: list[tuple[str, str]] = []
    try:
        con = _db_connect(db)
    except AttestError as exc:
        check(exc.code, False, exc.detail)
        return _finish(checks, residual, meta, att, observed, evidence_paths,
                       meta_path, meta_sha, attempt_path, db, checked_at,
                       eligible=False)
    try:
        session_rows = _db_rows(
            con, "select parent_id, task_type, trace_id from session where id=?",
            (child,))
        if not check("host_session_missing", bool(session_rows), child):
            return _finish(checks, residual, meta, att, observed,
                           evidence_paths, meta_path, meta_sha, attempt_path,
                           db, checked_at, eligible=False)
        parent_id, task_type, session_trace = session_rows[0]
        check("host_session_parent_mismatch",
              str(parent_id or "") == str(meta.get("parentSessionId") or ""),
              f"{parent_id} != {meta.get('parentSessionId')}")
        check("host_session_kind_mismatch", str(task_type or "") == "subagent_child",
              str(task_type))

        usage_columns = host_records.table_columns(con, "tool_usage")
        optional = [name for name in host_records.TOOL_USAGE_OPTIONAL_COLUMNS
                    if name in usage_columns]
        usage_sql = (
            "select tool_call_id, tool_name, read_only, side_effect_scope, "
            "status, trace_id, turn_id"
            + "".join(", " + name for name in optional)
            + " from tool_usage where session_id=? "
              "order by started_at, tool_call_id")
        usage = _db_rows(con, usage_sql, (child,))
        if not check("host_tool_usage_missing", bool(usage), child):
            return _finish(checks, residual, meta, att, observed,
                           evidence_paths, meta_path, meta_sha, attempt_path,
                           db, checked_at, eligible=False)
        declared = meta.get("totalToolUseCount")
        check("host_tool_count_mismatch",
              isinstance(declared, int) and not isinstance(declared, bool)
              and declared == len(usage),
              f"tool_usage={len(usage)} metadata={declared!r}")
        records: list[dict] = []
        for row in usage:
            record = {
                "call_id": str(row[0] or ""),
                "name": str(row[1] or ""),
                "read_only": row[2],
                "side": row[3],
                "status": str(row[4] or ""),
                "trace": str(row[5] or ""),
                "turn": str(row[6] or ""),
            }
            for index, key in enumerate(optional):
                record[key] = row[7 + index]
            record["execution"] = host_records.tool_execution_state(record)
            records.append(record)
        names = [record["name"] for record in records]
        call_ids = [record["call_id"] for record in records]
        denied_calls = [record for record in records
                        if record["execution"] == "denied_not_executed"]
        executed_calls = [record for record in records
                          if record["execution"] != "denied_not_executed"]
        check("host_call_id_missing", all(call_ids), "tool_usage 存在空 call id")
        check("host_call_id_duplicate",
              len(set(call_ids)) == len(call_ids), "tool_usage 存在重复 call id")
        # 白名单只约束实际执行/状态未知的调用。宿主明确拒绝且证明未执行的
        # 请求（如 plan 模式拒绝 Bash）不构成工具违规，也不消耗复核资格。
        violations = {record["name"] for record in executed_calls} - READONLY_TOOLS
        check("host_tool_not_allowed", not violations,
              ",".join(sorted(violations)))

        for record in records:
            name = record["name"]
            status = record["status"]
            if record["execution"] == "denied_not_executed":
                continue
            if status == "completed":
                read_only, side = record["read_only"], record["side"]
                if int(read_only or 0) != 1:
                    check("host_tool_not_readonly", False,
                          f"{name} read_only={read_only}")
                if str(side or "") != "none":
                    check("host_tool_side_effect", False, f"{name} scope={side}")
            elif status == "error":
                # 失败调用没有副作用分类：允许 NULL，但工具名仍受白名单约束。
                read_only, side = record["read_only"], record["side"]
                if read_only not in (1, None):
                    check("host_tool_not_readonly", False,
                          f"{name} read_only={read_only} (error)")
                if side not in ("none", "", None):
                    check("host_tool_side_effect", False,
                          f"{name} scope={side} (error)")
                failed_calls.append(name)
            else:
                check("host_tool_status_invalid", False, f"{name} status={status}")
        denied_tool_counts: dict[str, int] = {}
        for record in denied_calls:
            value = record["name"]
            denied_tool_counts[value] = denied_tool_counts.get(value, 0) + 1
        if denied_calls:
            residual.append(
                "宿主拒绝且确认未执行的调用 "
                + str(len(denied_calls)) + " 次（"
                + ", ".join(sorted(denied_tool_counts))
                + "）：不计入 G4 工具违规，也不消耗复核资格")
        repeated_denied = sorted(name for name, count in denied_tool_counts.items()
                                 if count >= REPEATED_DENIAL_THRESHOLD)
        if repeated_denied:
            residual.append(
                "重复被宿主拒绝的调用（" + ", ".join(repeated_denied)
                + "）：Reviewer 应停止无效重试，继续用 Read/Grep/Glob 完成复核；"
                  "若仍持续尝试越权工具，由主代理终止该会话并新建只读复核")
        traces = {record["trace"] for record in records}
        turns = {record["turn"] for record in records}
        check("host_execution_split",
              len(traces) == 1 and len(turns) == 1
              and all(traces) and all(turns),
              f"trace={sorted(traces)} turn={sorted(turns)}")
        only_trace = next(iter(traces)) if len(traces) == 1 else ""
        turn_id = next(iter(turns)) if len(turns) == 1 else ""
        check("host_trace_mismatch",
              bool(only_trace) and str(session_trace or "") == only_trace,
              f"session={session_trace} tools={sorted(traces)}")

        part_rows = _db_rows(
            con, "select data from part where session_id=? "
                 "order by sequence, id", (child,))
        part_calls: dict[str, tuple[str, str, dict]] = {}
        part_names: list[str] = []
        for (raw,) in part_rows:
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("type") != "tool":
                continue
            tool = str(data.get("tool") or "")
            call_id = str(data.get("callID") or "")
            state = data.get("state") or {}
            if not isinstance(state, dict):
                state = {}
            part_names.append(tool)
            if not call_id:
                continue
            if call_id in part_calls:
                check("host_part_duplicate_call", False, call_id)
            part_calls[call_id] = (tool, str(state.get("status") or ""),
                                   state.get("input") or {})
            if state.get("status") != "completed" \
                    or not isinstance(state.get("input"), dict):
                continue
            payload = state["input"]
            if tool == "Read" and payload.get("file_path"):
                if payload.get("offset") or payload.get("limit"):
                    partial_reads.append(
                        _literal_and_real(payload["file_path"], workspace))
                else:
                    completed_reads.append(
                        _literal_and_real(payload["file_path"], workspace))
            elif tool in ("Grep", "Glob"):
                target = payload.get("path") or workspace
                opened_dirs.append(_literal_and_real(target, workspace))
                if tool == "Glob":
                    pattern = str(payload.get("pattern") or "")
                    check("glob_pattern_outside",
                          not (os.path.isabs(pattern) or ".." in pattern.split("/")),
                          pattern)
        check("host_part_count_mismatch", len(part_names) == len(usage),
              f"parts={len(part_names)} tool_usage={len(usage)}")
        check("host_part_tool_mismatch", sorted(part_names) == sorted(names),
              f"parts={sorted(part_names)} usage={sorted(names)}")
        check("host_part_call_mismatch", set(part_calls) == set(call_ids),
              f"parts={len(part_calls)} usage={len(set(call_ids))}")
        usage_status = {record["call_id"]: record["status"]
                        for record in records}
        usage_tool = {record["call_id"]: record["name"] for record in records}
        status_mismatch = [cid for cid, (tool, pstatus, _payload) in part_calls.items()
                           if usage_status.get(cid) != pstatus]
        check("host_call_status_mismatch", not status_mismatch,
              ",".join(status_mismatch[:5]))
        tool_mismatch = [cid for cid, (tool, _pstatus, _payload)
                         in part_calls.items() if usage_tool.get(cid) != tool]
        check("host_call_tool_mismatch", not tool_mismatch,
              ",".join(tool_mismatch[:5]))

        selection = _db_rows(
            con, "select data from session_entry where session_id=? "
                 "and type='runtime/model_selection' "
                 "order by time_created desc limit 1", (child,))
        if selection:
            try:
                payload = json.loads(selection[0][0])
            except (TypeError, ValueError):
                payload = {}
            if isinstance(payload, dict) and isinstance(
                    payload.get("modelSelection"), dict):
                model = payload["modelSelection"]
        if require_model_selection:
            check("host_model_selection_missing",
                  bool(model.get("providerId") and model.get("modelId")),
                  "session_entry 缺少 runtime/model_selection")
        # plan 模式会让宿主给子代理注入 Bash（真实记录：planEnabled=true 的独立
        # 只读子会话执行过 `git status`）。有效工具策略因此不再精确等于
        # Read/Grep/Glob；必须在准入阶段证明子会话不是 plan 模式，而不是只在
        # 事后检查有没有实际调用 Bash。
        exec_rows = _db_rows(
            con, "select data from session_entry where session_id=? "
                 "and type='runtime/execution_state' "
                 "order by time_created desc limit 1", (child,))
        exec_state: dict = {"found": False, "mode": "", "plan_enabled": None,
                            "source": "session_entry/runtime/execution_state"}
        if exec_rows:
            # 与 capability 使用同一归一化：行存在但 JSON 非法/非对象时是
            # host_session_state_unknown，而不是“记录缺失”。
            exec_state = host_records.classify_execution_payload(exec_rows[0][0])
        observed["session_mode"] = exec_state.get("mode")
        observed["session_plan_enabled"] = exec_state.get("plan_enabled")
        for code, detail in host_records.execution_state_problems(exec_state):
            check(code, False, detail)
    except AttestError as exc:
        check(exc.code, False, exc.detail)
        return _finish(checks, residual, meta, att, observed, evidence_paths,
                       meta_path, meta_sha, attempt_path, db, checked_at,
                       eligible=False)
    finally:
        con.close()

    # 5.1 rollout：存在时做第三路交叉核对，缺失不是失败
    rollout_checked = False
    rollout_path = os.path.join(
        os.path.expanduser("~"), ".zcode", "cli", "rollout",
        f"model-io-{child}.jsonl")
    if os.path.isfile(rollout_path):
        roll_calls: list[tuple[str, str]] = []
        try:
            with open(rollout_path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    response = row.get("response")
                    if not isinstance(response, dict):
                        continue
                    for call in response.get("toolCalls") or []:
                        if isinstance(call, dict):
                            roll_calls.append((str(call.get("id") or ""),
                                               str(call.get("name") or "")))
        except OSError:
            roll_calls = []
        if roll_calls:
            rollout_checked = True
            roll_ids = [cid for cid, _name in roll_calls]
            check("host_rollout_duplicate_call",
                  len(set(roll_ids)) == len(roll_ids), "rollout 存在重复 call id")
            usage_pairs = {(cid, name) for cid, name in zip(call_ids, names)}
            check("host_rollout_call_mismatch",
                  set(roll_calls) <= usage_pairs,
                  "rollout 中存在 DB 没有的调用（部分保留时只做子集核对）")
    else:
        residual.append("rollout 缺失（仅短时保留）：以 DB part/tool_usage 交叉核对")

    # 6. 证据读取与路径边界
    snapshot_ids = {str(item.get("id") or "") for item in
                    (att.get("evidence_snapshot") or [])
                    if isinstance(item, dict)}
    for entry_id in manifest:
        check("review_manifest_unknown", entry_id in snapshot_ids, entry_id)
    evidence_files: list[dict] = []
    for item in att.get("evidence_snapshot") or []:
        if not isinstance(item, dict):
            check("evidence_entry_invalid", False, "evidence_snapshot 条目不是对象")
            continue
        path = str(item.get("path") or "")
        if not path:
            check("evidence_entry_invalid", False, "evidence_snapshot 条目缺少 path")
            continue
        if scope == "incremental" and str(item.get("id") or "") not in manifest:
            continue
        if not str(item.get("id") or "").strip():
            check("evidence_entry_invalid", False, f"{path} 缺少证据 id")
        if not str(item.get("sha256") or "").strip():
            check("evidence_snapshot_missing_sha", False, path)
        evidence_files.append({"path": path,
                               "sha256": str(item.get("sha256") or "").lower(),
                               "id": str(item.get("id") or "")})
    for path in evidence or []:
        evidence_files.append({"path": str(path), "sha256": "", "id": ""})
    evidence_real: list[str] = []
    evidence_info: list[dict] = []
    for item in evidence_files:
        literal, real = _literal_and_real(item["path"], workspace)
        if not literal:
            check("evidence_path_empty", False, item["path"])
            continue
        in_roots = any(_under(literal, root) for root in allowed_roots)
        check("evidence_outside_roots", in_roots,
              f"{literal} (real={real}) 必须在 workspace/task_dir 内")
        if real != literal and not any(_under(real, root) for root in allowed_roots):
            check("evidence_symlink_escape", False, f"{literal} -> {real}")
        if is_sensitive_path(real):
            check("evidence_sensitive_path", False, real)
        if item["sha256"]:
            if not os.path.isfile(real):
                check("evidence_missing", False, item["path"])
                recorded = ""
            else:
                recorded = sha256_file(real)
                check("evidence_drift", recorded == item["sha256"],
                      f"{item['path']} 内容已变")
        else:
            recorded = sha256_file(real) if os.path.isfile(real) else ""
            check("evidence_missing", bool(recorded), item["path"])
        if real not in evidence_real:
            evidence_real.append(real)
        evidence_info.append({"id": item["id"], "path": real, "sha256": recorded})
    evidence_info.sort(key=lambda entry: entry["path"])
    evidence_paths.extend(entry["path"] for entry in evidence_info)

    read_reals = [real for _literal, real in completed_reads]
    opened_reals = [real for _literal, real in opened_dirs]
    outside_reads_seen: list[str] = []
    observed["outside_reads"] = outside_reads_seen

    for literal, real in partial_reads:
        if real in evidence_real:
            check("evidence_partial_read", False, real)
        if is_sensitive_path(real):
            check("read_sensitive_path", False, real)
        if any(_under(real, root) for root in allowed_roots):
            continue
        code = "read_symlink_escape" if os.path.islink(literal) \
            else "read_outside_workspace"
        check(code, outside_reads == "record", f"部分读取工作区外文件：{real}")
        outside_reads_seen.append(real)
    for path in evidence_real:
        if path in read_reals:
            continue
        parent = os.path.dirname(path)
        if parent in read_reals or parent in opened_reals or path in opened_reals:
            check("evidence_not_read_exact_file", False,
                  f"{path} 只被目录访问，未按文件读取")
        else:
            check("evidence_not_inspected", False, path)

    for literal, real in completed_reads:
        if is_sensitive_path(real):
            check("read_sensitive_path", False, real)
        if real in evidence_real:
            continue
        if any(_under(real, root) for root in allowed_roots):
            continue
        code = "read_symlink_escape" if os.path.islink(literal) \
            else "read_outside_workspace"
        check(code, outside_reads == "record", f"读取工作区外文件：{real}")
        outside_reads_seen.append(real)
        if outside_reads == "record":
            residual.append(f"工作区外读取（已记录，不阻断）：{real}")
    for literal, real in opened_dirs:
        if is_sensitive_path(real):
            check("read_sensitive_path", False, real)
        if any(_under(real, root) for root in allowed_roots):
            continue
        code = "read_symlink_escape" if os.path.islink(literal) \
            else "read_outside_workspace"
        check(code, outside_reads == "record", f"目录访问在工作区外：{real}")
        outside_reads_seen.append(real)
        if outside_reads == "record":
            residual.append(f"工作区外目录访问（已记录，不阻断）：{real}")
    outside_reads_seen.sort()

    # 7. 代码快照漂移
    snapshot = att.get("code_snapshot")
    if not isinstance(snapshot, dict):
        check("code_snapshot_unavailable", False, "code_snapshot 不是对象")
        snapshot = {}
    snapshot_workspace = os.path.realpath(str(
        snapshot.get("workspace") or workspace))
    check("code_snapshot_workspace_mismatch", snapshot_workspace == workspace,
          f"{snapshot_workspace} != {workspace}")
    if not check("code_snapshot_unavailable", snapshot.get("available") is True,
                 str(snapshot.get("reason") or "code_snapshot.available != True")):
        snapshot = {}
    root = os.path.realpath(str(snapshot.get("git_root") or snapshot_workspace))
    # 允许 workspace 自身或包含 workspace 的真实仓库根（工作区可能是仓库子目录），
    # 但拒绝文件系统根等无边界的祖先目录。
    root_ok = _under(root, workspace) or (
        _under(workspace, root)
        and root not in ("/", os.path.realpath(os.path.expanduser("~")))
        and os.path.exists(os.path.join(root, ".git")))
    check("code_snapshot_root_outside", root_ok,
          f"git_root={root} workspace={workspace}")
    digest = str(snapshot.get("digest") or "")
    declared_scope = str(att.get("code_scope_sha256") or "")
    check("code_scope_digest_missing", bool(digest) and bool(declared_scope),
          "code_snapshot.digest 或 attempt.code_scope_sha256 为空")
    if declared_scope and digest:
        check("code_scope_digest_mismatch", declared_scope == digest,
              f"{declared_scope[:16]} != {digest[:16]}")
    raw_files = snapshot.get("files")
    files = raw_files if isinstance(raw_files, list) else []
    check("code_scope_empty", bool(files), "代码快照没有文件")
    drifted: list[str] = []
    recorded_files: list[dict] = []
    subtrees = _submodule_paths(snapshot.get("submodules") or [])
    for item in files:
        if not isinstance(item, dict):
            check("code_scope_file_invalid", False, "文件项不是对象")
            continue
        rel = str(item.get("path") or "")
        kind = str(item.get("kind") or "")
        expected = str(item.get("sha256") or "").lower()
        # 只拒绝真正的父目录穿越（`..` 或 `../x`），不误伤 `..foo.js`。
        rel_parts = rel.replace("\\", "/").split("/")
        if not rel or os.path.isabs(rel) or ".." in rel_parts:
            check("code_scope_path_invalid", False, rel or "<empty>")
            continue
        literal = os.path.abspath(os.path.join(root, rel))
        if not _under(literal, root) \
                or not _under(os.path.realpath(os.path.dirname(literal)), root):
            check("code_scope_path_invalid", False, f"{rel} 越出 git_root")
            continue
        dest = os.path.realpath(literal)
        if kind not in ("file", "symlink", "missing", "other"):
            check("code_scope_file_invalid", False, f"{rel} kind={kind!r}")
            continue
        if kind in ("file", "symlink") and not SHA_RE.match(expected):
            check("code_scope_file_invalid", False, f"{rel} sha256 非法")
            continue
        if kind == "file":
            if os.path.islink(literal):
                check("code_scope_file_invalid", False,
                      f"{rel} kind=file 但实际是符号链接")
                continue
            actual = sha256_file(literal) if os.path.isfile(literal) else ""
        elif kind == "symlink":
            actual = sha256_text(os.readlink(literal)) \
                if os.path.islink(literal) else ""
        elif kind == "missing":
            actual = "" if not os.path.exists(dest) else "unexpected"
            if item.get("sha256"):
                check("code_scope_file_invalid", False,
                      f"{rel} deleted 文件不应有 sha")
        else:
            # code_scope 对子模块 gitlink 等写 kind=other 且不带 sha。要求该路径
            # 确实是已检出的 submodule 目录，否则就是未校验的任意文件。
            if item.get("sha256"):
                check("code_scope_file_invalid", False,
                      f"{rel} kind=other 不应有 sha")
                continue
            matched = rel if rel in subtrees else next(
                (path for path in subtrees if rel == path
                 or rel.startswith(path.rstrip("/") + "/")), "")
            if not matched or not os.path.isdir(dest):
                check("code_scope_file_invalid", False,
                      f"{rel} kind=other 不是已检出的 submodule 目录")
                continue
            if not os.path.exists(os.path.join(dest, ".git")):
                check("code_scope_file_invalid", False,
                      f"{rel} kind=other 缺少 .git（未初始化 submodule）")
                continue
            actual = ""
            residual.append(
                f"代码快照 kind=other 按 submodule 目录处理（提交未重放）：{rel}")
        recorded_files.append({"path": rel, "kind": kind, "sha256": expected})
        if kind != "other" and actual != expected:
            drifted.append(rel)
    check("code_scope_drift", not drifted, ",".join(drifted[:10]))
    # 快照摘要必须能由冻结内容复算（防止 digest 与 files 被分别改写）。
    recomputed = _snapshot_digest(snapshot)
    check("code_snapshot_digest_mismatch", bool(digest) and recomputed == digest,
          f"recomputed={recomputed[:16]} digest={digest[:16]}")
    recorded_files.sort(key=lambda entry: entry["path"])
    submodules = [str(line) for line in snapshot.get("submodules") or []]
    if submodules:
        residual.append("代码快照包含 submodule；本模块未重新执行 git 校验其提交")
    worktrees = snapshot.get("submodule_worktrees") or []
    if not isinstance(worktrees, list):
        check("code_scope_submodule_state_invalid", False,
              "submodule_worktrees 不是数组")
        worktrees = []
    root = os.path.realpath(str(snapshot.get("git_root") or workspace))
    for item in worktrees:
        if not isinstance(item, dict) or not str(item.get("path") or ""):
            check("code_scope_submodule_state_invalid", False,
                  "submodule_worktrees 条目缺少 path")
            continue
        wt_path = os.path.realpath(os.path.join(
            root, str(item.get("path") or "")))
        if os.path.commonpath([root, wt_path]) != root:
            check("code_scope_submodule_state_invalid", False,
                  f"{item.get('path')} 位于 git_root 之外")
        if str(item.get("error") or ""):
            check("code_scope_submodule_state_invalid", False,
                  f"{item['path']}: {item['error']}")
        head = str(item.get("head") or "")
        if head and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
            check("code_scope_submodule_state_invalid", False,
                  f"{item['path']} head 非法：{head[:16]}")
    if worktrees:
        residual.append(
            "submodule 工作区内容（HEAD/tracked/untracked/嵌套）已纳入摘要；"
            "本模块按摘要与结构校验，不逐文件重读")

    def _check_repo_state(items: list, label: str) -> None:
        for item in items:
            if not isinstance(item, dict) or not str(item.get("path") or ""):
                check("code_scope_nested_repo_invalid", False,
                      f"{label} 条目缺少 path")
                continue
            repo_path = os.path.realpath(os.path.join(
                root, str(item.get("path") or "")))
            if os.path.commonpath([root, repo_path]) != root:
                check("code_scope_nested_repo_invalid", False,
                      f"{item.get('path')} 位于 git_root 之外")
            if str(item.get("error") or ""):
                check("code_scope_nested_repo_invalid", False,
                      f"{item.get('path')}: {item['error']}")
            head = str(item.get("head") or "")
            if head and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
                check("code_scope_nested_repo_invalid", False,
                      f"{item.get('path')} head 非法")
            _check_repo_state(list(item.get("nested") or []), label)

    nested_repos = snapshot.get("nested_repos") or []
    if not isinstance(nested_repos, list):
        check("code_scope_nested_repo_invalid", False,
              "nested_repos 不是数组")
    else:
        _check_repo_state(nested_repos, "nested_repos")
        if nested_repos:
            residual.append(
                "未声明 submodule 的嵌套 Git 仓库已递归纳入摘要；"
                "本模块按摘要与结构校验")

    # 8. Reviewer 结论：优先用宿主 outputFile；文件被清理时回退到持久化
    # message/part 重建（与 outputFile 字节一致），两者都有则必须一致。
    out_path = os.path.abspath(os.path.expanduser(
        str(meta.get("outputFile") or "")))
    meta_dir = os.path.dirname(os.path.realpath(meta_path))
    file_present = os.path.isfile(out_path)
    if file_present:
        check("host_output_outside_agent_dir",
              os.path.dirname(os.path.realpath(out_path)) == meta_dir,
              out_path)
    db_output = host_records.assistant_output(db, child) if child else None
    if not file_present and not db_output:
        check("host_output_missing", False,
              f"{out_path}（DB 也没有该会话的 assistant 文本输出）")
        return _finish(checks, residual, meta, att, observed, evidence_info,
                       meta_path, meta_sha, attempt_path, db, checked_at,
                       eligible=False)
    raw_review = b""
    if file_present:
        with open(out_path, "rb") as fh:
            raw_review = fh.read()
        review_sha = hashlib.sha256(raw_review).hexdigest()
        text = raw_review.decode("utf-8", errors="replace")
        output_source = "outputFile"
        if db_output:
            check("host_output_db_mismatch",
                  db_output["sha256"] == review_sha,
                  f"file={review_sha[:16]} db={db_output['sha256'][:16]}")
        else:
            residual.append(
                "宿主 DB 缺少该会话的 assistant 文本；仅以 outputFile 取证")
            output_source = "outputFile_only"
    else:
        review_sha = db_output["sha256"]
        text = db_output["text"]
        output_source = "db.message.part"
        residual.append(
            "outputFile 已被清理；Reviewer 原始输出由 message/part 重建，"
            "文本 SHA 与宿主记录一致")
    expected_output = str(expected_output_sha256 or "").strip().lower()
    if expected_output:
        check("host_output_sha_mismatch",
              expected_output == review_sha,
              f"expected={expected_output[:16]} actual={review_sha[:16]}")
    review = _read_review(text)
    check("review_output_unparseable", review is not None,
          "outputFile 无 JSON 对象")
    review = review if isinstance(review, dict) else {}
    schema = str(review.get("schema") or "")
    check("review_schema_missing", bool(schema), "结论缺少 schema")
    if schema:
        check("review_schema_unknown", schema in REVIEW_SCHEMAS, schema)
    findings = review.get("findings")
    if not isinstance(findings, list):
        check("review_findings_invalid", False,
              f"findings 类型={type(findings).__name__}")
        findings = []
    invalid = [index for index, item in enumerate(findings)
               if not isinstance(item, dict)]
    check("review_findings_invalid", not invalid,
          f"findings[{invalid[0]}] 不是对象" if invalid else "")
    severities = {}
    for index, item in enumerate(findings):
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "").strip().lower()
        severities[index] = severity
        check("review_finding_severity_invalid",
              severity in ("blocking", "major", "minor", "info"),
              f"findings[{index}] severity={severity!r}")
    blocking = [str(item.get("id") or "?") for index, item in enumerate(findings)
                if isinstance(item, dict) and severities.get(index) == "blocking"]
    check("review_blocking_findings", not blocking, ",".join(blocking))
    unverified = review.get("unverified")
    if not isinstance(unverified, list):
        check("review_unverified_invalid", False,
              f"unverified 类型={type(unverified).__name__}")
        unverified = []
    invalid = [index for index, item in enumerate(unverified)
               if not isinstance(item, (str, dict))]
    check("review_unverified_invalid", not invalid,
          f"unverified[{invalid[0]}] 类型非法" if invalid else "")
    changed = review.get("changed_files")
    if not isinstance(changed, list):
        check("review_changed_files", False,
              f"changed_files 类型={type(changed).__name__}")
        changed = []
    check("review_changed_files", not changed, str(changed))
    check("review_attempt_mismatch",
          str(review.get("review_attempt_id") or "") == attempt_id,
          f"结论 attempt={review.get('review_attempt_id')!r}")
    check("review_verdict_not_pass",
          str(review.get("verdict") or "").strip().lower() == "pass",
          str(review.get("verdict")))
    for key in ("task_id", "work_item_id", "execution_result_sha256",
                "plan_id", "dispatch_id"):
        observed_value = str(review.get(key) or "").strip().lower()
        expected_value = str(att.get(key) or "").strip().lower()
        if not expected_value:
            continue
        if not observed_value:
            check("review_binding_missing", False, key)
        elif observed_value != expected_value:
            check("review_binding_mismatch", False, key)
    # contract_sha256 不在 Reviewer 输出契约里（由信封与 ingest 校验）；若它主动
    # 回写则必须一致，但不因缺失而拒绝。
    observed_value = str(review.get("contract_sha256") or "").strip().lower()
    expected_value = str(att.get("contract_sha256") or "").strip().lower()
    if observed_value and expected_value and observed_value != expected_value:
        check("review_binding_mismatch", False, "contract_sha256")
    hard_tokens = set(evidence_real)
    for entry in evidence_real:
        hard_tokens.add(os.path.basename(entry))
    hard_tokens.update(str(entry.get("id") or "") for entry in evidence_info)
    hard_tokens.add(str(att.get("work_item_id") or ""))
    for item in recorded_files:
        rel = item["path"]
        hard_tokens.add(rel)
        hard_tokens.add(os.path.join(root, rel))
        hard_tokens.add(os.path.basename(rel))
    blocking_unverified, soft_unverified = [], []
    for item in unverified:
        text_item = item if isinstance(item, str) else json.dumps(
            item, ensure_ascii=False)
        if _mentions(text_item, hard_tokens):
            blocking_unverified.append(text_item)
        else:
            soft_unverified.append(text_item)
    check("review_unverified_evidence", not blocking_unverified,
          blocking_unverified[0][:200] if blocking_unverified else "")
    residual.extend(soft_unverified)
    verdict = str(review.get("verdict") or "").strip().lower()
    findings_list = review.get("findings")
    findings_count = len(findings_list) if isinstance(findings_list, list) else 0

    return _finish(checks, residual, meta, att, observed, evidence_info,
                   meta_path, meta_sha, attempt_path, db, checked_at,
                   eligible=True, extra={
                       "model_selection": model,
                       "session_trace": session_trace,
                       "session_mode": exec_state.get("mode"),
                       "session_plan_enabled": exec_state.get("plan_enabled"),
                       "tool_counts": {name: names.count(name)
                                       for name in sorted(set(names))},
                       "tools": sorted(set(names)),
                       "executed_tools": sorted(
                           {record["name"] for record in executed_calls}),
                       "denied_tool_calls": [
                           _call_label(record) for record in denied_calls],
                       "denied_tool_counts": dict(sorted(
                           denied_tool_counts.items())),
                       "repeated_denied_tools": repeated_denied,
                       "tool_call_ids": sorted(call_ids),
                       "turn_id": turn_id,
                       "trace_id": only_trace,
                       "code_files_checked": len(recorded_files),
                       "workspace": workspace,
                       "task_dir": task_root,
                       "output_file": out_path,
                       "output_source": output_source,
                       "output_sha256": review_sha,
                       "output_message_id": str(
                           (db_output or {}).get("message_id") or ""),
                       "output_part_ids": list(
                           (db_output or {}).get("part_ids") or []),
                       "output_db_sha256": str(
                           (db_output or {}).get("sha256") or ""),
                       "output_created_at_ms": (db_output or {}).get(
                           "created_at_ms"),
                       "profile_path": profile_real,
                       "code_snapshot": {
                           "digest": digest,
                           "workspace": snapshot_workspace,
                           "git_root": root,
                           "git_head": str(snapshot.get("git_head") or ""),
                           "git_base": str(snapshot.get("git_base") or ""),
                           "files": recorded_files,
                           "submodules": submodules,
                       },
                       "attempt_bindings": {
                           "work_item_id": str(att.get("work_item_id") or ""),
                           "execution_result_sha256":
                               str(att.get("execution_result_sha256") or ""),
                           "contract_sha256":
                               str(att.get("contract_sha256") or ""),
                           "plan_id": str(att.get("plan_id") or ""),
                           "plan_sha256": str(att.get("plan_sha256") or ""),
                           "dispatch_id": str(att.get("dispatch_id") or ""),
                           "criteria_sha256":
                               str(att.get("criteria_sha256") or ""),
                       },
                       "review_sha256": review_sha,
                       "review_verdict": verdict,
                       "review_findings": findings_count,
                       "review_findings_detail": [
                           {"id": str(item.get("id") or ""),
                            "severity": severities.get(index, "")}
                           for index, item in enumerate(findings)
                           if isinstance(item, dict)],
                       "review_schema": schema,
                       "review_scope": scope,
                       "evidence_manifest": manifest,
                       "based_on_review_sha256":
                           str(att.get("based_on_review_sha256") or ""),
                       "rollout_checked": rollout_checked,
                       "failed_tool_calls": failed_calls,
                       "replay_protection": "consume_marker" if consume_marker
                       else "attempt.consumed only",
                       "profile_sha256": profile_sha,
                       "profile": {
                           "name": profile_name,
                           "source": source,
                           "path": profile_real,
                           "sha256": profile_sha,
                           "tools": [str(item) for item in tools]
                           if isinstance(tools, list) else [],
                           "disallowedTools": sorted(
                               str(item) for item in (raw_disallowed or [])),
                           "permissionMode": "" if mode is None else str(mode),
                       },
                       "policy": {
                           "require_target": bool(require_target),
                           "require_executor_identity":
                               bool(require_executor_identity),
                           "require_model_selection": bool(require_model_selection),
                           "outside_reads": outside_reads,
                           "diagnostic": not (require_target
                                              and require_executor_identity
                                              and require_model_selection)
                           or outside_reads != "reject",
                       },
                   })


def verify_stored(record: dict, output_path: str = "") -> dict:
    """仅凭已持久化的 attestation 摘要与原始输出文件复核（不读宿主 DB）。

    用于 Session/message 被宿主清理后的历史审计：原始输出 SHA 必须与摘要一致，
    且该文件仍能解析为合法 review JSON；任何缺失/漂移都 fail-closed。
    """
    checks: list[dict] = []
    summary = record.get("attestation") if isinstance(record, dict) else None
    if not isinstance(summary, dict):
        summary = record if isinstance(record, dict) else {}
    raw = summary.get("raw_output") if isinstance(
        summary.get("raw_output"), dict) else {}
    path = output_path or str(raw.get("path") or "")
    recorded = str(raw.get("sha256") or summary.get("output_sha256") or "")
    review_sha = str(summary.get("review_sha256") or "")
    checks.append({"code": "stored_output_path", "passed": bool(path),
                   "detail": "" if path else "缺少持久化输出路径"})
    data = b""
    if path and os.path.isfile(path):
        with open(path, "rb") as fh:
            data = fh.read()
    checks.append({"code": "stored_output_exists", "passed": bool(data),
                   "detail": "" if data else f"文件不存在或为空：{path}"})
    actual = hashlib.sha256(data).hexdigest() if data else ""
    checks.append({"code": "stored_output_sha",
                   "passed": bool(recorded) and actual == recorded,
                   "detail": f"recorded={recorded[:16]} actual={actual[:16]}"})
    if review_sha:
        checks.append({"code": "review_sha_matches_output",
                       "passed": actual == review_sha,
                       "detail": f"review={review_sha[:16]}"})
    review = None
    if data:
        try:
            review = review_output.parse_review_output(
                data.decode("utf-8", errors="replace"))
        except review_output.ReviewOutputError:
            review = None
    checks.append({"code": "stored_output_parseable",
                   "passed": review is not None, "detail": ""})
    verdict = str((review or {}).get("verdict") or "").lower()
    expected_verdict = str(summary.get("verdict")
                           or summary.get("review_verdict") or "").lower()
    checks.append({"code": "stored_verdict_matches",
                   "passed": bool(verdict) and verdict == expected_verdict,
                   "detail": f"file={verdict} record={expected_verdict}"})
    recorded_findings = summary.get("review_findings_detail")
    raw_findings = (review or {}).get("findings") or []
    if isinstance(recorded_findings, list) and recorded_findings:
        expected_findings = [
            {"id": str(item.get("id") or ""),
             "severity": str(item.get("severity") or "").strip().lower()}
            for item in raw_findings if isinstance(item, dict)]
        checks.append({"code": "stored_findings_match",
                       "passed": recorded_findings == expected_findings,
                       "detail": f"file={len(expected_findings)} "
                                 f"record={len(recorded_findings)}"})
    elif raw_findings:
        # 阶段八之前的 attestation 未保存 findings 摘要：旧记录只能核对 raw
        # 本身，不能声称 findings 已与摘要比对；作为限制披露，不伪造通过。
        checks.append({"code": "stored_findings_match", "passed": True,
                       "detail": "旧记录无 findings 摘要；仅核对 raw 输出"})
    else:
        checks.append({"code": "stored_findings_match", "passed": True,
                       "detail": "双方均无 findings"})
    return {
        "schema": "copilot-g4-stored-verification/v1",
        "ok": all(item["passed"] for item in checks),
        "checks": checks,
        "raw_output_sha256": actual,
        "review_verdict": verdict,
        "attestation_id": str(summary.get("attestation_id") or ""),
        "attempt_id": str(summary.get("attempt_id") or ""),
        "target_sha256": str(summary.get("target_sha256") or ""),
        "limitations": [
            "仅证明持久化输出与其 SHA/结论一致，不重新读取宿主 DB 或 metadata",
            *(["旧 attestation 没有 findings 摘要，findings 只核对 raw 本身"]
              if raw_findings and not (isinstance(recorded_findings, list)
                                       and recorded_findings) else [])],
    }


def _finish(checks: list[dict], residual: list[str], meta: dict, att: dict,
            observed: dict, evidence: list, meta_path: str, meta_sha: str,
            attempt_path: str, db: str, checked_at: str, *,
            eligible: bool, extra: dict | None = None) -> dict:
    rejections = [{"code": item["code"], "detail": item["detail"]}
                  for item in checks if not item["passed"]]
    eligible = bool(eligible) and not rejections
    if evidence and isinstance(evidence[0], dict):
        evidence_core = sorted(
            ({"id": entry.get("id", ""), "path": entry["path"],
              "sha256": entry.get("sha256", "")} for entry in evidence),
            key=lambda entry: entry["path"])
    else:
        evidence_core = [{"id": "", "path": path, "sha256": ""}
                         for path in sorted(evidence)]
    core = {
        "task_id": str(att.get("task_id") or ""),
        "review_attempt_id": str(att.get("review_attempt_id") or ""),
        "attempt_sha256": observed.get("attempt_sha") or (
            sha256_file(attempt_path) if os.path.isfile(attempt_path) else ""),
        "target_sha256": str(att.get("target_sha256") or ""),
        "code_scope_sha256": str(att.get("code_scope_sha256") or ""),
        "metadata_sha256": meta_sha,
        "agent_id": str(meta.get("agentId") or ""),
        "child_session_id": str(meta.get("childSessionId") or ""),
        "parent_session_id": str(meta.get("parentSessionId") or ""),
        "profile_id": str(meta.get("profileId") or ""),
        "created_at": str(meta.get("createdAt") or ""),
        "completed_at": str(meta.get("completedAt") or ""),
        "total_tool_use_count": meta.get("totalToolUseCount"),
        "outside_reads": sorted(observed.get("outside_reads") or []),
        "evidence": evidence_core,
        "checks": checks,
        "rejections": rejections,
        "residual": list(residual),
        "limitations": [
            "metadata.json 与本地 SQLite 没有密码学签名：只能证明取证时多源记录一致，"
            "不能证明同用户进程无法伪造",
            "未重新执行 git：attempt 之后新增的未跟踪文件与 submodule 提交由 G2 "
            "code_scope 负责",
            "路径检查只能拒绝 G4，不能阻止越界内容已经进入模型上下文",
        ],
    }
    core.update(extra or {})
    return {
        "schema": SCHEMA,
        "checked_at": checked_at or _now_iso(),
        "eligible": eligible,
        "code": rejections[0]["code"] if rejections else "",
        "detail": rejections[0]["detail"] if rejections else "",
        "attestation_id": sha256_text(_canonical_json(core).decode("utf-8")),
        "host_db": _db_fingerprint(db),
        **core,
    }


def write_consume_marker(path: str, token: str) -> None:
    """原子创建消费标记；已存在时抛 FileExistsError。"""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)



def _protected_paths(args) -> set[str]:
    """--output / --consume-marker 不得覆盖任何已读输入（含结论/证据/代码/Profile）。"""
    paths: list[str] = [args.metadata, args.attempt, args.db, *args.evidence]
    try:
        meta = _load_json(_metadata_path(args.metadata))
    except AttestError:
        meta = {}
    if isinstance(meta, dict):
        paths.append(str(meta.get("outputFile") or ""))
        snap = meta.get("profileSnapshot")
        if isinstance(snap, dict):
            paths.append(str(snap.get("path") or ""))
    try:
        att = _load_json(os.path.abspath(os.path.expanduser(args.attempt)))
    except AttestError:
        att = {}
    if isinstance(att, dict):
        for item in att.get("evidence_snapshot") or []:
            if isinstance(item, dict):
                paths.append(str(item.get("path") or ""))
        workspace = os.path.realpath(os.path.abspath(
            os.path.expanduser(args.workspace or "")))
        snapshot = att.get("code_snapshot")
        if isinstance(snapshot, dict):
            root = os.path.realpath(str(snapshot.get("git_root")
                                        or snapshot.get("workspace") or workspace))
            for item in snapshot.get("files") or []:
                if isinstance(item, dict) and item.get("path"):
                    paths.append(os.path.join(root, str(item["path"])))
    return {os.path.realpath(os.path.abspath(os.path.expanduser(path)))
            for path in paths if path}


# ── CLI ─────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="G4 宿主取证：只读校验 metadata/SQLite/attempt/证据/输出")
    ap.add_argument("--metadata", required=True, help="metadata.json 或其目录")
    ap.add_argument("--attempt", required=True, help="attempt JSON")
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--expected-target", default="",
                    help="当前 final_evidence_digest(state)")
    ap.add_argument("--evidence", action="append", default=[],
                    help="额外的证据绝对路径；可多次")
    ap.add_argument("--db", default=os.path.expanduser(
        "~/.zcode/cli/db/db.sqlite"))
    ap.add_argument("--task-dir", default="")
    ap.add_argument("--executor-agent", default="")
    ap.add_argument("--executor-session", default="")
    ap.add_argument("--no-model-selection-required", action="store_true")
    ap.add_argument("--outside-reads", choices=("reject", "record"),
                    default="reject")
    ap.add_argument("--consume-marker", default="")
    ap.add_argument("--skip-target-check", action="store_true",
                    help="仅诊断用途：允许不提供 --expected-target")
    ap.add_argument("--skip-executor-check", action="store_true",
                    help="仅诊断用途：允许不提供执行方身份")
    ap.add_argument("--output", default="")
    args = ap.parse_args(argv)
    if args.consume_marker and not args.output:
        print(json.dumps({"schema": SCHEMA, "eligible": False,
                          "code": "consume_marker_requires_output",
                          "detail": "--consume-marker 必须与 --output 同时使用，"
                                    "避免已消费但取证记录未落盘"},
                         ensure_ascii=False))
        return 1
    try:
        protected = _protected_paths(args)
    except (AttestError, OSError, ValueError) as exc:
        code = getattr(exc, "code", "input_unreadable")
        detail = getattr(exc, "detail", str(exc))
        print(json.dumps({"schema": SCHEMA, "eligible": False, "code": code,
                          "detail": detail}, ensure_ascii=False))
        return 1
    outputs = [value for value in (args.output, args.consume_marker) if value]
    for value in outputs:
        if os.path.realpath(os.path.abspath(
                os.path.expanduser(value))) in protected:
            print(json.dumps({"schema": SCHEMA, "eligible": False,
                              "code": "output_path_conflicts",
                              "detail": f"输出路径与输入文件相同：{value}"},
                             ensure_ascii=False))
            return 1
    if len(outputs) == 2 and os.path.realpath(os.path.abspath(
            os.path.expanduser(outputs[0]))) == os.path.realpath(
            os.path.abspath(os.path.expanduser(outputs[1]))):
        print(json.dumps({"schema": SCHEMA, "eligible": False,
                          "code": "output_path_conflicts",
                          "detail": "--output 与 --consume-marker 相同"},
                         ensure_ascii=False))
        return 1
    try:
        record = attest(
            metadata=args.metadata, attempt=args.attempt, task_id=args.task_id,
            workspace=args.workspace, expected_target=args.expected_target,
            evidence=args.evidence, db=args.db, task_dir=args.task_dir,
            executor_agent=args.executor_agent,
            executor_session=args.executor_session,
            require_model_selection=not args.no_model_selection_required,
            require_target=not args.skip_target_check,
            require_executor_identity=not args.skip_executor_check,
            outside_reads=args.outside_reads,
            consume_marker=args.consume_marker)
    except (AttestError, OSError, ValueError) as exc:
        code = getattr(exc, "code", "internal_error")
        detail = getattr(exc, "detail", str(exc))
        print(json.dumps({"schema": SCHEMA, "eligible": False, "code": code,
                          "detail": detail}, ensure_ascii=False))
        return 1
    except Exception as exc:  # 深嵌套 JSON 等非 ValueError 异常也必须失败关闭
        print(json.dumps({"schema": SCHEMA, "eligible": False,
                          "code": "internal_error",
                          "detail": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False))
        return 1

    text = json.dumps(record, ensure_ascii=False, indent=2)
    marker_written = False
    if args.consume_marker and record.get("eligible"):
        try:
            write_consume_marker(args.consume_marker,
                                 str(record.get("attestation_id") or ""))
            marker_written = True
        except FileExistsError:
            print(json.dumps({"schema": SCHEMA, "eligible": False,
                              "code": "attempt_replayed",
                              "detail": f"consume marker 已存在："
                                        f"{args.consume_marker}"},
                             ensure_ascii=False))
            return 1
        except OSError as exc:
            print(json.dumps({"schema": SCHEMA, "eligible": False,
                              "code": "consume_marker_failed",
                              "detail": str(exc)}, ensure_ascii=False))
            return 1
    if args.output:
        target = os.path.abspath(os.path.expanduser(args.output))
        try:
            directory = os.path.dirname(target)
            fd, tmp = __import__("tempfile").mkstemp(
                prefix=".g4-attest-", dir=directory, text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
            os.replace(tmp, target)
        except OSError as exc:
            if marker_written:
                try:
                    os.remove(args.consume_marker)
                except OSError:
                    pass
            try:
                os.unlink(tmp)
            except (OSError, UnboundLocalError, NameError):
                pass
            print(json.dumps({"schema": SCHEMA, "eligible": False,
                              "code": "output_write_failed",
                              "detail": str(exc)}, ensure_ascii=False))
            return 1
    print(text)
    return 0 if record.get("eligible") else 1


if __name__ == "__main__":
    raise SystemExit(main())

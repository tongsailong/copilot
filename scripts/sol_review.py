#!/usr/bin/env python3
"""SOL 零工具单轮复核（v7 唯一的活动远程复核通道）。

Copilot v7 用一次性的 SOL REVIEW 取代原生 Reviewer 多轮复核：

* **零工具是能力级约束**，不靠提示词自律：出站请求体**永不包含** `tools` /
  `functions` / `parallel_tool_calls` 等工具定义，并显式 `tool_choice="none"`；
  上游响应只要出现 `message` / `reasoning` 之外的非良性 output 条目，即判安全
  事件、拒绝消费（退出码 5），绝不代执行。
* **单轮**：一次调用只发一次 HTTP 请求。超时、429、网络错误、截断、身份不符、
  格式异常都不自动重试、不换 Provider、不降级到其他链路；失败原因如实记录，
  由主代理决定是否人工重新发起。
* **模型身份是硬门**：上游 `model` 回显必须与请求完全一致，否则该回复不得进入
  任务状态（退出码 9）。
* **只做 REVIEW**：不恢复 CONSULT / PLAN，不把 SOL 当工程执行或权限证明，
  执行或技术决策。结果只是建议，主代理判断、修复并验证。

本模块不读写任务状态：状态登记、单轮判定与 finding 合并由
`copilot_dispatch.py sol-review` 编排；这里只构建请求、发一次请求、解析与审计。
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

REVIEW_SCHEMA = "copilot-sol-review/v1"
ADMISSION_SCHEMA = "copilot-sol-admission/v1"
MODEL = "gpt-6.1-sol"
BASE_URL = "http://127.0.0.1:8317/v1"
ENDPOINT = "/responses"
REASONING_EFFORT = "high"
API_KEY_ENV = "CUSTOM_127_API_KEY"
ZCODE_PROVIDER_FILE = os.path.expanduser("~/.zcode/v2/provider_config.json")
REASONIX_ENV_FILE = os.path.expanduser("~/.reasonix/.env")

DEFAULT_TIMEOUT = 300.0
DEFAULT_MAX_OUTPUT_TOKENS = 24000
MAX_MATERIALS_CHARS = 400_000
WARN_MATERIALS_CHARS = 200_000

BENIGN_OUTPUT_TYPES = {"message", "reasoning"}
TOOL_KEYS = ("tools", "functions", "parallel_tool_calls", "tool_resources",
             "tool_config")
SEVERITIES = ("blocking", "major", "minor", "info")
VERDICTS = ("pass", "partial", "blocked")

# 允许再次发起 SOL 复核的实质原因（首次复核不需要）。没有这些原因时，同一
# 材料摘要直接复用已有结论，不同材料摘要也拒绝自动进入第二轮。
RETRY_TRIGGERS = {
    "user-request": "用户明确要求再次独立复核",
    "scope-changed": "变更范围发生实质变化",
    "new-risk": "出现新的关键风险，需要独立判断",
    "materials-omitted": "首次材料存在重大遗漏，需要补充后重新判断",
}

_FENCE = re.compile(r"```[ \t]*[A-Za-z0-9_+.-]*[ \t]*\r?\n?(.*?)```", re.S)

EXIT_OK, EXIT_USAGE, EXIT_UPSTREAM, EXIT_TIMEOUT, EXIT_TOOLCALL = 0, 2, 3, 4, 5
EXIT_SHAPE, EXIT_AUDIT, EXIT_QUOTA, EXIT_IDENTITY = 6, 7, 8, 9


class SolError(Exception):
    """SOL 复核错误基类；`exit_code` 是调用方应返回的进程退出码。"""

    exit_code = EXIT_UPSTREAM

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


class SolUsageError(SolError):
    exit_code = EXIT_USAGE


class SolUpstreamError(SolError):
    exit_code = EXIT_UPSTREAM


class SolTimeoutError(SolError):
    exit_code = EXIT_TIMEOUT


class SolToolCallError(SolError):
    exit_code = EXIT_TOOLCALL


class SolResponseError(SolError):
    exit_code = EXIT_SHAPE


class SolAuditError(SolError):
    exit_code = EXIT_AUDIT


class SolQuotaError(SolError):
    exit_code = EXIT_QUOTA


class SolIdentityError(SolError):
    exit_code = EXIT_IDENTITY


# ── 凭据（只返回值，绝不回显、绝不写日志） ─────────────────────────────────
def _read_env_value(path: str, key: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                if name.strip() != key:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                return value or None
    except FileNotFoundError:
        return None
    return None


def _is_local_cpa_url(raw: str) -> bool:
    """严格判定本机 CPA 端点：协议 http(s)、回环主机、无 userinfo。"""
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username or parsed.password:
        return False
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        return False
    return port == 8317


def _read_zcode_provider_key(path: str = "") -> str | None:
    """从既有 ZCode Provider 配置中取本机 CPA（127.0.0.1:8317）的 apiKey。

    只接受 baseUrl 严格指向本机 CPA 的规则，不读取、不迁移、不改写其他 Provider。
    不做子串匹配（远端 URL 含 8317 字符串或描述含 "cpa" 都不算本机 CPA）。
    """
    target = path or ZCODE_PROVIDER_FILE
    try:
        with open(target, "r", encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, ValueError):
        return None
    cfg = config.get("config") if isinstance(config, dict) else None
    rules = ((cfg or {}).get("providerConfigRules") or {}).get("providerRules") or []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule_config = rule.get("config") or {}
        base_url = str((rule_config.get("api") or {}).get("baseUrl") or "")
        if not _is_local_cpa_url(base_url):
            continue
        key = str((rule_config.get("access") or {}).get("apiKey") or "").strip()
        if key:
            return key
    return None


def resolve_api_key(env_name: str = API_KEY_ENV) -> str:
    """沿用已有授权配置：环境变量 → Reasonix .env → ZCode 127 Provider。"""
    key = (os.environ.get(env_name)
           or _read_env_value(REASONIX_ENV_FILE, env_name)
           or _read_zcode_provider_key())
    if not key:
        raise SolUsageError(
            "sol_credentials_missing",
            f"没有找到本机 CPA 的 {env_name} 凭据（环境变量 / "
            f"{REASONIX_ENV_FILE} / {ZCODE_PROVIDER_FILE} 均为空）。"
            "不新增 Provider、账户或密钥；请先在既有授权配置中补齐。")
    return key


def resolve_base_url() -> str:
    """只使用既有本机 CPA 出口；不提供任何外部 Provider 回退。"""
    raw = str(os.environ.get("COPILOT_SOL_BASE_URL") or BASE_URL).strip()
    if not _is_local_cpa_url(raw):
        raise SolUsageError(
            "sol_route_not_local",
            f"SOL 只允许本机 CPA 出口（http(s)://127.0.0.1:8317，"
            f"无 userinfo），拒绝 {raw!r}；不新增或切换 Provider。")
    return raw.rstrip("/")


# ── 请求构建与零工具不变量 ─────────────────────────────────────────────────
INSTRUCTIONS = """你是 Copilot 的独立复核者（SOL REVIEW）。你只能基于本轮材料做一次性审查：
- 你没有、也不能使用任何工具；不能读取文件、执行命令、访问网络或修改代码。
- 只报告材料中有依据的问题；不得虚构文件、行号、测试结果或工程事实。
- 无法从材料确认的内容写入 unverified，不得假装已经检查。
- 只输出一个 JSON 对象（可放在单个 ```json 围栏内），不要输出其他文字。

JSON 结构：
{
  "schema": "copilot-sol-review/v1",
  "verdict": "pass" | "partial" | "blocked",
  "summary": "总体结论",
  "findings": [
    {"id": "F1", "severity": "blocking" | "major" | "minor" | "info",
     "file": "相对路径", "line": 123,
     "summary": "问题", "evidence": "材料中的依据（文件名/行号/片段）",
     "suggestion": "修复建议"}
  ],
  "unverified": ["无法从材料确认的事项"],
  "materials_reviewed": ["实际读过的材料章节"]
}

判定口径：
- pass：没有 blocking/major，且没有影响结论的未验证项；
- partial：存在问题或不确定性，需要主代理核实/修复；
- blocked：材料不足以判断。
findings 按严重度排序；没有问题用空数组。你的结论是建议，不是工程决策。"""


def build_payload(materials: str, *, model: str = MODEL,
                  effort: str = REASONING_EFFORT,
                  max_output_tokens: int | None = DEFAULT_MAX_OUTPUT_TOKENS,
                  instructions: str = INSTRUCTIONS) -> dict:
    """构建 Responses API 请求体；工具字段一律不存在。"""
    if effort not in ("low", "medium", "high"):
        raise SolUsageError("sol_effort_invalid",
                            f"推理强度 {effort!r} 不受支持。")
    payload = {
        "model": model,
        "input": materials,
        "store": False,
        "stream": False,
        "tool_choice": "none",
        "reasoning": {"effort": effort},
    }
    if max_output_tokens is not None:
        payload["max_output_tokens"] = int(max_output_tokens)
    if instructions.strip():
        payload["instructions"] = instructions
    assert_zero_tool(payload)
    return payload


def assert_zero_tool(payload: object, _path: str = "payload") -> None:
    """能力级零工具校验：任何工具定义键都视为安全事件。"""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key) in TOOL_KEYS:
                raise SolUsageError(
                    "sol_tools_in_payload",
                    f"请求体 {_path}.{key} 含工具定义；零工具不变量被破坏。")
            assert_zero_tool(value, f"{_path}.{key}")
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            assert_zero_tool(value, f"{_path}[{index}]")
    if _path == "payload" and payload.get("tool_choice") != "none":
        raise SolUsageError(
            "sol_tool_choice_invalid",
            "请求体必须显式 tool_choice=\"none\"。")


# ── 响应校验 ───────────────────────────────────────────────────────────────
def extract_text(payload: dict) -> str:
    parts = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for chunk in item.get("content") or []:
            if isinstance(chunk, dict) and chunk.get("type") == "output_text":
                parts.append(chunk.get("text") or "")
    return "".join(parts).strip()


def find_unexpected_output(payload: dict) -> list[dict]:
    """`message` / `reasoning` 之外的一切 output 类型都按非良性处理。"""
    found = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict) and item.get("type") not in BENIGN_OUTPUT_TYPES:
                found.append({"type": item.get("type"),
                              "name": item.get("name")})
    if payload.get("tool_choice") not in (None, "none"):
        found.append({"type": "unexpected_tool_choice",
                      "name": str(payload.get("tool_choice"))})
    return found


def response_problem(payload: dict) -> str | None:
    status = str(payload.get("status") or "").lower()
    err = payload.get("error")
    if err:
        detail = err.get("message") if isinstance(err, dict) else str(err)
        return f"response error: {detail}"
    if not status:
        return "response status missing"
    if status not in ("completed", "succeeded"):
        inc = payload.get("incomplete_details")
        return (f"response status={status}"
                + (f" incomplete_details={inc}" if inc else ""))
    if payload.get("incomplete_details"):
        return f"response incomplete_details={payload.get('incomplete_details')}"
    output = payload.get("output")
    if not isinstance(output, list) or not output:
        return "response output missing"
    for item in output:
        if not isinstance(item, dict):
            return f"malformed output item: {type(item).__name__}"
        if item.get("type") != "message":
            continue
        message_status = str(item.get("status") or "").lower()
        if message_status and message_status != "completed":
            return f"message status={message_status}"
        content = item.get("content")
        if content is not None and not isinstance(content, list):
            return "malformed message content"
        for chunk in content or []:
            if not isinstance(chunk, dict):
                return f"malformed content item: {type(chunk).__name__}"
            if chunk.get("type") == "output_text" and not isinstance(
                    chunk.get("text", ""), str):
                return "output_text is not a string"
    return None


def _quota_signal(status: int, payload: object) -> dict | None:
    code = ""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "")
        else:
            code = str(payload.get("code") or "")
    if status == 429 or code in ("usage_limit_reached", "model_cooldown"):
        return {"status": status, "code": code or "unknown"}
    return None


# ── 输出解析（严格 JSON，允许单个围栏；不做叙述猜测） ──────────────────────
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
        raise SolResponseError("sol_output_unparseable", str(exc)) from None
    if not isinstance(value, dict):
        raise SolResponseError("sol_output_not_object",
                               f"top level is {type(value).__name__}")
    return value


def _balanced_objects(text: str) -> list[str]:
    """提取文本中所有顶层平衡的花括号对象（字符串内括号不计）。"""
    out: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    out.append(text[start:index + 1])
                    start = -1
    return out


def _parse_json_text(cleaned: str) -> dict:
    try:
        return _loads(cleaned)
    except SolResponseError as direct:
        if direct.code == "sol_output_not_object":
            raise
        first_error = direct
    fences = list(_FENCE.finditer(cleaned))
    if len(fences) == 1:
        outside = _FENCE.sub("", cleaned).strip()
        if not outside:
            return _loads(fences[0].group(1).strip())
    # 容忍模型在 JSON 前后写少量说明：只接受恰好一个带本契约 schema 的对象。
    candidates = []
    for blob in _balanced_objects(cleaned):
        try:
            value = _loads(blob)
        except SolResponseError:
            continue
        if value.get("schema") == REVIEW_SCHEMA:
            candidates.append(value)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise SolResponseError("sol_output_ambiguous",
                               f"{len(candidates)} review objects")
    raise SolResponseError("sol_output_unparseable", first_error.detail)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _line(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _string_list(value: object, name: str) -> list[str]:
    """严格字符串数组：非数组或含非字符串元素一律拒绝，不静默丢弃。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise SolResponseError(f"sol_{name}_invalid",
                               f"{name} 必须是字符串数组")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise SolResponseError(
                f"sol_{name}_invalid",
                f"{name} 含非字符串元素：{type(item).__name__}")
        if item.strip():
            out.append(item.strip())
    return out


def _optional_string(item: dict, key: str, finding_id: str) -> str:
    value = item.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SolResponseError(
            "sol_finding_field_invalid",
            f"{finding_id}: {key} 必须是字符串")
    return value.strip()


def normalize_review(data: dict) -> dict:
    """校验并归一化 SOL 输出；PASS 与 blocking/major/unverified 冲突时降级。"""
    if data.get("schema") != REVIEW_SCHEMA:
        raise SolResponseError("sol_schema_mismatch",
                               f"schema={data.get('schema')!r}")
    verdict = _text(data.get("verdict")).lower()
    if verdict not in VERDICTS:
        raise SolResponseError("sol_verdict_invalid", repr(data.get("verdict")))
    findings = data.get("findings")
    if findings is None:
        findings = []
    if not isinstance(findings, list) or any(
            not isinstance(item, dict) for item in findings):
        raise SolResponseError("sol_findings_invalid", "findings 必须是对象数组")
    normalized: list[dict] = []
    used_ids: set[str] = set()
    for index, item in enumerate(findings, start=1):
        raw_id = item.get("id")
        if raw_id is not None and not isinstance(raw_id, str):
            raise SolResponseError(
                "sol_finding_field_invalid", f"第 {index} 项 id 必须是字符串")
        severity = _text(item.get("severity")).lower()
        if severity not in SEVERITIES:
            raise SolResponseError(
                "sol_severity_invalid",
                f"{item.get('id') or index}: {item.get('severity')!r}")
        finding_id = _text(raw_id).upper() or f"F{index}"
        if finding_id in used_ids:
            finding_id = f"F{index}"
        used_ids.add(finding_id)
        normalized.append({
            "id": finding_id,
            "severity": severity,
            "file": _optional_string(item, "file", finding_id),
            "line": _line(item.get("line")),
            "summary": _optional_string(item, "summary", finding_id),
            "evidence": _optional_string(item, "evidence", finding_id),
            "suggestion": _optional_string(
                item, "suggestion", finding_id)
            or _optional_string(item, "suggest", finding_id),
        })
    normalized.sort(key=lambda item: SEVERITIES.index(item["severity"]))
    unverified = _string_list(data.get("unverified"), "unverified")
    materials_reviewed = _string_list(
        data.get("materials_reviewed"), "materials_reviewed")
    adjusted = []
    if verdict == "pass":
        if any(item["severity"] in ("blocking", "major") for item in normalized):
            verdict = "partial"
            adjusted.append("pass_with_blocking_or_major")
        elif unverified:
            verdict = "partial"
            adjusted.append("pass_with_unverified")
    return {
        "verdict": verdict,
        "summary": _text(data.get("summary")),
        "findings": normalized,
        "unverified": unverified,
        "materials_reviewed": materials_reviewed,
        "verdict_adjusted": adjusted,
    }


def parse_review_output(text: str) -> dict:
    cleaned = str(text or "").lstrip("\ufeff").strip()
    if not cleaned:
        raise SolResponseError("sol_output_empty", "输出为空")
    return normalize_review(_parse_json_text(cleaned))


# ── 发送（一次，不重试） ───────────────────────────────────────────────────
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """3xx 一律失败：避免把 Authorization 转发到重定向目标、保证严格一次请求。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post_once(url: str, key: str, body: dict, timeout: float) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except (OSError, http.client.HTTPException):
            raw = b""
        payload = None
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            pass
        quota = _quota_signal(exc.code, payload)
        if quota:
            raise SolQuotaError(
                "sol_quota",
                f"上游限流/冷却（HTTP {exc.code}，code={quota['code']}）；"
                "未自动重试、未切换 Provider。") from None
        raise SolUpstreamError(
            "sol_upstream_error",
            f"上游 HTTP {exc.code}: "
            f"{raw.decode('utf-8', errors='replace')[:300]}") from None
    except (socket.timeout, TimeoutError) as exc:
        raise SolTimeoutError(
            "sol_timeout",
            f"请求在 {timeout:g}s 内未完成；结果未知，未自动重试。") from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise SolTimeoutError(
                "sol_timeout",
                f"请求在 {timeout:g}s 内未完成；结果未知，未自动重试。") from None
        raise SolUpstreamError("sol_transport_error", str(reason)) from None
    except (http.client.HTTPException, OSError) as exc:
        # 响应读到一半中断、连接重置、Content-Length 不足等：请求已发出，
        # 结果未知。显式映射为 SolError，确保进入失败审计与状态登记。
        raise SolUpstreamError(
            "sol_transport_error",
            f"{type(exc).__name__}: {exc}") from None
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise SolResponseError("sol_response_json_invalid", str(exc)) from None
    if not isinstance(payload, dict):
        raise SolResponseError("sol_response_not_object",
                               type(payload).__name__)
    payload["_http_status"] = status
    return payload


def _usage_summary(usage: object) -> dict | None:
    """保留顶层数值用量与 reasoning tokens，去掉冗长的 per-item attribution。"""
    if not isinstance(usage, dict):
        return None
    summary = {key: value for key, value in usage.items()
               if isinstance(value, (int, float)) and not isinstance(value, bool)}
    details = usage.get("output_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), int):
        summary["reasoning_tokens"] = details["reasoning_tokens"]
    return summary


def run_review(materials: str, *, audit_log: str = "",
               model: str = MODEL, effort: str = REASONING_EFFORT,
               max_output_tokens: int | None = DEFAULT_MAX_OUTPUT_TOKENS,
               timeout: float = DEFAULT_TIMEOUT,
               transport=None, api_key: str | None = None,
               base_url: str | None = None) -> dict:
    """发起一次 SOL REVIEW 并返回结构化结果；任何异常都显式失败。"""
    if not isinstance(materials, str) or not materials.strip():
        raise SolUsageError("sol_materials_empty", "审查材料为空。")
    if len(materials) > MAX_MATERIALS_CHARS:
        raise SolUsageError(
            "sol_materials_too_large",
            f"审查材料 {len(materials)} 字符超过上限 {MAX_MATERIALS_CHARS}；"
            "请主代理整理必要材料后重试，不静默截断。")
    payload = build_payload(
        materials, model=model, effort=effort,
        max_output_tokens=max_output_tokens)
    key = api_key if api_key is not None else resolve_api_key()
    url = (base_url or resolve_base_url()).rstrip("/") + ENDPOINT
    body_sha = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    started = time.monotonic()
    audit_base = {
        "ts": _now_iso(),
        "mode": "REVIEW",
        "model_requested": model,
        "reasoning_effort": effort,
        "materials_sha256": hashlib.sha256(
            materials.encode("utf-8")).hexdigest(),
        "materials_chars": len(materials),
        "request_sha256": body_sha,
        "zero_tool": True,
        "tools_sent": False,
        "tool_choice": "none",
        "attempts": 1,
    }
    try:
        response = (transport or _post_once)(url, key, payload, timeout)
    except SolError as exc:
        _audit(audit_log, {**audit_base, "ok": False,
                           "error": exc.code, "detail": exc.detail,
                           "elapsed_ms": int((time.monotonic() - started) * 1000)})
        raise
    elapsed_ms = int((time.monotonic() - started) * 1000)
    status = response.pop("_http_status", 0)
    quota = _quota_signal(int(status or 0), response)
    if quota:
        exc = SolQuotaError(
            "sol_quota",
            f"HTTP 200 携带限流/冷却（code={quota['code']}）；未自动重试。")
        _audit(audit_log, {**audit_base, "ok": False, "error": exc.code,
                           "http_status": status, "elapsed_ms": elapsed_ms})
        raise exc
    try:
        unexpected = find_unexpected_output(response)
        problem = response_problem(response)
        text = extract_text(response) if not problem else ""
    except (AttributeError, TypeError, ValueError, KeyError) as exc:
        _audit(audit_log, {**audit_base, "ok": False,
                           "error": "sol_malformed_response",
                           "detail": f"{type(exc).__name__}: {exc}",
                           "http_status": status, "elapsed_ms": elapsed_ms})
        raise SolResponseError("sol_malformed_response", str(exc)) from None
    model_resolved = response.get("model")
    identity_ok = (isinstance(model_resolved, str)
                   and model_resolved.strip() == model)
    usage_raw = response.get("usage") if isinstance(response.get("usage"), dict) else None
    usage = _usage_summary(usage_raw)
    if unexpected:
        exc = SolToolCallError(
            "sol_unexpected_output_item",
            f"上游返回非良性 output 条目 {unexpected}；已拒绝处理且未执行。")
        _audit(audit_log, {**audit_base, "ok": False, "error": exc.code,
                           "detail": json.dumps(unexpected, ensure_ascii=False),
                           "http_status": status,
                           "model_resolved": model_resolved,
                           "usage": usage, "elapsed_ms": elapsed_ms})
        raise exc
    if not identity_ok:
        exc = SolIdentityError(
            "sol_model_identity_mismatch",
            f"请求模型 {model!r}，上游回显 {model_resolved!r}；该回复不得被消费。")
        _audit(audit_log, {**audit_base, "ok": False, "error": exc.code,
                           "http_status": status,
                           "model_resolved": model_resolved,
                           "usage": usage, "elapsed_ms": elapsed_ms})
        raise exc
    if problem:
        exc = SolResponseError("sol_response_incomplete", problem)
        _audit(audit_log, {**audit_base, "ok": False, "error": exc.code,
                           "detail": problem, "http_status": status,
                           "model_resolved": model_resolved,
                           "usage": usage, "elapsed_ms": elapsed_ms})
        raise exc
    text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        parsed = parse_review_output(text)
    except SolResponseError as exc:
        _audit(audit_log, {**audit_base, "ok": False, "error": exc.code,
                           "detail": exc.detail, "http_status": status,
                           "model_resolved": model_resolved,
                           "reply_sha256": text_sha,
                           "reply_chars": len(text),
                           "usage": usage, "elapsed_ms": elapsed_ms})
        raise SolResponseError(exc.code, exc.detail) from None
    counts = {severity: sum(1 for item in parsed["findings"]
                            if item["severity"] == severity)
              for severity in SEVERITIES}
    result = {
        "schema": REVIEW_SCHEMA,
        "model": model,
        "model_resolved": model_resolved,
        "reasoning_effort": effort,
        "zero_tool": True,
        "attempts": 1,
        "materials_sha256": audit_base["materials_sha256"],
        "materials_chars": len(materials),
        "request_sha256": body_sha,
        "reply_text": text,
        "reply_sha256": text_sha,
        "reply_chars": len(text),
        "usage": usage,
        "usage_available": usage_raw is not None,
        "latency_ms": elapsed_ms,
        "verdict": parsed["verdict"],
        "summary": parsed["summary"],
        "findings": parsed["findings"],
        "unverified": parsed["unverified"],
        "materials_reviewed": parsed["materials_reviewed"],
        "verdict_adjusted": parsed["verdict_adjusted"],
        "findings_count": counts,
        "completed_at": _now_iso(),
    }
    _audit(audit_log, {**audit_base, "ok": True,
                       "http_status": status,
                       "model_resolved": model_resolved,
                       "reply_sha256": text_sha,
                       "reply_chars": len(text),
                       "verdict": parsed["verdict"],
                       "findings_count": counts,
                       "usage": usage, "elapsed_ms": elapsed_ms})
    return result


# ── 审计（只写脱敏元数据；正文由调用方存入任务目录） ──────────────────────
def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _audit(path: str, record: dict) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False,
                                sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        raise SolAuditError("sol_audit_write_failed",
                            f"无法写入审计日志 {path}：{exc}") from None


# ── 材料、单轮判定与准入 ───────────────────────────────────────────────────
def build_materials(state: dict, materials: str) -> str:
    """把任务合同/完成条件与主代理提供的材料拼成一次完整审查上下文。"""
    criteria = ((state.get("contract") or {}).get("criteria") or [])
    lines = [
        "# Copilot SOL REVIEW 材料",
        "",
        f"- 任务：{state.get('task_id') or '(未命名)'}",
        f"- 风险：{state.get('risk') or 'medium'}",
        f"- 执行后端：{state.get('execution_backend') or 'legacy'}",
        f"- 合同指纹：{state.get('contract_sha256') or '(未登记)'}",
        "",
        "## 任务目标",
        "",
        str(state.get("goal") or "").strip() or "(未登记)",
        "",
        "## 完成条件",
        "",
    ]
    if criteria:
        for item in criteria:
            cid = item.get("criterion_id") if isinstance(item, dict) else ""
            status = item.get("status") if isinstance(item, dict) else ""
            desc = item.get("description") if isinstance(item, dict) else ""
            lines.append(f"- {cid} [{status}] {desc}")
    else:
        lines.append("(未登记完成条件)")
    lines += [
        "",
        "## 合同正文",
        "",
        str((state.get("contract") or {}).get("text") or "").strip() or "(未登记)",
        "",
        "## 主代理提供的审查材料",
        "",
        materials.strip(),
        "",
        "## 复核要求",
        "",
        "一次性审查以上材料：只报告有材料依据的问题，未知项写入 unverified；"
        "按契约输出单个 JSON 对象。",
        "",
    ]
    return "\n".join(lines)


def review_digest(materials_text: str) -> str:
    """完整提示词的摘要（审计用，包含当前条件状态）。"""
    return hashlib.sha256(materials_text.encode("utf-8")).hexdigest()


def review_subject_sha256(state: dict, materials: str) -> str:
    """单轮判定用的稳定主体摘要：材料 + 目标/合同/条件描述。

    条件从 pending 变 met 属于流程状态变化，不是新的审查对象；同一材料重复调用
    应复用已完成的结论，避免上下文恢复后重复付费。目标、合同正文、条件描述或
    材料本身变化时摘要变化，才需要显式的再次复核原因。
    """
    contract = state.get("contract") or {}
    criteria = []
    for item in contract.get("criteria") or []:
        if not isinstance(item, dict):
            continue
        criteria.append([str(item.get("criterion_id") or ""),
                         str(item.get("description") or "")])
    blob = json.dumps({
        "goal": str(state.get("goal") or ""),
        "contract": str(contract.get("text") or ""),
        "criteria": criteria,
        "materials": materials,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def completed_reviews(state: dict) -> list[dict]:
    return [item for item in state.get("sol_reviews") or []
            if isinstance(item, dict) and item.get("status") == "completed"]


def single_round_decision(state: dict, materials_sha256: str, *,
                          force: bool = False, reason: str = "",
                          trigger: str = "") -> dict:
    """单轮判定：同一材料直接复用；任何再次复核都必须有实质原因。"""
    reviews = completed_reviews(state)
    same = [item for item in reviews
            if str(item.get("materials_sha256") or "") == materials_sha256]
    if same:
        return {"action": "reuse", "review": same[-1],
                "note": "相同审查材料已完成 SOL 复核；直接复用结论，不重复调用。"}
    if not reviews:
        return {"action": "call", "note": "首次 SOL 复核。"}
    trigger_value = str(trigger or "").strip().lower()
    reason_value = str(reason or "").strip()
    if not force:
        return {"action": "refuse", "code": "sol_review_requires_reason",
                "note": ("本任务已有 SOL 复核结论。单轮原则要求再次复核必须有明确原因；"
                         "使用 --force --trigger <原因类型> --reason <说明>。")}
    if trigger_value not in RETRY_TRIGGERS:
        return {"action": "refuse", "code": "sol_review_trigger_invalid",
                "note": ("--trigger 只能是 "
                         + ", ".join(RETRY_TRIGGERS) + "。")}
    if not reason_value:
        return {"action": "refuse", "code": "sol_review_reason_missing",
                "note": "再次复核必须用 --reason 说明具体原因。"}
    return {"action": "call", "trigger": trigger_value, "reason": reason_value,
            "note": f"允许再次复核：{RETRY_TRIGGERS[trigger_value]}。"}


def admission(*, risk: str, triggers: list[str] | None = None,
              reason: str = "") -> dict:
    """SOL 复核准入建议（只读建议，不是门禁）。

    简单/普通任务由主代理自检闭环；多模块改造、数据链路、安全与生产高风险、
    用户明确要求时建议调用一次。SOL 是 advisory，不构成收口门禁。
    """
    import review_policy as RP

    risk_value = str(risk or "").strip().lower()
    if risk_value not in ("low", "medium", "normal", "high"):
        raise SolUsageError("sol_risk_invalid", f"风险等级 {risk_value!r} 非法。")
    tier = "medium" if risk_value == "normal" else risk_value
    requested = []
    for item in triggers or []:
        value = str(item or "").strip().lower()
        if not value:
            continue
        if value not in RP.ADMISSION_TRIGGERS and value not in RETRY_TRIGGERS:
            raise SolUsageError(
                "sol_trigger_invalid",
                "未知触发因素：" + value + "；可选："
                + ", ".join(sorted(set(RP.ADMISSION_TRIGGERS) | set(RETRY_TRIGGERS)))
                + "。")
        if value not in requested:
            requested.append(value)
    if tier == "high":
        decision, scope = "recommended", "high-risk"
        detail = ("高风险/安全/生产变更建议在最终验证后调用一次 SOL 复核；"
                  "SOL 只提供独立审查意见，不证明宿主权限，也不构成收口门禁。")
    elif requested:
        decision, scope = "recommended", "triggered"
        detail = "声明了实质风险因素，建议调用一次 SOL 复核；主代理自行决定。"
    elif tier == "low":
        decision, scope = "not_needed", "none"
        detail = "低风险改动（文案/样式/简单修复/小范围既有模式）：主代理自检闭环，无需 SOL。"
    else:
        decision, scope = "optional", "normal"
        detail = "普通功能开发：按需调用一次 SOL；不强制、不设自动第二轮。"
    return {
        "schema": ADMISSION_SCHEMA,
        "risk": risk_value,
        "tier": tier,
        "decision": decision,
        "scope": scope,
        "triggers": requested,
        "reason": str(reason or "")[:1000],
        "single_round": True,
        "detail": detail,
    }

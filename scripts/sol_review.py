#!/usr/bin/env python3
"""SOL 一次性调用客户端：REVIEW / CONSULT / PLAN（零工具、单轮、建议性）。

Copilot 的 SOL 通道只承担**认知辅助**，不承担决策或执行：

* **零工具是能力级约束**，不靠提示词自律：出站请求体**永不包含** `tools` /
  `functions` / `parallel_tool_calls` 等工具定义，并显式 `tool_choice="none"`；
  上游响应只要出现 `message` / `reasoning` 之外的非良性 output 条目，即判安全
  事件、拒绝消费（退出码 5），绝不代执行。
* **单轮**：一次调用只发一次 HTTP 请求。超时、429、网络错误、截断、身份不符、
  格式异常都不自动重试、不换 Provider、不降级到其他链路；失败原因如实记录，
  由主代理决定是否人工重新发起。
* **模型与推理强度是硬门**：统一 `gpt-6.2-sol` + `reasoning.effort="high"`，
  上游 `model` 回显必须完全一致，`reasoning.effort` / `reasoning.mode` 回显
  也必须与请求一致（`standard`）；任何不一致都拒绝消费（退出码 9），
  不允许静默降级到 `medium`/`low` 或其他模型。
* **三种只读模式**：REVIEW（独立复核，输出结构化 findings）、CONSULT（问题
  分析、方案比较与风险识别）、PLAN（规划、依赖与替代路线建议）。三者都是
  advisory：不修改文件、不执行命令、不读写台账、不调度子代理，也不构成收口
  门禁；工程执行与技术决策始终由 ZCode 主代理负责。
* **按需调用**：由主代理判断是否有增量价值；没有 DECIDE 模式，没有强制调用，
  没有固定轮次。

本模块不写任务状态：状态登记、单轮判定与 finding 合并由
`copilot_dispatch.py sol-review` / `sol-advisory` 编排；这里只构建请求、
发一次请求、解析与审计。单轮判定需要读取统一 SOL 记录时，通过
`governance.iter_sol_calls` 只读复用，合并逻辑只有一份。
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

from lib_common import redact as _redact_text

REVIEW_SCHEMA = "copilot-sol-review/v1"
ADVISORY_SCHEMA = "copilot-sol-advisory/v1"
ADMISSION_SCHEMA = "copilot-sol-admission/v1"
MODEL = "gpt-6.2-sol"
BASE_URL = "http://127.0.0.1:8317/v1"
ENDPOINT = "/responses"
REASONING_EFFORT = "high"
REASONING_MODE = "standard"
# 所有 SOL 模式统一 high：不允许按角色降级，也不允许缺失时回退。
PINNED_EFFORTS = ("high",)
SOL_MODES = ("CONSULT", "PLAN", "REVIEW")
ADVISORY_MODES = ("CONSULT", "PLAN")
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
# GPT-6.2 Responses API 对推理模型不接受采样参数；SOL 从不发送这些字段。
UNSUPPORTED_PARAMS = ("temperature", "top_p", "top_logprobs", "logprobs",
                      "frequency_penalty", "presence_penalty")
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

# 错误详情进入 stderr、审计与任务状态：先脱敏再限长，绝不回显凭据或完整请求体。
MAX_ERROR_DETAIL = 500


def _safe_detail(value: object, limit: int = MAX_ERROR_DETAIL) -> str:
    """脱敏并限长错误详情；用于上游正文、异常文本与非受控字段。"""
    text = _redact_text(str(value or ""))
    if len(text) > limit:
        text = text[:limit] + "…(truncated)"
    return text


def _safe_url(raw: object) -> str:
    """只保留 scheme/host/port；丢弃 userinfo、路径、查询参数与片段。"""
    try:
        parsed = urllib.parse.urlsplit(str(raw or ""))
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return "<invalid-url>"
    return f"{parsed.scheme}://{parsed.hostname or ''}{port}"


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


class SolReasoningError(SolError):
    """上游回显的推理强度/模式与请求不一致：Provider 覆盖参数，拒绝消费。"""

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
            f"无 userinfo），拒绝 {_safe_url(raw)}；不新增或切换 Provider。")
    return raw.rstrip("/")


# ── 请求构建与零工具不变量 ─────────────────────────────────────────────────
_COMMON_RULES = """- 你没有、也不能使用任何工具；不能读取文件、执行命令、访问网络或修改代码。
- 只基于本轮材料作答；不得虚构文件、行号、测试结果或工程事实。
- 无法从材料确认的内容写入 unverified，不得假装已经检查。
- 你的输出只是建议：ZCode 主代理负责工程执行与技术决策，不要要求或代替它执行。
- 只输出一个 JSON 对象（可放在单个 ```json 围栏内），不要输出其他文字。"""

REVIEW_INSTRUCTIONS = """你是 Copilot 的独立复核者（SOL REVIEW）。你只能基于本轮材料做一次性审查：
""" + _COMMON_RULES + """

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

CONSULT_INSTRUCTIONS = """你是 Copilot 的独立技术顾问（SOL CONSULT）。你只能在材料范围内做一次性分析：
""" + _COMMON_RULES + """

JSON 结构：
{
  "schema": "copilot-sol-advisory/v1",
  "mode": "CONSULT",
  "summary": "结论摘要",
  "analysis": ["关键判断与依据（可引用材料中的文件名/行号/片段）"],
  "options": [
    {"id": "O1", "title": "方案名",
     "tradeoffs": "取舍与代价", "risks": "风险"}
  ],
  "risks": ["关键风险"],
  "recommendation": "建议方向与适用条件（主代理自行决定是否采纳）",
  "unverified": ["无法从材料确认的事项"]
}

要求：分析复杂问题、比较可选路线、识别关键风险；不确定时明确写入 unverified，
不要替主代理做最终决定，也不要提出需要工具验证却没有材料依据的结论。"""

PLAN_INSTRUCTIONS = """你是 Copilot 的独立规划顾问（SOL PLAN）。你只能在材料范围内给出规划建议：
""" + _COMMON_RULES + """

JSON 结构：
{
  "schema": "copilot-sol-advisory/v1",
  "mode": "PLAN",
  "summary": "规划结论摘要",
  "analysis": ["关键判断与依据"],
  "steps": [
    {"id": "S1", "title": "步骤", "depends_on": ["S0"],
     "risk": "该步骤的风险与验证点"}
  ],
  "risks": ["依赖或执行风险"],
  "alternatives": ["替代路线及其适用条件"],
  "recommendation": "建议的执行顺序与理由（主代理自行决定是否采纳）",
  "unverified": ["无法从材料确认的事项"]
}

要求：给出可执行、依赖清晰的步骤建议，标注风险与替代路线；不要假定未提供的
工程事实，不要把建议写成对主代理的强制指令。"""

MODE_INSTRUCTIONS = {
    "REVIEW": REVIEW_INSTRUCTIONS,
    "CONSULT": CONSULT_INSTRUCTIONS,
    "PLAN": PLAN_INSTRUCTIONS,
}
INSTRUCTIONS = REVIEW_INSTRUCTIONS  # 兼容既有调用方


def normalize_mode(mode: str) -> str:
    """归一大写并校验模式；未知模式失败关闭，不静默当作 REVIEW。"""
    value = str(mode or "").strip().upper()
    if value not in SOL_MODES:
        raise SolUsageError(
            "sol_mode_invalid",
            f"未知 SOL 模式 {mode!r}；只接受 " + " / ".join(SOL_MODES)
            + "（DECIDE 已退役，不恢复强制决策链）。")
    return value


def validate_effort(effort: str) -> str:
    """统一 high：拒绝缺失、xhigh/max 与任何静默降级。"""
    value = str(effort or "").strip().lower()
    if not value:
        raise SolUsageError(
            "sol_effort_missing",
            "未指定推理强度；SOL 统一使用 high，不允许缺失时回退到 medium。")
    if value == "high":
        return value
    raise SolUsageError(
        "sol_effort_not_pinned",
        f"推理强度 {effort!r} 被拒绝：CONSULT / PLAN / REVIEW 统一 high，"
        "不允许降级到 low/medium，也不使用 xhigh/max。")


def validate_materials(materials: object) -> str:
    """材料前置校验：非空、字符上限；dry-run 与真实调用共用同一口径。"""
    if not isinstance(materials, str) or not materials.strip():
        raise SolUsageError("sol_materials_empty", "审查材料为空。")
    if len(materials) > MAX_MATERIALS_CHARS:
        raise SolUsageError(
            "sol_materials_too_large",
            f"审查材料 {len(materials)} 字符超过上限 {MAX_MATERIALS_CHARS}；"
            "请主代理整理必要材料后重试，不静默截断。")
    return materials


def build_payload(materials: str, *, mode: str = "REVIEW",
                  model: str = MODEL, effort: str = REASONING_EFFORT,
                  max_output_tokens: int | None = DEFAULT_MAX_OUTPUT_TOKENS,
                  instructions: str = "") -> dict:
    """构建 Responses API 请求体；工具字段一律不存在、推理强度固定 high。"""
    validate_materials(materials)
    resolved_mode = normalize_mode(mode)
    resolved_effort = validate_effort(effort)
    if not str(model or "").strip():
        raise SolUsageError("sol_model_missing", "模型 ID 为空。")
    payload = {
        "model": model,
        "input": materials,
        "store": False,
        "stream": False,
        "tool_choice": "none",
        # 只显式给出 effort；mode 保持 Provider/模型默认的 standard。
        "reasoning": {"effort": resolved_effort},
    }
    if max_output_tokens is not None:
        payload["max_output_tokens"] = int(max_output_tokens)
    text = instructions or MODE_INSTRUCTIONS[resolved_mode]
    if text.strip():
        payload["instructions"] = text
    assert_zero_tool(payload)
    assert_responses_api_compat(payload)
    return payload


def assert_responses_api_compat(payload: dict) -> None:
    """GPT-6.2 推理模型不接受采样参数；出现即视为请求构造错误。"""
    for key in UNSUPPORTED_PARAMS:
        if key in payload:
            raise SolUsageError(
                "sol_unsupported_param",
                f"请求体包含推理模型不支持的参数 {key}；不要发送采样参数。")
    include = payload.get("include")
    if isinstance(include, list) and any("logprobs" in str(item) for item in include):
        raise SolUsageError(
            "sol_unsupported_param",
            "请求体 include 含 logprobs；SOL 不请求 token 概率。")


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


def _activity_nonzero(value: object) -> bool:
    """递归判断工具用量是否真的发生：全零计数视为未使用。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_activity_nonzero(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_activity_nonzero(item) for item in value)
    return bool(value)


def find_tool_activity(payload: dict) -> list[dict]:
    """上游回显的工具定义或真实工具用量；全零用量（如 image_gen/web_search）不算。

    GPT-6.2 响应固定回显 `tools` 与 `tool_usage` 计数（全零）；只有出现工具定义或
    非零用量时才说明 Provider 侧可能启用了工具，按安全事件拒绝。
    """
    found: list[dict] = []
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        found.append({"type": "tools_echo", "name": str(len(tools))})
    usage = payload.get("tool_usage")
    if isinstance(usage, dict):
        for name, stats in usage.items():
            if _activity_nonzero(stats):
                found.append({"type": "tool_usage", "name": str(name)})
    elif _activity_nonzero(usage):
        found.append({"type": "tool_usage", "name": type(usage).__name__})
    return found


def response_problem(payload: dict) -> str | None:
    status = str(payload.get("status") or "").lower()
    err = payload.get("error")
    if err:
        detail = err.get("message") if isinstance(err, dict) else str(err)
        return f"response error: {_safe_detail(detail)}"
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


def _parse_json_text(cleaned: str, schema: str = REVIEW_SCHEMA) -> dict:
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
        if value.get("schema") == schema:
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
            # 后备 ID 也必须唯一：重复的 "F2" 不能因为替换成 "F2" 继续冲突。
            candidate = f"F{index}"
            suffix = 1
            while candidate in used_ids:
                suffix += 1
                candidate = f"F{index}-{suffix}"
            finding_id = candidate
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
    return normalize_review(_parse_json_text(cleaned, REVIEW_SCHEMA))


def _object_list(value: object, name: str,
                 fields: tuple[str, ...]) -> list[dict]:
    """严格对象数组：非数组或字段类型不符一律拒绝，不静默丢弃。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise SolResponseError(f"sol_{name}_invalid", f"{name} 必须是对象数组")
    out: list[dict] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise SolResponseError(
                f"sol_{name}_invalid",
                f"{name} 第 {index} 项不是对象（{type(item).__name__}）")
        entry: dict = {}
        for key in fields:
            raw = item.get(key)
            if key == "depends_on":
                entry[key] = _string_list(raw, f"{name}_depends_on")
                continue
            if raw is None:
                entry[key] = ""
                continue
            if not isinstance(raw, str):
                raise SolResponseError(
                    f"sol_{name}_invalid",
                    f"{name} 第 {index} 项 {key} 必须是字符串")
            entry[key] = raw.strip()
        # 固定 ID：缺失时补 O<n>/S<n>，重复时保证唯一。
        if "id" in entry and not entry["id"]:
            entry["id"] = f"{name[0].upper()}{index}"
        out.append(entry)
    return out


def normalize_advisory(data: dict, mode: str) -> dict:
    """校验并归一化 CONSULT / PLAN 输出；schema 与 mode 必须与请求一致。"""
    resolved_mode = normalize_mode(mode)
    if resolved_mode not in ADVISORY_MODES:
        raise SolUsageError(
            "sol_mode_invalid", f"{resolved_mode} 不是辅助分析模式。")
    if data.get("schema") != ADVISORY_SCHEMA:
        raise SolResponseError("sol_schema_mismatch",
                               f"schema={data.get('schema')!r}")
    returned = _text(data.get("mode")).upper()
    if returned and returned != resolved_mode:
        raise SolResponseError(
            "sol_advisory_mode_mismatch",
            f"请求模式 {resolved_mode}，回复模式 {returned}；拒绝消费。")
    options = _object_list(data.get("options"), "options",
                           ("id", "title", "tradeoffs", "risks"))
    steps = _object_list(data.get("steps"), "steps",
                         ("id", "title", "depends_on", "risk"))
    for name, items in (("options", options), ("steps", steps)):
        used: set[str] = set()
        for index, item in enumerate(items, start=1):
            if item["id"] in used:
                item["id"] = f"{item['id']}-{index}"
            used.add(item["id"])
    return {
        "mode": resolved_mode,
        "summary": _text(data.get("summary")),
        "analysis": _string_list(data.get("analysis"), "analysis"),
        "options": options,
        "steps": steps,
        "risks": _string_list(data.get("risks"), "risks"),
        "alternatives": _string_list(data.get("alternatives"), "alternatives"),
        "recommendation": _text(data.get("recommendation")),
        "unverified": _string_list(data.get("unverified"), "unverified"),
    }


def parse_advisory_output(text: str, mode: str) -> dict:
    cleaned = str(text or "").lstrip("\ufeff").strip()
    if not cleaned:
        raise SolResponseError("sol_output_empty", "输出为空")
    return normalize_advisory(_parse_json_text(cleaned, ADVISORY_SCHEMA), mode)


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
    # 显式空 ProxyHandler：本机 CPA 请求不经过环境代理，避免凭据转发到第三方。
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
            headers = getattr(response, "headers", None)
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
            f"{_safe_detail(raw.decode('utf-8', errors='replace'), 300)}") from None
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
        raise SolUpstreamError("sol_transport_error",
                               _safe_detail(reason)) from None
    except (http.client.HTTPException, OSError) as exc:
        # 响应读到一半中断、连接重置、Content-Length 不足等：请求已发出，
        # 结果未知。显式映射为 SolError，确保进入失败审计与状态登记。
        raise SolUpstreamError(
            "sol_transport_error",
            _safe_detail(f"{type(exc).__name__}: {exc}")) from None
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise SolResponseError("sol_response_json_invalid", str(exc)) from None
    if not isinstance(payload, dict):
        raise SolResponseError("sol_response_not_object",
                               type(payload).__name__)
    payload["_http_status"] = status
    # 只保留追踪标识：请求 ID 用于取证，不回显任何响应头或凭据。
    try:
        request_id = (headers.get("X-Request-Id")
                      or headers.get("OpenAI-Request-Id") or "") if headers else ""
        trace_id = (headers.get("X-Cpa-Trace-Id") or "") if headers else ""
    except (AttributeError, TypeError):
        request_id = trace_id = ""
    payload["_request_id"] = str(request_id or "")
    payload["_trace_id"] = str(trace_id or "")
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


def run_sol(materials: str, *, mode: str = "REVIEW", audit_log: str = "",
            model: str = MODEL, effort: str = REASONING_EFFORT,
            max_output_tokens: int | None = DEFAULT_MAX_OUTPUT_TOKENS,
            timeout: float = DEFAULT_TIMEOUT,
            transport=None, api_key: str | None = None,
            base_url: str | None = None, instructions: str = "") -> dict:
    """发起一次 SOL 调用（REVIEW / CONSULT / PLAN）；任何异常都显式失败。"""
    resolved_mode = normalize_mode(mode)
    payload = build_payload(
        materials, mode=resolved_mode, model=model, effort=effort,
        max_output_tokens=max_output_tokens, instructions=instructions)
    resolved_effort = str(payload["reasoning"]["effort"])
    # 无论 URL 来自环境还是显式参数，都在解析凭据与发送前做同一本机出口校验。
    base = (base_url or resolve_base_url()).rstrip("/")
    if not _is_local_cpa_url(base):
        raise SolUsageError(
            "sol_route_not_local",
            f"SOL 只允许本机 CPA 出口（http(s)://127.0.0.1:8317，"
            f"无 userinfo），拒绝 {_safe_url(base)}；未解析凭据、未发送请求。")
    url = base + ENDPOINT
    key = api_key if api_key is not None else resolve_api_key()
    body_sha = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    started = time.monotonic()
    audit_base = {
        "ts": _now_iso(),
        "mode": resolved_mode,
        "model_requested": model,
        "reasoning_effort": resolved_effort,
        "reasoning_mode": REASONING_MODE,
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
        _audit_safe(audit_log, {**audit_base, "ok": False,
                           "error": exc.code, "detail": exc.detail,
                           "elapsed_ms": int((time.monotonic() - started) * 1000)})
        raise
    elapsed_ms = int((time.monotonic() - started) * 1000)
    status = response.pop("_http_status", 0)
    request_id = str(response.pop("_request_id", "") or "")
    trace_id = str(response.pop("_trace_id", "") or "")
    quota = _quota_signal(int(status or 0), response)
    if quota:
        exc = SolQuotaError(
            "sol_quota",
            f"HTTP 200 携带限流/冷却（code={quota['code']}）；未自动重试。")
        _audit_safe(audit_log, {**audit_base, "ok": False, "error": exc.code,
                           "http_status": status, "request_id": request_id,
                           "trace_id": trace_id, "elapsed_ms": elapsed_ms})
        raise exc
    try:
        unexpected = find_unexpected_output(response)
        problem = response_problem(response)
        text = extract_text(response) if not problem else ""
    except (AttributeError, TypeError, ValueError, KeyError) as exc:
        _audit_safe(audit_log, {**audit_base, "ok": False,
                           "error": "sol_malformed_response",
                           "detail": f"{type(exc).__name__}: {exc}",
                           "http_status": status, "elapsed_ms": elapsed_ms})
        raise SolResponseError("sol_malformed_response", str(exc)) from None
    model_resolved = response.get("model")
    identity_ok = (isinstance(model_resolved, str)
                   and model_resolved.strip() == model)
    usage_raw = response.get("usage") if isinstance(response.get("usage"), dict) else None
    usage = _usage_summary(usage_raw)
    reasoning_resolved = (response.get("reasoning")
                          if isinstance(response.get("reasoning"), dict) else {})
    echoed_effort = _text(reasoning_resolved.get("effort")).lower()
    echoed_mode = _text(reasoning_resolved.get("mode")).lower()
    reasoning_verified = bool(echoed_effort)
    # 上游回显 tools/tool_usage：全零计数是 GPT-6.2 的固定回显，不算工具使用；
    # 出现工具定义或非零用量才按安全事件拒绝。
    unexpected.extend(find_tool_activity(response))
    result_id = str(response.get("id") or "")
    common_audit = {"http_status": status, "model_resolved": model_resolved,
                    "reasoning_resolved": reasoning_resolved,
                    "reasoning_verified": reasoning_verified,
                    "request_id": request_id, "trace_id": trace_id,
                    "response_id": result_id,
                    "usage": usage, "elapsed_ms": elapsed_ms}
    if unexpected:
        exc = SolToolCallError(
            "sol_unexpected_output_item",
            f"上游返回非良性条目 {unexpected}；已拒绝处理且未执行。")
        _audit_safe(audit_log, {**audit_base, **common_audit, "ok": False,
                           "error": exc.code,
                           "detail": json.dumps(unexpected, ensure_ascii=False)})
        raise exc
    if not identity_ok:
        exc = SolIdentityError(
            "sol_model_identity_mismatch",
            f"请求模型 {model!r}，上游回显 "
            f"{_safe_detail(model_resolved, 200)!r}；该回复不得被消费。")
        _audit_safe(audit_log, {**audit_base, **common_audit, "ok": False,
                           "error": exc.code})
        raise exc
    if echoed_effort and echoed_effort != resolved_effort:
        exc = SolReasoningError(
            "sol_reasoning_effort_mismatch",
            f"请求 reasoning.effort={resolved_effort!r}，上游回显 "
            f"{echoed_effort!r}；推理强度被覆盖，该回复不得被消费。")
        _audit_safe(audit_log, {**audit_base, **common_audit, "ok": False,
                           "error": exc.code})
        raise exc
    if echoed_mode and echoed_mode != REASONING_MODE:
        exc = SolReasoningError(
            "sol_reasoning_mode_mismatch",
            f"请求 reasoning.mode={REASONING_MODE!r}（默认 standard），上游回显 "
            f"{echoed_mode!r}；该回复不得被消费。")
        _audit_safe(audit_log, {**audit_base, **common_audit, "ok": False,
                           "error": exc.code})
        raise exc
    if problem:
        exc = SolResponseError("sol_response_incomplete", problem)
        _audit_safe(audit_log, {**audit_base, **common_audit, "ok": False,
                           "error": exc.code, "detail": problem})
        raise exc
    text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        if resolved_mode == "REVIEW":
            parsed = parse_review_output(text)
        else:
            parsed = parse_advisory_output(text, resolved_mode)
    except SolResponseError as exc:
        _audit_safe(audit_log, {**audit_base, **common_audit, "ok": False,
                           "error": exc.code, "detail": exc.detail,
                           "reply_sha256": text_sha,
                           "reply_chars": len(text)})
        raise SolResponseError(exc.code, exc.detail) from None
    result = {
        "schema": (REVIEW_SCHEMA if resolved_mode == "REVIEW"
                   else ADVISORY_SCHEMA),
        "mode": resolved_mode,
        "model": model,
        "model_resolved": model_resolved,
        "reasoning_effort": resolved_effort,
        "reasoning_mode": REASONING_MODE,
        "reasoning_resolved": reasoning_resolved,
        "reasoning_verified": reasoning_verified,
        "zero_tool": True,
        "attempts": 1,
        "request_id": request_id,
        "trace_id": trace_id,
        "response_id": result_id,
        "materials_sha256": audit_base["materials_sha256"],
        "materials_chars": len(materials),
        "request_sha256": body_sha,
        "reply_text": text,
        "reply_sha256": text_sha,
        "reply_chars": len(text),
        "usage": usage,
        "usage_available": usage_raw is not None,
        "latency_ms": elapsed_ms,
        "completed_at": _now_iso(),
    }
    if resolved_mode == "REVIEW":
        counts = {severity: sum(1 for item in parsed["findings"]
                                if item["severity"] == severity)
                  for severity in SEVERITIES}
        result.update({
            "verdict": parsed["verdict"],
            "summary": parsed["summary"],
            "findings": parsed["findings"],
            "unverified": parsed["unverified"],
            "materials_reviewed": parsed["materials_reviewed"],
            "verdict_adjusted": parsed["verdict_adjusted"],
            "findings_count": counts,
        })
        audit_extra = {"verdict": parsed["verdict"], "findings_count": counts}
    else:
        result.update({
            "summary": parsed["summary"],
            "analysis": parsed["analysis"],
            "options": parsed["options"],
            "steps": parsed["steps"],
            "risks": parsed["risks"],
            "alternatives": parsed["alternatives"],
            "recommendation": parsed["recommendation"],
            "unverified": parsed["unverified"],
        })
        audit_extra = {
            "analysis_items": len(parsed["analysis"]),
            "options": len(parsed["options"]),
            "steps": len(parsed["steps"]),
            "risks": len(parsed["risks"]),
            "alternatives": len(parsed["alternatives"]),
        }
    audit_error = _audit_safe(audit_log, {**audit_base, **common_audit,
                                          **audit_extra, "ok": True,
                                          "reply_sha256": text_sha,
                                          "reply_chars": len(text)})
    # 远程请求已经成功并解析：审计/落盘失败只能作为告警，不能让调用方把这次
    # 付费结果当成“未发生”并再次发起请求。
    result["audit_written"] = audit_error is None
    result["audit_error"] = audit_error or ""
    return result


def run_review(materials: str, **kwargs) -> dict:
    """一次 SOL REVIEW（保持既有调用方签名）。"""
    return run_sol(materials, mode="REVIEW", **kwargs)


def run_advisory(materials: str, mode: str, **kwargs) -> dict:
    """一次 SOL CONSULT / PLAN；REVIEW 必须走 `run_review` 以保持语义清晰。"""
    resolved = normalize_mode(mode)
    if resolved not in ADVISORY_MODES:
        raise SolUsageError(
            "sol_mode_invalid",
            f"{resolved} 不是辅助分析模式；REVIEW 请使用 sol-review。")
    return run_sol(materials, mode=resolved, **kwargs)


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


def _audit_safe(path: str, record: dict) -> str | None:
    """审计写入失败不掩盖已经发生的远程结果；返回错误码供调用方记录。"""
    try:
        _audit(path, record)
    except SolAuditError as exc:
        return exc.code
    return None


# ── 材料、单轮判定与准入 ───────────────────────────────────────────────────
# 提示词/策略版本：进入单轮判定键。提示词、共享上下文或判定规则实质变化时，
# 旧结论不再被当成同一审查对象复用（缓存失效条件见 review_subject_sha256）。
PROMPT_VERSION = "sol-prompt-v2"

_EVIDENCE_LIMIT = 80
_EVIDENCE_MAX_CHARS = 6000
_ACTION_LIMIT = 40


def _criteria_lines(state: dict) -> list[str]:
    criteria = ((state.get("contract") or {}).get("criteria") or [])
    lines = []
    for item in criteria:
        if not isinstance(item, dict):
            continue
        lines.append(f"- {item.get('criterion_id') or ''} "
                     f"[{item.get('status') or 'pending'}] "
                     f"{item.get('description') or ''}")
    return lines or ["(未登记完成条件)"]


def _evidence_lines(state: dict, *, limit: int = _EVIDENCE_LIMIT,
                    max_chars: int = _EVIDENCE_MAX_CHARS) -> list[str]:
    """把任务证据登记压缩成可审查清单（自动注入，主代理不必重复粘贴）。

    只列事实：ID、类型、摘要前缀、大小、被哪些完成条件引用、是否已归档。它让
    SOL 与 G2 使用同一份证据来源；完整证据内容仍由主代理放在材料里。
    """
    evidence = [item for item in state.get("evidence") or []
                if isinstance(item, dict)]
    if not evidence:
        return ["(尚未登记证据)"]
    supports: dict[str, list[str]] = {}
    for criterion in ((state.get("contract") or {}).get("criteria") or []):
        if not isinstance(criterion, dict):
            continue
        cid = str(criterion.get("criterion_id") or "")
        for ref in criterion.get("evidence_refs") or []:
            if isinstance(ref, dict):
                supports.setdefault(str(ref.get("id") or ""), []).append(cid)
    lines: list[str] = []
    used = 0
    for index, item in enumerate(evidence):
        eid = str(item.get("id") or "")
        parts = [f"- {eid} [{item.get('kind') or 'file'}]",
                 f"sha={str(item.get('sha256') or '')[:12]}",
                 f"{item.get('size') or 0}B"]
        refs = sorted(set(filter(None, supports.get(eid) or [])))
        if refs:
            parts.append("支持 " + ",".join(refs))
        hint = str(item.get("path_hint") or "")
        if hint and hint != "<inline>":
            parts.append(f"path={hint}")
        if item.get("dropped"):
            parts.append("dropped=yes（已归档，不参与收口）")
        line = " ".join(parts)
        if index >= limit or used + len(line) > max_chars:
            lines.append(f"(其余 {len(evidence) - index} 条证据已省略："
                         "完整清单见任务状态 evidence)")
            break
        lines.append(line)
        used += len(line)
    return lines


def _action_lines(state: dict, *, limit: int = _ACTION_LIMIT) -> list[str]:
    actions = [item for item in state.get("actions") or []
               if isinstance(item, dict)]
    if not actions:
        return ["(尚未登记工作项)"]
    lines = [f"- {item.get('id') or ''} [{item.get('status') or ''}] "
             f"{str(item.get('title') or '')[:160]}" for item in actions[:limit]]
    if len(actions) > limit:
        lines.append(f"(其余 {len(actions) - limit} 个工作项已省略)")
    return lines


def _context_lines(state: dict) -> list[str]:
    """REVIEW / CONSULT / PLAN 共用的任务上下文：一份合同、一套条件与证据。"""
    contract = state.get("contract") or {}
    boundaries = str(contract.get("boundaries") or "").strip()
    return [
        f"- 任务：{state.get('task_id') or '(未命名)'}",
        f"- 风险：{state.get('risk') or 'medium'}",
        f"- 执行后端：{state.get('execution_backend') or 'legacy'}",
        f"- 合同指纹：{state.get('contract_sha256') or '(未登记)'}",
        f"- 模型：{MODEL}（reasoning.effort={REASONING_EFFORT}，零工具）",
        f"- 提示词版本：{PROMPT_VERSION}",
        "",
        "## 任务目标",
        "",
        str(state.get("goal") or "").strip() or "(未登记)",
        "",
        "## 完成条件",
        "",
        *_criteria_lines(state),
        "",
        "## 合同正文",
        "",
        # 合同正文存放在 `done_criteria_text`（与合同指纹同源）；旧实现误读了
        # 不存在的 `text` 键，导致 SOL 从未看到合同全文。
        str(contract.get("done_criteria_text") or "").strip() or "(未登记)",
        "",
        *(["## 合同边界", "", boundaries, ""] if boundaries else []),
        "## 工作项（按任务状态自动注入）",
        "",
        *_action_lines(state),
        "",
        "## 证据登记（按任务状态自动注入）",
        "",
        *_evidence_lines(state),
        "",
    ]


def build_prompt(state: dict, materials: str, *, mode: str = "REVIEW",
                 question: str = "") -> str:
    """把共享任务上下文与主代理材料拼成一次完整上下文（三模式共用）。"""
    resolved = normalize_mode(mode)
    if resolved not in SOL_MODES:
        raise SolUsageError("sol_mode_invalid", f"{resolved} 不是 SOL 模式。")
    lines = [f"# Copilot SOL {resolved} 材料", "", *_context_lines(state)]
    if resolved == "REVIEW":
        lines += [
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
    else:
        lines += [
            "## 主代理的问题",
            "",
            str(question or "").strip() or "(未提供，按材料给出总体建议)",
            "",
            "## 主代理提供的材料",
            "",
            materials.strip(),
            "",
            f"## {resolved} 要求",
            "",
            "一次性给出辅助分析：只基于材料，未知项写入 unverified；"
            "按契约输出单个 JSON 对象。你的输出是建议，由 ZCode 主代理决定是否采纳。",
            "",
        ]
    return "\n".join(lines)


def review_digest(materials_text: str) -> str:
    """完整提示词的摘要（审计用，包含当前条件状态）。"""
    return hashlib.sha256(materials_text.encode("utf-8")).hexdigest()


def evidence_digest(state: dict) -> str:
    """任务证据集合摘要：只含证据身份与归档标记，不含条件/工作项状态。"""
    rows = sorted([str(item.get("id") or ""), str(item.get("sha256") or ""),
                   bool(item.get("dropped"))]
                  for item in state.get("evidence") or []
                  if isinstance(item, dict))
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True).encode("utf-8")).hexdigest()


def review_subject_sha256(state: dict, materials: str, *, mode: str = "",
                          question: str = "") -> str:
    """单轮判定用的稳定主体摘要（同一份上下文只允许一次调用）。

    缓存键成分：提示词版本、模型与推理强度、目标、合同正文（`done_criteria_text`
    与边界）、完成条件描述、**证据集合摘要**与主代理材料；辅助分析额外绑定模式
    与问题。以下变化即失效：
    - 合同/目标/条件描述或材料实质变化；
    - 证据集合变化（新增、摘要变化、归档）；
    - 提示词版本、模型或推理强度变化（升级后旧结论不再复用）。
    条件从 pending 变 met 属于流程状态变化，不是新的审查对象：同一材料在证据
    未变时仍复用已完成的结论，避免上下文恢复后重复付费。
    """
    contract = state.get("contract") or {}
    criteria = []
    for item in contract.get("criteria") or []:
        if not isinstance(item, dict):
            continue
        criteria.append([str(item.get("criterion_id") or ""),
                         str(item.get("description") or "")])
    blob = {
        "prompt_version": PROMPT_VERSION,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "goal": str(state.get("goal") or ""),
        "contract": str(contract.get("done_criteria_text") or ""),
        "boundaries": str(contract.get("boundaries") or ""),
        "criteria": criteria,
        "evidence_digest": evidence_digest(state),
        "materials": materials,
    }
    if mode:
        blob["mode"] = normalize_mode(mode)
    if question:
        blob["question"] = str(question)
    return hashlib.sha256(
        json.dumps(blob, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def record_mode(record: dict) -> str:
    """记录模式；历史记录可能没有 mode 字段，按当时存放位置判定。"""
    mode = str(record.get("mode") or "").upper()
    if mode in SOL_MODES:
        return mode
    return "CONSULT" if record.get("advisory_id") else "REVIEW"


def completed_reviews(state: dict, records_key: str = "sol_calls") -> list[dict]:
    """已完成记录：默认读取统一 `sol_calls` 流（含历史只读兼容）。

    显式传入 `sol_reviews` / `sol_advisories` 时只读该历史键，供兼容调用方使用。
    """
    if records_key == "sol_calls":
        import governance as G  # 延迟导入：状态合并逻辑只有一份
        rows = G.iter_sol_calls(state)
    else:
        rows = [item for item in state.get(records_key) or []
                if isinstance(item, dict)]
    return [item for item in rows if item.get("status") == "completed"]


def single_round_decision(state: dict, materials_sha256: str, *,
                          force: bool = False, reason: str = "",
                          trigger: str = "",
                          records_key: str = "sol_calls",
                          mode: str = "REVIEW") -> dict:
    """单轮判定：同一材料直接复用；任何再次调用都必须有实质原因。

    辅助分析（CONSULT / PLAN）与复核（REVIEW）各自独立判定，不会互相复用或
    互相覆盖；记录统一存放在 `sol_calls`，历史 `sol_reviews` /
    `sol_advisories` 仍可只读复用。
    """
    resolved_mode = normalize_mode(mode)
    records = [item for item in completed_reviews(state, records_key)
               if record_mode(item) == resolved_mode]
    same = [item for item in records
            if str(item.get("materials_sha256") or "") == materials_sha256]
    if same:
        return {"action": "reuse", "review": same[-1],
                "note": "相同材料已完成同模式 SOL 调用；直接复用结论，不重复调用。"}
    if not records:
        return {"action": "call", "note": f"首次 SOL {resolved_mode} 调用。"}
    trigger_value = str(trigger or "").strip().lower()
    reason_value = str(reason or "").strip()
    if not force:
        return {"action": "refuse", "code": "sol_review_requires_reason",
                "note": ("本任务已有同模式 SOL 结论。单轮原则要求再次调用必须有明确原因；"
                         "使用 --force --trigger <原因类型> --reason <说明>。")}
    if trigger_value not in RETRY_TRIGGERS:
        return {"action": "refuse", "code": "sol_review_trigger_invalid",
                "note": ("--trigger 只能是 "
                         + ", ".join(RETRY_TRIGGERS) + "。")}
    if not reason_value:
        return {"action": "refuse", "code": "sol_review_reason_missing",
                "note": "再次调用必须用 --reason 说明具体原因。"}
    return {"action": "call", "trigger": trigger_value, "reason": reason_value,
            "note": f"允许再次调用：{RETRY_TRIGGERS[trigger_value]}。"}



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

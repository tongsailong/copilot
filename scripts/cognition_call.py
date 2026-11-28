#!/usr/bin/env python3
"""认知一次性调用客户端：CONSULT / PLAN（默认认知引擎）/ REVIEW（用户明确要求的独立复核）。

Copilot 的认知通道只承担**认知**，不承担决策或执行：

* **零工具是能力级约束**，不靠提示词自律：出站请求体**永不包含** `tools` /
  `functions` / `parallel_tool_calls` 等工具定义，并显式 `tool_choice="none"`；
  上游响应只要出现 `message` / `reasoning` 之外的非良性 output 条目，即判安全
  事件、拒绝消费（退出码 5），绝不代执行。
* **单轮**：一次调用只发一次 HTTP 请求。超时、429、网络错误、截断、身份不符、
  格式异常都不自动重试、不换 Provider、不降级到其他链路；失败原因如实记录。
  CONSULT / PLAN 失败按可降级处理（`copilot_dispatch.py cognition-consult|cognition-plan`
  记录后返回 0，主代理继续），REVIEW 失败仍显式失败，因为它是用户明确要求的
  独立结论。
* **模型与推理强度是硬门**：默认 `gpt-6.2-sol` + `reasoning.effort="high"`
  （`gpt-6.2-astra` 保留为显式可选，`COPILOT_COGNITION_MODEL` 或显式参数指定）；
  上游 `model` 回显必须与本次请求完全一致，`reasoning.effort` / `reasoning.mode`
  回显也必须与请求一致（`standard`）；任何不一致都拒绝消费（退出码 9），
  不允许静默降级到 `medium`/`low` 或其他模型；模型名不在注册表内时拒绝发送。
  模型选择是显式配置，不是失败回退：`gpt-6.2-sol` 不可用时不会自动改选 Astra。
* **三种模式**：CONSULT 是默认认知引擎，任务启动、执行期新证据、阶段复盘与
  收口都能按需发出一次判断；PLAN 在调查之后给出方案比较与规划判断；REVIEW
  只在用户明确要求时做独立复核，输出结构化 findings。三者都是 advisory：
  不修改文件、不执行命令、不读写台账、不调度子代理，也不构成收口门禁；
  工程执行与技术决策始终由 ZCode 主代理负责。THINK 是 v8.4–v8.5 的旧名称，
  读取时归一为 CONSULT，不再产生第三套流程。
* **按需调用**：由主代理结合任务性质判断是否有增量价值；没有 DECIDE 模式，
  没有硬性调用次数或 Token 上限，也不执行按阶段的机械重复流程。

本模块不写任务状态：状态登记、单轮判定与 finding 合并由
`copilot_dispatch.py cognition-consult` / `cognition-plan` / `cognition-review` 编排；这里只构建
请求、发一次请求、解析与审计。单轮判定需要读取统一认知记录时，通过
`governance.iter_cognition_calls` 只读复用，合并逻辑只有一份。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from lib_common import redact as _redact_text
import review_policy as _RP

REVIEW_SCHEMA = "copilot-cognition-review/v1"
# v9 认知输出是单一超集结构：同时覆盖启动理解、方案规划、执行期动态咨询、
# 阶段复盘与收口判断；v1 记录继续只读兼容。
THINK_SCHEMA = "copilot-cognition-think/v2"
ADMISSION_SCHEMA = "copilot-cognition-admission/v1"
# 模型身份只在这里定义：默认 SOL（v9.14.0 回切），Astra 是注册表内的显式可选
# 模型，也是默认模型发生可归因故障时唯一允许的单次兜底目标（v9.15.0）。
PROVIDER = "codex"
DEFAULT_MODEL = "gpt-6.2-sol"
# 显式可选模型：可被显式选择，也是 `FALLBACK_MODEL` 的来源；注册表外的模型名拒绝发送。
EXPLICIT_MODELS = ("gpt-6.2-astra",)
SUPPORTED_MODELS = (DEFAULT_MODEL,) + EXPLICIT_MODELS
MODEL = DEFAULT_MODEL
# 单次自动兜底：默认模型的一次请求发生**可明确归因**的模型级/传输级故障时，
# 用备用模型接管**同一次**认知调用（同一材料、模式、推理等级、零工具）。
# 它不写默认模型、不切 Provider、不产生第三次尝试；显式选择备用模型时不反向回退。
FALLBACK_MODEL = "gpt-6.2-astra"
FALLBACK_ENV = "COPILOT_COGNITION_FALLBACK"
MAX_MODEL_ATTEMPTS = 2
# 显式模型选择通道：省略 `--model` 时代替默认值；未知模型名仍拒绝发送。
MODEL_ENV = "COPILOT_COGNITION_MODEL"
BASE_URL = "http://127.0.0.1:8317/v1"
ENDPOINT = "/responses"
REASONING_EFFORT = "high"
REASONING_MODE = "standard"
# 所有认知模式统一 high：不允许按角色降级，也不允许缺失时回退。
# 实际校验在 validate_effort()；这里不再保留未被引用的常量副本。
# CONSULT / PLAN 是默认认知生命周期的两种一等模式；REVIEW 只用于用户明确
# 要求的独立复核。THINK 是 v8.4-v8.5 的旧名称，只读兼容并归一到 CONSULT。
CALL_MODES = ("CONSULT", "PLAN", "REVIEW")
COGNITIVE_MODES = ("CONSULT", "PLAN")
LEGACY_MODES = ("THINK",)
LEGACY_MODE_ALIASES = {"THINK": "CONSULT"}
# 认知调用所处的生命周期阶段：只影响提示词重点与推荐命令，不是门禁。
COGNITION_STAGES = ("understanding", "planning", "development", "stage", "closure")
DEFAULT_STAGE = "development"
API_KEY_ENV = "CUSTOM_127_API_KEY"
ZCODE_PROVIDER_FILE = os.path.expanduser("~/.zcode/v2/provider_config.json")
REASONIX_ENV_FILE = os.path.expanduser("~/.reasonix/.env")

DEFAULT_TIMEOUT = 300.0
DEFAULT_MAX_OUTPUT_TOKENS = 24000
MAX_MATERIALS_CHARS = 400_000
WARN_MATERIALS_CHARS = 200_000
# 认知调用只接收主代理整理过的材料：目标、基线、已确认事实、已尝试方案、
# 证据摘要与本次问题。完整仓库、冗长日志或重复报告应在本地消化。
THINK_MAX_MATERIALS_CHARS = 120_000
THINK_WARN_MATERIALS_CHARS = 60_000
MATERIALS_LIMITS = {"CONSULT": THINK_MAX_MATERIALS_CHARS,
                    "PLAN": THINK_MAX_MATERIALS_CHARS,
                    "REVIEW": MAX_MATERIALS_CHARS}
MATERIALS_WARN = {"CONSULT": THINK_WARN_MATERIALS_CHARS,
                  "PLAN": THINK_WARN_MATERIALS_CHARS,
                  "REVIEW": WARN_MATERIALS_CHARS}

# 图像材料：认知只能通过 Responses API 的 `input_image` 内容块看到真实像素。
# 这里只做本地文件读取、编码与上限校验；不解码像素、不做 OCR、不把图片文字化。
# 上限偏保守：一次设计审美咨询通常 2–6 张代表性截图，不需要整包截图。
IMAGE_MAX_COUNT = 6
IMAGE_MAX_BYTES = 8 * 1024 * 1024
IMAGE_MAX_TOTAL_BYTES = 24 * 1024 * 1024
IMAGE_MIMES = ("image/png", "image/jpeg", "image/webp")

BENIGN_OUTPUT_TYPES = {"message", "reasoning"}
TOOL_KEYS = ("tools", "functions", "parallel_tool_calls", "tool_resources",
             "tool_config")
# GPT-6.2 Responses API 对推理模型不接受采样参数；认知从不发送这些字段。
UNSUPPORTED_PARAMS = ("temperature", "top_p", "top_logprobs", "logprobs",
                      "frequency_penalty", "presence_penalty")
SEVERITIES = ("blocking", "major", "minor", "info")
VERDICTS = ("pass", "partial", "blocked")

# 允许再次发起认知调用的实质原因（同一生命周期阶段的首次调用不需要）。
# 没有新的决策价值时，同一问题与材料直接复用已有结论，不重复付费。这里没有
# 次数上限：只要出现新的认知对象或升级型证据，就可以按对应原因继续调用。
RETRY_TRIGGERS = {
    "user-request": "用户明确要求再次判断",
    "scope-changed": "变更范围发生实质变化",
    "new-risk": "出现新的关键风险，需要独立判断",
    "new-evidence": "出现与原假设冲突或改变判断的新证据",
    "approach-failed": "原方案被证明不可行，需要改变路线",
    "new-route": "出现新的可行技术路线，需要比较取舍",
    "tradeoff-decision": "出现实质性方案取舍，需要独立判断",
    "stage-result": "重要阶段完成，需要复盘成果与下一阶段方向",
    "strategy-failed": "原方案连续失败，需要改变整体策略",
    "materials-omitted": "首次材料存在重大遗漏，需要补充后重新判断",
    "major-uncertainty": "出现超出当前判断能力的重大不确定性",
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


class CognitionError(Exception):
    """认知调用错误基类；`exit_code` 是调用方应返回的进程退出码。

    除错误码与详情外，错误对象还携带可机器判读的上游事实：HTTP 状态、上游错误码、
    Retry-After、请求/追踪 ID 与稳定分类 `error_class`。这些字段只用于诊断与退避
    判断，不改变"单次请求、不自动重试、不切换 Provider"的语义，也不构成成功证据。
    """

    exit_code = EXIT_UPSTREAM

    def __init__(self, code: str, detail: str = "", *,
                 http_status: int | None = None,
                 retry_after: int | None = None,
                 retry_after_raw: str = "",
                 upstream_code: str = "",
                 error_class: str = "",
                 request_id: str = "",
                 trace_id: str = "",
                 response_received: bool = False):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.http_status = http_status
        self.retry_after = retry_after
        # 原始 Retry-After 文本（可能是 HTTP-date）：数值化后仍保留原值供审计。
        self.retry_after_raw = str(retry_after_raw or "")
        self.upstream_code = str(upstream_code or "")
        self.error_class = str(error_class or "")
        self.request_id = str(request_id or "")
        self.trace_id = str(trace_id or "")
        # 收到过 HTTP 响应（哪怕是错误响应）时发送事实是确定的；只有客户端
        # 超时/网络异常才保持 unknown。urllib 无法证明字节是否出站，但可以证明
        # 是否读到了响应。
        self.response_received = bool(response_received)
        self.request_attempted = False
        self.send_observed = ("response_received" if self.response_received
                              else "not_attempted")


class CognitionUsageError(CognitionError):
    exit_code = EXIT_USAGE


class CognitionUpstreamError(CognitionError):
    exit_code = EXIT_UPSTREAM


class CognitionTimeoutError(CognitionError):
    exit_code = EXIT_TIMEOUT


class CognitionToolCallError(CognitionError):
    exit_code = EXIT_TOOLCALL


class CognitionResponseError(CognitionError):
    exit_code = EXIT_SHAPE


class CognitionAuditError(CognitionError):
    exit_code = EXIT_AUDIT


class CognitionQuotaError(CognitionError):
    exit_code = EXIT_QUOTA


class CognitionIdentityError(CognitionError):
    exit_code = EXIT_IDENTITY


class CognitionReasoningError(CognitionError):
    """上游回显的推理强度/模式与请求不一致：Provider 覆盖参数，拒绝消费。"""

    exit_code = EXIT_IDENTITY


# ── 上游失败分类（稳定、机器可读；只描述事实，不决定重试） ────────────────
# 分类用于诊断与退避判断，不改变"一次调用一次请求、不自动重试、不切 Provider"。
# 关键不变量：`auth_unavailable` 是 CPA 的调度结果（凭据冷却），不是认证失效；
# 真正的认证失败是 401/403。因此过载/冷却永远不会被归入 `upstream_auth`。
UPSTREAM_OVERLOADED = "upstream_overloaded"
UPSTREAM_STREAM_TIMEOUT = "upstream_stream_timeout"
UPSTREAM_CREDENTIAL_COOLING = "upstream_credential_cooling"
UPSTREAM_AUTH = "upstream_auth"
UPSTREAM_RATE_LIMIT = "upstream_rate_limit"
UPSTREAM_SERVER = "upstream_server_error"
UPSTREAM_CLIENT = "upstream_client_error"
CLIENT_TIMEOUT = "client_timeout"
NETWORK_ERROR = "network_error"
RESPONSE_INCOMPLETE = "response_incomplete"

ERROR_CLASS_CODE = {
    UPSTREAM_OVERLOADED: "cognition_upstream_overloaded",
    UPSTREAM_STREAM_TIMEOUT: "cognition_upstream_stream_timeout",
    UPSTREAM_CREDENTIAL_COOLING: "cognition_upstream_credential_cooling",
    UPSTREAM_AUTH: "cognition_upstream_auth",
    UPSTREAM_RATE_LIMIT: "cognition_quota",
    UPSTREAM_SERVER: "cognition_upstream_server_error",
    UPSTREAM_CLIENT: "cognition_upstream_client_error",
}

ERROR_CLASS_LABEL = {
    UPSTREAM_OVERLOADED: "上游模型容量过载",
    UPSTREAM_STREAM_TIMEOUT: "上游流在完成前断开",
    UPSTREAM_CREDENTIAL_COOLING: "CPA 凭据冷却窗口内本地拒绝",
    UPSTREAM_AUTH: "上游认证失败",
    UPSTREAM_RATE_LIMIT: "上游限流/配额",
    UPSTREAM_SERVER: "上游 5xx 错误",
    UPSTREAM_CLIENT: "上游拒绝请求本身（4xx）",
    CLIENT_TIMEOUT: "客户端等待超时",
    NETWORK_ERROR: "网络/传输错误",
    RESPONSE_INCOMPLETE: "响应不完整或不可消费",
}

# 这些分类表示上游明确不可用：调度层应在 Retry-After 窗口内推迟新请求，
# 而不是换一条同质请求制造更多无效调用（仍然不自动重试）。
UPSTREAM_RETRYABLE_CLASSES = frozenset({
    UPSTREAM_OVERLOADED, UPSTREAM_CREDENTIAL_COOLING, UPSTREAM_SERVER,
})


class CognitionOverloadedError(CognitionUpstreamError):
    """上游容量过载（`server_is_overloaded` / 529 / 503 过载）。"""


class CognitionStreamTimeoutError(CognitionUpstreamError):
    """上游流在 `response.completed` 之前断开（HTTP 408 / `request_timeout`）。"""


class CognitionCredentialCoolingError(CognitionUpstreamError):
    """CPA 凭据冷却导致的本地 503（`auth_unavailable`）。

    这是调度结果而不是认证失效：不得据此轮换凭据、更换 Provider 或判定账号
    认证不可用；只按上游 Retry-After 推迟下一次请求。
    """


class CognitionAuthError(CognitionUpstreamError):
    """上游认证失败（401/403）；需人工核实，不自动重试、不切换 Provider。"""


class CognitionUpstreamClientError(CognitionUpstreamError):
    """上游拒绝了请求本身（4xx，非认证/限流）；请求构造需要检查。"""


_UPSTREAM_ERROR_CLASSES = {
    UPSTREAM_OVERLOADED: CognitionOverloadedError,
    UPSTREAM_STREAM_TIMEOUT: CognitionStreamTimeoutError,
    UPSTREAM_CREDENTIAL_COOLING: CognitionCredentialCoolingError,
    UPSTREAM_AUTH: CognitionAuthError,
    UPSTREAM_RATE_LIMIT: CognitionQuotaError,
    UPSTREAM_SERVER: CognitionUpstreamError,
    UPSTREAM_CLIENT: CognitionUpstreamClientError,
}


# ── 单次自动兜底判定（只判定，不发送请求）──────────────────────────────────
# 允许回退：可以明确归因到“模型实例/上游服务/传输”的一次性故障——同一材料换一个
# 已注册模型有机会得到有效回复。禁止回退：认证、账户额度、凭据冷却（共享 CPA
# 通道，换模型同样失败）、请求构造错误（4xx）、用户取消与回答质量/格式问题
# （这些没有 error_class，属于“回复不可用”而不是“通道不可用”）。
FALLBACK_ERROR_CLASSES = frozenset({
    UPSTREAM_OVERLOADED,      # server_is_overloaded / 529
    UPSTREAM_STREAM_TIMEOUT,  # 408 / 流在 response.completed 之前断开
    UPSTREAM_SERVER,          # 500 / 502 / 503 / 504 等 5xx
    CLIENT_TIMEOUT,           # 客户端等待超时（结果未知，按不确定性记录）
    NETWORK_ERROR,            # 连接失败 / 连接重置 / 传输中断
    RESPONSE_INCOMPLETE,      # 响应截断、非完整、协议层损坏
})
# 模型身份不符：回复不可消费，换同一材料的备用模型再取一次是安全的。
FALLBACK_ERROR_CODES = frozenset({"cognition_model_identity_mismatch"})
# 429 必须区分模型级限流与账户级额度：只有模型级冷却换模型有意义。
MODEL_LEVEL_RATE_LIMIT_CODES = frozenset({"model_cooldown"})
NO_FALLBACK_ERROR_CLASSES = frozenset({
    UPSTREAM_AUTH, UPSTREAM_CREDENTIAL_COOLING, UPSTREAM_CLIENT,
})
# 账户额度/支付与认证类错误即使被上游包进 5xx 外壳，也仍然是账户或凭据问题：
# 换模型掩盖不了它们，只会多花一次调用。按结构化错误码与精确 token 判定。
ACCOUNT_QUOTA_ERROR_CODES = frozenset({
    "usage_limit_reached", "insufficient_quota", "quota_exceeded",
    "billing_hard_limit_reached", "billing_not_active",
})
AUTH_ERROR_CODES = frozenset({
    "invalid_api_key", "invalid_organization", "account_deactivated",
})
# 安全终止与用户取消：不是通道故障，换模型只会把同一拒绝再执行一次。
TERMINAL_REFUSAL_ERROR_CODES = frozenset({
    "content_policy_violation", "content_filter", "moderation_blocked",
    "safety_blocked", "user_canceled", "cancelled_by_user",
    "request_canceled",
})


def fallback_enabled() -> bool:
    """兜底开关（运维例外用）：`COPILOT_COGNITION_FALLBACK=off` 关闭。"""
    value = str(os.environ.get(FALLBACK_ENV) or "").strip().lower()
    return value not in ("off", "0", "false", "no")


def fallback_model(requested: str = "") -> str:
    """本次请求在合格故障发生时接管的模型；不可回退时返回空串。

    只有“实际请求的是默认模型且兜底未关闭”才成立：显式选择备用模型
    （`COPILOT_COGNITION_MODEL`）时不再反向回退，避免绕圈。
    """
    if not fallback_enabled():
        return ""
    if resolve_model(requested) != DEFAULT_MODEL:
        return ""
    return FALLBACK_MODEL


def fallback_decision(exc: object) -> dict:
    """一次失败是否允许换模型重试一次；返回事实与理由，不产生授权。

    `reason` 是稳定的机器可读原因（`allowed:*` / `denied:*`），直接进入调用记录，
    供审计判断“这次为什么没有回退”，而不是只能看到一句自然语言。
    """
    error_class = str(getattr(exc, "error_class", "") or "")
    error_code = str(getattr(exc, "code", "") or "")
    upstream_code = str(getattr(exc, "upstream_code", "") or "")
    facts = {"error_class": error_class, "error_code": error_code,
             "upstream_code": upstream_code, "http_status":
             getattr(exc, "http_status", None)}
    if not isinstance(exc, CognitionError):
        return {**facts, "allow": False, "reason": "denied:not-a-cognition-error"}
    if error_class in NO_FALLBACK_ERROR_CLASSES:
        return {**facts, "allow": False, "reason": f"denied:{error_class}"}
    if error_class == UPSTREAM_RATE_LIMIT:
        if upstream_code in MODEL_LEVEL_RATE_LIMIT_CODES:
            return {**facts, "allow": True,
                    "reason": f"allowed:{UPSTREAM_RATE_LIMIT}:{upstream_code}"}
        # 账户额度耗尽、支付问题或无法归因的 429 一律不回退：换模型掩盖不了
        # 账户/公共通道问题。
        return {**facts, "allow": False,
                "reason": "denied:account-quota-or-unknown-rate-limit"}
    if error_class in FALLBACK_ERROR_CLASSES:
        return {**facts, "allow": True, "reason": f"allowed:{error_class}"}
    if error_code in FALLBACK_ERROR_CODES:
        return {**facts, "allow": True, "reason": f"allowed:{error_code}"}
    if not error_class:
        # 解析/质量/参数类失败：换模型不会改善，且可能把质量问题掩盖成通道问题。
        return {**facts, "allow": False, "reason": "denied:reply-quality-or-request"}
    return {**facts, "allow": False, "reason": f"denied:{error_class}"}


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
    """严格判定本机 CPA 端点：http(s)、回环主机、无 userinfo、无 query/fragment。

    `base_url` 会被原样拼到 `<base>/responses` 并写进任务状态与 `--json` 输出；
    只允许裸端点或 `/v1`，拒绝其它 path 与 query/fragment，避免把
    `?token=...` 之类的敏感内容带进持久化位置。
    """
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
    if parsed.path.rstrip("/") not in ("", "/v1") or parsed.query \
            or parsed.fragment:
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
        raise CognitionUsageError(
            "cognition_credentials_missing",
            f"没有找到本机 CPA 的 {env_name} 凭据（环境变量 / "
            f"{REASONIX_ENV_FILE} / {ZCODE_PROVIDER_FILE} 均为空）。"
            "不新增 Provider、账户或密钥；请先在既有授权配置中补齐。")
    return key


def resolve_base_url() -> str:
    """只使用既有本机 CPA 出口；不提供任何外部 Provider 回退。"""
    raw = str(os.environ.get("COPILOT_COGNITION_BASE_URL") or BASE_URL).strip()
    if not _is_local_cpa_url(raw):
        raise CognitionUsageError(
            "cognition_route_not_local",
            f"认知只允许本机 CPA 出口（http(s)://127.0.0.1:8317，"
            f"无 userinfo），拒绝 {_safe_url(raw)}；不新增或切换 Provider。")
    return raw.rstrip("/")


# ── 请求构建与零工具不变量 ─────────────────────────────────────────────────
_COMMON_RULES = """- 你没有、也不能使用任何工具；不能读取文件、执行命令、访问网络或修改代码。
- 只基于本轮材料作答；不得虚构文件、行号、测试结果或工程事实。
- 无法从材料确认的内容写入 unverified，不得假装已经检查。
- 你的输出只是建议：ZCode 主代理负责工程执行与技术决策，不要要求或代替它执行。
- 只输出一个 JSON 对象（可放在单个 ```json 围栏内），不要输出其他文字。"""

REVIEW_INSTRUCTIONS = """你是 Copilot 的独立复核者（认知 REVIEW）。你只能基于本轮材料做一次性审查：
""" + _COMMON_RULES + """

JSON 结构：
{
  "schema": "copilot-cognition-review/v1",
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

_COGNITIVE_RULES = """- 你不承担工程工作：不写代码、不阅读仓库、不跑测试、不维护台账、不做机械式逐行审查。
- 不要输出逐文件改动清单、实施步骤流水账或重复的检查流程；只给判断与可采用的认知结果。
- 你的建议是认知输入：ZCode 主代理决定是否采纳、如何实现、如何验证。
- 不要要求 ZCode 必须服从；也不要把“建议调用更多工具/模型”当作结论。"""

CONSULT_INSTRUCTIONS = """你是 Copilot 的默认认知引擎（认知 CONSULT）。你只做一次性高价值判断：
""" + _COMMON_RULES + "\n" + _COGNITIVE_RULES + """

JSON 结构（所有字段都必须出现；不适用的数组用空数组）：
{
  "schema": "copilot-cognition-think/v2",
  "judgment": "核心判断（一句话结论）",
  "goal_understanding": "对用户真实目标与隐含需求的理解",
  "essence": "问题本质、现状与关键矛盾",
  "strategy": "推荐方向与适用条件",
  "alternatives": ["备选方案及其与推荐方案的差别"],
  "tradeoffs": ["关键取舍：选择什么、放弃什么、代价是什么"],
  "risks": ["主要风险、隐含依赖与可能遗漏"],
  "priority": ["优先级与下一步重点"],
  "investigation": ["建议优先核实的调查方向"],
  "assumptions": ["结论依赖但尚未证实的假设"],
  "decisions": ["需要用户决定的事项（产品方向/优先级/授权）"],
  "verify": ["需要重点关注的验证结果或信号"],
  "reconsult": ["出现哪些新事实时应重新咨询"],
  "unverified": ["无法从材料确认的事项"],
  "agenda": [
    {"kind": "core-question|wrong-premise|missing-risk|completion-gap|decision|verify|counterexample",
     "text": "一句话：ZCode 最该注意的认知事项"}
  ]
}

议程（agenda，可选）不是计划也不是任务清单：只列 1–4 条真正影响判断的事项，
每条一句话。kind 含义：core-question 最需要回答的问题、wrong-premise 可能的
错误前提、missing-risk 易遗漏风险、completion-gap 完成条件缺项、decision 值得
单独分析的取舍、verify 最值得优先验证的事实、counterexample 可能推翻结论的反例。
没有新增内容时给空数组，不要复述材料。

阶段重点：
- understanding（任务启动）：先确认目标理解、问题判断、关键矛盾、初步策略、主要风险与调查方向；
- development（执行期）：针对本次新证据或新取舍给出是否调整策略的判断；
- stage（阶段成果）：判断是否偏离目标、还有哪些未解决问题、下一阶段方向；
- closure（收口）：判断目标达成情况、重要遗漏与剩余风险，不替代工程验收。"""

PLAN_INSTRUCTIONS = """你是 Copilot 的默认规划认知引擎（认知 PLAN）。你只做一次性方案与规划判断：
""" + _COMMON_RULES + "\n" + _COGNITIVE_RULES + """
- 你提供战略与规划建议，不规定全部具体代码操作；ZCode 负责最终技术方案与执行计划。
- 只使用已核实的现场事实；材料不足时明确写入 assumptions / unverified，不猜测。

JSON 结构（所有字段都必须出现；不适用的数组用空数组）：
{
  "schema": "copilot-cognition-think/v2",
  "judgment": "核心判断（推荐哪条路线，一句话）",
  "goal_understanding": "对目标与完成条件的理解",
  "essence": "问题本质、约束与关键矛盾",
  "strategy": "推荐方案及其成立条件",
  "alternatives": ["备选方案与不采用的理由"],
  "tradeoffs": ["方案比较中的关键取舍与代价"],
  "risks": ["实施风险、依赖与可能遗漏"],
  "priority": ["实施优先级与资源分配"],
  "phase_goals": ["阶段目标与每阶段的完成标志"],
  "assumptions": ["方案成立所依赖的关键假设"],
  "omissions": ["调查或方案中可能遗漏的问题"],
  "decisions": ["需要用户决定的事项"],
  "verify": ["验收关注点与关键验证信号"],
  "reconsult": ["出现哪些新事实时应重新规划"],
  "unverified": ["无法从材料确认的事项"],
  "agenda": [
    {"kind": "core-question|wrong-premise|missing-risk|completion-gap|decision|verify|counterexample",
     "text": "一句话：ZCode 最该注意的认知事项"}
  ]
}

议程（agenda，可选）不是实施步骤：只列 1–4 条真正影响方案或验收的事项，每条
一句话；kind 含义同 CONSULT。没有新增内容时给空数组，不要复述已知事实。"""

MODE_INSTRUCTIONS = {
    "CONSULT": CONSULT_INSTRUCTIONS,
    "PLAN": PLAN_INSTRUCTIONS,
    "REVIEW": REVIEW_INSTRUCTIONS,
}

# 阶段专属提示：只改变输出重点，不改变 JSON 契约。
STAGE_FOCUS = {
    "understanding": ("本次是任务启动理解：重点给出目标理解、问题判断、关键矛盾、"
                      "初步策略、主要风险与优先调查方向；不要假装已经掌握现场事实。"),
    "planning": ("本次是方案规划：重点比较可行路线、推荐方案、阶段目标、优先级、"
                 "验收关注点与可能遗漏。"),
    "development": ("本次是执行期动态判断：重点判断新证据是否改变目标理解、方案方向、"
                    "优先级或关键风险，并给出下一步策略调整。"),
    "stage": ("本次是阶段成果复盘：判断是否偏离最初目标、哪些问题仍未解决、"
              "下一阶段应优先做什么。"),
    "closure": ("本次是收口前综合判断：判断真实目标是否达成（测试与自检通过不等于"
                "业务成功）、是否还有未解决的关键风险或能推翻结论的反例、证据是否"
                "支持结论、此前的认知判断哪些已不再成立；工程验收仍由 ZCode 依据"
                "真实证据完成。"),
}


def normalize_stage(stage: str, *, mode: str = "") -> str:
    """校验生命周期阶段；省略时按模式给出默认阶段。"""
    value = str(stage or "").strip().lower()
    if not value:
        return "planning" if str(mode).strip().upper() == "PLAN" \
            else DEFAULT_STAGE
    if value not in COGNITION_STAGES:
        raise CognitionUsageError(
            "cognition_stage_invalid",
            f"未知认知阶段 {stage!r}；只接受 " + " / ".join(COGNITION_STAGES) + "。")
    return value


def normalize_mode(mode: str) -> str:
    """归一大写并校验模式；未知模式失败关闭，不静默当作 REVIEW。

    THINK 是 v8.4-v8.5 的旧名称：读取旧记录、旧调用方仍可用，但统一归一到
    CONSULT；新流程只产生 CONSULT / PLAN / REVIEW。
    """
    value = str(mode or "").strip().upper()
    if value in CALL_MODES:
        return value
    if value in LEGACY_MODE_ALIASES:
        return LEGACY_MODE_ALIASES[value]
    raise CognitionUsageError(
        "cognition_mode_invalid",
        f"未知认知模式 {mode!r}；只接受 " + " / ".join(CALL_MODES)
        + "（THINK 是历史别名；DECIDE 已退役）。")


def validate_effort(effort: str) -> str:
    """统一 high：拒绝缺失、xhigh/max 与任何静默降级。"""
    value = str(effort or "").strip().lower()
    if not value:
        raise CognitionUsageError(
            "cognition_effort_missing",
            "未指定推理强度；认知统一使用 high，不允许缺失时回退到 medium。")
    if value == "high":
        return value
    raise CognitionUsageError(
        "cognition_effort_not_pinned",
        f"推理强度 {effort!r} 被拒绝：认知统一 high，"
        "不允许降级到 low/medium，也不使用 xhigh/max。")


def resolve_model(model: str = "") -> str:
    """模型解析：显式参数 > `COPILOT_COGNITION_MODEL` > 默认 SOL；拒绝注册表外模型。

    显式通道用于选择已注册模型（例如把认知临时切到 `gpt-6.2-astra`）。默认仍是
    SOL；SOL 发生可归因故障时由调用层用 `FALLBACK_MODEL` 接管**同一次**调用
    （见 `fallback_decision`），不写默认值、不切换 Provider；显式选择 Astra 时
    不存在反向回退。未知或历史模型名一律拒绝，避免把路由意外改到别的 Provider。
    """
    value = str(model or "").strip()
    if not value:
        value = str(os.environ.get(MODEL_ENV, "") or "").strip()
    if not value:
        value = DEFAULT_MODEL
    if value not in SUPPORTED_MODELS:
        raise CognitionUsageError(
            "cognition_model_unsupported",
            f"模型 {value!r} 不在支持列表（{', '.join(SUPPORTED_MODELS)}）；"
            f"默认模型是 {DEFAULT_MODEL}，未发送请求。")
    return value


def model_key(model: str = "", provider: str = "") -> str:
    """退避/统计隔离键：`provider:model`。"""
    return f"{str(provider or PROVIDER).strip()}:{resolve_model(model)}"


def normalize_context_text(text: object) -> str:
    """复用键用的上下文归一：只消除空白差异，不改变实际发送内容。

    行尾空白、制表符与连续空行不属于“新的认知对象”；把它们归一后，恢复上下文
    或重新排版材料不会让同一问题再次付费。代码块缩进保留（只压缩单行内连续空白
    与行首行尾），避免把结构不同的材料误判为同一对象。
    """
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def validate_materials(materials: object, mode: str = "CONSULT") -> str:
    """材料前置校验：非空、字符上限；dry-run 与真实调用共用同一口径。

    认知模式（CONSULT / PLAN）的上限比 REVIEW 更紧：它们只接收主代理整理过的
    认知材料，完整仓库、冗长日志与重复报告应在本地消化，超限直接拒绝而不是
    静默截断。
    """
    resolved = normalize_mode(mode)
    limit = MATERIALS_LIMITS[resolved]
    if not isinstance(materials, str) or not materials.strip():
        raise CognitionUsageError("cognition_materials_empty", "认知材料为空。")
    if len(materials) > limit:
        raise CognitionUsageError(
            "cognition_materials_too_large",
            f"认知{resolved} 材料 {len(materials)} 字符超过上限 {limit}；"
            + ("请精简为目标/基线/已确认事实/已尝试方案/证据摘要/本次问题后重试，"
               "不静默截断。" if resolved in COGNITIVE_MODES else
               "请主代理整理必要材料后重试，不静默截断。"))
    return materials


def probe_image_bytes(data: bytes) -> tuple[str, int | None, int | None]:
    """按魔数识别 PNG / JPEG / WebP，并尽量读出显示尺寸；不解码像素。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return "image/png", width or None, height or None
    if data.startswith(b"\xff\xd8\xff"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xD8, 0xD9):
                index += 2
                continue
            if index + 4 > len(data):
                break
            length = int.from_bytes(data[index + 2:index + 4], "big")
            if length < 2 or index + 2 + length > len(data):
                break
            # SOF0..SOF15（跳过 DHT/JPG/DAC 等非 SOF 标记）
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height = int.from_bytes(data[index + 5:index + 7], "big")
                width = int.from_bytes(data[index + 7:index + 9], "big")
                return "image/jpeg", width or None, height or None
            index += 2 + length
        return "image/jpeg", None, None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        fourcc = data[12:16]
        if fourcc == b"VP8X":
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return "image/webp", width, height
        if fourcc == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return "image/webp", width or None, height or None
        if fourcc == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return "image/webp", width, height
        return "image/webp", None, None
    raise CognitionUsageError(
        "cognition_image_format_unsupported",
        "只接受真实 PNG / JPEG / WebP 文件；不按扩展名猜测，也不用 OCR 或文字描述代替像素。")


def load_images(paths: list[str], *, labels: list[str] | None = None) -> list[dict]:
    """读取本地图片为 Responses API 的 input_image 内容块。

    返回项含 `data_url`（仅用于请求体，绝不写审计）与可公开的元数据。
    任何一张不可读、超限或格式不符都整体失败，不静默跳过。
    """
    if not paths:
        return []
    if len(paths) > IMAGE_MAX_COUNT:
        raise CognitionUsageError(
            "cognition_image_count_exceeded",
            f"一次最多 {IMAGE_MAX_COUNT} 张图片，收到 {len(paths)} 张；"
            "请只给代表性页面/状态，不整包上传。")
    if labels is not None and len(labels) != len(paths):
        raise CognitionUsageError(
            "cognition_image_label_mismatch",
            f"--image 有 {len(paths)} 个，--image-note 有 {len(labels)} 个；"
            "两者必须一一对应。")
    images = []
    total = 0
    for index, path in enumerate(paths, start=1):
        resolved = os.path.abspath(os.path.expanduser(str(path)))
        if not os.path.isfile(resolved):
            raise CognitionUsageError(
                "cognition_image_missing",
                f"第 {index} 张图片不存在：{resolved}")
        size = os.path.getsize(resolved)
        if size <= 0:
            raise CognitionUsageError(
                "cognition_image_empty", f"第 {index} 张图片为空文件：{resolved}")
        if size > IMAGE_MAX_BYTES:
            raise CognitionUsageError(
                "cognition_image_too_large",
                f"第 {index} 张图片 {size} 字节，超过单张上限 "
                f"{IMAGE_MAX_BYTES}；请先导出适合评审的尺寸，不静默压缩。")
        total += size
        if total > IMAGE_MAX_TOTAL_BYTES:
            raise CognitionUsageError(
                "cognition_image_total_too_large",
                f"图片总量 {total} 字节超过上限 {IMAGE_MAX_TOTAL_BYTES}；"
                "请减少数量或尺寸。")
        with open(resolved, "rb") as fh:
            data = fh.read()
        mime, width, height = probe_image_bytes(data)
        if mime not in IMAGE_MIMES:
            raise CognitionUsageError(
                "cognition_image_format_unsupported",
                f"第 {index} 张图片格式 {mime!r} 不在允许范围 {IMAGE_MIMES}。")
        import base64
        images.append({
            "path": resolved,
            "name": os.path.basename(resolved),
            "mime": mime,
            "bytes": size,
            "sha256": hashlib.sha256(data).hexdigest(),
            "width": width,
            "height": height,
            "label": (labels[index - 1] if labels else "").strip(),
            "data_url": "data:%s;base64,%s" % (
                mime, base64.b64encode(data).decode("ascii")),
        })
    return images


def validate_images(images: object) -> list[dict]:
    """请求构造前的图像校验：结构、数量与内容块形状。"""
    if images in (None, []):
        return []
    if not isinstance(images, list):
        raise CognitionUsageError("cognition_image_invalid", "images 必须是列表。")
    if len(images) > IMAGE_MAX_COUNT:
        raise CognitionUsageError(
            "cognition_image_count_exceeded",
            f"一次最多 {IMAGE_MAX_COUNT} 张图片，收到 {len(images)} 张。")
    checked = []
    for index, item in enumerate(images, start=1):
        if not isinstance(item, dict):
            raise CognitionUsageError(
                "cognition_image_invalid", f"第 {index} 张图片元数据不是对象。")
        data_url = item.get("data_url")
        mime = str(item.get("mime") or "")
        if not isinstance(data_url, str) or not data_url.startswith("data:"):
            raise CognitionUsageError(
                "cognition_image_invalid", f"第 {index} 张图片缺少 data_url。")
        if mime not in IMAGE_MIMES:
            raise CognitionUsageError(
                "cognition_image_format_unsupported",
                f"第 {index} 张图片格式 {mime!r} 不在允许范围。")
        checked.append(item)
    return checked


def image_public_metadata(images: list[dict] | None) -> list[dict]:
    """可写入状态/审计的图片元数据；不含 path 与 base64。"""
    public = []
    for item in images or []:
        public.append({
            "name": item.get("name") or "",
            "mime": item.get("mime") or "",
            "bytes": int(item.get("bytes") or 0),
            "sha256": item.get("sha256") or "",
            "width": item.get("width"),
            "height": item.get("height"),
            "label": item.get("label") or "",
        })
    return public


def images_digest(images: list[dict] | None) -> str:
    """图片集合摘要：用于判断“同一问题是否换了图”，不含像素本体。"""
    payload = [
        {"sha256": item.get("sha256") or "", "label": item.get("label") or ""}
        for item in images or []
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _payload_input(materials: str, images: list[dict] | None) -> object:
    """文本时保持字符串；有图片时用 Responses API 的 content 块数组。"""
    if not images:
        return materials
    content = [{"type": "input_text", "text": materials}]
    for item in images:
        content.append({"type": "input_image",
                        "image_url": item["data_url"]})
    return [{"role": "user", "content": content}]


def build_payload(materials: str, *, mode: str = "CONSULT",
                  model: str = "", effort: str = REASONING_EFFORT,
                  max_output_tokens: int | None = DEFAULT_MAX_OUTPUT_TOKENS,
                  instructions: str = "",
                  images: list[dict] | None = None) -> dict:
    """构建 Responses API 请求体；工具字段一律不存在、推理强度固定 high。"""
    resolved_mode = normalize_mode(mode)
    validate_materials(materials, resolved_mode)
    resolved_effort = validate_effort(effort)
    checked_images = validate_images(images)
    resolved_model = resolve_model(model)
    payload = {
        "model": resolved_model,
        "input": _payload_input(materials, checked_images),
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
            raise CognitionUsageError(
                "cognition_unsupported_param",
                f"请求体包含推理模型不支持的参数 {key}；不要发送采样参数。")
    include = payload.get("include")
    if isinstance(include, list) and any("logprobs" in str(item) for item in include):
        raise CognitionUsageError(
            "cognition_unsupported_param",
            "请求体 include 含 logprobs；认知不请求 token 概率。")


def assert_zero_tool(payload: object, _path: str = "payload") -> None:
    """能力级零工具校验：任何工具定义键都视为安全事件。"""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key) in TOOL_KEYS:
                raise CognitionUsageError(
                    "cognition_tools_in_payload",
                    f"请求体 {_path}.{key} 含工具定义；零工具不变量被破坏。")
            assert_zero_tool(value, f"{_path}.{key}")
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            assert_zero_tool(value, f"{_path}[{index}]")
    if _path == "payload" and payload.get("tool_choice") != "none":
        raise CognitionUsageError(
            "cognition_tool_choice_invalid",
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


def _header_lookup(headers, name: str) -> str:
    """大小写不敏感地读取单个响应头；只返回值，不记录整组头。"""
    if headers is None:
        return ""
    try:
        value = headers.get(name)
        if value:
            return str(value).strip()
    except (AttributeError, TypeError):
        pass
    try:
        items = headers.items()
    except (AttributeError, TypeError):
        return ""
    target = name.lower()
    for key, value in items:
        if str(key).lower() == target and value:
            return str(value).strip()
    return ""


def _parse_retry_after_date(value: str) -> int | None:
    """RFC 7231 HTTP-date → 距现在的秒数（≥1）。

    时区缺失按 UTC 解释（HTTP-date 规定为 GMT）；已过去的时间或无法解析的值返回
    None，由本地退避策略兜底——不把过去时间当成"立即重试"，也不猜时区。
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        delta = (parsed - datetime.now(timezone.utc)).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None
    if delta <= 0:
        return None
    return max(1, int(round(delta)))


def _retry_after_seconds(headers, payload: object,
                         raw_out: dict | None = None) -> int | None:
    """提取 Retry-After（秒），优先响应头，其次错误体里的 headers。

    支持三种形式：数值秒、`retry-after-ms`（毫秒）、RFC 7231 HTTP-date（换算为
    剩余秒数）。HTTP-date 已过期或取值无法解析时返回 None —— 由本地退避策略兜底，
    不猜测、不把过去时间当作"可以立刻重试"。

    `raw_out` 只用于保留原始头部值供审计追溯，不参与判定。
    """
    sources = [headers]
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            sources.append(err.get("headers"))
        sources.append(payload.get("headers"))
    seconds = None
    for source in sources:
        if source is None:
            continue
        try:
            items = list(source.items())
        except (AttributeError, TypeError):
            continue
        for key, value in items:
            key = str(key).lower()
            if key not in ("retry-after", "retry-after-ms"):
                continue
            text = str(value).strip()
            if raw_out is not None and key == "retry-after" and text \
                    and not raw_out.get("retry_after_raw"):
                raw_out["retry_after_raw"] = text
            try:
                number = float(text)
            except (TypeError, ValueError):
                if key == "retry-after":
                    dated = _parse_retry_after_date(text)
                    if dated is not None:
                        return dated
                continue
            if number <= 0:
                continue
            if key == "retry-after":
                return max(1, int(round(number)))
            if seconds is None:
                seconds = max(1, int(round(number / 1000.0)))
    return seconds


def _payload_error_message(payload: object) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or "")
        if err:
            return str(err)
        return str(payload.get("message") or "")
    return ""


def _payload_error_code(payload: object) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("code") or "")
        return str(payload.get("code") or "")
    return ""


def classify_upstream_failure(status: int, payload: object) -> str:
    """把一次上游 HTTP 错误映射成稳定分类；只描述事实，不决定重试。

    顺序很重要：`auth_unavailable` 先于 5xx 判定，避免 CPA 冷却被当成认证失效；
    账户额度/认证/安全终止类结构化错误码先于 5xx 判定，避免被包在 502 里的账户或
    终止语义触发换模型；408/`request_timeout` 先于过载判定，保留"流在完成前断开"的语义。
    """
    code = _payload_error_code(payload).lower()
    message = (_payload_error_message(payload)
               + " " + (str((payload or {}).get("error") or "")
                        if isinstance(payload, dict) else "")).lower()
    text = f"{code} {message}"
    status = int(status or 0)
    if status == 429 or code in ("usage_limit_reached", "model_cooldown"):
        return UPSTREAM_RATE_LIMIT
    if "auth_unavailable" in text or "no auth available" in text:
        # 凭据冷却 / 调度结果：不是认证失效，不得据此轮换凭据或换 Provider。
        return UPSTREAM_CREDENTIAL_COOLING
    if code in ACCOUNT_QUOTA_ERROR_CODES or any(
            token in text for token in ACCOUNT_QUOTA_ERROR_CODES):
        # 被 5xx 外壳包裹的账户额度/支付错误仍是账户问题，不得换模型。
        return UPSTREAM_RATE_LIMIT
    if code in AUTH_ERROR_CODES or any(
            token in text for token in AUTH_ERROR_CODES):
        # 同理：包在 5xx 里的凭据/账户失效不是瞬时容量问题。
        return UPSTREAM_AUTH
    if code in TERMINAL_REFUSAL_ERROR_CODES or any(
            token in text for token in TERMINAL_REFUSAL_ERROR_CODES):
        # 安全终止/用户取消：请求本身被拒，换模型只会重复同一拒绝。
        return UPSTREAM_CLIENT
    if status == 408 or code == "request_timeout" \
            or "stream disconnected before completion" in text \
            or "stream closed before response.completed" in text:
        return UPSTREAM_STREAM_TIMEOUT
    if "server_is_overloaded" in text or "overloaded" in text or status == 529:
        return UPSTREAM_OVERLOADED
    if status in (401, 403):
        return UPSTREAM_AUTH
    if status in (400, 404, 405, 409, 422):
        return UPSTREAM_CLIENT
    return UPSTREAM_SERVER


def _upstream_failure_detail(status: int, upstream_code: str,
                             retry_after: int | None, payload: object,
                             error_class: str) -> str:
    """构造脱敏、限长的失败说明；保留诊断所需的原始状态与上游错误码。"""
    parts = [f"HTTP {status}",
             f"upstream_code={upstream_code or 'unknown'}",
             f"class={error_class}"]
    if retry_after:
        parts.append(f"Retry-After={retry_after}s")
    message = _payload_error_message(payload)
    if message:
        parts.append("upstream_message=" + _safe_detail(message, 160))
    hint = {
        UPSTREAM_OVERLOADED: "上游瞬时容量不足，不是本地认证或配置故障",
        UPSTREAM_STREAM_TIMEOUT: "上游流在 response.completed 之前断开，本次无有效回复",
        UPSTREAM_CREDENTIAL_COOLING: "CPA 凭据冷却窗口内的本地拒绝，不是认证失效，不轮换凭据",
        UPSTREAM_AUTH: "上游认证失败，需人工核实凭据",
        UPSTREAM_RATE_LIMIT: "上游限流/配额，按 Retry-After 等待",
        UPSTREAM_SERVER: "上游 5xx 错误",
        UPSTREAM_CLIENT: "上游拒绝请求本身，需检查请求构造",
    }.get(error_class, "上游错误")
    return f"{hint}（{'；'.join(parts)}）；未自动重试、未切换 Provider。"


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
        raise CognitionResponseError("cognition_output_unparseable", str(exc)) from None
    if not isinstance(value, dict):
        raise CognitionResponseError("cognition_output_not_object",
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
    except CognitionResponseError as direct:
        if direct.code == "cognition_output_not_object":
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
        except CognitionResponseError:
            continue
        if value.get("schema") == schema:
            candidates.append(value)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise CognitionResponseError("cognition_output_ambiguous",
                               f"{len(candidates)} 个符合契约的对象")
    raise CognitionResponseError("cognition_output_unparseable", first_error.detail)


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
        raise CognitionResponseError(f"cognition_{name}_invalid",
                               f"{name} 必须是字符串数组")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise CognitionResponseError(
                f"cognition_{name}_invalid",
                f"{name} 含非字符串元素：{type(item).__name__}")
        if item.strip():
            out.append(item.strip())
    return out


def _optional_string(item: dict, key: str, finding_id: str) -> str:
    value = item.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CognitionResponseError(
            "cognition_finding_field_invalid",
            f"{finding_id}: {key} 必须是字符串")
    return value.strip()


def normalize_review(data: dict) -> dict:
    """校验并归一化认知输出；PASS 与 blocking/major/unverified 冲突时降级。"""
    if data.get("schema") != REVIEW_SCHEMA:
        raise CognitionResponseError("cognition_schema_mismatch",
                               f"schema={data.get('schema')!r}")
    verdict = _text(data.get("verdict")).lower()
    if verdict not in VERDICTS:
        raise CognitionResponseError("cognition_verdict_invalid", repr(data.get("verdict")))
    findings = data.get("findings")
    if findings is None:
        findings = []
    if not isinstance(findings, list) or any(
            not isinstance(item, dict) for item in findings):
        raise CognitionResponseError("cognition_findings_invalid", "findings 必须是对象数组")
    normalized: list[dict] = []
    used_ids: set[str] = set()
    for index, item in enumerate(findings, start=1):
        raw_id = item.get("id")
        if raw_id is not None and not isinstance(raw_id, str):
            raise CognitionResponseError(
                "cognition_finding_field_invalid", f"第 {index} 项 id 必须是字符串")
        severity = _text(item.get("severity")).lower()
        if severity not in SEVERITIES:
            raise CognitionResponseError(
                "cognition_severity_invalid",
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
        raise CognitionResponseError("cognition_output_empty", "输出为空")
    return normalize_review(_parse_json_text(cleaned, REVIEW_SCHEMA))


THINK_TEXT_FIELDS = ("judgment", "goal_understanding", "essence", "strategy")
THINK_LIST_FIELDS = ("alternatives", "tradeoffs", "risks", "priority",
                     "investigation", "phase_goals", "assumptions", "omissions",
                     "decisions", "verify", "reconsult", "unverified")


def normalize_think(data: dict) -> dict:
    """校验并归一化认知输出；schema 必须一致，核心判断不能为空。

    单一超集结构：CONSULT 与 PLAN 共用同一契约，阶段差异只体现在内容重点与
    提示词，不引入第二套台账。缺失的可选数组按空数组处理，类型不符或没有判断
    内容时拒绝消费。
    """
    if data.get("schema") != THINK_SCHEMA:
        raise CognitionResponseError("cognition_schema_mismatch",
                               f"schema={data.get('schema')!r}")
    result: dict = {}
    for key in THINK_TEXT_FIELDS:
        value = data.get(key)
        if value is None:
            result[key] = ""
        elif isinstance(value, str):
            result[key] = value.strip()
        else:
            raise CognitionResponseError(
                "cognition_think_field_invalid", f"{key} 必须是字符串")
    for key in THINK_LIST_FIELDS:
        result[key] = _string_list(data.get(key), key)
    # 议程是可选增量：缺失视为空（旧调用方与旧材料继续有效），出现时只做确定性
    # 归一（固定类型、短文本、去重、限长），不把自由格式带进状态。
    agenda_raw = data.get("agenda")
    if agenda_raw is not None and not isinstance(agenda_raw, list):
        raise CognitionResponseError("cognition_agenda_invalid", "agenda 必须是数组")
    result["agenda"] = _RP.normalize_agenda(agenda_raw)
    if not result["judgment"] and not result["strategy"]:
        raise CognitionResponseError(
            "cognition_think_empty", "judgment 与 strategy 均为空：回复没有认知内容。")
    return result


def parse_think_output(text: str) -> dict:
    cleaned = str(text or "").lstrip("\ufeff").strip()
    if not cleaned:
        raise CognitionResponseError("cognition_output_empty", "输出为空")
    return normalize_think(_parse_json_text(cleaned, THINK_SCHEMA))


# ── 发送（一次，不重试） ───────────────────────────────────────────────────
def _network():
    """惰性导入网络栈：只有真正发请求时才付出 urllib/ssl/email 的导入成本。

    `urllib.request` 会连带 `ssl`、`http.client`、`email`（实测约 40–45 ms）。
    生命周期命令（`start`/`drive`/`record`/`closure` 等）和 Stop/SessionStart
    钩子每次都要启动进程，但绝大多数不调用认知；把这些导入放在请求路径内，
    只读/回填命令少付一次网络栈开销，而认知调用本身的行为与异常语义不变。
    """
    import http.client
    import socket
    import urllib.error
    import urllib.request
    return http.client, socket, urllib.error, urllib.request


_NO_REDIRECT: type | None = None


def _no_redirect_class():
    """3xx 一律失败的 opener handler；类按需构建，避免模块级导入网络栈。"""
    global _NO_REDIRECT
    if _NO_REDIRECT is None:
        _, _, _, request_module = _network()

        class _NoRedirect(request_module.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        _NO_REDIRECT = _NoRedirect
    return _NO_REDIRECT


def __getattr__(name: str):
    # `_NoRedirect` 对外仍是可取的模块属性（历史测试与外部调用直接构造它），
    # 只是延迟到首次访问时才真正导入 urllib.request。
    if name == "_NoRedirect":
        return _no_redirect_class()
    raise AttributeError(name)


def _post_once(url: str, key: str, body: dict, timeout: float) -> dict:
    http_client, socket, urllib_error, urllib_request = _network()
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib_request.Request(
        url, data=data, method="POST",
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"})
    # 显式空 ProxyHandler：本机 CPA 请求不经过环境代理，避免凭据转发到第三方。
    opener = urllib_request.build_opener(
        urllib_request.ProxyHandler({}), _no_redirect_class())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
            headers = getattr(response, "headers", None)
    except urllib_error.HTTPError as exc:
        try:
            raw = exc.read()
        except (OSError, http_client.HTTPException):
            raw = b""
        payload = None
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            pass
        error_headers = getattr(exc, "headers", None)
        request_id = (_header_lookup(error_headers, "X-Request-Id")
                      or _header_lookup(error_headers, "OpenAI-Request-Id"))
        trace_id = _header_lookup(error_headers, "X-Cpa-Trace-Id")
        retry_raw: dict = {}
        retry_after = _retry_after_seconds(error_headers, payload, retry_raw)
        retry_after_raw = str(retry_raw.get("retry_after_raw") or "")
        upstream_code = _payload_error_code(payload)
        quota = _quota_signal(exc.code, payload)
        if quota:
            raise CognitionQuotaError(
                "cognition_quota",
                f"上游限流/冷却（HTTP {exc.code}，code={quota['code']}"
                + (f"，Retry-After={retry_after}s" if retry_after else "")
                + "）；未自动重试、未切换 Provider。",
                http_status=int(exc.code), retry_after=retry_after,
                retry_after_raw=retry_after_raw,
                upstream_code=upstream_code or str(quota["code"]),
                error_class=UPSTREAM_RATE_LIMIT, request_id=request_id,
                trace_id=trace_id, response_received=True) from None
        error_class = classify_upstream_failure(exc.code, payload)
        error_cls = _UPSTREAM_ERROR_CLASSES[error_class]
        raise error_cls(
            ERROR_CLASS_CODE[error_class],
            _upstream_failure_detail(exc.code, upstream_code, retry_after,
                                     payload, error_class),
            http_status=int(exc.code), retry_after=retry_after,
            retry_after_raw=retry_after_raw,
            upstream_code=upstream_code, error_class=error_class,
            request_id=request_id, trace_id=trace_id,
            response_received=True) from None
    except (socket.timeout, TimeoutError):
        raise CognitionTimeoutError(
            "cognition_timeout",
            f"请求在 {timeout:g}s 内未完成；结果未知，未自动重试。",
            error_class=CLIENT_TIMEOUT) from None
    except urllib_error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise CognitionTimeoutError(
                "cognition_timeout",
                f"请求在 {timeout:g}s 内未完成；结果未知，未自动重试。",
                error_class=CLIENT_TIMEOUT) from None
        raise CognitionUpstreamError("cognition_transport_error",
                               _safe_detail(reason),
                               error_class=NETWORK_ERROR) from None
    except (http_client.HTTPException, OSError) as exc:
        # 响应读到一半中断、连接重置、Content-Length 不足等：请求已发出，
        # 结果未知。显式映射为 CognitionError，确保进入失败审计与状态登记。
        raise CognitionUpstreamError(
            "cognition_transport_error",
            _safe_detail(f"{type(exc).__name__}: {exc}"),
            error_class=NETWORK_ERROR) from None
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise CognitionResponseError("cognition_response_json_invalid", str(exc),
                               error_class=RESPONSE_INCOMPLETE) from None
    if not isinstance(payload, dict):
        raise CognitionResponseError("cognition_response_not_object",
                               type(payload).__name__,
                               error_class=RESPONSE_INCOMPLETE)
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


def run_cognition(materials: str, *, mode: str = "CONSULT", audit_log: str = "",
            model: str = "", effort: str = REASONING_EFFORT,
            max_output_tokens: int | None = DEFAULT_MAX_OUTPUT_TOKENS,
            timeout: float = DEFAULT_TIMEOUT,
            transport=None, api_key: str | None = None,
            base_url: str | None = None, instructions: str = "",
            images: list[dict] | None = None) -> dict:
    """发起一次认知调用（CONSULT / PLAN / REVIEW）；任何异常都显式失败。"""
    resolved_mode = normalize_mode(mode)
    # 模型在一次调用内只解析一次：请求体、身份校验、审计与记录共用同一值。
    model = resolve_model(model)
    payload = build_payload(
        materials, mode=resolved_mode, model=model, effort=effort,
        max_output_tokens=max_output_tokens, instructions=instructions,
        images=images)
    resolved_effort = str(payload["reasoning"]["effort"])
    # 无论 URL 来自环境还是显式参数，都在解析凭据与发送前做同一本机出口校验。
    base = (base_url or resolve_base_url()).rstrip("/")
    if not _is_local_cpa_url(base):
        raise CognitionUsageError(
            "cognition_route_not_local",
            f"认知只允许本机 CPA 出口（http(s)://127.0.0.1:8317，"
            f"无 userinfo），拒绝 {_safe_url(base)}；未解析凭据、未发送请求。")
    url = base + ENDPOINT
    key = api_key if api_key is not None else resolve_api_key()
    body_sha = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    started = time.monotonic()
    public_images = image_public_metadata(images)
    audit_base = {
        "ts": _now_iso(),
        "mode": resolved_mode,
        "provider": PROVIDER,
        "model_requested": model,
        "model_key": model_key(model),
        "reasoning_effort": resolved_effort,
        "reasoning_mode": REASONING_MODE,
        "materials_sha256": hashlib.sha256(
            materials.encode("utf-8")).hexdigest(),
        "materials_chars": len(materials),
        # 只记录图片指纹与尺寸；不记录路径、base64 或像素。
        "images": public_images,
        "image_count": len(public_images),
        "images_sha256": images_digest(images),
        "input_type": "content_blocks" if public_images else "text",
        "request_sha256": body_sha,
        "zero_tool": True,
        "tools_sent": False,
        "tool_choice": "none",
        "attempts": 1,
        "request_attempted": False,
        "send_observed": "not_attempted",
    }
    transport_entered = False
    try:
        transport_entered = True
        response = (transport or _post_once)(url, key, payload, timeout)
        if isinstance(response, dict):
            response.setdefault("_request_attempted", True)
            response.setdefault("_send_observed", "response_received")
    except CognitionError as exc:
        attempted = transport_entered
        # 收到过 HTTP 响应（含错误响应）时发送事实是确定的；客户端超时/网络
        # 异常才保持 unknown。
        if getattr(exc, "response_received", False):
            observed = "response_received"
        else:
            observed = "unknown" if transport_entered else "not_attempted"
        exc.request_attempted = attempted
        exc.send_observed = observed
        _audit_safe(audit_log, {**audit_base, "ok": False,
                           "request_attempted": attempted,
                           "send_observed": observed,
                           "error": exc.code,
                           "error_class": str(getattr(exc, "error_class", "")),
                           "http_status": getattr(exc, "http_status", None),
                           "retry_after": getattr(exc, "retry_after", None),
                           "retry_after_raw": str(getattr(exc, "retry_after_raw", "")),
                           "upstream_code": str(getattr(exc, "upstream_code", "")),
                           "request_id": str(getattr(exc, "request_id", "")),
                           "trace_id": str(getattr(exc, "trace_id", "")),
                           "detail": exc.detail,
                           "elapsed_ms": int((time.monotonic() - started) * 1000)})
        raise
    elapsed_ms = int((time.monotonic() - started) * 1000)
    status = response.pop("_http_status", 0)
    request_attempted = bool(response.pop("_request_attempted", True))
    send_observed = str(response.pop("_send_observed", "response_received"))
    request_id = str(response.pop("_request_id", "") or "")
    trace_id = str(response.pop("_trace_id", "") or "")
    quota = _quota_signal(int(status or 0), response)
    if quota:
        exc = CognitionQuotaError(
            "cognition_quota",
            f"HTTP 200 携带限流/冷却（code={quota['code']}）；未自动重试。",
            http_status=int(status or 0), error_class=UPSTREAM_RATE_LIMIT,
            upstream_code=str(quota["code"]), request_id=request_id,
            trace_id=trace_id, response_received=True)
        _audit_safe(audit_log, {**audit_base, "ok": False, "error": exc.code,
                           "error_class": UPSTREAM_RATE_LIMIT,
                           "http_status": status, "request_id": request_id,
                           "trace_id": trace_id, "elapsed_ms": elapsed_ms})
        raise exc
    try:
        unexpected = find_unexpected_output(response)
        problem = response_problem(response)
        text = extract_text(response) if not problem else ""
    except (AttributeError, TypeError, ValueError, KeyError) as exc:
        _audit_safe(audit_log, {**audit_base, "ok": False,
                           "error": "cognition_malformed_response",
                           "error_class": RESPONSE_INCOMPLETE,
                           "detail": f"{type(exc).__name__}: {exc}",
                           "http_status": status, "elapsed_ms": elapsed_ms})
        raise CognitionResponseError("cognition_malformed_response", str(exc),
                               http_status=int(status or 0),
                               error_class=RESPONSE_INCOMPLETE,
                               response_received=True) from None
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
    common_audit = {"http_status": status,
                    "request_attempted": request_attempted,
                    "send_observed": send_observed,
                    "reasoning_resolved": reasoning_resolved,
                    "reasoning_verified": reasoning_verified,
                    "request_id": request_id, "trace_id": trace_id,
                    "response_id": result_id,
                    "usage": usage, "elapsed_ms": elapsed_ms}
    if unexpected:
        exc = CognitionToolCallError(
            "cognition_unexpected_output_item",
            f"上游返回非良性条目 {unexpected}；已拒绝处理且未执行。")
        _audit_safe(audit_log, {**audit_base, **common_audit, "ok": False,
                           "error": exc.code,
                           "detail": json.dumps(unexpected, ensure_ascii=False)})
        raise exc
    if not identity_ok:
        exc = CognitionIdentityError(
            "cognition_model_identity_mismatch",
            f"请求模型 {model!r}，上游回显 "
            f"{_safe_detail(model_resolved, 200)!r}；该回复不得被消费。")
        _audit_safe(audit_log, {**audit_base, **common_audit, "ok": False,
                           "error": exc.code})
        raise exc
    if echoed_effort and echoed_effort != resolved_effort:
        exc = CognitionReasoningError(
            "cognition_reasoning_effort_mismatch",
            f"请求 reasoning.effort={resolved_effort!r}，上游回显 "
            f"{echoed_effort!r}；推理强度被覆盖，该回复不得被消费。")
        _audit_safe(audit_log, {**audit_base, **common_audit, "ok": False,
                           "error": exc.code})
        raise exc
    if echoed_mode and echoed_mode != REASONING_MODE:
        exc = CognitionReasoningError(
            "cognition_reasoning_mode_mismatch",
            f"请求 reasoning.mode={REASONING_MODE!r}（默认 standard），上游回显 "
            f"{echoed_mode!r}；该回复不得被消费。")
        _audit_safe(audit_log, {**audit_base, **common_audit, "ok": False,
                           "error": exc.code})
        raise exc
    if problem:
        exc = CognitionResponseError("cognition_response_incomplete", problem,
                               http_status=int(status or 0),
                               error_class=RESPONSE_INCOMPLETE,
                               request_id=request_id, trace_id=trace_id,
                               response_received=True)
        _audit_safe(audit_log, {**audit_base, **common_audit, "ok": False,
                           "error": exc.code, "error_class": RESPONSE_INCOMPLETE,
                           "detail": problem})
        raise exc
    text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        if resolved_mode == "REVIEW":
            parsed = parse_review_output(text)
        else:
            parsed = parse_think_output(text)
    except CognitionResponseError as exc:
        _audit_safe(audit_log, {**audit_base, **common_audit, "ok": False,
                           "error": exc.code, "detail": exc.detail,
                           "reply_sha256": text_sha,
                           "reply_chars": len(text)})
        raise CognitionResponseError(exc.code, exc.detail) from None
    result = {
        "schema": (REVIEW_SCHEMA if resolved_mode == "REVIEW"
                   else THINK_SCHEMA),
        "request_attempted": request_attempted,
        "send_observed": send_observed,
        "mode": resolved_mode,
        "provider": PROVIDER,
        "model": model,
        "model_key": model_key(model),
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
        "images": public_images,
        "image_count": len(public_images),
        "images_sha256": audit_base["images_sha256"],
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
            "judgment": parsed["judgment"],
            "goal_understanding": parsed["goal_understanding"],
            "essence": parsed["essence"],
            "strategy": parsed["strategy"],
            "alternatives": parsed["alternatives"],
            "tradeoffs": parsed["tradeoffs"],
            "risks": parsed["risks"],
            "priority": parsed["priority"],
            "investigation": parsed["investigation"],
            "phase_goals": parsed["phase_goals"],
            "assumptions": parsed["assumptions"],
            "omissions": parsed["omissions"],
            "decisions": parsed["decisions"],
            "verify": parsed["verify"],
            "reconsult": parsed["reconsult"],
            "unverified": parsed["unverified"],
            "agenda": parsed["agenda"],
        })
        audit_extra = {
            key: len(parsed[key])
            for key in ("alternatives", "tradeoffs", "risks", "priority",
                        "investigation", "phase_goals", "assumptions",
                        "omissions", "decisions", "verify", "reconsult",
                        "agenda")
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
    """一次认知 REVIEW（用户明确要求的独立复核）。"""
    return run_cognition(materials, mode="REVIEW", **kwargs)


def run_consult(materials: str, **kwargs) -> dict:
    """一次认知 CONSULT（默认认知引擎：理解/判断/风险/策略调整）。"""
    return run_cognition(materials, mode="CONSULT", **kwargs)


def run_plan(materials: str, **kwargs) -> dict:
    """一次认知 PLAN（调查之后的方案比较与规划判断）。"""
    return run_cognition(materials, mode="PLAN", **kwargs)


def run_think(materials: str, **kwargs) -> dict:
    """历史名称：等同 `run_consult`，保留给既有调用方。"""
    return run_cognition(materials, mode="CONSULT", **kwargs)


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
        raise CognitionAuditError("cognition_audit_write_failed",
                            f"无法写入审计日志 {path}：{exc}") from None


def _audit_safe(path: str, record: dict) -> str | None:
    """审计写入失败不掩盖已经发生的远程结果；返回错误码供调用方记录。"""
    try:
        _audit(path, record)
    except CognitionAuditError as exc:
        return exc.code
    return None


# ── 材料、单轮判定与准入 ───────────────────────────────────────────────────
# 提示词/策略版本：进入单轮判定键。提示词、共享上下文或判定规则实质变化时，
# 旧结论不再被当成同一审查对象复用（缓存失效条件见 review_subject_sha256）。
PROMPT_VERSION = "cognition-prompt-v6"

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
    认知与 G2 使用同一份证据来源；完整证据内容仍由主代理放在材料里。
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


def _prior_lines(state: dict) -> list[str]:
    """历史认知判断与未处理议程：让新一轮判断做增量，而不是从零重问。

    只注入最近两次判断的核心结论与采纳状态，以及最近一次仍有效的议程；不含完整
    回复。旧结论失效条件（合同修订、重开、新事件）由生命周期与 `reconsult` 决定，
    这里不替认知判断“是否仍然成立”。
    """
    calls = state.get("cognition_calls") or []
    prior = _RP.prior_cognition(calls, limit=2)
    agenda = _RP.open_agenda(calls)
    if not prior and not agenda:
        return []
    lines = ["## 历史认知判断（只读事实，不是本轮结论）", ""]
    for item in prior:
        lines.append(f"- {item['call_id']} {item['mode']}/{item['stage']} "
                     f"采纳={item['acceptance']}："
                     f"{item['judgment'] or '(无判断文本)'}")
    if agenda:
        lines.append("- 上次议程（仍相关的事项请在本轮继续判断，已处理的不必重复）：")
        lines += [f"  - [{entry['kind']}] {entry['text']}" for entry in agenda]
    lines.append("")
    return lines


def _context_lines(state: dict, model: str = "") -> list[str]:
    """CONSULT / PLAN / REVIEW 共用的任务上下文：一份合同、一套条件与证据。"""
    contract = state.get("contract") or {}
    boundaries = str(contract.get("boundaries") or "").strip()
    return [
        f"- 任务：{state.get('task_id') or '(未命名)'}",
        f"- 风险：{state.get('risk') or 'medium'}",
        f"- 执行后端：{state.get('execution_backend') or 'legacy'}",
        f"- 合同指纹：{state.get('contract_sha256') or '(未登记)'}",
        f"- 模型：{resolve_model(model)}（reasoning.effort={REASONING_EFFORT}，零工具）",
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
        # 不存在的 `text` 键，导致认知从未看到合同全文。
        str(contract.get("done_criteria_text") or "").strip() or "(未登记)",
        "",
        *(["## 合同边界", "", boundaries, ""] if boundaries else []),
        *_prior_lines(state),
        "## 工作项（按任务状态自动注入）",
        "",
        *_action_lines(state),
        "",
        "## 证据登记（按任务状态自动注入）",
        "",
        *_evidence_lines(state),
        "",
    ]


def build_prompt(state: dict, materials: str, *, mode: str = "CONSULT",
                 question: str = "", stage: str = "",
                 model: str = "") -> str:
    """把共享任务上下文与主代理材料拼成一次完整上下文（三模式共用）。"""
    resolved = normalize_mode(mode)
    if resolved == "REVIEW":
        lines = [f"# Copilot 认知{resolved} 材料", "",
                 *_context_lines(state, model)]
        lines += [
            "## 主代理提供的审查材料",
            "",
            materials.strip(),
            "",
            "## 复核要求",
            "",
            "一次性审查以上材料：只报告有材料依据的问题，未知项写入 unverified；"
            "按契约输出单个 JSON 对象。你的结论是建议，不是工程决策。",
            "",
        ]
        return "\n".join(lines)
    resolved_stage = normalize_stage(stage, mode=resolved)
    lines = [f"# Copilot 认知{resolved} 材料"
             f"（阶段：{resolved_stage}）", "", *_context_lines(state, model)]
    lines += [
        "## 本次需要判断的事项",
        "",
        str(question or "").strip()
        or "(未提供：请围绕材料给出本次最需要判断的事项)",
        "",
        "## 主代理整理的材料",
        "",
        materials.strip(),
        "",
        "## 输入要求",
        "",
        "材料应包含：任务目标、当前工程基线、已确认事实、关键问题与约束、"
        "已尝试方案及结果、必要证据摘要、与上次判断相比的新增信息。不要包含"
        "完整仓库、冗长日志、重复报告或无关历史；已确认的事实不要重复调查。",
        "",
        "## 本次重点",
        "",
        STAGE_FOCUS[resolved_stage],
        "",
        "## 输出要求",
        "",
        "一次性给出认知结果：核心判断、问题本质、推荐方向、备选方案、关键取舍、"
        "风险与优先级、关键假设、需要用户决定的事项、需要关注的验证信号与重新"
        "咨询的触发条件。未知项写入 unverified；按契约输出单个 JSON 对象。"
        "优先给结论、依据、风险与建议：不复述任务背景或材料原文，不展示内部"
        "推理过程，不重复已知事实。"
        "你的输出是认知输入，由 ZCode 主代理决定是否采纳以及如何实现。",
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


def prompt_template_sha256() -> str:
    """模式指令与阶段重点的摘要，用于让模板变更自动使旧结论失效。"""
    blob = json.dumps({"modes": MODE_INSTRUCTIONS, "stages": STAGE_FOCUS},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def review_subject_sha256(state: dict, materials: str, *, mode: str = "",
                          question: str = "", stage: str = "",
                          model: str = "") -> str:
    """单轮判定用的稳定主体摘要（同一份上下文只允许一次调用）。

    缓存键成分：提示词版本、任务身份、Provider 与模型、推理强度、目标、合同正文
    （`done_criteria_text` 与边界）、完成条件描述、**证据集合摘要**与主代理材料；
    认知调用额外绑定模式、阶段与本次问题。以下变化即失效：
    - 任务、合同/目标/条件描述或材料实质变化；
    - 证据集合变化（新增、摘要变化、归档）；
    - 提示词版本、Provider/模型或推理强度变化（换模型后旧结论不再复用）；
    - 纯空白/排版差异（归一后视为同一对象，不重复付费）。
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
        # 模板本身也进键：改了模式指令/阶段重点却忘记 bump PROMPT_VERSION 时，
        # 旧结论不能在“新提示词”下被复用。
        "prompt_template": prompt_template_sha256(),
        "task_id": str(state.get("task_id") or ""),
        "provider": PROVIDER,
        "model": resolve_model(model),
        "reasoning_effort": REASONING_EFFORT,
        "goal": str(state.get("goal") or ""),
        "contract": normalize_context_text(contract.get("done_criteria_text")),
        "boundaries": normalize_context_text(contract.get("boundaries")),
        "criteria": criteria,
        "evidence_digest": evidence_digest(state),
        # 发送的提示词用 materials.strip()，键必须与之一致；再消除纯空白差异：
        # 恢复上下文或重新排版材料不会把同一问题当成新对象而重复付费。
        "materials": normalize_context_text(materials),
    }
    if mode:
        resolved_mode = normalize_mode(mode)
        blob["mode"] = resolved_mode
        if resolved_mode in COGNITIVE_MODES:
            blob["stage"] = normalize_stage(stage, mode=resolved_mode)
    if question:
        blob["question"] = str(question)
    return hashlib.sha256(
        json.dumps(blob, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def record_mode(record: dict) -> str:
    """记录的认知模式：历史 THINK 归入 CONSULT，CONSULT / PLAN 各自独立。

    历史记录可能没有 mode 字段，按当时存放位置判定（`advisory_id` 表示辅助
    分析，否则是复核）。归一后 CONSULT 与 PLAN 的覆盖判断互不替代，但同一
    合同内不因升级而强制重新调用历史 THINK 结论。
    """
    mode = str(record.get("mode") or "").upper()
    if mode == "REVIEW":
        return "REVIEW"
    if mode == "PLAN":
        return "PLAN"
    if mode == "CONSULT" or mode in LEGACY_MODES:
        return "CONSULT"
    return "CONSULT" if record.get("advisory_id") else "REVIEW"


def completed_reviews(state: dict, records_key: str = "cognition_calls") -> list[dict]:
    """已完成记录：默认读取统一 `cognition_calls` 流（含历史只读兼容）。

    显式传入 `sol_reviews` / `sol_advisories` 时只读该历史键，供兼容调用方使用。
    """
    if records_key == "cognition_calls":
        import governance as G  # 延迟导入：状态合并逻辑只有一份
        rows = G.iter_cognition_calls(state)
    else:
        rows = [item for item in state.get(records_key) or []
                if isinstance(item, dict)]
    return [item for item in rows if item.get("status") == "completed"]


def reuse_identity_problem(item: dict, target_model: str) -> str:
    """记录是否可被 `target_model` 复用；返回空字符串表示身份可复用。

    复用只认**实际产出模型**，不能只看请求模型：
    - 请求模型与本次不一致 → 不跨模型复用（原有模型门）；
    - 缺 `actual_model`（v9.14 及更早的历史记录）→ 无法确认身份，按未命中处理，
      不猜测来源；
    - `actual_model` 与 `requested_model` 不一致（兜底接管）→ 结论属于接管模型，
      不能供下一次主模型请求复用；
    - `fallback_used` 记录只保留审计、追踪、采纳与统计价值，不进入可复用缓存，
      下一次主模型请求仍先尝试主模型。
    """
    requested = str(item.get("requested_model")
                    or item.get("model") or "").strip()
    actual = str(item.get("actual_model") or "").strip()
    if not requested or requested != target_model:
        return "requested-model-mismatch"
    if not actual:
        return "actual-model-unknown"
    if actual != requested:
        return "produced-by-fallback-model"
    if item.get("fallback_used"):
        return "fallback-result-not-reusable"
    return ""


def single_round_decision(state: dict, materials_sha256: str, *,
                          force: bool = False, reason: str = "",
                          trigger: str = "",
                          records_key: str = "cognition_calls",
                          mode: str = "CONSULT", model: str = "") -> dict:
    """单轮判定：同一认知对象直接复用；同模式再次调用必须有实质原因。

    CONSULT / PLAN / REVIEW 各自独立判定，不会互相复用；记录统一存放在
    `cognition_calls`，历史 `sol_reviews` / `sol_advisories` 仍可只读复用。
    **模型门**：复用按**请求模型与实际产出模型**双重隔离——记录里的
    `requested_model`（缺省回退到 `model`）必须与本次请求模型一致，`actual_model`
    必须存在且与之一致，且记录不是兜底产物（`fallback_used` 为假）。因此默认模型
    请求发生兜底后得到的结论（`requested_model` 记默认模型、`model`/`actual_model`
    记接管模型、`fallback_used=true`）**不会**被后续同一个默认模型请求复用：结论
    仍可审计、追踪、采纳与统计，但下一次请求仍先尝试默认模型。显式选择备用模型的
    请求不会复用默认请求的结论，缺少实际模型身份的历史记录一律视为不可复用（按
    未命中处理，不猜测来源）。
    “单轮”约束的是同一认知对象只付一次费，不是调用次数上限：只要问题、阶段
    或材料构成新的认知对象，就可以按对应 trigger 继续调用。
    """
    resolved_mode = normalize_mode(mode)
    target_model = resolve_model(model)
    records = [item for item in completed_reviews(state, records_key)
               if record_mode(item) == resolved_mode
               and not reuse_identity_problem(item, target_model)]
    same = [item for item in records
            if str(item.get("materials_sha256") or "") == materials_sha256]
    # `--force` 必须能覆盖复用命中：上下文/进度变了但主体键不变时（工作项、风险、
    # 证据绑定等不进键），复用分支不能把“明确要求重新咨询”的调用挡回去。
    if same and not force:
        return {"action": "reuse", "review": same[-1],
                "note": "相同材料已完成同模式认知调用；直接复用结论，不重复调用。"}
    if not records:
        return {"action": "call", "note": f"首次认知{resolved_mode} 调用。"}
    trigger_value = str(trigger or "").strip().lower()
    reason_value = str(reason or "").strip()
    if not force:
        return {"action": "refuse", "code": "cognition_review_requires_reason",
                "note": ("本任务已有同模式认知结论。单轮原则要求再次调用必须有明确原因；"
                         "使用 --force --trigger <原因类型> --reason <说明>。")}
    if trigger_value not in RETRY_TRIGGERS:
        return {"action": "refuse", "code": "cognition_call_trigger_invalid",
                "note": ("--trigger 只能是 "
                         + ", ".join(RETRY_TRIGGERS) + "。")}
    if not reason_value:
        return {"action": "refuse", "code": "cognition_call_reason_missing",
                "note": "再次调用必须用 --reason 说明具体原因。"}
    return {"action": "call", "trigger": trigger_value, "reason": reason_value,
            "note": f"允许再次调用：{RETRY_TRIGGERS[trigger_value]}。"}



def admission(*, risk: str, triggers: list[str] | None = None,
              reason: str = "") -> dict:
    """认知介入的准入建议（只读建议，不是门禁）。

    简单/普通任务由主代理自检闭环；多模块改造、数据链路、安全与生产高风险、
    用户明确要求时建议调用一次认知判断。认知是 advisory，不构成收口门禁。
    """
    import review_policy as RP

    risk_value = str(risk or "").strip().lower()
    if risk_value not in ("low", "medium", "normal", "high"):
        raise CognitionUsageError("cognition_risk_invalid", f"风险等级 {risk_value!r} 非法。")
    tier = "medium" if risk_value == "normal" else risk_value
    requested = []
    for item in triggers or []:
        value = str(item or "").strip().lower()
        if not value:
            continue
        if value not in RP.ADMISSION_TRIGGERS and value not in RETRY_TRIGGERS:
            raise CognitionUsageError(
                "cognition_trigger_invalid",
                "未知触发因素：" + value + "；可选："
                + ", ".join(sorted(set(RP.ADMISSION_TRIGGERS) | set(RETRY_TRIGGERS)))
                + "。")
        if value not in requested:
            requested.append(value)
    if tier == "high":
        decision, scope = "recommended", "high-risk"
        detail = ("高风险/安全/生产变更建议在形成判断后调用一次认知评估；"
                  "认知只提供判断与风险意见，不证明宿主权限，也不构成收口门禁。")
    elif requested:
        decision, scope = "recommended", "triggered"
        detail = "声明了实质风险因素，建议调用一次认知判断；主代理自行决定。"
    elif tier == "low":
        decision, scope = "not_needed", "none"
        detail = "低风险改动（文案/样式/简单修复/小范围既有模式）：主代理自检闭环，无需认知。"
    else:
        decision, scope = "optional", "normal"
        detail = "普通功能开发：按需调用一次认知；不强制、不设自动第二轮。"
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

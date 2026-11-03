#!/usr/bin/env python3
"""copilot 共享工具：脱敏、证据指纹、敏感路径判定、凭据解析。

被 bridge_evidence.py 与 call_gpt.py 共同导入。只提供纯函数，不产生副作用。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone

# 凭据解析链（按序尝试，只返回值，绝不回显、绝不进 argv）：
#   1. 环境变量（调用方传入的名字，如 CUSTOM_127_API_KEY）
#   2. Reasonix 全局 .env（迁移兼容：本机两套宿主共存，同一把 CPA 网关钥匙）
#   3. ZCode 原生 provider 配置：取指向本机 CPA（baseUrl 含 8317 或规则体含 cpa）
#      的规则的 apiKey 字段，与 designwise/scripts/zcode_image.py 同一套匹配口径。
ENV_FILE = os.path.expanduser("~/.reasonix/.env")
ZCODE_PROVIDER_FILE = os.path.expanduser("~/.zcode/v2/provider_config.json")


def read_zcode_provider_key(config_path: str | None = None) -> str | None:
    """从 ZCode provider 配置解析本机 CPA 网关的 apiKey；找不到返回 None。"""
    path = config_path or ZCODE_PROVIDER_FILE
    try:
        with open(path, "r", encoding="utf-8") as fh:
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
        if "8317" not in base_url and "cpa" not in json.dumps(rule, ensure_ascii=False).lower():
            continue
        key = str((rule_config.get("access") or {}).get("apiKey") or "").strip()
        if key:
            return key
    return None

# ── 脱敏 ────────────────────────────────────────────────────────────────────
# 明确形态的凭据串：整体遮蔽。
_TOKEN_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{8,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{12,}"),
]

# 赋值形态：键名命中敏感词时，只保留键名与值的长度，不落值。
# 分隔符限定为行内空白 + 单个 :/=，禁止跨行匹配——否则会把形如
# `_TOKEN_PATTERNS:\n        text = ...` 的普通源码误判为赋值并改写。
# 带引号的值按引号边界取（可含空格）；不带引号的值仍只取到空白前——
# 否则 `password: xxx then do something` 这类自然语言会被整行吞掉。
_ASSIGN = re.compile(
    r"(?i)(?P<q1>['\"]?)(?P<key>[A-Za-z0-9_\-]*(?:api[_-]?key|secret|token"
    r"|password|passwd|credential|private[_-]?key|access[_-]?key)[A-Za-z0-9_\-]*)"
    r"(?P=q1)(?P<sep>[ \t]*[:=][ \t]*)"
    r"(?:"
    r"(?P<q2>['\"])(?P<qval>.{1,}?)(?P=q2)"
    r"|(?P<val>[^\s'\"]{3,})"
    r")"
)

# 可能含凭据的文件名（§3：先按可能含凭据处理）。
_SENSITIVE_NAME = re.compile(
    r"(?i)(^|[._\-/])("
    r"\.env(\..*)?|\.credentials.*|.*credentials?.*|.*secret.*|.*凭据.*"
    r"|.*settings.*\.ya?ml|.*\.pem|.*\.key|.*\.keystore|.*\.jks|.*\.p12|.*\.pfx"
    r"|id_rsa|id_ed25519|\.netrc|\.npmrc"
    r")$"
)


def redact(text: str) -> str:
    """遮蔽凭据形态的串，防止意外进入请求体、证据包或日志。

    幂等：已经遮蔽出的 `<redacted:N>` 不会被二次改写（见 _EXPR_PATTERNS 判定）。
    """
    for pat in _TOKEN_PATTERNS:
        text = pat.sub(lambda m: f"<redacted:{len(m.group(0))}>", text)
    return _ASSIGN.sub(_mask_assignment, text)


# 只放过「明显是取值表达式」的值：函数调用、下标、模板片段、已遮蔽标记。
# 早先按「值里出现任一代码符号就放过」，会让含 + = \ 等合法凭据字符的字面量
# 原样出站（如 password="Abc+123456"）。凭据字符集不能当成代码判定依据。
_EXPR_PATTERNS = (
    re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*\s*\("),        # get_token( / os.environ.get(
    re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*\s*\["),         # creds["k"]
    re.compile(r"^[A-Za-z_$][\w$.]*(::[A-Za-z_][\w$]*)+$"),     # ns::name
    re.compile(r"^<redacted:\d+>$"),                             # 已遮蔽，保持幂等
    re.compile(r"^[%{]"),                                        # 模板占位 %s / {token}
    re.compile(r"^\.\.\.$|^\*\*\*$"),                            # 文档里的省略占位
)


def _is_code_expression(value: str) -> bool:
    return any(p.search(value) for p in _EXPR_PATTERNS)


def _mask_assignment(m: re.Match) -> str:
    quoted = m.group("qval")
    value = quoted if quoted is not None else m.group("val")
    # 引号内是字面量，不是取值表达式：只有未加引号的值才允许走表达式豁免。
    # 已遮蔽标记本身放行，否则 `password="<redacted:18>"` 会被二次改写、破坏幂等。
    if _is_code_expression(value) and (quoted is None or value.startswith("<redacted:")):
        return m.group(0)
    q2 = m.group("q2") or ""
    return (f"{m.group('q1')}{m.group('key')}{m.group('q1')}{m.group('sep')}"
            f"{q2}<redacted:{len(value)}>{q2}")


def is_sensitive_path(path: str) -> bool:
    """文件名命中凭据命名模式 → True（默认拒绝整读，需显式放行）。"""
    return bool(_SENSITIVE_NAME.search(os.path.basename(path.rstrip("/")) or path))


# ── 指纹与统计 ──────────────────────────────────────────────────────────────
def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ── 任务合同指纹与审计路径信任边界 ──────────────────────────────────────────
# 本地信任模型（2026-11-01，本机 P0 收尾）：
#   * 这里的 sha256 只证明「同一份目标/完成条件/边界」「同一份回复正文」，是**关联与
#     完整性**凭证，不是身份认证或防伪签名。没有密钥、没有 HMAC，能改写本机文件的
#     用户也能改写审计与状态——这仍在信任边界内，不宣称绝对防伪。
#   * 信任边界外的是「同机其他进程之外的世界」：上游模型、网络、被投递的旧回复文件。
#     本模块的摘要与任务归属用于阻止这些来源冒充本轮指导。
SKILL_HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STANDARD_AUDIT_LOG = os.path.join(SKILL_HOME, ".audit", "copilot.jsonl")
# 只给自动化测试用：允许隔离的临时审计路径进入自动协作通道。正式运行不得设置。
AUDIT_TEST_MODE_ENV = "COPILOT_AUDIT_TEST_MODE"


def normalize_block(text: str) -> str:
    """规范化文本块：统一换行、去行尾空白、折叠连续空行、去首尾空行。

    目标/完成条件/边界以文本形式进入合同指纹，必须对常见的复制粘贴差异
    （CRLF、行尾空白、首尾空行、连续空行）不敏感。**不覆盖**段间空行增删、
    行内空格或缩进变化——这些视为合同内容变化，指纹随之改变。
    """
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for line in raw.split("\n"):
        line = line.rstrip()
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def contract_sha256(goal: str, contract: str = "") -> str:
    """任务合同指纹：goal 与可选的完成条件/边界正文，规范化后取 sha256。

    goal 与 contract 分别放进 JSON 结构再哈希，避免 goal 正文里出现
    `\\n\\ncontract:\\n` 之类的分隔串时产生域碰撞（不同合同同指纹）。
    只做关联，不证明内容可信；见文件头的本地信任模型。
    """
    body = json.dumps({"goal": normalize_block(goal),
                       "contract": normalize_block(contract)},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(body)


def is_standard_audit_path(path: str) -> bool:
    """是否指向本 Skill 的标准审计日志（正式运行唯一认可的审计路径）。"""
    if not str(path or "").strip():
        return False
    return (os.path.realpath(os.path.abspath(path))
            == os.path.realpath(os.path.abspath(STANDARD_AUDIT_LOG)))


def audit_path_trusted(path: str) -> bool:
    """自动协作通道的审计路径准入：标准路径，或测试显式声明的隔离路径。"""
    return is_standard_audit_path(path) or os.environ.get(AUDIT_TEST_MODE_ENV) == "1"


# ── 图片证据：按魔数嗅探（不信任扩展名）──────────────────────────────────────
# 白名单依据：2026-10-16 对 CPA 127.0.0.1:8317 /v1/responses 的实测
# （当时请求模型为 gpt-6.1-sol-high，该 ID 已退役；当前 copilot 的 CPA 角色模型为 gpt-6-astra-high）。
# 每格式一次最小 input_image 请求；实测支持 png/jpeg/webp/gif；
# bmp 被上游拒绝：`input_image image_url unsupported image format: image/bmp`。
# 该白名单按文件头魔数判定，与模型选择无关；通道或上游变化后需重新实测格式支持。
SUPPORTED_IMAGE_MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")


def sniff_image_mime(head: bytes) -> str | None:
    """按文件头魔数判定图片类型；非图片或不受支持返回 None。扩展名不参与判定。"""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_dimensions(path: str) -> tuple[int, int, str | None]:
    """返回 (宽, 高, 错误)。PIL 不可用或解码失败时宽高为 0，并在第三项说明原因。

    `verify()` 会读遍文件、校验结构完整性，因此截断 / 损坏的图片在这里就会被挡下，
    而不是等上游解码失败。Pillow 是**图片证据的必需依赖**：没有它就无法做完整性校验，
    采集端（bridge_evidence.collect_image）会因此拒绝纳入图片，而不是放行未校验的像素。
    """
    try:
        from PIL import Image
    except ImportError:
        return 0, 0, ("缺少依赖 Pillow：无法校验图片完整性，采集端会拒绝该图片。"
                      "请先安装 Pillow（pip install Pillow）；不要以文字描述替代像素。")
    try:
        with Image.open(path) as im:
            width, height = int(im.width), int(im.height)
            im.verify()  # 校验完整文件，捕获截断 / 结构损坏
        return width, height, None
    except Exception as exc:  # noqa: BLE001 — 损坏/截断/未知格式统一按解码失败处理
        return 0, 0, f"图片解码失败（{type(exc).__name__}）：文件可能损坏或不是有效图像"


# ── 凭据解析（绝不回显、绝不进 argv）─────────────────────────────────────────
def read_env_value(path: str, key: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == key:
                    v = v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                        v = v[1:-1]
                    return v or None
    except FileNotFoundError:
        return None
    return None


def resolve_api_key(env_name: str) -> str | None:
    """环境变量 → Reasonix .env（迁移兼容）→ ZCode provider 配置。只返回值，由调用方决定如何失败。"""
    return (os.environ.get(env_name)
            or read_env_value(ENV_FILE, env_name)
            or read_zcode_provider_key())

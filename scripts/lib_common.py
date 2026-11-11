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
_SENSITIVE_KEY = (r"[A-Za-z0-9_\-]*(?:api[_-]?key|secret|token|password|passwd"
                  r"|credential|private[_-]?key|access[_-]?key)[A-Za-z0-9_\-]*")
_SENSITIVE_KEY_FULL = re.compile(r"(?i)^" + _SENSITIVE_KEY + r"$")
# 词界前置断言：没有它，`finditer` 会对超长字母数字串的**每个**位置尝试前缀扫描，
# 长串（16K 单行日志/哈希）退化成二次方，实测 40s+；有了它，词内位置立即失败，
# 只有词首会进入昂贵的前缀匹配。密钥词只能从词首开始，语义不变。
_ASSIGN = re.compile(
    r"(?i)(?<![\w\-])(?P<q1>['\"]?)(?P<key>" + _SENSITIVE_KEY + r")"
    r"(?P=q1)(?P<sep>[ \t]*[:=][ \t]*)"
    r"(?:"
    r"(?P<q2>['\"])(?P<qval>.{1,}?)(?P=q2)"
    # JSON/JSONL 里被转义过的字符串值（`"stdout": "api_key = \"…\""`）：
    # `q2` 看不到 `\"`，整个赋值会被跳过而把凭据原样带出。这里按转义引号取值，
    # 且取**本行最后一处**配对（贪婪）：值内部还可以有 `\\\"` 这样的更深一层转义，
    # 非贪婪会在第一处内部转义引号处收手，把凭据后缀留在可见部分。
    r"|(?P<eq>\\['\"])(?P<eval>[^\n]*)(?P=eq)"
    r"|(?P<lam>lambda\b)"
    # 裸值到空白前：保持旧口径（含标点的凭据 `Abc+123456` 整体遮蔽、逗号分隔的
    # 散文列表仍按空白切分）。**开括号开头不算赋值值**：`key = {...}`/`key = [...]`
    # 由容器扫描器处理，`_ASSIGN` 再去匹配 `[re.compile(r` 这种表达式前缀只会把
    # 代码改坏（且遮蔽范围没有增加）。
    r"|(?P<val>(?![\[{])[^\s'\"]{3,})"
    r")"
)

# 敏感键的 lambda 赋值：函数是表达式，不等于函数体里没有凭据。整段放行会把
# `api_key = lambda: "<真实凭据>"` 原样带出，因此只遮蔽函数体内的带引号字面量，
# 保留函数体代码（函数名、参数与调用结构仍是可复核的证据）。
# 语句边界按引号外的 `;`/换行/注释判定，并跨过括号内的换行：不能把同一行后面的
# 另一个敏感赋值当成函数体一并吞掉（`…; auth_tokens = 987654321` 仍须遮蔽）。
_LAMBDA_HEAD = re.compile(
    r"(?i)(?<![\w\-])(?P<q1>['\"]?)(?P<key>" + _SENSITIVE_KEY + r")"
    r"(?P=q1)(?P<sep>[ \t]*[:=][ \t]*)lambda\b")
# 完整标记才放行（幂等）。`\\["']` 只覆盖 JSON/JSONL 里的转义收尾引号：
# `\"<redacted:10>\"` 在字符串里被看成一个整体，标记后跟一个转义引号仍是同一标记。
_REDACTED_MARK = re.compile(r"^<redacted:\d+>(?:\\[\"'])?$")
# 裸值尾部的容器结构符号（`<redacted:1>},`、`true}}`）：判定只看核心，命中就原样
# 放行，避免把 `}`/`,` 吞进标记（括号丢失、二次脱敏形态不同）。
_VALUE_CORE = re.compile(r"^(?P<core>.*?)(?P<tail>[}\],;)]*)$", re.S)


def _is_redacted_marker(inner: str) -> bool:
    """是否已是本模块产出的完整标记（幂等判定）。

    忽略换行：三引号遮蔽会按原字面量保留换行个数，标记后因此可能跟着若干 `\\n`。
    带任何其它字符的内容一律不算标记——`"<redacted:12>还夹着凭据>"` 必须重遮。
    """
    return bool(_REDACTED_MARK.match(inner.replace("\n", "")))


def _mask_segment(inner: str) -> str:
    """遮蔽一段字面量内容，保留其中的换行个数（披露按行号给出，行数不能变）。"""
    return f"<redacted:{len(inner)}>" + "\n" * inner.count("\n")


# 注释里只遮**成对**的完整字面量：`don't` 这类撇号不能被当成字符串起点，
# 否则整条注释都会被遮掉、证据变得不可读；而注释里成对的凭据（`# password "…"`）仍要遮。
_COMMENT_LITERAL = re.compile(r"\"[^\"\n]*\"|'[^'\n]*'")


def _mask_comment(text: str) -> str:
    def repl(m: re.Match) -> str:
        body = m.group(0)
        inner = body[1:-1]
        if _is_redacted_marker(inner):
            return body
        return f"{body[0]}{_mask_segment(inner)}{body[-1]}"
    return _COMMENT_LITERAL.sub(repl, text)


# 可能含凭据的文件名（§3：先按可能含凭据处理）。
_SENSITIVE_NAME = re.compile(
    r"(?i)(^|[._\-/])("
    r"\.env(\..*)?|\.credentials.*|.*credentials?.*|.*secret.*|.*凭据.*"
    r"|.*settings.*\.ya?ml|.*\.pem|.*\.key|.*\.keystore|.*\.jks|.*\.p12|.*\.pfx"
    r"|id_rsa|id_ed25519|\.netrc|\.npmrc"
    r")$"
)


def redact(text: str, _depth: int = 0) -> str:
    """遮蔽凭据形态的串，防止意外进入请求体、证据包或日志。

    幂等：已经遮蔽出的 `<redacted:N>` 不会被二次改写（见 _EXPR_PATTERNS 判定）。
    整份文本是 JSON 时走结构化路径（见 `_redact_json_document`）：JSON 里的 `\\"` 是
    转义内容而不是字符串定界符，纯词法扫描会把结束转义引号一起吞掉，把合法 JSON
    改成不可解析文本。
    """
    if _depth < 8:
        structured = _redact_json_document(text, _depth)
        if structured is None:
            # 命令采集的条目是「$ 命令 + 输出 + 退出码」的混合正文：JSON 报告不再占据
            # 整份文本，整份判定失效，但报告本身仍要按结构脱敏。
            structured = _redact_embedded_json(text, _depth)
        if structured is not None:
            return structured
    return _redact_lexical(text, _depth)


def _redact_lexical(text: str, depth: int = 0) -> str:
    """词法路径：不解析结构，按记号与赋值形态遮蔽。"""
    # 记号替换必须保留换行个数：`Bearer\\s+` 可以跨行匹配折叠的 HTTP 头，
    # 直接换成单行标记会让披露行号与磁盘错位（见 `_mask_segment`）。
    for pat in _TOKEN_PATTERNS:
        text = pat.sub(lambda m: _mask_segment(m.group(0)), text)
    text = _mask_lambda_literals(text)
    text = _mask_sensitive_containers(text, depth)
    return _ASSIGN.sub(_mask_assignment, text)


_TRIPLE_QUOTES = ('"""', "'''")


def _statement_end(text: str, start: int) -> int:
    """从 start 起找 lambda 函数体的结束位置。

    边界：引号外的 `;`；括号外的换行；行注释（在括号内同样识别）。
    - 注释必须识别：括号内 `# don't …` 的撇号不能被当成字符串起点，否则扫描在这里
      收手，后面真正的字面量就漏遮了。
    - 三引号可跨行；普通字符串不能跨行，行尾未闭合即**在本行结束语句**：越过换行继续
      扫描会把下一行的 JSON 键、后续语句当成函数体一起遮蔽（`\"` 在 JSON 文本里不是
      定界符，一旦错判，状态要到下一处裸引号才对上）。
    - `\\` + 换行是合法显式续行（`api_key = lambda: \\` 折行写字面量），必须跨过去继续，
      否则函数体在续行处被截断，下一行的字面量漏遮。
    **遮蔽范围宁可偏大**：这里只决定「哪些字面量要替换」，多遮一角是可见的披露，
    漏遮才是凭据外泄。
    """
    quote = ""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if quote in _TRIPLE_QUOTES:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if text.startswith(quote, i):
                quote = ""
                i += 3
                continue
            i += 1
            continue
        if quote:
            if ch == "\\" and i + 1 < n:
                i += 2  # 转义字符；`\` + 换行是续行，字符串仍然延续
                continue
            if ch == quote:
                quote = ""
            elif ch == "\n":
                return i  # 普通字符串不跨行：未闭合即本行结束（畸形文本兜底）
            i += 1
            continue
        if text.startswith('"""', i) or text.startswith("'''", i):
            quote = text[i:i + 3]
            i += 3
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "\\" and i + 1 < n:
            i += 2  # 转义/续行本身不是结构符号；`\` + 换行须跨过去继续扫描
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0 and ch in ("\n", ";"):
            return i
        i += 1
    return n


def _mask_literals_in(text: str) -> str:
    """遮蔽函数体里的字符串字面量：三引号、普通引号、行尾未闭合的引号段。

    单条正则做不到三件事：三引号跨行、注释里的撇号不算字符串起点、行尾未闭合的
    引号段也必须遮到行尾（否则凭据就留在可见部分）。换行个数保持不变——披露按行号给出。
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "#":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(_mask_comment(text[i:j]))
            i = j
            continue
        triple = text[i:i + 3]
        if triple in _TRIPLE_QUOTES:
            j = i + 3
            closed = False
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text.startswith(triple, j):
                    closed = True
                    break
                j += 1
            inner = text[i + 3:j]
            if not inner or _is_redacted_marker(inner):
                out.append(text[i:j + 3 if closed else j])
            else:
                body = _mask_segment(inner)
                out.append(f"{triple}{body}{triple if closed else ''}")
            i = j + 3 if closed else n
            continue
        if ch in ("'", '"'):
            j = i + 1
            while j < n and text[j] != ch and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            closed = j < n and text[j] == ch
            inner = text[i + 1:j]
            if not inner or _is_redacted_marker(inner):
                out.append(text[i:j + 1 if closed else j])
            else:
                body = _mask_segment(inner)
                out.append(f"{ch}{body}{ch if closed else ''}")
            i = j + 1 if closed else j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _mask_lambda_literals(text: str) -> str:
    """只遮蔽敏感键 lambda 函数体内的字面量，保留函数体代码。"""
    out: list[str] = []
    pos = 0
    for m in _LAMBDA_HEAD.finditer(text):
        if m.start() < pos:
            continue
        end = _statement_end(text, m.end())
        out.append(text[pos:m.end()])
        out.append(_mask_literals_in(text[m.end():end]))
        pos = end
    out.append(text[pos:])
    return "".join(out)


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

# 明确是计数量的键：本项目审计与证据里真实出现的用量字段。任意 `*_tokens` 后缀
# 不能证明业务语义是统计量（`access_tokens = 12345678901234567890` 可能是数字形式
# 的认证材料），因此不做后缀通配放行。遮蔽计数既不防泄露，也让证据里的 token
# 用量不可读（`max_output_tokens = None` 曾被遮成 `<redacted:4>`，复核无法判断
# 升档逻辑），这里按白名单放行数值与布尔/空值；带引号的字面量仍然一律遮蔽。
_STAT_KEYS = frozenset({
    "input_tokens", "output_tokens", "total_tokens", "cached_tokens",
    "cache_write_tokens", "reasoning_tokens", "prompt_tokens",
    "completion_tokens", "max_output_tokens", "max_output_tokens_sent",
    "max_total_tokens", "observed_output_tokens", "truncated_output_tokens",
    "max_truncated_output_tokens", "best_truncated_output_tokens",
    "previous_truncated_output_tokens", "last_truncated_output_tokens",
})
_STAT_VALUE = re.compile(
    r"(?i)^[+-]?\d[\d_]*(?:\.\d+)?[)\],}]*$|^(?:none|true|false|null)[)\],}]*$")
# 白名单统计键的取值也可能是**代码引用**（`max_output_tokens_sent: output_budget`）。
# 这类值只是一个变量名，遮蔽它既不防泄露，又让证据里的预算链路变成不可读的
# `<redacted:N>`，复核对升档/截断逻辑的核查因此失去依据。只对白名单键放行裸标识符。
_STAT_REF = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*[)\],}]*$")


def _is_code_expression(value: str) -> bool:
    return any(p.search(value) for p in _EXPR_PATTERNS)


def _mask_assignment(m: re.Match) -> str:
    if m.group("lam") is not None:
        # lambda 关键字本身不是凭据：函数体内的字面量已由 _mask_lambda_literals
        # 遮蔽，这里只放行关键字，避免把整行剩余部分当成函数体吞掉。
        return m.group(0)
    quoted = m.group("qval")
    escaped = m.group("eval")
    value = quoted if quoted is not None else (
        escaped if escaped is not None else m.group("val"))
    # 裸值可能连着一串容器结构符号（`true}}`、`<redacted:1>},`）。判定「是不是
    # 已遮蔽标记 / 布尔空值 / 统计量 / 代码表达式」只看核心；尾部结构符号单独保存，
    # 遮蔽后原样追加。旧口径把尾部一起算进标记长度并丢弃，`{password: hunter2,
    # token: xyz}` 会丢掉逗号与两个闭括号——无泄漏，但证据结构被改写。
    core = value
    tail = ""
    if quoted is None and escaped is None:
        parts = _VALUE_CORE.match(value)
        core, tail = parts.group("core"), parts.group("tail")
    # 引号内是字面量，不是取值表达式：只有未加引号的值才允许走表达式豁免。
    # 已遮蔽标记本身放行，否则 `password="<redacted:18>"` 会被二次改写、破坏幂等。
    if _is_code_expression(core) and (quoted is None and escaped is None
                                      or _is_redacted_marker(core)):
        return m.group(0)
    # 布尔与空值不是凭据：`secret_free: true` 这类**检测结果字段**被遮成
    # `<redacted:4>` 后，复核读不出判定，证据反而失去可核性。
    if (quoted is None and escaped is None
            and core.lower() in ("true", "false", "none", "null")):
        return m.group(0)
    if (quoted is None and escaped is None and m.group("key").lower() in _STAT_KEYS
            and (_STAT_VALUE.match(core) or _STAT_REF.match(core))):
        return m.group(0)
    # 引号形态原样保留：带引号的值只在引号内替换；转义引号（JSON 里的 `\"`）同理，
    # 否则脱敏会改变文本结构，让证据里的 JSON 不再是合法 JSON。
    q2 = m.group("q2") or m.group("eq") or ""
    return (f"{m.group('q1')}{m.group('key')}{m.group('q1')}{m.group('sep')}"
            f"{q2}<redacted:{len(core)}>{q2}{tail}")


# JSON 文本里 `\"` 是**转义内容**，不是字符串定界符。纯词法扫描区分不了
# `{"shape": "api_key = lambda: \"SECRET\""}` 里的里外两层引号，会把结束的 `\"`
# 连同值一起遮掉，留下 `\"<redacted:N>` 这种残缺 JSON——复核拿到的是不可解析的
# 证据包，关键字段读不出来。整份文本能由同一套序列化参数逐字节还原时，就按
# 结构处理：值（含凭据键下的标量）逐项脱敏，键与结构原样保留。
_JSON_STYLES = (
    {"ensure_ascii": False, "indent": 2},
    {"ensure_ascii": False, "indent": 4},
    {"ensure_ascii": True, "indent": 2},
    {"ensure_ascii": True, "indent": 4},
    {"ensure_ascii": False, "separators": (",", ":")},
    {"ensure_ascii": True, "separators": (",", ":")},
    # `json.dumps(x)` 的默认形态（无缩进、`, ` / `: `）。审计 JSONL 与临时脚本里的
    # 直接打印都是这个风格；漏掉它会让这些行退到词法路径，检测键再被改坏。
    {"ensure_ascii": False},
    {"ensure_ascii": True},
)


def _redact_json_scalar(value) -> str:
    length = len(value) if isinstance(value, str) else len(json.dumps(value))
    return f"<redacted:{length}>"


def _mask_json_key(key: str) -> str:
    """键名本身是凭据形态时遮蔽键名，与词法路径同口径。

    词法路径的 `_TOKEN_PATTERNS` 对文本任意位置生效，凭据作为**键**也会被遮；结构
    路径只处理值，若不单独处理，`{"sk-…": "v"}` 会原样出站——相对基线是新泄漏。
    """
    for pat in _TOKEN_PATTERNS:
        if pat.search(key):
            return f"<redacted:{len(key)}>"
    return key


# 词法路径的容器缺口：`{"api_key": {"v": "真值"}}` 在**非规范排版**时不能逐字节
# 还原（`_JSON_STYLES` 覆盖不到任意空格），整块退回词法路径；`_ASSIGN` 的取值分支
# 只接受引号字面量、转义引号、lambda 或裸词，开括号前面的 `{`/`[` 一个都不匹配，
# 凭据键下的整棵容器因此原样出站。这里补一个原地扫描器：只替换容器内部的值记号，
# 括号、键名、布尔/空值、换行和缩进全部保持原样（披露按行号给出，不能重排）。
_CONTAINER_HEAD = re.compile(
    r"(?i)(?<![\w\-])(?P<q1>['\"]?)(?P<key>" + _SENSITIVE_KEY + r")(?P=q1)"
    # 分隔符允许键与冒号之间换行：JSON 里空白不限行，`"api_key"\n  :\n  {"v": …}`
    # 只允许行内空白会整块退到旧路径而漏遮。开括号单独定位（见 `_container_gap_end`），
    # 这样键与容器之间夹注释（`api_key:  # 说明\n{…}`）也不会漏。
    r"(?P<sep>[ \t\r\n]*[:=])"
)
_REDACTED_PREFIX = "<redacted:"
# 键与容器之间最多跨过的间隔（空白与注释）：防止把几百 KB 以外的块并进同一个容器。
# 上限只挡病态输入；正常注释（哪怕几万字符）都在限内，超限的畸形间隔见 `_mask_container_at`
# 的兜底口径（宁可多遮）。
_CONTAINER_GAP_LIMIT = 65536


def _container_gap_end(text: str, pos: int) -> int:
    """从分隔符之后跳过空白与注释，返回容器开括号位置；不是容器则 -1。

    注释形态与 `_mask_container_at` 同口径：`#`/`//` 到行尾、`/* */` 成对。未闭合的
    `/*` 按「行注释」处理到行尾——否则键与容器两条路径会同时放弃，块被当独立 JSON
    文档而漏遮（`api_key: /* c\\n{"v": …}`）。
    """
    n = len(text)
    i = pos
    while i < n and i - pos <= _CONTAINER_GAP_LIMIT:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "#" or (text.startswith("//", i) and _is_line_comment_start(text, i)):
            j = text.find("\n", i)
            if j < 0:
                return -1
            i = j + 1
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j >= 0:
                i = j + 2
                continue
            # 未闭合：按行注释处理到行尾（`j` 是换行符位置，跳过它本身）。
            j = text.find("\n", i + 2)
            if j < 0:
                return -1
            i = j + 1
            continue
        break
    if i < n and i - pos <= _CONTAINER_GAP_LIMIT and text[i] in "{[":
        return i
    return -1


def _is_line_comment_start(text: str, i: int) -> bool:
    """`//` 是行注释还是除号：`1024//2`、`a//b` 不是注释。

    除操作数（字母数字、`)`、`]`、`>`、引号、`.`）之外都按注释处理；`https://` 被前一个
    `:` 排除。`>` 覆盖已遮蔽标记（`<redacted:4>//2` 是二次脱敏时的同一种除法）。
    """
    if i == 0:
        return True
    prev = text[i - 1]
    if prev == ":":  # `https://`、`file://`
        return False
    return not (prev.isalnum() or prev in ")]>\"'._")


_CODE_WORDS = frozenset((
    "for", "if", "elif", "else", "in", "lambda", "and", "or", "not",
    "return", "yield", "await", "async", "import", "from", "with",
))


def _is_hash_comment_start(text: str, i: int) -> bool:
    """`#` 是行注释还是裸标量的一部分：`abc#def`、`a#1` 是一个值。

    YAML/JSON5 的 `#` 只有出现在行首或空白/分隔符之后才是注释；把 `abc#def` 当注释
    会吞掉同一行后面的键值对（`{v: abc#def, w: S3CRET}` 的 `w` 整段留在可见区）。
    """
    if i == 0:
        return True
    return text[i - 1] in " \t\n,{["


def _finalize_container(out: list[str], bare_values: list[tuple[int, int]],
                        code_like: bool) -> str:
    """容器含代码关键字时保留裸标识符，否则把数据形态的裸标量换成 `<redacted:原值长度>` 标记。"""
    if not code_like:
        for slot, length in bare_values:
            out[slot] = f"{_REDACTED_PREFIX}{length}>"
    return "".join(out)


def _mask_container_at(text: str, start: int) -> tuple[str, int]:
    """从 `start` 处的开括号起原地遮蔽；返回 (遮蔽文本, 容器结束位置)。

    与结构化路径的 `secret=True` 分支同口径：字符串与数字（白名单统计键的数值与
    `_STAT_KEYS` 一致放行）替换为 `<redacted:原值长度>` 标记；括号、键名、布尔/空值、注释与换行原样
    保留。**裸标识符**分两类：容器里有代码关键字（`for`/`if`/`in`/`lambda`…）时整体
    按代码处理、不遮（`[t for t in porcelain.split("\\0") if t]`）；否则按数据形态的
    裸标量遮蔽（YAML flow `{v: hunter2secret}` 的 `hunter2secret` 是字面量，不是
    变量名）。调用目标与括号内的实参仍按代码处理（`re.compile(r"…")`、`os.environ.get`）。
    找不到配对闭括号（截断/畸形输入）时一直遮到文本末尾——宁可多遮，也不把凭据留在
    可见部分。已是完整 `<redacted:数字>` 标记的片段幂等放行；只带前缀的伪标记与其它
    数据标量同样遮蔽。
    """
    opener = text[start]
    out = [opener]
    i, n, depth = start + 1, len(text), 1
    last_key = ""
    paren = 0
    code_like = False
    # 键位：只有容器开括号或逗号之后才可能是键。`{v: hunter2:x}` 的 `hunter2` 在值位上，
    # 后面那个冒号属于值的一部分，不能据此把它当键名放行（否则值的前缀整段可见）。
    expect_key = True
    bare_values: list[tuple[int, int]] = []
    while i < n:
        ch = text[i]
        if text.startswith(_REDACTED_PREFIX, i):
            j = text.find(">", i)
            if j >= 0:
                token = text[i:j + 1]
                if _is_redacted_marker(token):
                    out.append(token)
                else:
                    # 只有本模块产出的完整标记（`<redacted:数字>`）才幂等放行。伪标记
                    # （`<redacted:SYN-MARK-13>`、带额外载荷）与裸数据标量同形态，必须
                    # 遮蔽：否则容器里的秘密只要包一层 `<redacted:` 就能原样出去。
                    out.append(_mask_segment(token))
                i = j + 1
                expect_key = False
                continue
        if ch in " \t\r\n":
            out.append(ch)
            i += 1
            continue
        if ch == "#" and _is_hash_comment_start(text, i):  # 行注释：括号不是结构符号
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(_mask_comment(text[i:j]))
            i = j
            continue
        # `//`、`/* */` 是 JSON5/JS/YAML 注释：注释里的 `}` 若被当成闭括号，
        # 扫描会提前收手，后面的真实内容整段留在可见区（`{ // }\n "v": "真值" }`）。
        # `https://` 这类协议前缀、`1024//2` 这类整除不按注释处理。
        if ch == "/" and text.startswith("//", i) and _is_line_comment_start(text, i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(_mask_comment(text[i:j]))
            i = j
            continue
        if ch == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j >= 0:
                out.append(_mask_comment(text[i:j + 2]))
                i = j + 2
                continue
            # 未闭合的 `/*` 不是注释段：吞到末尾会把后面的字面量整段留在可见区。
            # 按普通字符继续，字符串与数字仍照常遮蔽。
        if ch in ("'", '"'):
            j = i + 1
            while j < n and text[j] != ch and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            closed = j < n and text[j] == ch
            inner = text[i + 1:j]
            k = j + 1 if closed else j
            while k < n and text[k] in " \t":
                k += 1
            is_key = expect_key and k < n and text[k] == ":"
            if not inner or _is_redacted_marker(inner):
                out.append(text[i:j + 1 if closed else j])
            elif is_key:
                last_key = inner
                out.append(f"{ch}{_mask_json_key(inner)}{ch if closed else ''}")
            elif paren:
                # 调用实参是代码，不是凭据内容（`split("\\0")`、`re.compile(r"…")`）。
                out.append(text[i:j + 1 if closed else j])
            else:
                out.append(f"{ch}{_mask_segment(inner)}{ch if closed else ''}")
            i = j + 1 if closed else j
            expect_key = False
            continue
        if ch == "(":
            paren += 1
            out.append(ch)
            i += 1
            continue
        if ch == ")":
            paren = max(0, paren - 1)
            out.append(ch)
            i += 1
            continue
        if ch in "{[":  # 嵌套容器
            depth += 1
            out.append(ch)
            i += 1
            expect_key = True
            continue
        if ch in "}]":
            depth -= 1
            out.append(ch)
            i += 1
            expect_key = False
            if depth == 0:
                return _finalize_container(out, bare_values, code_like), i
            continue
        if ch == ",":
            out.append(ch)
            i += 1
            expect_key = True
            continue
        if ch.isdigit() or (ch in "+-" and i + 1 < n and text[i + 1].isdigit()):
            m = re.match(r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", text[i:])
            if m:
                if paren or last_key.lower() in _STAT_KEYS:
                    out.append(m.group(0))
                else:
                    out.append(f"{_REDACTED_PREFIX}{len(m.group(0))}>")
                i += len(m.group(0))
                expect_key = False
                continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_."):
                j += 1
            word = text[i:j]
            k = j
            while k < n and text[k] in " \t":
                k += 1
            if expect_key and k < n and text[k] == ":":  # 裸键（JSON5/YAML/源码）
                last_key = word
                out.append(word)
            elif word.lower() in _CODE_WORDS:
                code_like = True
                out.append(word)
            elif word.lower() in ("true", "false", "none", "null"):
                out.append(word)
            elif paren or (k < n and text[k] == "("):
                # 括号内的实参与调用目标是代码，不是字面量内容。
                out.append(word)
            elif last_key.lower() in _STAT_KEYS:
                out.append(word)
            else:
                # 数据形态的裸标量（YAML flow / JSON5 的未加引号值）按内容遮蔽。
                # 先记下位置：容器里一旦出现代码关键字，整段按代码处理（见函数末尾）。
                bare_values.append((len(out), len(word)))
                out.append(word)
            i = j
            expect_key = False
            continue
        out.append(ch)
        i += 1
        # 普通标点（`:`、`/`、`*`、未闭合注释的残片）不占键位：`{/* "v": "…"}`
        # 里 `"v"` 仍是键名，遮成值会丢掉字段名，复核读不出遮的是哪个字段。
    return _finalize_container(out, bare_values, code_like), n


def _enclosing_string(text: str, pos: int) -> tuple[int, int] | None:
    """`pos` 落在同一行的字符串字面量内时，返回该字面量的 (开引号, 闭引号后) 位置。

    证据包里的测试源码片段（`('{"api_key": { // }\\n  "v": …', …`）会把键头写在字面量
    内部。容器扫描不认字面量边界，`//` 分支又只认真实换行（样例里是转义的两个字符），
    于是扫描越过字面量一直吃到远处真正的闭括号，把后续条目整段吞进遮蔽区间（元数据被
    连同改写）。这种情况只在字面量内容里递归脱敏，作用范围不越出字面量。

    只处理同一行闭合的普通引号；三引号与跨行字面量返回 None，由外层逐字符逻辑处理。
    """
    line_start = text.rfind("\n", 0, pos) + 1
    quote = ""
    lit_start = -1
    i = line_start
    while i < pos:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            lit_start = i
        i += 1
    if not quote or lit_start < 0:
        return None
    n = len(text)
    j = i
    while j < n:
        ch = text[j]
        if ch == "\n":
            return None
        if ch == "\\":
            j += 2
            continue
        if ch == quote:
            return lit_start, j + 1
        j += 1
    return None


def _mask_sensitive_containers(text: str, depth: int = 0) -> str:
    """遮蔽凭据键下的容器值；已进入遮蔽区间的嵌套键头不重复处理。"""
    out: list[str] = []
    pos = 0
    for m in _CONTAINER_HEAD.finditer(text):
        if m.start() < pos:
            continue
        literal = _enclosing_string(text, m.start())
        if literal is not None:
            open_at, close_at = literal
            if open_at < pos:
                continue
            inner = text[open_at + 1:close_at - 1]
            out.append(text[pos:open_at + 1])
            # 字面量内容里递归脱敏：作用范围限于这个字面量，不会吞掉后面的条目。
            out.append(_mask_segment(inner) if depth >= 8 else redact(inner, depth + 1))
            out.append(text[close_at - 1])
            pos = close_at
            continue
        br = _container_gap_end(text, m.end())
        if br < 0:
            continue
        out.append(text[pos:br])
        masked, pos = _mask_container_at(text, br)
        out.append(masked)
    out.append(text[pos:])
    return "".join(out)


def _redact_json_string(value: str, depth: int) -> str:
    """脱敏字符串值；引号不配平时按首尾引号整体遮蔽。

    解析把 `\\"` 还原成真引号，字面量边界信息随之丢失：`api_key = "AAA"SECRET"`
    这种值在文本路径上靠「取本行最后一处转义引号」遮对，解析后再逐段遮蔽会在第一个
    内部引号处收手，后缀原样留下。引号个数为奇数说明有字面量不配平，此时按首个到
    最后一个引号整体遮蔽，宁可多遮也不漏。
    """
    if _is_redacted_marker(value):
        return value
    if value.count('"') % 2:
        first, last = value.find('"'), value.rfind('"')
        if 0 <= first < last:
            return (value[:first + 1] + _mask_segment(value[first + 1:last])
                    + value[last:])
    return redact(value, depth + 1)


def _redact_json_node(node, key: str | None, depth: int, secret: bool = False):
    """`secret` 表示当前节点在凭据键之下：叶子标量整体遮蔽，容器结构保留。

    只遮标量而不是把整棵子树换成一个标记：遮成标记会改变物理行数，而披露是按行号
    给出的；容器里的每个叶子都可能带凭据（`{"api_key": {"v": "真值"}}`）。
    """
    if isinstance(node, dict):
        return {_mask_json_key(k): _redact_json_node(
                    v, k, depth, secret or bool(_SENSITIVE_KEY_FULL.match(k)))
                for k, v in node.items()}
    if isinstance(node, list):
        return [_redact_json_node(v, key, depth, secret) for v in node]
    if isinstance(node, str) and _is_redacted_marker(node):
        return node
    if secret:
        # 布尔与空值不是凭据；遮掉会让 `secret_free: true` 这类判定字段不可读。
        if node is None or isinstance(node, bool):
            return node
        # 白名单统计键的数值与文本路径同口径放行（`max_output_tokens` 这类键名本身
        # 就含 `token`，不能一律当凭据）。
        if key and key.lower() in _STAT_KEYS and isinstance(node, (int, float)):
            return node
        return _redact_json_scalar(node)
    if isinstance(node, str):
        return _redact_json_string(node, depth)
    return node


def _redact_json_document(text: str, depth: int = 0,
                          secret: bool = False) -> str | None:
    """整份文本是 JSON 文档时按结构脱敏；不满足前提返回 None（回退文本路径）。

    `secret` 表示这份文档整体位于凭据键之下（行首块紧跟 `api_key:`），所有叶子标量
    按凭据遮蔽、容器结构保留。

    前提：去掉尾部空白后能由某套标准序列化参数**逐字节还原**。否则缩进/转义口径
    未知，重排会改变物理行数，而披露是按行号给出的（`- 脱敏改写：第 N 行`）。
    结果必须仍是合法 JSON 且行数不变，否则同样回退。
    """
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        return None
    tail = text[len(text.rstrip()):]
    body = text[:len(text) - len(tail)] if tail else text
    if body != body.lstrip():
        return None
    style = None
    for cand in _JSON_STYLES:
        try:
            if json.dumps(doc, **cand) == body:
                style = cand
                break
        except (TypeError, ValueError, RecursionError):
            return None
    if style is None:
        return None
    try:
        out = json.dumps(_redact_json_node(doc, None, depth, secret=secret), **style) + tail
        json.loads(out)
    except (TypeError, ValueError, RecursionError):
        return None
    if out.count("\n") != text.count("\n"):
        return None
    return out


# 正文里可能夹着 JSON 文档：`--cmd` 条目是「$ 命令 + 输出 + 退出码」，命令行本身
# 就带 `$`、`;`、重定向。整份判定因此不成立，但**报告部分**仍要按结构脱敏，
# 否则二次脱敏会把里面的报告改坏，复核无法从包里读出判定字段。
_JSON_BLOCK_START = re.compile(r"(?m)^[ \t]*([{\[])")
# 块的行首 `{` 若属于**上一行**的凭据键（`api_key:` 换行后紧跟容器），它就不是
# 独立 JSON 文档：按块结构处理会看不到外层键，把凭据原样带出。这类块不按独立块
# 解析，留给词法容器扫描器整段处理（宁可多遮）。键与块之间可以夹注释与空行
# （`api_key:  # 说明\n{…}`、`api_key: /* c */\n{…}`），因此回看按行扫描而不是
# 单条正则。
_SECRET_HEAD_END = re.compile(
    r"(?i)(?<![\w\-])(?P<q1>['\"]?)(?P<key>" + _SENSITIVE_KEY + r")(?P=q1)"
    r"[ \t]*[:=][ \t]*$")


def _has_secret_key_before(text: str, start: int, max_lines: int = 4096) -> bool:
    """行首块之前（可隔注释与空行）是否是凭据键的赋值头。

    从块起点向前逐段剥掉尾部空白、行注释与块注释，再看剩下的最后一行是不是
    `key:` / `key=` 的**纯**赋值头（`_SECRET_HEAD_END` 要求行尾就是分隔符）。
    多行块注释必须整体剥掉：`api_key:` / `/*` / `说明` / `*/` 里中间行既不是空行
    也不是注释起始，逐行判断会把这一块误判成无关文档而漏遮。

    **宁可多遮**：窗口内全是空行/注释（键头可能在更上面或注释畸形）时按凭据键处理，
    让块退回词法容器扫描；只有明确读到一行既非注释也非键头才判定无关。
    """
    end = start
    while end > 0 and text[end - 1] in " \t\r\n":
        end -= 1
    for _ in range(max_lines):
        if end == 0:
            return False
        if text.endswith("*/", 0, end):  # 块注释（可能跨行）：先整体剥掉再看
            k = text.rfind("/*", 0, end)
            if k < 0:
                return False
            end = k
            while end > 0 and text[end - 1] in " \t\r\n":
                end -= 1
            continue
        line_start = text.rfind("\n", 0, end) + 1
        line = text[line_start:end]
        # 行尾注释：`api_key:  # 说明`、`api_key: // 说明` 之后才是容器。取最早的注释
        # 起点，否则 `// note # 3` 会先被 `#` 截断而读不到键头。
        cuts = [k - line_start for k in (
            text.find("#", line_start, end), text.find("//", line_start, end)) if k >= 0]
        if cuts:
            end = line_start + min(cuts)
            while end > 0 and text[end - 1] in " \t\r\n":
                end -= 1
            continue
        if not line.strip():  # 空行：键头可能在更上面，继续回看
            end = line_start
            while end > 0 and text[end - 1] in " \t\r\n":
                end -= 1
            continue
        if _SECRET_HEAD_END.search(line):
            return True
        # 行尾未闭合的块注释（`api_key: /* 说明`）：键头之后只有注释文本，
        # 容器在下一行。这里不认，块会被当独立 JSON 文档而漏遮。
        k = line.find("/*")
        if k >= 0 and _SECRET_HEAD_END.search(line[:k].rstrip()):
            return True
        return False
    # 窗口内全是空行/注释：键头可能在更上面，按凭据键处理（fail-closed）。
    return True
# 单个正文里最多按结构处理的 JSON 文档数。合并证据包动辄上百条，每条都可能带报告；
# 上限只防病态输入，不按「第几条之后改走词法」设阈（那会让靠后的条目被改坏）。
_JSON_MAX_BLOCKS = 512


def _redact_embedded_json(text: str, depth: int = 0) -> str | None:
    """正文里嵌着 JSON 文档时只对这些文档做结构化脱敏；没有可解析块则返回 None。

    逐块扫描而不是「找最长块再对左右递归」：后者每下沉一块就 `depth + 1`，同一份
    文本里第一百个报告会撞上 `_depth < 8` 退到词法路径，字符串边界和键名被改坏
    （正好落在合并包靠后的条目上）。这里的块互为**兄弟**，间隙各自从同一层脱敏。
    """
    if len(text) < 16:
        return None
    decoder = json.JSONDecoder()
    blocks: list[tuple[int, int, bool]] = []
    cursor = 0
    attempts = 0
    for m in _JSON_BLOCK_START.finditer(text):
        if len(blocks) >= _JSON_MAX_BLOCKS:
            break
        start = m.start(1)
        if start < cursor:  # 落在已识别块内部的行首 `{` 不再单独处理
            continue
        # 属于上一行凭据键的容器：仍按结构遮蔽，但整棵按「凭据键之下」处理。交给词法
        # 容器扫描也可以，可它要跨过键与块之间的间隔（上限 `_CONTAINER_GAP_LIMIT`），
        # 间隔更长时两条路径都会放弃而漏遮；这里按块结构处理不受该上限约束。
        secret = _has_secret_key_before(text, start)
        attempts += 1
        if attempts > 4096:  # 病态输入（海量伪块）下不把解析器拖成平方级
            break
        try:
            _, end = decoder.raw_decode(text, start)
        except (ValueError, RecursionError):
            continue
        blocks.append((start, end, secret))
        cursor = end
    if not blocks:
        return None
    parts: list[str] = []
    last = 0
    for start, end, secret in blocks:
        if start > last:
            parts.append(redact(text[last:start], depth + 1))
        cleaned = _redact_json_document(text[start:end], depth, secret=secret)
        if cleaned is None and secret:
            masked, stop = _mask_container_at(text, start)
            cleaned = masked if stop == end else None
        if cleaned is None:
            # 单个块不可逐字节还原（非规范序列化）时只让这一块退词法路径：
            # 合并包里有几十个块，不能因为其中一个块让整份文本失去结构保护。
            cleaned = _redact_lexical(text[start:end], depth)
        parts.append(cleaned)
        last = end
    if last < len(text):
        parts.append(redact(text[last:], depth + 1))
    out = "".join(parts)
    if out.count("\n") != text.count("\n"):
        return None
    return out


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

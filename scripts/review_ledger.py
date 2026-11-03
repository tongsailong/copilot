#!/usr/bin/env python3
"""copilot — REVIEW 复核台账：跨轮状态、证据合并与轮次上限。

为什么需要这个模块（2026-10-17 Reasonix 2.43.0 升级复核复盘）：

* CPA 的每次调用都是**无状态会话**：GPT 看不到上一轮提交的证据与自己的裁决。
  如果每轮只提交增量证据，GPT 会重新打开已关闭项、再次索要已经提交过的条目，
  形成“补证 → 复核 → 再补证”的循环。
* 证据包编号每包都从 E1 开始。多轮证据直接拼在一起会出现 E1 冲突，
  导致 GPT 与执行方对同一个编号指向不同材料。
* 没有轮次上限时，`PARTIAL（缺证）` 会被误当成“必须继续复核”的信号。

本模块做三件事：

1. **合并**：把此前各轮证据包与本轮证据按内容去重、统一重编号，形成本轮唯一编号空间；
2. **携带**：把此前裁决、回复原文、已提交证据指纹注入本轮 prompt，让 GPT 只重判变化项；
3. **限轮**：普通任务最多 2 次 REVIEW，高风险最多 3 次；达到上限即停止自动复核，
   按 PARTIAL/BLOCKED 汇报。两个例外：新发现的重大安全问题（security，不设次数），
   以及**执行方已按上一轮裁决完成整改后的定向复核（remediation，每个台账只有一次）**——
   后者必须携带新增证据，避免「整改完却没有复核出口」迫使执行方新建台账绕过上限
   （2026-10-19 pedometer 长任务复盘的实际问题）。

台账是本地 JSON，只存证据包路径、指纹、裁决与回复文本；证据正文仍来自原始证据包文件。
历史证据包必须保留到复核结束——删除或改写会被明确拒绝，而不是静默丢失证据。

轮次不可重置：新建台账必须声明交付物标识（`subject`），并与审计日志核对；
同一交付物换台账路径、删台账重建、删证据包都不会把轮次归零。
审计记录同时是「GPT 已返回但台账未入账」的持久账：这类调用仍占用轮次，
不会被后续重试免费抵掉。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile

try:
    import fcntl
except ImportError:  # 非 POSIX 平台：退化为不加锁，其余写入路径不变
    fcntl = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_common import now_iso, sha256_file, sha256_text  # noqa: E402

LEDGER_VERSION = 1
# 证据包格式版本（与 bridge_evidence.PACK_FORMAT_VERSION 对应）。
# 正式复核只接受当前版本：旧采集器固定三反引号包裹，正文里的 `### E<n> —`
# 可以泄漏到围栏外并伪造出「合法的第二个条目」，item 级结构推断无法可靠区分，
# 只能在格式层面显式拒绝。
PACK_FORMAT_FIELD = "证据包格式"
PACK_FORMAT_VERSION = 2
PACK_FORMAT_RE = re.compile(r"^\s*[-*]\s*" + PACK_FORMAT_FIELD + r"\s*[：:]\s*(\d+)\s*$", re.M)
RISK_MAX_ROUNDS = {"normal": 2, "high": 3}
# remediation：执行方已按上一轮裁决完成整改后的**一次性**定向复核，可越过轮次上限一次。
# 它不是「再跑一遍完整复核」：必须携带新增证据，且整份台账只允许出现一次。
EXTRA_BASES = ("new-evidence", "security", "remediation")
REMEDIATION_BASIS = "remediation"

# 正式 REVIEW 的裁决行。`裁决：` 是默认写法；`最终裁决：` 与 `整体结论：`
# 是明确且唯一的兼容写法。裸 `结论：` 不认（太容易是正文或转述），
# `copilot-plan` 的 decision 也不是三态裁决。
# 只在行首识别；引用块、代码块（含缩进代码块）里的同名行都是材料或转述，
# 不能当成本轮裁决。
VERDICT_LABELS = r"(?:最终裁决|整体结论|裁决)"
VERDICT_TOKEN_RE = re.compile(r"\b(PASS|PARTIAL|BLOCKED)\b", re.I)
VERDICT_LINE_RE = re.compile(
    r"^\s*(?:[-+*]\s+)?#{0,6}\s*\**\s*" + VERDICT_LABELS
    + r"\s*\**\s*[：:]\s*(?P<value>.*)$", re.I)
ITEM_RE = re.compile(r"^### (E\d+) — (.+)$")
OMITTED_RE = re.compile(r"^- (E\d+) `([^`]+)`：(.+)$")
# 现行标记 + 更名前的历史标记：旧台账里的图片引用必须继续被识别，
# 否则历史图片证据会在合并时静默降级为纯文本元数据。
IMAGE_MARKER_PREFIXES = ("<!-- copilot-image ", "<!-- gpt-assist-image ")

# 标准审计日志：正式复核的权威留痕。新建台账时除传入日志外也核对它，
# 避免「删掉台账 + 换一份空日志」把已有轮次重置。
# ZCode 侧路径（迁移自 ~/.reasonix/skills/copilot/.audit/copilot.jsonl）：
# Reasonix 侧历史台账绑定的是旧路径，继续在 Reasonix 侧推进；ZCode 侧从空日志开始。
DEFAULT_AUDIT_LOG = os.path.expanduser(
    "~/.zcode/skills/copilot/.audit/copilot.jsonl")

MAX_REPLY_CHARS = 12000        # 注入 prompt 的单轮回复上限
MAX_STORED_REPLY = 20000       # 台账内保存的单轮回复上限
MAX_LEDGER_REPLY_TOTAL = 24000  # 所有往轮回复注入 prompt 的总上限


class LedgerError(Exception):
    """台账或证据合并的可预期错误（调用方应明确失败，不静默降级）。"""


def iter_fence_flags(text: str):
    """逐行产出 (行内容, 是否在代码围栏内)。围栏用实际标记长度与类型跟踪。

    供裁决解析与图片引用解析共用：围栏内的内容是数据，不能当作控制信息。
    """
    fence: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if fence is None:
            marker = re.match(r"^(`{3,}|~{3,})", stripped)
            if marker:
                fence = marker.group(1)
                yield line, True
                continue
            yield line, False
        else:
            closer = re.match(r"^(`{3,}|~{3,})\s*$", stripped)
            if closer and closer.group(1)[0] == fence[0] \
                    and len(closer.group(1)) >= len(fence):
                fence = None
            yield line, True


def parse_verdict(reply: str) -> str | None:
    """从 REVIEW 回复中提取本轮三态裁决；提取不到、或存在冲突时返回 None。

    只认代码围栏之外、非引用块、非缩进代码块的行首裁决行。回复里引用上一轮结论、
    贴输出示例、给格式模板都很常见，直接对全文 search 会把引用或示例中的 PASS
    当成本轮裁决。同一行出现多个不同三态词（如 `PASS | PARTIAL`）视为模板或自相
    矛盾，不猜；不同行给出互相冲突的裁决时同样返回 None，交给调用方按「无裁决」
    记录（不得当 PASS）。裸 `结论：` 与 `copilot-plan` 的 decision 都不算正式裁决。
    """
    found: list[str] = []
    for line, in_fence in iter_fence_flags(reply or ""):
        if in_fence:
            continue
        stripped = line.lstrip()
        if stripped.startswith(">"):
            continue
        indent = len(line) - len(stripped)
        if indent >= 4 or stripped.startswith("\t"):
            continue
        m = VERDICT_LINE_RE.match(line)
        if not m:
            continue
        tokens = {t.upper() for t in VERDICT_TOKEN_RE.findall(m.group("value") or "")}
        if len(tokens) != 1:
            # 缺值、模板（PASS | PARTIAL | BLOCKED）或自相矛盾：本行不作数。
            continue
        verdict = next(iter(tokens))
        if verdict not in found:
            found.append(verdict)
    return found[0] if len(found) == 1 else None


# ── 证据包解析 ──────────────────────────────────────────────────────────────
# 采集器约定的小节标题：出现在条目之外是正常结构；出现在条目正文之外、
# 却不属于这些小节，说明包裹围栏被提前闭合、后续正文漏到了围栏外。
PACK_SECTIONS = ("## 证据条目", "## 未纳入证据", "## 执行方说明", "## 本轮目标")


def _fence_bounds(lines: list[str]) -> tuple[int, int, str, str] | None:
    """找最外层包裹围栏：返回 (开行下标, 长度, 字符, 信息串)；没有围栏返回 None。"""
    for idx, line in enumerate(lines):
        m = re.match(r"^(`{3,}|~{3,})(.*)$", line.strip())
        if m:
            return idx, len(m.group(1)), m.group(1)[0], m.group(2).strip()
    return None


def _fence_closer(lines: list[str], start: int, width: int, char: str) -> int | None:
    """从开行之后找**第一个**合格闭合行（与解析器实际使用的那个一致）。"""
    for idx in range(start + 1, len(lines)):
        m = re.match(r"^(`{3,}|~{3,})\s*$", lines[idx].strip())
        if m and m.group(1)[0] == char and len(m.group(1)) >= width:
            return idx
    return None


def _is_image_item(kind_source: str) -> bool:
    """条目类型以标题里的 `image: ` 为准，而不是「正文里有没有围栏」。

    用围栏当代理会误伤：图片条目的 note 是合法用户文本，里面可以带围栏块；
    反过来文本正文里也可以出现图片注释（那是被评审的材料，不是控制信息）。
    """
    return kind_source.split(":", 1)[0].strip().lower() == "image"


def _image_ref(lines: list[str], is_image: bool) -> dict | None:
    """图片条目的内容键取像素指纹；非图片条目一律不看注释行。"""
    if not is_image:
        return None
    marker = next((ln for ln in lines if ln.lstrip().startswith(IMAGE_MARKER_PREFIXES)), None)
    if not marker:
        return None
    start, end = marker.find("{"), marker.rfind("}")
    if 0 <= start < end:
        try:
            ref = json.loads(marker[start:end + 1])
        except json.JSONDecodeError:
            return None
        if isinstance(ref, dict) and str(ref.get("sha256") or "").strip():
            return ref
    return None


def _content_key(payload: str, is_image: bool = False) -> str:
    """证据正文的内容键：剥掉最外层包裹围栏，只留正文。

    重复证据闸门用它判断「还是不是同一份材料」。用原始 payload 判断不成立——
    把同一内容的包裹围栏从 3 个反引号改成 4 个，payload 就变了，会被当成新证据。
    """
    lines = payload.splitlines()
    ref = _image_ref(lines, is_image)
    if ref:
        return sha256_text(f"image\0{str(ref['sha256']).strip()}")
    bounds = _fence_bounds(lines)
    if bounds is None:
        # 没有围栏的条目（早期纯元数据 / 图片）：采集元数据行不参与内容判定，
        # 否则同一份正文换一次采集时间或大小显示就会变成「新证据」。
        kept = [ln for ln in lines
                if not re.match(r"^\s*-\s*(采集方式|大小|行数|sha256|截断|备注|版本/上下文)\s*[：:]", ln)]
        return sha256_text("\n".join(kept).strip())
    start, width, char, _info = bounds
    # 只剥最外层那一对包裹围栏：正文自己经常带围栏（代码块 / 测试输出 / diff）。
    # 围栏内的正文原样参与判定——包括形如 `- 备注：...` 的内容行，
    # 它可能是这份证据的关键差异，不能当采集元数据过滤掉。
    closer = _fence_closer(lines, start, width, char)
    body = lines[start + 1:closer] if closer is not None else lines[start + 1:]
    return sha256_text("\n".join(body).strip())


def _fence_intact(payload: str) -> bool:
    """判断条目的包裹围栏是否结构完整（未被正文自带的围栏提前闭合）。

    采集器（bridge_evidence.render）对文本条目恒定输出
    `{围栏}text` + 正文 + `{围栏}`，因此有三条可判定的结构不变量：
    1. 开启行必须带信息串（`text`）——正文泄漏出来的围栏是裸围栏，
       被当成新条目的开启行时信息串为空（旧包伪造 `### E<n>` 就是这种形态）；
    2. 除最后一个纯围栏（收尾行）外，不能再出现长度 ≥ 包裹围栏的纯围栏
       ——出现就说明正文提前闭合了包裹围栏；
    3. 收尾行必须是同字符、长度不短于包裹围栏的裸围栏，且之后没有非空内容。

    `leaked` 由 `split_pack` 在丢弃行时补充（`## ` 断点）；第 1、2 条覆盖 `### E<n>` 断点。
    """
    lines = payload.splitlines()
    bounds = _fence_bounds(lines)
    if bounds is None:
        # 文本条目必须带包裹围栏（空文件也有空围栏）；只有标题、没有正文/围栏的
        # 「条目」不是有效证据。图片条目在调用前已按 kind 排除。
        return False
    start, width, char, info = bounds
    if not info:
        return False  # 文本条目的开启行必须带信息串；裸围栏说明是泄漏出来的正文围栏
    fence_lines = [(idx, m.group(1)) for idx in range(start + 1, len(lines))
                   for m in [re.match(r"^(`{3,}|~{3,})\s*$", lines[idx].strip())] if m]
    if not fence_lines:
        return False
    for idx, marker in fence_lines[:-1]:
        if marker[0] == char and len(marker) >= width:
            return False
    last_idx, last_marker = fence_lines[-1]
    if last_marker[0] != char or len(last_marker) < width:
        return False
    return not any(ln.strip() for ln in lines[last_idx + 1:])


def split_pack(text: str) -> tuple[list[dict], list[dict]]:
    """把 bridge_evidence.py 产出的证据包拆成条目与「未纳入证据」。

    只识别代码围栏之外的 `### E<n> — ` 标题，避免把证据正文里的同名行误当成新条目。
    围栏按实际标记（字符与长度）配对，不靠「遇到 ``` 就翻转」：正文里出现更长的围栏
    或 ~ 围栏时，朴素翻转会提前结束围栏，让正文中的伪造标题变成真实条目。
    条目指纹只看正文：同一内容重复提交仍要能被判定为「重复证据」。
    来源另存在 `sources` 里，由合并阶段累积——同内容不同来源不算新证据，但两个来源都可见。
    `digest` 保留旧口径（含包裹围栏行）用于历史可复现校验；重复判定用 `content_key`。
    """
    items: list[dict] = []
    omitted: list[dict] = []
    current: dict | None = None
    omitted_section = False
    fence: str | None = None

    for raw in text.splitlines(keepends=True):
        line = raw
        stripped = line.rstrip("\r\n")
        body = stripped.strip()

        if fence is None:
            opening = re.match(r"^(`{3,}|~{3,})", body)
            if opening:
                fence = opening.group(1)
                if current is not None:
                    current["lines"].append(line)
                continue
        else:
            closing = re.match(r"^(`{3,}|~{3,})\s*$", body)
            if closing and closing.group(1)[0] == fence[0] \
                    and len(closing.group(1)) >= len(fence):
                fence = None
            if current is not None:
                current["lines"].append(line)
            continue

        if stripped.startswith("### "):
            m = ITEM_RE.match(stripped)
            if m:
                if current is not None:
                    items.append(current)
                current = {
                    "old_id": m.group(1),
                    "kind_source": m.group(2),
                    "lines": [line],
                }
                omitted_section = False
                continue

        if stripped.startswith("## "):
            if current is not None:
                # 条目正文被一个 `## ` 小节标题截断时，该标题之后的内容会被整体丢弃。
                # 采集器只会在这几处放包级小节，别的 `## ` 只能是「包裹围栏提前闭合后
                # 漏到围栏外的正文」——这类行一旦丢弃就再也看不到，必须在解析阶段标出来，
                # 否则 `_fence_intact` 只看到已经被裁干净的 payload，会误判为完整。
                if not stripped.startswith(PACK_SECTIONS):
                    current["leaked"] = True
                items.append(current)
                current = None
            omitted_section = stripped.startswith("## 未纳入证据")
            continue

        if omitted_section:
            m = OMITTED_RE.match(stripped)
            if m:
                omitted.append({
                    "old_id": m.group(1),
                    "source": m.group(2),
                    "reason": m.group(3),
                })
                continue

        if current is not None:
            current["lines"].append(line)

    if current is not None:
        items.append(current)

    for it in items:
        body = "".join(it["lines"])
        heading_end = body.find("\n")
        payload = body[heading_end + 1:] if heading_end >= 0 else ""
        is_image = _is_image_item(it.get("kind_source", ""))
        # digest 保持旧口径（含包裹围栏行）：台账里的 all_item_hashes 用它做历史可复现校验。
        # content_key 只看围栏内的证据正文：重复证据闸门用它判断，
        # 否则把同一内容的包裹围栏从 3 个反引号改成 4 个就能「造出」一条新证据。
        it["body"] = body
        it["sources"] = [it.get("kind_source", "")]
        it["digest"] = sha256_text(payload.strip())
        it["content_key"] = _content_key(payload, is_image)
        # 结构完整性：包裹围栏必须由同字符、长度不短于开启围栏的裸围栏收尾，
        # 中途不能再出现长度 ≥ 包裹围栏的纯围栏（`### E<n>` 断点与 `## ` 断点都在这里挡住），
        # 且之后没有非空内容；被 `## ` 小节标题截断（leaked）同样判为不完整。
        # 旧采集器（Reasonix 侧仍用固定三反引号）遇到正文自带围栏时会提前闭合，
        # 条目在此处就会被判定为不完整；首次复核（没有历史可比）也就能挡住这类被截断的包。
        # 图片条目（kind=image）没有围栏正文，不做此判定：它的 note 是合法用户文本，
        # 里面可以带围栏块，不能因此判成截断。
        it["intact"] = (True if is_image else _fence_intact(payload)) \
            and not it.pop("leaked", False)

    for om in omitted:
        om["digest"] = sha256_text(f"{om['source']}\0{om['reason']}".strip())
        om["line"] = f"- {om['old_id']} `{om['source']}`：{om['reason']}"

    return items, omitted


def _kind_source(item: dict) -> tuple[str, str]:
    raw = item.get("kind_source", "")
    kind, _, source = raw.partition(": ")
    return (kind or "evidence"), (source or raw)


# ── 台账读写 ────────────────────────────────────────────────────────────────
def _save(path: str, data: dict) -> None:
    """原子写入台账：独占临时文件 → fsync → rename → fsync 目录。

    临时文件名必须唯一：多个写入者共用 `path + ".tmp"` 时，一个进程的 rename
    会让另一个进程的临时文件消失（实测报 FileNotFoundError），并把台账回退到旧状态。
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".",
                               suffix=".tmp")
    try:
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        try:
            os.close(dfd)
        except OSError:
            pass


# 进程内已持有的台账锁：同一进程重复调用 main（测试、批量）不必重复加锁。
_LEDGER_LOCKS: dict[str, int] = {}


def acquire_ledger_lock(path: str) -> None:
    """为同一台账的正式复核取得串行准入（flock 独占，随进程退出释放）。

    锁覆盖「读轮次 → 准入 → 请求 → 入账」整段：没有它，两个进程可能同时通过最后一轮
    准入而重复付费，后写的那个还会覆盖先写的轮次记录。非 POSIX 平台退化为不加锁。
    """
    key = os.path.realpath(path)
    if key in _LEDGER_LOCKS:
        # 同一进程内重复加锁（测试或批量脚本再次调用 main）：视为重入，
        # 跨进程互斥由 flock 保证，不在这里报错。
        return
    if fcntl is None:
        _LEDGER_LOCKS[key] = -1
        return
    lock_path = key + ".lock"
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as exc:
        raise LedgerError(f"复核台账无法建立锁文件：{lock_path}（{exc}）") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        raise LedgerError(
            f"同一复核台账正在进行另一轮 REVIEW：{key}。"
            "并发复核会绕过轮次上限，并可能产生两次付费调用；本次未发起请求。"
            "请等前一次调用结束后再继续（同一交付物不要并行复核）。") from exc
    # 故意不主动释放：整个进程生命周期就是一次复核的范围，退出时内核自动解锁。
    _LEDGER_LOCKS[key] = fd


def normalize_path(path: str) -> str:
    """把路径规范化为稳定的绝对路径（去符号链接），用于绑定比较。"""
    raw = (path or "").strip()
    if not raw:
        return ""
    return os.path.realpath(os.path.abspath(raw))


def _iter_audit(audit_log: str):
    """逐行读取审计日志；无法读取即失败，残行跳过（正常写入不会产生残行）。"""
    if not audit_log or not os.path.isfile(audit_log):
        return
    try:
        fh = open(audit_log, "r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise LedgerError(f"审计日志无法读取：{audit_log}（{exc}）") from exc
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec


def review_calls(audit_log: str, subject: str = "", ledger_path: str = "") -> list[dict]:
    """审计日志中属于该交付物的 REVIEW 成功调用。

    审计记录是台账之外的持久账：台账被删、证据包丢失、换路径重来时，
    仍能据此确认「这次复核已经发生过」，因此轮次无法靠重建台账重置。
    """
    subject = (subject or "").strip()
    target = normalize_path(ledger_path)
    if not subject and not target:
        return []
    found: list[dict] = []
    for rec in _iter_audit(audit_log):
        if rec.get("event") != "call_result" or rec.get("mode") != "REVIEW":
            continue
        if not rec.get("ok"):
            continue
        # 只有绑定台账的复核才计入轮次；一次性视觉评审（无台账）不占用台账轮次。
        if not rec.get("ledger"):
            continue
        same_subject = bool(subject) and rec.get("subject") == subject
        same_ledger = bool(target) and normalize_path(str(rec.get("ledger"))) == target
        if same_subject or same_ledger:
            found.append(rec)
    return found


def audit_binding_hits(ledger: dict, ledger_path: str, audit_log: str) -> int:
    """统计审计日志中可确认属于本台账的历史记录（call_id 或台账路径匹配）。

    只认硬证据：目录记录里的 `ledger` 指向本台账，或 `call_id` 已记在本台账轮次里；
    若台账与记录都带 subject，还要求一致。旧格式记录（无 event/call_id/ledger）不计入。
    """
    target = normalize_path(ledger_path)
    subject = str(ledger.get("subject") or "").strip()
    call_ids = {str(r.get("call_id")) for r in ledger.get("rounds", []) if r.get("call_id")}
    hits = 0
    for rec in _iter_audit(audit_log):
        if rec.get("event") != "call_result" or rec.get("mode") != "REVIEW":
            continue
        if not rec.get("ok"):
            continue
        rec_subject = str(rec.get("subject") or "").strip()
        if subject and rec_subject and rec_subject != subject:
            continue
        same_ledger = bool(target) and normalize_path(str(rec.get("ledger") or "")) == target
        same_call = str(rec.get("call_id") or "") in call_ids
        if same_ledger or same_call:
            hits += 1
    return hits


def load_or_init(path: str, risk: str | None = "normal", subject: str | None = None,
                 audit_log: str = "") -> dict:
    """读取台账；不存在则创建。

    新建必须声明交付物标识（subject），并用审计日志核对是否已有该交付物的复核记录。
    已有台账的 risk 与 subject 都不允许被覆盖；缺任何一项可核对信息时拒绝新建（安全失败）。
    """
    subject = (subject or "").strip()
    audit_ref = normalize_path(audit_log)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise LedgerError(f"复核台账无法读取：{path}（{exc}）") from exc
        if data.get("version") != LEDGER_VERSION:
            raise LedgerError(f"复核台账版本不支持：{data.get('version')}（期望 {LEDGER_VERSION}）")
        if not isinstance(data.get("rounds"), list):
            raise LedgerError("复核台账结构异常：rounds 不是列表")
        if risk is not None and risk != data.get("risk"):
            raise LedgerError(
                f"复核台账已按 risk={data.get('risk')} 创建（上限 {data.get('max_rounds')} 轮），"
                f"不能用 risk={risk} 覆盖。风险级别在创建台账时确定，之后不可更改。"
                "只有复核对象不同（新的交付物）才能新建台账；"
                "同一交付物的持续复核必须复用本台账，不得用新建台账重置轮次或改写风险级别。"
            )
        stored = str(data.get("subject") or "")
        if not stored and not subject and audit_log:
            raise LedgerError(
                f"复核台账 {path} 没有交付物标识（早期版本创建），无法核对它是否属于同一交付物。"
                "请在本次调用中用 --subject 显式声明一次（同一交付物后续沿用同一个值）；"
                "不能靠省略 --subject 跳过身份核对。")
        if subject and stored and stored != subject:
            raise LedgerError(
                f"复核台账 {path} 属于交付物 {stored!r}，不能当作 {subject!r} 使用。"
                "同一交付物的持续复核必须复用对应台账；"
                "确属新的交付物时，请使用新的台账路径与新的 --subject。")
        # 审计日志绑定：台账创建时记录用的是哪一份日志；之后正式复核必须继续用同一份。
        # 换日志会让跨台账核对失去历史，等同于重置轮次，因此拒绝而不是静默改绑。
        bound = normalize_path(str(data.get("audit_log") or ""))
        adopt_audit = False
        if audit_ref:
            if bound:
                if bound != audit_ref:
                    raise LedgerError(
                        f"复核台账 {path} 绑定的是审计日志 {bound}，本次传入的是 {audit_ref}。"
                        "同一交付物的复核必须使用同一份审计日志；换日志会失去跨台账的轮次核对，"
                        "等同于把轮次重置。请改回绑定日志。"
                        "确需更换时，由用户确认原日志后手工更新台账的 audit_log 字段；"
                        "脚本不会静默改绑。")
            else:
                hits = audit_binding_hits(data, path, audit_ref)
                if hits:
                    # 有可核对的硬证据（call_id 或台账路径）才采纳。
                    adopt_audit = True
                else:
                    raise LedgerError(
                        f"复核台账 {path} 没有记录审计日志绑定（早期版本创建），"
                        f"并且无法从 {audit_ref} 确认它记录了本台账的历史。"
                        "为避免换日志重置轮次，已拒绝继续：脚本不会把旧台账静默绑定到新日志。"
                        "请恢复原审计日志后重试；或由用户确认原日志后，手工在该台账中补 "
                        "\"audit_log\" 字段（规范化绝对路径）。")
        # 同一交付物只能有一份台账在推进：若审计显示别的台账已有本台账未包含的轮次，
        # 说明这是复制/另起的第二份历史，继续用它就不算「复用原有轮次」。
        # 先核对、后落盘：核对失败时不改动台账文件。
        subject_for_check = stored or subject
        if subject_for_check and audit_log:
            covered = {str(r.get("call_id")) for r in data.get("rounds", []) if r.get("call_id")}
            elsewhere = []
            for rec in review_calls(audit_log, subject=subject_for_check):
                call_id = str(rec.get("call_id") or "")
                if call_id and call_id in covered:
                    continue
                other = str(rec.get("ledger") or "")
                if other and normalize_path(other) != normalize_path(path):
                    elsewhere.append(other)
            if elsewhere:
                paths: list[str] = []
                for p in elsewhere:
                    if p not in paths:
                        paths.append(p)
                raise LedgerError(
                    f"交付物 {subject_for_check!r} 已在另一份台账继续复核：{', '.join(paths)}。"
                    f"{path} 缺少这些轮次记录，继续使用会把轮次拉回旧状态。"
                    "请回到该台账继续；需要搬移台账时，连同台账一起移动（审计会按 call_id 对齐）。")
        changed = False
        if subject and not stored:
            # 旧台账（无 subject）在首次显式声明时采纳，之后不再允许改动。
            data["subject"] = subject
            changed = True
        if adopt_audit:
            # 旧台账（无绑定）在可核对时采纳；之后换日志即拒绝。
            data["audit_log"] = audit_ref
            changed = True
        if changed:
            data["updated_at"] = now_iso()
            try:
                _save(path, data)
            except OSError as exc:
                raise LedgerError(f"复核台账无法写入：{path}（{exc}）") from exc
        return data

    if risk not in RISK_MAX_ROUNDS:
        if risk is None:
            raise LedgerError(f"复核台账不存在：{path}")
        raise LedgerError(f"risk 只能是 {', '.join(RISK_MAX_ROUNDS)}，收到 {risk!r}")
    if not subject:
        raise LedgerError(
            "新建复核台账必须用 --subject 声明交付物标识。没有稳定标识就无法判断这份台账"
            "是否属于同一交付物，也就无法阻止通过换路径或重建台账把轮次重置为第 1 轮。"
            "请为本次复核对象指定稳定的 subject（同一交付物始终相同）。")
    if not audit_log:
        raise LedgerError(
            "新建复核台账需要 --audit-log 核对已有复核记录：没有可核对的审计日志时拒绝新建"
            "（安全失败），以免在已有复核历史的情况下把轮次重置为第 1 轮。")
    # 新建台账必须绑定标准审计日志（唯一权威留痕）。否则「先用非标准日志 A、删台账、
    # 再用非标准日志 B 新建」会找不到 A 的历史而把轮次重置为第 1 轮——单靠查询本次日志
    # 与标准日志无法发现第三份日志里的既往轮次，因此只能在入口处限死。
    standard = normalize_path(DEFAULT_AUDIT_LOG)
    if standard and audit_ref != standard:
        raise LedgerError(
            f"新建复核台账必须使用标准审计日志：{standard}\n"
            f"本次传入的是 {audit_ref or '（空）'}。"
            "轮次核对只能覆盖「本次日志 + 标准日志」，用其他日志会留下无法跨台账发现的历史，"
            "删掉台账后即可把轮次重置为第 1 轮。请改回标准路径继续。")
    prior = review_calls(audit_ref, subject=subject, ledger_path=path)
    if prior:
        paths: list[str] = []
        for rec in prior:
            p = str(rec.get("ledger") or "")
            if p and p not in paths:
                paths.append(p)
        rounds = max(int(rec.get("review_round") or 0) for rec in prior)
        raise LedgerError(
            f"交付物 {subject!r} 已有 {len(prior)} 次 REVIEW 调用记录（最高第 {rounds} 轮），"
            "不能新建台账重置轮次。"
            f"已有台账：{', '.join(paths) or '（审计未记录路径）'}。"
            "请复用该台账继续此前的轮次；确属新的交付物时，请使用新的 --subject 与新的台账路径。")
    data = {
        "version": LEDGER_VERSION,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "subject": subject,
        "audit_log": audit_ref,
        "risk": risk,
        "max_rounds": RISK_MAX_ROUNDS[risk],
        "rounds": [],
    }
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        _save(path, data)
    except OSError as exc:
        raise LedgerError(f"复核台账无法写入：{path}（{exc}）") from exc
    return data


def _prior_pack_entries(ledger: dict) -> list[dict]:
    entries: list[dict] = []
    for rnd in ledger.get("rounds", []):
        for pack in rnd.get("evidence_packs", []):
            entries.append({**pack, "round": rnd.get("n", 0)})
    return entries


def pack_format_version(text: str) -> int:
    """读取证据包**包头**声明的格式版本；没有声明返回 0（旧格式）。

    只看第一个条目标题或第一个 `## ` 小节之前的部分——`--goal` 段属于调用方自由文本、
    位于包头区域内，里面的同名字段不能被当成格式声明；正文里的同名行同样不算。
    """
    cuts = [m.start() for m in (re.search(r"^### E\d+ — ", text, re.M),
                                re.search(r"^## ", text, re.M)) if m]
    head = text[:min(cuts)] if cuts else text
    hit = PACK_FORMAT_RE.search(head)
    return int(hit.group(1)) if hit else 0


def _assert_history_reproducible(ledger: dict, merged: list[dict],
                                 merged_omitted: list[dict]) -> None:
    """校验历史轮次记录的条目指纹都能被当前解析规则复现；否则失败关闭。

    台账的 `all_item_hashes` 是写入时的完整指纹集合。解析规则一旦变化，旧证据包可能
    被少算条目——那样合并结果会静默丢证据，且「同内容不得重复提交」的闸门会失效
    （旧指纹不再出现，同一内容重投会被当成新证据）。
    """
    stored = {str(h) for rnd in ledger.get("rounds", [])
              for h in (rnd.get("all_item_hashes") or [])}
    if not stored:
        return
    actual = {it["digest"] for it in merged} | {om["digest"] for om in merged_omitted}
    missing = stored - actual
    if not missing:
        return
    raise LedgerError(
        f"历史证据无法按当前解析规则完整复现（缺失 {len(missing)}/{len(stored)} 条条目指纹）。"
        "旧版采集器用固定三反引号包裹正文，正文自带围栏时会提前闭合，证据包本身存在歧义；"
        "当前解析规则更严格，因此会少算条目。继续合并会让这些证据无声消失，"
        "并使「同一内容不得重复提交」的闸门失效。\n"
        "请保留该台账与证据包原状，按 PARTIAL（缺证）/ BLOCKED 如实汇报；"
        "同一交付物的轮次不能重置，也不能换台账重建历史。确需继续时，先由用户确认"
        "是否把整改后的产物作为新的交付物（新的 subject 与台账）重新立项；"
        "执行方不得自行换路径、改 subject 或删除台账来绕开本限制。")


def _read_prior_pack(pack: dict) -> str:
    path = pack.get("path", "")
    if not path or not os.path.isfile(path):
        raise LedgerError(
            f"第 {pack.get('round')} 轮证据包已不存在：{path or '<空路径>'}。"
            "复核台账要求保留历史证据包；请先从备份或原采集目录恢复该文件，再继续复核。"
            "证据包丢失不构成新建台账的理由：同一交付物的 REVIEW 换台账或重建台账不会重置轮次，"
            "会被审计记录拒绝。确实恢复不了时，按 PARTIAL（缺证）/ BLOCKED 汇报并写明缺口，"
            "不要另起一份复核历史。"
        )
    digest = sha256_file(path)
    if pack.get("sha256") and digest != pack["sha256"]:
        raise LedgerError(
            f"第 {pack.get('round')} 轮证据包内容已变化：{path}"
            f"（台账 {pack['sha256'][:12]}… ≠ 当前 {digest[:12]}…）。"
            "不得用改写后的文件冒充历史证据；请把变化作为本轮新证据提交。"
        )
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


# ── 合并、变更识别与轮次闸门 ─────────────────────────────────────────────────
def _remediation_used(ledger: dict, pending: list[dict]) -> bool:
    """整改定向复核是否已用过：已入账轮次与「成功但未入账」的调用都算。"""
    return (
        any(rnd.get("extra_basis") == REMEDIATION_BASIS for rnd in ledger.get("rounds", []))
        or any(str(rec.get("review_basis") or "") == REMEDIATION_BASIS for rec in pending))


def merge_and_guard(
    ledger: dict,
    current_paths: list[str],
    extra_reason: str = "",
    extra_basis: str = "",
    audit_log: str = "",
    ledger_path: str = "",
) -> tuple[str, dict]:
    """合并历史与本轮证据，并执行轮次上限/重复证据闸门。

    返回 (合并后的证据包 Markdown, 元数据)。任何不满足纪律的情况抛 LedgerError。

    `audit_log` 用于把「GPT 已返回但台账未入账」的调用计入已用轮次：
    台账落盘失败或台账被回滚时，后续复核不能因此免费多拿一轮。
    """
    if extra_basis and extra_basis not in EXTRA_BASES:
        raise LedgerError(f"extra_basis 只能是 {', '.join(EXTRA_BASES)}，收到 {extra_basis!r}")
    if not current_paths:
        raise LedgerError(
            "REVIEW 必须提供至少一个本轮证据包。"
            "首轮没有证据时不调用 GPT 复核，直接按 PARTIAL（缺证）或 BLOCKED 汇报。"
        )
    # 绑定防御：即使调用方绕过 load_or_init 直接合并，也不允许换一份审计日志算轮次。
    bound = normalize_path(str(ledger.get("audit_log") or ""))
    if bound and audit_log and bound != normalize_path(audit_log):
        raise LedgerError(
            f"复核台账绑定的审计日志是 {bound}，本次传入的是 {normalize_path(audit_log)}。"
            "同一交付物的复核必须使用同一份审计日志，不能靠换日志重置轮次。"
            "请改回绑定日志。")

    # 已入账的调用按 call_id 记账；审计里成功但未入账的调用同样占用轮次。
    recorded_ids = {str(r.get("call_id")) for r in ledger.get("rounds", []) if r.get("call_id")}
    pending: list[dict] = []
    if audit_log:
        for rec in review_calls(audit_log, subject=str(ledger.get("subject") or ""),
                                ledger_path=ledger_path):
            call_id = str(rec.get("call_id") or "")
            if call_id and call_id not in recorded_ids:
                pending.append(rec)
    used = len(ledger.get("rounds", [])) + len(pending)
    current_round = used + 1

    prior_entries = _prior_pack_entries(ledger)
    prior_paths = {os.path.abspath(p["path"]): p.get("sha256") for p in prior_entries if p.get("path")}

    current_packs: list[dict] = []
    for raw_path in current_paths:
        path = os.path.abspath(raw_path)
        if not os.path.isfile(path):
            raise LedgerError(f"本轮证据包不存在：{path}")
        digest = sha256_file(path)
        if path in prior_paths and prior_paths[path] != digest:
            raise LedgerError(
                f"本轮证据包覆盖了此前轮次使用的路径：{path}"
                f"（历史 {prior_paths[path][:12] if prior_paths[path] else '?'}… ≠ 当前 {digest[:12]}…）。"
                "请保留历史证据包，并为新证据使用新路径。"
            )
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        version = pack_format_version(text)
        if version < PACK_FORMAT_VERSION:
            raise LedgerError(
                f"证据包格式过旧（{path}：声明 {version or '无'}，"
                f"当前要求 {PACK_FORMAT_VERSION}）。"
                "旧采集器固定用三反引号包裹正文，正文自带围栏时会提前闭合，"
                "其中的 `### E<n> —` 标题与图片注释会泄漏到围栏外，"
                "从而可能伪造出额外证据条目或被当成控制信息——"
                "这种包在结构上无法可靠区分「合法的第二个条目」与「泄漏出来的伪造条目」。"
                "请用当前 bridge_evidence.py 重新采集该证据包后再复核。")
        items, omitted = split_pack(text)
        if not items:
            raise LedgerError(
                f"证据包内没有已纳入的证据条目：{path}（只有「未纳入证据」不算证据）。"
                "REVIEW 只接受 bridge_evidence.py 产出的、实际包含条目的证据包；"
                "执行方说明不能当证据。"
            )
        # 首次复核没有历史指纹可比，旧采集器（Reasonix 侧仍固定三反引号）产出的截断包
        # 会在这里被结构校验挡住，而不是默默把被截断的正文送审。
        broken = [it for it in items if not it.get("intact", True)]
        if broken:
            names = "、".join(f"{it['old_id']}（{it['kind_source']}）" for it in broken[:3])
            raise LedgerError(
                f"证据包结构不完整：{path} 中 {len(broken)} 条条目的包裹围栏被提前闭合"
                f"（例如 {names}）。这通常是旧版采集器用固定三反引号包裹正文、"
                "正文自带围栏时造成的截断；被截断的正文不能作为证据。"
                "请用当前 bridge_evidence.py 重新采集该证据包后再复核。")
        current_packs.append({
            "path": path,
            "sha256": digest,
            "items": items,
            "omitted": omitted,
        })

    # 历史条目（按轮次顺序）+ 历史未纳入项；按内容键去重。
    # 同内容不同来源不丢来源：合并到同一编号下，把来源累积起来（F8）。
    merged: list[dict] = []
    merged_omitted: list[dict] = []
    seen: dict[str, int] = {}

    def _absorb(item: dict, origin_round: int, is_new: bool) -> dict | None:
        """把一条证据并入合并集；新内容返回合并后的条目，重复返回 None。

        用 `content_key`（只含围栏内的正文）而非 `digest` 判定重复：
        `digest` 含包裹围栏行，改一下围栏长度就能把同一份证据伪装成新证据。
        """
        key = item.get("content_key") or item["digest"]
        idx = seen.get(key)
        if idx is not None:
            holder = merged[idx]
            src = item.get("kind_source", "")
            if src and src not in holder["sources"]:
                holder["sources"].append(src)
            return None
        seen[key] = len(merged)
        entry = {**item, "origin_round": origin_round, "is_new": is_new}
        merged.append(entry)
        return entry

    for pack in prior_entries:
        text = _read_prior_pack(pack)
        items, omitted = split_pack(text)
        for it in items:
            _absorb(it, pack.get("round", 0), False)
        for om in omitted:
            if om["digest"] in seen:
                continue
            seen[om["digest"]] = len(merged_omitted)
            merged_omitted.append({**om, "origin_round": pack.get("round", 0)})

    # 历史证据必须能按当前解析规则完整复现。旧采集器用固定三反引号包裹，正文自带围栏
    # 时会提前闭合，包本身就有歧义；解析规则收紧后同一份文件会少算条目。若继续合并，
    # 被少算的条目会无声消失，而「同一内容不得重复提交」的闸门也会因为旧指纹不再复现
    # 而被绕过——所以这里失败关闭，不做静默降级。
    _assert_history_reproducible(ledger, merged, merged_omitted)

    new_items: list[dict] = []
    duplicate_current = 0
    for pack in current_packs:
        for it in pack["items"]:
            entry = _absorb(it, current_round, True)
            if entry is None:
                duplicate_current += 1
                continue
            new_items.append(entry)
        for om in pack["omitted"]:
            if om["digest"] in seen:
                continue
            seen[om["digest"]] = len(merged_omitted)
            merged_omitted.append({**om, "origin_round": current_round})

    # 闸门 1：第 2 轮起必须有新的/变化的证据，不能拿同一组证据重复复核。
    if (ledger.get("rounds") or pending) and not new_items and extra_basis != "security":
        raise LedgerError(
            "本轮证据与已提交证据内容相同（新增/变化 0 条）。"
            "不得用同一组证据重复发起 REVIEW；仅调整措辞、补充背景或复核者重复同一要求时，"
            "直接修正并记录，不重新调用。若确有新的实质证据，请先把它放进本轮证据包。"
        )

    # 闸门 2：轮次上限。达到上限即停止自动复核；两个例外：
    #  - security：新发现的重大安全问题，不设次数（安全优先）；
    #  - remediation：执行方已按上一轮裁决完成整改后的一次定向复核，每个台账只允许一次，
    #    且必须有新增/变化证据（整改产出的实际产物与验证输出）。它存在的理由是：
    #    「整改完却没有复核出口」会迫使执行方新建台账，从而事实上绕过轮次上限
    #    （2026-10-19 pedometer 长任务即如此做了 3 次「首轮」）。
    max_rounds = int(ledger.get("max_rounds", RISK_MAX_ROUNDS["normal"]))
    remediation_used = _remediation_used(ledger, pending)
    if extra_basis == REMEDIATION_BASIS:
        if remediation_used:
            raise LedgerError(
                "本台账已使用过一次整改后定向复核（remediation），不能再用第二次。"
                "若整改后仍有未闭合项，按 PARTIAL/BLOCKED 汇报并写明缺口与责任方；"
                "仅当出现新发现的重大安全问题才可用 --extra-round-basis security 越限。"
            )
        if not new_items:
            raise LedgerError(
                "整改后定向复核（remediation）必须携带整改产生的新增/变化证据："
                "本轮证据与已提交证据内容相同（新增 0 条）。"
                "只改措辞或重复同一组证据时不要重新调用，直接记录修正。"
            )
    if used >= max_rounds:
        if extra_basis not in ("security", REMEDIATION_BASIS):
            raise LedgerError(
                f"复核轮次已达上限（{used}/{max_rounds}），停止自动复核。"
                "请按 PARTIAL（缺证）或 BLOCKED 汇报，写明仍缺什么、由谁决定下一步；"
                "不要为了得到 PASS 继续复核，也不要重复提交已经提交过的证据。"
                "两个例外：新发现的重大安全问题用 --extra-round-basis security；"
                "已按上一轮裁决完成整改、需要一次定向复核时用 --extra-round-basis "
                "remediation（每份台账仅限一次，且必须带新增证据）。"
                + (f"（另有 {len(pending)} 次 GPT 已返回但未入账的调用，已按已用轮次计入；"
                   "先修复台账，不要靠重试重置轮次。）" if pending else "")
            )
        if not extra_reason.strip():
            raise LedgerError(
                "以 security / remediation 越过轮次上限时，仍需 --extra-round-reason "
                "说明安全风险或整改内容（改了什么、对应哪条裁决、新增证据是哪几条）。"
            )

    # 闸门 3：首轮之外，每次复核都必须说明“为什么还需要一轮”。
    # 只调整措辞、补充背景或重复同一要求时，不应走到这里。
    if used >= 1 and not extra_reason.strip():
        raise LedgerError(
            f"这是第 {used + 1} 次 REVIEW。除首轮外，每次复核都必须用 --extra-round-reason "
            "说明它对应哪一种理由：新的实质性证据 / 尚未解决的关键缺陷 / 足以改变结论的矛盾。"
            "若只是调整措辞、补充背景或重复同一要求，直接修正并记录，不重新调用。"
        )

    merged_text = _render_merged(merged, merged_omitted, current_round, new_items, duplicate_current)
    meta = {
        "round": current_round,
        "used": used,
        "pending_calls": len(pending),
        "pending_call_ids": [str(rec.get("call_id") or "") for rec in pending],
        "max_rounds": max_rounds,
        "risk": ledger.get("risk"),
        "current_packs": [{"path": p["path"], "sha256": p["sha256"]} for p in current_packs],
        "items": [
            {
                "id": f"E{idx}",
                "source": _kind_source(it)[1],
                "kind": _kind_source(it)[0],
                "sources": it.get("sources") or [_kind_source(it)[1]],
                "origin_round": it.get("origin_round", 0),
                "is_new": bool(it.get("is_new")),
                "digest": it["digest"],
            }
            for idx, it in enumerate(merged, 1)
        ],
        "omitted": [
            {
                "id": f"E{len(merged) + idx}",
                "source": om["source"],
                "reason": om["reason"],
                "origin_round": om.get("origin_round", 0),
            }
            for idx, om in enumerate(merged_omitted, 1)
        ],
        "new_ids": [f"E{len(merged) - len(new_items) + idx}" for idx in range(1, len(new_items) + 1)],
        "new_count": len(new_items),
        "carried_count": len(merged) - len(new_items),
        "duplicate_count": duplicate_current,
        "merged_chars": len(merged_text),
        "all_item_hashes": [it["digest"] for it in merged] + [om["digest"] for om in merged_omitted],
        "extra_reason": extra_reason.strip(),
        "extra_basis": extra_basis,
    }
    return merged_text, meta


def _rewrite_image_ids(body: str, eid: str) -> str:
    """只改写图片引用行里的 id，避免误改证据正文中的同名 JSON 字段。"""
    out: list[str] = []
    for line in body.splitlines(keepends=True):
        if line.lstrip().startswith(IMAGE_MARKER_PREFIXES):
            start, end = line.find("{"), line.rfind("}")
            if 0 <= start < end:
                try:
                    ref = json.loads(line[start:end + 1])
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(ref, dict):
                        ref["id"] = eid
                        line = (line[:start] + json.dumps(ref, ensure_ascii=False, sort_keys=True)
                                + line[end + 1:])
        out.append(line)
    return "".join(out)


def _insert_before_fence(body: str, note: str) -> str:
    """把一行说明插到条目正文的包裹围栏之前（采集元数据区末尾）。

    追加到围栏之后会让本包再次被解析时出现「收尾行之后还有内容」，
    被结构完整性校验判成截断。
    """
    lines = body.splitlines()
    for idx, line in enumerate(lines):
        if re.match(r"^(`{3,}|~{3,})", line.strip()):
            return "\n".join(lines[:idx] + [note, ""] + lines[idx:])
    return body.rstrip("\n") + "\n" + note


def _render_merged(
    items: list[dict],
    omitted: list[dict],
    round_no: int,
    new_items: list[dict],
    duplicate_current: int,
) -> str:
    lines = [
        f"# copilot 复核合并证据包（第 {round_no} 轮）",
        "",
        f"- {PACK_FORMAT_FIELD}：{PACK_FORMAT_VERSION}",
        f"- 生成时间：{now_iso()}",
        f"- 本轮：新增/变化 {len(new_items)} 条；沿用此前 {len(items) - len(new_items)} 条；"
        f"当前包内与历史重复、已去重 {duplicate_current} 条；未纳入 {len(omitted)} 条。",
        f"- 统一编号：E1–E{len(items)}"
        + (f"（未纳入项 E{len(items) + 1}–E{len(items) + len(omitted)}）" if omitted else ""),
        "- 编号说明：以下编号是本轮统一编号。来源文件正文里出现的 E 编号可能是该文件内部标签，"
        "不是本包条目 ID；引用一律以 `###` 标题为准。",
        "- 历史纪律：此前证据按提交轮次保留。若后提交的证据与先前矛盾，**不要默认采信任何一方**；"
        "保留双方来源，判断是对象状态变化、条件差异还是真正矛盾，并说明采信理由；"
        "无法解释的矛盾按未闭合处理。",
        "",
        "## 证据条目",
        "",
    ]
    for idx, it in enumerate(items, 1):
        eid = f"E{idx}"
        body = re.sub(r"^### E\d+ — ", f"### {eid} — ", it["body"], count=1, flags=re.M)
        # 只有图片条目才重写控制标记；文本正文里的同名注释是数据，改写会破坏证据保真。
        if _is_image_item(it.get("kind_source", "")):
            body = _rewrite_image_ids(body, eid)
        extra = [s for s in (it.get("sources") or [])[1:] if s]
        if extra:
            # 同内容不同来源必须都可见。插在包裹围栏**之前**（采集元数据区），
            # 不能追加在围栏之后——否则本包被再次解析时最后一条会被判成结构不完整。
            note = ("- 相同内容的其他来源（同一编号，非重复条目）："
                    + "、".join(f"`{s}`" for s in extra))
            body = _insert_before_fence(body, note)
        lines.append(body.rstrip("\n"))
        lines.append("")
    if omitted:
        lines += ["## 未纳入证据（禁止据此判为通过）", ""]
        base = len(items)
        for idx, om in enumerate(omitted, 1):
            lines.append(f"- E{base + idx} `{om['source']}`：{om['reason']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def render_ledger(ledger: dict, meta: dict) -> str:
    """把跨轮状态渲染成一段直接注入 prompt 的 Markdown。"""
    used = int(meta.get("used", len(ledger.get("rounds", []))))
    max_rounds = int(ledger.get("max_rounds", RISK_MAX_ROUNDS["normal"]))
    over_limit = used >= max_rounds
    basis = meta.get("extra_basis") or ""
    if over_limit and basis == "security":
        over_note = "（超限复核：安全事件）"
    elif over_limit and basis == REMEDIATION_BASIS:
        over_note = "（超限复核：整改后定向复核，一次性）"
    elif over_limit:
        over_note = "（超限复核）"
    else:
        over_note = ""
    lines = [
        "=== 复核台账（copilot 自动注入，不是执行方结论）===",
        "",
        f"- 风险级别：{ledger.get('risk')}；已用 REVIEW 轮次：{used} / {max_rounds}；"
        f"本次为第 {used + 1} 轮{over_note}。",
        f"- 本轮证据：新增/变化 {meta['new_count']} 条；沿用此前 {meta['carried_count']} 条；"
        f"当前包内与历史重复去重 {meta['duplicate_count']} 条。",
    ]
    if meta.get("pending_calls"):
        lines.append(
            f"- 未入账调用：{meta['pending_calls']} 次（GPT 已返回但台账未记录，"
            "已按已用轮次计入，不得靠重试重置）。"
        )
    if meta.get("extra_reason"):
        if basis == "security":
            label = "超限复核理由（安全事件）" if over_limit else "复核理由"
        elif basis == REMEDIATION_BASIS:
            label = "整改后定向复核理由（一次性，不安排下一轮）" if over_limit else "复核理由"
        else:
            label = "超限复核理由" if over_limit else "复核理由"
        lines.append(f"- {label}（`{basis or '未指定'}`）：{meta['extra_reason']}")

    by_round: dict[int, list[str]] = {}
    for it in meta["items"]:
        by_round.setdefault(int(it["origin_round"]), []).append(it["id"])
    lines.append("- 已提交证据（合并后编号，按首次提交轮次）：")
    if by_round:
        for rnd in sorted(by_round):
            label = "本轮" if rnd == meta["round"] else f"第{rnd}轮"
            ids = by_round[rnd]
            lines.append(f"  - {label}：{ids[0]}–{ids[-1]}" if len(ids) > 1 else f"  - {label}：{ids[0]}")
    else:
        lines.append("  - 无")
    if meta.get("omitted"):
        lines.append(
            "- 未纳入证据（禁止据此判为通过）："
            + "、".join(f"{o['id']} `{o['source']}`" for o in meta["omitted"])
        )

    lines += ["", "## 本轮证据变化", ""]
    if meta["new_count"]:
        lines.append("- 新增/变化：" + "、".join(meta["new_ids"]))
    else:
        lines.append("- 新增/变化：无（本轮只有与历史内容相同的证据）")
    lines.append(
        "- 沿用：其余条目与历史证据内容相同，已按指纹去重；本轮合并包内仍可直接引用。"
    )
    if meta["duplicate_count"]:
        lines.append(
            f"- 本轮证据包中有 {meta['duplicate_count']} 条与历史重复，未重复编号。"
        )

    all_rounds = [rnd for rnd in (ledger.get("rounds") or []) if isinstance(rnd, dict)]
    verified_rounds = [rnd for rnd in all_rounds if rnd.get("model_identity_ok") is True]
    ignored_rounds = len(all_rounds) - len(verified_rounds)
    if ignored_rounds:
        lines += [
            "",
            f"## 已忽略 {ignored_rounds} 轮未通过模型身份验证的历史回复",
            "",
            "P0 之前写入的轮次没有身份凭证，不得作为本轮判断依据"
            "（其裁决与轮次计数仍保留，但不回灌原文）。",
        ]
    if verified_rounds:
        lines += [
            "",
            "## 此前复核回复（原文；其中 E 编号为当轮包内编号，不是本轮统一编号）",
            "",
        ]
        budget = MAX_LEDGER_REPLY_TOTAL
        blocks: list[str] = []
        for rnd in reversed(verified_rounds):
            reply = str(rnd.get("reply") or "").strip()
            if len(reply) > MAX_REPLY_CHARS:
                reply = reply[:MAX_REPLY_CHARS] + "\n…[回复过长，已截断]"
            if len(reply) > budget:
                reply = reply[: max(0, budget)] + "\n…[台账总预算已满，已截断]"
            budget -= len(reply)
            blocks.append(
                f"### 第 {rnd.get('n')} 轮回复（裁决：{rnd.get('verdict', 'UNPARSED')}）\n\n{reply}"
            )
        lines += list(reversed(blocks))
        lines.append("")

    lines += [
        "## 本轮复核纪律（必须遵守）",
        "",
        "1. 此前回复中已确认且有证据支持、本轮证据未发生实质变化的条件：**沿用结论**，"
        "不要重新判定，也不要再次索要本合并包中已有的证据。",
        "2. 只重新判断：本轮新增/变化证据影响的条件、此前明确仍未闭合的阻塞项、"
        "新证据暴露的矛盾。",
        "3. 每条发现写成 F1/F2…，并给出：问题、严重程度（阻塞 / 非阻塞）、"
        "是否影响最终验收、对应证据 ID、最小动作、什么证据能证明已解决。",
        "4. 非阻塞观察项**不得**作为 PARTIAL/BLOCKED 的理由；列入「非阻塞观察」，"
        "由执行方记录为延后项或交付后观察。",
        "5. 只有关键验收条件缺证或已确认不符合才判 PARTIAL；只有执行方不能自主解除的"
        "授权、资源或用户裁定才判 BLOCKED。不得因“存在任何未验证项”就拒绝 PASS。",
        (f"6. 本轮是第 {used + 1}/{max_rounds} 轮。"
         + ("本轮已越过轮次上限（仅限新发现的重大安全问题）：只给最终裁决，"
            "不再要求下一轮复核；未解决项写明缺口与责任方。"
            if over_limit and basis == "security" else
            "本轮是整改后的一次性定向复核（已越过轮次上限）：只判断整改证据是否关闭此前的"
            "阻塞项、以及整改是否引入新的实质问题；只给最终裁决，不安排下一轮自动复核。"
            if over_limit and basis == REMEDIATION_BASIS else
            "若达到上限，只给最终裁决，不再要求下一轮复核；未解决项写明缺口与责任方。")),
        "7. 新发现的重大安全问题不受轮次上限限制：立即判 BLOCKED 或要求停止受影响操作，"
        "并说明需要用户裁定的事项。",
        "8. 引用证据一律用本轮合并包编号；往轮回复中的编号只作历史参考。",
        "",
    ]
    return "\n".join(lines)


def _identity_fields(identity: dict | None) -> dict:
    """校验并规范化模型身份留痕；缺失即拒绝入账（P0 安全边界）。"""
    if not isinstance(identity, dict):
        raise LedgerError(
            "复核轮次缺少模型身份凭证：未验证模型身份的回复不得计入台账。")
    requested = str(identity.get("model_requested") or "").strip()
    resolved = str(identity.get("model_resolved") or "").strip()
    if identity.get("model_identity_ok") is not True or not requested or requested != resolved:
        raise LedgerError(
            f"复核轮次模型身份未通过（请求 {requested or '-'} / 回显 {resolved or '-'}）；"
            "该回复不得计入台账。")
    source = str(identity.get("model_source") or "").strip()
    if source not in ("default", "override"):
        raise LedgerError(f"复核轮次的模型来源无效（model_source={source!r}）。")
    return {
        "model_requested": requested,
        "model_resolved": resolved,
        "model_source": source,
        "model_identity_ok": True,
    }


def record_round(
    ledger: dict,
    path: str,
    meta: dict,
    verdict: str | None,
    reply: str,
    prompt_sha256: str,
    call_id: str = "",
    *,
    identity: dict | None = None,
    attribution: dict | None = None,
) -> dict:
    """记录本轮结果并落盘；返回轮次记录。

    幂等：同一 `call_id` 已经入账时直接返回既有记录（`duplicate=True`），
    不会因为恢复或重试而重复计账。

    `identity` 是**必需**的模型身份凭证：没有「请求模型 == 回显模型且验证通过」
    的轮次不得入账，避免把未验证模型的裁决记成有效复核。`attribution` 是可选的任务
    归属（task_id / round_id / contract_sha256），有则随轮次留痕，便于跨台账核对。
    """
    call_id = (call_id or "").strip()
    if call_id:
        for existing in ledger.get("rounds", []):
            if str(existing.get("call_id") or "") == call_id:
                return {**existing, "duplicate": True}
    identity_fields = _identity_fields(identity)
    attribution_fields = {}
    if isinstance(attribution, dict):
        for key in ("task_id", "round_id", "contract_sha256"):
            value = str(attribution.get(key) or "").strip()
            if value:
                attribution_fields[key] = value
    record = {
        "n": int(meta["round"]),
        "ts": now_iso(),
        "call_id": call_id,
        "verdict": verdict or "UNPARSED",
        "evidence_packs": meta["current_packs"],
        "new_ids": meta["new_ids"],
        "new_count": meta["new_count"],
        "carried_count": meta["carried_count"],
        "merged_chars": meta["merged_chars"],
        "all_item_hashes": meta["all_item_hashes"],
        **identity_fields,
        **attribution_fields,
        "reply_sha256": sha256_text(reply),
        "reply": (reply or "")[:MAX_STORED_REPLY],
        "prompt_sha256": prompt_sha256,
        "extra_reason": meta.get("extra_reason", ""),
        "extra_basis": meta.get("extra_basis", ""),
    }
    previous_updated = ledger.get("updated_at")
    ledger.setdefault("rounds", []).append(record)
    ledger["updated_at"] = now_iso()
    try:
        _save(path, ledger)
    except OSError:
        # 落盘失败时回滚内存状态：不能出现「内存里已入账、磁盘上没有」的半记账。
        ledger["rounds"].pop()
        ledger["updated_at"] = previous_updated
        raise
    return record


# ── 只读 CLI（排查台账用）────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="查看 copilot REVIEW 复核台账（只读）")
    ap.add_argument("--file", required=True, help="台账 JSON 路径")
    args = ap.parse_args()
    try:
        ledger = load_or_init(args.file, risk=None)
    except LedgerError as exc:
        print(f"copilot: {exc}", file=sys.stderr)
        return 2
    print(f"交付物：{ledger.get('subject') or '（未声明）'}；"
          f"审计日志：{ledger.get('audit_log') or '（未绑定）'}；"
          f"风险级别：{ledger.get('risk')}；上限：{ledger.get('max_rounds')} 轮；"
          f"已用：{len(ledger.get('rounds', []))} 轮")
    for rnd in ledger.get("rounds", []):
        packs = ", ".join(os.path.basename(p.get("path", "")) for p in rnd.get("evidence_packs", []))
        print(f"- 第{rnd.get('n')}轮：{rnd.get('verdict')}；新增 {rnd.get('new_count')} 条；"
              f"证据包 {packs or '无'}")
        if rnd.get("extra_reason"):
            print(f"  复核理由（{rnd.get('extra_basis') or '未指定'}）：{rnd['extra_reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

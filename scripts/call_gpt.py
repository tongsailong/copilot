#!/usr/bin/env python3
"""copilot — 经 CPA Responses API 调用 GPT-6.1-SOL（High），承载完整 ChatGPT 认知角色。

与旧 gpt-cpa 的安全不变量完全一致（能力级零工具，不靠提示词自律）：
  1. 出站请求体 *永不包含* `tools` 字段，并显式 `tool_choice="none"`。
  2. 上游响应中出现任何非良性 output 条目（function_call / web_search_call / …）即判安全事件，
     留痕、拒绝处理、退出码 5。绝不代执行。
  3. status 非 completed / 带 error / 带 incomplete_details 的响应不构成可交付裁决（退出码 6）。
  4. 超时与上游错误显式失败，不静默降级到更宽能力的链路，不伪造结果。
  5. 审计日志是正式调用的硬条件（`--audit-log` 必填），只写脱敏元数据；API key 永不进入
     日志、argv 或请求体 dump。先落盘 `call_requested` 开始记录（fsync）再出站；结果记录写
     不进去即按「审计不完整」失败，不静默成功、不自动重试。
  6. 模型身份是硬门：上游回显模型必须与请求模型完全一致。不一致 / 缺失 / 不可解析时，
     该回复**不构成任何可用结果**——不输出正文、不写 `--out`、不入复核台账、不写交接
     状态，退出码 9；只留脱敏身份证据（回显名、回复 sha256 与字符数）。
  7. 自动协作通道（带 `--dispatch-state`）严格绑定 `gpt-6.1-sol` + `reasoning.effort=high`
     + `model_source=default` + 零工具 + 标准审计路径；禁止 `--model` 覆盖或降档。
     交接状态必须带任务级凭证（task_id / contract_sha256），每次调用在发送前把
     pending_call（task_id / round_id / call_id / 合同指纹 / 模式 / 模型 / effort）落盘；
     成功后轮次、last_call 与 pending 清理在一次原子写入里提交；提交失败保留 pending，
     可用 `copilot_dispatch.py recover` 依据 call_id/round_id 精确恢复，不重复调用 GPT。
     并发共用同一状态时以 round_seq 做比较交换：后到者在发送请求前失败，不会双计费
     或覆盖已提交轮次。
  8. 本地信任边界：审计与状态里的 sha256 只证明关联与完整性，不是签名或防伪；
     能改写本机文件的用户仍在信任边界内，本脚本不宣称绝对防伪。标准审计路径之外的
     文件不能作为自动协作凭证（测试需显式设置 COPILOT_AUDIT_TEST_MODE=1）。

模型路由（2026-11-01 升级为完整认知角色）：
  * CONSULT / PLAN / REVIEW → CPA（`127` provider，Responses API）`gpt-6.1-sol`，
    请求体显式携带 `reasoning.effort="high"`（`--reasoning-effort` 可显式降档，
    默认值就是 high；不做任何自动降级或缺省回退）。
  * 三种模式是**内部模式**，不是调用范围：它们只决定留痕字段、证据纪律与输出契约，
    角色本身是完整的 ChatGPT 认知职责（交流、分析、产品探索、方案规划、验收与指导），
    覆盖范围见 `references/role.md`。用户明确要求某个模式时按其指定。
  * 原 DECIDE 模式已合并：方案比较、成本与风险权衡、方案选择与行动安排由 PLAN 承担；
    问题分析与风险识别由 CONSULT 承担；裁决与收口由 REVIEW 承担。
  * TypeSafe `jev-latest` 仍是宿主底层能力（`system_one`，`decision_model`），
    保留给既有调用方（如 `jev-triage`）按需使用；它不是 copilot 的模式，
    本脚本不承载、也不会把它的路由改发 CPA。
  * 未指定 `--mode` 时沿用 CPA 通道；显式 `--model` 只允许在 CPA 路由内覆盖，
    以及被取代或已退役的模型 ID（`gpt-6.1-sol-max`、`gpt-6.1-sol-high`）被拒绝。
    `gpt-6-astra-high` 等旧入口仍可显式指定（回滚路径，保留不删），但不再是默认值，
    且不会被自动选用。
  * 注意：宿主（ZCode）自身的规划、子代理、看图、常规复核与自动命名走宿主主模型；
    SOL 只在显式调用本 Skill 时使用。

copilot 相对旧实现的增量：
  * 把证据包（bridge_evidence.py 产出）作为独立章节注入，并在 prompt 尾部附**证据自检表**
    （条目数 / 纳入数 / 未纳入条目），使「证据不足」在 GPT 侧可见而无法被摘要掩盖。
  * 对最终 prompt 兜底脱敏一次，并报告脱敏次数。
  * **图片证据**：证据包声明的图片按魔数 + sha256 校验后读取真实像素，作为 `input_image` 内联
    （有图时 `input` 从字符串切换为 content 数组）；像素绝不能以路径、文字描述或 base64 文本代替。
    证据包声明了图片却读不到同一份像素 → 明确失败。请求体 dump / 审计只留图片元数据，不落 base64。
  * **REVIEW 复核台账**（`--review-ledger`）：合并历史证据并按内容去重/重编号，注入往轮裁决，
    执行默认复核预算（普通 2 轮 / 高风险 3 轮）、普通弹性追加额度（普通 +2 / 高风险 +3）
    与 Security 独立额度（普通 +1 / 高风险 +2，仅普通预算全部用尽后可用）；
    首轮之外的复核必须说明理由，任何追加都必须携带新增证据。
    `--dry-run` 只构建并检查 prompt/证据/台账，不调用 GPT、不写审计；但可能创建/更新
    复核台账（subject/audit 绑定），不是完全无副作用。

退出码：0 成功 / 2 用法或凭据错误 / 3 上游错误 / 4 超时 / 5 检测到非良性 output 条目 /
6 响应异常 / 7 请求可能已发出但结果审计未能写入（审计不完整）/
8 配额或冷却限制（`usage_limit_reached` / `model_cooldown`，stderr 带 `reset_seconds`）/
9 模型身份验证失败（回显模型缺失、不可解析或与请求不一致；该回复不得被消费）/
12 任务预算拒绝出站（请求数硬上限 / 已上报 Token 阈值 / 保守预估越界；
   发生在发送前，未创建 pending、未发 HTTP 请求、未递增轮次）
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import shlex
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

try:
    import fcntl
except ImportError:  # 非 POSIX 平台：退化为不加锁，其余写入路径不变
    fcntl = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_common import (  # noqa: E402
    AUDIT_TEST_MODE_ENV,
    SUPPORTED_IMAGE_MIMES,
    audit_path_trusted,
    human_bytes,
    now_iso,
    redact,
    resolve_api_key,
    sha256_file,
    sha256_text,
    sniff_image_mime,
)
import copilot_dispatch  # noqa: E402
from review_ledger import (  # noqa: E402
    PACK_FORMAT_VERSION,
    RISK_MAX_ROUNDS,
    LedgerError,
    acquire_ledger_lock,
    load_or_init,
    normalize_path,
    merge_and_guard,
    pack_format_version,
    parse_verdict,
    record_round,
    render_ledger,
    split_pack,
    iter_fence_flags,
)

DEFAULT_BASE_URL = "http://127.0.0.1:8317/v1"
# CPA 通道（127 provider，Responses API）的 GPT 认知角色模型。
# 2026-11-01 升级：gpt-6-astra-high → gpt-6.1-sol + 请求体 reasoning.effort=high。
# 实测依据（2026-11-01 单次真实调用）：CPA /v1/models 返回该裸 ID；请求名与响应
# `model` 字段完全一致（无别名、无回退）；上游接受 `reasoning.effort="high"` 并产出
# 独立 reasoning 条目。**未验证**：High 相对低档位的实际能力增益（缺对照实验）。
# `-high` 后缀不存在于 SOL 的 CPA 别名里，因此 High 必须由客户端在本脚本内注入。
DEFAULT_MODEL = "gpt-6.1-sol"
# 推理强度：默认 high（显式写进每个请求体）。可显式降档，但绝不自动降档。
DEFAULT_REASONING_EFFORT = "high"
REASONING_EFFORTS = ("none", "low", "medium", "high")
API_KEY_ENV = "CUSTOM_127_API_KEY"
ENDPOINT = "/responses"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ROLE_FILE = os.path.join(HERE, "references", "role.md")

# 模式 → 传输通道。copilot 只有三种正式模式，全部走 CPA Responses API。
# 原 DECIDE 已并入 CONSULT/PLAN/REVIEW；TypeSafe/jev-latest 仍是宿主
# system_one 的底层能力，由 jev-triage 等既有调用方使用，不在此路由。
MODE_ROUTES = {
    "CONSULT": "cpa",
    "PLAN": "cpa",
    "REVIEW": "cpa",
}
# 已退役 / 不再是本通道目标的模型 ID：显式拒绝，避免静默沿用旧路由或悄悄换模型。
RETIRED_MODELS = {"gpt-6.1-sol-high", "gpt-6.1-sol-max"}

EXIT_OK, EXIT_USAGE, EXIT_UPSTREAM, EXIT_TIMEOUT, EXIT_TOOLCALL, EXIT_SHAPE = 0, 2, 3, 4, 5, 6
EXIT_AUDIT = 7  # 请求可能已发出，但结果审计未能写入：审计不完整，不得视为成功
# 配额/冷却限制：与「上游错误」分开，便于主控区分「等一会儿再试」与「链路坏了」。
# 仍不自动重试——429 不证明计费未发生，也不证明额度已恢复。
EXIT_QUOTA = 8
# 模型身份验证失败：回显模型缺失、不可解析或与请求不一致。该回复不得输出、落盘、
# 入账或写回交接状态——未经验证身份的响应不能成为任何有效 GPT 指导。
EXIT_IDENTITY = 9
# 预算耗尽：本任务请求数已达硬上限，或累计 Token 已达阈值/预估将越过阈值。
# 该退出码发生在发送前（或保守预估拦截），不得据此认为本次请求已发出。
EXIT_BUDGET = 12
# 配额类错误码（CPA / 上游 usage 限制与模型冷却）。命中即退出码 8，不重试、不换模型。
QUOTA_ERROR_CODES = {"usage_limit_reached", "model_cooldown"}
BENIGN_OUTPUT_TYPES = {"message", "reasoning"}
TEXT_FIELDS = {"input", "instructions"}
# REVIEW 的默认输出契约：不依赖调用方任务文本是否写了标准格式。
# GPT 仍可写 `最终裁决：` / `整体结论：`，但程序优先识别独立的标准行。
REVIEW_OUTPUT_CONTRACT = (
    "\n## 输出契约（REVIEW，必须遵守）\n\n"
    "必须在回复中单独一行输出一个正式裁决：`裁决：PASS`、`裁决：PARTIAL` 或 "
    "`裁决：BLOCKED`。该行不得放进代码块、引用块或示例中；全篇只能有一个裁决值。\n"
    "`最终裁决：` / `整体结论：` 只在明确且唯一时兼容；裸 `结论：` 与 "
    "`copilot-plan` 的 decision 不是三态裁决。\n"
)

# 可证明「请求尚未送达上游」的网络错误特征：连接根本没建立，自动重试不会重复计费。
# 其余传输层失败（超时、连接被重置、响应读取中断等）请求结果未知——上游可能已接收并
# 处理本次 POST（CPA 无幂等键，不得假设上游支持幂等），自动重发可能产生重复付费调用，
# 因此只写审计并显式报告「结果未知」，由人工确认后再决定是否重试。
# 已收到 HTTP 错误响应（含 429/5xx），但无法据此判断模型服务是否处理请求或产生费用，
# 因此同样不自动重发，只保留原始状态码与输出留痕并显式失败。
NEVER_SENT_HINTS = ("refused", "getaddrinfo", "name or service not known",
                    "temporary failure in name resolution", "no route to host",
                    "network is unreachable")


def _review_round_meta(merged_meta: dict) -> dict:
    """REVIEW 轮次审计字段：普通预算、Security 独立额度与新增证据留痕。

    审计是预算与追加理由的权威留痕；这里只写元数据，裁决仍在结果记录里。
    Security 轮次额外记录新增证据编号与 SHA-256，便于事后还原证据变化。
    """
    meta = {
        "review_round": merged_meta["round"],
        "review_max_rounds": merged_meta["max_rounds"],
        "review_default_rounds": merged_meta.get("default_rounds", merged_meta["max_rounds"]),
        "review_kind": merged_meta.get("review_kind", "normal"),
        "review_risk": merged_meta.get("risk"),
        "review_extra_index": merged_meta.get("extra_index", 0),
        "review_extra_allowance": merged_meta.get("extra_allowance"),
        "review_extra_remaining_after": merged_meta.get("extra_remaining_after"),
        "review_ordinary_used": merged_meta.get("ordinary_used"),
        "review_security_used": merged_meta.get("security_used"),
        "review_security_index": merged_meta.get("security_index", 0),
        "review_security_allowance": merged_meta.get("security_allowance"),
        "review_security_remaining": merged_meta.get("security_remaining_after"),
        "review_basis": merged_meta.get("extra_basis") or "",
        "review_reason": merged_meta.get("extra_reason") or "",
        # 证据完整性留痕：哪些条目正文不完整、是否带理由放行。
        "review_truncated_items": [t["id"] for t in (merged_meta.get("truncated") or [])],
        "review_truncated_chars": merged_meta.get("truncated_chars", 0),
        "review_truncation_accept_reason": merged_meta.get("truncation_accept_reason") or "",
    }
    if meta["review_kind"] == "security":
        new_ids = set(merged_meta.get("new_ids") or [])
        meta["review_new_evidence"] = [
            {"id": it["id"], "sha256": it["digest"]}
            for it in merged_meta.get("items", []) if it["id"] in new_ids
        ]
    return meta


def request_result_unknown(exc: Exception) -> bool:
    """True = 无法证明上游未收到请求（不得自动重发同一付费 POST）。"""
    if isinstance(exc, urllib.error.URLError):
        reason = str(exc.reason).lower()
        return not any(hint in reason for hint in NEVER_SENT_HINTS)
    return True  # TimeoutError 及其他传输层异常：一律按结果未知处理（fail-closed）

# 图片证据：证据包里的机器可读引用（bridge_evidence.py 写入），像素由此处读取并内联
# 现行标记 + 更名前的历史标记：旧复核台账里的图片引用必须继续被识别，
# 否则合并包中的历史像素证据会静默丢失（只剩元数据，GPT 看不到图）。
IMAGE_REF_RE = re.compile(
    r"^<!--\s*(?:copilot|gpt-assist)-image\s+(\{.*\})\s*-->[ \t]*$", re.M)
IMAGE_HEAD = 64
MAX_IMAGES = 8
MAX_IMAGE_BYTES = 8 << 20


def die(code: int, message: str) -> None:
    print(f"copilot: {message}", file=sys.stderr)
    sys.exit(code)


def read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        die(EXIT_USAGE, f"无法读取 {path}：{exc}")


class AuditError(Exception):
    """审计记录无法可靠写入。正式调用的审计是硬条件，不是可选旁路。"""


def _rollback(fd: int, start: int, written: int) -> None:
    """撤销本次写入留下的残行；只在文件尾部正好是本次写入时才截断。

    并发追加时文件大小已不等于 start + written，此时宁保留残行，也不删除其他
    进程已经 fsync 的记录：审计丢失比不可解析的一行更严重。
    """
    try:
        if written and os.fstat(fd).st_size == start + written:
            os.ftruncate(fd, start)
            os.fsync(fd)
    except OSError:
        pass


def _fsync_dir(path: str) -> None:
    """新建审计文件后同步父目录项。失败只警告：文件本身已 fsync，不因此阻断调用。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        dfd = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        print(f"copilot: 警告：审计目录无法打开，跳过目录 fsync（{exc}）", file=sys.stderr)
        return
    try:
        os.fsync(dfd)
    except OSError as exc:
        print(f"copilot: 警告：审计目录 fsync 失败（{exc}）", file=sys.stderr)
    finally:
        try:
            os.close(dfd)
        except OSError:
            # 目录 fsync 是加固项：关闭失败不改变「记录已写入」的判断。
            pass


def _audit_sanitize(value: object) -> object:
    """审计出口脱敏：上游错误/工具条目/usage 等自由文本可能回显提示词或凭据。

    只保留事件类型、状态与调用关联信息；自由文本统一脱敏并限制长度。
    """
    if isinstance(value, str):
        cleaned = redact(value)
        if len(cleaned) > 4000:
            return cleaned[:4000] + f"...[truncated {len(cleaned) - 4000} chars]"
        return cleaned
    if isinstance(value, dict):
        return {str(k): _audit_sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_audit_sanitize(v) for v in value]
    return value


def append_audit(path: str, record: dict) -> None:
    """追加一条审计记录：写入并 fsync 成功才返回，否则抛 AuditError。

    严格模式：不吞写入错误、不静默跳过。调用方据此在出站前失败（开始记录）
    或按「审计不完整」失败（结果记录），不会出现「请求发出但没有留痕」。
    """
    if not path:
        raise AuditError("未指定审计日志路径（--audit-log 是正式 GPT 调用的必需参数）")
    line = json.dumps(_audit_sanitize(record), ensure_ascii=False) + "\n"
    data = line.encode("utf-8")
    created = not os.path.exists(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    except OSError as exc:
        raise AuditError(f"审计日志无法打开：{path}（{exc}）") from exc
    if fcntl is not None:
        # 同一日志被多个调用并发写时串行化：让「写入 — fsync — 必要时回滚」成为原子区。
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            raise AuditError(f"审计日志无法加锁：{path}（{exc}）") from exc
    try:
        try:
            start = os.fstat(fd).st_size
        except OSError as exc:
            raise AuditError(f"审计日志无法读取状态：{path}（{exc}）") from exc
        written = 0
        try:
            written = os.write(fd, data)
            if written != len(data):
                raise OSError(f"写入不完整（{written}/{len(data)} 字节）")
            os.fsync(fd)
        except OSError as exc:
            _rollback(fd, start, written)
            raise AuditError(f"审计日志写入失败：{path}（{exc}）") from exc
    finally:
        try:
            os.close(fd)
        except OSError:
            # 记录已写入时，close 失败不改变「已落盘」的判断。
            pass
    if created:
        _fsync_dir(path)


# ── 模型路由 ────────────────────────────────────────────────────────────────

def resolve_route(mode: str, model_flag: str = "") -> tuple[str, str]:
    """按模式解析 (channel, model)。

    copilot 只暴露 CONSULT / PLAN / REVIEW 三种正式模式，全部走 CPA Responses API。
    原 DECIDE 已合并到这三种模式；TypeSafe `jev-latest` 仍是宿主 `system_one`
    的底层能力，由既有调用方按需使用，不是本脚本的模式，也不得改发 CPA。

    失败关闭：任何不在三种模式内的非空 mode（包括已移除的 DECIDE、拼错成 DECID）
    都在发送前拒绝，不默认落到 CPA。
    仅空 mode 沿用既有兼容行为（未指定模式时走 CPA 通道）。
    """
    model = model_flag.strip()
    if model:
        provider, sep, bare = model.partition("/")
        if sep:
            if provider != "127":
                die(EXIT_USAGE, f"模型 {model!r} 不属于 CPA 通道：本脚本只发送到 "
                                f"`127` provider，不能改用 {provider!r}。")
            if not bare.strip():
                die(EXIT_USAGE, f"模型 {model!r} 只有 provider 前缀、没有模型名；"
                                f"请写 {DEFAULT_MODEL!r} 或 `127/{DEFAULT_MODEL}`。")
            model = bare.strip()
        if model in RETIRED_MODELS:
            die(EXIT_USAGE, f"模型 {model!r} 已退役，不再属于当前路由。"
                            f"CPA 通道使用 {DEFAULT_MODEL!r}。")

    if mode not in MODE_ROUTES:
        if mode:
            die(EXIT_USAGE, f"未知模式 {mode!r}。只接受 CONSULT / PLAN / REVIEW"
                            "（空值表示沿用 CPA 通道）。拼错的模式不会被发送到任何后端。")
        return "cpa", (model or DEFAULT_MODEL)

    return "cpa", (model or DEFAULT_MODEL)


# ── 证据包自检 ──────────────────────────────────────────────────────────────


def evidence_selfcheck_text(text: str, display_path: str) -> tuple[str, dict]:
    """解析证据包文本，产出交给 GPT 的自检表。缺证在 GPT 侧必须可见。"""
    items, omitted = split_pack(text)
    entries = [it["old_id"] for it in items]
    entry_map = [(it["old_id"], it["kind_source"]) for it in items]
    refused = [(om["old_id"], om["source"], om["reason"]) for om in omitted]
    image_refs = parse_image_refs(text, display_path)
    image_ids = [str(r.get("id", "?")) for r in image_refs]
    truncated = [
        {"id": it["old_id"], "source": it["kind_source"],
         "omitted_chars": it.get("omitted_chars", 0)}
        for it in items if it.get("truncated")
    ]
    stats = {
        "path": display_path,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "chars": len(text),
        "entries": len(entries),
        "entry_ids": entries,
        "entry_map": [{"id": i, "source": s} for i, s in entry_map],
        "images": image_ids,
        "unincluded": [{"id": i, "source": s, "reason": r} for i, s, r in refused],
        "truncated": truncated,
    }
    lines = [
        "",
        "## 证据自检表（由 copilot 自动生成，非执行方结论）",
        "",
        f"- 证据包：`{display_path}`",
        f"- 证据包 sha256：`{stats['sha256']}`（{stats['chars']} 字符）",
        f"- 证据条目数：{stats['entries']}（{', '.join(entries) if entries else '无'}）",
    ]
    if entry_map:
        lines.append("- 条目对照（引用一律用这个编号，不要用来源文件内部标签）：")
        lines += [f"  - {i} ← `{s}`" for i, s in entry_map]
    if image_ids:
        lines.append(
            f"- 图片证据：{len(image_ids)} 条（{', '.join(image_ids)}）——真实像素已作为 `input_image` "
            "内联在本消息中；视觉判断只能依据这些图像本身，文件名、路径与本表元数据都不构成视觉证据。"
        )
    if truncated:
        lines.append(
            f"- **证据被截断 {len(truncated)} 条**（采集器按上限保留头尾，正文不完整）：")
        lines += [f"  - {t['id']} `{t['source']}`：省略 {t['omitted_chars']} 字符" for t in truncated]
        lines.append(
            "  - 对这些条目，「没看到」不等于「没有」：不得据此判定缺证或已满足；"
            "需要全文时按「需补取的最小证据」列出，由执行方重新采集。")
    if refused:
        lines.append(f"- **未纳入证据 {len(refused)} 条**（不得据此推断为通过）：")
        lines += [f"  - {i} `{s}`：{r}" for i, s, r in refused]
    else:
        lines.append("- 未纳入证据：无（所有采集源均已纳入）")
    lines += [
        "",
        "判定口径（不得违反）：",
        "- 本表只说明证据包里有什么、缺什么；**不构成任何 PASS 依据**。",
        "- 若关键验收条件在证据条目中没有对应材料 → 判 PARTIAL（缺证），并列出需要补取的条目。",
        "- 若未纳入证据中含关键条件 → 同样按缺证处理，不得假设其已通过。",
        "- 只有证据实际支持时才判 PASS；关键条件缺证时：执行方可补 → PARTIAL（缺证），"
        "需用户授权 / 资源 → BLOCKED。",
        "- 非阻塞观察项不得作为 PARTIAL/BLOCKED 的理由，也不得要求为此继续复核。",
        "",
    ]
    if not stats["entries"] and not stats["unincluded"]:
        lines.insert(3, "- ⚠ 证据包内未解析到任何条目：按「无证据」处理，不得判 PASS。")
    return "\n".join(lines) + "\n", stats


def evidence_selfcheck(path: str) -> tuple[str, dict]:
    """兼容入口：从文件读取证据包并生成自检表。"""
    return evidence_selfcheck_text(read_text(path), os.path.abspath(path))


def build_prompt(prompt: str, evidence_block: str, selfcheck: str,
                 ledger_block: str = "", dispatch_block: str = "") -> str:
    parts = [prompt.rstrip(), ""]
    if evidence_block:
        parts += ["=== 现场证据包（执行方从真实环境采集）===", "", evidence_block.strip(), ""]
    if selfcheck:
        parts += [selfcheck.strip(), ""]
    if dispatch_block:
        # 交接状态是执行反馈，不是现场证据：单独成块并显式标注，避免被当成事实依据。
        parts += [dispatch_block.strip(), ""]
    if ledger_block:
        parts += [ledger_block.strip(), ""]
    return "\n".join(parts) + "\n"


def apply_review_output_contract(mode: str, prompt: str) -> str:
    """REVIEW 默认注入正式裁决行契约；其他模式原样返回。

    任务文本可能按用户要求写成「整体结论：」等格式，但台账与审计需要可机读的
    单一裁决。这里只追加一段输出契约，不改变证据、授权或判断口径。
    """
    if mode != "REVIEW":
        return prompt
    marker = "## 输出契约（REVIEW，必须遵守）"
    if marker in prompt:
        return prompt
    return prompt.rstrip() + "\n\n" + REVIEW_OUTPUT_CONTRACT.strip() + "\n"


def build_payload(model: str, instructions: str, prompt: str, max_output_tokens: int,
                  images: list[dict] | None = None,
                  reasoning_effort: str | None = None) -> dict:
    """安全不变量：返回的 dict 必须不含 `tools` 键。

    images 为空时保持既有纯文本 input（向后兼容）；非空时按 Responses API 多模态结构构建：
    `input=[{"role":"user","content":[{"type":"input_text",...},{"type":"input_image",...}]}]`。
    图片 base64 只作为 `input_image.image_url` 的数据载体，绝不作为普通文本交给 GPT。

    `reasoning_effort` 决定请求体的 `reasoning.effort`：缺省取 `DEFAULT_REASONING_EFFORT`
    （high），传空串表示显式不加该字段（仅用于离线对比），任何取值都必须落在
    `REASONING_EFFORTS` 内。**不做任何自动降档**：调用方没要求就按默认高档发。
    """
    effort = DEFAULT_REASONING_EFFORT if reasoning_effort is None else reasoning_effort.strip()
    if effort and effort not in REASONING_EFFORTS:
        die(EXIT_USAGE, f"推理强度 {effort!r} 不受支持，只接受 "
                        f"{' / '.join(REASONING_EFFORTS)}。")

    if images:
        content: list[dict] = [{"type": "input_text", "text": prompt}]
        for img in images:
            caption = (f"\n【{img['id']} 真实图片证据】来源 {img['source']}"
                       f"（{img['mime']}，{human_bytes(img['bytes'])}，sha256 {img['sha256']}）")
            if img["note"]:
                caption += f"\n版本/上下文：{img['note']}"
            caption += "\n紧随其后的 input_image 即该文件原始像素；视觉判断以图像内容为准。\n"
            content.append({"type": "input_text", "text": caption})
            content.append({"type": "input_image", "image_url": img["data_url"]})
        payload_input: object = [{"role": "user", "content": content}]
    else:
        payload_input = prompt

    payload = {
        "model": model,
        "input": payload_input,
        "max_output_tokens": max_output_tokens,
        "store": False,
        "stream": False,
        "tool_choice": "none",
    }
    if effort:
        # 显式写进请求体：CPA 对 SOL 没有 payload 规则，不写就不会 High。
        payload["reasoning"] = {"effort": effort}
    if instructions.strip():
        payload["instructions"] = instructions
    assert "tools" not in payload, "安全不变量被破坏：请求体不得包含 tools"
    return payload


def parse_image_refs(text: str, pack_path: str) -> list[dict]:
    """解析证据包里的图片证据引用（只有元数据，没有像素）。引用不可解析即失败。

    只看代码围栏之外的行：围栏里是被评审的材料（源码、日志、协议示例），
    里面的同名注释只是数据，不能触发本地图片读取或额外出站。引用还必须带
    完整 sha256——没有指纹就无法证明发送的像素就是证据包声明的那一份。
    """
    refs: list[dict] = []
    for line, in_fence in iter_fence_flags(text):
        if in_fence:
            continue
        for m in IMAGE_REF_RE.finditer(line + "\n"):
            try:
                ref = json.loads(m.group(1))
            except json.JSONDecodeError as exc:
                die(EXIT_USAGE, f"证据包 {pack_path} 的图片引用无法解析（{exc}）；"
                                "图片证据不可靠时拒绝调用 GPT。")
            if not isinstance(ref, dict):
                die(EXIT_USAGE, f"证据包 {pack_path} 的图片引用结构异常。")
            if not str(ref.get("sha256") or "").strip():
                die(EXIT_USAGE, f"证据包 {pack_path} 的图片引用缺少 sha256 指纹"
                                f"（id={ref.get('id')!r}）；无法证明发送的像素与证据一致，"
                                "请用 bridge_evidence.py 重新采集该图片。")
            ref["_pack"] = os.path.abspath(pack_path)
            refs.append(ref)
    return refs


def load_images(refs: list[dict], max_images: int, max_bytes: int) -> list[dict]:
    """读取并逐张校验图片证据，返回可内联记录（含 data URL）。

    任一环节不成立都明确失败——绝不退化成「用路径、文字或 base64 文本代替像素」。
    大小、魔数、指纹与实际发送字节都来自**同一次有界读取**：分次读取时，
    文件在两次读取之间被覆盖会导致「校验的是旧文件、发送的是新像素」。
    """
    if len(refs) > max_images:
        die(EXIT_USAGE, f"图片证据 {len(refs)} 张超出单次上限 {max_images} 张（--max-images 可调）；"
                        "请分批调用或只保留判定所需的图，不得静默丢弃视觉证据。")
    out: list[dict] = []
    for ref in refs:
        eid = str(ref.get("id") or "?")
        src = str(ref.get("source") or "")
        where = f"{eid}（证据包 {ref.get('_pack')}）"
        if not src:
            die(EXIT_USAGE, f"图片证据 {where} 没有 source 路径，无法取得真实像素。")
        if not os.path.isfile(src):
            die(EXIT_USAGE, f"图片证据 {where} 的文件不存在：{src}。证据包声明了图片却读不到像素，"
                            "不得以路径或文字描述替代，调用终止。")
        try:
            # 多读 1 字节用于判定是否超限：既得到准确上限判断，又只用一次读取。
            with open(src, "rb") as fh:
                data = fh.read(max_bytes + 1)
        except OSError as exc:
            die(EXIT_USAGE, f"图片证据 {where} 读取失败：{exc}。不得以路径或文字描述替代像素。")
        if len(data) > max_bytes:
            die(EXIT_USAGE, f"图片证据 {where} 超出单张上限 {human_bytes(max_bytes)}"
                            f"（--max-image-bytes 可调）。")
        digest = hashlib.sha256(data).hexdigest()
        expected = str(ref.get("sha256") or "").strip()
        if not expected:
            die(EXIT_USAGE, f"图片证据 {where} 在证据包里没有 sha256 指纹，无法证明发送的像素"
                            "就是被评审的那一份；请用 bridge_evidence.py 重新采集。")
        if expected != digest:
            die(EXIT_USAGE, f"图片证据 {where} 的内容与证据包记录不一致：sha256 {digest} ≠ "
                            f"{expected}。文件已被改动，拒绝按旧证据调用。")
        mime = sniff_image_mime(data[:IMAGE_HEAD])
        if mime is None:
            die(EXIT_USAGE, f"图片证据 {where} 的魔数不是受支持的图片格式"
                            f"（支持 {', '.join(SUPPORTED_IMAGE_MIMES)}）。")
        if ref.get("mime") and ref["mime"] != mime:
            die(EXIT_USAGE, f"图片证据 {where} 类型与证据包记录不一致：{mime} ≠ {ref['mime']}。")
        out.append({
            "id": eid,
            "source": os.path.abspath(src),
            "mime": mime,
            "bytes": len(data),
            "sha256": digest,
            "note": str(ref.get("note") or ""),
            "data_url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii"),
        })
    return out


def count_payload_images(payload: dict) -> int:
    """统计出站 payload 中实际携带的 input_image 数量。"""
    inp = payload.get("input")
    if not isinstance(inp, list):
        return 0
    total = 0
    for msg in inp:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            total += sum(1 for c in content
                         if isinstance(c, dict) and c.get("type") == "input_image")
    return total


def enforce_image_carry(payload: dict, images: list[dict]) -> None:
    """硬约束：证据包声明了图片，实际出站 payload 就必须逐张携带 input_image。"""
    carried = count_payload_images(payload)
    if carried != len(images):
        die(EXIT_USAGE, f"出站 payload 携带 {carried} 张 input_image，与证据包声明的 {len(images)} 张不符；"
                        "证据包声明了图片却未携带对应 input_image 时按失败处理，"
                        "不得以路径或文字描述冒充视觉证据。")


def summarize_input(value: object) -> object:
    """出站请求体审计视图：自由文本只留指纹，图片只留类型前缀与长度，绝不落像素。"""
    if isinstance(value, str):
        return {"redacted": True, "chars": len(value),
                "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()}
    if isinstance(value, list):
        messages = []
        for msg in value:
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                messages.append({"redacted": True})
                continue
            parts = []
            for chunk in content:
                ctype = chunk.get("type") if isinstance(chunk, dict) else None
                if ctype == "input_image":
                    url = str(chunk.get("image_url") or "")
                    prefix = url.split(",", 1)[0] if url.startswith("data:") else "<non-data-url>"
                    parts.append({"type": "input_image",
                                  "image_url": {"redacted": True, "data_url_prefix": prefix,
                                                "payload_chars": len(url)}})
                elif ctype == "input_text":
                    text = str(chunk.get("text") or "")
                    parts.append({"type": "input_text", "redacted": True, "chars": len(text),
                                  "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
                else:
                    parts.append({"type": ctype})
            messages.append({"role": msg.get("role") if isinstance(msg, dict) else None,
                             "content": parts})
        return messages
    return value


def write_request_dump(path: str, payload: dict) -> None:
    dumped: dict = {}
    for key, value in payload.items():
        if key in TEXT_FIELDS and isinstance(value, (str, list)):
            dumped[key] = summarize_input(value)
        else:
            dumped[key] = value
    dumped["_audit"] = {
        "tools_present": "tools" in payload,
        "tool_choice": payload.get("tool_choice"),
        "input_image_count": count_payload_images(payload),
        "note": "自由文本字段已仅保留指纹，图片仅保留类型前缀与长度；本文件不含 API key，也不含图片像素。",
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(dumped, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """拒绝一切重定向：urllib 默认会把 Authorization 原样带到重定向目标。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"redirect refused ({code}) to {newurl}；带凭据请求不跟随重定向",
            headers, fp,
        )


def post_json(url: str, api_key: str, payload: dict, timeout: float) -> tuple[int, dict, str]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "Accept": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, safe_json(raw), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return exc.code, safe_json(raw), raw
    except urllib.error.URLError as exc:
        raise TimeoutError(str(exc.reason)) if "timed out" in str(exc.reason) else exc
    except TimeoutError:
        raise


def safe_json(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def quota_signal(status: int, data: dict) -> dict | None:
    """识别「配额用尽 / 模型冷却」：命中返回信号，未命中返回 None。

    只认错误体里的 code/type 落在 `QUOTA_ERROR_CODES` 这一类可区分证据。裸 429（限流但
    没有配额错误码）沿用既有 `EXIT_UPSTREAM`，不缩小也不扩大原有退出码契约——退出码 8
    只是新增的细分，不接管原有失败路径。
    `reset_seconds` 只从上游响应体里取（HTTP 头在 post_json 里已丢弃），取不到就是 None，
    不猜默认值——猜错会让主控以为额度已恢复。
    """
    if not isinstance(data, dict):
        data = {}
    err = data.get("error") if isinstance(data.get("error"), dict) else {}
    # code 与 type 分别检查：上游可能把配额码放在 type，而 code 是通用错误码。
    # 只有命中明确集合才返回信号，裸 429 / 其他错误一律不误判。
    code = ""
    for candidate in (err.get("code"), err.get("type"),
                      data.get("code"), data.get("type")):
        value = str(candidate or "")
        if value in QUOTA_ERROR_CODES:
            code = value
            break
    if not code:
        return None
    reset = None
    for src in (err, data):
        for key in ("resets_in_seconds", "reset_seconds", "retry_after"):
            value = src.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or value < 0:
                continue
            try:
                candidate = int(value)
                # 超出 datetime 可表示范围的值无法落成 reset_at：
                # 按未知冷却处理（继续阻塞），不能让转换异常把退出码变成 1。
                datetime.now().astimezone() + timedelta(seconds=candidate)
            except (OverflowError, ValueError):
                continue
            reset = candidate
            break
        if reset is not None:
            break
    return {"status": status, "code": code or "unknown", "reset_seconds": reset}


def extract_text(payload: dict) -> str:
    parts = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for chunk in item.get("content") or []:
            if isinstance(chunk, dict) and chunk.get("type") == "output_text":
                parts.append(chunk.get("text") or "")
    return "".join(parts).strip()


def find_tool_calls(payload: dict) -> list[dict]:
    """白名单判定：BENIGN_OUTPUT_TYPES 之外的一切类型都按可疑处理。"""
    found = []
    output = payload.get("output")
    if isinstance(output, list):
        # 结构异常（非 dict 项）由 response_problem 按「响应不完整」拒绝；
        # 这里只负责识别真正的非良性 output 类型，避免把错误响应误报成安全事件。
        for item in output:
            if isinstance(item, dict) and item.get("type") not in BENIGN_OUTPUT_TYPES:
                found.append({"type": item.get("type"), "name": item.get("name")})
    if payload.get("tool_choice") not in (None, "none"):
        found.append({"type": "unexpected_tool_choice", "name": str(payload.get("tool_choice"))})
    return found


def response_problem(payload: dict) -> str | None:
    status = str(payload.get("status") or "").lower()
    err = payload.get("error")
    if err:
        detail = err.get("message") if isinstance(err, dict) else str(err)
        return f"response error: {detail}"
    # 没有完成状态就不是可交付的完整成功；不能因为「有文字」放行。
    if not status:
        return "response status missing"
    if status not in ("completed", "succeeded"):
        inc = payload.get("incomplete_details")
        return f"response status={status}" + (f" incomplete_details={inc}" if inc else "")
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
        msg_status = str(item.get("status") or "").lower()
        if msg_status and msg_status != "completed":
            return f"message status={msg_status}"
        content = item.get("content")
        if content is not None and not isinstance(content, list):
            return "malformed message content"
        for chunk in content or []:
            if not isinstance(chunk, dict):
                return f"malformed content item: {type(chunk).__name__}"
            if chunk.get("type") == "output_text" and not isinstance(chunk.get("text", ""), str):
                return "output_text is not a string"
    return None


def _same_file(a: str, b: str) -> bool:
    """两个路径是否指向同一文件（含符号链接与硬链接别名）。"""
    if not a or not b:
        return False
    try:
        if os.path.realpath(a) == os.path.realpath(b):
            return True
    except OSError:
        pass
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def assert_distinct_paths(args) -> None:
    """拒绝会让截断写入破坏审计/台账/交接状态的路径别名。"""
    targets = [
        ("--audit-log", args.audit_log),
        ("--out", args.out),
        ("--dump-request", args.dump_request),
        ("--review-ledger", args.review_ledger),
        ("--dispatch-state", args.dispatch_state),
    ]
    for i in range(len(targets)):
        for j in range(i + 1, len(targets)):
            name_a, path_a = targets[i]
            name_b, path_b = targets[j]
            if _same_file(path_a, path_b):
                die(EXIT_USAGE,
                    f"{name_a} 与 {name_b} 指向同一文件（{path_a}）；"
                    "输出或请求 dump 的截断写入会破坏审计/台账/交接记录，拒绝执行。")


def main() -> int:
    ap = argparse.ArgumentParser(description="copilot：带现场证据桥接的 GPT 认知通道（零工具）")
    ap.add_argument("--prompt-file", default="-", help="本轮任务文本；'-' 表示 stdin")
    ap.add_argument("--evidence", action="append", default=[],
                    help="bridge_evidence.py 产出的证据包；作为独立章节注入并生成自检表"
                         "（包内图片证据引用会被读取为真实 input_image）")
    ap.add_argument("--role-file", default=DEFAULT_ROLE_FILE,
                    help="GPT 的角色/规则文件（默认 copilot/references/role.md）")
    ap.add_argument("--mode", default="", help="CONSULT/PLAN/REVIEW（留痕 + 证据纪律注入）")
    ap.add_argument("--review-ledger", default="",
                    help="REVIEW 复核台账 JSON：自动合并历史证据、注入往轮裁决，并执行轮次预算"
                         "（默认普通 2 轮 / 高风险 3 轮；默认用尽后普通追加 +2 / +3；"
                         "普通预算全部用尽后 Security 独立额度 +1 / +2；任何追加都要新增证据）")
    ap.add_argument("--one-shot-visual-review", action="store_true",
                    help="显式声明一次性视觉评审（无台账）：要求证据包至少含 1 张图片，"
                         "不携带跨轮状态；普通带图续轮仍必须使用 --review-ledger")
    ap.add_argument("--risk", choices=tuple(RISK_MAX_ROUNDS), default=None,
                    help="创建台账时的风险级别（默认 normal）；台账已存在时不得改变")
    ap.add_argument("--extra-round-reason", default="",
                    help="首轮之外每次复核的理由（新增证据 / 未解决的关键缺陷 / 新矛盾）；"
                         "默认预算用尽后的普通追加轮次必须写明实际整改内容、对应的未闭合项"
                         "与新增证据是哪几条；Security 追加必须写明具体安全风险")
    ap.add_argument("--extra-round-basis", default="",
                    choices=("new-evidence", "security", "remediation"),
                    help="复核依据记录：new-evidence（新增实质证据）/ security（普通预算全部用尽后的"
                         "独立安全例外，必须带此前未用于 REVIEW 的新增证据与安全风险说明）"
                         "/ remediation（已按上一轮裁决完成整改后的定向复核）。"
                         "new-evidence 与 remediation 可在默认预算用尽后弹性追加，普通最多 +2 / "
                         "高风险 +3；security 只能在此后使用独立额度（普通 +1 / 高风险 +2），"
                         "用尽后需用户明确授权；任何依据都不允许没有新证据的重复复核")
    ap.add_argument("--accept-evidence-truncation", default="", metavar="REASON",
                    help="显式放行被采集器截断的 REVIEW 证据，并写明理由"
                         "（为什么无法重新采集、省略的是哪部分、结论如何不受影响）；"
                         "放行理由随轮次入账，审计可追溯")
    ap.add_argument("--dry-run", action="store_true",
                    help="只构建并检查 prompt/证据/台账，不调用 GPT（不写审计）；"
                         "但会读取或创建复核台账，并可能写入 subject/audit_log 绑定，"
                         "不是完全无副作用的沙盒")
    ap.add_argument("--no-redact", action="store_true", help="关闭对最终 prompt 的兜底脱敏（默认开启）")
    ap.add_argument("--model", default="",
                    help=f"仅覆盖 CPA 通道模型（默认 {DEFAULT_MODEL}）；旧入口如 "
                         f"`gpt-6-astra-high` 仍可显式指定，用于回滚")
    ap.add_argument("--reasoning-effort", default=None, choices=REASONING_EFFORTS,
                    help=f"请求体 reasoning.effort（默认 {DEFAULT_REASONING_EFFORT}）。"
                         "只会按这里显式给出的值发送，不会自动降档或自动重试换档")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--out", default="", help="把回复写入该文件（同时打到 stdout）")
    ap.add_argument("--subject", default="",
                    help="本次复核对象（交付物）的稳定标识。新建 REVIEW 台账时必须提供；"
                         "同一交付物始终使用同一个值。它会写入台账与审计，用于拒绝换路径/"
                         "重建台账来重置轮次")
    ap.add_argument("--audit-log", default="",
                    help="脱敏审计日志（JSONL，追加）。正式调用必填：缺失或不可写时在出站前失败；"
                         "结果记录写不进去按「审计不完整」失败（退出码 7）")
    ap.add_argument("--dump-request", default="", help="写明出站请求体供审计（自由文本仅留指纹）")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--max-retries", type=int, default=None,
                    help="最大重试次数（仅对可证明未送达的连接失败生效）；"
                         "native 自动任务默认 0，legacy/手动默认 2")
    ap.add_argument("--max-output-tokens", type=int, default=8192)
    ap.add_argument("--max-images", type=int, default=MAX_IMAGES,
                    help=f"单次内联图片证据上限（默认 {MAX_IMAGES}）；超出明确失败，不静默丢弃视觉证据")
    ap.add_argument("--max-image-bytes", type=int, default=MAX_IMAGE_BYTES,
                    help=f"单张图片证据大小上限（默认 {MAX_IMAGE_BYTES // (1 << 20)}MB）；超出明确失败")
    ap.add_argument("--dispatch-state", default="",
                    help="可选的 copilot 任务交接状态 JSON：注入当前工作项与执行反馈，"
                         "并在回复含 ```copilot-plan``` 时写回新工作项。"
                         "PLAN 或多阶段任务建议使用；一次性 REVIEW 不需要")
    args = ap.parse_args()

    # 任何破坏性写入之前先拒绝路径别名（同一文件被当成两个用途）。
    assert_distinct_paths(args)

    # 审计是正式调用的硬条件：先查参数，再查路径可写性，最后才允许出站。
    # --dry-run 不发送请求，因此不要求审计日志（也不写审计）。
    if not args.dry_run and not args.audit_log.strip():
        die(EXIT_USAGE,
            "--audit-log 是正式 GPT 调用的必需参数；缺少审计记录不得发送请求。"
            "请传入 --audit-log <路径>（通常为 "
            "~/.zcode/skills/copilot/.audit/copilot.jsonl）。"
            "只做离线检查时用 --dry-run（不出站、不写审计；但可能创建/更新台账）。")

    mode = args.mode.strip().upper()
    requested_model_arg = args.model.strip()
    model_explicit = bool(requested_model_arg)
    channel, model = resolve_route(mode, requested_model_arg)
    args.model = model  # 审计与出站 payload 一律使用路由解析后的模型
    # 模型选择来源：默认通道 vs 显式 --model 覆盖。覆盖是保留的回滚/手动通道，
    # 但它不是自动协作通道，回复不得冒充默认 SOL 判断（由审计/台账/状态三处留痕）。
    model_source = "default" if args.model == DEFAULT_MODEL else "override"
    if model_source == "override":
        print(f"copilot: 提示：本次为手动模型覆盖（{args.model}，默认通道为 "
              f"{DEFAULT_MODEL}）；该回复按手动模式留痕，不得当作自动协作通道的判断。",
              file=sys.stderr)
    if args.review_ledger and mode != "REVIEW":
        die(EXIT_USAGE, "--review-ledger 只用于 REVIEW 模式。")
    if args.one_shot_visual_review and mode != "REVIEW":
        die(EXIT_USAGE, "--one-shot-visual-review 只用于 REVIEW 模式。")
    if args.one_shot_visual_review and args.review_ledger:
        die(EXIT_USAGE, "--one-shot-visual-review 与 --review-ledger 不能同时使用；"
                        "多轮视觉复核请使用台账。")
    if args.one_shot_visual_review and args.dispatch_state:
        die(EXIT_USAGE, "--one-shot-visual-review 是一次性视觉评审，不携带跨轮状态，"
                        "不得与 --dispatch-state 组合（否则会写任务轮次但不入评审台账）。"
                        "需要写交接状态时请改用常规 REVIEW + --review-ledger。")
    if args.review_ledger and not args.evidence:
        die(EXIT_USAGE, "REVIEW + --review-ledger 至少需要一个 --evidence 证据包；"
                        "没有证据时不要调用 GPT，直接按 PARTIAL（缺证）/ BLOCKED 汇报。")

    prompt = sys.stdin.read() if args.prompt_file == "-" else read_text(args.prompt_file)
    if not prompt.strip():
        die(EXIT_USAGE, "任务文本为空。")

    ledger = None
    ledger_block = ""
    merged_meta = None
    evidence_block, selfchecks, stats_all = "", [], []
    image_refs: list[dict] = []

    if args.review_ledger:
        risk = args.risk
        if risk is None and not os.path.exists(args.review_ledger):
            risk = "normal"
        try:
            # 串行准入：同一台账的「读轮次 → 请求 → 入账」全程独占，
            # 否则两个进程可能同时通过最后一轮准入并各发起一次付费调用。
            if not args.dry_run:
                acquire_ledger_lock(args.review_ledger)
            ledger = load_or_init(args.review_ledger, risk=risk,
                                  subject=args.subject, audit_log=args.audit_log)
            merged_text, merged_meta = merge_and_guard(
                ledger, args.evidence, args.extra_round_reason, args.extra_round_basis,
                audit_log=args.audit_log, ledger_path=args.review_ledger)
        except LedgerError as exc:
            die(EXIT_USAGE, str(exc))
        evidence_block = (f"\n<!-- 复核合并证据包（第 {merged_meta['round']} 轮） -->\n"
                          + merged_text.strip() + "\n")
        check, stats = evidence_selfcheck_text(
            merged_text, f"{os.path.abspath(args.review_ledger)}#第{merged_meta['round']}轮")
        selfchecks.append(check)
        stats_all.append(stats)
        image_refs.extend(parse_image_refs(merged_text, args.review_ledger))
        ledger_block = render_ledger(ledger, merged_meta)

        # 编号对照：任务文本引用了包内不存在的 E 编号时明确警告。
        # 复盘案例中执行方用来源文件内部标签（§E12）写任务文本，GPT 因无法定位而反复补证。
        known = set(stats["entry_ids"]) | {u["id"] for u in stats["unincluded"]}
        referenced = {f"E{n}" for n in re.findall(r"(?<![\w/])E(\d+)(?!\.\d)", prompt)}
        missing = sorted(referenced - known, key=lambda x: int(x[1:]))
        if missing:
            print("copilot: 警告：任务文本引用了本轮合并证据包中不存在的条目 "
                  f"{', '.join(missing)}；GPT 无法定位这些引用。请改用包内编号，"
                  "或说明它们只是来源文件内部标签。", file=sys.stderr)
    else:
        for path in args.evidence:
            text = read_text(path)
            # 与台账路径同一格式门槛：旧采集器的包存在围栏歧义，
            # 正文里的 `### E<n> —`/图片注释可能泄漏到围栏外，不能当证据送审。
            version = pack_format_version(text)
            if version < PACK_FORMAT_VERSION:
                die(EXIT_USAGE,
                    f"证据包格式过旧（{path}：声明 {version or '无'}，"
                    f"当前要求 {PACK_FORMAT_VERSION}）。旧采集器固定用三反引号包裹正文，"
                    "正文自带围栏时会提前闭合，条目结构存在歧义。"
                    "请用当前 bridge_evidence.py 重新采集该证据包。")
            check, stats = evidence_selfcheck(path)
            selfchecks.append(check)
            stats_all.append(stats)
            image_refs.extend(parse_image_refs(text, path))
            evidence_block += (f"\n<!-- 证据包 {os.path.abspath(path)} -->\n" + text.strip() + "\n")

    # 截断闸门：采集器按上限保留头尾时，条目正文并不完整。让 GPT 在不完整证据上做
    # 复核，会把「没看到」当成「没有」，产出无法闭环的缺证裁决——这正是复核反复的直接
    # 来源。本轮新提交的截断条目在 REVIEW 前失败关闭，要求重新采集或显式放行；已入账
    # 轮次沿用的截断只提示，不能让一次历史截断永久锁死后续复核。
    accept_reason = args.accept_evidence_truncation.strip()
    if merged_meta is not None:
        truncated_new = list(merged_meta.get("truncated_new") or [])
        truncated_carried = [t for t in (merged_meta.get("truncated") or [])
                             if not t.get("is_new")]
    else:
        truncated_new = [t for s in stats_all for t in (s.get("truncated") or [])]
        truncated_carried = []
    if truncated_carried:
        names = "、".join(f"{t['id']}（{t['source']}）" for t in truncated_carried[:3])
        print(f"copilot: 提示：合并证据包内有 {len(truncated_carried)} 条沿用条目曾被截断"
              f"（如 {names}），已在自检表中标注为「正文不完整」；"
              "不得据其缺失内容判定缺证。", file=sys.stderr)
    if truncated_new and mode == "REVIEW":
        detail = "；".join(f"{t['id']} `{t['source']}` 省略 {t['omitted_chars']} 字符"
                          for t in truncated_new[:3])
        if not accept_reason:
            die(EXIT_USAGE,
                f"本轮证据有 {len(truncated_new)} 条被截断，正文不完整，不能用于 REVIEW"
                f"（{detail}）。被截断的条目只能作为线索，不能支撑 PASS/缺证结论。"
                "请重新采集：提高 `bridge_evidence.py --per-source-chars` / `--max-chars`，"
                "或用 `--file::A/B`、`--file::/正则/`、`--json` 精确取需要的片段；"
                "确因单条证据超过模型可承载规模而必须截断时，用 "
                "`--accept-evidence-truncation <理由>` 显式放行并留痕。")
        print(f"copilot: 警告：本轮有 {len(truncated_new)} 条证据被截断，已按显式理由放行"
              f"（{accept_reason}）；复核结论不得把被省略的内容当作不存在。", file=sys.stderr)
    elif truncated_new and not args.dry_run:
        # CONSULT/PLAN 不做硬门（证据仍可作线索），但必须让调用方知道正文不完整。
        names = "、".join(f"{t['id']}（{t['source']}）" for t in truncated_new[:3])
        print(f"copilot: 提示：本轮证据有 {len(truncated_new)} 条被截断（如 {names}），"
              "自检表已标注；结论不得把被省略的内容当作不存在。", file=sys.stderr)
    if merged_meta is not None:
        # 放行理由随轮次入账：事后能区分「当时证据完整」与「带截断放行」。
        merged_meta["truncation_accept_reason"] = (
            accept_reason if (truncated_new and mode == "REVIEW") else "")

    if mode == "REVIEW":
        if not args.evidence:
            die(EXIT_USAGE, "REVIEW 必须提供 --evidence 证据包；没有证据时不要调用 GPT，"
                            "直接按 PARTIAL（缺证）/ BLOCKED 汇报。")
        if not args.review_ledger:
            if args.one_shot_visual_review:
                if not image_refs:
                    die(EXIT_USAGE, "--one-shot-visual-review 要求证据包至少包含 1 张图片；"
                                    "文本证据请使用 --review-ledger。")
                print("copilot: 提示：按显式声明的一次性视觉评审处理"
                      "（--one-shot-visual-review）；本轮不携带跨轮状态。", file=sys.stderr)
            else:
                die(EXIT_USAGE, "REVIEW 必须使用 --review-ledger（复核台账）。"
                                "只有显式 --one-shot-visual-review 且证据包含图片的一次性视觉评审"
                                "可以免台账；普通带图续轮也必须使用台账。"
                                "没有台账时本轮不携带历史证据与往轮裁决，会重复补证；"
                                "请按 SKILL.md「REVIEW 复核收敛」创建台账。")

    if not args.evidence:
        selfchecks.insert(0,
            "\n## 证据自检表（由 copilot 自动生成）\n\n"
            "- 本轮未附加证据包（CONSULT/PLAN 常见）。若结论依赖现场事实而材料未给出，"
            "请在「待澄清」中列出需由执行方补取的最小证据，不要凭空推断。\n")

    dispatch_state = None
    dispatch_block = ""
    if args.dispatch_state:
        try:
            dispatch_state = copilot_dispatch.load_state(args.dispatch_state)
        except copilot_dispatch.DispatchError as exc:
            die(EXIT_USAGE, str(exc))
        # 已收口任务不再发起新调用：收口后只有用户授权的 reopen 能重新开工。
        if bool((dispatch_state.get("closure") or {}).get("closed")):
            die(EXIT_USAGE,
                "任务已收口（closure.closed=true），拒绝发起新调用；"
                "如确需继续，先由用户确认后 `copilot_dispatch.py closure "
                f"--state {args.dispatch_state} --reopen --reason <原因>`。")
        dispatch_block = copilot_dispatch.render_state(dispatch_state)

    # 重试默认值：native 自动任务固定 0（只尝试一次）；legacy / 手动通道保留历史
    # 默认 2。显式 --max-retries 始终优先，不会被这里覆盖。
    if args.max_retries is None:
        native_state = (dispatch_state is not None
                        and str(dispatch_state.get("execution_backend") or "legacy")
                        == "native")
        args.max_retries = 0 if native_state else 2
    if args.max_retries < 0:
        die(EXIT_USAGE, "--max-retries 不能为负数。")

    instructions = read_text(args.role_file) if args.role_file else ""
    prompt = build_prompt(prompt, evidence_block, "\n".join(selfchecks), ledger_block,
                          dispatch_block)
    prompt = apply_review_output_contract(mode, prompt)

    redactions = 0
    if not args.no_redact:
        cleaned = redact(prompt)
        redactions = 0 if cleaned == prompt else 1
        prompt = cleaned
        cleaned_instr = redact(instructions)
        instructions = cleaned_instr

    images = load_images(image_refs, args.max_images, args.max_image_bytes)
    if images:
        print("copilot: 内联图片证据 " + str(len(images)) + " 张：" + ", ".join(
            f"{i['id']} {i['mime']} {human_bytes(i['bytes'])} sha256={i['sha256'][:12]}"
            for i in images), file=sys.stderr)

    payload = build_payload(args.model, instructions, prompt, args.max_output_tokens, images,
                            args.reasoning_effort)
    enforce_image_carry(payload, images)
    # 审计记「实际发出的档位」而不是命令行原值：默认值也必须在留痕里看得见。
    effort_sent = str((payload.get("reasoning") or {}).get("effort") or "")

    # 审计只记脱敏元数据：图片数量 / 条目 ID / 指纹，不含路径内容与像素
    image_meta = {
        "images": len(images),
        "image_ids": [i["id"] for i in images],
        "image_sha256": [i["sha256"] for i in images],
    }
    # 一次调用一个 call_id：开始记录与结果记录靠它关联；审计日志里不得出现提示词或像素。
    call_id = os.urandom(8).hex()

    # 审计关联字段：交付物标识与台账路径，供跨台账核对轮次（不含正文、密钥或像素）。
    # 审计里的 subject 以台账实际绑定为准：已有台账允许省略 --subject，
    # 若照抄命令行空值，审计会丢掉交付物锚点，删台账后就无法核对轮次。
    review_subject = ""
    if ledger is not None:
        review_subject = str(ledger.get("subject") or "")
    if not review_subject:
        review_subject = args.subject.strip()
    review_ref = {
        "subject": review_subject,
        # 与台账绑定同一套规范化规则，避免符号链接/相对路径让两边对不上。
        "ledger": normalize_path(args.review_ledger),
    }
    # 任务级凭证字段（task_id / round_id / contract_sha256）：自动协作通道的每次调用
    # 在发送前写入 pending_call 后填充，随后出现在所有审计记录里。手动通道保持为空。
    task_ref: dict = {}

    def aud(record: dict, event: str = "call_result") -> None:
        """写结果记录。写不进去即失败：请求可能已发出，不能当作成功，也不能重试。"""
        payload = {"channel": channel, "event": event, "call_id": call_id,
                   **review_ref, **task_ref, **record, **image_meta}
        try:
            append_audit(args.audit_log, payload)
        except AuditError as exc:
            die(EXIT_AUDIT,
                f"GPT 请求可能已经发出，但审计结果记录写入失败：{exc}。"
                "本轮不构成成功结果，也不得自动重试；请先修复审计日志路径，"
                "并按「审计不完整」报告（调用开始记录可用于追溯本次尝试）。")

    if args.dump_request:
        write_request_dump(args.dump_request, payload)

    if args.dry_run:
        ledger_note = ""
        if merged_meta:
            kind = merged_meta.get("review_kind", "normal")
            if kind == "extra":
                round_note = (f"弹性追加第 {merged_meta.get('extra_index')}"
                              f"/{merged_meta.get('extra_allowance')} 轮")
            elif kind == "security":
                round_note = (f"Security 独立追加第 {merged_meta.get('security_index')}"
                              f"/{merged_meta.get('security_allowance')} 轮")
            else:
                round_note = f"默认预算 {merged_meta['round']}/{merged_meta['max_rounds']} 轮"
            ledger_note = (f"；复核第 {merged_meta['round']} 轮，{round_note}"
                           f"（新增 {merged_meta['new_count']} 条，沿用 {merged_meta['carried_count']} 条）")
        if truncated_new or truncated_carried:
            parts = []
            if truncated_new:
                parts.append(f"本轮 {len(truncated_new)} 条（省略 "
                             f"{sum(t['omitted_chars'] for t in truncated_new)} 字符）")
            if truncated_carried:
                parts.append(f"沿用 {len(truncated_carried)} 条")
            ledger_note += "；证据被截断：" + "，".join(parts)
        print(f"copilot: dry-run（未调用 GPT）：mode={mode or '-'}；channel={channel}；"
              f"model={args.model}；reasoning.effort={effort_sent or '-'}；"
              f"证据包 {len(args.evidence)} 个；"
              f"证据条目 {sum(s['entries'] for s in stats_all)} 条；prompt {len(prompt)} 字符；"
              f"图片 {len(images)} 张{ledger_note}"
              + ("；已注入任务交接状态" if dispatch_state is not None else ""), file=sys.stderr)
        return EXIT_OK

    # 发送前冷却门：在创建 pending 与任何 HTTP 请求之前拒绝，避免重复计费。
    # 只读状态，不写配额、不改轮次/工作项；冷却结束也不自动补发旧请求。
    if dispatch_state is not None and copilot_dispatch.is_quota_blocked(dispatch_state):
        quota = dispatch_state.get("quota") or {}
        reset_at = str(quota.get("reset_at") or "").strip()
        if not reset_at:
            detail = "恢复时间未知（上游未提供可用的 reset_seconds）"
        else:
            try:
                seconds = int((datetime.fromisoformat(reset_at)
                               - datetime.now().astimezone()).total_seconds())
                detail = f"预计恢复时间 {reset_at}（剩余约 {max(seconds, 0)} 秒）"
            except (ValueError, TypeError):
                detail = f"恢复时间无法解析（{reset_at}）"
        die(EXIT_QUOTA,
            f"配额/模型冷却尚未结束：{detail}。本次拒绝调用：未创建 pending_call、"
            "未发送 HTTP 请求、未消耗额度，也未改变工作项与轮次。冷却结束后需由用户"
            "确认再发起；本脚本不会自动补发此前失败的请求。")

    pending = None
    # 辅助复核标记：仅当主任务状态保持 PLAN、本次是带独立台账的 REVIEW 调用时成立。
    # 它决定提交阶段只留痕、不改变主任务判断；恢复流程也按 pending 中的标记走同一路径。
    auxiliary_review = False
    if dispatch_state is not None:
        # 自动协作通道（--dispatch-state）入口硬门：任务凭证、模式、严格 SOL High、
        # 默认来源与标准审计路径任一不满足，都在出站前失败。手动覆盖不得借道自动状态。
        try:
            task = copilot_dispatch.task_credential(dispatch_state)
        except copilot_dispatch.DispatchError as exc:
            die(EXIT_USAGE, str(exc))
        if task is None:
            die(EXIT_USAGE,
                "交接状态没有任务级凭证（task_id / contract_sha256）：旧格式状态只能"
                "人工补录，不能进入自动协作通道。请用 `copilot_dispatch.py start "
                "--contract-file` 重建状态；历史轮次不会被带入新任务。")
        state_mode = str(dispatch_state.get("mode") or "").strip().upper()
        # 严格限定的辅助复核豁免：PLAN 主任务可发起一次 REVIEW 辅助调用，
        # 但必须同时携带独立复核台账（REVIEW 专属门与证据要求已在前面执行）。
        # 其余模式组合一律拒绝；主任务模式保持 PLAN，不因本次调用改变。
        auxiliary_review = (mode == "REVIEW" and state_mode == "PLAN"
                            and bool(args.review_ledger))
        if state_mode and mode != state_mode and not auxiliary_review:
            die(EXIT_USAGE,
                f"调用模式 {mode or '-'} 与交接状态模式 {state_mode} 不一致；"
                "同一任务不得跨模式共用状态。唯一例外：PLAN 主任务带 "
                "--review-ledger 发起 REVIEW 辅助复核。")
        if model_explicit:
            die(EXIT_USAGE,
                f"自动协作通道禁止 --model 覆盖（本次 {requested_model_arg!r}）。"
                f"请去掉 --model 使用默认 {copilot_dispatch.AUTO_MODEL} + "
                f"{copilot_dispatch.AUTO_REASONING_EFFORT}；手动模型通道只能在"
                "不绑定 --dispatch-state 时使用，其结果不得写入任务状态。")
        if args.model != copilot_dispatch.AUTO_MODEL or model_source != "default":
            die(EXIT_USAGE,
                f"自动协作通道只接受 {copilot_dispatch.AUTO_MODEL}（默认来源），"
                f"本次为 {args.model}（{model_source}）；拒绝执行。")
        if effort_sent != copilot_dispatch.AUTO_REASONING_EFFORT:
            die(EXIT_USAGE,
                f"自动协作通道只接受 reasoning.effort="
                f"{copilot_dispatch.AUTO_REASONING_EFFORT}（本次 {effort_sent or '缺失'}）；"
                "不得降档后写入任务状态。")
        if not audit_path_trusted(args.audit_log):
            die(EXIT_USAGE,
                f"自动协作通道只接受标准审计日志 "
                f"{copilot_dispatch.DEFAULT_AUDIT_LOG}（本次 {args.audit_log}）。"
                f"测试请设置 {AUDIT_TEST_MODE_ENV}=1；"
                "任意外部审计文件不得当作正式任务凭证。")
        # 出站前预算门：按审计重建本任务已用请求数/Token。请求数是硬上限；
        # Token 阈值只在响应后才知道实际用量，最终可能超出一次调用，另用保守
        # 预估提前拦截。拒绝时尚未创建 pending、尚未发请求、尚未递增轮次。
        try:
            budget = copilot_dispatch.budget_status(dispatch_state, args.audit_log)
        except copilot_dispatch.DispatchError as exc:
            die(EXIT_USAGE, f"无法核算任务预算（请求未发出）：{exc}")
        if not budget["allowed"]:
            limits = budget["limits"]
            spend = budget["spend"]
            die(EXIT_BUDGET,
                f"预算拒绝出站（{budget['reason']}）：{budget['detail']} "
                f"已用请求 {spend['requests']}/{limits['max_requests'] or '-'}，"
                f"已上报 Token {spend['total_tokens']}/"
                f"{limits['max_total_tokens'] or '-'}。"
                "本次拒绝发生在发送前：未创建 pending_call、未发 HTTP 请求、"
                "未消耗额度，也未递增轮次。")
        # 并发比较交换的基线：prepare_pending 会把它加一，落盘时要求磁盘仍是旧值。
        round_seq_before = dispatch_state.get("round_seq", 0)
        try:
            pending = copilot_dispatch.prepare_pending(
                dispatch_state, call_id=call_id, mode=mode,
                model_requested=args.model, reasoning_effort=effort_sent,
                model_source=model_source,
                auxiliary_review=auxiliary_review)
        except copilot_dispatch.DispatchError as exc:
            die(EXIT_USAGE, str(exc))
        task_ref.update({
            "task_id": pending["task_id"],
            "round_id": pending["round_id"],
            "contract_sha256": pending["contract_sha256"],
        })
        # 调用意图先落盘：进程在请求中途被杀也能按 task_id/round_id/call_id 精确恢复，
        # 而不是靠「审计里最后一条成功调用」推断归属。
        # expect=round_seq 是并发防护：两个进程共用同一状态时，只有先落盘者能拿到本轮，
        # 另一个在发送请求前失败（否则会双计费并互相覆盖轮次）。
        try:
            copilot_dispatch.save_state(args.dispatch_state, dispatch_state,
                                        expect={"round_seq": round_seq_before})
        except (OSError, copilot_dispatch.DispatchError) as exc:
            die(EXIT_USAGE,
                f"调用前无法持久化任务调用意图（pending_call）：{exc}；请求未发出。"
                "先修复交接状态路径/权限或处理并发调用再重试。")

    def release_pending(reason: str) -> bool:
        """已知没有可用回复的失败路径：清空 pending，避免阻塞下一轮。

        返回是否确实清理并落盘；调用方据此给出准确提示。
        """
        if dispatch_state is None or not task_ref:
            return False
        if not copilot_dispatch.release_pending(dispatch_state, call_id, reason):
            return False
        try:
            copilot_dispatch.save_state(args.dispatch_state, dispatch_state,
                                        expect={"pending_call_id": call_id})
        except (OSError, copilot_dispatch.DispatchError) as exc:
            print(f"copilot: 警告：清理 pending_call 失败（{exc}）；"
                  "下一次调用前需要先 recover。", file=sys.stderr)
            return False
        return True

    api_key = resolve_api_key(API_KEY_ENV)
    if not api_key:
        # 请求尚未发出：释放本轮 pending，避免下一次调用被误判为「有未结算调用」。
        release_pending("no_credentials")
        die(EXIT_USAGE, f"缺少凭据：环境变量 {API_KEY_ENV}、~/.reasonix/.env 与 "
                    "ZCode provider 配置中均未找到。")

    # 先持久化「调用开始」记录（fsync 成功）再出站：没有开始记录就不发请求。
    start_record = {
        "ts": now_iso(), "event": "call_requested", "call_id": call_id,
        "mode": mode, "model": args.model, "model_requested": args.model,
        "model_source": model_source,
        "reasoning_effort": effort_sent,
        "tools_sent": False, "tool_choice": "none",
        **review_ref, **task_ref,
        "max_attempts": args.max_retries + 1,
        "evidence_packs": len(args.evidence),
        "evidence_entries": sum(s["entries"] for s in stats_all),
        "evidence_images": sum(len(s["images"]) for s in stats_all),
        "prompt_redacted": bool(redactions),
        **image_meta,
    }
    if merged_meta:
        # 只记轮次元数据；裁决在结果记录里，开始记录不带结论。
        start_record.update(_review_round_meta(merged_meta))
    try:
        append_audit(args.audit_log, {"channel": channel, **start_record})
    except AuditError as exc:
        release_pending("audit_unavailable")  # 请求未发出，释放 pending
        die(EXIT_USAGE, f"审计开始记录写入失败，GPT 请求未发出：{exc}")

    url = args.base_url.rstrip("/") + ENDPOINT
    attempts, last_error, last_status = 0, "", 0
    last_elapsed_ms = None
    last_quota = None
    result_unknown = False
    http_response_received = False

    for attempt in range(args.max_retries + 1):
        attempts = attempt + 1
        result_unknown = False
        try:
            started = time.monotonic()
            status, data, raw = post_json(url, api_key, payload, args.timeout)
        except TimeoutError:
            last_elapsed_ms = int((time.monotonic() - started) * 1000)
            last_error, last_status = "timeout", 0
            result_unknown = True
        except urllib.error.URLError as exc:
            last_elapsed_ms = int((time.monotonic() - started) * 1000)
            result_unknown = request_result_unknown(exc)
            label = "network(result unknown)" if result_unknown else "network"
            last_error, last_status = f"{label}: {redact(str(exc))}", 0
        except OSError as exc:  # URLError 之外的传输层异常（如 RemoteDisconnected）
            last_elapsed_ms = int((time.monotonic() - started) * 1000)
            last_error, last_status = f"network(result unknown): {redact(str(exc))}", 0
            result_unknown = True
        except http.client.HTTPException as exc:
            last_elapsed_ms = int((time.monotonic() - started) * 1000)
            # 响应读到一半中断（IncompleteRead 等）：请求已发出，结果未知，不得重发。
            last_error, last_status = f"response read failed: {redact(str(exc))}", 0
            result_unknown = True
        else:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            last_elapsed_ms = elapsed_ms
            last_status = status
            if status == 200:
                try:
                    tool_calls = find_tool_calls(data)
                    problem = response_problem(data)
                    text = extract_text(data) if not problem else ""
                    verdict = parse_verdict(text) if (mode == "REVIEW" and text) else None
                except (AttributeError, TypeError, ValueError, KeyError) as exc:
                    # 畸形结构不得绕过结果审计：先记失败，再明确退出。
                    aud({
                        "ts": now_iso(), "mode": mode, "model": args.model,
                        "tools_sent": False, "tool_choice": "none", "attempts": attempts,
                        "ok": False, "error": "malformed_response",
                        "detail": f"{type(exc).__name__}: {exc}",
                        "response_id": data.get("id") if isinstance(data, dict) else None,
                    })
                    release_pending("malformed_response")
                    die(EXIT_SHAPE, f"上游响应结构异常（{type(exc).__name__}），无法安全解析；"
                                    "未产出结果。")
                if tool_calls:
                    aud({
                        "ts": now_iso(), "mode": mode, "model": args.model, "tools_sent": False,
                        "tool_choice": "none", "attempts": attempts, "ok": False,
                        "security_event": "unexpected_output_item", "detail": tool_calls,
                    })
                    release_pending("unexpected_output_item")
                    die(EXIT_TOOLCALL,
                        f"安全事件：上游返回了非良性 output 条目 {tool_calls}；已拒绝处理且未执行，"
                        "请检查链路是否被改动。")

                if problem:
                    aud({
                        "ts": now_iso(), "mode": mode, "model": args.model, "tools_sent": False,
                        "tool_choice": "none", "attempts": attempts, "ok": False,
                        "error": "non_success_response", "detail": problem,
                        "response_id": data.get("id"),
                    })
                    release_pending("non_success_response")
                    die(EXIT_SHAPE, f"上游响应非完整成功（{redact(problem)}），"
                                    "不构成可交付裁决；未产出结果。")

                if not text:
                    aud({
                        "ts": now_iso(), "mode": mode, "model": args.model, "tools_sent": False,
                        "tool_choice": "none", "attempts": attempts, "ok": False,
                        "error": "empty_output", "response_id": data.get("id"),
                    })
                    release_pending("empty_output")
                    die(EXIT_SHAPE, "上游未返回可用的 message 文本。")

                if mode == "REVIEW" and verdict is None:
                    print("copilot: 警告：REVIEW 回复没有可机读的唯一裁决行"
                          "（`裁决：` / `最终裁决：` / `整体结论：` 均可）；"
                          "按无裁决处理，不得当作 PASS。", file=sys.stderr)

                review_meta = {}
                if merged_meta:
                    review_meta = {
                        **_review_round_meta(merged_meta),
                        "review_new_items": merged_meta["new_count"],
                        "review_verdict": verdict or "UNPARSED",
                    }

                # 模型身份门（安全边界）：请求名与上游回显名必须完全一致。
                # 不一致 / 缺失 / 不可解析时，该回复不构成任何可用结果：
                # 不输出正文、不写 --out、不入复核台账、不写交接状态，退出码 9。
                # 不重试、不换模型、不降档——换模型重发本身就是被禁止的自动降级。
                model_resolved = data.get("model")
                model_identity_ok = (isinstance(model_resolved, str)
                                     and model_resolved.strip() == args.model)
                if not model_identity_ok:
                    if model_resolved is None:
                        identity_reason = "missing"
                    elif not isinstance(model_resolved, str):
                        identity_reason = "not_a_string"
                    else:
                        identity_reason = "mismatch"
                    reply_sha = sha256_text(text)
                    aud({
                        "ts": now_iso(), "mode": mode, "model": args.model,
                        "model_requested": args.model, "model_resolved": model_resolved,
                        "model_identity_ok": False, "identity_reason": identity_reason,
                        "model_source": model_source,
                        "reply_sha256": reply_sha, "reply_chars": len(text),
                        "exit_code": EXIT_IDENTITY,
                        "reasoning_effort": effort_sent, "elapsed_ms": elapsed_ms,
                        "tools_sent": False, "tool_choice": "none",
                        "attempts": attempts, "ok": False,
                        "error": "model_identity_mismatch", "http_status": status,
                        "response_id": data.get("id"),
                        # 身份校验失败也消耗了真实调用：记下 usage，避免预算少算。
                        "usage": (data.get("usage")
                                  if isinstance(data.get("usage"), dict) else None),
                    })
                    # 刻意不改写 last_call：失败调用不是本轮结果，覆盖 last_call 会让
                    # 上一轮「已取得但尚未 ingest」的有效回复永久无法补录。只清空本次
                    # 调用自己的 pending（该调用没有可用回复），不动已提交轮次。
                    release_pending("model_identity_mismatch")
                    if args.out:
                        print(f"copilot: 未写入 {args.out}；该路径若已存在文件，"
                              "是上一轮遗留内容，不得当作本轮结果。", file=sys.stderr)
                    die(EXIT_IDENTITY,
                        f"模型身份验证失败：请求模型 {args.model!r}，上游回显 "
                        f"{model_resolved!r}（{identity_reason}）。本次回复已丢弃："
                        "未输出正文、未写 --out、未入台账、未写交接状态；"
                        "未重试、未换模型、未降档。请检查 CPA 别名或上游路由后，"
                        "由人工决定是否重新发起。")

                aud({
                    "ts": now_iso(), "mode": mode, "model": args.model,
                    "model_requested": args.model, "model_resolved": model_resolved,
                    "model_identity_ok": True, "model_source": model_source,
                    "reply_sha256": sha256_text(text), "reply_chars": len(text),
                    "reasoning_effort": effort_sent, "elapsed_ms": elapsed_ms,
                    "tools_sent": False, "tool_choice": "none",
                    "attempts": attempts, "ok": True, "http_status": status,
                    "response_id": data.get("id"), "usage": data.get("usage"),
                    "evidence_packs": len(args.evidence),
                    "evidence_entries": sum(s["entries"] for s in stats_all),
                    "evidence_images": sum(len(s["images"]) for s in stats_all),
                    "prompt_redacted": bool(redactions),
                    **review_meta,
                })
                # 成功响应已写入审计：按审计重算累计 usage，随后的 save_state
                # 把它持久化。审计是权威来源；这里失败只警告，不改变本轮结果。
                if dispatch_state is not None:
                    try:
                        copilot_dispatch.refresh_budget_usage(
                            dispatch_state, args.audit_log)
                    except copilot_dispatch.DispatchError as exc:
                        print(f"copilot: 警告：预算用量刷新失败（{exc}）；"
                              "审计仍是权威来源，下一次请求前会重新核算。",
                              file=sys.stderr)
                # 回复先给出：之后无论输出落盘还是台账落盘失败，裁决都不会丢。
                print(text)

                # 先入账，再写输出文件：GPT 已返回的复核不能因为 --out 失败而漏记轮次。
                if ledger is not None and merged_meta is not None:
                    try:
                        record_round(
                            ledger, args.review_ledger, merged_meta, verdict, text,
                            hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                            call_id=call_id,
                            identity={
                                "model_requested": args.model,
                                "model_resolved": model_resolved,
                                "model_source": model_source,
                                "model_identity_ok": True,
                            },
                            attribution=task_ref or None,
                        )
                    except (LedgerError, OSError) as exc:
                        aud({
                            "ts": now_iso(), "mode": mode, "model": args.model,
                            "tools_sent": False, "tool_choice": "none", "attempts": attempts,
                            "ok": False, "error": "ledger_write_failed", "detail": str(exc),
                            "response_id": data.get("id"), **review_meta,
                        }, "call_post_failure")
                        die(EXIT_USAGE,
                            f"已获得回复（见上方输出），但复核台账写入失败：{exc}。"
                            f"本轮（第 {merged_meta['round']} 轮）尚未入账，磁盘上也没有半份记录；"
                            "这次调用已记入审计，后续复核会按已用轮次计入，不要靠重试重置轮次。"
                            "先修复台账路径/权限，再继续。")

                if args.out:
                    try:
                        with open(args.out, "w", encoding="utf-8") as fh:
                            fh.write(text + "\n")
                    except OSError as exc:
                        # 轮次已经入账；这里明确报告输出失败，不让调用方误以为完整交付。
                        aud({
                            "ts": now_iso(), "mode": mode, "model": args.model,
                            "tools_sent": False, "tool_choice": "none", "attempts": attempts,
                            "ok": False, "error": "reply_write_failed", "detail": str(exc),
                            "response_id": data.get("id"), **review_meta,
                        }, "call_post_failure")
                        round_note = (f"第 {merged_meta['round']} 轮已计入台账；"
                                      if merged_meta else "")
                        die(EXIT_USAGE,
                            f"已获得回复，{round_note}但回复文件写入失败：{exc}。"
                            "回复已打印到 stdout，请从终端输出保存；"
                            "请勿重复调用 GPT（重试会计入轮次，同一组证据也会被拒绝）。")
                if dispatch_state is not None and pending is not None:
                    # 任务级来源凭证：11 个字段一起证明「哪个任务 / 哪一轮 / 哪次调用 /
                    # 哪个模型 / 哪档 effort / 哪份回复」。ingest 逐项核对，任一不符即拒绝。
                    receipt = {
                        "task_id": pending["task_id"],
                        "round_id": pending["round_id"],
                        "contract_sha256": pending["contract_sha256"],
                        "call_id": call_id,
                        "mode": mode,
                        "reply_sha256": sha256_text(text),
                        "model_requested": args.model,
                        "model_resolved": model_resolved,
                        "model_identity_ok": True,
                        "reasoning_effort": effort_sent,
                        "model_source": model_source,
                    }
                    dispatch_script = os.path.join(HERE, "scripts", "copilot_dispatch.py")
                    reply_ref = shlex.quote(args.out) if args.out else "-"
                    where = (f"回复已写入 {args.out}" if args.out
                             else "回复仅在 stdout（本轮未传 --out）")
                    stdin_hint = ("" if args.out else
                                  "（回复已打印到 stdout；先保存成文件，"
                                  "或用 --reply-file - 从管道读取）")
                    try:
                        outcome = copilot_dispatch.commit_reply(
                            dispatch_state, text, mode, receipt=receipt,
                            review_only=auxiliary_review)
                    except copilot_dispatch.DispatchError as exc:
                        # 归属/凭证校验失败：状态未被改动（不消费凭证，也不写轮次）。
                        # 解析/安排校验失败：轮次留痕已写入且凭证已消费（不得重放）。
                        consumed = any(
                            str(r.get("call_id") or "") == call_id
                            for r in dispatch_state.get("rounds") or []
                            if isinstance(r, dict))
                        saved = consumed
                        if consumed:
                            try:
                                copilot_dispatch.save_state(
                                    args.dispatch_state, dispatch_state,
                                    expect={"pending_call_id": call_id})
                            except (OSError, copilot_dispatch.DispatchError) as save_exc:
                                saved = False
                                print(f"copilot: 警告：交接状态落盘失败（{save_exc}）；"
                                      "本轮轮次记录未能保存。", file=sys.stderr)
                        aud({
                            "ts": now_iso(), "mode": mode, "model": args.model,
                            "tools_sent": False, "tool_choice": "none", "attempts": attempts,
                            "ok": False,
                            "error": ("dispatch_plan_rejected" if consumed
                                      else "dispatch_credential_rejected"),
                            "detail": str(exc),
                            "response_id": data.get("id"), **review_meta,
                        }, "call_post_failure")
                        if consumed and saved:
                            headline = ("本轮安排未生效（轮次已保存，工作项未改动；"
                                        "该凭证已消费，不得重放）")
                        elif consumed:
                            headline = "本轮轮次留痕写入失败"
                        else:
                            headline = "任务凭证/归属校验失败（状态未改动）"
                        die(EXIT_USAGE,
                            f"已获得回复，但{headline}：{exc}。{where}。"
                            + ("" if consumed else
                               "该回复不能用于本任务状态；请人工核对任务/轮次/合同绑定后，"
                               "重新发起调用。"))
                    # 轮次、工作项、last_call 与 pending 清理一次原子提交；失败时磁盘上
                    # 仍是调用前的 pending，可按 call_id/round_id 精确恢复。
                    # expect 校验「挂起的仍是本次调用」，防止覆盖外部修改过的状态。
                    try:
                        copilot_dispatch.save_state(
                            args.dispatch_state, dispatch_state,
                            expect={"pending_call_id": call_id,
                                    "round_seq": pending["round_seq"]})
                    except (OSError, copilot_dispatch.DispatchError) as exc:
                        aud({
                            "ts": now_iso(), "mode": mode, "model": args.model,
                            "tools_sent": False, "tool_choice": "none", "attempts": attempts,
                            "ok": False, "error": "dispatch_commit_failed", "detail": str(exc),
                            "response_id": data.get("id"), **review_meta,
                        }, "call_post_failure")
                        die(EXIT_USAGE,
                            f"已获得回复，但任务交接状态提交失败：{exc}。"
                            f"pending_call 已保留，回复不会丢失（{where}）。修正路径/权限后执行 "
                            f"`python3 {shlex.quote(dispatch_script)} recover "
                            f"--state {shlex.quote(args.dispatch_state)} "
                            f"--reply-file {reply_ref} "
                            f"--call-id {call_id} "
                            f"--audit-log {shlex.quote(args.audit_log)}`"
                            f"{stdin_hint}；不要重新调用 GPT。")
                    # 成功回复已入账后解除配额等待。清除失败不得影响已取得的回复，
                    # 也不得触发任何重发：只留告警与可恢复的 waiting 状态。
                    if str((dispatch_state.get("quota") or {}).get("state") or "ok") == "waiting":
                        try:
                            copilot_dispatch.clear_quota(
                                args.dispatch_state,
                                f"调用 {call_id} 成功且通过模型身份验证",
                                source="call", call_id=call_id)
                        except (copilot_dispatch.DispatchError, OSError) as exc:
                            aud({
                                "ts": now_iso(), "mode": mode, "model": args.model,
                                "tools_sent": False, "tool_choice": "none",
                                "attempts": attempts, "ok": False,
                                "error": "quota_clear_failed", "detail": str(exc),
                                "response_id": data.get("id"), **review_meta,
                            }, "call_post_failure")
                            print("copilot: 警告：回复已提交，但配额等待状态未能清除"
                                  f"（{exc}）。不要重新调用 GPT；先人工核实额度，"
                                  "再执行 `copilot_dispatch.py quota --state ... "
                                  "--clear --reason <依据>`。", file=sys.stderr)
                    if outcome.get("auxiliary_review"):
                        print("copilot: 辅助复核已写入独立复核台账并留痕；"
                              "主任务 verdict、工作项与完成条件未改动。",
                              file=sys.stderr)
                    elif outcome["skipped"]:
                        print("copilot: 提示：回复未包含 ```copilot-plan``` 安排块；"
                              "未创建新工作项（仅有结论时属正常）。", file=sys.stderr)
                    else:
                        print("copilot: 已写回交接状态：decision=" + str(outcome["decision"])
                              + "；新增工作项 "
                              + (", ".join(a["id"] for a in outcome["added"]) or "无")
                              + "；更新工作项 "
                              + (", ".join(a["id"] for a in outcome["updated"]) or "无"),
                              file=sys.stderr)

                return EXIT_OK

            last_error = redact(raw[:500] or f"HTTP {status}")
            last_quota = quota_signal(status, data)
            # 已收到 HTTP 错误响应，但无法据此判断模型服务是否处理请求或产生费用：
            # 不得自动重发同一付费 POST；保留原始状态码与输出留痕后立即失败。
            http_response_received = True
            break

        if result_unknown:
            # 请求结果未知：可能已被上游接收并计费，不进入下一轮自动重发。
            break
        if attempt < args.max_retries:
            time.sleep(min(2 ** attempt, 8))

    aud({
        "ts": now_iso(), "mode": mode, "model": args.model, "model_requested": args.model,
        "model_source": model_source,
        "reasoning_effort": effort_sent, "elapsed_ms": last_elapsed_ms,
        "tools_sent": False,
        "tool_choice": "none", "attempts": attempts, "ok": False,
        "http_status": last_status, "error": last_error[:300],
        **({"quota": last_quota} if last_quota else {}),
    })
    if last_quota:
        reset = last_quota["reset_seconds"]
        reset_text = "unknown" if reset is None else str(reset)
        quota_note = ""
        if dispatch_state is not None:
            try:
                copilot_dispatch.record_quota(args.dispatch_state, last_quota, call_id)
            except (copilot_dispatch.DispatchError, OSError) as exc:
                # 配额状态写不进去：保留 pending_call，阻止下一条自动调用；
                # 不把「无法记录」当成「额度已恢复」，也不自动重试。
                die(EXIT_QUOTA,
                    f"配额或冷却限制（{last_quota['code']}，HTTP "
                    f"{last_quota['status']}），但配额状态写入失败：{exc}。"
                    "pending_call 已保留以阻止重复调用；请先修复状态文件，"
                    "再按 recover 处理本轮（不要重新调用 GPT）。")
            # record_quota 在锁内基于磁盘当前内容只 patch quota；必须重新加载，
            # 否则随后 release_pending 的整文件写回会用它进程的旧快照覆盖刚记录的配额。
            try:
                dispatch_state = copilot_dispatch.load_state(args.dispatch_state)
            except (copilot_dispatch.DispatchError, OSError) as exc:
                quota_note = (f"；配额已记录，但重新加载状态失败（{exc}），"
                              "pending_call 仍保留，需先 recover")
            else:
                released = release_pending(f"quota:{last_quota['code']}")
                quota_note = ("；已写入任务配额状态并清理本轮 pending"
                              if released else
                              "；已写入任务配额状态（pending 未匹配，未改动）")
        die(EXIT_QUOTA,
            f"配额或冷却限制（{last_quota['code']}，HTTP {last_quota['status']}）："
            f"reset_seconds={reset_text}{quota_note}。未自动重试、未换模型、未降档；"
            "额度恢复后需人工确认再发起（`/v1/models` 能列出模型并不证明额度已恢复）。"
            f"最后错误：{last_error}")
    code = EXIT_TIMEOUT if last_error == "timeout" else EXIT_UPSTREAM
    if result_unknown:
        # 结果未知：保留 pending（可能已计费/回复在途），必须人工 recover 确认后才能再发起。
        if task_ref:
            print("copilot: 注意：本次调用结果未知，pending_call 已保留；"
                  "再次发起前先用 `copilot_dispatch.py recover` 确认（或 --abandon 放弃）。",
                  file=sys.stderr)
        die(code, f"调用未成功且请求结果未知（尝试 {attempts} 次，最后错误：{last_error}）。"
                  "请求可能已被上游接收并处理，本脚本未自动重发；未获得 GPT 结果，"
                  "不得当作成功，也不要未经确认自动重试。")
    if http_response_received:
        release_pending("http_error")
        die(code, f"调用未成功（HTTP {last_status}，尝试 {attempts} 次，最后错误：{last_error}）。"
                  "已收到 HTTP 错误响应，但无法据此判断模型服务是否处理请求或产生费用；"
                  "本脚本未自动重发。未获得 GPT 结果，不得当作成功，"
                  "如需重试请人工确认后再发起。")
    release_pending("upstream_error")
    die(code, f"调用未成功（尝试 {attempts} 次，最后错误：{last_error}）。未获得 GPT 结果。")


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""copilot — 现场任务交接：把 copilot 的判断落成主代理可执行、可回填的工作项。

定位（不要重复建设调度系统）：

* copilot 仍然是**零工具**判断方：它不读文件、不执行命令、不派发子代理。
* 真正的执行由 ZCode 主代理承接：主代理直接做，或用原生 Agent 子代理
  把具体工作派给子代理（不得派给 GPT）。
* 本模块只做两件原本缺失的事：
  1. **交接**：把 copilot 回复里的结构化安排（```copilot-plan``` 块）解析成工作项；
  2. **反馈**：记录每项的执行状态、产出与证据条目，让下一轮 copilot 看到真实反馈，
     而不是靠对话记忆或用户转述。

工作项不是授权：超出用户授权或属于不可逆/生产/付费的动作，主代理仍须先取得授权。
本模块不执行任何任务，也不改变 GPT 的工具边界。

**来源凭证**：交接状态只接受经过 `call_gpt.py` 验证的回复。写入前必须提供
`receipt`（call_id / 回复摘要 / 请求与回显模型 / 模型来源 / 身份结论），同一 call_id
不得重复入账。任务级状态（有 `task_id`）只认本轮 `pending_call`：任务、轮次、合同指纹、
模式、模型档位任一不符即拒绝，**不再用「审计日志最后一条成功调用」推断归属**。
旧格式状态（P0 之前、无任务凭证）保留人工兼容：有 `last_call` 时与它一致，没有时必须是
审计里最后一条成功 `call_result`；这条旧路径不得进入自动协作。CLI 的 `ingest` 从审计日志
重建凭证：找不到对应成功调用、身份未通过、摘要不符或归属不符时一律拒绝——不能仅凭文件
存在或正文格式正确就接受回复。

**任务级凭证（2026-11-01 收尾）**：新建状态一律绑定 `task_id` 与 `contract_sha256`
（目标/完成条件/边界规范化摘要）。自动协作通道的每次调用在发送前写入 `pending_call`
（task_id / round_id / call_id / 合同指纹 / 模式 / 请求模型 / effort / 模型来源），
成功回复的凭证携带同一组字段；`ingest` 必须逐项核对任务、轮次、合同、模式、模型身份
与回复摘要——任一不符即拒绝。没有任务归属的历史凭证只能走旧格式人工补录，
不得进入任务级自动协作；同一凭证（call_id / round_id）不得重复消费。
任务级自动协作必须声明模式（`start --mode`）；状态创建时为空则由首次调用钉住。

**恢复**：调用前先持久化 `pending_call`；成功后在**一次**原子写入里提交轮次、`last_call`
并清空 pending。提交写盘失败时 pending 保留，可用 `recover --reply-file` 依据
task_id / round_id / 审计凭证精确补录；已提交的轮次再次恢复是幂等的，不会重复入账。
结果未知（超时 / 网络中断 / 审计无结果）与「审计成功但回复已找不回」都不自动清理，
必须 `--abandon` 人工确认。

状态文件是本地 JSON；写入使用旁路锁 + 临时文件 + fsync + 原子替换，避免半写坏状态；
`save_state(expect=...)` 在同一把锁内做比较交换（`round_seq` / 挂起 `call_id`），
防止两个并发自动调用共用轮次号或互相覆盖。摘要只证明关联与完整性，不是防伪签名；
能改写本机文件的用户仍在信任边界内（见 lib_common）。

`execution_backend` 默认 `legacy`；`native` 为显式 opt-in，额外保存
`native_dispatch` / `native_results` / `native_reviews` / `review_channel` /
`review_round` / `review_consumed`。旧状态升级时补齐空值，不改变 legacy 行为。
执行报告（`kind=execution`）与复核报告（`kind=review`）必须分开登记，复核报告绑定
`review_of` 且 `g4_eligible=false`；普通原生复核不改变 G1–G4。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from review_ledger import parse_verdict  # noqa: E402
from lib_common import (  # noqa: E402
    AUDIT_TEST_MODE_ENV, STANDARD_AUDIT_LOG, audit_path_trusted,
    is_standard_audit_path, normalize_block, now_iso, sha256_file, sha256_text,
)
from reply_protocol import (  # noqa: E402
    NORMALIZER_VERSION, ProtocolError, normalize_plan,
)

# 状态版本 2（阶段 2）：在 v1 基础上增量加入合同/完成条件、业务决策、执行报告、
# 证据登记、预算、配额与收口。v1 仍可加载（无损升级、幂等），写入一律成 v2。
STATE_VERSION = 2
SUPPORTED_STATE_VERSIONS = (1, 2)
RISKS = ("normal", "high")
# 执行后端：legacy 保持原有「主代理直接执行 + manifest 回填」；native 才启用
# Agent Bridge（dispatch 信封 / --native-result）。默认 legacy，旧状态迁移也默认
# legacy，不能因为新增能力改变既有任务的行为。
EXECUTION_BACKENDS = ("legacy", "native")
# 复核通道只用于区分“原生 Reviewer”与历史 GPT REVIEW 台账。两者不能在同一轮
# 同时拥有复核权威；原生结果回填遇到 gpt_review 时失败关闭。
REVIEW_CHANNELS = (None, "native", "gpt_review")
VERDICTS = ("pass", "partial", "blocked")
# 复核对象类型：native 保持原有 execution_result_sha256 语义；legacy 明确区分
# 「执行报告摘要」与「已登记证据摘要」，不把两者混成同一个 review_of。
REVIEW_OF_KINDS = ("execution-report", "evidence")
NATIVE_REVIEW_OF_KIND = "native-execution-result"
REVIEW_SOURCES = ("native", "legacy")
VERDICT_DECISION = {"pass": "stop", "partial": "continue", "blocked": "blocked"}
DECISION_VERDICT = {"stop": "pass", "continue": "partial", "blocked": "blocked"}
CRITERION_ID_RE = re.compile(r"^C[1-9][0-9]{0,2}$")
EVIDENCE_ID_RE = re.compile(r"^E[1-9][0-9]{0,3}$")
CRITERION_STATUSES = ("pending", "met", "not_met")
# 自动协作通道的唯一契约：默认 SOL + 显式 high + 非手动覆盖。与 call_gpt.py 的
# DEFAULT_MODEL / DEFAULT_REASONING_EFFORT 必须一致（测试会断言，防止漂移）。
AUTO_MODEL = "gpt-6.1-sol"
AUTO_REASONING_EFFORT = "high"
# `reasonix` 仅兼容迁移前的历史 copilot-plan 块；新安排默认 `zcode`（主代理直接做）
# 或 `deepseek`（经宿主子代理派发）。GPT 不是合法 assignee。
ASSIGNEES = ("zcode", "deepseek", "reasonix")
STATUSES = ("pending", "running", "done", "failed", "blocked", "dropped")
# 终结状态：不可再变更，也不可复用 id。failed / blocked 不是终结——
# 它们需要主代理重试或在阻塞解除后继续，因此仍可被更新与回填。
TERMINAL = {"done", "dropped"}
PLAN_MARKER = "copilot-plan"
# ```copilot-plan ... ``` 或 ~~~copilot-plan ... ~~~
# 安排块只在**顶层**生效：围栏内的内容是示例/数据，不能当成指令。
PLAN_OPEN_RE = re.compile(r"^(`{3,}|~{3,})[ \t]*copilot-plan[ \t]*$", re.I)
BROKEN_PLAN_RE = re.compile(r"^[ \t]*(?:`{3,}|~{3,})[ \t]*copilot-plan(?![\w-])", re.M | re.I)
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
MAX_ACTIONS = 24
MAX_RESULT_CHARS = 4000  # 注入 prompt 的单条执行反馈上限；超出截断并显式标注
# 往轮 GPT 判断的回灌上限：多阶段协作要求后续调用能恢复「前序判断」，
# 但历史判断不能挤占本轮证据与工作项的空间。
MAX_ROUNDS_INJECTED = 6
MAX_ROUND_SUMMARY_CHARS = 1200
# 阶段 2 注入预算：总字符上限 + 逐级裁剪的参数（见 render_state）。
MAX_RENDER_CHARS = 20000
MAX_ACTION_RESULT_INJECTED = 600
MAX_DECISIONS_INJECTED = 8
MAX_DECISION_CHARS = 300
MAX_EVIDENCE_INDEXED = 20
MAX_TEXT_SECTION_CHARS = 1500
CHECKPOINT_EVERY = 8
# 2026-11-08：请求数 / 累计 Token **不再有代码默认上限**。
# - 默认值全部为 None：不因调用次数或累计费用阻止正常调用；用量仍从审计重建并记录。
# - 只有用户在 `start --max-requests/--max-total-tokens` 显式给出时才保留该上限，
#   并明确标注为「显式运营上限」，不是默认阈值。
# - 历史状态里显式写入的数值继续生效（不追溯删除用户设定），代码默认值不再补 6/500k。
NATIVE_DEFAULT_MAX_REQUESTS = None
NATIVE_DEFAULT_MAX_TOTAL_TOKENS = None
NATIVE_BUDGET_PROJECTION = False
# 默认审计日志：与 call_gpt.py 使用同一个文件，CLI 的 ingest 从这里重建调用凭证。
DEFAULT_AUDIT_LOG = STANDARD_AUDIT_LOG


def _default_contract() -> dict:
    return {"done_criteria_text": "", "boundaries": "", "criteria": [],
            "criteria_sha256": "", "criteria_state": "draft",
            "confirmed_round_id": None, "captured": None, "history": []}


def _default_budget() -> dict:
    # last_progress_parts 是内部辅助：保存六维各自的摘要，用于在提示里指出
    # 「哪些维度没有变化」；对外契约仍以 last_progress_digest 为准。
    # usage 是审计重建结果的本地缓存（审计才是权威来源），仅用于展示与延续；
    # 每次请求前后都会按审计重算，进程重启不会让累计值归零。
    return {"rounds_limit": None, "checkpoint_every": CHECKPOINT_EVERY,
            "max_requests": None, "max_total_tokens": None,
            "projection": None,
            "usage": {"requests": 0, "input_tokens": 0, "output_tokens": 0,
                      "total_tokens": 0, "unknown_usage_calls": 0,
                      "known_usage_calls": 0, "failed_calls": 0,
                      "failed_with_usage_calls": 0,
                      "failed_unknown_usage_calls": 0,
                      "not_delivered_calls": 0,
                      "last_call_total": 0, "last_call_id": "",
                      "updated_at": None},
            "last_prompt_sha256": None, "last_progress_digest": None,
            "last_progress_parts": {}, "progress_streak": 0,
            "streak_round_seq": None}


def _default_quota() -> dict:
    return {"state": "ok", "detected_at": None, "reset_at": None,
            "reset_seconds": None, "code": None, "last_call_id": None,
            "cleared_at": None, "clear_reason": None}


def _default_closure() -> dict:
    return {"closed": False, "closed_by": None, "verdict": None, "round_id": None,
            "closed_at": None, "gate": {"passed": None, "round_id": None, "reasons": []},
            # 用户 reopen 后旧 PASS 作废：轮次仍在历史里，但不再驱动阶段机/收口。
            "invalidated_pass_rounds": [],
            "reopen_history": []}


class DispatchError(Exception):
    """可预期的交接错误：调用方必须明确报告，不得静默丢弃安排或结果。"""


# ── 加载 / 保存 ─────────────────────────────────────────────────────────────
def _merge_defaults(value: object, defaults: dict, name: str) -> dict:
    """补齐缺失字段（不覆盖已有值）；类型不符时拒绝加载，避免半坏状态被继续使用。"""
    if not isinstance(value, dict):
        raise DispatchError(f"交接状态的 {name} 不是对象（{type(value).__name__}）；拒绝加载。")
    for key, default in defaults.items():
        if key not in value:
            value[key] = json.loads(json.dumps(default))
        elif isinstance(default, dict):
            value[key] = _merge_defaults(value[key], default, f"{name}.{key}")
    return value


def _validate_criteria(criteria: object) -> list[dict]:
    if not isinstance(criteria, list):
        raise DispatchError("完成条件 criteria 必须是数组。")
    seen: set[str] = set()
    for item in criteria:
        if not isinstance(item, dict):
            raise DispatchError("完成条件条目必须是对象。")
        cid = str(item.get("criterion_id") or "").strip()
        if not CRITERION_ID_RE.match(cid):
            raise DispatchError(f"完成条件 id 非法：{cid!r}（应形如 C1）。")
        if cid in seen:
            raise DispatchError(f"完成条件 id 重复：{cid}。")
        seen.add(cid)
        if not str(item.get("description") or "").strip():
            raise DispatchError(f"完成条件 {cid} 缺少 description。")
        if str(item.get("status") or "") not in CRITERION_STATUSES:
            raise DispatchError(
                f"完成条件 {cid} 的 status 非法（{item.get('status')!r}）；"
                f"只能是 {' / '.join(CRITERION_STATUSES)}。")
        refs = item.get("evidence_refs")
        if not isinstance(refs, list):
            raise DispatchError(f"完成条件 {cid} 的 evidence_refs 必须是数组。")
        for ref in refs:
            if not isinstance(ref, dict) or not EVIDENCE_ID_RE.match(
                    str(ref.get("id") or "").strip()):
                raise DispatchError(
                    f"完成条件 {cid} 的证据引用非法：{ref!r}（应为 {{id: E<n>, sha256}}）。")
    return criteria


def _upgrade_state(state: dict, path: str = "") -> None:
    """把 v1 状态无损升级到 v2（内存中补齐新节，不改既有字段）。"""
    legacy_v1 = state.get("version") == 1
    state.setdefault("actions", [])
    state.setdefault("goal", "")
    state.setdefault("rounds", [])
    state.setdefault("pending_call", None)
    state.setdefault("decisions", [])
    state.setdefault("reports", [])
    state.setdefault("evidence", [])
    # 阶段 3 增量字段：旧状态加载时补 legacy/空值，不改变既有行为；只有在
    # start --execution-backend native 或显式迁移后才启用 Bridge。
    backend = state.setdefault("execution_backend", "legacy")
    if backend not in EXECUTION_BACKENDS:
        raise DispatchError(
            f"交接状态的 execution_backend 非法（{backend!r}）；"
            f"只能是 {' / '.join(EXECUTION_BACKENDS)}。")
    native_dispatch = state.setdefault("native_dispatch", None)
    if native_dispatch is not None and not isinstance(native_dispatch, dict):
        raise DispatchError("交接状态的 native_dispatch 必须是对象或 null。")
    review_channel = state.setdefault("review_channel", None)
    if review_channel not in REVIEW_CHANNELS:
        raise DispatchError(
            f"交接状态的 review_channel 非法（{review_channel!r}）；"
            f"只能是 {', '.join(str(x) for x in REVIEW_CHANNELS)}。")
    review_round = state.setdefault("review_round", None)
    if review_round is not None and not isinstance(review_round, str):
        raise DispatchError("交接状态的 review_round 必须是字符串或 null。")
    for key in ("review_consumed", "native_results", "native_reviews"):
        value = state.setdefault(key, [])
        if not isinstance(value, list):
            raise DispatchError(f"交接状态的 {key} 不是数组；拒绝加载。")
    # 只有 v1 没有 risk 字段（当时无风险分级）。这里按 v1 遗留语义补为 normal；
    # v2 缺 risk 属于状态损坏，保持缺失交给 G4 fail-closed（native_review_missing），
    # 不得在加载层静默降级。
    if legacy_v1:
        state.setdefault("risk", "normal")
    for key in ("decisions", "reports", "evidence", "rounds", "actions"):
        if not isinstance(state.get(key), list):
            raise DispatchError(f"交接状态的 {key} 不是数组；拒绝加载。")
    _merge_defaults(state.setdefault("contract", _default_contract()),
                    _default_contract(), "contract")
    _merge_defaults(state.setdefault("budget", _default_budget()),
                    _default_budget(), "budget")
    _merge_defaults(state.setdefault("quota", _default_quota()),
                    _default_quota(), "quota")
    _merge_defaults(state.setdefault("closure", _default_closure()),
                    _default_closure(), "closure")
    _validate_criteria(state["contract"]["criteria"])
    for entry in state["evidence"]:
        if not isinstance(entry, dict) or not EVIDENCE_ID_RE.match(
                str(entry.get("id") or "").strip()):
            raise DispatchError(f"交接状态的 evidence 条目非法：{entry!r}。")
    seq = state.setdefault("round_seq", 0)
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise DispatchError(f"交接状态 {path} 的 round_seq 无效（{seq!r}）。")
    task_id = state.get("task_id")
    if task_id is not None and not isinstance(task_id, str):
        raise DispatchError(f"交接状态 {path} 的 task_id 无效（不是字符串）。")
    state["version"] = STATE_VERSION


def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        raise DispatchError(
            f"交接状态文件不存在：{path}。请先用 `copilot_dispatch.py start` 创建。") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchError(f"无法读取交接状态 {path}：{exc}") from None
    version = state.get("version") if isinstance(state, dict) else None
    # bool 是 int 的子类（True == 1），且 1.0 == 1；版本号必须是真正的整数，
    # 否则 `true` / `1.0` 会被当成 v1 并走遗留补 normal 路径。
    if (not isinstance(state, dict) or not isinstance(version, int)
            or isinstance(version, bool)
            or version not in SUPPORTED_STATE_VERSIONS):
        raise DispatchError(
            f"交接状态 {path} 版本不受支持（支持 {SUPPORTED_STATE_VERSIONS}，"
            f"实际 {version if isinstance(state, dict) else '非对象'}）。")
    _upgrade_state(state, path)
    return state


def _lock_state_file(fh) -> None:
    """对状态文件的旁路锁文件加独占锁。

    平台没有 fcntl 时退化为无锁：此时「读-比较-写」不再原子，只由调用方的
    expect 比较交换降低风险，不保证并发安全（本机为 Linux，实际走 flock）。
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover — 本机为 Linux
        return
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _unlock_state_file(fh) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover
        return
    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _fsync_dir(path: str) -> None:
    """同步目录项，保证 replace 后的文件名在断电场景下也可见（失败不致命）。"""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _check_expect(path: str, expect: dict) -> None:
    """比较交换：磁盘状态与调用方加载时不一致就拒绝写入（并发自动调用防护）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            current = json.load(fh)
    except FileNotFoundError:
        current = {}
    except (OSError, ValueError) as exc:
        raise DispatchError(f"无法读取交接状态 {path} 做并发校验：{exc}") from None
    if not isinstance(current, dict):
        current = {}
    if "round_seq" in expect:
        actual = current.get("round_seq", 0)
        if actual != expect["round_seq"]:
            raise DispatchError(
                f"交接状态已被另一次调用推进（round_seq {expect['round_seq']} → {actual}）："
                "本轮未获得轮次号，请求不得发出。先确认那一轮的结果再继续。")
    if "pending_call_id" in expect:
        pending = current.get("pending_call")
        actual = str(pending.get("call_id") or "") if isinstance(pending, dict) else ""
        want = str(expect["pending_call_id"] or "")
        if actual != want:
            raise DispatchError(
                f"交接状态的挂起调用已被外部修改（期望 {want or '-'}，实际 {actual or '-'}）："
                "请先用 recover 核对，避免覆盖他人轮次或重复消费凭证。")
    if "state_sha256" in expect:
        # 恢复入口用它证明「加载后到写盘前状态没有被改过」；摘要覆盖整个状态，
        # 比只看 round_seq/pending 更严。旧格式状态升级会改变内容，因此调用方
        # 只在状态加载前后确实一致（version=2）时才传这一项。
        actual = sha256_text(json.dumps(current, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")))
        want = str(expect["state_sha256"] or "")
        if actual != want:
            raise DispatchError(
                "交接状态在加载后被外部修改（state_sha256 不一致）："
                "本次恢复未写入；请重新读取状态并确认没有其他进程/人工改动。")
    if "quota" in expect:
        # 配额补丁只比较 quota 节：它由调用失败/成功路径单独改写，不参与轮次推进，
        # 因此不能要求 round_seq 一致（否则会与在途调用互相拒绝）。
        want = json.dumps(expect["quota"], ensure_ascii=False, sort_keys=True)
        actual = json.dumps(current.get("quota"), ensure_ascii=False, sort_keys=True)
        if want != actual:
            raise DispatchError(
                "交接状态的配额节已被外部修改：本次配额写入未落盘；"
                "请重新读取状态后再记录，避免覆盖他人的配额结论。")


def save_state(path: str, state: dict, expect: dict | None = None) -> None:
    """原子写盘（mkstemp + fsync + os.replace）。

    `expect` 给定时，在同一把文件锁内先做比较交换：`round_seq` 或
    `pending_call_id` 与磁盘不一致即失败，避免两次并发自动调用共用同一轮次号、
    后写覆盖前者已提交的轮次。
    """
    target = os.path.abspath(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    lock_path = os.path.join(directory, "." + os.path.basename(target) + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_fh:
        _lock_state_file(lock_fh)
        try:
            if expect is not None:
                _check_expect(target, expect)
            state["updated"] = now_iso()
            fd, tmp = tempfile.mkstemp(prefix=".copilot-dispatch-", dir=directory, text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
                    fh.write("\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, target)
                _fsync_dir(directory)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        finally:
            _unlock_state_file(lock_fh)


TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _section_text(text: str, title: str) -> str:
    """取 Markdown 二级/三级标题 `title` 下的正文；没有该节返回空串。"""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    active = False
    for line in lines:
        stripped = line.strip()
        if re.match(r"^#{2,3}\s+", stripped):
            if active:
                break
            active = stripped.lstrip("#").strip().startswith(title)
            continue
        if active:
            out.append(line)
    return "\n".join(out).strip()


def _criteria_from_text(contract: str) -> list[str]:
    """从合同的「完成条件」小节解析未编号列表项；不解析则返回空（不猜语义）。"""
    body = _section_text(contract, "完成条件")
    items: list[str] = []
    for line in body.split("\n"):
        match = re.match(r"^\s*[-*]\s+(.*\S)\s*$", line)
        if match:
            items.append(match.group(1))
    return items


def _normalize_criteria(raw: object) -> list[dict]:
    """把「字符串列表 / 对象列表」规范成结构化完成条件；状态一律从 pending 开始。"""
    if not raw:
        return []
    if not isinstance(raw, list):
        raise DispatchError("完成条件必须是数组。")
    out: list[dict] = []
    for index, item in enumerate(raw, 1):
        if isinstance(item, str):
            description = item.strip()
            cid = f"C{index}"
        elif isinstance(item, dict):
            description = str(item.get("description") or "").strip()
            cid = str(item.get("criterion_id") or "").strip() or f"C{index}"
        else:
            raise DispatchError(f"完成条件第 {index} 项既不是字符串也不是对象。")
        if not description:
            raise DispatchError(f"完成条件第 {index} 项缺少描述。")
        if not CRITERION_ID_RE.match(cid):
            raise DispatchError(f"完成条件 id 非法：{cid!r}（应形如 C1）。")
        out.append({"criterion_id": cid, "description": description,
                    "status": "pending", "evidence_refs": [],
                    "updated_round_id": None, "updated_by": None, "note": ""})
    _validate_criteria(out)
    return out


def criteria_digest(criteria: list[dict]) -> str:
    """完成条件集合的摘要：只覆盖 id 与描述，状态变化不改变指纹（防止条件集漂移）。"""
    body = json.dumps(
        [{"criterion_id": c["criterion_id"], "description": c["description"]}
         for c in criteria],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(body)


def _contract_fingerprint(goal: str, contract_text: str,
                          criteria: list[dict]) -> str:
    """有效合同指纹：目标 + 合同正文 + 结构化完成条件（id/描述）。

    只含 id 与描述的完成条件集合也进入指纹：否则「改条件但合同正文不变」不会
    改变合同指纹，旧的 GPT PASS 与旧的复核归属会继续被当作当前合同的结论。
    状态（pending/met）与证据引用不进入指纹——它们随执行进展变化，不是合同内容。
    """
    body = json.dumps({
        "goal": normalize_block(goal),
        "contract": normalize_block(contract_text),
        "criteria": [{"criterion_id": c["criterion_id"],
                      "description": c["description"]}
                     for c in criteria],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(body)


def start(path: str, goal: str, mode: str = "", note: str = "",
          contract: str = "", task_id: str = "", risk: str = "normal",
          rounds_limit: int | None = None,
          criteria: list | None = None,
          execution_backend: str = "legacy",
          max_requests: int | None = None,
          max_total_tokens: int | None = None,
          budget_projection: bool | None = None) -> dict:
    """初始化交接状态。已存在时不覆盖（避免丢弃未回填的工作项）。

    新建状态一律绑定任务级凭证：`task_id`（唯一，进入每轮 round_id）与
    `contract_sha256`（目标 + 合同正文 + 结构化完成条件，规范化后取摘要）。没有
    任务凭证的旧状态仍可加载用于人工补录，但不得进入自动协作通道。合同实质修改
    会重算该指纹，使旧合同下的 PASS 与复核归属自然失效。

    完成条件（阶段 2）：可显式给出（`criteria`），否则从合同 `## 完成条件` 列表项解析；
    两者都没有时保持空集与 `criteria_state=draft`——由主代理起草、GPT 在首轮 PLAN
    审定后才确认并生成 `criteria_sha256`。自动协作不因缺少条件而拒绝启动，但
    PASS 核销（G2）要求条件已确认。
    """
    if os.path.exists(path):
        raise DispatchError(f"交接状态已存在：{path}；续轮请直接使用，勿重建。")
    goal = (goal or "").strip()
    if not goal:
        raise DispatchError("目标为空：交接状态必须绑定本轮目标。")
    task_id = (task_id or "").strip() or "task-" + os.urandom(8).hex()
    if not TASK_ID_RE.match(task_id):
        raise DispatchError(
            f"task_id={task_id!r} 非法：只允许字母、数字、点、下划线、连字符（≤64）。")
    risk_norm = str(risk or "normal").strip().lower()
    if risk_norm not in RISKS:
        raise DispatchError(f"risk={risk!r} 非法；只能是 {' / '.join(RISKS)}。")
    backend = str(execution_backend or "legacy").strip().lower()
    if backend not in EXECUTION_BACKENDS:
        raise DispatchError(
            f"execution_backend={execution_backend!r} 非法；"
            f"只能是 {' / '.join(EXECUTION_BACKENDS)}。")
    limit: int | None = None
    if rounds_limit is not None and rounds_limit != "":
        if isinstance(rounds_limit, bool):
            raise DispatchError("rounds_limit 必须是正整数或留空。")
        try:
            limit = int(rounds_limit)
        except (TypeError, ValueError):
            raise DispatchError(f"rounds_limit={rounds_limit!r} 不是整数。") from None
        if limit <= 0:
            raise DispatchError("rounds_limit 必须是正整数（8 轮检查点不受此限制）。")
    max_req = _budget_int(max_requests, "max_requests")
    max_tok = _budget_int(max_total_tokens, "max_total_tokens")
    # 两种后端默认都不设请求/Token 上限（NATIVE_DEFAULT_* 为 None）：用量只记录进审计，
    # 不阻断正常判断或执行。只有调用方显式给出数值时才是「显式运营上限」。
    projection: bool | None = None
    if backend == "native":
        if max_req is None:
            max_req = NATIVE_DEFAULT_MAX_REQUESTS
        if max_tok is None:
            max_tok = NATIVE_DEFAULT_MAX_TOTAL_TOKENS
        projection = NATIVE_BUDGET_PROJECTION if budget_projection is None else bool(budget_projection)
    elif budget_projection is not None:
        projection = bool(budget_projection)
    raw_criteria = criteria if criteria else _criteria_from_text(contract)
    criteria_items = _normalize_criteria(raw_criteria)
    state = {
        "version": STATE_VERSION,
        "task_id": task_id,
        "contract_sha256": _contract_fingerprint(goal, contract, criteria_items),
        "goal": goal,
        "goal_sha256": sha256_text(goal),
        "mode": (mode or "").strip().upper(),
        "note": (note or "").strip(),
        "risk": risk_norm,
        "execution_backend": backend,
        "round_seq": 0,
        "pending_call": None,
        "created": now_iso(),
        "updated": now_iso(),
        "rounds": [],
        "actions": [],
        "contract": {
            "done_criteria_text": (contract or "").strip(),
            "boundaries": _section_text(contract, "边界"),
            "criteria": criteria_items,
            "criteria_sha256": "",
            "criteria_state": "draft",
            "confirmed_round_id": None,
            "captured": now_iso(),
            "history": [],
        },
        "budget": {**_default_budget(), "rounds_limit": limit,
                   "max_requests": max_req, "max_total_tokens": max_tok,
                   "projection": projection},
        "quota": _default_quota(),
        "closure": _default_closure(),
        "decisions": [],
        "reports": [],
        "evidence": [],
        "native_dispatch": None,
        "native_results": [],
        "native_reviews": [],
        "review_channel": None,
        "review_round": None,
        "review_consumed": [],
    }
    save_state(path, state)
    return state


# ── 解析 copilot 回复中的安排块 ─────────────────────────────────────────────
def _detect_cycle(edges: dict[str, list[str]]) -> str | None:
    """返回依赖环中一个可读的路径；无环返回 None。"""
    state: dict[str, int] = {}

    def visit(node: str, stack: list[str]) -> str | None:
        mark = state.get(node, 0)
        if mark == 1:  # 回到当前递归栈 → 成环
            return " → ".join(stack[stack.index(node):] + [node])
        if mark == 2:
            return None
        state[node] = 1
        for dep in edges.get(node, []):
            found = visit(dep, stack + [node])
            if found:
                return found
        state[node] = 2
        return None

    for node in edges:
        found = visit(node, [])
        if found:
            return found
    return None


def _validate_actions(raw: object, known_ids: set[str]) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise DispatchError("安排块缺少非空的 actions 数组。")
    if len(raw) > MAX_ACTIONS:
        raise DispatchError(f"单轮安排 {len(raw)} 项，超出上限 {MAX_ACTIONS}；请拆分并聚焦关键路径。")
    out: list[dict] = []
    seen: set[str] = set()
    declared = {str(x.get("id")).strip() for x in raw if isinstance(x, dict)}
    for idx, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise DispatchError(f"actions[{idx}] 不是对象。")
        aid = str(item.get("id") or "").strip()
        if not aid:
            raise DispatchError(f"actions[{idx}] 缺少 id。")
        if aid in seen:
            raise DispatchError(f"安排块内 id 重复：{aid}。")
        seen.add(aid)
        title = str(item.get("title") or "").strip()
        if not title:
            raise DispatchError(f"{aid} 缺少 title。")
        assignee = str(item.get("assignee") or "zcode").strip().lower()
        if assignee not in ASSIGNEES:
            raise DispatchError(
                f"{aid} 的 assignee={assignee!r} 不受支持；只能是 {' / '.join(ASSIGNEES)}。")
        deps = item.get("depends_on") or []
        if not isinstance(deps, list):
            raise DispatchError(f"{aid} 的 depends_on 必须是数组。")
        deps = [str(d).strip() for d in deps if str(d).strip()]
        if aid in deps:
            raise DispatchError(f"{aid} 不能依赖自身。")
        unknown = [d for d in deps if d not in known_ids and d not in declared]
        if unknown:
            raise DispatchError(f"{aid} 依赖不存在的工作项：{', '.join(unknown)}。")
        # 记录本项**实际给出**的字段：续轮更新只覆盖这些键，不把省略字段重置为默认值。
        given = [k for k in ("title", "assignee", "goal", "constraints",
                             "expected_evidence", "depends_on") if k in item]
        goal = str(item.get("goal") or "").strip()
        expected = str(item.get("expected_evidence") or "").strip()
        out.append({
            "id": aid,
            "title": title,
            "assignee": assignee,
            "goal": goal,
            "constraints": str(item.get("constraints") or "").strip(),
            "expected_evidence": expected,
            "depends_on": deps,
            "status": "pending",
            "result": "",
            "evidence_ids": [],
            "created": now_iso(),
            "updated": now_iso(),
            # 指令是否可执行：PARTIAL 安排若缺目标或需返回证据，会被标注并按未闭合
            # 条件补齐执行说明（来源可追溯），而不是把没写清的工作项直接派下去。
            "directive_complete": bool(goal and expected),
            "_given": given,
        })
    return out


def _plan_blocks(reply: str, strict: bool = True) -> list[tuple[int, int, str]]:
    """只返回顶层 copilot-plan 块：[(起始行, 结束行, 正文)]（0-based，闭区间）。

    逐行跟踪围栏状态：四反引号示例里的三反引号安排块属于数据，不能当指令。
    strict=True 时，顶层出现无法闭合/格式破损的安排围栏要报错（不静默丢安排）。
    """
    lines = (reply or "").splitlines()
    blocks: list[tuple[int, int, str]] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        opening = PLAN_OPEN_RE.match(stripped)
        if opening:
            token = opening.group(1)
            body: list[str] = []
            j = i + 1
            while j < len(lines):
                close = _FENCE_RE.match(lines[j].strip())
                if close and close.group(1)[0] == token[0] \
                        and len(close.group(1)) >= len(token) \
                        and lines[j].strip().strip(token[0]).strip() == "":
                    blocks.append((i, j, "\n".join(body)))
                    break
                body.append(lines[j])
                j += 1
            else:
                if strict:
                    raise DispatchError(
                        f"copilot-plan 块未闭合（第 {i + 1} 行开始），无法解析；"
                        "请用独立一行的同类围栏结束。")
                return blocks
            i = j + 1
            continue
        marker = _FENCE_RE.match(stripped)
        if marker:
            # 顶层出现「像安排块但不合规」的围栏：明确报错，不静默丢安排。
            if BROKEN_PLAN_RE.match(lines[i]):
                if strict:
                    raise DispatchError(
                        "检测到 copilot-plan 围栏但无法解析；请用单独一行的 "
                        "```copilot-plan 开始、单独一行的 ``` 结束，块内为合法 JSON"
                        "（不要缩进围栏，开闭围栏要一致）。")
                i += 1
                continue
            # 非安排围栏：整块跳过，避免把其中的示例当成顶层安排。
            token = marker.group(1)
            j = i + 1
            while j < len(lines):
                close = _FENCE_RE.match(lines[j].strip())
                if close and close.group(1)[0] == token[0] \
                        and len(close.group(1)) >= len(token) \
                        and lines[j].strip().strip(token[0]).strip() == "":
                    break
                j += 1
            i = j + 1
            continue
        i += 1
    return blocks


def parse_plan_ex(reply: str) -> tuple[dict | None, list[dict], str]:
    """解析 copilot-plan，并返回 (规范化计划, 字段映射, 规范化器版本)。

    结构化字段先经 ``reply_protocol`` 规范化：只有旧字段时补规范字段，两者冲突
    时失败关闭；原始字段保留在返回的计划里，回复正文不被修改。
    """
    blocks = _plan_blocks(reply)
    if not blocks:
        # 顶层破损围栏已由 _plan_blocks 报错；围栏内的示例不算安排，也不报错。
        return None, [], NORMALIZER_VERSION
    if len(blocks) > 1:
        # 多个安排块会让「哪一份生效」变成猜测：明确拒绝，而不是静默取第一个。
        raise DispatchError(f"回复包含 {len(blocks)} 个 copilot-plan 块；请只给一个。")
    try:
        raw = json.loads(blocks[0][2])
    except json.JSONDecodeError as exc:
        raise DispatchError(f"copilot-plan 块不是合法 JSON：{exc}") from None
    if not isinstance(raw, dict):
        raise DispatchError("copilot-plan 块必须是 JSON 对象。")
    try:
        normalized = normalize_plan(raw)
    except ProtocolError as exc:
        raise DispatchError(f"copilot-plan 字段结构不兼容：{exc.detail}") from None
    plan = normalized["plan"]
    decision = str(plan.get("decision") or "").strip().lower()
    if decision not in ("continue", "stop", "blocked"):
        raise DispatchError("copilot-plan 的 decision 只能是 continue / stop / blocked。")
    # 下游直接读 plan["decision"]；校验用规范化值，写回也必须是同一个值，
    # 否则 " CONTINUE " 会通过校验却被当成非 continue。
    plan["decision"] = decision
    verdict = str(plan.get("verdict") or "").strip().lower()
    if verdict and verdict not in VERDICTS:
        raise DispatchError(
            "copilot-plan 的 verdict 只能是 pass / partial / blocked。")
    if verdict:
        expected = VERDICT_DECISION[verdict]
        if decision != expected:
            raise DispatchError(
                f"verdict={verdict} 与 decision={decision} 不一致；"
                f"按映射必须写成 decision={expected}。")
        plan["verdict"] = verdict
    else:
        # 旧格式回复可以没有 verdict；它不得被推断成 PASS（只有 GPT 明示才算）。
        plan["verdict"] = None
    return plan, normalized["mapping"], normalized["version"]


def parse_plan(reply: str) -> dict | None:
    """从回复顶层提取 ```copilot-plan``` JSON；没有该块返回 None，格式错误抛 DispatchError。"""
    plan, _mapping, _version = parse_plan_ex(reply)
    return plan


def _reply_digest(reply: str) -> str:
    """把回复正文压缩成可回灌的判断记录（去掉安排块，截断到存储上限）。

    无 ``copilot-plan`` 的 CONSULT 回复是合法形态（只给分析、不排工作项）。
    原先这类回复只留下「未包含安排块」的备注，结论本身被丢弃，
    导致下一次调用拿不到前序判断（2026-10-20 真实多阶段验证中由 GPT 指出并实测证实）。
    """
    lines = (reply or "").splitlines()
    drop: set[int] = set()
    for start, end, _body in _plan_blocks(reply or "", strict=False):
        drop.update(range(start, end + 1))
    text = "\n".join(line for idx, line in enumerate(lines) if idx not in drop)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_RESULT_CHARS]


# ── 来源凭证（防止未验证/历史回复被当成有效指导）──────────────────────────
def _digest_variants(reply: str) -> set[str]:
    """回复正文的可接受摘要：原文，以及去掉一个尾部换行（--out 落盘会补 `\\n`）。"""
    variants = {sha256_text(reply)}
    if reply.endswith("\r\n"):
        variants.add(sha256_text(reply[:-2]))
    elif reply.endswith("\n"):
        variants.add(sha256_text(reply[:-1]))
    return variants


def _receipt_fields(receipt: dict | None, reply: str) -> dict:
    """校验来源凭证并返回写入轮次的身份字段；任何缺失都失败关闭。"""
    if not isinstance(receipt, dict):
        raise DispatchError(
            "缺少来源凭证：交接状态只接受经 call_gpt.py 验证身份的回复。"
            "手工补录必须提供 --call-id（CLI 会从审计日志重建凭证）；"
            "不得仅凭文件存在或正文格式写入。")
    call_id = str(receipt.get("call_id") or "").strip()
    if not call_id:
        raise DispatchError("来源凭证缺少 call_id：无法关联到原始调用。")
    if receipt.get("model_identity_ok") is not True:
        raise DispatchError(f"调用 {call_id} 的模型身份未通过验证，回复不得写入交接状态。")
    requested = str(receipt.get("model_requested") or "").strip()
    resolved = str(receipt.get("model_resolved") or "").strip()
    if not requested or not resolved:
        raise DispatchError(f"调用 {call_id} 的来源凭证缺少请求/回显模型，无法证明身份。")
    if requested != resolved:
        raise DispatchError(
            f"调用 {call_id} 的请求模型 {requested!r} 与回显模型 {resolved!r} 不一致，"
            "回复不得写入交接状态。")
    source = str(receipt.get("model_source") or "").strip()
    if source not in ("default", "override"):
        raise DispatchError(f"调用 {call_id} 的模型来源无效（model_source={source!r}）。")
    digest = str(receipt.get("reply_sha256") or "").strip()
    if digest not in _digest_variants(reply):
        raise DispatchError(
            f"回复摘要与调用 {call_id} 的凭证不一致：文件可能被改动，"
            "或不是该次调用的输出；拒绝写入。")
    fields = {
        "call_id": call_id,
        "model_requested": requested,
        "model_resolved": resolved,
        "model_source": source,
        "reply_sha256": digest,
        "model_identity_ok": True,
    }
    # 任务级凭证字段：只写实际给出的值；缺失由 _attribution 对任务级状态失败关闭。
    for key in ("task_id", "round_id", "contract_sha256", "reasoning_effort"):
        value = str(receipt.get(key) or "").strip()
        if value:
            fields[key] = value
    return fields


# ── 任务级凭证与挂起调用（防跨任务/跨轮次串线）─────────────────────────────
def task_credential(state: dict) -> dict | None:
    """返回状态的 task_id/合同指纹；旧格式状态返回 None（只能人工兼容补录）。"""
    task_id = str(state.get("task_id") or "").strip()
    if not task_id:
        return None
    contract = str(state.get("contract_sha256") or "").strip()
    if not contract:
        raise DispatchError(
            f"任务 {task_id} 缺少 contract_sha256（目标/完成条件/边界摘要）；"
            "无法证明回复属于本任务合同，拒绝自动协作。请重建交接状态并补齐合同。")
    return {"task_id": task_id, "contract_sha256": contract}


def allocate_round(state: dict) -> str:
    """发送前分配本轮 round_id；调用方必须先把 state 落盘再发请求。"""
    cred = task_credential(state)
    if cred is None:
        raise DispatchError(
            "交接状态缺少任务级凭证（task_id/contract_sha256）：旧格式状态不能进入"
            "自动协作通道；请用 `copilot_dispatch.py start` 重建状态，"
            "历史状态仅保留人工补录。")
    seq = state.get("round_seq", 0)
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise DispatchError(f"round_seq 无效（{seq!r}）。")
    seq += 1
    state["round_seq"] = seq
    return f"{cred['task_id']}-r{seq}"


def prepare_pending(state: dict, *, call_id: str, mode: str, model_requested: str,
                    reasoning_effort: str, model_source: str,
                    auxiliary_review: bool = False) -> dict:
    """调用前写入 pending_call：调用意图先落盘，中断后才能精确恢复而不是靠全局推断。

    `auxiliary_review=True`（PLAN 主任务的 REVIEW 辅助复核）把本次调用标记为
    「只留痕」：pending.mode 必须是 REVIEW，但状态的 mode 仍保持 PLAN，提交阶段
    不得解析安排块或改动主任务判断/工作项/完成条件。
    """
    cred = task_credential(state)
    if cred is None:
        raise DispatchError(
            "交接状态缺少任务级凭证（task_id/contract_sha256）：不能进入自动协作通道。")
    pending = state.get("pending_call")
    if isinstance(pending, dict):
        raise DispatchError(
            "存在尚未结束的调用（pending_call："
            f"round_id={pending.get('round_id') or '-'}，call_id={pending.get('call_id') or '-'}）。"
            "先用 `copilot_dispatch.py recover` 处理该轮，再发起新调用——"
            "结果未知的调用可能已被上游计费，不能直接覆盖。")
    mode_norm = str(mode or "").strip().upper()
    if not mode_norm:
        raise DispatchError(
            "任务级自动协作必须声明模式（`start --mode` 或调用 `--mode`）："
            "空模式会让不同模式共用同一任务状态，无法证明轮次属于同一契约。")
    if auxiliary_review:
        state_mode = str(state.get("mode") or "").strip().upper()
        if mode_norm != "REVIEW":
            raise DispatchError(
                f"辅助复核必须是 REVIEW 模式（本次 {mode_norm}）；拒绝写入。")
        if state_mode != "PLAN":
            raise DispatchError(
                f"辅助复核只允许挂在模式为 PLAN 的主任务上（状态模式 "
                f"{state_mode or '缺失'}）；拒绝写入。")
    if not str(state.get("mode") or "").strip():
        # 状态创建时未声明模式：由首次自动调用钉住，之后不得再换模式。
        state["mode"] = mode_norm
    round_id = allocate_round(state)
    state["pending_call"] = {
        **cred,
        "call_id": str(call_id),
        "round_id": round_id,
        "round_seq": state["round_seq"],
        "mode": mode_norm,
        "model_requested": str(model_requested),
        "reasoning_effort": str(reasoning_effort),
        "model_source": str(model_source),
        "requested_at": now_iso(),
    }
    if auxiliary_review:
        state["pending_call"]["auxiliary_review"] = True
    return state["pending_call"]


def release_pending(state: dict, call_id: str, reason: str) -> bool:
    """终止失败调用：清空 pending 并留一条 abandoned 记录（不产生轮次）。"""
    pending = state.get("pending_call")
    if not isinstance(pending, dict) or str(pending.get("call_id") or "") != str(call_id):
        return False
    state["pending_call"] = None
    trace = state.setdefault("abandoned_calls", [])
    trace.append({
        "ts": now_iso(), "call_id": str(call_id),
        "task_id": pending.get("task_id"), "round_id": pending.get("round_id"),
        "reason": str(reason or ""),
    })
    del trace[:-20]
    return True


def _attribution(state: dict, receipt: dict, fields: dict, mode: str, *,
                 allow_existing_round: bool = False,
                 require_pending: bool = True) -> None:
    """任务级归属校验：任务、合同、轮次、模式与自动通道档位，任一不符即拒绝。

    ``allow_existing_round`` / ``require_pending`` 只供「已消费但解析失败」的恢复
    路径使用：恢复必须复用原轮次、原 call_id，且此时 pending 已被正常调用清空。
    默认值保持正常 ingest 的严格行为，不改变原有入口。
    """
    cred = task_credential(state)
    if cred is None:
        return  # 旧格式状态：走人工兼容路径（由 ingest_reply 的旧校验把关）
    call_id = fields["call_id"]
    r_task = str(receipt.get("task_id") or "").strip()
    if not r_task:
        raise DispatchError(
            f"调用 {call_id} 的凭证没有 task_id（旧格式/人工调用）："
            "不得写入任务级自动协作状态；请重新发起绑定本任务的调用。")
    if r_task != cred["task_id"]:
        raise DispatchError(
            f"调用 {call_id} 属于任务 {r_task}，不能写入任务 {cred['task_id']}；"
            "跨任务回复一律拒绝。")
    r_contract = str(receipt.get("contract_sha256") or "").strip()
    if not r_contract:
        raise DispatchError(
            f"调用 {call_id} 的凭证缺少 contract_sha256，无法证明目标合同一致。")
    if r_contract != cred["contract_sha256"]:
        raise DispatchError(
            "目标合同指纹不一致：该回复不是针对本任务的目标/完成条件/边界生成的；"
            "不同目标合同不共享凭证，拒绝写入。")
    r_round = str(receipt.get("round_id") or "").strip()
    if not r_round:
        raise DispatchError(f"调用 {call_id} 的凭证缺少 round_id，无法证明轮次归属。")
    if not r_round.startswith(cred["task_id"] + "-r"):
        raise DispatchError(f"轮次 {r_round} 不属于任务 {cred['task_id']}，拒绝写入。")
    committed = {str(rnd.get("round_id") or "") for rnd in (state.get("rounds") or [])
                 if isinstance(rnd, dict)}
    if r_round in committed and not allow_existing_round:
        raise DispatchError(
            f"轮次 {r_round} 已写入（同一凭证不得重复消费）；请重新发起下一轮调用。")
    receipt_mode = str(receipt.get("mode") or receipt.get("call_mode") or "").strip().upper()
    if require_pending:
        pending = state.get("pending_call")
        if not isinstance(pending, dict):
            raise DispatchError(
                "交接状态没有该轮次的挂起凭证：旧回复或旧任务凭证不得写入新任务；"
                "若本轮调用因写盘失败未提交，请用 `recover --reply-file` 按 pending 恢复。")
        if str(pending.get("call_id") or "") != call_id:
            raise DispatchError(
                f"调用 {call_id} 不是当前挂起轮次的调用"
                f"（挂起：{pending.get('call_id') or '-'}）；拒绝写入。")
        if str(pending.get("round_id") or "") != r_round:
            raise DispatchError(
                f"轮次不一致（挂起：{pending.get('round_id') or '-'}，凭证：{r_round}）；"
                "拒绝写入。")
        if (str(pending.get("task_id") or "") != cred["task_id"]
                or str(pending.get("contract_sha256") or "") != cred["contract_sha256"]):
            raise DispatchError("挂起凭证与状态的任务/合同不一致（状态可能被改动）；拒绝写入。")
        p_mode = str(pending.get("mode") or "").strip().upper()
        if not p_mode:
            raise DispatchError(
                "挂起调用缺少模式：任务级自动协作必须绑定唯一模式，拒绝写入。")
    else:
        # 恢复路径没有 pending；模式仍必须与任务状态和原审计凭证一致。
        p_mode = str(state.get("mode") or "").strip().upper()
        if not p_mode:
            raise DispatchError(
                "任务状态缺少模式：无法证明恢复回复与原调用模式一致；拒绝恢复。")
    expected = str(mode or "").strip().upper() or p_mode
    if expected != p_mode:
        raise DispatchError(f"调用模式不一致（挂起：{p_mode}，本次写入：{expected}）；拒绝写入。")
    if receipt_mode != p_mode:
        raise DispatchError(
            f"凭证模式 {receipt_mode or '缺失'} 与挂起模式 {p_mode} 不一致；拒绝写入。")
    if fields["model_requested"] != AUTO_MODEL:
        raise DispatchError(
            f"自动协作只接受 {AUTO_MODEL} 的回复（本次凭证：{fields['model_requested']}）；"
            "手动模型/回滚通道不得写入任务级状态。")
    if fields["model_source"] != "default":
        raise DispatchError(
            f"自动协作只接受默认通道（model_source=default），本次为 "
            f"{fields['model_source']}（手动覆盖）；拒绝写入。")
    effort = str(receipt.get("reasoning_effort") or "").strip()
    if effort != AUTO_REASONING_EFFORT:
        raise DispatchError(
            f"自动协作只接受 reasoning_effort={AUTO_REASONING_EFFORT}"
            f"（本次：{effort or '缺失'}）；拒绝写入。")


def _mark_committed(state: dict, receipt: dict, reply: str, fields: dict) -> None:
    """消费本轮凭证：清空 pending，把来源凭证写入 last_call（仅内存，由调用方落盘）。"""
    state["pending_call"] = None
    state["last_call"] = {
        "call_id": fields["call_id"], "ts": now_iso(), "ok": True,
        **{k: v for k, v in fields.items() if k != "call_mode"},
        "reply_chars": len(reply),
    }


def _is_auxiliary_pending(state: dict) -> bool:
    pending = state.get("pending_call")
    return isinstance(pending, dict) and pending.get("auxiliary_review") is True


def ingest_auxiliary_review(state: dict, reply: str, mode: str = "", *,
                            receipt: dict | None = None) -> dict:
    """写入 PLAN 主任务的 REVIEW 辅助复核留痕：只记录来源凭证，不改主任务状态。

    与 `ingest_reply` 走同一套凭证/任务/合同/轮次/模式/身份校验，但**不解析安排块**：
    辅助复核的结论只进入调用方已写入的独立复核台账；任务状态里只追加一条
    `auxiliary_review=True`、无 decision/verdict 的轮次，供恢复与追溯使用。
    因此不会改动 verdict、工作项、完成条件或收口门。
    """
    fields = _receipt_fields(receipt, reply)
    _assert_open(state, "写入辅助复核")
    for rnd in state.get("rounds") or []:
        if (isinstance(rnd, dict)
                and str(rnd.get("call_id") or "") == fields["call_id"]):
            raise DispatchError(
                f"调用 {fields['call_id']} 已入账；同一回复只能写入一次，"
                "拒绝重复 ingest。")
    if task_credential(state) is None:
        raise DispatchError(
            "辅助复核只允许绑定任务级状态（task_id/contract_sha256）；"
            "旧格式状态不能写入辅助复核。")
    _attribution(state, receipt, fields, mode)
    if str(mode or "").strip().upper() != "REVIEW":
        raise DispatchError(
            f"辅助复核必须以 REVIEW 模式落账（本次 {mode or '-'}）；拒绝写入。")
    main_mode = str(state.get("mode") or "").strip().upper()
    if main_mode != "PLAN":
        raise DispatchError(
            f"主任务模式 {main_mode or '缺失'} 不是 PLAN，拒绝写入辅助复核；"
            "辅助复核不得改变主任务模式。")
    pending = state.get("pending_call")
    if not isinstance(pending, dict) or pending.get("auxiliary_review") is not True:
        raise DispatchError(
            "挂起调用没有辅助复核标记：REVIEW 回复不得作为普通任务轮次写入；"
            "请按正常 REVIEW 流程使用独立复核台账。")
    if str(pending.get("mode") or "").strip().upper() != "REVIEW":
        raise DispatchError(
            f"辅助复核的挂起模式必须是 REVIEW（本次 {pending.get('mode') or '-'}）；"
            "拒绝写入。")
    state["rounds"].append({
        "ts": now_iso(), "mode": "REVIEW", "decision": None,
        "actions": [], "auxiliary_review": True,
        "note": "PLAN 主任务的 REVIEW 辅助复核：结论只以独立复核台账为准；"
                "本留痕不改变主任务 verdict、工作项与完成条件，也不构成 G4 依据",
        "reply_digest": _reply_digest(reply),
        **fields,
    })
    return {"added": [], "updated": [], "decision": None, "skipped": True,
            "auxiliary_review": True}


def commit_reply(state: dict, reply: str, mode: str = "", *,
                 receipt: dict | None = None, review_only: bool = False) -> dict:
    """验证 → 写轮次/工作项 → 消费凭证 → 记录 last_call（全部在内存完成）。

    调用方必须紧接着用**一次** `save_state` 原子提交；提交失败时磁盘上仍是调用前
    持久化的 pending，可依据 call_id/round_id 精确恢复。

    `review_only=True` 或 pending 带 `auxiliary_review` 标记时只走辅助复核留痕，
    不解析安排块、不改主任务判断；恢复流程依据 pending 标记自动走同一路径。
    """
    auxiliary = _is_auxiliary_pending(state)
    if review_only and not auxiliary:
        raise DispatchError(
            "调用要求按辅助复核写入，但挂起调用没有 auxiliary_review 标记；拒绝写入。")
    if auxiliary:
        outcome = ingest_auxiliary_review(state, reply, mode, receipt=receipt)
        _mark_committed(state, receipt, reply, _receipt_fields(receipt, reply))
        return outcome
    before = len(state.get("rounds") or [])
    try:
        outcome = ingest_reply(state, reply, mode, receipt=receipt)
    except DispatchError:
        # 归属/凭证校验失败发生在写轮次之前：不消费凭证（状态不变）。
        # 解析/安排校验失败已经写入本轮留痕：凭证必须消费，同一回复不得重放。
        call_id = str((receipt or {}).get("call_id") or "")
        consumed = any(
            isinstance(rnd, dict) and str(rnd.get("call_id") or "") == call_id
            for rnd in (state.get("rounds") or [])[before:])
        if consumed and receipt is not None:
            _mark_committed(state, receipt, reply, _receipt_fields(receipt, reply))
        raise
    _mark_committed(state, receipt, reply, _receipt_fields(receipt, reply))
    return outcome


def _audit_records(path: str) -> list[dict]:
    """读审计 JSONL；无法解析的行跳过（残行不能证明任何调用）。"""
    if not os.path.exists(path):
        raise DispatchError(f"审计日志不存在：{path}；无法重建调用凭证。")
    records: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError as exc:
        raise DispatchError(f"无法读取审计日志 {path}：{exc}") from None
    return records


def latest_call_result_id(audit_log: str) -> str:
    """审计里最后一条 `call_result` 的 call_id（无论成败），用于无状态绑定时判定本轮。

    只看成功记录会让「失败调用之后补录更早的成功回复」蒙混过关；这里要求补录对象
    就是审计中最后一次调用，且它必须已通过身份验证（由 verify_call_receipt 保证）。
    """
    latest = ""
    for rec in _audit_records((audit_log or "").strip() or DEFAULT_AUDIT_LOG):
        if rec.get("event") == "call_result":
            latest = str(rec.get("call_id") or "")
    return latest


def verify_call_receipt(audit_log: str, call_id: str, reply: str) -> dict:
    """从审计日志重建调用凭证：call_id 必须对应一次身份验证通过的成功调用。"""
    call_id = (call_id or "").strip()
    if not call_id:
        raise DispatchError(
            "缺少 --call-id：无法证明该回复来自一次通过身份验证的真实调用，拒绝写入。")
    path = (audit_log or "").strip() or DEFAULT_AUDIT_LOG
    matches = [r for r in _audit_records(path)
               if str(r.get("call_id") or "") == call_id and r.get("event") == "call_result"]
    if not matches:
        raise DispatchError(
            f"审计日志 {path} 中找不到 call_id={call_id} 的结果记录；"
            "无法证明该回复来自一次真实调用，拒绝写入。")
    record = matches[-1]
    if record.get("ok") is not True:
        detail = f"，{record.get('error')}" if record.get("error") else ""
        raise DispatchError(
            f"调用 {call_id} 在审计中不是成功结果（ok={record.get('ok')!r}{detail}）；"
            "拒绝写入。")
    if record.get("model_identity_ok") is not True:
        raise DispatchError(
            f"调用 {call_id} 未通过模型身份验证（model_identity_ok="
            f"{record.get('model_identity_ok')!r}）；该回复不得写入交接状态。")
    if not str(record.get("reply_sha256") or "").strip():
        raise DispatchError(
            f"调用 {call_id} 的审计记录缺少 reply_sha256（P0 之前的旧记录），"
            "无法校验回复文件，拒绝写入。")
    receipt = {
        "call_id": call_id,
        "reply_sha256": record.get("reply_sha256"),
        "model_requested": record.get("model_requested"),
        "model_resolved": record.get("model_resolved"),
        "model_source": record.get("model_source"),
        "model_identity_ok": True,
        "reasoning_effort": record.get("reasoning_effort"),
        "mode": record.get("mode"),
    }
    # 任务级归属：旧记录没有这些字段 → 凭证不完整，任务级状态会在 _attribution 拒绝。
    for key in ("task_id", "round_id", "contract_sha256"):
        if record.get(key):
            receipt[key] = record.get(key)
    fields = _receipt_fields(receipt, reply)
    # 调用模式只用于模式绑定检查；不进入轮次记录。
    fields["call_mode"] = str(record.get("mode") or "").strip().upper()
    return fields


def _plan_criteria_entries(plan: dict) -> list[dict] | None:
    """解析 plan.criteria 的结构（不读状态）；结构违规一律拒绝。"""
    raw = plan.get("criteria")
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise DispatchError("plan.criteria 必须是数组。")
    entries: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise DispatchError(f"plan.criteria 第 {index} 项必须是对象。")
        cid = str(item.get("criterion_id") or "").strip()
        if not CRITERION_ID_RE.match(cid):
            raise DispatchError(f"plan.criteria 第 {index} 项 id 非法：{cid!r}。")
        if cid in seen:
            raise DispatchError(f"plan.criteria 中 {cid} 重复出现。")
        seen.add(cid)
        status = str(item.get("status") or "").strip().lower()
        if status not in CRITERION_STATUSES:
            raise DispatchError(f"plan.criteria 中 {cid} 的 status 非法：{status!r}。")
        refs_raw = item.get("evidence_refs")
        if refs_raw is None:
            refs_raw = []
        if not isinstance(refs_raw, list):
            raise DispatchError(f"plan.criteria 中 {cid} 的 evidence_refs 必须是数组。")
        refs = []
        for ref in refs_raw:
            if isinstance(ref, str):
                rid, given = ref.strip(), ""
            elif isinstance(ref, dict):
                rid = str(ref.get("id") or "").strip()
                given = str(ref.get("sha256") or "").strip()
            else:
                raise DispatchError(
                    f"plan.criteria 中 {cid} 的引用必须是 id 字符串或对象。")
            if not EVIDENCE_ID_RE.match(rid):
                raise DispatchError(f"plan.criteria 中 {cid} 引用了非法证据 id：{rid!r}。")
            refs.append({"id": rid, "sha256_given": given})
        if status == "met" and not refs:
            raise DispatchError(
                f"plan.criteria 中 {cid} 判定 met 但没有 evidence_refs；"
                "met 必须引用已登记证据。")
        entries.append({
            "criterion_id": cid, "status": status, "refs": refs,
            "description": str(item.get("description") or "").strip(),
            "note": str(item.get("note") or "").strip()[:500]})
    return entries


def _plan_criteria_updates(state: dict, plan: dict, round_id: str,
                           verdict: str | None) -> tuple[list[dict], bool]:
    """在副本上校验本轮条件判断；任何违规都不改状态（由调用方最后统一提交）。"""
    contract = state["contract"]
    existing = {str(c["criterion_id"]): c for c in contract.get("criteria") or []}
    entries = _plan_criteria_entries(plan)
    confirm_requested = bool(plan.get("criteria_confirmed"))
    if (entries is not None or confirm_requested) and not round_id:
        raise DispatchError(
            "条件核销必须绑定任务级轮次（round_id）；旧格式状态不得写入条件判断。")
    confirmed_now = False
    if confirm_requested:
        if contract.get("criteria_state") != "confirmed":
            if not existing:
                raise DispatchError("criteria_confirmed=true 但没有已起草的条件可确认。")
            if entries:
                for entry in entries:
                    old = existing.get(entry["criterion_id"])
                    if old is None:
                        raise DispatchError(
                            "criteria_confirmed=true 时 criteria 只能引用已起草条件；"
                            f"{entry['criterion_id']} 不存在。")
                    if entry["description"] \
                            and entry["description"] != str(old.get("description") or ""):
                        raise DispatchError(
                            f"确认完成条件时不得改写 {entry['criterion_id']} 的描述；"
                            "如需变更请用 contract --amend（用户决策）。")
            confirmed_now = True
    if entries is None:
        if verdict == "pass":
            raise DispatchError(
                "verdict=pass 必须逐条给出 criteria（不得用自然语言总结代替逐条核销）。")
        return [], confirmed_now
    ids = [entry["criterion_id"] for entry in entries]
    if verdict == "pass":
        if contract.get("criteria_state") != "confirmed" and not confirmed_now:
            raise DispatchError(
                "verdict=pass 前必须先确认完成条件（criteria_confirmed 或 contract --amend）。")
        if not existing:
            raise DispatchError("verdict=pass 但任务没有完成条件，无法逐条核销。")
        if set(ids) != set(existing) or len(ids) != len(existing):
            missing = sorted(set(existing) - set(ids))
            unknown = sorted(set(ids) - set(existing))
            raise DispatchError(
                "verdict=pass 必须恰好覆盖全部完成条件一次；"
                f"缺少 {', '.join(missing) or '无'}，未知 {', '.join(unknown) or '无'}。")
        non_met = [entry["criterion_id"] for entry in entries
                   if entry["status"] != "met"]
        if non_met:
            raise DispatchError(
                f"verdict=pass 与条件状态矛盾（{', '.join(non_met)} 不是 met）；"
                "请改判 partial 或先补齐证据。")
    else:
        unknown = sorted(set(ids) - set(existing))
        if unknown:
            raise DispatchError(f"plan.criteria 引用了未知条件：{', '.join(unknown)}。")
    evidence = {str(e.get("id") or ""): e for e in state.get("evidence") or []}
    task_id = str(state.get("task_id") or "")
    updates: list[dict] = []
    for entry in entries:
        old = existing[entry["criterion_id"]]
        if entry["description"] \
                and entry["description"] != str(old.get("description") or ""):
            raise DispatchError(
                f"条件 {entry['criterion_id']} 的描述不可原地改写；"
                "需要变更请用 contract --amend（用户决策）。")
        bound = []
        for ref in entry["refs"]:
            registered = evidence.get(ref["id"])
            if registered is None or registered.get("dropped"):
                raise DispatchError(
                    f"条件 {entry['criterion_id']} 引用未登记或已归档证据 "
                    f"{ref['id']}；本轮拒绝入账。")
            if str(registered.get("task_id") or "") != task_id:
                raise DispatchError(
                    f"证据 {ref['id']} 不属于当前任务 {task_id}；本轮拒绝入账。")
            if ref["sha256_given"] \
                    and ref["sha256_given"] != str(registered.get("sha256") or ""):
                raise DispatchError(
                    f"证据 {ref['id']} 的摘要与登记不一致；本轮拒绝入账。")
            bound.append({"id": ref["id"],
                          "sha256": str(registered.get("sha256") or "")})
        updates.append({
            "criterion_id": entry["criterion_id"], "status": entry["status"],
            "evidence_refs": bound, "note": entry["note"],
            "updated_round_id": round_id, "updated_by": "gpt"})
    return updates, confirmed_now


def _commit_plan_criteria(state: dict, updates: list[dict], confirmed_now: bool,
                          round_id: str) -> None:
    contract = state["contract"]
    if confirmed_now:
        contract["criteria_state"] = "confirmed"
        contract["criteria_sha256"] = criteria_digest(contract.get("criteria") or [])
        contract["confirmed_round_id"] = round_id
    by_id = {str(c["criterion_id"]): c for c in contract.get("criteria") or []}
    for update in updates:
        criterion = by_id.get(update["criterion_id"])
        if criterion is not None:
            criterion.update(update)


def _apply_plan_changes(state: dict, plan: dict, round_id: str,
                        fail) -> dict:
    """校验并提交一份已解析的 plan；正常 ingest 与恢复共用同一条路径。

    ``fail`` 由调用方提供：它负责留痕（新轮次或恢复记录）后抛错。所有校验都在
    副本上完成，只有全部通过才改 ``state``；因此失败不会留下半更新状态。
    """
    decision = plan["decision"]
    verdict = plan.get("verdict")

    # 先判 decision 与 actions 是否自相矛盾：stop/blocked 是收口信号，
    # 带新工作项时应直接拒绝，而不是撞上「id 已存在」这类下游错误。
    if decision != "continue" and plan.get("actions"):
        fail("decision=stop/blocked 时不应再安排新工作项。")

    # 逐条条件判断先在副本上校验（结构、覆盖、证据绑定）；任何违规都不改状态。
    try:
        criteria_updates, criteria_confirmed_now = _plan_criteria_updates(
            state, plan, round_id, verdict)
    except DispatchError as exc:
        fail(str(exc))

    known = {a["id"] for a in state["actions"]}
    actions = plan.get("actions") or []
    added: list[dict] = []
    updated: list[dict] = []
    if decision == "continue" and actions:
        try:
            parsed = _validate_actions(actions, known)
        except DispatchError as exc:
            fail(str(exc))
        by_id = {a["id"]: a for a in state["actions"]}
        # 先在副本上算出更新结果，全部校验通过后才提交，避免失败时把内存改成半成品。
        pending_updates: list[tuple[dict, dict]] = []
        for action in parsed:
            given = action.pop("_given", [])
            existing = by_id.get(action["id"])
            if existing is None:
                # 原生 Bridge 只接受来自已验证 SOL 安排块的工作项；把来源轮次
                # 写进工作项，派发时再核对轮次仍在已验证集合中。
                action["source_round_id"] = round_id
                added.append(action)
            elif existing["status"] in TERMINAL:
                fail(f"工作项 {action['id']} 已终结（{existing['status']}）；"
                     "续做请用新的 id，不要覆盖已完成/已放弃的产出。")
            else:
                # 同 id 在续轮出现 = 更新该工作项的说明；只覆盖**本项实际给出**的字段，
                # 未重述的目标/约束/依赖/执行方保持原值，不被静默清空。
                merged = dict(existing)
                for key in given:
                    merged[key] = action[key]
                merged["last_updated_round_id"] = round_id
                if not str(merged.get("source_round_id") or "").strip():
                    merged["source_round_id"] = round_id
                pending_updates.append((existing, merged))
        # 在副本上查环：既检查更新后的既有项，也检查本轮新增项。
        edges = {a["id"]: list(a.get("depends_on") or []) for a in state["actions"]}
        edges.update({a["id"]: list(a.get("depends_on") or []) for a in added})
        for original, merged in pending_updates:
            edges[original["id"]] = list(merged.get("depends_on") or [])
        cycle = _detect_cycle(edges)
        if cycle:
            fail(f"工作项依赖成环：{cycle}；请打破依赖后再提交。")
        # 提交：此时所有校验都已通过。
        for original, merged in pending_updates:
            merged["updated"] = now_iso()
            original.clear()
            original.update(merged)
            updated.append(original)
        state["actions"].extend(added)

    # 条件判断最后提交：前面任何校验失败都不会留下半份条件状态。
    _commit_plan_criteria(state, criteria_updates, criteria_confirmed_now,
                          round_id)
    return {"added": added, "updated": updated, "criteria": criteria_updates,
            "criteria_confirmed": criteria_confirmed_now}


def ingest_reply(state: dict, reply: str, mode: str = "", *,
                 receipt: dict | None = None) -> dict:
    """把本轮回复写入交接状态：记录轮次、追加新工作项或更新既有项。

    返回 {'added': [...], 'updated': [...], 'decision': str|None, 'skipped': bool}。
    没有安排块时不写入，skipped=True（由调用方警告，不伪造安排）。
    `decision=continue` 且没有 actions 是合法状态：表示继续当前已有工作项（例如刚回填反馈）。

    `receipt` 是**必需**的来源凭证（call_id / reply_sha256 / 请求与回显模型 /
    模型来源 / model_identity_ok）。缺失、身份未通过、摘要不符、重复入账，
    或（任务级状态）任务/合同/轮次/模式/档位不符时一律拒绝：未经验证身份的响应
    不能变成可执行工作项。
    """
    fields = _receipt_fields(receipt, reply)
    # 收口守卫必须最先执行：解析失败/无安排块分支也会留痕，不能在 CLOSED 后写入。
    _assert_open(state, "写入回复")
    for rnd in state.get("rounds") or []:
        if str(rnd.get("call_id") or "") == fields["call_id"]:
            raise DispatchError(
                f"调用 {fields['call_id']} 已入账；同一回复只能写入一次，拒绝重复 ingest。")
    if task_credential(state) is not None:
        # 任务级状态：只看本轮 pending 凭证，不再用全局「最后一条成功调用」推断归属。
        _attribution(state, receipt, fields, mode)
    else:
        last_call = state.get("last_call")
        if isinstance(last_call, dict):
            if str(last_call.get("call_id") or "") != fields["call_id"]:
                raise DispatchError(
                    "该回复不属于交接状态记录的最近一次调用"
                    f"（状态：{last_call.get('call_id') or '无'}，凭证：{fields['call_id']}）。"
                    "历史文件或旧轮回复不得当作本轮结果；请重新发起调用。")
            if last_call.get("ok") is False:
                raise DispatchError(
                    f"最近一次调用未通过验证（{last_call.get('error') or '未知'}），"
                    "该轮没有可用回复，拒绝写入。")
        elif state.get("rounds"):
            raise DispatchError(
                "交接状态缺少最近调用凭证（历史格式），无法证明该回复属于本轮；"
                "拒绝写入，请重新发起调用后再回填。")
    try:
        plan = parse_plan(reply)
    except DispatchError as exc:
        # 解析失败也要留痕：CLI 会保存这次轮次记录，避免「回复已拿到却查不到本轮」。
        state["rounds"].append({
            "ts": now_iso(), "mode": mode.upper(), "decision": None,
            "actions": [], "note": f"安排块解析失败：{exc}",
            "reply_digest": _reply_digest(reply),
            **fields,
        })
        raise
    if plan is None:
        record = {
            "ts": now_iso(), "mode": mode.upper(), "decision": None,
            "actions": [], "note": "回复未包含 copilot-plan 安排块（仅有结论时属正常）",
            # 无安排块是合法形态（CONSULT 常只给分析）。结论必须留痕并回灌，
            # 否则下一轮拿不到前序判断，多阶段协作退化为从零开始。
            "reply_digest": _reply_digest(reply),
            **fields,
        }
        if mode.upper() == "REVIEW":
            # REVIEW 的正式裁决行按契约独立成行，通常不在 copilot-plan 里。
            # 不落账的话，PARTIAL 自动派生与 PASS 收口都看不到本轮判断。
            # decision 按同一映射派生，保持 pass_pending / BLOCKED 语义一致。
            verdict = parse_verdict(reply)
            verdict = verdict.strip().lower() if verdict else None
            record["verdict"] = verdict
            if verdict:
                record["decision"] = VERDICT_DECISION[verdict]
                record["note"] = (f"REVIEW 无安排块；已落账正式裁决 {verdict.upper()}"
                                  f"（decision={VERDICT_DECISION[verdict]}）。")
            else:
                record["note"] = ("REVIEW 无安排块，且未解析到唯一正式裁决行；"
                                  "本轮不改变控制状态，不得当作 PASS。")
        state["rounds"].append(record)
        return {"added": [], "updated": [], "decision": record["decision"],
                "verdict": record.get("verdict"), "skipped": True}
    decision = plan["decision"]
    verdict = plan.get("verdict")
    round_id = str(fields.get("round_id") or "")

    def fail(message: str) -> None:
        """校验失败也写一条轮次记录后再抛：不让「回复已拿到却查不到本轮」成立。"""
        state["rounds"].append({
            "ts": now_iso(), "mode": mode.upper(), "decision": None,
            "attempted_decision": decision, "attempted_verdict": verdict,
            "rejected": True,
            "actions": [], "updated": [], "note": f"安排被拒绝：{message}",
            "reply_digest": _reply_digest(reply),
            **fields,
        })
        raise DispatchError(message)

    applied = _apply_plan_changes(state, plan, round_id, fail)
    state["rounds"].append({
        "ts": now_iso(), "mode": mode.upper(), "decision": decision,
        "verdict": verdict, "criteria": [u["criterion_id"] for u in applied["criteria"]],
        "criteria_confirmed": applied["criteria_confirmed"],
        "actions": [a["id"] for a in applied["added"]],
        "updated": [a["id"] for a in applied["updated"]],
        "summary": str(plan.get("summary") or "").strip(),
        **fields,
    })
    return {"added": applied["added"], "updated": applied["updated"],
            "decision": decision, "verdict": verdict,
            "criteria": applied["criteria"], "skipped": False}


# ── 领取 / 回填 / 渲染 ──────────────────────────────────────────────────────
def _by_id(state: dict, aid: str) -> dict:
    for action in state["actions"]:
        if action["id"] == aid:
            return action
    raise DispatchError(f"不存在的工作项：{aid}。")


def _verified_rounds(state: dict) -> list[dict]:
    """只返回通过模型身份验证的轮次；P0 前的旧轮次没有身份凭证，不得当作指导。"""
    return [rnd for rnd in (state.get("rounds") or [])
            if isinstance(rnd, dict) and rnd.get("model_identity_ok") is True]


def _effective_decision(state: dict) -> str | None:
    """最近一次给出了 decision 的已验证轮次；未验证身份的旧轮次不改变控制状态。"""
    for rnd in reversed(_verified_rounds(state)):
        decision = rnd.get("decision")
        if decision:
            return str(decision)
    return None


def ready_actions(state: dict) -> list[dict]:
    """可领取的工作项：pending，且所有依赖已 done；已 stop/blocked 时不再发放。"""
    # 收口后或本轮 verdict=pass 待核验时封锁执行面：PASS 不能靠继续干活绕开四门。
    if bool((state.get("closure") or {}).get("closed")) or pass_pending(state):
        return []
    if _effective_decision(state) in ("stop", "blocked"):
        return []
    done = {a["id"] for a in state["actions"] if a["status"] == "done"}
    out = []
    for action in state["actions"]:
        if action["status"] != "pending":
            continue
        if all(dep in done for dep in action.get("depends_on", [])):
            out.append(action)
    return out


def stalled_actions(state: dict) -> list[tuple[dict, str]]:
    """需要人看、但不会出现在可领取列表里的工作项：running / failed / blocked / 永久 pending。

    返回 [(action, 原因)]。目的是让崩溃遗留的 running、待重试的 failed、待解阻的 blocked
    和依赖已放弃项的 pending 不会从 `next` 里静默消失。
    """
    out: list[tuple[dict, str]] = []
    by_id = {a["id"]: a for a in state["actions"]}
    for action in state["actions"]:
        status = action["status"]
        if status in ("running", "failed", "blocked"):
            out.append((action, f"{status}：需重新 record 才能继续"))
            continue
        if status != "pending":
            continue
        missing = [d for d in action.get("depends_on", []) if d not in by_id]
        if missing:
            out.append((action, f"依赖 {', '.join(missing)} 在状态中不存在"))
            continue
        blockers = [d for d in action.get("depends_on", [])
                    if by_id.get(d, {}).get("status") in ("dropped", "failed", "blocked")]
        if blockers:
            out.append((action, f"依赖 {', '.join(blockers)} 不可满足或尚需解阻"))
    return out


DERIVED_WORK_PREFIX = "AUTO-"
MAX_DERIVED_FINDINGS = 12
MAX_DERIVED_LINE_CHARS = 400


def _reply_findings(reply: str) -> list[str]:
    """从 PARTIAL 回复原文里提取「哪里还没做好」的线索（只摘录，不解读）。

    跳过 copilot-plan 围栏与普通代码块，只取列表行/编号行；每行裁剪到固定长度。
    这些原文由主代理按原文核对，不能当成已确认事实，也不能替代证据。
    """
    out: list[str] = []
    in_fence = False
    for raw in (reply or "").splitlines():
        line = raw.strip()
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        match = re.match(r"^([-*•]|\d+[.)])\s+(.+)$", line)
        if not match:
            continue
        text = match.group(2).strip()
        if len(text) < 6:
            continue
        out.append(text[:MAX_DERIVED_LINE_CHARS])
        if len(out) >= MAX_DERIVED_FINDINGS:
            break
    return out


def unmet_criteria(state: dict) -> list[dict]:
    """尚未核销的完成条件（pending / not_met）；没有条件时返回空列表。"""
    criteria = (state.get("contract") or {}).get("criteria") or []
    return [c for c in criteria
            if str(c.get("status") or "pending") != "met"]


def derive_work(state: dict, reply: str = "", *, round_id: str = "",
                source: str = "partial-without-plan") -> dict:
    """把 PARTIAL 判断转成主代理可执行的工作项（纯本地，不发起任何调用）。

    触发条件：最近一个已验证轮次 `verdict=partial`，但 SOL 没有给出可执行安排
    （或安排缺少 goal / expected_evidence）。派生的动作**明确标注来源**：
    `origin=partial-without-plan`、`derived_from_round_id`，不是 SOL 直接安排，
    也不改写 SOL 的判断。目标与证据要求来自原合同的未闭合条件，约束来自合同边界。

    只派生一个工作项：一个人工/代理可一次做完的补证动作，避免把 PARTIAL 拆成
    无人负责的碎片。调用方负责落盘（与 `record` / `add_report` 相同约定）。
    """
    _assert_open(state, "派生下一步工作项")
    if isinstance(state.get("pending_call"), dict):
        raise DispatchError("存在未结算的 pending_call：先 recover 再派生工作项。")
    rounds = _verified_rounds(state)
    if not rounds:
        raise DispatchError("没有已验证轮次：不能派生工作项。")
    latest = rounds[-1]
    verdict = str(latest.get("verdict") or "").strip().lower()
    if verdict != "partial":
        raise DispatchError(
            f"最近已验证轮次 verdict={verdict or '-'}，不是 partial；不派生工作项。")
    rid = (round_id or str(latest.get("round_id") or "")).strip()
    unmet = unmet_criteria(state)
    findings = _reply_findings(reply)
    if not unmet and not findings:
        raise DispatchError(
            "没有未闭合完成条件，也没有可提取的缺口描述；不派生工作项。"
            "请补取证据或让 SOL 给出明确的下一步。")
    existing = {str(a.get("id") or "") for a in state.get("actions") or []}
    index = 1
    while f"{DERIVED_WORK_PREFIX}{index}" in existing:
        index += 1
    aid = f"{DERIVED_WORK_PREFIX}{index}"
    goal_lines = [f"补齐第 {rid or '最近'} 轮 PARTIAL 判断指出的未闭合项，"
                  "并提交可直接核销的原始证据。"]
    if unmet:
        goal_lines.append("未闭合完成条件：")
        goal_lines += [f"- {c.get('criterion_id')}："
                       f"{c.get('description') or '（未描述）'}" for c in unmet]
    if findings:
        goal_lines.append("SOL 本轮指出的问题（原文摘录，按原文核对）：")
        goal_lines += [f"- {text}" for text in findings]
    boundaries = str((state.get("contract") or {}).get("boundaries") or "").strip()
    constraints = ["不得越过原始授权范围；不得自行修改目标、完成条件或业务规则。",
                   "只做本轮缺口需要的最小改动，不顺手重构无关内容。"]
    if boundaries:
        constraints.insert(0, f"原合同边界：{boundaries}")
    evidence_need = "、".join(str(c.get("criterion_id") or "") for c in unmet)
    action = {
        "id": aid,
        "title": f"补齐第 {rid or '最近'} 轮 PARTIAL 缺口",
        "assignee": "zcode",
        "goal": "\n".join(goal_lines),
        "constraints": "\n".join(constraints),
        "expected_evidence": (
            f"覆盖未闭合条件 {evidence_need} 的原始产物（命令输出 / 文件内容 / 日志 / "
            "diff / 测试结果），登记为证据后在 manifest 中引用"
            if evidence_need else
            "能证明上述缺口已闭合的原始产物（命令输出 / 文件内容 / 日志 / diff）"),
        "depends_on": [],
        "status": "pending",
        "result": "",
        "evidence_ids": [],
        "created": now_iso(),
        "updated": now_iso(),
        "origin": source,
        "derived_from_round_id": rid,
        # 绑定来源轮次：该轮已通过模型身份验证（verdict=partial）。派生动作本身由
        # 本地生成，不冒充 SOL 的直接安排，但它的授权与判断依据就是这一轮。
        "source_round_id": rid,
        "directive_complete": True,
    }
    state.setdefault("actions", []).append(action)
    return action


def _evidence_entry(state: dict, eid: str) -> dict | None:
    for entry in state.get("evidence") or []:
        if str(entry.get("id") or "") == (eid or "").strip():
            return entry
    return None


def _validate_evidence_refs(state: dict, evidence_ids: list[str]) -> list[str]:
    """校验工作项回填引用的证据：已登记、属于当前任务、摘要仍一致。

    全部校验在修改工作项之前完成：任一引用无效即整次回填失败，不产生半更新
    （状态、产出与证据列表都不会被改动）。复用证据登记记录作为唯一事实来源；
    文件型证据重读摘要，内联证据以登记时摘要为准。
    """
    clean: list[str] = []
    task_id = str(state.get("task_id") or "")
    for raw in evidence_ids or []:
        eid = str(raw).strip()
        if not eid:
            continue
        if not EVIDENCE_ID_RE.match(eid):
            raise DispatchError(f"证据 id {eid!r} 非法（应形如 E1）。")
        entry = _evidence_entry(state, eid)
        if entry is None or entry.get("dropped"):
            raise DispatchError(f"证据 {eid} 未登记或已归档；请先登记证据再引用。")
        if str(entry.get("task_id") or "") != task_id:
            raise DispatchError(f"证据 {eid} 不属于任务 {task_id}；拒绝跨任务引用。")
        expected = str(entry.get("sha256") or "")
        if not expected:
            raise DispatchError(f"证据 {eid} 缺少登记摘要；引用无效。")
        hint = str(entry.get("path_hint") or "")
        if hint and hint != "<inline>":
            if not os.path.exists(hint):
                raise DispatchError(f"证据 {eid} 的文件不存在：{hint}")
            try:
                actual = sha256_file(hint)
            except OSError as exc:
                raise DispatchError(f"证据 {eid} 无法重读：{exc}") from None
            if actual != expected:
                raise DispatchError(f"证据 {eid} 的文件摘要与登记不一致；拒绝引用。")
        if eid not in clean:
            clean.append(eid)
    return clean


def record(state: dict, aid: str, status: str, result: str = "",
           evidence_ids: list[str] | None = None) -> dict:
    if status not in STATUSES:
        raise DispatchError(f"status={status!r} 不受支持；只能是 {', '.join(STATUSES)}。")
    _assert_open(state, "回填工作项")
    action = _by_id(state, aid)
    # done / dropped 是终结状态：既不能改状态，也不能改写产出与证据，
    # 否则历史记录会被静默覆盖。failed / blocked 仍可继续或重试。
    if action["status"] in TERMINAL:
        raise DispatchError(
            f"{aid} 已终结（{action['status']}）；不能再次回填。续做请新建工作项。")
    clean_evidence = (_validate_evidence_refs(state, evidence_ids)
                      if evidence_ids is not None else None)
    action["status"] = status
    # 连续失败计数：同一工作项 failed ≥ 3 次视为无进展（§3.3 子规则），但不自动
    # dropped / 失败——只提示主代理给根因或建议改判。
    if status == "failed":
        action["fail_streak"] = int(action.get("fail_streak") or 0) + 1
    elif status in ("running", "done"):
        action["fail_streak"] = 0
    if result.strip():
        action["result"] = result.strip()
    if clean_evidence is not None:
        action["evidence_ids"] = clean_evidence
    action["updated"] = now_iso()
    return action


# ── 阶段 2 写入面：决策 / 证据 / 报告 / 完成条件 / 配额 ──────────────────────
def _assert_open(state: dict, action: str) -> None:
    """收口后禁止继续写入：只有只读命令与用户授权的 reopen 例外。"""
    if bool((state.get("closure") or {}).get("closed")):
        raise DispatchError(
            f"任务已收口（closure.closed=true），拒绝{action}；"
            "继续需用户明确授权后执行 `closure --reopen --reason ...`。")


def _verified_round_ids(state: dict) -> list[str]:
    return [str(rnd.get("round_id")) for rnd in _verified_rounds(state)
            if str(rnd.get("round_id") or "")]


def add_decision(state: dict, text: str, source: str = "user") -> dict:
    """记录已确认的业务决策（用户或 GPT）；这是多轮上下文的事实来源之一。"""
    _assert_open(state, "记录业务决策")
    text = (text or "").strip()
    if not text:
        raise DispatchError("决策内容为空。")
    if len(text) > 2000:
        raise DispatchError(f"单条决策 {len(text)} 字，超出上限 2000；请精简后再记录。")
    src = (source or "user").strip().lower()
    if src not in ("user", "gpt"):
        raise DispatchError(f"决策来源 {source!r} 非法；只能是 user / gpt。")
    entry = {"ts": now_iso(), "source": src, "text": text}
    state["decisions"].append(entry)
    return entry


def register_evidence(state: dict, eid: str, *, kind: str = "", text: str | None = None,
                      file_path: str | None = None, note: str = "") -> dict:
    """登记一条证据：只存元数据与摘要，正文仍由证据包承载。

    幂等：同 id 且摘要相同视为重复登记，直接返回既有条目；摘要不同则拒绝
    （历史证据不得被静默替换，需新 id）。
    """
    _assert_open(state, "登记证据")
    eid = (eid or "").strip()
    if not EVIDENCE_ID_RE.match(eid):
        raise DispatchError(f"证据 id {eid!r} 非法（应形如 E1）。")
    if bool(file_path) == (text is not None):
        raise DispatchError("证据登记必须且只能提供 --file 或 --text 之一。")
    if file_path:
        path = os.path.abspath(file_path)
        if not os.path.exists(path):
            raise DispatchError(f"证据文件不存在：{path}")
        digest = sha256_file(path)
        size = os.path.getsize(path)
        hint = path
    else:
        raw = text or ""
        digest = sha256_text(raw)
        size = len(raw.encode("utf-8"))
        hint = "<inline>"
    for entry in state["evidence"]:
        if str(entry.get("id") or "") != eid:
            continue
        if entry.get("dropped"):
            raise DispatchError(f"证据 {eid} 已归档（dropped）；请使用新的 id。")
        if str(entry.get("sha256") or "") != digest:
            raise DispatchError(
                f"证据 {eid} 已登记且摘要不同：历史证据不得被静默替换；"
                "请使用新的 id（或先 --drop 归档旧条目）。")
        return entry
    entry = {
        "id": eid,
        "kind": (kind or "file").strip()[:64],
        "sha256": digest,
        "size": size,
        "captured_at": now_iso(),
        "task_id": str(state.get("task_id") or ""),
        "path_hint": hint,
        "note": (note or "").strip()[:500],
        "dropped": False,
        "dropped_reason": "",
    }
    state["evidence"].append(entry)
    return entry


def drop_evidence(state: dict, eid: str, reason: str) -> dict:
    """归档一条证据（保留历史与 id 占位）；仍被完成条件引用时核销会失败。"""
    _assert_open(state, "归档证据")
    reason = (reason or "").strip()
    if not reason:
        raise DispatchError("归档证据必须说明原因（--reason）。")
    for entry in state["evidence"]:
        if str(entry.get("id") or "") == (eid or "").strip():
            if entry.get("dropped"):
                return entry
            entry["dropped"] = True
            entry["dropped_reason"] = reason[:500]
            return entry
    raise DispatchError(f"不存在证据 {eid}。")


def _find_evidence_by_id(state: dict, eid: str) -> dict | None:
    for entry in state.get("evidence") or []:
        if isinstance(entry, dict) and str(entry.get("id") or "") == eid:
            return entry
    return None


def _next_evidence_id(state: dict) -> str:
    used = {str(entry.get("id") or "") for entry in state.get("evidence") or []
            if isinstance(entry, dict)}
    for number in range(1, 10000):
        candidate = f"E{number}"
        if candidate not in used:
            return candidate
    raise DispatchError("证据 id 已用尽（E1–E9999）；请先归档不再使用的证据。")


def _verify_review_target(state: dict, sha: str, review_of_kind: str,
                          review_source: str) -> None:
    """复核对象必须真实存在，且类型与来源声明一致。"""
    if review_of_kind == NATIVE_REVIEW_OF_KIND:
        if review_source != "native":
            raise DispatchError("native-execution-result 只能用于 native 复核报告。")
        for entry in state.get("native_results") or []:
            if (isinstance(entry, dict)
                    and str(entry.get("kind") or "execution") == "execution"
                    and str(entry.get("result_sha256") or "") == sha):
                return
        raise DispatchError(
            f"复核对象 {sha} 不是本任务已入账的 native 执行结果；不能凭空复核。")
    if review_source != "legacy":
        raise DispatchError(
            f"{review_of_kind} 只能用于 legacy 复核报告；native 复核必须绑定 "
            "execution_result_sha256。")
    if review_of_kind == "execution-report":
        for entry in state.get("reports") or []:
            if (isinstance(entry, dict)
                    and str(entry.get("kind") or "execution") == "execution"
                    and str(entry.get("report_sha256") or "") == sha):
                return
        raise DispatchError(
            f"复核对象 {sha} 不是本任务已登记的执行报告摘要（execution-report）。")
    if review_of_kind == "evidence":
        for entry in state.get("evidence") or []:
            if (isinstance(entry, dict) and not entry.get("dropped")
                    and str(entry.get("sha256") or "") == sha
                    and str(entry.get("task_id") or "") == str(state.get("task_id") or "")):
                return
        raise DispatchError(f"复核对象 {sha} 不是本任务已登记且未归档的证据。")
    raise DispatchError(
        f"review_of_kind={review_of_kind!r} 非法；只能是 "
        f"{NATIVE_REVIEW_OF_KIND} / {' / '.join(REVIEW_OF_KINDS)}。")


def register_review_evidence(state: dict, *, file_path: str, review_of: str,
                             review_of_kind: str, review_source: str,
                             verdict: str, round_id: str = "",
                             eid: str = "", note: str = "") -> dict:
    """登记 Reviewer 原始报告为证据，并绑定复核对象与来源。

    只接受真实文件；正文不进入状态，证据包按登记时的 SHA-256 重新校验。
    同一原始文件 + 同一复核对象重复登记是幂等的；同一证据 id 摘要不同、
    或同一复核对象已有不同原始报告时拒绝，避免静默覆盖历史。
    """
    _assert_open(state, "登记复核报告证据")
    review_of = (review_of or "").strip().lower()
    review_of_kind = (review_of_kind or "").strip().lower()
    review_source = (review_source or "").strip().lower()
    verdict = (verdict or "").strip().lower()
    round_id = (round_id or "").strip()
    eid = (eid or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", review_of):
        raise DispatchError("复核证据必须绑定 64 位 review_of SHA-256。")
    if review_of_kind not in (NATIVE_REVIEW_OF_KIND, *REVIEW_OF_KINDS):
        raise DispatchError(
            f"review_of_kind={review_of_kind!r} 非法；只能是 "
            f"{NATIVE_REVIEW_OF_KIND} / {' / '.join(REVIEW_OF_KINDS)}。")
    if review_source not in REVIEW_SOURCES:
        raise DispatchError(
            f"review_source={review_source!r} 非法；只能是 {' / '.join(REVIEW_SOURCES)}。")
    if verdict not in VERDICTS:
        raise DispatchError(
            f"复核证据 verdict={verdict!r} 非法；只能是 {' / '.join(VERDICTS)}。")
    if review_source == "native" and review_of_kind != NATIVE_REVIEW_OF_KIND:
        raise DispatchError("native 复核证据必须使用 native-execution-result 绑定。")
    if review_source == "legacy" and review_of_kind == NATIVE_REVIEW_OF_KIND:
        raise DispatchError("legacy 复核证据不得冒用 native 执行结果绑定。")
    if review_source == "native":
        if state.get("execution_backend") != "native":
            raise DispatchError("只有 native 任务可以登记 native 复核证据。")
        channel = state.get("review_channel")
        if channel not in (None, "native"):
            raise DispatchError(
                f"当前 review_channel={channel!r}，与原生复核证据冲突。")
    elif state.get("execution_backend") == "native":
        raise DispatchError(
            "native 任务必须回填 native 复核信封；不得用 legacy 报告混入"
            "同一复核权威与 G4 判定。")
    elif state.get("review_channel") == "native":
        raise DispatchError(
            "当前任务已有原生复核权威（review_channel=native）；"
            "不得再以 legacy 报告混入同一复核结论。")
    _verify_review_target(state, review_of, review_of_kind, review_source)
    if not file_path:
        raise DispatchError("复核证据必须提供原始报告文件（--file）。")
    path = os.path.abspath(file_path)
    if not os.path.isfile(path):
        raise DispatchError(f"复核报告文件不存在：{path}")
    digest = sha256_file(path)
    if eid and not EVIDENCE_ID_RE.match(eid):
        raise DispatchError(f"证据 id {eid!r} 非法（应形如 E1）。")
    existing = _find_evidence_by_id(state, eid) if eid else None
    if existing is not None:
        if existing.get("dropped"):
            raise DispatchError(f"证据 {eid} 已归档（dropped）；请使用新的 id。")
        if str(existing.get("task_id") or "") != str(state.get("task_id") or ""):
            raise DispatchError(f"证据 {eid} 不属于当前任务；不能作为复核原始报告。")
        if str(existing.get("sha256") or "") != digest:
            raise DispatchError(
                f"证据 {eid} 已登记且摘要不同：不得静默替换复核原始报告。")
        if existing.get("origin") == "review-report" and (
                str(existing.get("review_of") or "") != review_of
                or str(existing.get("review_of_kind") or "") != review_of_kind
                or str(existing.get("review_source") or "") != review_source):
            raise DispatchError(
                f"证据 {eid} 已绑定到其他复核对象或来源；不能改绑。")
    if existing is None:
        for entry in state.get("evidence") or []:
            if (isinstance(entry, dict)
                    and entry.get("origin") == "review-report"
                    and str(entry.get("task_id") or "") == str(state.get("task_id") or "")
                    and str(entry.get("sha256") or "") == digest
                    and str(entry.get("review_of") or "") == review_of
                    and str(entry.get("review_of_kind") or "") == review_of_kind
                    and str(entry.get("review_source") or "") == review_source):
                if entry.get("dropped"):
                    raise DispatchError("该复核报告证据已归档；不能继续引用。")
                existing = entry
                break
    if existing is None:
        existing = register_evidence(
            state, eid or _next_evidence_id(state),
            kind=f"{review_source}-review-report", file_path=path, note=note)
    existing["kind"] = f"{review_source}-review-report"
    existing["origin"] = "review-report"
    existing["review_of"] = review_of
    existing["review_of_kind"] = review_of_kind
    existing["review_source"] = review_source
    existing["verdict"] = verdict
    existing["round_id"] = round_id
    existing["raw_report_sha256"] = digest
    existing["review_task_id"] = str(state.get("task_id") or "")
    if note and not existing.get("note"):
        existing["note"] = note[:500]
    return existing


def add_report(state: dict, summary: str, round_id: str = "", *,
               blocked: bool = False, reason: str = "",
               kind: str = "execution", work_item_id: str = "",
               dispatch_id: str = "", result_sha256: str = "",
               review_of: str = "", verdict: str = "",
               g4_eligible: bool = False, review_of_kind: str = "",
               evidence_id: str = "", raw_report_sha256: str = "",
               review_source: str = "") -> dict:
    """登记执行或独立复核报告，绑定到已验证轮次。

    `kind=execution` 与 `kind=review` 必须分开登记：执行方自报不能替代复核，
    复核报告也不能混进执行报告。复核必须绑定 `review_of` 和 verdict，并通过
    `evidence_id` 指向已登记的原始报告证据；`g4_eligible` 只能为 false。
    """
    _assert_open(state, "写入执行/复核报告")
    verified = _verified_round_ids(state)
    if not verified:
        raise DispatchError("还没有已验证轮次：报告必须绑定到本轮的 GPT 调用。")
    rid = (round_id or verified[-1]).strip()
    if rid not in verified:
        raise DispatchError(f"轮次 {rid} 不是已验证轮次；报告必须绑定真实调用。")
    kind = (kind or "execution").strip().lower()
    if kind not in ("execution", "review"):
        raise DispatchError(f"报告 kind={kind!r} 非法；只能是 execution / review。")
    summary = (summary or "").strip()
    reason = (reason or "").strip()
    if blocked and not reason:
        raise DispatchError("blocked 报告必须给出原因（--reason）。")
    if not summary:
        raise DispatchError("报告内容为空。")
    if len(summary) > 8000:
        raise DispatchError(f"报告 {len(summary)} 字，超出上限 8000；请精简。")
    review_of = (review_of or "").strip().lower()
    verdict = (verdict or "").strip().lower()
    result_sha256 = (result_sha256 or "").strip().lower()
    review_of_kind = (review_of_kind or "").strip().lower()
    evidence_id = (evidence_id or "").strip()
    raw_report_sha256 = (raw_report_sha256 or "").strip().lower()
    review_source = (review_source or "").strip().lower()
    if g4_eligible:
        raise DispatchError(
            "普通报告不得声明 g4_eligible=true；G4 目前仍 fail-closed，"
            "原生 Reviewer 的 PASS 不是宿主可信复核凭证。")
    if kind == "review":
        if not review_of or not re.fullmatch(r"[0-9a-f]{64}", review_of):
            raise DispatchError("复核报告必须绑定 64 位 review_of SHA-256。")
        if verdict not in VERDICTS:
            raise DispatchError(
                f"复核报告 verdict={verdict!r} 非法；只能是 {' / '.join(VERDICTS)}。")
        review_source = review_source or "legacy"
        if review_source not in REVIEW_SOURCES:
            raise DispatchError(
                f"复核报告 review_source={review_source!r} 非法；"
                f"只能是 {' / '.join(REVIEW_SOURCES)}。")
        if review_source == "native" and state.get("execution_backend") != "native":
            raise DispatchError("只有 native 任务可以写入 native 复核报告。")
        if (review_source == "legacy"
                and state.get("execution_backend") == "native"):
            raise DispatchError(
                "native 任务不得用 legacy 复核报告混入复核权威；"
                "请回填 native 复核信封。")
        review_of_kind = review_of_kind or (
            NATIVE_REVIEW_OF_KIND if review_source == "native" else "")
        if review_of_kind not in (NATIVE_REVIEW_OF_KIND, *REVIEW_OF_KINDS):
            raise DispatchError(
                "复核报告必须明确 review_of_kind：legacy 使用 "
                "execution-report / evidence，native 使用 "
                f"{NATIVE_REVIEW_OF_KIND}。")
        if review_source == "native" and review_of_kind != NATIVE_REVIEW_OF_KIND:
            raise DispatchError("native 复核报告必须使用 native-execution-result 绑定。")
        if review_source == "legacy" and review_of_kind == NATIVE_REVIEW_OF_KIND:
            raise DispatchError("legacy 复核报告不得冒用 native 执行结果绑定。")
        _verify_review_target(state, review_of, review_of_kind, review_source)
        if not evidence_id or not EVIDENCE_ID_RE.match(evidence_id):
            raise DispatchError(
                "复核报告必须用 evidence_id 指向已登记的原始报告证据（形如 E7）。")
        evidence = _find_evidence_by_id(state, evidence_id)
        if evidence is None:
            raise DispatchError(f"复核报告引用的证据 {evidence_id} 不存在。")
        if evidence.get("dropped"):
            raise DispatchError(f"复核报告引用的证据 {evidence_id} 已归档。")
        if str(evidence.get("task_id") or "") != str(state.get("task_id") or ""):
            raise DispatchError(f"复核报告引用的证据 {evidence_id} 不属于当前任务。")
        if not raw_report_sha256 or not re.fullmatch(r"[0-9a-f]{64}",
                                                    raw_report_sha256):
            raise DispatchError("复核报告必须提供原始报告 64 位 SHA-256。")
        if (evidence.get("origin") != "review-report"
                or str(evidence.get("sha256") or "") != raw_report_sha256
                or str(evidence.get("review_of") or "") != review_of
                or str(evidence.get("review_of_kind") or "") != review_of_kind
                or str(evidence.get("review_source") or "") != review_source):
            raise DispatchError(
                f"证据 {evidence_id} 不是该复核对象/来源的原始报告证据；拒绝绑定。")
        for existing in state.get("reports") or []:
            if not isinstance(existing, dict) or existing.get("kind") != "review":
                continue
            if str(existing.get("raw_report_sha256") or "") != raw_report_sha256:
                continue
            same = (
                str(existing.get("round_id") or "") == rid
                and str(existing.get("review_of") or "") == review_of
                and str(existing.get("review_of_kind") or "") == review_of_kind
                and str(existing.get("review_source") or "") == review_source
                and str(existing.get("verdict") or "") == verdict
                and str(existing.get("evidence_id") or "") == evidence_id)
            if same:
                return {**existing, "duplicate": True}
            raise DispatchError(
                "同一原始复核报告已登记，但轮次/复核对象/结论/证据绑定不同；"
                "不得静默改写历史复核。")
        for existing in state.get("reports") or []:
            if (isinstance(existing, dict)
                    and existing.get("kind") == "review"
                    and str(existing.get("review_of") or "") == review_of
                    and str(existing.get("review_of_kind") or "") == review_of_kind
                    and str(existing.get("review_source") or "") == review_source
                    and existing.get("raw_report_sha256")
                    and str(existing.get("raw_report_sha256")) != raw_report_sha256):
                raise DispatchError(
                    "同一复核对象已登记不同原始报告；复核结论不得被后一份替换。")
    elif (review_of or verdict or review_of_kind or evidence_id
          or raw_report_sha256 or review_source):
        raise DispatchError(
            "执行报告不得携带 review_of / verdict / 复核证据字段；复核必须单独登记。")
    entry = {"round_id": rid, "ts": now_iso(), "summary": summary,
             "report_sha256": sha256_text(summary),
             "blocked": bool(blocked), "reason": reason,
             "kind": kind, "g4_eligible": False}
    if work_item_id:
        entry["work_item_id"] = str(work_item_id).strip()
    if dispatch_id:
        entry["dispatch_id"] = str(dispatch_id).strip()
    if result_sha256:
        entry["result_sha256"] = str(result_sha256).strip()
    if kind == "review":
        entry["review_of"] = review_of
        entry["review_of_kind"] = review_of_kind
        entry["review_source"] = review_source
        entry["verdict"] = verdict
        entry["evidence_id"] = evidence_id
        entry["raw_report_sha256"] = raw_report_sha256
    state["reports"].append(entry)
    return entry


def _invalidate_pass_rounds(state: dict, reason: str) -> list[str]:
    """作废所有显式 PASS 轮（保留轮次与审计，只移除其收口资格）。

    合同实质修改后必须调用：旧 PASS 是对旧合同作出的判断，不能继续驱动
    pass_pending、G2 核销或收口。返回本次新增作废的轮次 id。
    """
    closure = state.setdefault("closure", {})
    invalidated = [str(rid) for rid in closure.get("invalidated_pass_rounds") or []
                   if str(rid or "").strip()]
    added: list[str] = []
    candidates = [str(closure.get("round_id") or "")]
    for rnd in _verified_rounds(state):
        if str(rnd.get("verdict") or "").strip().lower() == "pass":
            candidates.append(str(rnd.get("round_id") or ""))
    for rid in candidates:
        if rid and rid not in invalidated:
            invalidated.append(rid)
            added.append(rid)
    closure["invalidated_pass_rounds"] = invalidated
    closure["gate"] = {"passed": False, "round_id": None,
                       "reasons": [reason[:500]]}
    return added


def amend_criteria(state: dict, criteria: list, *, source: str = "zcode",
                   reason: str = "") -> dict:
    """调整完成条件集合。确认后只能由用户决策修改（并留痕），不得静默漂移。

    实质修改（id/描述集合变化）会：
    * 重算 `contract_sha256`（目标 + 合同正文 + 条件集合），旧合同的 GPT 凭证失效；
    * 把 `criteria_state` 放回 draft、清空 `criteria_sha256` 与确认轮次，条件必须由
      GPT 在新合同下重新审定；
    * 作废旧 PASS 轮（保留历史记录），旧 PASS 不得再用于 pass_pending / G2 / 收口。
    存在未结束调用时拒绝修改，避免「请求按旧合同发出、回复按新合同入账」。
    """
    _assert_open(state, "调整完成条件")
    contract = state["contract"]
    confirmed = contract.get("criteria_state") == "confirmed"
    src = (source or "zcode").strip().lower()
    if src not in ("zcode", "user"):
        raise DispatchError(f"amend 来源 {source!r} 非法；只能是 zcode / user。")
    if confirmed and src != "user":
        raise DispatchError(
            "完成条件已确认（criteria_sha256 已生成）：后续修改必须由用户决策"
            "（--source user --reason ...），主代理不得擅自改变。")
    if confirmed and not (reason or "").strip():
        raise DispatchError("确认后修改完成条件必须说明原因（--reason）。")
    items = _normalize_criteria(criteria)
    if not items:
        raise DispatchError("调整后的完成条件不能为空。")
    old_criteria = list(contract.get("criteria") or [])
    new_contract = _contract_fingerprint(
        state.get("goal") or "", contract.get("done_criteria_text") or "", items)
    if (criteria_digest(items) == criteria_digest(old_criteria)
            and str(state.get("contract_sha256") or "") == new_contract):
        # 条件集合与合同指纹都未变：不改状态、不作废 PASS（避免无意义的重判开销）。
        # 旧版本状态的指纹不含条件集合，会走到下面的重算与失效，作为显式修复路径。
        return contract
    if isinstance(state.get("pending_call"), dict):
        raise DispatchError(
            "存在未结束的调用（pending_call）：先 recover 确定本轮结果，"
            "再修改完成条件；不能按旧合同发出的请求套用新合同。")
    existing = {c["criterion_id"]: c for c in old_criteria}
    merged: list[dict] = []
    for item in items:
        old = existing.get(item["criterion_id"])
        if old and old.get("description") == item["description"]:
            merged.append({**old, "status": old.get("status") or "pending"})
        else:
            merged.append(item)
    previous_contract = str(state.get("contract_sha256") or "")
    contract["history"].append({
        "ts": now_iso(), "source": src, "reason": (reason or "").strip(),
        "previous_criteria": old_criteria,
        "previous_sha256": contract.get("criteria_sha256") or "",
        "previous_state": contract.get("criteria_state") or "draft",
        "previous_confirmed_round_id": contract.get("confirmed_round_id"),
        "previous_contract_sha256": previous_contract,
        "contract_sha256": new_contract,
    })
    contract["criteria"] = merged
    contract["criteria_sha256"] = ""
    contract["criteria_state"] = "draft"
    contract["confirmed_round_id"] = None
    state["contract_sha256"] = new_contract
    _invalidate_pass_rounds(
        state, "contract_amended — 完成条件已修改，需 GPT 按新合同重新判断")
    return contract


# ── 配额状态（唯一归属：结构、迁移、冷却判定都在本模块）───────────────────
def _now_dt() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _patch_quota_state(path: str, mutate) -> dict:
    """在文件锁内重读 → 只替换 quota → 原子写回（不碰轮次/pending/工作项）。

    配额补丁不参与轮次推进，因此不拿 round_seq 做 CAS；但必须在锁内基于磁盘
    **当前**内容改写——否则会用它进程的旧快照覆盖已提交的轮次（丢失更新）。
    """
    target = os.path.abspath(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    lock_path = os.path.join(directory, "." + os.path.basename(target) + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_fh:
        _lock_state_file(lock_fh)
        try:
            try:
                with open(target, "r", encoding="utf-8") as fh:
                    current = json.load(fh)
            except FileNotFoundError:
                raise DispatchError(f"交接状态不存在：{target}") from None
            except (OSError, ValueError) as exc:
                raise DispatchError(f"无法读取交接状态 {target}：{exc}") from None
            if not isinstance(current, dict):
                raise DispatchError(f"交接状态 {target} 不是对象；拒绝写入配额。")
            if bool((current.get("closure") or {}).get("closed")):
                raise DispatchError(
                    "任务已收口（closure.closed=true），拒绝写入配额；"
                    "继续需用户明确授权后执行 `closure --reopen --reason ...`。")
            raw = current.get("quota")
            quota = _merge_defaults({} if raw is None else raw,
                                    _default_quota(), "quota")
            # mutate 同时拿到磁盘当前状态：来源校验（如 --source call 的成功调用
            # 凭证）必须基于锁内最新轮次，不能用进程里的旧快照。
            mutate(quota, current)
            current["quota"] = quota
            current["updated"] = now_iso()
            fd, tmp = tempfile.mkstemp(prefix=".copilot-dispatch-", dir=directory,
                                       text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(current, fh, ensure_ascii=False, indent=2, sort_keys=True)
                    fh.write("\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, target)
                _fsync_dir(directory)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        finally:
            _unlock_state_file(lock_fh)
    return current["quota"]


def record_quota(path: str, signal: dict, call_id: str = "") -> dict:
    """记录配额/冷却信号：只 patch quota 节，不碰轮次、pending 与工作项。

    重复信号取较晚的 reset_at（避免后到的短窗口覆盖先前更长的等待）。
    `reset_seconds` 缺失时 reset_at 为 None：冷却结束时间未知，只有人工确认才能清除。
    """
    now = _now_dt()
    reset_seconds = signal.get("reset_seconds") if isinstance(signal, dict) else None
    if not isinstance(reset_seconds, int) or isinstance(reset_seconds, bool) \
            or reset_seconds < 0:
        reset_seconds = None
    reset_at = _iso(now + timedelta(seconds=reset_seconds)) \
        if reset_seconds is not None else None
    code = str((signal or {}).get("code") or "") or None
    last_call = (call_id or "").strip() or None

    def mutate(quota: dict, _current: dict) -> None:
        merged = reset_at
        if merged and quota.get("reset_at"):
            try:
                merged = _iso(max(datetime.fromisoformat(quota["reset_at"]),
                                  datetime.fromisoformat(merged)))
            except (ValueError, TypeError):
                pass
        quota.update({
            "state": "waiting",
            "detected_at": _iso(now),
            # 新信号没有可解析的 reset 时，reset_at 必须是 None（未知冷却），
            # 不能继承旧值：旧的 reset_at 可能早已过期或已被 clear，继承会让
            # is_quota_blocked 误判为「冷却已结束」而放行下一次付费调用。
            "reset_at": merged,
            "reset_seconds": reset_seconds,
            "code": code,
            "last_call_id": last_call,
            "cleared_at": None,
            "clear_reason": None,
        })

    return _patch_quota_state(path, mutate)


def clear_quota(path: str, reason: str, source: str = "user",
                call_id: str = "") -> dict:
    """清除配额等待：必须给出原因（用户确认或成功调用自证）。

    `--source call` 不是一句自我声明：必须给出 `--call-id`，且该调用必须已作为
    通过身份验证的轮次记录在本状态中，并且发生在配额检测之后。否则任何人都能用
    一个 CLI 参数把 waiting 改回 ok，绕过「冷却结束并核实后再发起」的纪律。
    """
    reason = (reason or "").strip()
    if not reason:
        raise DispatchError("清除配额等待必须说明原因（--reason）。")
    src = (source or "user").strip().lower()
    if src not in ("user", "call"):
        raise DispatchError(f"清除来源 {source!r} 非法；只能是 user / call。")
    cid = (call_id or "").strip()
    if src == "call" and not cid:
        raise DispatchError(
            "--source call 必须同时提供 --call-id：用一次已入账的成功调用自证恢复，"
            "不能只凭来源参数清掉冷却。")
    cleared_at = _iso(_now_dt())

    def mutate(quota: dict, current: dict) -> None:
        if src == "call":
            round_rec = None
            for rnd in current.get("rounds") or []:
                if (isinstance(rnd, dict)
                        and str(rnd.get("call_id") or "") == cid):
                    round_rec = rnd
                    break
            if round_rec is None or round_rec.get("model_identity_ok") is not True:
                raise DispatchError(
                    f"调用 {cid} 不是本状态中已入账且通过身份验证的轮次；"
                    "不能用它证明配额已恢复。")
            detected = str(quota.get("detected_at") or "").strip()
            called_at = str(round_rec.get("ts") or "").strip()
            if not detected:
                # 没有检测时间就无法证明调用发生在冷却之后；不能把「未知」当「无需比较」。
                raise DispatchError(
                    f"配额状态缺少检测时间，无法证明调用 {cid} 发生在冷却之后；"
                    "拒绝以调用自证清除。")
            try:
                if datetime.fromisoformat(called_at) < datetime.fromisoformat(detected):
                    raise DispatchError(
                        f"调用 {cid} 早于配额检测时间 {detected}；"
                        "不能作为冷却恢复的证据。")
            except (ValueError, TypeError):
                raise DispatchError(
                    f"无法比对调用 {cid} 与配额检测时间；拒绝以调用自证清除。") from None
        quota.update({"state": "ok", "cleared_at": cleared_at,
                      "clear_reason": f"{src}: {reason}"[:500]})

    return _patch_quota_state(path, mutate)


def is_quota_blocked(state: dict, now: datetime | None = None) -> bool:
    """冷却是否仍阻塞新调用（唯一判定）。reset_at 未知时保守视为仍阻塞。"""
    quota = state.get("quota") or {}
    if str(quota.get("state") or "ok") != "waiting":
        return False
    reset_at = str(quota.get("reset_at") or "").strip()
    if not reset_at:
        return True
    try:
        return (now or _now_dt()) < datetime.fromisoformat(reset_at)
    except (ValueError, TypeError):
        # 时间串无法解析或时区不一致：保守视为仍在冷却，绝不因格式问题放行调用。
        return True


# ── PASS 挂起 / 四门核销 / 收口 / 阶段机（阶段 2 §1、§2.4）─────────────────
def _invalidated_pass_rounds(state: dict) -> set[str]:
    closure = state.get("closure") or {}
    return {str(rid) for rid in closure.get("invalidated_pass_rounds") or []
            if str(rid or "").strip()}


def _latest_pass_round(state: dict) -> dict | None:
    """最近一个显式 verdict=pass 的已验证轮次（只看 GPT 明示值，不接受推断）。"""
    invalid = _invalidated_pass_rounds(state)
    for rnd in reversed(_verified_rounds(state)):
        if str(rnd.get("round_id") or "") in invalid:
            continue
        if str(rnd.get("verdict") or "").strip().lower() == "pass":
            return rnd
    return None


def pass_pending(state: dict) -> bool:
    """PASS 已拿到但尚未收口：此期间不执行普通工作项。

    只认最近一个**带判断**的已验证轮的显式 verdict：缺失 verdict 的旧回复、
    无安排块的咨询轮与被拒绝的结构违规轮都不改变控制状态。
    """
    if bool((state.get("closure") or {}).get("closed")):
        return False
    invalid = _invalidated_pass_rounds(state)
    for rnd in reversed(_verified_rounds(state)):
        if str(rnd.get("round_id") or "") in invalid:
            continue
        if rnd.get("rejected") or not rnd.get("decision"):
            continue
        return str(rnd.get("verdict") or "").strip().lower() == "pass"
    return False


def _gate_actions(state: dict) -> dict:
    """G1：全部工作项终态；dropped 必须有原因。"""
    reasons = []
    for action in state.get("actions") or []:
        status = str(action.get("status") or "")
        if status not in TERMINAL:
            reasons.append({"code": "action_open",
                            "detail": f"{action.get('id')} 仍为 {status}"})
        elif status == "dropped" and not str(action.get("result") or "").strip():
            reasons.append({"code": "dropped_without_reason",
                            "detail": f"{action.get('id')} 已放弃但未说明原因"})
    return {"passed": not reasons, "reasons": reasons}


def _gate_criteria(state: dict, round_id: str) -> dict:
    """G2：逐条核销（8 项本地检查）。

    与 v2.1 §2.4 的差异：**不做 evidence_stale 时效检查**——按本轮补充约束，
    证据只需属于当前任务、摘要一致、可读，时效性由条件自身判断，不要求晚于合同创建。
    """
    contract = state.get("contract") or {}
    criteria = contract.get("criteria") or []
    reasons = []
    # 合同指纹漂移：旧版本状态或未走 amend 的合同/条件改动。此时任何旧 PASS 都是
    # 对旧合同作出的判断，不能驱动 G2 核销或收口，必须先重算指纹并重新确认/判断。
    expected_contract = _contract_fingerprint(
        state.get("goal") or "", contract.get("done_criteria_text") or "", criteria)
    if str(state.get("contract_sha256") or "") != expected_contract:
        reasons.append({
            "code": "contract_fingerprint_mismatch",
            "detail": "合同指纹与当前目标/合同正文/完成条件不一致；"
                      "旧 PASS 不适用于当前合同，需重新确认条件并重新判断"})
    if not round_id:
        reasons.append({"code": "no_pass_round",
                        "detail": "没有可核销的 PASS 轮次"})
    if not criteria:
        reasons.append({"code": "criteria_missing",
                        "detail": "完成条件为空，无法逐条核销"})
    expected = criteria_digest(criteria) if criteria else ""
    if contract.get("criteria_state") != "confirmed":
        reasons.append({"code": "criteria_unconfirmed",
                        "detail": "完成条件尚未由 GPT 确认，不能作为 PASS 依据"})
    elif str(contract.get("criteria_sha256") or "") != expected:
        reasons.append({"code": "criteria_drift",
                        "detail": "完成条件集合与确认时的 criteria_sha256 不一致"})
    evidence = {str(e.get("id") or ""): e for e in state.get("evidence") or []}
    task_id = str(state.get("task_id") or "")
    for criterion in criteria:
        cid = str(criterion.get("criterion_id") or "")
        status = str(criterion.get("status") or "")
        if status != "met":
            reasons.append({
                "code": "criterion_pending" if status == "pending" else "criterion_not_met",
                "detail": f"{cid} 当前状态 {status}"})
            continue
        refs = criterion.get("evidence_refs") or []
        if not refs:
            reasons.append({"code": "criterion_without_evidence",
                            "detail": f"{cid} 判定为 met 但没有证据引用"})
            continue
        for ref in refs:
            eid = str(ref.get("id") or "")
            entry = evidence.get(eid)
            if entry is None or entry.get("dropped"):
                reasons.append({"code": "evidence_missing",
                                "detail": f"{cid} 引用的 {eid} 未登记或已归档"})
                continue
            if str(entry.get("task_id") or "") != task_id:
                reasons.append({"code": "evidence_foreign",
                                "detail": f"{eid} 不属于任务 {task_id}"})
            if str(ref.get("sha256") or "") != str(entry.get("sha256") or ""):
                reasons.append({"code": "evidence_sha_changed",
                                "detail": f"{cid} 引用的 {eid} 摘要与登记不一致"})
            hint = str(entry.get("path_hint") or "")
            if hint and hint != "<inline>":
                if not os.path.exists(hint):
                    reasons.append({"code": "evidence_file_missing",
                                    "detail": f"{eid} 证据文件已不存在：{hint}"})
                    continue
                try:
                    if sha256_file(hint) != str(entry.get("sha256") or ""):
                        reasons.append({"code": "evidence_tampered",
                                        "detail": f"{eid} 文件内容已被改动"})
                except OSError as exc:
                    reasons.append({"code": "evidence_unreadable",
                                    "detail": f"{eid} 无法重读：{exc}"})
        if str(criterion.get("updated_round_id") or "") != round_id:
            reasons.append({
                "code": "criterion_stale_judgment",
                "detail": f"{cid} 的 met 不是本 PASS 轮（{round_id}）作出的"})
    return {"passed": not reasons, "reasons": reasons}


def _gate_stalled(state: dict) -> dict:
    """G3：没有 running/failed/blocked 或依赖不可满足的挂置项。"""
    reasons = [{"code": "stalled",
                "detail": f"{action.get('id')}：{reason}"}
               for action, reason in stalled_actions(state)]
    return {"passed": not reasons, "reasons": reasons}


def _gate_review(state: dict) -> dict:
    """G4：高风险任务需要原生独立复核记录；当前阶段 fail-closed。

    ZCode 3.17.1 尚无宿主可信绑定 task_id / contract_sha256 / round_id / 证据的
    原生复核记录入口。因此本阶段不实现 `--native-review`，也不把任何调用者可写
    的 JSON 当成原生复核证据；高风险任务一律以 `native_review_missing` 失败，
    直到宿主能力或经授权的正式记录机制接入。

    GPT REVIEW 台账（`--review-ledger`）仅用于审计，不是 G4 依据。即使台账轮次
    的 `verdict=PASS`、`model_identity_ok=true`、`task_id` 与 `contract_sha256`
    全部匹配，也不能通过 G4；普通 GPT REVIEW、PLAN 的 REVIEW 辅助调用、手写
    同构 JSON 同样不能通过。低风险任务保持自动通过。

    `risk` 按与 `start()` 相同的口径归一化（去空白、转小写）；缺失或非法取值
    不能当成普通风险放行，按 fail-closed 拒绝（v1 状态由 `_upgrade_state` 显式
    补为 normal，兼容旧任务）。
    """
    raw = state.get("risk")
    risk = raw.strip().lower() if isinstance(raw, str) else ""
    if risk == "normal":
        return {"passed": True, "reasons": []}
    if risk != "high":
        return {"passed": False, "reasons": [{
            "code": "native_review_missing",
            "detail": "任务 risk 缺失或非法，无法确认是否需要原生独立复核；G4 按 "
                      "fail-closed 拒绝。应在 start 时显式声明 normal/high，不得私改状态降级。",
        }]}
    return {"passed": False, "reasons": [{
        "code": "native_review_missing",
        "detail": "高风险任务需要原生独立复核记录；当前 ZCode 尚无宿主可信绑定 "
                  "task_id/contract_sha256/round_id/证据的原生复核入口，G4 按 "
                  "fail-closed 拒绝。GPT REVIEW 台账仅用于审计，不能作为 G4 依据。",
    }]}


def closure_check(state: dict, review_ledger: str = "",
                  state_path: str = "") -> dict:
    """只读四门核销：G1 工作项 / G2 逐条条件 / G3 停摆 / G4 原生复核。

    `review_ledger` 与 `state_path` 保留仅为兼容旧调用；G4 不读取 GPT REVIEW
    台账，传入任何台账都不会改变判定。
    """
    pass_round = _latest_pass_round(state)
    round_id = str((pass_round or {}).get("round_id") or "")
    gates = {
        "G1": _gate_actions(state),
        "G2": _gate_criteria(state, round_id),
        "G3": _gate_stalled(state),
        "G4": _gate_review(state),
    }
    reasons = []
    for name in ("G1", "G2", "G3", "G4"):
        for reason in gates[name]["reasons"]:
            reasons.append({"gate": name, **reason})
    return {"passed": not reasons, "round_id": round_id,
            "gates": gates, "reasons": reasons}


def _gate_summary(check: dict) -> list[str]:
    return [f"{reason.get('gate')}:{reason.get('code')} — {reason.get('detail')}"
            for reason in check.get("reasons") or []]


def _persist_gate(path: str, state: dict, check: dict) -> bool:
    """把四门结果写入 closure.gate（只在此处与 close 时写；closure-check 本身只读）。"""
    gate = {"passed": bool(check["passed"]), "round_id": check.get("round_id") or None,
            "reasons": _gate_summary(check)}
    review_round = (check.get("gates") or {}).get("G4", {}).get("round")
    if review_round:
        # 当前阶段 G4 不产生原生复核轮次；保留该分支供未来可信原生记录接入。
        gate["review"] = review_round
    current = (state.get("closure") or {}).get("gate") or {}
    if (current.get("passed") == gate["passed"]
            and current.get("round_id") == gate["round_id"]
            and (current.get("reasons") or []) == gate["reasons"]
            and current.get("review") == gate.get("review")):
        return False
    state["closure"]["gate"] = gate
    save_state(path, state, expect={"round_seq": state.get("round_seq", 0),
                                    "pending_call_id": ""})
    return True


def _check_close_preconditions(state: dict) -> None:
    if bool((state.get("closure") or {}).get("closed")):
        raise DispatchError("任务已收口（closure.closed=true）；如需继续请先 `closure --reopen`。")
    if isinstance(state.get("pending_call"), dict):
        raise DispatchError(
            "存在未结束的调用（pending_call）：先 recover 确定结果，再收口。")


def close_task(path: str, round_id: str = "", *, force: bool = False,
               reason: str = "", review_ledger: str = "") -> dict:
    """收口：普通路径要求最近 PASS 轮 + 四门通过；force 只记录用户终止。"""
    state = load_state(path)
    _check_close_preconditions(state)
    closure = state["closure"]
    reason = (reason or "").strip()
    if force:
        if not reason:
            raise DispatchError("--force-close 必须给出用户终止原因（--reason）。")
        closure.update({
            "closed": True, "closed_by": "user", "closed_at": now_iso(),
            # 用户终止不是 PASS：不继承可能存在的旧 verdict/round_id，避免把
            # force-close 记成 verdict=pass 的收口。
            "verdict": None, "round_id": None,
            "gate": {"passed": None, "round_id": None, "reasons": [],
                     "skipped_by_user": True, "reason": reason[:500]},
        })
        save_state(path, state, expect={"round_seq": state.get("round_seq", 0),
                                        "pending_call_id": ""})
        return closure
    pass_round = _latest_pass_round(state)
    if pass_round is None:
        raise DispatchError(
            "最近已验证轮次没有 verdict=pass：不能收口；主代理不得把 PARTIAL 改成 PASS。")
    if not pass_pending(state):
        # PASS 之后又出现了新的已验证判断（例如重新判断改判 partial）：旧的 PASS
        # 已被覆盖，不能再拿它收口。
        raise DispatchError(
            "最近一次已验证判断不是 verdict=pass（PASS 已被后续判断覆盖）：不能收口。")
    rid = (round_id or str(pass_round.get("round_id") or "")).strip()
    if rid != str(pass_round.get("round_id") or ""):
        raise DispatchError(f"轮次 {rid} 不是最近 PASS 轮次"
                            f"（{pass_round.get('round_id')}）；不能收口。")
    # native 模式的额外收口前置条件：Reviewer 流程必须完成且最新复核可放行。
    # 这不是 G4、也不产生 G4 凭证；G1–G4 定义保持不变。放在四门记录检查之前，
    # 保证「PASS 已拿到但复核未完成」总是得到明确的复核原因，而不是笼统要求先跑
    # loop-step。
    review = native_review_status(state)
    if not review["passed"]:
        raise DispatchError(
            "native 复核流程未完成或未通过，拒绝收口："
            + "；".join(f"{item.get('code')}: {item.get('detail')}"
                        for item in review["reasons"])
            + "（复核记录不构成 G4 凭证；PASS 不能绕过 Reviewer。）")
    stored = closure.get("gate") or {}
    if stored.get("passed") is not True or stored.get("round_id") != rid:
        if stored.get("round_id") == rid and stored.get("passed") is False:
            raise DispatchError(
                "四门未通过，拒绝收口：" + "；".join(stored.get("reasons") or []))
        raise DispatchError(
            "缺少本轮四门通过记录：先运行 loop-step（closure-check 只读，不落四门记录），"
            "四门通过后才能收口。")
    check = closure_check(state, review_ledger, path)
    if not check["passed"]:
        raise DispatchError("四门未通过，拒绝收口：" + "；".join(_gate_summary(check)))
    gate_record = {"passed": True, "round_id": rid, "reasons": []}
    review_round = (check.get("gates") or {}).get("G4", {}).get("round")
    if review_round:
        gate_record["review"] = review_round
    closure.update({
        "closed": True, "closed_by": "gate", "verdict": "pass", "round_id": rid,
        "closed_at": now_iso(),
        "gate": gate_record,
    })
    save_state(path, state, expect={"round_seq": state.get("round_seq", 0),
                                    "pending_call_id": ""})
    return closure


def reopen_task(path: str, reason: str) -> dict:
    """用户授权重开：记 reopen_history，回到未收口（不产生 PASS）。"""
    reason = (reason or "").strip()
    if not reason:
        raise DispatchError("重开任务必须给出原因（--reason），并确认是用户授权。")
    state = load_state(path)
    closure = state["closure"]
    if not closure.get("closed"):
        raise DispatchError("任务尚未收口，无需 reopen。")
    # 重开意味着用户要回到「继续工作」：旧 PASS 立即作废，不得靠它再次收口，
    # 也不得让 pass_pending 把执行面锁死。轮次本身保留在历史里。
    invalidated = list(closure.get("invalidated_pass_rounds") or [])
    candidates = [str(closure.get("round_id") or "")]
    # 重开后不得再有任何历史 PASS 驱动阶段机：全部显式 PASS 轮一并作废。
    for rnd in _verified_rounds(state):
        if str(rnd.get("verdict") or "").strip().lower() == "pass":
            candidates.append(str(rnd.get("round_id") or ""))
    for rid in candidates:
        if rid and rid not in invalidated:
            invalidated.append(rid)
    closure["invalidated_pass_rounds"] = invalidated
    closure.setdefault("reopen_history", []).append({
        "ts": now_iso(), "reason": reason[:500],
        "closed_at": closure.get("closed_at"), "closed_by": closure.get("closed_by"),
        "verdict": closure.get("verdict"),
    })
    closure.update({
        "closed": False, "closed_by": None, "verdict": None, "round_id": None,
        "closed_at": None,
        "gate": {"passed": False, "round_id": None,
                 "reasons": [f"reopened — {reason[:200]}"]},
    })
    save_state(path, state, expect={"round_seq": state.get("round_seq", 0),
                                    "pending_call_id": ""})
    return closure


def _next_commands(phase: str, state: dict, path: str,
                   gate: dict | None) -> list[str]:
    quota = state.get("quota") or {}
    # GPT REVIEW 台账只用于审计，不进入 G4；收口命令不得携带它，避免生成
    # 「用台账完成独立复核」的误导性下一步。
    if phase == "CLOSED":
        return [f"copilot_dispatch.py closure --state {path} --reopen --reason <用户授权原因>"]
    if phase == "RECOVER":
        return [f"copilot_dispatch.py recover --state {path} --audit-log <审计日志>"]
    if phase == "BLOCKED":
        return [f"copilot_dispatch.py decide --state {path} --text <用户决策>",
                f"copilot_dispatch.py brief --state {path}"]
    if phase == "QUOTA_WAIT":
        if gate and gate.get("passed"):
            return [f"copilot_dispatch.py closure --state {path} --close "
                    f"--round-id {gate.get('round_id')}"]
        if not quota.get("reset_at"):
            return ["配额恢复时间未知：先人工核实额度，再 "
                    f"copilot_dispatch.py quota --state {path} --clear --reason <依据>"]
        return ["等待冷却到点后先核实额度；期间不调用 GPT、不执行工作项。"]
    if phase == "CLOSURE_CHECK":
        rid = (gate or {}).get("round_id") or ""
        return [f"copilot_dispatch.py closure --state {path} --close "
                f"--round-id {rid}"]
    if phase == "REVIEW_PENDING":
        unreviewed = native_review_status(state).get("unreviewed") or []
        ids = ", ".join(str(item.get("work_item_id") or "?") for item in unreviewed)
        return [f"启动独立 Native Reviewer（reviewer Profile，只读）复核 {ids or '<work_item>'}",
                "native_agent.py review ... 生成复核信封",
                f"copilot_auto.py ingest --state {path} --native-result <review.json>",
                f"复核回填后再显式运行 copilot_auto.py drive --state {path} 让 SOL 判断"]
    if phase == "REJUDGE":
        return [f"copilot_dispatch.py report --state {path} --blocked "
                "--reason <四门失败原因码与缺口>",
                f"call_gpt.py ... --dispatch-state {path}（重新判断：partial/补证/blocked）"]
    if phase == "EXECUTE":
        if state.get("execution_backend") == "native":
            return [f"copilot_auto.py ingest --state {path} "
                    "--native-result <execution-result.json>",
                    f"copilot_auto.py ingest --state {path} "
                    "--native-result <review-result.json>",
                    "执行/复核结果必须分开回填（ingest 纯本地、0 请求）；不得用 "
                    "--result-file 混入执行方自报。"]
        return [f"copilot_dispatch.py next --state {path}",
                f"copilot_dispatch.py record --state {path} --id <T> --status <s> ..."]
    return [f"copilot_dispatch.py brief --state {path} --prompt-file <本轮问题>",
            f"call_gpt.py ... --dispatch-state {path}"]


def _native_review_gap(check: dict) -> bool:
    """四门失败是否由「缺少原生独立复核记录」这一外部能力缺口导致。"""
    return any(
        str(reason.get("gate") or "") == "G4"
        and str(reason.get("code") or "") == "native_review_missing"
        for reason in check.get("reasons") or [])


# ── native 预算（请求数 / Token）与复核完成度 ──────────────────────────────
def _budget_int(value: object, name: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise DispatchError(f"{name} 必须是正整数或留空。")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise DispatchError(f"{name}={value!r} 不是整数。") from None
    if number <= 0:
        raise DispatchError(f"{name} 必须是正整数或留空。")
    return number


def budget_limits(state: dict) -> dict:
    """解析本任务的请求/Token 用量上限。

    2026-11-08 起默认**不设上限**：`max_requests` / `max_total_tokens` 为 None 表示
    「不因调用次数或累计费用阻止正常调用」，用量仍从审计重建并写入 `budget.usage`
    供观察。只有用户在 `start` 时显式给出上限（或历史状态里显式保存了数值）才生效，
    此时 `enforced=True` 且报告为显式运营上限，而不是代码默认阈值。
    """
    budget = state.get("budget") or {}
    backend = str(state.get("execution_backend") or "legacy")
    max_requests = _budget_int(budget.get("max_requests"), "budget.max_requests")
    max_tokens = _budget_int(budget.get("max_total_tokens"), "budget.max_total_tokens")
    projection = budget.get("projection")
    if projection is None:
        projection = NATIVE_BUDGET_PROJECTION
    enforced = max_requests is not None or max_tokens is not None
    return {"backend": backend,
            "applicable": enforced,
            "enforced": enforced,
            "explicit": enforced,
            "default_unbounded": True,
            "max_requests": max_requests,
            "max_total_tokens": max_tokens,
            "projection": bool(projection)}


def _usage_numbers(usage: object) -> dict:
    """从一次响应的 usage 取输入/输出/总量；缺失按 0 计，不猜测。"""
    data = usage if isinstance(usage, dict) else {}

    def num(*keys: str) -> int:
        for key in keys:
            value = data.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return 0

    input_tokens = num("input_tokens", "prompt_tokens")
    output_tokens = num("output_tokens", "completion_tokens")
    total = num("total_tokens")
    if not total:
        total = input_tokens + output_tokens
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": total}


def task_spend_from_audit(audit_log: str, task_id: str) -> dict:
    """从审计日志重建本任务的真实请求数与 usage（跨进程、重启不丢）。

    `call_requested` 是发送前落盘的记录：即使进程中途被杀，也算一次请求尝试。
    只认带 task_id 的记录——无任务级凭证的手工调用不计入任务预算。

    usage 分两类，不能把「未知」当成 0：
    - 有 usage（含失败/截断/身份不符但上游确实回了 usage）：计入真实 Token；
    - 无 usage 但请求已送达或可能送达（HTTP 响应、超时、连接中断、200 但结构
      异常）：计入 `unknown_usage_calls`，不伪造 Token；
    - 能确认未送达的传输层失败（拒绝连接/解析失败/不可达）不计入未知上游用量。
    `failed_calls` 等状态计数与 Token 统计分开保留，便于审计失败请求本身。
    """
    spend = {"requests": 0, "input_tokens": 0, "output_tokens": 0,
             "total_tokens": 0, "unknown_usage_calls": 0,
             "known_usage_calls": 0, "failed_calls": 0,
             "failed_with_usage_calls": 0, "failed_unknown_usage_calls": 0,
             "not_delivered_calls": 0,
             "last_call_total": 0, "last_call_id": ""}
    task_id = str(task_id or "").strip()
    if not task_id or not audit_log or not os.path.exists(audit_log):
        return spend
    seen_responses: set[tuple[str, str, str]] = set()
    for record in _audit_records(audit_log):
        if str(record.get("task_id") or "") != task_id:
            continue
        event = str(record.get("event") or "")
        if event == "call_requested":
            spend["requests"] += 1
        elif event == "call_result":
            numbers = _usage_numbers(record.get("usage"))
            ok = record.get("ok") is True
            call_id = str(record.get("call_id") or "")
            response_id = str(record.get("response_id") or "")
            if response_id:
                # 同一条上游响应可能因重试/恢复被重复写入；按响应身份去重，
                # 但不用它合并不同请求（避免把重试用量算少）。
                key = (call_id, str(record.get("output_escalation_index") or ""),
                       response_id)
                if key in seen_responses:
                    continue
                seen_responses.add(key)
            if not ok:
                spend["failed_calls"] += 1
            if numbers["total_tokens"]:
                for key in ("input_tokens", "output_tokens", "total_tokens"):
                    spend[key] += numbers[key]
                spend["known_usage_calls"] += 1
                if not ok:
                    spend["failed_with_usage_calls"] += 1
                spend["last_call_total"] = numbers["total_tokens"]
                spend["last_call_id"] = call_id
                continue
            # 无 usage：先判断请求是否可能已送达上游。已知字段优先；
            # 旧记录没有这些字段时，HTTP 200 / response_id / 非本地错误码
            # 一律按「可能送达」保守计入未知，绝不当作 0。
            delivered = record.get("request_may_have_been_delivered")
            if delivered is None:
                status = record.get("http_status")
                delivered = bool(record.get("http_response_received")
                                 or (isinstance(status, int) and status > 0)
                                 or response_id)
            if ok:
                spend["unknown_usage_calls"] += 1
            elif delivered:
                spend["unknown_usage_calls"] += 1
                spend["failed_unknown_usage_calls"] += 1
            else:
                spend["not_delivered_calls"] += 1
    return spend


def budget_status(state: dict, audit_log: str = "") -> dict:
    """请求/Token 用量状态（发送前判定）。

    2026-11-08 起**默认 `allowed=True`**：请求数与累计 Token 只记录、不阻断，费用数据
    用于审计与观察。只有显式配置的运营上限被触及时才 `allowed=False`：
    - request_limit_reached：显式 `max_requests` 已达到（出站前可精确判定）；
    - token_budget_reached：已上报 usage 达到显式阈值（单次调用后才可知）；
    - token_budget_projected：按上一次调用总量预估将越过显式阈值（估计值）。
    服务商真实的限额、配额与限流仍由上游错误码处理，不由这里放行。
    """
    limits = budget_limits(state)
    spend = task_spend_from_audit(audit_log, str(state.get("task_id") or ""))
    projected = spend["total_tokens"] + spend["last_call_total"]
    allowed = True
    reason = ""
    detail = ""
    if limits["max_requests"] is not None and spend["requests"] >= limits["max_requests"]:
        allowed = False
        reason = "request_limit_reached"
        detail = (f"本任务已发起 {spend['requests']} 次请求，达到**显式运营上限** "
                  f"{limits['max_requests']}；拒绝继续出站。"
                  "该上限由调用方显式设置，不是代码默认值。")
    elif (limits["max_total_tokens"] is not None
          and spend["total_tokens"] >= limits["max_total_tokens"]):
        allowed = False
        reason = "token_budget_reached"
        detail = (f"已上报累计 {spend['total_tokens']} Token，达到**显式运营阈值** "
                  f"{limits['max_total_tokens']}；拒绝继续出站"
                  f"（单次调用可能使总量最多超出一次调用的量）。")
    elif (limits["projection"] and limits["max_total_tokens"] is not None
          and spend["last_call_total"]
          and projected > limits["max_total_tokens"]):
        allowed = False
        reason = "token_budget_projected"
        detail = (f"按上一次调用 {spend['last_call_total']} Token 预估累计将达 "
                  f"{projected}，超过**显式运营阈值** {limits['max_total_tokens']}；"
                  f"这是保守估计，不是严格上界。")
    return {"allowed": allowed, "reason": reason, "detail": detail,
            "limits": limits, "spend": spend, "projected_total": projected,
            "limits_enforced": limits["enforced"],
            "unbounded_default": True}


def refresh_budget_usage(state: dict, audit_log: str = "") -> dict:
    """按审计重算 usage 并写入 budget.usage（审计仍是权威来源）。"""
    spend = task_spend_from_audit(audit_log, str(state.get("task_id") or ""))
    budget = state.setdefault("budget", _default_budget())
    budget["usage"] = {**spend, "updated_at": now_iso()}
    return spend


def native_review_status(state: dict) -> dict:
    """native 复核完成度：执行结果是否都有绑定复核、最新复核是否可放行。

    这是 native 模式的**收口前置条件**，不是 G4：G1–G4 的定义保持不变，复核
    记录也不构成 G4 凭证（`g4_eligible` 恒为 false）。旧复核的 blocking 由后续
    执行 + 复核轮次体现；只看最新执行绑定的复核，避免历史问题永久锁死收口。
    """
    backend = str(state.get("execution_backend") or "legacy")
    if backend != "native":
        return {"applicable": False, "passed": True, "reasons": [],
                "executions": 0, "unreviewed": [], "latest_review": None}
    executions = [r for r in state.get("native_results") or []
                  if isinstance(r, dict) and str(r.get("kind") or "") == "execution"]
    if not executions:
        return {"applicable": True, "passed": True, "reasons": [],
                "executions": 0, "unreviewed": [], "latest_review": None}
    reviews: dict[str, dict] = {}
    for review in state.get("native_reviews") or []:
        if not isinstance(review, dict):
            continue
        sha = str(review.get("execution_result_sha256") or "")
        if sha:
            reviews[sha] = review
    unreviewed = []
    for execution in executions:
        sha = str(execution.get("result_sha256") or "")
        if sha and sha not in reviews:
            unreviewed.append({"execution_result_sha256": sha,
                               "work_item_id": execution.get("work_item_id"),
                               "round_id": execution.get("round_id")})
    reasons = []
    if unreviewed:
        reasons.append({
            "code": "native_review_missing",
            "detail": ("以下执行结果尚未回填独立复核："
                       + ", ".join(str(item.get("work_item_id") or "?")
                                   for item in unreviewed)),
        })
    latest = None
    latest_exec_sha = str(executions[-1].get("result_sha256") or "")
    if latest_exec_sha:
        latest = reviews.get(latest_exec_sha)
    if latest is not None:
        findings = [item for item in latest.get("findings") or []
                    if isinstance(item, dict) and item.get("severity") == "blocking"]
        verdict = str(latest.get("verdict") or "").strip().lower()
        if verdict != "pass" or findings:
            summary = "; ".join(str(item.get("summary") or item.get("detail") or "")
                                for item in findings) or "无 blocking 明细"
            reasons.append({
                "code": "native_review_blocking",
                "detail": (f"最新复核 {latest.get('work_item_id') or '?'} "
                           f"verdict={verdict or '?'}：{summary}"),
            })
    return {"applicable": True, "passed": not reasons, "reasons": reasons,
            "executions": len(executions), "unreviewed": unreviewed,
            "latest_review": latest}


def loop_step(path: str, review_ledger: str = "", *, stamp: bool = False,
              prompt: str = "", persist: bool = True) -> dict:
    """八阶段求值：CLOSED > RECOVER > BLOCKED > QUOTA_WAIT > CLOSURE_CHECK >
    REJUDGE > EXECUTE > GUIDANCE_NEEDED。

    本轮补充约束：配额冷却期间若存在 pass_pending，仍优先做**本地**四门核销
    （通过即可直接收口）；四门失败需要在冷却期重新判断时，停在 QUOTA_WAIT，
    不调用 GPT、不执行普通工作项。

    高风险任务缺少原生独立复核记录时，G4 返回 `native_review_missing`；这是
    外部能力缺口，阶段直接停在 BLOCKED，不进入 REJUDGE，也不生成 GPT 复核命令。
    """
    if stamp and persist:
        stamp_progress(path, prompt)
    state = load_state(path)
    quota_blocked = is_quota_blocked(state)
    pending = state.get("pending_call")
    pending_info = None
    if isinstance(pending, dict):
        pending_info = {"call_id": pending.get("call_id"),
                        "round_id": pending.get("round_id")}
    pp = pass_pending(state)
    review = native_review_status(state)
    gate = None
    notes: list[str] = []
    if bool((state.get("closure") or {}).get("closed")):
        phase = "CLOSED"
    elif isinstance(pending, dict):
        phase = "RECOVER"
    elif _effective_decision(state) == "blocked":
        phase = "BLOCKED"
    elif review["applicable"] and review["unreviewed"]:
        # 执行已回填但复核未回填：先完成复核流程，再开放新的执行/指导。
        # 该阶段只做本地回填，不受配额冷却影响（不调用 GPT）。
        phase = "REVIEW_PENDING"
        notes.append("native 执行结果尚未回填独立复核；回填前不进行最终判断/收口，"
                     "也不开放新的工作项。")
    elif quota_blocked and not pp:
        phase = "QUOTA_WAIT"
    elif pp:
        check = closure_check(state, review_ledger, path)
        if persist:
            if _persist_gate(path, state, check):
                state = load_state(path)
            gate = (state.get("closure") or {}).get("gate")
        else:
            # 只读求值（自动驱动 --dry-run）：不落四门结果，也不因门禁结果改状态。
            gate = check
        if not review["passed"]:
            # 复核存在 blocking finding：PASS 不能绕过，交给 SOL 依据具体问题决定
            # 修复还是继续。冷却期不发起重新判断，保持 QUOTA_WAIT。
            if quota_blocked:
                phase = "QUOTA_WAIT"
                notes.append("native 复核存在 blocking finding；配额冷却期间不发起重新判断。")
            else:
                phase = "REJUDGE"
                notes.append("native 复核存在 blocking finding；PASS 不能绕过，"
                             "需重新判断修复或继续。")
        elif check["passed"]:
            phase = "CLOSURE_CHECK"
        elif _native_review_gap(check):
            # G4 缺少原生复核属于宿主能力缺口，不是 GPT 再判一次能解决的问题；
            # 停在 BLOCKED，避免进入无意义的 REJUDGE/付费调用循环。
            phase = "BLOCKED"
            notes.append("G4 缺少原生独立复核记录；这是外部能力缺口，停在 BLOCKED，"
                         "不进入 REJUDGE，也不要求 GPT 提供独立复核。")
        elif quota_blocked:
            phase = "QUOTA_WAIT"
            notes.append("配额冷却期间不发起重新判断；四门失败原因已记录，"
                         "冷却结束并核实后再 call。")
            notes.append("PASS 挂起期间不得执行普通工作项。")
        else:
            phase = "REJUDGE"
    elif ready_actions(state):
        phase = "EXECUTE"
    else:
        phase = "GUIDANCE_NEEDED"
    budget = state.get("budget") or {}
    seq = int(state.get("round_seq") or 0)
    checkpoint_every = int(budget.get("checkpoint_every") or CHECKPOINT_EVERY)
    criteria = (state.get("contract") or {}).get("criteria") or []
    ready = ready_actions(state)
    stalled = stalled_actions(state)
    return {
        "phase": phase,
        "pass_pending": pp,
        "revision": seq,
        "checkpoint": seq > 0 and seq % checkpoint_every == 0,
        "no_progress": int(budget.get("progress_streak") or 0) >= 2
                       or bool(_failed_streaks(state)),
        "pending": pending_info,
        "quota": {"state": (state.get("quota") or {}).get("state") or "ok",
                  "blocked": quota_blocked,
                  "reset_at": (state.get("quota") or {}).get("reset_at")},
        "ready_actions": [a["id"] for a in ready],
        "stalled": [{"id": a["id"], "reason": reason} for a, reason in stalled],
        "criteria": {"met": sum(1 for c in criteria if c.get("status") == "met"),
                     "total": len(criteria)},
        "gate": gate,
        "native_review": review,
        "notes": notes,
        "next_commands": _next_commands(phase, state, path, gate),
    }


# ── 无进展指纹（阶段 2 §3）──────────────────────────────────────────────────
PROGRESS_DIMENSIONS = ("actions", "reports", "evidence", "decisions", "criteria", "closure")
_DIMENSION_LABELS = {"actions": "工作项", "reports": "报告", "evidence": "证据",
                     "decisions": "决策", "criteria": "完成条件", "closure": "收口"}


def _canonical_json(obj: object) -> str:
    """固定分隔符与键序的规范 JSON：同一工作状态必须得到同一摘要。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _progress_values(state: dict) -> dict:
    """六维进展分量（未求摘要）。

    只覆盖「工作是否真的变了」：工作项、报告、证据、决策、完成条件、收口。
    不含轮次列表、round_seq、prompt 哈希、时间戳、配额与 budget——否则每轮必变，
    等于永远检测不到停滞。
    """
    actions = sorted(
        [[str(a.get("id") or ""), str(a.get("status") or ""),
          sha256_text(str(a.get("result") or "")),
          sorted(str(x) for x in (a.get("evidence_ids") or []))]
         for a in state.get("actions") or []],
        key=lambda item: item[0])
    reports = sorted(
        [[str(r.get("round_id") or ""), str(r.get("report_sha256") or "")]
         for r in state.get("reports") or []],
        key=lambda item: item[0])
    evidence = sorted(
        [[str(e.get("id") or ""), str(e.get("sha256") or "")]
         for e in state.get("evidence") or []],
        key=lambda item: item[0])
    decisions = [[str(d.get("ts") or ""), sha256_text(str(d.get("text") or ""))]
                 for d in state.get("decisions") or []]
    criteria = sorted(
        [[str(c.get("criterion_id") or ""), str(c.get("status") or ""),
          sorted(str(ref.get("id") or "") for ref in (c.get("evidence_refs") or []))]
         for c in (state.get("contract") or {}).get("criteria") or []],
        key=lambda item: item[0])
    closure = state.get("closure") or {}
    return {"actions": actions, "reports": reports, "evidence": evidence,
            "decisions": decisions, "criteria": criteria,
            "closure": [bool(closure.get("closed")),
                        (closure.get("gate") or {}).get("passed")]}


def progress_parts(state: dict) -> dict:
    """每个维度各自的摘要：用于指出「哪些维度没有变化」。"""
    return {name: sha256_text(_canonical_json(value))
            for name, value in _progress_values(state).items()}


def progress_digest(state: dict) -> str:
    """六维工作状态指纹；同一状态下稳定，与轮次/时间戳无关。"""
    return sha256_text(_canonical_json(progress_parts(state)))


def _failed_streaks(state: dict) -> list[str]:
    """连续 failed ≥ 3 次的工作项：属于无进展，但不自动改判或丢弃。"""
    return [str(a.get("id") or "") for a in state.get("actions") or []
            if int(a.get("fail_streak") or 0) >= 3]


def stamp_progress(path: str, prompt: str = "") -> dict:
    """在指导轮边界计算并持久化进展指纹（不递增轮次、不改工作项）。

    每个 round_seq 只计一次：同一轮重复调用不累加 streak、不覆盖指纹。
    写入只用 CAS(round_seq) 并只改 budget，避免与在途调用互相覆盖。
    """
    state = load_state(path)
    _assert_open(state, "更新进展指纹")
    budget = state["budget"]
    seq = int(state.get("round_seq") or 0)
    parts = progress_parts(state)
    digest = sha256_text(_canonical_json(parts))
    prompt_hash = sha256_text(prompt) if prompt else None
    prompt_repeat = bool(prompt_hash and budget.get("last_prompt_sha256")
                         and prompt_hash == budget["last_prompt_sha256"])
    failed_actions = _failed_streaks(state)
    already = (budget.get("streak_round_seq") == seq
               and bool(budget.get("last_progress_digest")))
    if already:
        streak = int(budget.get("progress_streak") or 0)
        prev_parts = budget.get("last_progress_parts") or {}
        return {"stamped": False, "already_stamped": True, "round_seq": seq,
                "progress_digest": digest, "progress_streak": streak,
                "no_progress": streak >= 2 or bool(failed_actions),
                "failed_actions": failed_actions, "prompt_repeat": prompt_repeat,
                "unchanged": [n for n in PROGRESS_DIMENSIONS
                              if parts[n] == prev_parts.get(n)]}
    prev = budget.get("last_progress_digest")
    prev_parts = budget.get("last_progress_parts") or {}
    if prev is not None and digest == prev:
        budget["progress_streak"] = int(budget.get("progress_streak") or 0) + 1
        unchanged = [n for n in PROGRESS_DIMENSIONS if parts[n] == prev_parts.get(n)]
    else:
        budget["progress_streak"] = 0
        unchanged = [] if prev is None else [
            n for n in PROGRESS_DIMENSIONS if parts[n] == prev_parts.get(n)]
    budget["last_progress_digest"] = digest
    budget["last_progress_parts"] = parts
    budget["streak_round_seq"] = seq
    if prompt_hash:
        budget["last_prompt_sha256"] = prompt_hash
    save_state(path, state, expect={"round_seq": seq})
    stick = int(budget["progress_streak"])
    return {"stamped": True, "already_stamped": False, "round_seq": seq,
            "progress_digest": digest, "progress_streak": stick,
            "no_progress": stick >= 2 or bool(failed_actions),
            "failed_actions": failed_actions, "prompt_repeat": prompt_repeat,
            "unchanged": unchanged}


# ── 上下文渲染 v2（九段 + 固定裁剪顺序）────────────────────────────────────
_RENDER_BASE = {"evidence": MAX_EVIDENCE_INDEXED, "summary": MAX_ROUND_SUMMARY_CHARS,
                "action": MAX_ACTION_RESULT_INJECTED, "decisions": MAX_DECISIONS_INJECTED}
# 固定裁剪顺序：证据索引 → 往轮摘要(1200→400) → 工作项结果(600→200) → 决策(8→3)。
# 目标、完成条件、边界、收口状态、阻断原因永不裁剪。
_RENDER_LADDER = (
    _RENDER_BASE,
    {**_RENDER_BASE, "evidence": 10},
    {**_RENDER_BASE, "evidence": 5},
    {**_RENDER_BASE, "evidence": 0},
    {**_RENDER_BASE, "evidence": 0, "summary": 400},
    {**_RENDER_BASE, "evidence": 0, "summary": 400, "action": 200},
    {**_RENDER_BASE, "evidence": 0, "summary": 400, "action": 200, "decisions": 3},
)


def _clip(text: object, limit: int) -> tuple[str, bool]:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value, False
    return value[:limit] + f"…[已截断至 {limit} 字符]", True


def _render_closure_line(state: dict) -> str:
    closure = state.get("closure") or {}
    if closure.get("closed"):
        gate = closure.get("gate") or {}
        return (f"- 收口：已收口（closed_by={closure.get('closed_by') or '-'}；"
                f"verdict={closure.get('verdict') or '-'}；"
                f"四门={'通过' if gate.get('passed') else ('跳过' if gate.get('passed') is None else '未通过')}）")
    return "- 收口：未收口"


def _render_sections(state: dict, cfg: dict, prompt_repeat: bool) -> tuple[list[str], dict]:
    omitted = {"evidence": 0, "rounds": 0, "decisions": 0}
    lines = ["=== copilot 任务交接状态（宿主主代理维护，是执行反馈，不是现场证据）===", ""]
    task_id = str(state.get("task_id") or "").strip()
    if task_id:
        contract = str(state.get("contract_sha256") or "")
        risk = str(state.get("risk") or "normal")
        lines.append(f"- 任务凭证：{task_id}（合同指纹 {contract[:12] or '-'}；"
                     f"已完成轮次 {len(_verified_rounds(state))}；风险 {risk}）")
    lines.append(f"- 轮次：round_seq={state.get('round_seq', 0)}")
    if state.get("execution_backend") == "native":
        channel = state.get("review_channel") or "未复核"
        lines.append(f"- 执行后端：native；复核通道：{channel}；"
                     f"原生 review_round：{state.get('review_round') or '-'}")
    lines.append(f"- 创建 / 更新：{state.get('created', '-')} / {state.get('updated', '-')}")
    lines.append(_render_closure_line(state))
    if state.get("note"):
        lines.append(f"- 执行方备注：{state['note']}")
    pending = state.get("pending_call")
    if isinstance(pending, dict):
        lines += ["", f"（注意：存在尚未结束的调用 round_id={pending.get('round_id') or '-'}"
                      f" / call_id={pending.get('call_id') or '-'}；"
                      "结果未知时先用 `copilot_dispatch.py recover` 处理，不要直接发起新调用。）"]
    if not cfg.get("_include_actions", True):
        goal, _ = _clip(state.get("goal"), MAX_TEXT_SECTION_CHARS)
        lines.append(f"- 本轮目标：{goal}")
        return lines, omitted

    # 段 1：原始目标（不可裁剪）
    goal, clipped = _clip(state.get("goal"), MAX_TEXT_SECTION_CHARS)
    lines += ["", "【原始目标】", goal or "（未记录）"]
    if clipped:
        lines.append(f"（目标超出 {MAX_TEXT_SECTION_CHARS} 字符已截断；完整目标见状态文件。）")

    # 段 2：完成条件与边界（不可裁剪）
    contract = state.get("contract") or {}
    criteria = contract.get("criteria") or []
    state_label = ("draft（待 GPT 首轮审定后确认）"
                   if contract.get("criteria_state") != "confirmed" else
                   f"confirmed（确认于轮次 {contract.get('confirmed_round_id') or '-'}）")
    lines += ["", f"【完成条件与边界】", f"完成条件：共 {len(criteria)} 条；状态 {state_label}"]
    if not criteria:
        lines.append("- 尚无结构化完成条件：本轮请给出可逐条核销的条件草案"
                     "（每条一句可判定的话），由主代理落盘后在下一轮确认。")
    used = 0
    for idx, item in enumerate(criteria):
        refs = item.get("evidence_refs") or []
        ref_text = ("；证据 " + ", ".join(
            f"{ref.get('id')}@{str(ref.get('sha256') or '')[:12]}" for ref in refs)) if refs else ""
        line = (f"- {item.get('criterion_id')} [{item.get('status')}] "
                f"{str(item.get('description') or '').strip()}{ref_text}")
        if used + len(line) > MAX_TEXT_SECTION_CHARS:
            lines.append(f"- …（其余 {len(criteria) - idx} 条因条件段预算省略；"
                         "完整条件见 contract 输出）")
            break
        lines.append(line)
        used += len(line)
    done_text = str(contract.get("done_criteria_text") or "").strip()
    if done_text:
        text, clipped = _clip(done_text, MAX_TEXT_SECTION_CHARS)
        lines += ["", "原始完成条件条文（供理解，不参与机器判定）：", text]
        if clipped:
            lines.append("（条文超长已截断。）")
    boundaries, clipped = _clip(contract.get("boundaries"), MAX_TEXT_SECTION_CHARS)
    lines += ["", "边界（不可越界）：", boundaries or "（未单独声明边界；仍受全局授权约束。）"]
    if clipped:
        lines.append(f"（边界超出 {MAX_TEXT_SECTION_CHARS} 字符已截断。）")

    # 段 3：已确认业务决策（可裁剪 8→3）
    decisions = state.get("decisions") or []
    recent_decisions = decisions[-cfg["decisions"]:] if decisions else []
    omitted["decisions"] = len(decisions) - len(recent_decisions)
    lines += ["", f"【已确认业务决策（最近 {len(recent_decisions)} 条）】"]
    if not recent_decisions:
        lines.append("（尚无已确认业务决策。）")
    for entry in recent_decisions:
        text, _ = _clip(entry.get("text"), MAX_DECISION_CHARS)
        lines.append(f"- [{entry.get('ts') or '-'}] {entry.get('source') or '-'}：{text}")
    if omitted["decisions"]:
        lines.append(f"（因预算省略更早的 {omitted['decisions']} 条决策。）")

    # 段 4：工作项进度（可裁剪结果 600→200）
    actions = state.get("actions") or []
    lines += ["", f"【工作项进度（{len(actions)} 项）】"]
    if not actions:
        lines.append("（尚无工作项：本轮由你判断需要做什么并给出 copilot-plan。）")
    for action in actions:
        deps = f"；依赖 {', '.join(action['depends_on'])}" if action.get("depends_on") else ""
        lines.append(f"- {action['id']} [{action['status']}] {action['title']}"
                     f"（执行方：{action.get('assignee', 'zcode')}{deps}）")
        if action.get("expected_evidence"):
            lines.append(f"  预期证据：{action['expected_evidence']}")
        if action.get("result"):
            feedback, clipped = _clip(action["result"], cfg["action"])
            if clipped:
                feedback += f"（完整反馈 {len(action['result'])} 字符，需要时向主代理索取）"
            lines.append(f"  执行反馈：{feedback}")
        if action.get("evidence_ids"):
            lines.append(f"  对应证据：{', '.join(action['evidence_ids'])}")
    ready = ready_actions(state)
    if ready:
        lines.append("当前可执行（依赖已满足）：" + ", ".join(a["id"] for a in ready))

    # 段 4b：原生执行/复核报告（仅 native 任务；两者必须分区，不能合并）
    reports = [r for r in state.get("reports") or [] if isinstance(r, dict)]
    native_exec = [r for r in reports if r.get("kind") == "execution"]
    native_rev = [r for r in reports if r.get("kind") == "review"]
    if state.get("execution_backend") == "native" and (native_exec or native_rev):
        lines += ["", "【原生执行与复核报告（独立记录，非现场证据）】"]
        if native_exec:
            latest = native_exec[-1]
            text, clipped = _clip(latest.get("summary"), cfg["summary"])
            lines.append(f"- 执行 {latest.get('work_item_id') or '-'}：{text}"
                         + ("（已截断）" if clipped else ""))
        if native_rev:
            latest = native_rev[-1]
            text, clipped = _clip(latest.get("summary"), cfg["summary"])
            lines.append(f"- 复核 {latest.get('work_item_id') or '-'}：{text}"
                         + ("（已截断）" if clipped else ""))
            lines.append("  说明：普通原生 Reviewer 的 PASS 不等于 G4；"
                         "该报告 g4_eligible=false。")
        if len(native_exec) + len(native_rev) > 2:
            lines.append(f"  （更早的 {len(native_exec) + len(native_rev) - 2} 条"
                         "执行/复核报告见状态文件。）")

    # 段 5：往轮判断（可裁剪 1200→400，轮数上限 6）
    rounds = state.get("rounds") or []
    verified_rounds = _verified_rounds(state)
    ignored = len(rounds) - len(verified_rounds)
    if ignored:
        lines += ["", f"（已忽略 {ignored} 轮未通过模型身份验证的历史记录："
                      "P0 前的旧轮次没有身份凭证，不得作为判断依据；"
                      "其工作项与轮次计数仍保留。）"]
    recent = verified_rounds[-MAX_ROUNDS_INJECTED:]
    omitted["rounds"] = len(verified_rounds) - len(recent)
    lines += ["", f"【往轮判断与安排（最近 {len(recent)} 轮；是历史判断，不是现场证据）】"]
    if not recent:
        lines.append("（无已验证的往轮判断。）")
    for rnd in recent:
        verdict = f" / verdict={rnd['verdict']}" if rnd.get("verdict") else ""
        lines.append(f"- {rnd.get('ts', '-')} {rnd.get('mode') or '-'}"
                     f"{verdict} / decision={rnd.get('decision') or '未给出'}")
        summary = str(rnd.get("summary") or "").strip()
        if summary:
            text, clipped = _clip(summary, cfg["summary"])
            if clipped:
                text += "（完整判断见轮次记录）"
            lines.append(f"  当时判断：{text}")
        elif rnd.get("reply_digest"):
            # 无安排块的 CONSULT / 被拒的安排：用回复前缀承载判断，避免下一轮从零开始。
            # 只是回复前缀，可能含对上文的引用；不当作当前事实或完整结论。
            digest, clipped = _clip(rnd["reply_digest"], cfg["summary"])
            if clipped:
                digest += "（完整回复见轮次记录）"
            lines.append(f"  历史回复前缀（可能含引用，不代表当前事实或完整结论）：{digest}")
        if rnd.get("actions"):
            lines.append(f"  新增工作项：{', '.join(rnd['actions'])}")
        if rnd.get("updated"):
            lines.append(f"  更新工作项：{', '.join(rnd['updated'])}")
    if verified_rounds:
        last = verified_rounds[-1]
        lines.append(f"- 上一轮：{last.get('mode') or '-'} / "
                     f"decision={last.get('decision') or '未给出'}")
        if last.get("note"):
            # 被拒绝的安排必须回灌，否则 copilot 会以为自己上一份安排已被接受。
            note, clipped = _clip(last["note"], cfg["summary"])
            lines.append(f"- 上一轮备注：{note}{'（已截断）' if clipped else ''}")

    # 段 6：待解决（不可裁剪）
    lines += ["", "【待解决】"]
    stalled = stalled_actions(state)
    if stalled:
        lines.append("需要处理（不会自动执行）：")
        lines += [f"- {a['id']} [{a['status']}] {a['title']} —— {reason}"
                  for a, reason in stalled]
    else:
        lines.append("（当前无停摆项。）")
    unmet = [c for c in criteria if c.get("status") != "met"]
    if unmet:
        lines.append("未满足完成条件：" + "；".join(
            f"{c.get('criterion_id')} [{c.get('status')}]" for c in unmet))
    blockers = []
    for report in reversed(state.get("reports") or []):
        if report.get("blocked"):
            blockers.append(f"轮次 {report.get('round_id') or '-'}："
                            f"{str(report.get('reason') or '未说明原因').strip()}")
            break
    gate = (state.get("closure") or {}).get("gate") or {}
    for reason in gate.get("reasons") or []:
        blockers.append(str(reason))
    if blockers:
        lines.append("阻断原因（不可裁剪）：")
        lines += [f"- {item}" for item in blockers]

    # 段 7：证据索引（优先裁剪项）
    evidence = state.get("evidence") or []
    shown = evidence[-cfg["evidence"]:] if cfg["evidence"] else []
    omitted["evidence"] = len(evidence) - len(shown)
    lines += ["", f"【证据索引（列出 {len(shown)} 条；只列元数据，正文走证据包）】"]
    if not evidence:
        lines.append("（尚未登记证据。）")
    elif not shown:
        lines.append(f"（因注入预算省略全部 {len(evidence)} 条证据索引；"
                     "需要时用 `evidence` 查询元数据。）")
    for entry in shown:
        dropped = "（已归档）" if entry.get("dropped") else ""
        lines.append(
            f"- {entry.get('id')} {entry.get('kind') or '-'} "
            f"sha256={str(entry.get('sha256') or '')[:12]} "
            f"{entry.get('size', 0)}B 采集 {entry.get('captured_at') or '-'}{dropped}")
    if omitted["evidence"]:
        lines.append(f"（因预算省略更早的 {omitted['evidence']} 条证据。）")

    # 段 8：预算 / 配额 / 检查点 / 无进展（固定）
    budget = state.get("budget") or {}
    quota = state.get("quota") or {}
    seq = int(state.get("round_seq") or 0)
    limit = budget.get("rounds_limit")
    limit_text = f"（软上限 {limit}）" if limit else ""
    checkpoint = int(budget.get("checkpoint_every") or CHECKPOINT_EVERY)
    on_checkpoint = seq > 0 and seq % checkpoint == 0
    quota_state = str(quota.get("state") or "ok")
    quota_line = f"- 配额：{quota_state}"
    if quota_state == "waiting":
        quota_line += (f"；预计恢复 {quota.get('reset_at') or '未知'}；"
                       f"当前阻塞：{'是' if is_quota_blocked(state) else '否（冷却已到点，仍需先核实）'}")
    streak = int(budget.get("progress_streak") or 0)
    failed_now = _failed_streaks(state)
    hard_blocked = streak >= 2 or bool(failed_now)
    parts = progress_parts(state)
    prev_parts = budget.get("last_progress_parts") or {}
    unchanged = [n for n in PROGRESS_DIMENSIONS if parts[n] == prev_parts.get(n)]
    lines += [
        "",
        "【预算 / 配额 / 检查点 / 无进展】",
        f"- 轮次：round_seq={seq}{limit_text}",
        f"- 检查点：每 {checkpoint} 轮核对进度；"
        + ("当前处于检查点（第 {} 轮）。".format(seq) if on_checkpoint
           else f"下一个在 round_seq={((seq // checkpoint) + 1) * checkpoint}。"),
        quota_line,
        f"- 无进展：progress_streak={streak}（"
        + ("已连续 2 轮无实质进展" if streak >= 2 else "未触发") + "）",
    ]
    if failed_now:
        lines.append("  连续失败的工作项：" + "、".join(
            f"{aid}（≥3 次）" for aid in failed_now)
            + "；属于无进展，但不自动改判或丢弃。")
    if hard_blocked:
        if unchanged:
            lines.append("  未变化维度：" + "、".join(_DIMENSION_LABELS[n] for n in unchanged))
        lines.append("  调整要求：换问题、补证据、拆解任务，或明确说明为何必须重复同一动作。")
        lines.append("  说明：停滞不自动转 PASS / BLOCKED / 失败，阶段仍由 verdict 与四门决定。")
    if prompt_repeat:
        lines.append("- 重复提问提示：本轮问题与上轮完全相同"
                     + ("，且工作状态无新变化。" if hard_blocked else "（仅提示，不判定停滞）。"))

    # 段 9：声明
    lines += [
        "",
        "说明：以上是交接与执行反馈，不是现场证据。判断仍以证据包条目为准；",
        "需要现场事实时，要求主代理补取，不要据执行反馈推断现场状态。",
    ]
    return lines, omitted


def render_state(state: dict, include_actions: bool = True,
                 prompt_repeat: bool = False) -> str:
    """渲染注入 copilot 的交接状态 v2（九段 + 20000 字预算 + 固定裁剪顺序）。"""
    cfg = dict(_RENDER_BASE)
    cfg["_include_actions"] = include_actions
    chosen = None
    lines: list[str] = []
    omitted: dict = {}
    for candidate in _RENDER_LADDER:
        trial = {**candidate, "_include_actions": include_actions}
        lines, omitted = _render_sections(state, trial, prompt_repeat)
        text = "\n".join(lines)
        if len(text) <= MAX_RENDER_CHARS:
            chosen = candidate
            cfg = trial
            break
    if chosen is None:
        lines, omitted = _render_sections(state, cfg, prompt_repeat)
        raise DispatchError(
            f"交接状态注入超出 {MAX_RENDER_CHARS} 字符预算，且已无可裁剪的上下文段；"
            "请精简合同/完成条件文本或减少工作项后再渲染。")
    if chosen != _RENDER_BASE:
        trims = []
        if omitted.get("evidence"):
            trims.append(f"证据索引省略 {omitted['evidence']} 条")
        if omitted.get("rounds"):
            trims.append(f"往轮记录省略 {omitted['rounds']} 轮")
        if omitted.get("decisions"):
            trims.append(f"决策省略 {omitted['decisions']} 条")
        if chosen["summary"] != MAX_ROUND_SUMMARY_CHARS:
            trims.append(f"往轮摘要截断至 {chosen['summary']} 字符")
        if chosen["action"] != MAX_ACTION_RESULT_INJECTED:
            trims.append(f"工作项反馈截断至 {chosen['action']} 字符")
        if chosen["decisions"] != MAX_DECISIONS_INJECTED:
            trims.append(f"决策仅保留最近 {chosen['decisions']} 条")
        if trims:
            lines += ["", "（注入预算裁剪：" + "；".join(trims) + "。）"]
    text = "\n".join(lines)
    if len(text) > MAX_RENDER_CHARS:
        raise DispatchError(
            f"交接状态注入 {len(text)} 字符，超过 {MAX_RENDER_CHARS} 字符预算；"
            "不可裁剪段（目标/完成条件/边界/收口）过长，请先精简。")
    return text


# ── CLI ─────────────────────────────────────────────────────────────────────
def _read_text_arg(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _read_json_arg(path: str) -> object:
    """读 JSON 参数文件（--criteria-file）；格式错误明确报错，不静默降级。"""
    try:
        text = _read_text_arg(path)
    except OSError as exc:
        raise DispatchError(f"无法读取 JSON 参数文件 {path}：{exc}") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DispatchError(f"JSON 参数文件 {path} 不是合法 JSON：{exc}") from None


# ── 恢复中断的调用 ──────────────────────────────────────────────────────────
def _result_unknown(error: object) -> bool:
    """审计里的失败是否属于「请求可能已发出但结果未知」（超时 / 网络中断 / 读响应中断）。"""
    err = str(error or "").strip()
    return (err == "timeout"
            or err.startswith("network(result unknown)")
            or err.startswith("response read failed"))


def recover_state(path: str, state: dict, audit_log: str, *, reply: str = "",
                  call_id: str = "", abandon: bool = False) -> dict:
    """依据 pending_call 与审计凭证精确恢复中断的调用，绝不重新调用 GPT。

    返回 {'status': nothing_to_recover|already_committed|abandoned|failed|committed}。
    * 已提交（轮次里有该 call_id）：只清理 pending，幂等，不重复入账/消费；
    * 审计成功但未提交：必须提供回复文件，重建凭证后提交；
    * 审计成功但回复已找不回：只有显式 --abandon 才清理，避免悄悄丢掉已取得的回复；
    * 审计失败（确定失败）：清理 pending，记入 abandoned_calls；
    * 审计失败但结果为「未知」（timeout / 网络中断）：可能已计费，只有显式
      --abandon 才清理；
    * 审计无结果（结果未知，可能已计费）：只有显式 --abandon 才清理。
    """
    _assert_open(state, "恢复中断调用")
    pending = state.get("pending_call")
    if not isinstance(pending, dict):
        return {"status": "nothing_to_recover"}
    p_call = str(pending.get("call_id") or "")
    p_round = str(pending.get("round_id") or "")
    if not p_call or not p_round:
        raise DispatchError("pending_call 缺少 call_id/round_id（状态可能被改动）；拒绝恢复。")
    if call_id and call_id.strip() != p_call:
        raise DispatchError(
            f"--call-id {call_id} 与挂起调用 {p_call} 不一致；只能恢复挂起的那一轮。")
    cred = task_credential(state)
    if cred is not None:
        if (str(pending.get("task_id") or "") != cred["task_id"]
                or str(pending.get("contract_sha256") or "") != cred["contract_sha256"]):
            raise DispatchError("挂起凭证与状态的任务/合同不一致（状态可能被改动）；拒绝恢复。")
        if not p_round.startswith(cred["task_id"] + "-r"):
            raise DispatchError(f"挂起轮次 {p_round} 不属于任务 {cred['task_id']}；拒绝恢复。")

    expect = {"pending_call_id": p_call}
    if any(isinstance(rnd, dict) and str(rnd.get("call_id") or "") == p_call
           for rnd in state.get("rounds") or []):
        # 提交成功但清理 pending 前中断：幂等清理，绝不能再次 ingest。
        release_pending(state, p_call, "已提交，仅清理 pending")
        save_state(path, state, expect=expect)
        return {"status": "already_committed", "round_id": p_round}

    audit_path = (audit_log or "").strip() or DEFAULT_AUDIT_LOG
    records = _audit_records(audit_path) if os.path.exists(audit_path) else []
    matches = [r for r in records
               if str(r.get("call_id") or "") == p_call
               and r.get("event") == "call_result"]
    record = matches[-1] if matches else None
    if record is None:
        if not abandon:
            raise DispatchError(
                f"挂起调用 {p_call}（轮次 {p_round}）在审计日志中没有结果记录："
                "结果未知，可能已被上游接收并计费。先确认是否需要从终端/--out 找回回复；"
                "确认无需恢复后，加 --abandon 才能清理 pending。")
        release_pending(state, p_call, "人工确认放弃（审计无结果，结果未知）")
        save_state(path, state, expect=expect)
        return {"status": "abandoned", "round_id": p_round,
                "reason": "审计无结果，人工确认放弃"}
    if record.get("ok") is not True:
        error = record.get("error")
        if _result_unknown(error) and not abandon:
            raise DispatchError(
                f"挂起调用 {p_call} 的结果未知（{error}）：请求可能已被上游接收并计费，"
                "不能直接当失败清掉。先找回回复或确认计费；确认放弃后加 --abandon。")
        release_pending(state, p_call, f"调用失败：{error or '未知'}")
        save_state(path, state, expect=expect)
        return {"status": "failed", "round_id": p_round, "error": error}
    if not reply.strip():
        if abandon:
            release_pending(state, p_call, "审计成功但回复无法找回（人工确认放弃）")
            save_state(path, state, expect=expect)
            return {"status": "abandoned", "round_id": p_round,
                    "reason": "审计成功但回复无法找回，人工确认放弃"}
        raise DispatchError(
            f"调用 {p_call} 已成功但尚未提交；请用 --reply-file 提供该次调用的回复"
            "（stdout 保存的文件或 --out 路径），不要重新调用 GPT。"
            "若回复确实已丢失，可用 --abandon 显式放弃本轮（不会重新调用）。")
    receipt = verify_call_receipt(audit_path, p_call, reply)
    try:
        outcome = commit_reply(state, reply, str(pending.get("mode") or ""),
                               receipt=receipt,
                               review_only=pending.get("auxiliary_review") is True)
    except DispatchError:
        save_state(path, state, expect=expect)  # 若已产生轮次留痕则保留，工作项不变
        raise
    save_state(path, state, expect=expect)
    return {"status": "committed", "round_id": p_round, "outcome": outcome}


RECOVERY_AUDIT_FILENAME = "recovery-audit.jsonl"


def _state_fingerprint(state: dict) -> str:
    """状态内容的规范摘要；用于证明恢复前后状态与并发写入。"""
    return sha256_text(json.dumps(state, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")))


def recovery_audit_path(state_path: str, override: str = "") -> str:
    """恢复审计与状态同目录、独立文件；不写模型调用审计。"""
    if (override or "").strip():
        return os.path.abspath(override.strip())
    return os.path.join(os.path.dirname(os.path.abspath(state_path)),
                        RECOVERY_AUDIT_FILENAME)


def _append_recovery_record(path: str, record: dict) -> None:
    """追加一条恢复审计（只追加，不重写历史）。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _read_recovery_records(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    records: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return records
    return records


def _render_normalized_reply(reply: str, plan: dict) -> str:
    """把规范化计划渲染成可复核的回复视图；原文中安排块以外的行逐字保留。"""
    blocks = _plan_blocks(reply)
    if not blocks:
        raise DispatchError("回复不含 copilot-plan 块，无法生成规范化视图。")
    start, end, _body = blocks[0]
    lines = reply.splitlines()
    body = json.dumps(plan, ensure_ascii=False, indent=2).split("\n")
    text = "\n".join(lines[:start + 1] + body + lines[end:])
    if reply.endswith("\n"):
        text += "\n"
    return text


def _verify_recovery_request(audit_path: str, call_id: str, fields: dict) -> None:
    """恢复前核对原始调用意图记录：必须有一次零工具 call_requested 在前。"""
    records = _audit_records(audit_path)
    requested = [i for i, rec in enumerate(records)
                 if str(rec.get("call_id") or "") == call_id
                 and rec.get("event") == "call_requested"]
    results = [i for i, rec in enumerate(records)
               if str(rec.get("call_id") or "") == call_id
               and rec.get("event") == "call_result"]
    if not requested:
        raise DispatchError(
            f"恢复只接受有调用意图记录的已消费轮次：审计中找不到 {call_id} 的 "
            "call_requested；拒绝恢复。")
    if not results or max(requested) > max(results):
        raise DispatchError(
            f"调用 {call_id} 的 call_requested 不在 call_result 之前；"
            "凭证顺序不成立，拒绝恢复。")
    request = records[max(requested)]
    result = records[max(results)]
    for key in ("task_id", "round_id"):
        if str(request.get(key) or "") != str(fields.get(key) or ""):
            raise DispatchError(
                f"调用 {call_id} 的请求记录 {key} 与结果凭证不一致；拒绝恢复。")
        if str(result.get(key) or "") != str(fields.get(key) or ""):
            raise DispatchError(
                f"调用 {call_id} 的结果记录 {key} 与凭证不一致；拒绝恢复。")
    expected_mode = str(fields.get("call_mode") or "")
    if str(request.get("mode") or "").upper() != expected_mode:
        raise DispatchError(
            f"调用 {call_id} 的请求模式与结果凭证不一致；拒绝恢复。")
    if str(request.get("model_requested") or "") != str(fields.get("model_requested") or ""):
        raise DispatchError(
            f"调用 {call_id} 的请求模型与结果凭证不一致；拒绝恢复。")
    if str(request.get("reasoning_effort") or "") != str(fields.get("reasoning_effort") or ""):
        raise DispatchError(
            f"调用 {call_id} 的 reasoning_effort 与结果凭证不一致；拒绝恢复。")
    if request.get("tools_sent") is not False:
        raise DispatchError(
            f"调用 {call_id} 的请求记录不是零工具调用（tools_sent="
            f"{request.get('tools_sent')!r}）；恢复只接受原生零工具通道。")
    if result.get("model_identity_ok") is not True:
        raise DispatchError(
            f"调用 {call_id} 的结果记录未通过模型身份验证；拒绝恢复。")


def recover_consumed_reply(path: str, state: dict, audit_log: str, *,
                           call_id: str, reply: str, dry_run: bool = False,
                           recovery_audit: str = "") -> dict:
    """恢复「已被原调用消费、但当时未成功应用」的回复，不重复调用模型。

    只允许恢复任务级状态中 **最后一个轮次**：该轮 `rejected=true`、`decision`
    为空，且没有后续轮次或按该轮次写入的工作项/条件。原 call_id、轮次、回复文件
    必须与审计成功结果逐项一致；规范化只在内存中进行，原始回复文件不改写。

    * 正常路径：校验全部通过 → 在状态副本上应用 → 一次原子写盘 → 追加恢复审计；
    * `dry_run=True`：只做校验并返回预览，不写状态、不写恢复审计；
    * 已应用：幂等返回 `already_applied`，不重复写条件/工作项；
    * 任何校验/应用失败：状态不变，恢复审计记录拒绝原因。
    """
    _assert_open(state, "恢复已消费回复")
    audit_path = (audit_log or "").strip() or DEFAULT_AUDIT_LOG
    if task_credential(state) is None:
        raise DispatchError(
            "恢复只适用于任务级状态（task_id/contract_sha256）；旧格式状态请重新发起调用。")
    if isinstance(state.get("pending_call"), dict):
        raise DispatchError(
            "存在尚未结束的调用（pending_call）；恢复已消费回复前必须先处理该轮。")
    call_id = (call_id or "").strip()
    if not call_id:
        raise DispatchError("缺少 --call-id：无法关联原始调用。")
    if not (reply or "").strip():
        raise DispatchError("缺少原始回复正文；不能用空内容恢复。")
    rounds = state.get("rounds") or []
    matches = [rnd for rnd in rounds
               if isinstance(rnd, dict) and str(rnd.get("call_id") or "") == call_id]
    if not matches:
        raise DispatchError(
            f"任务状态中没有 call_id={call_id} 的已消费轮次；拒绝恢复未消费的回复。")
    if len(matches) > 1:
        raise DispatchError(
            f"call_id={call_id} 在任务状态中出现 {len(matches)} 次；状态可能被改动，拒绝恢复。")
    target = matches[0]
    round_id = str(target.get("round_id") or "").strip()
    if not round_id:
        raise DispatchError(f"调用 {call_id} 的轮次记录缺少 round_id；拒绝恢复。")
    if target.get("decision") is not None and not target.get("rejected"):
        audit_target = recovery_audit_path(path, recovery_audit)
        current_fp = _state_fingerprint(state)
        if not dry_run:
            # 写盘成功但「结果记录」写入前中断时，重跑恢复是幂等的；这里补记
            # 缺失的结果，避免恢复审计只留 intent 而无结论。正常已入账轮次
            # 没有 intent，不会因此多写。
            records = _read_recovery_records(audit_target)
            intents = [r for r in records if r.get("event") == "recover_intent"
                       and str(r.get("call_id") or "") == call_id
                       and str(r.get("round_id") or "") == round_id]
            results = [r for r in records if r.get("event") == "recover_result"
                       and str(r.get("call_id") or "") == call_id
                       and str(r.get("round_id") or "") == round_id
                       and r.get("result") == "applied"]
            if intents and not results:
                _append_recovery_record(audit_target, {
                    "ts": now_iso(), "task_id": str(state.get("task_id") or ""),
                    "round_id": round_id, "call_id": call_id,
                    "event": "recover_result", "result": "applied",
                    "before_state_sha256": str(intents[-1].get(
                        "before_state_sha256") or ""),
                    "after_state_sha256": current_fp,
                    "normalizer_version": target.get("recovery", {}).get(
                        "normalizer_version"),
                    "mapping": target.get("recovery", {}).get("mapping") or [],
                    "original_reply_sha256": target.get("recovery", {}).get(
                        "original_reply_sha256"),
                    "audit_reply_sha256": target.get("recovery", {}).get(
                        "audit_reply_sha256"),
                    "normalized_reply_sha256": target.get("recovery", {}).get(
                        "normalized_reply_sha256"),
                    "reason": "状态已应用、结果记录缺失；重复执行时补记",
                })
        return {"status": "already_applied", "round_id": round_id,
                "decision": target.get("decision"),
                "verdict": target.get("verdict"),
                "state_sha256": current_fp}
    if target.get("rejected") is not True:
        raise DispatchError(
            f"轮次 {round_id} 不是「已消费但未应用」的拒绝轮次"
            f"（rejected={target.get('rejected')!r}，decision="
            f"{target.get('decision')!r}）；拒绝恢复。")
    if rounds[-1] is not target:
        raise DispatchError(
            f"轮次 {round_id} 之后已有后续轮次；为避免覆盖后续进展，拒绝恢复旧轮次。")
    for action in state.get("actions") or []:
        if round_id in (str(action.get("source_round_id") or ""),
                        str(action.get("last_updated_round_id") or "")):
            raise DispatchError(
                f"工作项 {action.get('id')} 已按轮次 {round_id} 写入；"
                "该轮已有进展，拒绝重复恢复。")
    for criterion in (state.get("contract") or {}).get("criteria") or []:
        if str(criterion.get("updated_round_id") or "") == round_id:
            raise DispatchError(
                f"完成条件 {criterion.get('criterion_id')} 已按轮次 {round_id} 更新；"
                "该轮已有进展，拒绝重复恢复。")

    if target.get("model_identity_ok") is not True:
        raise DispatchError(
            f"轮次 {round_id} 的记录未通过模型身份验证；拒绝恢复。")
    fields = verify_call_receipt(audit_path, call_id, reply)
    if str(fields.get("round_id") or "") != round_id:
        raise DispatchError(
            f"审计凭证轮次 {fields.get('round_id') or '-'} 与状态轮次 {round_id} 不一致；"
            "拒绝恢复。")
    if str(target.get("reply_sha256") or "") != str(fields.get("reply_sha256") or ""):
        raise DispatchError(
            f"轮次 {round_id} 记录的回复摘要与审计凭证不一致；拒绝恢复。")
    _verify_recovery_request(audit_path, call_id, fields)
    _attribution(state, fields, fields, str(target.get("mode") or ""),
                 allow_existing_round=True, require_pending=False)

    audit_target = recovery_audit_path(path, recovery_audit)
    before_fp = _state_fingerprint(state)
    base_record = {
        "ts": now_iso(), "task_id": str(state.get("task_id") or ""),
        "round_id": round_id, "call_id": call_id,
        "original_reply_sha256": sha256_text(reply),
        "audit_reply_sha256": str(fields.get("reply_sha256") or ""),
        "before_state_sha256": before_fp,
    }

    def reject(reason: str) -> None:
        if not dry_run:
            _append_recovery_record(audit_target, {
                **base_record, "event": "recover_result", "result": "rejected",
                "after_state_sha256": before_fp, "reason": reason,
            })
        raise DispatchError(f"恢复被拒绝：{reason}")

    try:
        plan, mapping, version = parse_plan_ex(reply)
    except DispatchError as exc:
        reject(str(exc))
    if plan is None:
        reject("原始回复不含 copilot-plan 安排块，无法作为计划轮次恢复。")
    normalized_reply = _render_normalized_reply(reply, plan)
    normalized_sha = sha256_text(normalized_reply)

    working = copy.deepcopy(state)
    failure: list[str] = []

    def fail_apply(message: str) -> None:
        failure.append(message)
        raise DispatchError(message)

    try:
        applied = _apply_plan_changes(working, plan, round_id, fail_apply)
    except DispatchError as exc:
        reject(failure[-1] if failure else str(exc))
    target_work = next(rnd for rnd in working["rounds"]
                       if str(rnd.get("call_id") or "") == call_id)
    recovery_meta = {
        "ts": now_iso(),
        "normalizer_version": version,
        "original_reply_sha256": sha256_text(reply),
        "audit_reply_sha256": str(fields.get("reply_sha256") or ""),
        "normalized_reply_sha256": normalized_sha,
        "mapping": mapping,
        "previous_note": str(target.get("note") or ""),
        "recovery_audit": audit_target,
    }
    target_work["rejected"] = False
    target_work["recovered"] = True
    target_work["decision"] = plan["decision"]
    target_work["verdict"] = plan.get("verdict")
    target_work["criteria"] = [u["criterion_id"] for u in applied["criteria"]]
    target_work["criteria_confirmed"] = applied["criteria_confirmed"]
    target_work["actions"] = [a["id"] for a in applied["added"]]
    target_work["updated"] = [a["id"] for a in applied["updated"]]
    target_work["summary"] = str(plan.get("summary") or "").strip()
    target_work["note"] = ("已从「已消费未应用」状态恢复入账；"
                           "原拒绝原因见 recovery.previous_note")
    target_work["recovery"] = recovery_meta

    preview = {
        "decision": plan["decision"], "verdict": plan.get("verdict"),
        "criteria": [{"criterion_id": u["criterion_id"],
                      "status": u["status"],
                      "evidence_refs": u["evidence_refs"]}
                     for u in applied["criteria"]],
        "criteria_confirmed": applied["criteria_confirmed"],
        "actions_added": [a["id"] for a in applied["added"]],
        "actions_updated": [a["id"] for a in applied["updated"]],
        "mapping": mapping, "normalizer_version": version,
        "original_reply_sha256": sha256_text(reply),
        "audit_reply_sha256": str(fields.get("reply_sha256") or ""),
        "normalized_reply_sha256": normalized_sha,
    }
    if dry_run:
        return {"status": "preview", "round_id": round_id,
                "before_state_sha256": before_fp, **preview}

    _append_recovery_record(audit_target, {
        **base_record, "event": "recover_intent", "result": "intent",
        "normalizer_version": version, "mapping": mapping,
        "normalized_reply_sha256": normalized_sha, "preview": preview,
    })
    expect = {"round_seq": state.get("round_seq", 0), "pending_call_id": ""}
    if state.get("version") == 2:
        expect["state_sha256"] = before_fp
    try:
        save_state(path, working, expect=expect)
    except (DispatchError, OSError) as exc:
        _append_recovery_record(audit_target, {
            **base_record, "event": "recover_result", "result": "failed",
            "after_state_sha256": before_fp,
            "reason": f"状态写盘失败：{exc}",
        })
        raise
    # 与其它写入入口保持一致：成功后调用方手里的 state 也反映已落盘内容。
    state.clear()
    state.update(working)
    after_fp = _state_fingerprint(working)
    result_record = {
        **base_record, "event": "recover_result", "result": "applied",
        "normalizer_version": version, "mapping": mapping,
        "normalized_reply_sha256": normalized_sha,
        "after_state_sha256": after_fp,
    }
    _append_recovery_record(audit_target, result_record)
    return {"status": "applied", "round_id": round_id,
            "before_state_sha256": before_fp, "after_state_sha256": after_fp,
            **preview}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="copilot 任务交接：工作项、执行反馈与领取")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="初始化交接状态（不覆盖已有状态）")
    p_start.add_argument("--state", required=True)
    p_start.add_argument("--goal-file", default="")
    p_start.add_argument("--goal", default="")
    p_start.add_argument("--mode", default="")
    p_start.add_argument("--note", default="")
    p_start.add_argument("--contract", default="",
                         help="完成条件与边界正文（可选）；与 --goal 一起规范化后"
                              "生成 contract_sha256，用于证明回复属于本任务合同")
    p_start.add_argument("--contract-file", default="")
    p_start.add_argument("--task-id", default="",
                         help="显式指定任务 id（默认随机生成；仅测试/迁移需要）")
    p_start.add_argument("--risk", default="normal", choices=["normal", "high"],
                         help="风险级别：high 时 PASS 核销要求原生独立复核记录（G4 fail-closed）")
    p_start.add_argument("--execution-backend", default="legacy",
                         choices=list(EXECUTION_BACKENDS),
                         help="执行后端：legacy（默认，主代理直接执行 + manifest）"
                              "或 native（Agent Bridge dispatch/--native-result）")
    p_start.add_argument("--rounds-limit", default="",
                         help="软轮次上限（可选）；8 轮检查点始终生效，不是硬上限")
    p_start.add_argument("--max-requests", default="",
                         help="本任务请求数硬上限（按审计 call_requested 计数）；"
                              "native 默认 6，legacy 默认不设限")
    p_start.add_argument("--max-total-tokens", default="",
                         help="本任务累计 Token 阈值（按审计 usage 核算）；"
                              "native 默认 500000。usage 响应后才知道，"
                              "不是严格上界")
    p_start.add_argument("--no-budget-projection", action="store_true",
                         help="关闭按上一次调用总量做的保守预估拦截"
                              "（仍保留请求数硬上限与 usage 阈值）")
    p_start.add_argument("--criteria-file", default="",
                         help="结构化完成条件 JSON（数组：字符串或 {criterion_id, description}）；"
                              "留空时从合同「## 完成条件」列表解析")

    p_next = sub.add_parser("next", help="列出当前可执行的工作项")
    p_next.add_argument("--state", required=True)
    p_next.add_argument("--json", action="store_true")

    p_record = sub.add_parser("record", help="回填执行结果与证据")
    p_record.add_argument("--state", required=True)
    p_record.add_argument("--id", required=True)
    p_record.add_argument("--status", required=True, choices=STATUSES)
    p_record.add_argument("--result", default="")
    p_record.add_argument("--result-file", default="")
    p_record.add_argument("--evidence-id", action="append", default=[])

    p_render = sub.add_parser("render", help="输出可注入 copilot 的交接状态")
    p_render.add_argument("--state", required=True)

    p_ingest = sub.add_parser("ingest", help="从 copilot 回复解析安排块并写入状态")
    p_ingest.add_argument("--state", required=True)
    p_ingest.add_argument("--reply-file", required=True)
    p_ingest.add_argument("--mode", default="")
    p_ingest.add_argument("--call-id", required=True,
                          help="该回复所属计费调用的 call_id（审计日志中的关联键）；"
                               "必须对应一次身份验证通过的成功调用，否则拒绝写入")
    p_ingest.add_argument("--audit-log", default=DEFAULT_AUDIT_LOG,
                          help=f"用于重建调用凭证的审计日志（默认 {DEFAULT_AUDIT_LOG}）")
    p_ingest.add_argument("--strict", action="store_true",
                          help="缺少安排块时视为错误（默认只警告）")

    p_recover = sub.add_parser(
        "recover", help="按 pending_call + 审计凭证恢复中断的调用（不重复调用 GPT）")
    p_recover.add_argument("--state", required=True)
    p_recover.add_argument("--audit-log", default=DEFAULT_AUDIT_LOG,
                           help=f"用于重建调用凭证的审计日志（默认 {DEFAULT_AUDIT_LOG}）")
    p_recover.add_argument("--call-id", default="",
                           help="可选；给出时必须与 pending_call 的 call_id 一致")
    p_recover.add_argument("--reply-file", default="",
                           help="成功调用尚未提交时，从 stdout/--out 保存的回复文件；'-' 表示 stdin")
    p_recover.add_argument("--abandon", action="store_true",
                           help="审计中没有结果记录（结果未知）时，人工确认放弃该轮；"
                                "不加此参数不会清空 pending")

    p_recover_reply = sub.add_parser(
        "recover-reply",
        help="恢复已消费但未成功应用的解析失败回复（不重复调用模型）")
    p_recover_reply.add_argument("--state", required=True)
    p_recover_reply.add_argument("--reply-file", required=True,
                                 help="该次调用保存下来的原始回复文件；'-' 表示 stdin")
    p_recover_reply.add_argument("--call-id", required=True,
                                 help="原始调用 call_id；必须对应审计中的成功结果")
    p_recover_reply.add_argument("--audit-log", default=DEFAULT_AUDIT_LOG,
                                 help=f"用于重建调用凭证的审计日志（默认 {DEFAULT_AUDIT_LOG}）")
    p_recover_reply.add_argument("--recovery-audit", default="",
                                 help="独立恢复审计路径（默认状态文件同目录 "
                                      f"{RECOVERY_AUDIT_FILENAME}）")
    p_recover_reply.add_argument("--dry-run", action="store_true",
                                 help="只校验并输出恢复预览；不写状态、不写恢复审计")

    p_decide = sub.add_parser("decide", help="记录已确认的业务决策（多轮上下文事实）")
    p_decide.add_argument("--state", required=True)
    p_decide.add_argument("--text", default="")
    p_decide.add_argument("--text-file", default="")
    p_decide.add_argument("--source", default="user", choices=["user", "gpt"])

    p_evidence = sub.add_parser("evidence", help="登记/归档证据（只存元数据与摘要）")
    p_evidence.add_argument("--state", required=True)
    p_evidence.add_argument("--id", default="")
    p_evidence.add_argument("--file", default="")
    p_evidence.add_argument("--text", default="")
    p_evidence.add_argument("--kind", default="file")
    p_evidence.add_argument("--note", default="")
    p_evidence.add_argument("--drop", action="store_true")
    p_evidence.add_argument("--reason", default="")

    p_report = sub.add_parser(
        "report", help="登记本轮执行报告或 legacy 复核报告（绑定已验证轮次）")
    p_report.add_argument("--state", required=True)
    p_report.add_argument("--kind", choices=("execution", "review"),
                          default="execution")
    p_report.add_argument("--summary", default="")
    p_report.add_argument("--summary-file", default="")
    p_report.add_argument("--round-id", default="")
    p_report.add_argument("--blocked", action="store_true")
    p_report.add_argument("--reason", default="")
    p_report.add_argument("--file", default="",
                          help="复核原始报告文件（kind=review 时必填；登记为证据）")
    p_report.add_argument("--evidence-id", default="",
                          help="复核原始报告证据 id（可选；默认自动分配）")
    p_report.add_argument("--review-of", default="",
                          help="被复核对象的 SHA-256（执行报告摘要或证据摘要）")
    p_report.add_argument("--review-of-kind", choices=REVIEW_OF_KINDS, default="",
                          help="复核对象类型：execution-report / evidence")
    p_report.add_argument("--verdict", choices=VERDICTS, default="")

    p_quota = sub.add_parser("quota", help="查看/清除配额冷却状态")
    p_quota.add_argument("--state", required=True)
    p_quota.add_argument("--json", action="store_true")
    p_quota.add_argument("--clear", action="store_true")
    p_quota.add_argument("--reason", default="")
    p_quota.add_argument("--source", default="user", choices=["user", "call"],
                         help="user=用户确认；call=用一次已入账的成功调用自证"
                              "（必须同时给 --call-id）")
    p_quota.add_argument("--call-id", default="",
                         help="--source call 时必填：该成功调用的 call_id")

    p_contract = sub.add_parser("contract", help="查看/调整完成条件")
    p_contract.add_argument("--state", required=True)
    p_contract.add_argument("--json", action="store_true")
    p_contract.add_argument("--amend", action="store_true")
    p_contract.add_argument("--criteria-file", default="")
    p_contract.add_argument("--source", default="zcode", choices=["zcode", "user"])
    p_contract.add_argument("--reason", default="")

    p_brief = sub.add_parser(
        "brief", help="输出本轮注入块，并计算/持久化无进展指纹（不递增轮次）")
    p_brief.add_argument("--state", required=True)
    p_brief.add_argument("--prompt", default="",
                         help="本轮要发给 GPT 的问题正文（用于重复提问提示）")
    p_brief.add_argument("--prompt-file", default="",
                         help="从文件读取问题正文；'-' 表示 stdin")
    p_brief.add_argument("--no-stamp", action="store_true",
                         help="只渲染，不更新 budget 指纹/streak（只读预览）")
    p_brief.add_argument("--json", action="store_true",
                         help="输出渲染结果与指纹信息（JSON）")

    p_check = sub.add_parser(
        "closure-check", help="只读核销 PASS 四门（G1 工作项 / G2 条件 / G3 停摆 / G4 原生复核）")
    p_check.add_argument("--state", required=True)
    p_check.add_argument("--review-ledger", default="",
                         help="GPT REVIEW 台账路径（仅审计；不构成 G4 依据）")
    p_check.add_argument("--json", action="store_true")

    p_closure = sub.add_parser("closure", help="收口 / 强制收口 / 重开任务")
    p_closure.add_argument("--state", required=True)
    group = p_closure.add_mutually_exclusive_group(required=True)
    group.add_argument("--close", action="store_true",
                       help="按四门结果收口（要求最近 PASS 轮且四门通过）")
    group.add_argument("--force-close", action="store_true",
                       help="用户终止任务（必须带 --reason；不伪造 PASS）")
    group.add_argument("--reopen", action="store_true",
                       help="用户授权重开已收口任务（必须带 --reason）")
    p_closure.add_argument("--round-id", default="",
                           help="--close 时可显式指定 PASS 轮次（必须等于最近 PASS 轮）")
    p_closure.add_argument("--reason", default="")
    p_closure.add_argument("--review-ledger", default="",
                           help="GPT REVIEW 台账路径（仅审计；不构成 G4 依据）")
    p_closure.add_argument("--json", action="store_true")

    p_loop = sub.add_parser(
        "loop-step", help="按八阶段给出本轮动作并落盘四门结果（不调用 GPT）")
    p_loop.add_argument("--state", required=True)
    p_loop.add_argument("--review-ledger", default="",
                        help="GPT REVIEW 台账路径（仅审计；不改变 G4 判定）")
    p_loop.add_argument("--stamp", action="store_true",
                        help="额外记录本轮无进展指纹（gate 结果总会落盘）")
    p_loop.add_argument("--prompt", default="")
    p_loop.add_argument("--prompt-file", default="")
    p_loop.add_argument("--json", action="store_true")

    p_derive = sub.add_parser(
        "derive-work",
        help="PARTIAL 未给出可执行安排时，按未闭合条件派生一个可执行工作项（纯本地）")
    p_derive.add_argument("--state", required=True)
    p_derive.add_argument("--reply-file", default="",
                          help="PARTIAL 回复原文（用于摘录缺口线索）；'-' 表示 stdin")
    p_derive.add_argument("--round-id", default="",
                          help="缺省使用最近已验证轮次")
    p_derive.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "start":
            goal = args.goal
            if args.goal_file:
                goal = _read_text_arg(args.goal_file)
            contract = args.contract
            if args.contract_file:
                contract = _read_text_arg(args.contract_file)
            criteria = _read_json_arg(args.criteria_file) if args.criteria_file else None
            state = start(args.state, goal, args.mode, args.note,
                          contract=contract, task_id=args.task_id,
                          risk=args.risk, rounds_limit=args.rounds_limit,
                          criteria=criteria,
                          execution_backend=args.execution_backend,
                          max_requests=args.max_requests or None,
                          max_total_tokens=args.max_total_tokens or None,
                          budget_projection=False if args.no_budget_projection else None)
            print(f"copilot: 交接状态已创建 {args.state}（task_id={state['task_id']}，"
                  f"目标指纹 {state['goal_sha256'][:12]}，"
                  f"合同指纹 {state['contract_sha256'][:12]}，"
                  f"完成条件 {len(state['contract']['criteria'])} 条/待确认；"
                  f"执行后端 {state['execution_backend']}）",
                  file=sys.stderr)
            return 0

        if args.cmd == "quota":
            state = load_state(args.state)
            if args.clear:
                quota = clear_quota(args.state, args.reason, args.source,
                                    args.call_id)
                print(f"copilot: 已清除配额等待（{quota.get('clear_reason')}）。",
                      file=sys.stderr)
                return 0
            quota = state.get("quota") or {}
            blocked = is_quota_blocked(state)
            if args.json:
                print(json.dumps({"quota": quota, "blocked": blocked},
                                 ensure_ascii=False, indent=2))
            else:
                print(f"配额状态：{quota.get('state') or 'ok'}"
                      f"；冷却阻塞：{'是' if blocked else '否'}")
                if quota.get("detected_at"):
                    print(f"  检测时间：{quota['detected_at']}")
                if quota.get("reset_at"):
                    print(f"  预计恢复：{quota['reset_at']}")
                if quota.get("reset_seconds") is None and quota.get("state") == "waiting":
                    print("  注意：上游未给出 reset_seconds，恢复时间未知；"
                          "需人工核实后 `quota --clear --reason ...`。")
                if quota.get("code"):
                    print(f"  错误码：{quota['code']}")
            return 0

        if args.cmd == "brief":
            prompt = args.prompt
            if args.prompt_file:
                prompt = _read_text_arg(args.prompt_file)
            if args.no_stamp:
                state = load_state(args.state)
                budget = state["budget"]
                seq = int(state.get("round_seq") or 0)
                parts = progress_parts(state)
                streak = int(budget.get("progress_streak") or 0)
                prompt_hash = sha256_text(prompt) if prompt else None
                prev_parts = budget.get("last_progress_parts") or {}
                info = {
                    "stamped": False, "already_stamped": False, "round_seq": seq,
                    "progress_digest": sha256_text(_canonical_json(parts)),
                    "progress_streak": streak, "no_progress": streak >= 2,
                    "prompt_repeat": bool(prompt_hash
                                          and budget.get("last_prompt_sha256") == prompt_hash),
                    "unchanged": [n for n in PROGRESS_DIMENSIONS
                                  if parts[n] == prev_parts.get(n)],
                }
            else:
                info = stamp_progress(args.state, prompt)
                state = load_state(args.state)
            text = render_state(state, prompt_repeat=info.get("prompt_repeat", False))
            if args.json:
                print(json.dumps({**info, "injection": text},
                                 ensure_ascii=False, indent=2))
            else:
                print(text)
            print(f"copilot: 无进展指纹 streak={info['progress_streak']}"
                  f"{'（no_progress）' if info['no_progress'] else ''}；"
                  f"round_seq={info['round_seq']}；"
                  f"{'已更新' if info['stamped'] else '未更新（本轮已计或 --no-stamp）'}",
                  file=sys.stderr)
            return 0

        if args.cmd == "closure-check":
            state = load_state(args.state)
            check = closure_check(state, args.review_ledger, args.state)
            if args.json:
                print(json.dumps(check, ensure_ascii=False, indent=2))
            else:
                print(f"PASS 四门：{'通过' if check['passed'] else '未通过'}"
                      f"（PASS 轮次：{check.get('round_id') or '无'}）")
                for name in ("G1", "G2", "G3", "G4"):
                    gate = check["gates"][name]
                    print(f"  {name} {'通过' if gate['passed'] else '未通过'}")
                    for reason in gate["reasons"]:
                        print(f"    - {reason['code']}：{reason['detail']}")
            return 0

        if args.cmd == "closure":
            if args.reopen:
                closure = reopen_task(args.state, args.reason)
                print("copilot: 任务已重开（未产生 PASS）；"
                      f"原因：{closure['reopen_history'][-1]['reason']}", file=sys.stderr)
            elif args.force_close:
                closure = close_task(args.state, force=True, reason=args.reason)
                print(f"copilot: 任务已由用户终止收口（{args.reason}）；"
                      "四门未核销，不构成 PASS。", file=sys.stderr)
            else:
                closure = close_task(args.state, args.round_id,
                                     review_ledger=args.review_ledger)
                print(f"copilot: 任务已按四门收口（轮次 {closure['round_id']}）。",
                      file=sys.stderr)
            if args.json:
                print(json.dumps(closure, ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "loop-step":
            prompt = args.prompt
            if args.prompt_file:
                prompt = _read_text_arg(args.prompt_file)
            result = loop_step(args.state, args.review_ledger,
                               stamp=args.stamp, prompt=prompt)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"阶段：{result['phase']}"
                      f"（rev {result['revision']}"
                      f"{'，PASS 待收口' if result['pass_pending'] else ''}"
                      f"{'，检查点' if result['checkpoint'] else ''}"
                      f"{'，无进展' if result['no_progress'] else ''}）")
                print(f"完成条件：{result['criteria']['met']}/{result['criteria']['total']}"
                      " 已核销")
                print(f"可执行工作项：{', '.join(result['ready_actions']) or '无'}")
                for note in result["notes"]:
                    print(f"注意：{note}")
                for command in result["next_commands"]:
                    print(f"  {command}")
            return 0

        if args.cmd == "derive-work":
            reply = ""
            if args.reply_file:
                reply = _read_text_arg(args.reply_file)
            state = load_state(args.state)
            snapshot = {k: state.get(k) for k in ("round_seq", "pending_call")}
            action = derive_work(state, reply, round_id=args.round_id)
            save_state(args.state, state, expect=snapshot)
            payload = {
                "status": "derived",
                "action": {
                    "id": action["id"], "title": action["title"],
                    "assignee": action["assignee"], "goal": action["goal"],
                    "constraints": action["constraints"],
                    "expected_evidence": action["expected_evidence"],
                    "origin": action["origin"],
                    "derived_from_round_id": action["derived_from_round_id"],
                },
                "state_path": os.path.abspath(args.state),
                "http_requests": 0,
                "notes": ["派生工作项来自未闭合完成条件与 PARTIAL 原文摘录，"
                          "不是 SOL 直接安排；执行后仍由 SOL 复核。"],
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"已派生工作项 {action['id']}：{action['title']}")
                print(action["goal"])
            return 0

        state = load_state(args.state)
        # 人工入口（record / ingest）写入前也用加载时的快照做比较交换：
        # 若期间有自动调用落盘了 pending（round_seq 前进），拒绝覆盖，避免抹掉在途调用。
        pending_now = state.get("pending_call")
        state_snapshot = {
            "round_seq": state.get("round_seq", 0),
            "pending_call_id": (str(pending_now.get("call_id") or "")
                                if isinstance(pending_now, dict) else ""),
        }

        if args.cmd == "recover-reply":
            task = task_credential(state)
            if task is not None and not audit_path_trusted(args.audit_log):
                raise DispatchError(
                    f"任务级自动协作只接受标准审计日志 {DEFAULT_AUDIT_LOG}"
                    f"（当前：{args.audit_log}）。测试请设置 {AUDIT_TEST_MODE_ENV}=1；"
                    "任意外部审计文件不得当作正式生产凭证。")
            result = recover_consumed_reply(
                args.state, state, args.audit_log,
                call_id=args.call_id,
                reply=_read_text_arg(args.reply_file),
                dry_run=args.dry_run,
                recovery_audit=args.recovery_audit)
            status = result["status"]
            if status == "already_applied":
                print(f"copilot: 轮次 {result['round_id']} 已应用"
                      f"（decision={result.get('decision')}）；"
                      "未重复写入、未重复消费。", file=sys.stderr)
                return 0
            label = "恢复预览（未写入）" if status == "preview" else "已恢复入账"
            print(f"copilot: {label}轮次 {result['round_id']}："
                  f"decision={result['decision']}；verdict={result.get('verdict')}",
                  file=sys.stderr)
            for item in result["criteria"]:
                print(f"  {item['criterion_id']} [{item['status']}] 证据 "
                      f"{', '.join(ref['id'] for ref in item['evidence_refs']) or '无'}",
                      file=sys.stderr)
            if result["actions_added"] or result["actions_updated"]:
                print(f"  新增工作项：{', '.join(result['actions_added']) or '无'}；"
                      f"更新工作项：{', '.join(result['actions_updated']) or '无'}",
                      file=sys.stderr)
            if result.get("mapping"):
                print(f"  字段映射 {len(result['mapping'])} 处"
                      f"（{result['normalizer_version']}）。", file=sys.stderr)
            print(f"  原始回复 SHA-256：{result['original_reply_sha256']}", file=sys.stderr)
            print(f"  规范化视图 SHA-256：{result['normalized_reply_sha256']}",
                  file=sys.stderr)
            if status == "applied":
                print(f"  状态摘要：{result['before_state_sha256'][:16]}… → "
                      f"{result['after_state_sha256'][:16]}…", file=sys.stderr)
            return 0

        if args.cmd == "recover":
            task = task_credential(state)
            if task is not None and not audit_path_trusted(args.audit_log):
                raise DispatchError(
                    f"任务级自动协作只接受标准审计日志 {DEFAULT_AUDIT_LOG}"
                    f"（当前：{args.audit_log}）。测试请设置 {AUDIT_TEST_MODE_ENV}=1；"
                    "任意外部审计文件不得当作正式生产凭证。")
            result = recover_state(args.state, state, args.audit_log,
                                   reply=_read_text_arg(args.reply_file)
                                   if args.reply_file else "",
                                   call_id=args.call_id, abandon=args.abandon)
            status = result["status"]
            if status == "nothing_to_recover":
                print("copilot: 没有未完成的调用（pending_call 为空），无需恢复。",
                      file=sys.stderr)
            elif status == "already_committed":
                print(f"copilot: 轮次 {result['round_id']} 已提交；已清理 pending，"
                      "未重复消费、未重复入账。", file=sys.stderr)
            elif status == "abandoned":
                print(f"copilot: 已放弃挂起轮次 {result['round_id']}（"
                      f"{result.get('reason') or '人工确认'}），未产生轮次记录。",
                      file=sys.stderr)
            elif status == "failed":
                print(f"copilot: 挂起轮次 {result['round_id']} 的调用已失败（"
                      f"{result.get('error') or '未知'}）；已清理 pending，未产生可用回复。",
                      file=sys.stderr)
            else:
                outcome = result["outcome"]
                print(f"copilot: 已恢复提交轮次 {result['round_id']}：decision="
                      f"{outcome['decision']}；新增工作项 "
                      f"{', '.join(a['id'] for a in outcome['added']) or '无'}；更新工作项 "
                      f"{', '.join(a['id'] for a in outcome['updated']) or '无'}",
                      file=sys.stderr)
            return 0

        if args.cmd == "next":
            ready = ready_actions(state)
            stalled = stalled_actions(state)
            if args.json:
                print(json.dumps({"ready": ready,
                                  "stalled": [{"action": a, "reason": r} for a, r in stalled]},
                                 ensure_ascii=False, indent=2))
            else:
                if not ready:
                    print("（当前没有依赖已满足的待执行工作项）")
                for action in ready:
                    print(f"{action['id']} [{action['assignee']}] {action['title']}")
                    if action.get("goal"):
                        print(f"  目标：{action['goal']}")
                    if action.get("constraints"):
                        print(f"  约束：{action['constraints']}")
                    if action.get("expected_evidence"):
                        print(f"  需返回证据：{action['expected_evidence']}")
                if stalled:
                    print("需要处理（不会自动进入可执行列表）：")
                    for action, reason in stalled:
                        print(f"  {action['id']} [{action['status']}] {action['title']} —— {reason}")
            return 0

        if args.cmd == "record":
            result = args.result
            if args.result_file:
                result = _read_text_arg(args.result_file)
            action = record(state, args.id, args.status, result, args.evidence_id)
            save_state(args.state, state, expect=state_snapshot)
            print(f"copilot: {action['id']} → {action['status']}", file=sys.stderr)
            return 0

        if args.cmd == "decide":
            text = args.text
            if args.text_file:
                text = _read_text_arg(args.text_file)
            entry = add_decision(state, text, args.source)
            save_state(args.state, state, expect=state_snapshot)
            print(f"copilot: 已记录业务决策（{entry['source']}）。", file=sys.stderr)
            return 0

        if args.cmd == "evidence":
            if args.drop:
                entry = drop_evidence(state, args.id, args.reason)
                save_state(args.state, state, expect=state_snapshot)
                print(f"copilot: 已归档证据 {entry['id']}。", file=sys.stderr)
                return 0
            if not args.id:
                raise DispatchError("登记证据必须给出 --id（形如 E1）。")
            entry = register_evidence(
                state, args.id, kind=args.kind,
                text=_read_text_arg("-") if args.text == "-" else
                     (args.text if args.text else None),
                file_path=args.file or None, note=args.note)
            save_state(args.state, state, expect=state_snapshot)
            print(f"copilot: 证据 {entry['id']} 已登记（sha256 {entry['sha256'][:12]}，"
                  f"{entry['size']} 字节）。", file=sys.stderr)
            return 0

        if args.cmd == "report":
            if args.kind == "review":
                if not args.file:
                    raise DispatchError(
                        "kind=review 必须用 --file 提供 Reviewer 原始报告；"
                        "未登记原文的复核不能作为证据。")
                if not args.review_of or not args.review_of_kind:
                    raise DispatchError(
                        "kind=review 必须同时给出 --review-of 与 --review-of-kind。")
                if not args.verdict:
                    raise DispatchError("kind=review 必须给出 --verdict。")
                evidence = register_review_evidence(
                    state, file_path=args.file, review_of=args.review_of,
                    review_of_kind=args.review_of_kind,
                    review_source="legacy", verdict=args.verdict,
                    round_id=args.round_id, eid=args.evidence_id,
                    note=args.reason)
                summary = args.summary
                if args.summary_file:
                    summary = _read_text_arg(args.summary_file)
                if not summary:
                    summary = (
                        "Legacy Reviewer 原始报告（未经 native 身份校验；"
                        f"verdict={args.verdict}；复核对象 {args.review_of}"
                        f"（{args.review_of_kind}）；原始证据 {evidence['id']}，"
                        f"SHA-256 {evidence['sha256']}）")
                entry = add_report(
                    state, summary, args.round_id, blocked=args.blocked,
                    reason=args.reason, kind="review",
                    review_of=args.review_of,
                    verdict=args.verdict,
                    review_of_kind=args.review_of_kind,
                    evidence_id=evidence["id"],
                    raw_report_sha256=evidence["sha256"],
                    review_source="legacy")
                if not evidence.get("round_id"):
                    evidence["round_id"] = entry["round_id"]
                save_state(args.state, state, expect=state_snapshot)
                suffix = "（重复登记，未新增）" if entry.get("duplicate") else ""
                print(f"copilot: legacy 复核报告已登记{suffix}（轮次 {entry['round_id']}，"
                      f"原始证据 {entry['evidence_id']}，"
                      f"结论 {entry['verdict']}，摘要 {entry['report_sha256'][:12]}）。",
                      file=sys.stderr)
                return 0
            summary = args.summary
            if args.summary_file:
                summary = _read_text_arg(args.summary_file)
            entry = add_report(state, summary, args.round_id,
                               blocked=args.blocked, reason=args.reason)
            save_state(args.state, state, expect=state_snapshot)
            print(f"copilot: 执行报告已登记（轮次 {entry['round_id']}，"
                  f"摘要 {entry['report_sha256'][:12]}）。", file=sys.stderr)
            return 0

        if args.cmd == "contract":
            contract = state["contract"]
            if args.amend:
                if not args.criteria_file:
                    raise DispatchError("--amend 需要 --criteria-file（新的完成条件 JSON）。")
                amend_criteria(state, _read_json_arg(args.criteria_file),
                               source=args.source, reason=args.reason)
                save_state(args.state, state, expect=state_snapshot)
                print(f"copilot: 完成条件已更新（{len(contract['criteria'])} 条，"
                      f"状态 {contract['criteria_state']}）。", file=sys.stderr)
                return 0
            payload = {
                "criteria_state": contract.get("criteria_state"),
                "criteria_sha256": contract.get("criteria_sha256"),
                "confirmed_round_id": contract.get("confirmed_round_id"),
                "criteria": contract.get("criteria") or [],
                "boundaries": contract.get("boundaries") or "",
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"完成条件状态：{payload['criteria_state']}"
                      f"（{len(payload['criteria'])} 条）")
                if payload["criteria_sha256"]:
                    print(f"  指纹：{payload['criteria_sha256']}")
                for item in payload["criteria"]:
                    print(f"  {item['criterion_id']} [{item['status']}] "
                          f"{item['description']}")
            return 0

        if args.cmd == "render":
            print(render_state(state))
            return 0

        if args.cmd == "ingest":
            reply = _read_text_arg(args.reply_file)
            task = task_credential(state)
            if task is not None and not audit_path_trusted(args.audit_log):
                raise DispatchError(
                    f"任务级自动协作只接受标准审计日志 {DEFAULT_AUDIT_LOG}"
                    f"（当前：{args.audit_log}）。测试请设置 {AUDIT_TEST_MODE_ENV}=1；"
                    "任意外部审计文件不得当作正式生产凭证。")
            # 先重建来源凭证：没有真实调用、身份未通过或摘要不符的回复一律拒绝，
            # 不能仅凭文件存在或正文格式正确就写成可执行工作项。
            receipt = verify_call_receipt(args.audit_log, args.call_id, reply)
            expected_mode = (args.mode or str(state.get("mode") or "")).strip().upper()
            call_mode = str(receipt.get("call_mode") or "")
            if expected_mode and call_mode and expected_mode != call_mode:
                raise DispatchError(
                    f"该回复来自 {call_mode} 调用，不能写入 {expected_mode} 交接状态；"
                    "请使用同模式的调用结果。")
            if task is None and not isinstance(state.get("last_call"), dict):
                # 旧格式状态且没有本轮凭证（新建状态后补录/状态落盘失败）：补录对象
                # 必须是审计里最后一次调用（且已通过身份验证）。任务级状态不走这里：
                # 它只认 pending_call，不再用全局最后成功调用推断归属。
                latest = latest_call_result_id(args.audit_log)
                if latest != receipt["call_id"]:
                    raise DispatchError(
                        "交接状态没有本轮调用凭证；补录只接受审计日志中最后一次调用"
                        f"（最后记录：{latest or '无'}，凭证：{receipt['call_id']}）。"
                        "更早的历史回复不得当作本轮结果；若确需恢复更早的回复，"
                        "请重新发起调用。")
            try:
                outcome = commit_reply(state, reply, args.mode, receipt=receipt)
            except DispatchError:
                # 保留轮次记录（若已消费凭证），工作项不变
                save_state(args.state, state, expect=state_snapshot)
                raise
            save_state(args.state, state, expect=state_snapshot)
            if outcome["skipped"]:
                message = "回复未包含 copilot-plan 安排块；未创建任何工作项。"
                if args.strict:
                    raise DispatchError(message)
                print(f"copilot: 警告：{message}", file=sys.stderr)
            else:
                print(f"copilot: decision={outcome['decision']}；新增工作项 "
                      f"{', '.join(a['id'] for a in outcome['added']) or '无'}；更新工作项 "
                      f"{', '.join(a['id'] for a in outcome['updated']) or '无'}", file=sys.stderr)
            return 0
    except (DispatchError, OSError) as exc:
        print(f"copilot: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())

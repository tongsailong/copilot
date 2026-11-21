#!/usr/bin/env python3
"""Copilot v6 本地协作治理核心（ZCode Autonomous Development Edition）。

定位
----
Copilot 不再是「远程模型决策通道」，而是运行于 ZCode 内的**轻量协作治理层**：

* 任务合同：目标、完成条件、边界、风险与授权记录；
* 工作项：由 ZCode 本地计划产生，记录状态、结果与证据；
* 证据：登记、SHA-256、文件重读、脱敏由 `bridge_evidence.py` 负责；
* 审计：状态文件保存任务、轮次式迭代、决策、报告与历史；历史 v1/v2 SOL
  状态只读展示，不再驱动任何调用；
* 判定：**完全本地**。PASS 由完成条件 + 证据 + G1–G3 机械核销；PARTIAL 表示
  仍有可执行工作；BLOCKED 只表示缺少授权、用户决策、资源或真实外部限制。

本模块不调用任何模型、不发起 HTTP 请求、不读取 Provider/凭据配置。工作项不是
授权：超出用户原始授权、不可逆、生产数据、付费、推送与部署仍需用户明确授权。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import code_scope as CS
import review_policy as RP

SCHEMA = "copilot-governance/v1"
VERSION = 1

# 风险层级与复核策略一致：low/medium 默认不派 Reviewer；high 建议一次
# SOL 零工具单轮复核（advisory，不是收口门禁）。
# `normal` 是历史标签，start 时归一化为 medium；读取旧状态时按 non-high 处理。
RISKS = ("low", "medium", "high", "normal")
BACKENDS = ("legacy", "native")
# 新计划只允许 zcode（主代理直接执行）与 deepseek（主代理派发原生子代理）。
# reasonix 仅用于读取历史状态，不再接受为新工作项执行方。
ASSIGNEES = ("zcode", "deepseek")
LEGACY_ASSIGNEES = ("zcode", "deepseek", "reasonix")
STATUSES = ("pending", "running", "done", "failed", "blocked", "dropped")
TERMINAL = {"done", "dropped"}
CRITERION_STATUSES = ("pending", "met", "not_met")
VERDICTS = ("pass", "partial", "blocked")
# verify 的结构化质量结论：原始 verdict 只允许这五种，大小写由解析层归一。
QUALITY_SCHEMA = "copilot-verify-result/v1"
QUALITY_INGEST_VERSION = "6.7.1"
QUALITY_VERDICTS = ("pass", "pass_with_warnings", "partial", "blocked", "fail")
PWW = "pass_with_warnings"
QUALITY_REPORT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
QUALITY_WARNING_RE = re.compile(r"^W[1-9][0-9]{0,2}$")
QUALITY_RISK_RE = re.compile(r"^R[1-9][0-9]{0,2}$")
QUALITY_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
QUALITY_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
QUALITY_FIELDS = frozenset({
    "schema", "report_id", "generated_at", "verdict", "warnings",
    "residual_risks", "unverified", "acceptance_required",
    "acceptance_status", "checks", "subject", "supersedes_report_id",
    "resolved_warning_ids", "resolved_residual_risk_ids",
})
QUALITY_ACCEPTANCE_STATUSES = ("pending", "partial", "accepted",
                               "superseded", "stale")
CRITERION_ID_RE = re.compile(r"^C[1-9][0-9]{0,2}$")
EVIDENCE_ID_RE = re.compile(r"^E[1-9][0-9]{0,3}$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ACTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REVIEW_ATTEMPT_RE = re.compile(r"^RA[0-9a-f]{24}$")

TERMINAL_PHASES = ("CLOSED", "BLOCKED", "PASS")
PHASES = ("CLOSED", "BLOCKED", "PASS", "RUNNING", "PARTIAL")
MAX_ACTIONS = 64
MAX_DERIVED_PER_CALL = 12
MAX_RESULT_CHARS = 20000
MAX_REPORT_CHARS = 8000


class GovernanceError(Exception):
    """可预期的治理错误；调用方必须显式报告，不得静默丢弃。"""


# ── 基础工具 ───────────────────────────────────────────────────────────────
def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256_text(text: str) -> str:
    import hashlib
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _sha256_file(path: str) -> str:
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value: object, name: str, *, allow_empty: bool = False,
          limit: int = 0) -> str:
    if not isinstance(value, str):
        raise GovernanceError(f"{name} 必须是字符串。")
    text = value.strip()
    if not text and not allow_empty:
        raise GovernanceError(f"{name} 不能为空。")
    if limit and len(text) > limit:
        text = text[:limit]
    return text


def _list(value: object, name: str) -> list:
    if not isinstance(value, list):
        raise GovernanceError(f"{name} 必须是数组。")
    return value


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise GovernanceError(f"{name} 必须是布尔值。")
    return value


# ── 状态加载 / 原子保存 ────────────────────────────────────────────────────
def _default_test_policy() -> dict:
    return {
        "captured": None,
        "source": "",
        "required": False,
        "commands": [],
        "report": "",
        "artifacts": [],
        "require_test_result": False,
        "require_artifact_integrity": False,
    }


def _default_contract() -> dict:
    return {
        "done_criteria_text": "",
        "boundaries": "",
        "criteria": [],
        "criteria_sha256": "",
        "criteria_state": "draft",
        "test_policy": _default_test_policy(),
        "captured": None,
        "history": [],
    }


def _normalize_test_policy(raw: object) -> dict:
    """把合同里的测试要求固化成可信策略；执行方不能事后降低。"""
    if raw in (None, {}):
        return _default_test_policy()
    if not isinstance(raw, dict):
        raise GovernanceError("test_policy 必须是对象。")
    commands = [str(item).strip() for item in raw.get("commands") or []
                if str(item).strip()]
    artifacts = [str(item).strip() for item in raw.get("artifacts") or []
                 if str(item).strip()]
    required = raw.get("required")
    if not isinstance(required, bool):
        raise GovernanceError("test_policy.required 必须是布尔值。")
    report = str(raw.get("report") or "").strip()
    if required and not commands:
        raise GovernanceError(
            "test_policy.required=true 时必须同时给出 commands（执行前确定测试命令）。")
    require_result = bool(raw.get("require_test_result")) or bool(report) or required
    require_artifacts = bool(raw.get("require_artifact_integrity")) or bool(artifacts)
    return {
        "captured": now_iso(),
        "source": str(raw.get("source") or "contract").strip()[:64],
        "required": required,
        "commands": commands,
        "report": report,
        "artifacts": artifacts,
        "require_test_result": require_result,
        "require_artifact_integrity": require_artifacts,
    }


def _default_closure() -> dict:
    return {
        "closed": False,
        "closed_by": None,
        "verdict": None,
        "iteration_id": None,
        "closed_at": None,
        "gate": {"passed": None, "iteration_id": None, "reasons": []},
        "quality": None,
        "reopen_history": [],
    }


def _default_state() -> dict:
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "task_id": "",
        "contract_sha256": "",
        "goal": "",
        "goal_sha256": "",
        "mode": "",
        "note": "",
        "risk": "medium",
        "task_kind": RP.DEFAULT_TASK_KIND,
        "task_kind_source": "default",
        "admission_triggers": [],
        "execution_backend": "legacy",
        "workspace": "",
        "workspace_source": "",
        "created": now_iso(),
        "updated": now_iso(),
        "save_seq": 0,
        "iteration_seq": 0,
        "actions": [],
        # 调度领取记录：drive 把工作项交给某个会话时落盘，避免并发重复执行。
        # 记录本身不是执行证明；工作项终态仍以 status/result/evidence 为准。
        "claims": [],
        "plans": [],
        "iterations": [],
        "judgments": [],
        "blocks": [],
        "evidence": [],
        "reports": [],
        "decisions": [],
        "quality_reports": [],
        "contract": _default_contract(),
        "native_dispatch": None,
        "native_results": [],
        # SOL 调用记录（CONSULT / PLAN / REVIEW，历史 THINK 只读兼容）统一存放在
        # `sol_calls`。历史状态里的 `sol_reviews` / `sol_advisories` 继续只读兼容，
        # 不再新增默认键，也不回写。
        "sol_calls": [],
        "review_admissions": [],
        "review_stops": [],
        "finding_ledger": {
            "schema": RP.LEDGER_SCHEMA,
            "next_id": 1,
            "items": [],
        },
        "execution_bindings": [],
        "formal_reviews": [],
        "formal_review_authorization": None,
        "closure": _default_closure(),
        "history": [],
    }


def _merge_defaults(value: object, defaults: dict, name: str) -> dict:
    if not isinstance(value, dict):
        raise GovernanceError(f"状态的 {name} 不是对象（{type(value).__name__}）。")
    for key, default in defaults.items():
        if key not in value:
            value[key] = json.loads(json.dumps(default))
        elif isinstance(default, dict):
            value[key] = _merge_defaults(value[key], default, f"{name}.{key}")
    return value


def is_legacy_state(data: object) -> bool:
    """v1/v2 SOL 状态：只读展示，不再进入 v6 驱动。"""
    if not isinstance(data, dict):
        return False
    if str(data.get("schema") or "") == SCHEMA:
        return False
    version = data.get("version")
    return (isinstance(version, int) and not isinstance(version, bool)
            and version in (1, 2))


def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise GovernanceError(
            f"任务状态不存在：{path}。请先用 `copilot_dispatch.py start` 创建。") from None
    except (OSError, ValueError) as exc:
        raise GovernanceError(f"无法读取任务状态 {path}：{exc}") from None
    if is_legacy_state(data):
        raise GovernanceError(
            f"{path} 是 v{data.get('version')} SOL 历史状态，v6 不再驱动它；"
            "请用 `copilot_dispatch.py legacy-status --state <path>` 只读查看。")
    if not isinstance(data, dict) or str(data.get("schema") or "") != SCHEMA:
        raise GovernanceError(
            f"{path} 不是 {SCHEMA}（schema={data.get('schema') if isinstance(data, dict) else '-'}）。")
    state = _merge_defaults(data, _default_state(), "state")
    version = state.get("version")
    if version != VERSION:
        raise GovernanceError(f"状态版本 {version!r} 不受支持（当前 {VERSION}）。")
    return state


def _save_seq(state: dict) -> int:
    value = state.get("save_seq") if isinstance(state, dict) else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _lock_file(fh) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover — 本机 Linux
        return
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _unlock_file(fh) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover
        return
    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def task_lock(task_dir: str, name: str = ".sol-review.lock"):
    """同一任务的跨进程互斥锁；用于远程付费调用前的“检查—调用—登记”临界区。

    锁文件与状态文件分离：持有时间覆盖一次 SOL 请求，但不会阻塞普通状态命令
    的保存（保存只锁自己的状态文件）。平台无 fcntl 时退化为无锁。
    """
    os.makedirs(task_dir, exist_ok=True)
    path = os.path.join(task_dir, name)
    with open(path, "a+", encoding="utf-8") as fh:
        _lock_file(fh)
        try:
            yield
        finally:
            _unlock_file(fh)


def _fsync_dir(path: str) -> None:
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
    if expect.get("absent"):
        if os.path.exists(path):
            raise GovernanceError(
                f"任务状态在初始化落盘前已被创建：{path}；拒绝覆盖已有任务。")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            current = json.load(fh)
    except FileNotFoundError:
        current = {}
    except (OSError, ValueError) as exc:
        raise GovernanceError(f"无法读取任务状态 {path} 做并发校验：{exc}") from None
    if not isinstance(current, dict):
        current = {}
    if "save_seq" in expect:
        actual = _save_seq(current)
        want = _save_seq({"save_seq": expect["save_seq"]})
        if actual != want:
            raise GovernanceError(
                f"任务状态已被其他进程写入（save_seq {want} → {actual}）：本次未写入；"
                "请重新读取状态后再提交。")


def _same_content(path: str, payload: dict) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            current = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(current, dict):
        return False
    keys = ("updated", "save_seq")
    left = {k: v for k, v in current.items() if k not in keys}
    right = {k: v for k, v in payload.items() if k not in keys}
    return left == right


def save_state(path: str, state: dict, expect: dict | None = None) -> None:
    """原子写盘 + 单调 save_seq 比较交换。"""
    target = os.path.abspath(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    lock_path = os.path.join(directory, "." + os.path.basename(target) + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_fh:
        _lock_file(lock_fh)
        try:
            if expect is not None:
                expect = dict(expect)
                expect.setdefault("save_seq", _save_seq(state))
                _check_expect(target, expect)
            elif os.path.exists(target):
                _check_expect(target, {"save_seq": _save_seq(state)})
            payload = dict(state)
            payload["save_seq"] = _save_seq(state) + 1
            payload["updated"] = now_iso()
            if _same_content(target, payload):
                return
            fd, tmp = tempfile.mkstemp(prefix=".copilot-governance-", dir=directory,
                                       text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
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
            state["save_seq"] = payload["save_seq"]
            state["updated"] = payload["updated"]
        finally:
            _unlock_file(lock_fh)


SESSION_ENV_KEYS = ("ZCODE_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")
SESSION_BINDING_FILE = "session.json"
MAX_BOUND_SESSIONS = 20


def current_session_id() -> str:
    """当前 ZCode 会话标识（宿主在 Bash/钩子环境里注入）；不可用时返回空串。"""
    for key in SESSION_ENV_KEYS:
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def touch_session(state_path: str, *, session: str = "") -> str:
    """把当前会话绑定到任务目录，供 Stop 守卫判定“本会话是否负责该任务”。

    只写任务目录下的 `session.json` 旁路文件，不改 governance 状态、证据或历史；
    没有会话标识（手工运行、非 ZCode 环境）时不做任何事。失败不影响命令本身：
    守卫在缺失绑定时会退化为“不拦截”，不会因此误判任务完成。
    """
    sid = str(session or "").strip() or current_session_id()
    if not sid or not state_path:
        return ""
    target = os.path.abspath(state_path)
    directory = os.path.dirname(target)
    if not os.path.isdir(directory):
        return ""
    path = os.path.join(directory, SESSION_BINDING_FILE)
    rows: list[dict] = []
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                existing = json.load(fh)
            if isinstance(existing, dict) and isinstance(existing.get("sessions"), list):
                rows = [row for row in existing["sessions"]
                        if isinstance(row, dict) and str(row.get("session_id") or "")]
    except (OSError, ValueError):
        rows = []
    now = now_iso()
    rows = [row for row in rows if str(row.get("session_id") or "") != sid]
    rows.append({"session_id": sid, "at": now})
    rows = rows[-MAX_BOUND_SESSIONS:]
    payload = {"sessions": rows, "updated": now}
    workspace = str(os.environ.get("ZCODE_PROJECT_DIR") or "").strip()
    if workspace:
        payload["workspace"] = workspace
    try:
        fd, tmp = tempfile.mkstemp(prefix=".copilot-session-", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        return ""
    return path


def log_attempt(state_path: str, kind: str, code: str, message: str,
                envelope: dict | None = None) -> str:
    """把被拒/无法生成的信封尝试追加到任务目录 `review-attempts.jsonl`。

    无效信封是复盘“为什么返工”的关键证据，但过去只打印到终端。这里只记定位
    字段（时间、类型、错误码、工作项、摘要），不写信封正文或证据内容；失败时
    只打印警告，不影响原流程。执行信封生成/回填被拒也走同一通道，
    `review-metrics` 的无效信封计数才完整。
    """
    envelope = envelope if isinstance(envelope, dict) else {}
    entry = {
        "at": now_iso(),
        "kind": kind,
        "code": code,
        "message": str(message)[:400],
        "work_item_id": str(envelope.get("work_item_id") or ""),
        "execution_result_sha256": str(
            envelope.get("execution_result_sha256") or ""),
        "result_sha256": str(envelope.get("result_sha256") or ""),
    }
    target = os.path.join(os.path.dirname(os.path.abspath(state_path)),
                          "review-attempts.jsonl")
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"警告：无法记录无效信封尝试到 {target}：{exc}", file=sys.stderr)
    return target


# ── 合同 / 完成条件 ────────────────────────────────────────────────────────
def _section_text(text: str, title: str) -> str:
    lines = (text or "").splitlines()
    out: list[str] = []
    active = False
    for line in lines:
        if line.strip().startswith("##"):
            heading = line.lstrip("#").strip()
            active = heading == title or heading.startswith(title)
            continue
        if active:
            out.append(line)
    return "\n".join(out).strip()


def _criteria_from_text(contract: str) -> list[str]:
    body = _section_text(contract, "完成条件")
    out: list[str] = []
    for line in body.splitlines():
        match = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$", line)
        if match:
            out.append(match.group(1))
    return out


def _normalize_criteria(raw: object) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise GovernanceError("完成条件 criteria 必须是数组。")
    out: list[dict] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw, 1):
        if isinstance(item, str):
            description = item.strip()
            cid = f"C{idx}"
            refs: list = []
        elif isinstance(item, dict):
            cid = str(item.get("criterion_id") or f"C{idx}").strip()
            description = str(item.get("description") or "").strip()
            refs = item.get("evidence_refs") or []
        else:
            raise GovernanceError(f"完成条件第 {idx} 条必须是字符串或对象。")
        if not CRITERION_ID_RE.match(cid):
            raise GovernanceError(f"完成条件 id {cid!r} 非法（应形如 C1）。")
        if cid in seen:
            raise GovernanceError(f"完成条件 id {cid} 重复。")
        if not description:
            raise GovernanceError(f"完成条件 {cid} 缺少描述。")
        seen.add(cid)
        out.append({
            "criterion_id": cid,
            "description": description[:2000],
            "status": "pending",
            "evidence_refs": [],
            "updated_at": None,
            "updated_iteration": None,
            "history": [],
            "_initial_refs": refs,
        })
    return out


def criteria_digest(criteria: list[dict]) -> str:
    return _sha256_text(canonical_json([
        {"criterion_id": c.get("criterion_id"),
         "description": c.get("description")}
        for c in criteria
    ]))


def _contract_fingerprint(goal: str, contract_text: str,
                          criteria: list[dict],
                          test_policy: dict | None = None) -> str:
    payload = {
        "goal": (goal or "").strip(),
        "contract": (contract_text or "").strip(),
        "criteria": criteria_digest(criteria),
    }
    # 只有显式合同测试策略才进入指纹：旧任务不带策略时哈希口径保持不变。
    if test_policy and test_policy.get("captured"):
        payload["test_policy"] = {
            key: test_policy.get(key) for key in
            ("required", "commands", "report", "artifacts",
             "require_test_result", "require_artifact_integrity")}
    return _sha256_text(canonical_json(payload))


def _contract_source_record(path: str) -> dict:
    """记录合同来源文件的路径与摘要；空路径返回空记录。"""
    value = str(path or "").strip()
    if not value:
        return {}
    absolute = os.path.abspath(os.path.expanduser(value))
    if not os.path.isfile(absolute):
        raise GovernanceError(f"合同文件不存在：{absolute}")
    return {"path": absolute, "sha256": _sha256_file(absolute),
            "captured": now_iso()}


def contract_source_warnings(state: dict) -> list[str]:
    """合同源文件与状态记录的合同不一致时给出**非阻断**提醒。

    任务执行以状态里的合同为准（指纹、条件、证据都绑定它）；源文件被改动不会
    自动生效，也不让任务失败。但需要让调用方知道“你正在编辑的文件”和“任务
    实际执行的条件”已经不同步，避免按旧文本判断进展。
    """
    source = (state.get("contract") or {}).get("source") or {}
    src_path = str(source.get("path") or "").strip()
    if not src_path:
        return []
    if not os.path.isfile(src_path):
        return [f"合同源文件已不存在：{src_path}；当前条件只能从 state 读取，"
                "请确认以哪份为准。"]
    try:
        current = _sha256_file(src_path)
    except OSError as exc:
        return [f"合同源文件无法读取：{src_path}（{exc}）。"]
    recorded = str(source.get("sha256") or "")
    if current != recorded:
        return [f"合同源文件已变更：{src_path}；任务仍按登记时的条件执行"
                f"（{recorded[:12]}…）。如变更应生效，请用 contract --amend "
                "重新登记合同。"]
    return []


def _canonical_workspace(path: str) -> str:
    """工作区规范路径：展开、realpath，并尽量归一到 git 根。"""
    value = str(path or "").strip()
    if not value:
        return ""
    absolute = os.path.realpath(os.path.abspath(os.path.expanduser(value)))
    root = CS.git_root(absolute)
    return os.path.realpath(root) if root else absolute


def _git_workspace(path: str) -> str:
    value = str(path or "").strip()
    if not value:
        return ""
    root = CS.git_root(os.path.abspath(os.path.expanduser(value)))
    return os.path.realpath(root) if root else ""


def _normalize_task_kind(value: object) -> tuple[str, str]:
    """任务类型归一化；未知类型回落到 standard（非简单，默认进入 SOL 认知流程）。"""
    raw = str(value or "").strip().lower()
    if not raw:
        return RP.DEFAULT_TASK_KIND, "default"
    if raw not in RP.TASK_KINDS:
        raise GovernanceError(
            f"task_kind={value!r} 非法；只能是 {' / '.join(sorted(RP.TASK_KINDS))}。")
    return raw, "declared"


def _normalize_triggers(value: object) -> list[dict]:
    """声明触发因素；只接受字符串名称，去重保序。"""
    items: object = value
    if isinstance(items, str):
        items = [part for part in items.replace(";", ",").split(",")]
    if items is None:
        return []
    if not isinstance(items, (list, tuple, set)):
        raise GovernanceError("triggers 必须是字符串数组。")
    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        name = str(item or "").strip().lower().replace("_", "-")
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "source": "declared", "at": now_iso()})
    return out


def _add_triggers(state: dict, triggers: object) -> list[dict]:
    """把触发因素并入状态；同名只保留首次登记，返回本次新增条目。"""
    added: list[dict] = []
    existing = {str(t.get("name") or "") for t in state.get("admission_triggers") or []
                if isinstance(t, dict)}
    for item in _normalize_triggers(triggers):
        if item["name"] in existing:
            continue
        existing.add(item["name"])
        state.setdefault("admission_triggers", []).append(item)
        added.append(item)
    return added


def add_admission_triggers(path: str, triggers: object, *, source: str = "zcode") -> dict:
    """登记新的触发因素（新证据/新风险/范围变化等）；不改变任务状态与门禁。"""
    state = load_state(path)
    _assert_open(state, "登记 SOL 触发因素", allow_blocked=True)
    added = _add_triggers(state, triggers)
    for item in added:
        item["source"] = str(source or "zcode")[:40]
    if added:
        _history(state, "admission_triggers_added",
                 {"triggers": [t["name"] for t in added]})
        save_state(path, state)
    return {"added": [t["name"] for t in added],
            "triggers": [t["name"] for t in state.get("admission_triggers") or []]}


def _task_workspace_info(state: dict, state_path: str = "") -> tuple[str, list[str]]:
    """任务工作区与候选来源；显式绑定优先，否则要求候选唯一。"""
    explicit = _canonical_workspace(state.get("workspace") or "")
    if explicit:
        return explicit, [explicit]
    candidates: set[str] = set()
    for binding in state.get("execution_bindings") or []:
        if isinstance(binding, dict) and binding.get("workspace"):
            root = _git_workspace(str(binding.get("workspace") or ""))
            if root:
                candidates.add(root)
    source = (state.get("contract") or {}).get("source") or {}
    if isinstance(source, dict) and source.get("path"):
        root = _git_workspace(str(source.get("path") or ""))
        if root:
            candidates.add(root)
    for root in CS.evidence_roots(state):
        value = _git_workspace(root)
        if value:
            candidates.add(value)
    if state_path:
        root = _git_workspace(os.path.dirname(os.path.abspath(state_path)))
        if root:
            candidates.add(root)
    ordered = sorted(candidates)
    return (ordered[0] if len(ordered) == 1 else ""), ordered


def task_workspace(state: dict, state_path: str = "") -> str:
    return _task_workspace_info(state, state_path)[0]


def set_task_workspace(path: str, workspace: str, *, source: str = "zcode") -> dict:
    """首次绑定任务工作区；已绑定或与证据仓库冲突时拒绝。"""
    state = load_state(path)
    value = _canonical_workspace(workspace)
    if not value:
        raise GovernanceError("工作区为空；必须给出实际 git 仓库路径。")
    if not os.path.isdir(value):
        raise GovernanceError(f"工作区不存在：{value}")
    existing, candidates = _task_workspace_info(state, path)
    if existing and existing != value:
        raise GovernanceError(
            "[quality_code_scope_workspace_mismatch] 任务已绑定工作区 "
            f"{existing!r}，不能再改为 {value!r}。")
    if not existing and candidates and value not in candidates:
        raise GovernanceError(
            "[quality_code_scope_workspace_mismatch] 工作区 "
            f"{value!r} 与任务证据/派发上下文不一致：{candidates!r}。")
    if str(state.get("workspace") or "") == value:
        return state
    state["workspace"] = value
    state["workspace_source"] = str(source or "zcode")[:80]
    _history(state, "task_workspace_bound",
             {"workspace": value, "source": state["workspace_source"]})
    save_state(path, state)
    return state


def start_state(path: str, goal: str, *, contract: str = "", criteria: object = None,
                risk: str = "medium", execution_backend: str = "legacy",
                task_id: str = "", mode: str = "", note: str = "",
                contract_source: str = "", workspace: str = "",
                test_policy: dict | None = None,
                task_kind: str = "", triggers: object = None) -> dict:
    if os.path.exists(path):
        raise GovernanceError(f"任务状态已存在：{path}；续跑请直接使用，勿重建。")
    goal = (goal or "").strip()
    if not goal:
        raise GovernanceError("目标为空：任务状态必须绑定目标。")
    task_id = (task_id or "").strip() or "task-" + os.urandom(8).hex()
    if not TASK_ID_RE.match(task_id):
        raise GovernanceError(
            f"task_id={task_id!r} 非法：只允许字母、数字、点、下划线、连字符（≤64）。")
    risk = str(risk or "medium").strip().lower()
    if risk not in RISKS:
        raise GovernanceError(f"risk={risk!r} 非法；只能是 {' / '.join(RISKS)}。")
    if risk == "normal":
        # 历史标签等价于 medium；新状态只保存三级风险。
        risk = "medium"
    backend = str(execution_backend or "legacy").strip().lower()
    if backend not in BACKENDS:
        raise GovernanceError(
            f"execution_backend={execution_backend!r} 非法；只能是 {' / '.join(BACKENDS)}。")
    kind, kind_source = _normalize_task_kind(task_kind)
    admission_triggers = _normalize_triggers(triggers)
    raw_criteria = criteria if criteria else _criteria_from_text(contract)
    items = _normalize_criteria(raw_criteria)
    policy = _normalize_test_policy(test_policy)
    if risk == "high" and not policy.get("captured"):
        raise GovernanceError(
            "risk=high 必须在合同中显式声明 test_policy（required/commands/report/"
            "artifacts 等）；测试要求进入合同指纹，不能由执行方事后自选。")
    # 去掉初始化辅助字段；初始引用在合同确认后由 criterion 显式登记。
    for item in items:
        item.pop("_initial_refs", None)
    if not items:
        raise GovernanceError(
            "完成条件为空：v6 的 PASS 必须逐条核销，请用 --criteria-file 或合同"
            "「## 完成条件」列表给出至少一条条件。")
    workspace_value = _canonical_workspace(workspace)
    if str(workspace or "").strip() and not os.path.isdir(workspace_value):
        raise GovernanceError(f"任务工作区不存在：{workspace_value}")
    state = _default_state()
    state.update({
        "task_id": task_id,
        "goal": goal,
        "goal_sha256": _sha256_text(goal),
        "mode": (mode or "").strip().upper(),
        "note": (note or "").strip(),
        "risk": risk,
        "task_kind": kind,
        "task_kind_source": kind_source,
        "admission_triggers": admission_triggers,
        "execution_backend": backend,
        "workspace": workspace_value,
        "workspace_source": "start" if workspace_value else "",
        "contract_sha256": _contract_fingerprint(goal, contract, items, policy),
    })
    state["contract"].update({
        "done_criteria_text": (contract or "").strip(),
        "boundaries": _section_text(contract, "边界"),
        "criteria": items,
        "criteria_sha256": criteria_digest(items),
        "test_policy": policy,
        # v6 不再等待远程模型确认：完成条件在 start/amend 时由本地合同直接生效。
        "criteria_state": "confirmed",
        "captured": now_iso(),
        "history": [{"event": "criteria_confirmed", "at": now_iso(),
                     "source": "local-start", "criteria_sha256": criteria_digest(items)}],
    })
    source = _contract_source_record(contract_source)
    if source:
        state["contract"]["source"] = source
    _history(state, "task_started", {"task_id": task_id, "risk": risk,
                                     "execution_backend": backend,
                                     "task_kind": kind,
                                     "triggers": [t["name"] for t in admission_triggers]})
    save_state(path, state, expect={"absent": True})
    return state


def _history(state: dict, event: str, detail: dict | None = None) -> None:
    state.setdefault("history", []).append({
        "event": str(event), "at": now_iso(),
        "detail": detail or {},
    })


def amend_contract(path: str, *, contract: str = "",
                   criteria: object = None,
                   contract_source: str = "",
                   test_policy: dict | None = None,
                   task_kind: str = "", triggers: object = None,
                   authorized_by: str = "",
                   authorization_note: str = "") -> dict:
    """用户/任务范围内修订合同与完成条件；保留同描述条件的既有核销记录。

    BLOCKED 时允许修订：阻塞常常正是因为合同条款不可执行，禁止修订只会逼出
    “先假装解除、改完再重新登记”的无效往返。修订不会解除阻塞，任务仍在
    BLOCKED 直到阻塞被真实解决。

    破坏性修订（待核销条件净减少或放松 test_policy）必须由用户明确授权
    （`authorized_by="user"`）：否则执行方可以删掉未满足的条件并重算合同指纹，
    让 G2 从“条件未满足”直接变成“指纹一致”。判定看净减少而不是看描述变化：
    改写措辞、拆分或合并条件只要没有减少仍需核销的条数就放行，只有真正减少
    待完成工作量时才要求授权。PWW 接受条件是系统登记的，不参与这个计数。
    被删除的条件连同旧指纹写入合同历史，审计仍可追溯。

    授权说明（`authorization_note`）在破坏性修订时必填：`authorized_by` 只是
    调用方对用户要求的自报声明，不是可验证凭证；写入说明至少让事后审计能核对
    “用户到底要求了什么”，而不是只看到一个 `user` 字样。历史同时记录
    `authorization_source="caller-declared"` 与新增/删除的未核销条数，避免把
    自报字段当作用户授权证据。
    """
    state = load_state(path)
    _assert_open(state, "修订合同", allow_blocked=True)
    current = state.get("contract") or {}
    body = (contract or current.get("done_criteria_text") or "").strip()
    raw = criteria if criteria else _criteria_from_text(body)
    items = _normalize_criteria(raw)
    if not items:
        raise GovernanceError("修订后完成条件为空；PASS 需要至少一条可核销条件。")
    base_before = [c for c in current.get("criteria") or []
                   if not isinstance(c.get("acceptance"), dict)]
    old = {str(c.get("description") or ""): c for c in base_before}
    new_descriptions = {str(item.get("description") or "") for item in items}
    removed = [c for key, c in old.items() if key not in new_descriptions]
    outstanding_before = sum(
        1 for c in base_before if str(c.get("status") or "") != "met")
    outstanding_after = sum(
        1 for item in items if str(item.get("status") or "") != "met")
    destructive = outstanding_after < outstanding_before
    previous_policy = current.get("test_policy") or _default_test_policy()
    policy = (_normalize_test_policy(test_policy) if test_policy is not None
              else previous_policy)
    weakened = (bool(previous_policy.get("required"))
                and not bool(policy.get("required")))
    note_text = str(authorization_note or "").strip()
    if destructive or weakened:
        detail = []
        if destructive:
            detail.append(
                f"待核销条件净减少（{outstanding_before} → {outstanding_after}"
                + ("，删除 " + "、".join(str(c.get("criterion_id") or "?")
                                        for c in removed)
                   if removed else "") + "）")
        if weakened:
            detail.append("放松 test_policy（required → 未要求）")
        if str(authorized_by or "").strip().lower() != "user":
            raise GovernanceError(
                "合同修订会" + "；".join(detail) + "。这是破坏性修订，必须由用户明确授权："
                "显式传入 authorized_by=\"user\"（CLI：--authorized-by user）并说明原因；"
                "执行方不得自行改写完成条件或合同指纹来通过门禁。")
        if not note_text:
            raise GovernanceError(
                "合同修订会" + "；".join(detail) + "。破坏性修订必须写明用户授权说明"
                "（CLI：--authorization-note \"用户原话/要求\"）；只写 user 而不记录"
                "用户到底要求了什么，事后无法核对授权范围。")
    for item in items:
        item.pop("_initial_refs", None)
        previous = old.get(item["description"])
        if previous and str(previous.get("status") or "") == "met":
            item["status"] = "met"
            item["evidence_refs"] = previous.get("evidence_refs") or []
            item["updated_at"] = previous.get("updated_at")
            item["updated_iteration"] = previous.get("updated_iteration")
            item["history"] = previous.get("history") or []
    if str(state.get("risk") or "").lower() == "high" and not policy.get("captured"):
        raise GovernanceError(
            "risk=high 修订合同时不能移除 test_policy；测试要求必须保留在合同中。")
    base_items = [c for c in current.get("criteria") or []
                  if not isinstance(c.get("acceptance"), dict)]
    contract_unchanged = (
        body == str(current.get("done_criteria_text") or "").strip()
        and criteria_digest(items) == criteria_digest(base_items)
        and policy == (current.get("test_policy") or _default_test_policy()))
    # PWW 接受条件是系统登记的收口前置条件，不因合同修订静默消失；内容不变的
    # 空操作修订保留原接受记录，内容变化时保留为 pending 并让旧接受绑定失效。
    acceptance_previous = [c for c in current.get("criteria") or []
                           if isinstance(c.get("acceptance"), dict)]
    if acceptance_previous:
        used = set()
        for item in items:
            match = CRITERION_ID_RE.match(str(item.get("criterion_id") or ""))
            if match:
                used.add(int(match.group(0)[1:]))
        for previous in acceptance_previous:
            if any(isinstance(item.get("acceptance"), dict) for item in items):
                break
            if contract_unchanged:
                items.append(json.loads(json.dumps(previous)))
                continue
            number = 1
            while number in used:
                number += 1
            used.add(number)
            carried = json.loads(json.dumps(previous))
            carried["criterion_id"] = f"C{number}"
            carried["status"] = "pending"
            carried["evidence_refs"] = []
            carried["updated_at"] = now_iso()
            carried["updated_iteration"] = _current_iteration_id(state)
            carried.setdefault("acceptance", {}).update({
                "status": "pending", "accepted_ids": [], "accepted_by": "",
                "accepted_at": "", "quote": "", "binding_sha256": "",
            })
            items.append(carried)
    sha = criteria_digest(items)
    state["contract"].update({
        "done_criteria_text": body,
        "boundaries": _section_text(body, "边界") or current.get("boundaries") or "",
        "criteria": items,
        "criteria_sha256": sha,
        "test_policy": policy,
        "criteria_state": "confirmed",
        "captured": now_iso(),
    })
    state["contract"].setdefault("history", []).append({
        "event": "contract_amended", "at": now_iso(), "by": "zcode",
        "authorized_by": str(authorized_by or "").strip().lower(),
        # 自报声明不是凭证：显式标注来源，避免把 authorized_by 当作用户授权证据。
        "authorization_source": "caller-declared" if authorized_by else "",
        "authorization_note": note_text[:500],
        "destructive": bool(destructive or weakened),
        "outstanding_before": outstanding_before,
        "outstanding_after": outstanding_after,
        "removed_unmet": sum(1 for c in removed
                             if str(c.get("status") or "") != "met"),
        "added_unmet": sum(1 for item in items
                           if item["description"] not in old),
        "criteria_sha256": sha,
        "previous_criteria_sha256": current.get("criteria_sha256"),
        "criteria": [c["criterion_id"] for c in items],
        "removed_criteria": [
            {"criterion_id": c.get("criterion_id"), "status": c.get("status"),
             "description": c.get("description")} for c in removed],
        "test_policy_weakened": weakened,
    })
    state["contract_sha256"] = _contract_fingerprint(
        state.get("goal") or "", body, items, policy)
    source = _contract_source_record(contract_source)
    if source:
        state["contract"]["source"] = source
    if str(task_kind or "").strip():
        kind, kind_source = _normalize_task_kind(task_kind)
        state["task_kind"] = kind
        state["task_kind_source"] = kind_source
        _history(state, "task_kind_changed", {"task_kind": kind,
                                             "source": kind_source})
    if triggers:
        _add_triggers(state, triggers)
    _history(state, "contract_amended", {"criteria_sha256": sha})
    save_state(path, state)
    return {"contract_sha256": state["contract_sha256"], "criteria_sha256": sha,
            "criteria": items}


def authorize_formal_review(path: str, *, authorized_by: str = "",
                            reason: str = "", scope: str = "") -> dict:
    """记录用户对正式 REVIEW 的明确授权；授权不是门禁，也不自动执行 REVIEW。

    `authorized_by` 是调用方对「用户在该会话中明确要求」的自报声明，不是可验证凭证；
    因此不给默认值，必须由调用方显式传入 `user`，避免把默认参数当成用户要求。
    """
    state = load_state(path)
    # BLOCKED 只阻止收口和改变范围的动作；用户授权本身可以登记，但仍必须显式
    # 声明 authorized_by=user，不能因为 BLOCKED 放宽授权校验。
    _assert_open(state, "记录正式 REVIEW 授权", allow_blocked=True)
    authorized_by = (authorized_by or "").strip()
    reason = (reason or "").strip()
    if authorized_by != "user":
        raise GovernanceError(
            "正式 REVIEW 只能由用户明确授权：必须显式传入 --authorized-by user；"
            "该字段是调用方的声明，不接受默认值。")
    if not reason:
        raise GovernanceError("记录 REVIEW 授权必须写明用户要求与范围。")
    entry = {
        "authorized_by": "user",
        "reason": reason[:MAX_REPORT_CHARS],
        "scope": (scope or "current-task").strip()[:500],
        "at": now_iso(),
        "task_id": state.get("task_id"),
        "contract_sha256": state.get("contract_sha256"),
    }
    state["formal_review_authorization"] = entry
    _history(state, "formal_review_authorized", {"reason": reason[:200]})
    save_state(path, state)
    return entry


def _find_formal_review(state: dict, review_id: str) -> dict:
    target = (review_id or "").strip()
    if not target:
        raise GovernanceError("必须提供正式 REVIEW 的 review_id。")
    for record in state.get("formal_reviews") or []:
        if isinstance(record, dict) and str(record.get("review_id") or "") == target:
            return record
    raise GovernanceError(f"不存在正式 REVIEW 记录：{target}。")


def record_formal_review(path: str, *, verdict: str = "", summary: str = "",
                         review_ref: str = "", reviewer: str = "native",
                         amend: str = "", supersedes: str = "",
                         reason: str = "") -> dict:
    """在用户授权范围内登记正式 REVIEW 结论，或对既有结论追加修订。

    三种语义严格分开：

    * 新结论：不带 ``--amend``/``--supersedes``，必须给出 verdict 与 summary；
    * amendment：只修正引用、摘要或文字口径，必须绑定原 review_id，且目标摘要
      必须仍与原记录一致；它不会生成新的 PASS，也不会继承到新证据；
    * supersede：实质证据/结论变化后登记新结论，必须写明原因并关联旧 review_id；
      旧记录原样保留，由 `review-metrics` 标记为 superseded。

    BLOCKED 下允许登记，但授权、合同绑定和目标摘要校验一条都不放宽。
    """
    state = load_state(path)
    # 正式 REVIEW 是治理记录，不是收口门禁；BLOCKED 下仍可登记或修订，但授权、
    # 合同绑定与目标摘要校验一个都不能少。
    _assert_open(state, "登记正式 REVIEW", allow_blocked=True)
    authorization = state.get("formal_review_authorization") or {}
    if not authorization or str(authorization.get("authorized_by") or "") != "user":
        raise GovernanceError(
            "没有用户对正式 REVIEW 的明确授权；普通开发不得伪造正式 REVIEW。")
    if str(authorization.get("contract_sha256") or "") != str(
            state.get("contract_sha256") or ""):
        raise GovernanceError(
            "正式 REVIEW 授权绑定的是旧合同；合同已修订，需要用户重新授权。")

    amend_id = (amend or "").strip()
    supersedes_id = (supersedes or "").strip()
    reason = (reason or "").strip()
    summary = (summary or "").strip()
    review_ref = (review_ref or "").strip()
    reviewer = (reviewer or "native").strip()[:100]
    if amend_id and supersedes_id:
        raise GovernanceError("--amend 与 --supersedes 不能同时使用。")

    if amend_id:
        if str(verdict or "").strip():
            raise GovernanceError(
                "amendment 继承原结论的 verdict；不要同时传 --verdict。")
        if not reason:
            raise GovernanceError("正式 REVIEW amendment 必须说明修订原因。")
        original = _find_formal_review(state, amend_id)
        current_target = final_evidence_digest(state)
        if current_target != str(original.get("target_sha256") or ""):
            raise GovernanceError(
                "正式 REVIEW 的目标摘要已变化；amendment 不能继承旧 PASS。"
                "请对当前证据登记新的正式 REVIEW，并用 --supersedes 关联原记录。")
        amendment = {
            "amendment_id": "FA" + _sha256_text(canonical_json([
                state.get("task_id"), amend_id,
                len(original.get("amendments") or []),
                reason, summary, review_ref]))[:16],
            "reason": reason[:MAX_REPORT_CHARS],
            "summary": summary[:MAX_REPORT_CHARS],
            "review_ref": review_ref[:1000],
            "reviewer": reviewer,
            "target_sha256": current_target,
            "created": now_iso(),
        }
        original.setdefault("amendments", []).append(amendment)
        _history(state, "formal_review_amended", {
            "review_id": amend_id, "amendment_id": amendment["amendment_id"],
            "reason": reason[:200]})
        save_state(path, state)
        return {"review_id": amend_id, "verdict": original.get("verdict"),
                "amendment": amendment, "review": original}

    verdict = str(verdict or "").strip().lower()
    if verdict not in VERDICTS:
        raise GovernanceError(
            f"formal review verdict={verdict!r} 非法；只能是 {' / '.join(VERDICTS)}。")
    if not summary:
        raise GovernanceError("正式 REVIEW 必须记录结论说明。")
    old = None
    if supersedes_id:
        if not reason:
            raise GovernanceError("supersede 必须说明原结论为何失效。")
        old = _find_formal_review(state, supersedes_id)
    entry = {
        "review_id": "FR" + _sha256_text(
            canonical_json([state.get("task_id"), len(state.get("formal_reviews") or []),
                            summary]))[:16],
        "verdict": verdict,
        "summary": summary[:MAX_REPORT_CHARS],
        "review_ref": review_ref[:1000],
        "reviewer": reviewer,
        "authorization": authorization,
        "target_sha256": final_evidence_digest(state),
        "created": now_iso(),
    }
    if old is not None:
        entry["supersedes"] = str(old.get("review_id") or "")
        entry["supersede_reason"] = reason[:MAX_REPORT_CHARS]
    state.setdefault("formal_reviews", []).append(entry)
    _history(state, "formal_review_recorded", {
        "verdict": verdict, "review_id": entry["review_id"],
        "supersedes": entry.get("supersedes", "")})
    save_state(path, state)
    return entry


# ── 工作项 / 计划 ──────────────────────────────────────────────────────────
def _detect_cycle(edges: dict[str, list[str]]) -> str | None:
    marks: dict[str, int] = {}

    def visit(node: str, stack: list[str]) -> str | None:
        mark = marks.get(node, 0)
        if mark == 1:
            return " → ".join(stack[stack.index(node):] + [node])
        if mark == 2:
            return None
        marks[node] = 1
        for dep in edges.get(node, []):
            found = visit(dep, stack + [node])
            if found:
                return found
        marks[node] = 2
        return None

    for node in edges:
        found = visit(node, [])
        if found:
            return found
    return None


def _validate_actions(raw: object, known_ids: set[str]) -> list[dict]:
    items = _list(raw, "actions")
    if not items:
        raise GovernanceError("计划必须至少包含一个工作项。")
    if len(items) > MAX_ACTIONS:
        raise GovernanceError(f"单次计划最多 {MAX_ACTIONS} 个工作项。")
    out: list[dict] = []
    seen: set[str] = set()
    for idx, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise GovernanceError(f"actions[{idx}] 必须是对象。")
        aid = str(item.get("id") or "").strip()
        if not ACTION_ID_RE.match(aid):
            raise GovernanceError(f"actions[{idx}].id={aid!r} 非法。")
        if aid in seen:
            raise GovernanceError(f"工作项 id {aid} 在计划中重复。")
        seen.add(aid)
        assignee = str(item.get("assignee") or "zcode").strip().lower()
        if assignee not in ASSIGNEES:
            raise GovernanceError(
                f"{aid} 的 assignee={assignee!r} 非法；只能是 {' / '.join(ASSIGNEES)}。")
        title = str(item.get("title") or "").strip()
        goal = str(item.get("goal") or "").strip()
        expected = str(item.get("expected_evidence") or "").strip()
        if not title or not goal or not expected:
            raise GovernanceError(f"{aid} 必须包含 title / goal / expected_evidence。")
        deps = item.get("depends_on") or []
        if not isinstance(deps, list):
            raise GovernanceError(f"{aid}.depends_on 必须是数组。")
        deps = [str(d).strip() for d in deps if str(d).strip()]
        targets = item.get("target_criteria") or []
        if not isinstance(targets, list):
            raise GovernanceError(f"{aid}.target_criteria 必须是数组。")
        targets = list(dict.fromkeys(
            str(cid).strip() for cid in targets if str(cid).strip()))
        out.append({
            "id": aid,
            "title": title[:300],
            "assignee": assignee,
            "goal": goal[:4000],
            "constraints": str(item.get("constraints") or "").strip()[:2000],
            "expected_evidence": expected[:2000],
            "depends_on": deps,
            # 计划工作项可以声明自己承载的完成条件：派生/调度据此避免对同一条件
            # 重复生成工作项，阻塞时也能自动归属条件。值必须真实存在于合同中。
            "target_criteria": targets,
        })
    edges = {item["id"]: item["depends_on"] for item in out}
    universe = known_ids | {item["id"] for item in out}
    for item in out:
        for dep in item["depends_on"]:
            if dep not in universe:
                raise GovernanceError(f"{item['id']} 依赖不存在的工作项 {dep}。")
            if dep == item["id"]:
                raise GovernanceError(f"{item['id']} 不能依赖自身。")
    cycle = _detect_cycle(edges)
    if cycle:
        raise GovernanceError(f"工作项依赖成环：{cycle}")
    return out


def plan_state(path: str, plan: dict, *, source: str = "zcode") -> dict:
    """把 ZCode 本地计划写入状态（不调用任何模型）。"""
    state = load_state(path)
    _assert_open(state, "登记计划")
    if not isinstance(plan, dict):
        raise GovernanceError("计划顶层必须是对象。")
    summary = str(plan.get("summary") or "").strip()[:2000]
    raw_actions = plan.get("actions")
    known = {str(a.get("id") or "") for a in state.get("actions") or []}
    actions = _validate_actions(raw_actions, known)
    known_criteria = {str(c.get("criterion_id") or "")
                      for c in (state.get("contract") or {}).get("criteria") or []}
    declared_targets: dict[str, list[str]] = {}
    for item in actions:
        # target_criteria 不进入计划摘要：它是调度/归属元数据，不改变计划正文；
        # 放进摘要会让既有计划的指纹全部漂移（native 派发会判定 plan_drift）。
        targets = item.pop("target_criteria", [])
        unknown = [cid for cid in targets if cid not in known_criteria]
        if unknown:
            raise GovernanceError(
                f"{item['id']}.target_criteria 引用了不存在的完成条件："
                + "、".join(unknown) + "。")
        if targets:
            declared_targets[item["id"]] = targets
    plan_sha = _sha256_text(canonical_json({"summary": summary, "actions": actions}))
    for existing in state.get("plans") or []:
        if str(existing.get("sha256") or "") == plan_sha:
            return {"plan": existing, "actions": [], "idempotent": True}
    # 工作项 id 全任务唯一：跨计划重复会让 `_by_id` 永远命中第一条，第二条
    # 永远 pending，G1 无法收口，`drive` 反复输出同一工作单。
    duplicates = sorted({item["id"] for item in actions} & known)
    if duplicates:
        raise GovernanceError(
            "工作项 id 已在其他计划中登记，不能跨计划重复："
            + ", ".join(duplicates))
    plan_id = "P" + _sha256_text(canonical_json([state.get("task_id"), plan_sha]))[:16]
    created: list[dict] = []
    now = now_iso()
    for item in actions:
        action = dict(item)
        action.update({
            "status": "pending",
            "result": "",
            "evidence_ids": [],
            "fail_streak": 0,
            "origin": f"{source}-plan",
            "plan_id": plan_id,
            "created": now,
            "updated": now,
        })
        if declared_targets.get(item["id"]):
            action["target_criteria"] = declared_targets[item["id"]]
        state["actions"].append(action)
        created.append(action)
    record = {
        "plan_id": plan_id,
        "sha256": plan_sha,
        "summary": summary,
        "action_ids": [item["id"] for item in actions],
        "source": source,
        "created": now,
    }
    state.setdefault("plans", []).append(record)
    _history(state, "plan_registered", {"plan_id": plan_id, "sha256": plan_sha,
                                        "action_ids": record["action_ids"]})
    save_state(path, state)
    return {"plan": record, "actions": created, "idempotent": False}


def _by_id(state: dict, aid: str) -> dict:
    for action in state.get("actions") or []:
        if str(action.get("id") or "") == (aid or "").strip():
            return action
    raise GovernanceError(f"不存在的工作项：{aid}。")


def _tracked_actions(state: dict) -> list[dict]:
    """当前仍参与治理的工作项。

    v7 及更早的高风险任务可能留有 `origin=g4-review` 工作项；G4 已退役，
    这些历史记录只读保留，不再参与可领取、停摆、G1 或恢复派生。
    """
    out = []
    for action in state.get("actions") or []:
        if not isinstance(action, dict):
            continue
        origin = str(action.get("origin") or "")
        aid = str(action.get("id") or "")
        # 旧代码可能已从 G4 缺口派生过 AUTO-R-AUTO-G4*；它们同样是历史残留。
        if origin == "g4-review" or aid.startswith(
                ("AUTO-G4", "AUTO-R-AUTO-G4")):
            continue
        out.append(action)
    return out


def _active_blocks(state: dict) -> list[dict]:
    return [b for b in state.get("blocks") or [] if not b.get("resolved_at")]


def _task_blocks(state: dict) -> list[dict]:
    """任务级阻塞：整条任务暂停，没有工作项范围。

    只有目标/合同/资源整体不可推进时才允许；单条操作的授权缺口必须用
    `work_items` 关联到具体工作项，不能冻结其余工作。
    """
    return [b for b in _active_blocks(state) if not b.get("work_items")]


def _item_block_map(state: dict) -> dict[str, dict]:
    """工作项 id → 阻塞记录（只含未解除的项级阻塞）。"""
    out: dict[str, dict] = {}
    for block in _active_blocks(state):
        for aid in block.get("work_items") or []:
            out.setdefault(str(aid), block)
    return out


def _blocked_work_items(state: dict) -> list[dict]:
    """仍处于活动状态、且被项级阻塞冻结的工作项。"""
    mapping = _item_block_map(state)
    out = []
    for action in _tracked_actions(state):
        aid = str(action.get("id") or "")
        if aid not in mapping:
            continue
        if str(action.get("status") or "") in TERMINAL:
            continue
        out.append(action)
    return out


def _blocked_criteria(state: dict) -> set[str]:
    """被未解除工作项阻塞一并冻结的完成条件 id。"""
    out: set[str] = set()
    for block in _active_blocks(state):
        for cid in block.get("criteria") or []:
            out.add(str(cid))
    return out


def _action_criteria(action: dict) -> list[str]:
    """工作项声明（或由 AUTO-C<n> 隐含）的完成条件。"""
    declared = action.get("target_criteria") or action.get("criteria") or []
    if isinstance(declared, str):
        declared = [declared]
    out = [str(item or "").strip() for item in declared
           if str(item or "").strip()]
    if not out:
        match = re.fullmatch(r"AUTO-C([0-9]{1,3})", str(action.get("id") or ""))
        if match:
            out = [f"C{match.group(1)}"]
    return out


# ── 调度领取（claims）────────────────────────────────────────────────────
# `drive` 把工作项交给某个会话时写入一条短租约：并发会话不会拿到同一项；
# 租约到期自动失效，不会把工作永久锁死。领取只是调度事实，不是执行完成。
CLAIM_TTL_ENV = "COPILOT_CLAIM_TTL"
DEFAULT_CLAIM_TTL = 1800
LOCAL_CLAIM_OWNER = "local"


def claim_owner() -> str:
    """当前领取方：会话标识；无会话标识（手工/测试）时用 local。"""
    return current_session_id() or LOCAL_CLAIM_OWNER


def _claim_ttl() -> int:
    raw = str(os.environ.get(CLAIM_TTL_ENV, "")).strip()
    if not raw:
        return DEFAULT_CLAIM_TTL
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CLAIM_TTL
    return value if value >= 0 else DEFAULT_CLAIM_TTL


def _claim_active(claim: dict, now: datetime | None = None) -> bool:
    expires = _parse_time(claim.get("expires_at"))
    if expires is None:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > (now or datetime.now(timezone.utc))


def active_claims(state: dict, *, now: datetime | None = None) -> dict[str, dict]:
    """未过期领取记录：work_item_id → claim。"""
    out: dict[str, dict] = {}
    for claim in state.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        aid = str(claim.get("work_item_id") or "")
        if aid and _claim_active(claim, now):
            out[aid] = claim
    return out


def _drop_claim(state: dict, aid: str) -> None:
    """工作项回填、撤销或阻塞后，对应租约不再有意义。"""
    claims = state.get("claims")
    if not isinstance(claims, list):
        state["claims"] = []
        return
    state["claims"] = [claim for claim in claims
                       if not (isinstance(claim, dict)
                               and str(claim.get("work_item_id") or "")
                               == str(aid or ""))]


def claim_actions(state: dict, ids: list[str], *, owner: str = "",
                  ttl: int | None = None) -> dict:
    """为本次调度领取工作项；返回 owner / claimed / busy / ttl。

    同一领取方可重复领取（重试或再次 drive），不同领取方的有效租约不会被抢占。
    过期租约在本次领取时被清理，因此崩溃后最多等待 TTL 即可重新领取。
    """
    resolved_owner = str(owner or "").strip() or claim_owner()
    now = datetime.now(timezone.utc)
    ttl = _claim_ttl() if ttl is None else max(0, int(ttl))
    active = active_claims(state, now=now)
    claims = [claim for claim in state.get("claims") or []
              if isinstance(claim, dict) and _claim_active(claim, now)]
    by_aid = {str(claim.get("work_item_id") or ""): claim for claim in claims}
    expires_at = (now + timedelta(seconds=ttl)).isoformat()
    claimed: list[str] = []
    busy: list[str] = []
    for raw in ids:
        aid = str(raw or "").strip()
        if not aid:
            continue
        current = active.get(aid)
        if current is not None and str(current.get("owner") or "") != resolved_owner:
            busy.append(aid)
            continue
        previous = by_aid.get(aid) or {}
        handoff = int(previous.get("handoff_count") or 0) + 1
        record = {
            "work_item_id": aid,
            "owner": resolved_owner,
            "claim_id": "K" + _sha256_text(canonical_json(
                [state.get("task_id"), aid, resolved_owner,
                 now.isoformat(), handoff]))[:12],
            "acquired": now.isoformat(),
            "expires_at": expires_at,
            "handoff_count": handoff,
        }
        claims = [claim for claim in claims
                  if str(claim.get("work_item_id") or "") != aid]
        claims.append(record)
        by_aid[aid] = record
        claimed.append(aid)
    state["claims"] = claims
    return {"owner": resolved_owner, "claimed": claimed, "busy": busy,
            "ttl": ttl}


def ready_actions(state: dict, *, owner: str | None = None,
                  include_claimed: bool = False) -> list[dict]:
    if bool((state.get("closure") or {}).get("closed")):
        return []
    # 任务级阻塞冻结全部工作；项级阻塞只冻结被标记的工作项。
    if _task_blocks(state):
        return []
    blocked_ids = set(_item_block_map(state))
    mine = claim_owner() if owner is None else str(owner or "")
    claims = {} if include_claimed else active_claims(state)
    actions = _tracked_actions(state)
    done = {a["id"] for a in actions
            if str(a.get("status") or "") == "done"}
    out = []
    for action in actions:
        if str(action.get("status") or "") != "pending":
            continue
        aid = str(action.get("id") or "")
        if aid in blocked_ids:
            continue
        claim = claims.get(aid)
        if claim is not None and str(claim.get("owner") or "") != mine:
            continue
        if all(dep in done for dep in action.get("depends_on") or []):
            out.append(action)
    return out


def stalled_actions(state: dict) -> list[tuple[dict, str]]:
    out: list[tuple[dict, str]] = []
    blocked_ids = set(_item_block_map(state))
    actions = _tracked_actions(state)
    by_id = {a["id"]: a for a in actions}
    for action in actions:
        aid = str(action.get("id") or "")
        # 项级阻塞是已登记的真实阻塞，不是停摆：由 continuation 单独呈现，
        # 不派生恢复工作项，也不把它的下游依赖算作停摆。
        if aid in blocked_ids:
            continue
        if any(dep in blocked_ids for dep in action.get("depends_on") or []):
            continue
        status = str(action.get("status") or "")
        if status in ("running", "failed", "blocked"):
            out.append((action, f"{status}：需重新 record 或说明真实阻塞"))
            continue
        if status != "pending":
            continue
        missing = [d for d in action.get("depends_on") or [] if d not in by_id]
        if missing:
            out.append((action, f"依赖 {', '.join(missing)} 在状态中不存在"))
            continue
        blockers = [d for d in action.get("depends_on") or []
                    if str((by_id.get(d) or {}).get("status") or "")
                    in ("dropped", "failed", "blocked")]
        if blockers:
            out.append((action, f"依赖 {', '.join(blockers)} 不可满足或尚需解阻"))
    return out


def _validate_evidence_refs(state: dict, refs: list) -> list[dict]:
    task_id = str(state.get("task_id") or "")
    out: list[dict] = []
    if not isinstance(refs, list):
        raise GovernanceError("evidence_refs 必须是数组。")
    for raw in refs:
        if isinstance(raw, str):
            eid = raw.strip()
        elif isinstance(raw, dict):
            eid = str(raw.get("id") or "").strip()
        else:
            raise GovernanceError("证据引用必须是证据 id 或对象。")
        if not EVIDENCE_ID_RE.match(eid):
            raise GovernanceError(f"证据 id {eid!r} 非法（应形如 E1）。")
        entry = _evidence_entry(state, eid)
        if entry is None or entry.get("dropped"):
            raise GovernanceError(f"证据 {eid} 未登记或已归档；请先登记证据。")
        if str(entry.get("task_id") or "") != task_id:
            raise GovernanceError(f"证据 {eid} 不属于任务 {task_id}；拒绝跨任务引用。")
        expected = str(entry.get("sha256") or "")
        hint = str(entry.get("path_hint") or "")
        if not expected:
            raise GovernanceError(f"证据 {eid} 缺少摘要；引用无效。")
        if hint and hint != "<inline>":
            if not os.path.isfile(hint):
                raise GovernanceError(f"证据 {eid} 的文件不存在或不是普通文件：{hint}")
            try:
                actual = _sha256_file(hint)
            except OSError as exc:
                raise GovernanceError(f"证据 {eid} 无法重读：{exc}") from None
            if actual != expected:
                raise GovernanceError(f"证据 {eid} 的文件摘要与登记不一致；拒绝引用。")
        if eid not in [r["id"] for r in out]:
            out.append({"id": eid, "sha256": expected})
    return out


def record_action_state(state: dict, aid: str, status: str, *,
                        result: str = "",
                        evidence_ids: list[str] | None = None) -> dict:
    """在内存状态里记录工作项结果，不落盘；调用方负责与其它写入一次保存。"""
    # 阻塞期间其他工作仍可能完成；记录真实执行结果不等于解除阻塞。
    _assert_open(state, "回填工作项", allow_blocked=True)
    status = str(status or "").strip().lower()
    if status not in STATUSES:
        raise GovernanceError(f"status={status!r} 非法；只能是 {' / '.join(STATUSES)}。")
    action = _by_id(state, aid)
    current = str(action.get("status") or "")
    if current in TERMINAL and status != current:
        raise GovernanceError(
            f"{aid} 已终结（{current}），不能再改为 {status}；请新建工作项。")
    refs = _validate_evidence_refs(state, evidence_ids or [])
    result = (result or "").strip()
    if status == "done" and not result:
        raise GovernanceError(f"{aid} 标记 done 时必须说明实际结果。")
    if status == "dropped" and not result:
        raise GovernanceError(f"{aid} 标记 dropped 时必须说明放弃原因。")
    action["status"] = status
    if result:
        action["result"] = result[:MAX_RESULT_CHARS]
    action["evidence_ids"] = [r["id"] for r in refs]
    action["updated"] = now_iso()
    # 回填即表示该次领取已结束；无论结果是 done/failed/blocked，租约都不再有效。
    _drop_claim(state, aid)
    if status == "failed":
        action["fail_streak"] = int(action.get("fail_streak") or 0) + 1
    elif status == "done":
        action["fail_streak"] = 0
    _history(state, "action_recorded", {"id": aid, "status": status,
                                        "evidence_ids": action["evidence_ids"]})
    return action


def record_action(path: str, aid: str, status: str, *, result: str = "",
                  evidence_ids: list[str] | None = None) -> dict:
    state = load_state(path)
    action = record_action_state(state, aid, status, result=result,
                                 evidence_ids=evidence_ids)
    save_state(path, state)
    return action


def rerun_action(path: str, aid: str, *, reason: str = "") -> dict:
    """把已终结工作项重新打开为 pending，用于修复后的重新验证。

    之前的结果不会被覆盖：旧状态、结果、证据与时间写入 `rerun_history`，
    仍可从状态与 `native_results` 追溯。只允许对 done/dropped 的工作项重开；
    这不是“重置任务”，不改变合同、条件或其他工作项。
    """
    state = load_state(path)
    # 修复后重验可能发生在阻塞期间；重开只影响工作项生命周期，不解除阻塞。
    _assert_open(state, "重开工作项", allow_blocked=True)
    action = _by_id(state, aid)
    current = str(action.get("status") or "")
    if current not in TERMINAL:
        raise GovernanceError(
            f"{aid} 当前状态是 {current or '-'}，不需要重开；只有已终结工作项"
            "（done/dropped）才能 rerun。")
    action.setdefault("rerun_history", []).append({
        "status": current,
        "result": action.get("result"),
        "evidence_ids": action.get("evidence_ids") or [],
        "updated": action.get("updated"),
        "reason": (reason or "").strip()[:1000],
        "at": now_iso(),
    })
    action["status"] = "pending"
    action["result"] = ""
    action["evidence_ids"] = []
    action["fail_streak"] = 0
    action["rerun_count"] = int(action.get("rerun_count") or 0) + 1
    action["updated"] = now_iso()
    _drop_claim(state, aid)
    _history(state, "action_rerun", {"id": aid, "previous": current,
                                     "reason": (reason or "").strip()[:500]})
    save_state(path, state)
    return action


def _criterion_work_items(state: dict, cid: str) -> list[dict]:
    """该完成条件自动派生的包装工作项（AUTO-<cid> 或 AUTO-* 声明了 target_criteria）。

    只收自动派生项：计划里人写的工作项即使声明了 target_criteria，也可能是
    有独立价值的任务，不能因为条件被别的证据核销就自动撤销。
    """
    out: list[dict] = []
    for item in state.get("actions") or []:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("id") or "")
        targets = item.get("target_criteria")
        if aid == f"AUTO-{cid}" or (
                aid.startswith("AUTO-") and isinstance(targets, list)
                and cid in targets):
            out.append(item)
    return out


def _sync_criterion_work_items(state: dict, cid: str) -> list[str]:
    """条件核销/撤销时同步它自动派生的包装工作项。

    `AUTO-<cid>` 的含义就是“去闭合这个条件”：条件 met 后再要求 record 这条工作项
    只是重复劳动，还会让 PARTIAL 永远差一条。只自动收口仍然 pending、没有结果与
    证据的包装项，并用 dropped（不是 done）记录，表示“随条件闭合而撤销”，不虚构
    执行结果；条件重新变成未闭合时（reopen / criterion 回退）恢复为 pending。
    """
    criterion = _criterion(state, cid)
    met = str(criterion.get("status") or "") == "met"
    changed: list[str] = []
    for item in _criterion_work_items(state, cid):
        aid = str(item.get("id") or "")
        status = str(item.get("status") or "")
        if met:
            if status != "pending" or item.get("result") or item.get("evidence_ids"):
                continue
            item["status"] = "dropped"
            item["result"] = f"随完成条件 {cid} 核销自动撤销：条件已 met，无独立工作"
            item["auto_closed_for"] = cid
            item["updated"] = now_iso()
            _drop_claim(state, aid)
            changed.append(aid)
            _history(state, "work_auto_closed", {"id": aid, "criterion_id": cid})
        else:
            if status != "dropped" or str(item.get("auto_closed_for") or "") != cid:
                continue
            item["status"] = "pending"
            item["result"] = ""
            item["auto_closed_for"] = ""
            item["updated"] = now_iso()
            changed.append(aid)
            _history(state, "work_auto_reopened", {"id": aid, "criterion_id": cid})
    return changed


def record_criterion(path: str, cid: str, status: str, *,
                     evidence_ids: list[str] | None = None) -> dict:
    state = load_state(path)
    # 条件核销仍要求本任务已登记证据；BLOCKED 只表示外部条件未满足，
    # 不阻止记录已经完成的核销动作。收口仍会被 block 单独拒绝。
    _assert_open(state, "核销完成条件", allow_blocked=True)
    status = str(status or "").strip().lower()
    if status not in CRITERION_STATUSES:
        raise GovernanceError(
            f"criterion status={status!r} 非法；只能是 {' / '.join(CRITERION_STATUSES)}。")
    criterion = _criterion(state, cid)
    if isinstance(criterion.get("acceptance"), dict):
        raise GovernanceError(
            f"{cid} 是 PWW 用户接受条件：只能由用户明确表达后经 accept 命令登记；"
            "criterion 命令不得直接核销或改写该条件。")
    refs = _validate_evidence_refs(state, evidence_ids or [])
    if status == "met" and not refs:
        raise GovernanceError(f"{cid} 判定 met 时必须引用至少一条已登记证据。")
    criterion["status"] = status
    criterion["evidence_refs"] = refs
    criterion["updated_at"] = now_iso()
    criterion["updated_iteration"] = _current_iteration_id(state)
    criterion.setdefault("history", []).append({
        "status": status, "at": now_iso(), "evidence_refs": refs,
    })
    _history(state, "criterion_recorded", {"criterion_id": cid, "status": status,
                                           "evidence_ids": [r["id"] for r in refs]})
    _sync_criterion_work_items(state, cid)
    save_state(path, state)
    return criterion


def _criterion(state: dict, cid: str) -> dict:
    for criterion in (state.get("contract") or {}).get("criteria") or []:
        if str(criterion.get("criterion_id") or "") == (cid or "").strip():
            return criterion
    raise GovernanceError(f"不存在完成条件：{cid}。")


def record_judgment(path: str, status: str, summary: str, *,
                    evidence_ids: list[str] | None = None) -> dict:
    """记录 ZCode 的自我判断；只作审计，不替代本地 G1–G3。"""
    state = load_state(path)
    # 判断只作审计，不参与 G1–G3；BLOCKED 下保留真实判断记录。
    _assert_open(state, "记录判断", allow_blocked=True)
    status = str(status or "").strip().lower()
    if status not in VERDICTS:
        raise GovernanceError(f"judgment status={status!r} 非法；只能是 {' / '.join(VERDICTS)}。")
    summary = (summary or "").strip()
    if not summary:
        raise GovernanceError("判断必须附一句话说明。")
    refs = _validate_evidence_refs(state, evidence_ids or [])
    entry = {
        "judgment_id": "J" + _sha256_text(
            canonical_json([state.get("task_id"), len(state.get("judgments") or []),
                            summary]))[:16],
        "status": status,
        "summary": summary[:MAX_REPORT_CHARS],
        "evidence_ids": [r["id"] for r in refs],
        "iteration_id": _current_iteration_id(state),
        "source": "zcode",
        "created": now_iso(),
    }
    state.setdefault("judgments", []).append(entry)
    _history(state, "judgment_recorded", {"status": status, "summary": summary[:200]})
    save_state(path, state)
    return entry


def add_block(path: str, reason: str, needs: str,
              *, work_items: list[str] | None = None,
              criteria: list[str] | None = None,
              force: bool = False) -> dict:
    """登记真实阻塞。

    `work_items` 非空时只冻结这些工作项（其余工作继续推进），这是授权缺口
    （commit / push / 部署 / 生产数据 / 付费 / 用户决策）的默认用法；不指定
    工作项即任务级阻塞，只在目标或资源整体不可推进时使用。`criteria` 用于声明
    这些工作项同时卡住的完成条件，避免对不可执行的缺口重复派生工作项。

    `force` 只用于任务级阻塞：登记时仍有可执行工作项时，调用方必须显式确认
    “整体不可推进”。函数把该事实写入 block 供审计；CLI 会拒绝未加 --force 的
    任务级阻塞，避免单项授权缺口冻结其余工作。
    """
    state = load_state(path)
    # 已有阻塞时仍要允许登记第二个真实阻塞；否则只能改写旧记录或丢失事实。
    _assert_open(state, "记录阻塞", allow_blocked=True)
    reason = (reason or "").strip()
    needs = (needs or "").strip()
    if not reason or not needs:
        raise GovernanceError("BLOCKED 必须同时说明原因（--reason）与所需条件（--needs）。")
    items: list[str] = []
    auto_criteria: list[str] = []
    for raw in work_items or []:
        aid = str(raw or "").strip()
        if not aid or aid in items:
            continue
        action = _by_id(state, aid)
        items.append(aid)
        # 工作项可以在计划期声明自己承载的完成条件（target_criteria），派生工作项
        # AUTO-C<n> 天然承载 C<n>。阻塞时自动归属这些条件：否则条件悬空会被反复
        # 派生为新的“可执行”工作项，任务永远到不了真实 BLOCKED。
        for cid in _action_criteria(action):
            if cid not in auto_criteria:
                auto_criteria.append(cid)
    ready_now = [] if items else [
        str(action.get("id") or "") for action in
        ready_actions(state, include_claimed=True)]
    criterion_ids: list[str] = []
    known = {str(c.get("criterion_id") or "")
             for c in (state.get("contract") or {}).get("criteria") or []}
    explicit = [str(raw or "").strip() for raw in criteria or []]
    # 显式 --criterion 优先（并校验存在性）；未显式声明时用工作项自身的归属。
    # 自动归属只接受合同内真实存在的条件，拼写错误不会被悄悄冻结。
    for raw in (explicit if explicit else auto_criteria):
        cid = str(raw or "").strip()
        if not cid or cid in criterion_ids:
            continue
        if cid not in known:
            if explicit:
                raise GovernanceError(f"不存在完成条件：{cid}。")
            continue
        criterion_ids.append(cid)
    block = {
        "block_id": "B" + _sha256_text(
            canonical_json([state.get("task_id"), len(state.get("blocks") or []),
                            reason]))[:16],
        "scope": "work-items" if items else "task",
        "work_items": items,
        "criteria": criterion_ids,
        "reason": reason[:MAX_REPORT_CHARS],
        "needs": needs[:MAX_REPORT_CHARS],
        # 任务级阻塞登记时仍存在可执行项：记录事实，供审计判断是否本应只阻塞
        # 单项。CLI 默认拒绝这种用法，只有显式 --force 才会写入。
        "ready_at_creation": ready_now,
        "forced_scope": bool(ready_now and force),
        "created": now_iso(),
        "resolved_at": None,
        "resolved_by": None,
        "resolution": "",
    }
    state.setdefault("blocks", []).append(block)
    now = now_iso()
    for aid in items:
        action = _by_id(state, aid)
        if str(action.get("status") or "") in TERMINAL:
            continue
        action["status"] = "blocked"
        action["blocked_by"] = block["block_id"]
        action["blocked_reason"] = block["reason"]
        action["blocked_needs"] = block["needs"]
        action["updated"] = now
        # 阻塞即中止本次领取；解除阻塞后重新进入 pending，由下一次 drive 领取。
        _drop_claim(state, aid)
    _history(state, "block_added", {"block_id": block["block_id"],
                                    "scope": block["scope"],
                                    "work_items": items,
                                    "reason": reason[:200]})
    save_state(path, state)
    return block


def resolve_block(path: str, reason: str, block_id: str = "", *,
                  by: str = "zcode", quote: str = "") -> dict:
    state = load_state(path)
    if bool((state.get("closure") or {}).get("closed")):
        raise GovernanceError("任务已收口（CLOSED），不能解除阻塞；请先 reopen。")
    reason = (reason or "").strip()
    if not reason:
        raise GovernanceError("解除阻塞必须说明依据（--reason）。")
    resolved_by = str(by or "zcode").strip().lower()
    if resolved_by not in ("user", "zcode", "agent"):
        raise GovernanceError(
            f"unblock --by={by!r} 非法；只能是 user / zcode / agent。")
    quote = (quote or "").strip()
    if resolved_by == "user" and not quote:
        raise GovernanceError(
            "unblock --by user 必须同时提供 --quote \"<用户原话>\"："
            "用户授权是调用方声明，需要保留原话供审计核对，不能只写结论。")
    active = [b for b in state.get("blocks") or [] if not b.get("resolved_at")]
    if not active:
        raise GovernanceError("当前没有未解除的阻塞。")
    target_id = (block_id or "").strip()
    if target_id:
        target = [b for b in active
                  if str(b.get("block_id") or "") == target_id]
        if not target:
            raise GovernanceError(f"不存在未解除的阻塞：{target_id}。")
    else:
        # 允许多个真实阻塞后，一次 unblock 不能把不同原因一起解除；只有单条
        # 未解除阻塞时才允许省略 block_id。
        if len(active) != 1:
            raise GovernanceError(
                f"当前有 {len(active)} 条未解除阻塞；请用 --block-id 指定要解除"
                "的阻塞，不要用一条理由把不同阻塞一起解除。")
        target = active
    for block in target:
        block["resolved_at"] = now_iso()
        block["resolved_by"] = resolved_by
        # 与合同修订/重开同一口径：user 是调用方自报声明，不是可验证凭证；
        # 原话留在记录里供审计核对授权范围。
        block["authorization_source"] = (
            "caller-declared" if resolved_by == "user" else "")
        block["authorization_quote"] = quote[:MAX_REPORT_CHARS]
        block["resolution"] = reason[:MAX_REPORT_CHARS]
    ids = [str(b.get("block_id") or "") for b in target]
    # 项级阻塞解除后，被它冻结的工作项恢复可执行；这是「用户授权更新后对应
    # 工作项恢复」的唯一入口，不需要重新建工作项。
    resumed: list[str] = []
    now = now_iso()
    for block in target:
        for aid in block.get("work_items") or []:
            try:
                action = _by_id(state, aid)
            except GovernanceError:
                continue
            if str(action.get("status") or "") != "blocked":
                continue
            # 同一工作项可能叠加多条真实阻塞：只有全部解除才恢复可执行。
            # 只按 blocked_by 单值判定会在解除第一条时把它错误放行。
            remaining = [b for b in state.get("blocks") or []
                         if not b.get("resolved_at")
                         and aid in (b.get("work_items") or [])]
            if remaining:
                action["blocked_by"] = remaining[0].get("block_id")
                action["blocked_reason"] = remaining[0].get("reason") or ""
                action["blocked_needs"] = remaining[0].get("needs") or ""
                action["updated"] = now
                continue
            action["status"] = "pending"
            action["blocked_by"] = None
            action["blocked_reason"] = ""
            action["blocked_needs"] = ""
            action["updated"] = now
            resumed.append(aid)
    _history(state, "block_resolved", {"count": len(target), "block_ids": ids,
                                       "resumed_work_items": resumed,
                                       "resolved_by": resolved_by,
                                       "quote": quote[:500],
                                       "reason": reason[:200]})
    save_state(path, state)
    return {"resolved": len(target), "block_ids": ids, "resumed": resumed,
            "resolved_by": resolved_by, "reason": reason}


def add_decision(path: str, text: str, *, source: str = "user") -> dict:
    state = load_state(path)
    _assert_open(state, "记录决策")
    text = (text or "").strip()
    if not text:
        raise GovernanceError("决策文本不能为空。")
    entry = {"text": text[:MAX_REPORT_CHARS], "source": source,
             "at": now_iso()}
    state.setdefault("decisions", []).append(entry)
    _history(state, "decision_recorded", {"source": source, "text": text[:200]})
    save_state(path, state)
    return entry


def add_report(path: str, summary: str, *, blocked: bool = False,
               reason: str = "") -> dict:
    state = load_state(path)
    # 报告是执行事实，不是收口依据；BLOCKED 下允许登记真实进展。
    _assert_open(state, "登记报告", allow_blocked=True)
    summary = (summary or "").strip()
    if not summary:
        raise GovernanceError("报告内容不能为空。")
    entry = {"kind": "execution", "summary": summary[:MAX_REPORT_CHARS],
             "report_sha256": _sha256_text(summary), "blocked": bool(blocked),
             "reason": (reason or "").strip()[:MAX_REPORT_CHARS],
             "iteration_id": _current_iteration_id(state), "created": now_iso()}
    state.setdefault("reports", []).append(entry)
    _history(state, "report_recorded", {"sha256": entry["report_sha256"],
                                        "blocked": bool(blocked)})
    save_state(path, state)
    return entry


# ── 质量结论（verify 结构化报告） ──────────────────────────────────────────
# verify 产出的机器可读结论走固定结构（`copilot-verify-result/v1`）。Copilot 在
# 报告接入路径解析它：PWW 自动登记用户接受条件并绑定报告/警告/目标摘要；解析或
# 校验失败一律 fail-closed，不按普通 PASS 处理。
_QUALITY_CHECKS = ("required_tests_executed", "required_evidence_present",
                   "completion_criteria_met", "delivery_verifiable")
_QUALITY_MARKER = "<!-- copilot-verify-result/v1 -->"


def _quality_verdict(raw: object) -> str:
    text = str(raw or "").strip().replace("-", "_").lower()
    if text not in QUALITY_VERDICTS:
        raise GovernanceError(
            f"[verify_report_invalid] verdict={raw!r} 非法；只能是 "
            f"{' / '.join(v.upper() for v in QUALITY_VERDICTS)}。")
    return text


def _quality_items(raw: object, name: str) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise GovernanceError(f"[verify_report_invalid] {name} 必须是数组。")
    pattern = QUALITY_WARNING_RE if name == "warnings" else QUALITY_RISK_RE
    out: list[dict] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise GovernanceError(
                f"[verify_report_invalid] {name} 第 {idx} 项必须是对象。")
        rid = str(item.get("id") or "").strip()
        text = str(item.get("text") or "").strip()
        if not pattern.match(rid):
            raise GovernanceError(
                f"[verify_report_invalid] {name} id={rid!r} 非法（应形如 "
                f"{'W1' if name == 'warnings' else 'R1'}）。")
        if rid in seen:
            raise GovernanceError(f"[verify_report_invalid] {name} id {rid} 重复。")
        if not text:
            raise GovernanceError(
                f"[verify_report_invalid] {name} {rid} 缺少 text。")
        allowed_keys = {"id", "text", "severity"}
        unknown_keys = sorted(set(item) - allowed_keys)
        if unknown_keys:
            raise GovernanceError(
                f"[verify_report_invalid] {name} {rid} 含未知字段："
                + "、".join(unknown_keys)
                + "；质量报告不允许静默丢弃字段。")
        seen.add(rid)
        entry = {"id": rid, "text": text[:2000]}
        severity = str(item.get("severity") or "").strip().lower()
        if severity:
            if severity not in ("info", "minor", "major", "blocking"):
                raise GovernanceError(
                    f"[verify_report_invalid] {name} {rid} severity={severity!r} "
                    "非法；只能是 info / minor / major / blocking。")
            entry["severity"] = severity
        out.append(entry)
    return sorted(out, key=lambda item: int(re.sub(r"\D", "", item["id"])))


def _quality_unverified(raw: object) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise GovernanceError("[verify_report_invalid] unverified 必须是数组。")
    out = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            raise GovernanceError(
                "[verify_report_invalid] unverified 条目不能为空字符串。")
        out.append(text[:2000])
    return out


def _quality_checks(raw: object, *, required: bool) -> dict:
    if raw is None:
        if required:
            raise GovernanceError(
                "[verify_report_invalid] PASS/PASS_WITH_WARNINGS 必须提供 checks，"
                f"且 {'/'.join(_QUALITY_CHECKS)} 全为 true。")
        return {}
    if not isinstance(raw, dict):
        raise GovernanceError("[verify_report_invalid] checks 必须是对象。")
    out = {}
    for key, value in raw.items():
        if key not in _QUALITY_CHECKS:
            raise GovernanceError(f"[verify_report_invalid] checks.{key} 不是已知检查项。")
        if not isinstance(value, bool):
            raise GovernanceError(
                f"[verify_report_invalid] checks.{key} 必须是布尔值。")
        out[key] = value
    if required:
        missing = [key for key in _QUALITY_CHECKS if key not in out]
        failed = [key for key in _QUALITY_CHECKS if out.get(key) is not True]
        if missing or failed:
            raise GovernanceError(
                "[verify_pww_precondition_failed] PASS/PASS_WITH_WARNINGS 要求 "
                + "、".join(_QUALITY_CHECKS) + " 全部为 true；"
                + (f"缺少 {', '.join(missing)}；" if missing else "")
                + (f"未通过 {', '.join(failed)}。" if failed else ""))
    return out


def _quality_subject(raw: object) -> tuple[dict, str]:
    if not isinstance(raw, dict) or not raw:
        raise GovernanceError(
            "[verify_report_invalid] subject 必须是非空对象，记录本次验收覆盖的"
            "代码/产物范围（至少含 scope/commit/diff_sha256/files 之一）。")
    allowed = ("scope", "commit", "diff_sha256", "files", "summary")
    if not any(raw.get(key) for key in allowed):
        raise GovernanceError(
            "[verify_report_invalid] subject 缺少可核对的目标摘要；至少给出 "
            "scope/commit/diff_sha256/files/summary 之一。")
    if "commit" in raw and raw.get("commit") not in (None, ""):
        if not QUALITY_COMMIT_RE.match(str(raw.get("commit") or "")):
            raise GovernanceError(
                "[verify_report_invalid] subject.commit 必须是 40 位小写 Git SHA。")
    if "code_scope_sha256" in raw:
        value = str(raw.get("code_scope_sha256") or "")
        if not QUALITY_SHA256_RE.match(value):
            raise GovernanceError(
                "[verify_report_invalid] subject.code_scope_sha256 必须是 64 位小写 SHA-256。")
    if "dirty" in raw and not isinstance(raw.get("dirty"), bool):
        raise GovernanceError(
            "[verify_report_invalid] subject.dirty 必须是布尔值。")
    files = raw.get("files")
    if files is not None:
        if not isinstance(files, list):
            raise GovernanceError(
                "[verify_report_invalid] subject.files 必须是数组（干净工作区可用空数组）。")
        for item in files:
            if not isinstance(item, str):
                raise GovernanceError(
                    "[verify_report_invalid] subject.files 的每一项都必须是字符串路径；"
                    "code_scope_capture.py 的 files_paths 字段可直接使用。")
            path = item.strip()
            if not path or os.path.isabs(path) or ".." in path.replace("\\", "/").split("/"):
                raise GovernanceError(
                    "[verify_report_invalid] subject.files 必须是工作区内的相对路径。")
    return raw, _sha256_text(canonical_json(raw))


def _quality_resolved_ids(raw: object, name: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise GovernanceError(f"[verify_report_invalid] {name} 必须是数组。")
    pattern = QUALITY_WARNING_RE if name == "resolved_warning_ids" else QUALITY_RISK_RE
    out = []
    for item in raw:
        value = str(item or "").strip()
        if not pattern.match(value):
            raise GovernanceError(
                f"[verify_report_invalid] {name} 含非法 id {value!r}。")
        out.append(value)
    if len(set(out)) != len(out):
        raise GovernanceError(f"[verify_report_invalid] {name} 存在重复 id。")
    return out


def _quality_payload(block: dict, *, legacy: bool = False) -> dict:
    """进入报告摘要的规范载荷；report_id 不参与，重新运行由 generated_at 区分。

    v6.7.0 历史报告没有 supersede/resolved 字段，重算摘要时必须沿用旧载荷，
    否则历史报告会被误判为篡改。
    """
    payload = {
        "schema": QUALITY_SCHEMA,
        "verdict": block.get("verdict"),
        "warnings": block.get("warnings") or [],
        "residual_risks": block.get("residual_risks") or [],
        "unverified": block.get("unverified") or [],
        "acceptance_required": block.get("acceptance_required"),
        "checks": block.get("checks") or {},
        "subject": block.get("subject") or {},
    }
    generated_at = str(block.get("generated_at") or "")
    if generated_at or not legacy:
        # v6.7.0 当时没有 generated_at 字段时不写入空串；v6.7.1 报告必填。
        payload["generated_at"] = generated_at
    if not legacy:
        payload.update({
            "supersedes_report_id": block.get("supersedes_report_id") or "",
            "resolved_warning_ids": block.get("resolved_warning_ids") or [],
            "resolved_residual_risk_ids":
                block.get("resolved_residual_risk_ids") or [],
        })
    return payload


def _quality_report_payload(report: dict) -> dict:
    return _quality_payload(
        report, legacy=report.get("ingest_version") != QUALITY_INGEST_VERSION)


def _extract_quality_json(text: str) -> object:
    raw = (text or "").strip()
    if not raw:
        raise GovernanceError("[verify_report_missing] 质量报告为空。")
    lines = raw.splitlines()
    marker = next((i for i, line in enumerate(lines)
                   if line.strip() == _QUALITY_MARKER), None)
    if marker is None:
        raise GovernanceError(
            f"[verify_report_marker_missing] 未找到 {_QUALITY_MARKER}；"
            "不能只凭正文里的 PASS_WITH_WARNINGS 文本触发治理。")
    candidate = ""
    in_fence = False
    for line in lines[marker + 1:]:
        if not in_fence:
            if line.strip().startswith("```"):
                in_fence = True
            continue
        if line.strip().startswith("```"):
            break
        candidate += line + "\n"
    if not candidate.strip():
        raise GovernanceError(
            "[verify_report_marker_missing] 标记后没有 JSON 代码块。")
    try:
        return json.loads(candidate)
    except ValueError as exc:
        raise GovernanceError(f"[verify_report_invalid] JSON 解析失败：{exc}") from None


def parse_quality_report(text: str) -> dict:
    """解析并校验 verify 结构化结论；任何缺字段/自相矛盾都失败关闭。"""
    value = _extract_quality_json(text)
    if not isinstance(value, dict):
        raise GovernanceError("[verify_report_invalid] 质量结论必须是 JSON 对象。")
    unknown = sorted(set(value) - QUALITY_FIELDS)
    if unknown:
        raise GovernanceError(
            "[verify_report_unknown_field] 质量结论包含未知字段："
            + "、".join(unknown)
            + "；替代/解决关系必须使用契约字段，不能静默丢弃。")
    if str(value.get("schema") or "").strip() != QUALITY_SCHEMA:
        raise GovernanceError(
            f"[verify_report_invalid] schema 必须是 {QUALITY_SCHEMA}。")
    raw_verdict = str(value.get("verdict") or "").strip()
    verdict = _quality_verdict(raw_verdict)
    report_id = str(value.get("report_id") or "").strip()
    if not QUALITY_REPORT_ID_RE.match(report_id):
        raise GovernanceError(
            "[verify_report_invalid] report_id 缺失或非法（字母数字与 ._-，≤64）。")
    warnings = _quality_items(value.get("warnings"), "warnings")
    residual = _quality_items(value.get("residual_risks"), "residual_risks")
    unverified = _quality_unverified(value.get("unverified"))
    required = value.get("acceptance_required")
    if not isinstance(required, bool):
        raise GovernanceError(
            "[verify_report_invalid] acceptance_required 必须是布尔值。")
    status = str(value.get("acceptance_status") or "").strip().lower()
    subject, subject_digest = _quality_subject(value.get("subject"))
    supersedes = str(value.get("supersedes_report_id") or "").strip()
    if supersedes and not QUALITY_REPORT_ID_RE.match(supersedes):
        raise GovernanceError(
            "[verify_report_invalid] supersedes_report_id 非法（字母数字与 ._-，≤64）。")
    resolved_warnings = _quality_resolved_ids(
        value.get("resolved_warning_ids"), "resolved_warning_ids")
    resolved_risks = _quality_resolved_ids(
        value.get("resolved_residual_risk_ids"), "resolved_residual_risk_ids")
    generated_at = str(value.get("generated_at") or "").strip()
    if not generated_at:
        raise GovernanceError(
            "[verify_report_invalid] generated_at 缺失；每次验收必须记录生成时间，"
            "用于区分重新验收与重复接入。")

    if verdict == PWW:
        if not required:
            raise GovernanceError(
                "[verify_report_invalid] PASS_WITH_WARNINGS 必须 acceptance_required=true。")
        if status and status != "pending":
            raise GovernanceError(
                "[verify_acceptance_forbidden] verify 只能写 acceptance_status=pending；"
                "用户接受必须由 Copilot 的 accept 命令登记。")
        if not status:
            raise GovernanceError(
                "[verify_report_invalid] PASS_WITH_WARNINGS 必须显式声明 "
                "acceptance_status=pending。")
        if not warnings and not residual:
            raise GovernanceError(
                "[verify_report_invalid] PASS_WITH_WARNINGS 必须列出 warnings 或 "
                "residual_risks，供用户逐条接受。")
        if unverified:
            raise GovernanceError(
                "[verify_pww_has_unverified] 仍有未验证项，不得使用 PASS_WITH_WARNINGS；"
                "按 verify 语义改判 PARTIAL/BLOCKED。")
        severe = [item["id"] for item in warnings + residual
                  if item.get("severity") in ("blocking", "major")]
        if severe:
            raise GovernanceError(
                "[verify_pww_blocking_finding] "
                f"{', '.join(severe)} 标记为 blocking/major，不得使用 PASS_WITH_WARNINGS。")
        checks = _quality_checks(value.get("checks"), required=True)
        status = "pending"
    else:
        if required:
            raise GovernanceError(
                "[verify_report_invalid] 非 PASS_WITH_WARNINGS 不能要求用户接受。")
        if status not in ("", "not_required"):
            raise GovernanceError(
                "[verify_report_invalid] 非 PASS_WITH_WARNINGS 的 acceptance_status "
                "只能是 not_required。")
        if verdict == "pass":
            if warnings or residual or unverified:
                raise GovernanceError(
                    "[verify_report_invalid] PASS 不得携带 warnings/residual_risks/"
                    "unverified；有残余风险请用 PASS_WITH_WARNINGS。")
            checks = _quality_checks(value.get("checks"), required=True)
        else:
            checks = _quality_checks(value.get("checks"), required=False)
        status = "not_required"
    block = {
        "schema": QUALITY_SCHEMA,
        "report_id": report_id,
        "raw_verdict": raw_verdict,
        "verdict": verdict,
        "warnings": warnings,
        "residual_risks": residual,
        "unverified": unverified,
        "acceptance_required": required,
        "acceptance_status": status,
        "checks": checks,
        "subject": subject,
        "subject_digest": subject_digest,
        "generated_at": generated_at[:64],
        "supersedes_report_id": supersedes,
        "resolved_warning_ids": resolved_warnings,
        "resolved_residual_risk_ids": resolved_risks,
    }
    block["report_sha256"] = _sha256_text(canonical_json(_quality_payload(block)))
    return block


def parse_quality_report_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise GovernanceError(f"[verify_report_missing] 无法读取 {path}：{exc}") from None
    block = parse_quality_report(text)
    block["file_sha256"] = _sha256_file(path)
    block["source_path"] = os.path.abspath(path)
    return block


def _quality_required_ids(report: dict) -> list[str]:
    ids = [str(item.get("id") or "") for item in report.get("warnings") or []]
    ids += [str(item.get("id") or "") for item in report.get("residual_risks") or []]
    order = {item["id"]: idx for idx, item in enumerate(
        (report.get("warnings") or []) + (report.get("residual_risks") or []))}
    return sorted([i for i in ids if i], key=lambda i: order[i])


def _quality_report_structurally_valid(report: dict) -> bool:
    """state 可能被手工改写：进入摘要/绑定/门禁前先做结构校验。"""
    if not isinstance(report, dict):
        return False
    if not isinstance(report.get("subject"), dict):
        return False
    if "checks" in report and not isinstance(report.get("checks"), dict):
        return False
    if report.get("acceptance") is not None and \
            not isinstance(report.get("acceptance"), dict):
        return False
    for key in ("warnings", "residual_risks"):
        value = report.get(key, [])
        if not isinstance(value, list):
            return False
        for item in value:
            if not isinstance(item, dict):
                return False
            if not isinstance(item.get("id"), str) or \
                    not isinstance(item.get("text"), str):
                return False
            if "severity" in item and not isinstance(item.get("severity"), str):
                return False
    unverified = report.get("unverified", [])
    if not isinstance(unverified, list) or not all(
            isinstance(item, str) for item in unverified):
        return False
    unresolved = report.get("unresolved")
    if unresolved is not None and not isinstance(unresolved, list):
        return False
    return True


def _unresolved_entry(kind: str, item: dict, report_id: str,
                      subject_digest: str) -> dict:
    return {
        "kind": kind,
        "id": str(item.get("id") or ""),
        "text": str(item.get("text") or "")[:2000],
        "origin_report_id": report_id,
        "origin_subject_digest": subject_digest,
    }


def _report_unresolved(report: dict) -> list[dict]:
    """报告继承的未解决 W/R；已接受 id 不再算未解决。"""
    if not _quality_report_structurally_valid(report):
        return []
    acceptance = report.get("acceptance")
    if not isinstance(acceptance, dict):
        acceptance = {}
    accepted = {str(value) for value in acceptance.get("accepted_ids") or []}
    stored = report.get("unresolved")
    if isinstance(stored, list):
        out = []
        for item in stored:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            rid = str(item.get("id") or "")
            if kind not in ("warning", "residual_risk") or not rid or rid in accepted:
                continue
            out.append({
                "kind": kind, "id": rid,
                "text": str(item.get("text") or "")[:2000],
                "origin_report_id": str(item.get("origin_report_id") or ""),
                "origin_subject_digest": str(
                    item.get("origin_subject_digest") or ""),
            })
        return out
    if str(report.get("verdict") or "") != PWW:
        return []
    report_id = str(report.get("report_id") or "")
    subject_digest = str(report.get("subject_digest") or "")
    out = []
    for kind, key in (("warning", "warnings"), ("residual_risk", "residual_risks")):
        for item in report.get(key) or []:
            rid = str(item.get("id") or "")
            if rid and rid not in accepted:
                out.append(_unresolved_entry(kind, item, report_id, subject_digest))
    return out


def _quality_code_declared(subject: dict) -> bool:
    return any(subject.get(key) not in (None, "", [], {}) for key in
               ("commit", "diff_sha256", "files", "code_scope_sha256"))


def _snapshot_dirty(snapshot: dict) -> bool:
    if snapshot.get("files"):
        return True

    def any_dirty(items: object) -> bool:
        if not isinstance(items, list):
            return False
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("dirty"):
                return True
            if any_dirty(item.get("nested") or []):
                return True
        return False

    return any_dirty(snapshot.get("submodule_worktrees")) or \
        any_dirty(snapshot.get("nested_repos"))


def _path_covered(path: str, declared: list[str]) -> bool:
    value = str(path or "").replace("\\", "/")
    for item in declared:
        rel = str(item or "").replace("\\", "/").strip("/")
        if not rel:
            continue
        if value == rel or value.startswith(rel + "/"):
            return True
    return False


def _capture_quality_code_scope(state: dict, state_path: str,
                                report_file: str, block: dict,
                                workspace: str) -> dict | None:
    """把报告声明的代码范围与 ingest 时工作区快照核对并持久化。"""
    subject = block.get("subject") or {}
    if not _quality_code_declared(subject):
        return None
    commit = str(subject.get("commit") or "")
    declared_sha = str(subject.get("code_scope_sha256") or "")
    files = subject.get("files")
    if not QUALITY_COMMIT_RE.match(commit):
        raise GovernanceError(
            "[verify_code_scope_missing] 代码类报告必须在 subject.commit 给出 40 位 Git SHA。")
    if not QUALITY_SHA256_RE.match(declared_sha):
        raise GovernanceError(
            "[verify_code_scope_missing] 代码类报告必须给出 subject.code_scope_sha256"
            "（由 code_scope_capture.py 在 verify 时生成）。")
    if files is not None and not isinstance(files, list):
        raise GovernanceError(
            "[verify_code_scope_missing] subject.files 必须是数组。")
    workspace = str(workspace or "").strip()
    if not workspace:
        raise GovernanceError(
            "[verify_code_scope_workspace_required] 代码类报告接入必须用 --workspace "
            "指定实际工作区，不能由 Copilot 猜测。")
    requested = _canonical_workspace(workspace)
    if not os.path.isdir(requested):
        raise GovernanceError(
            f"[verify_code_scope_unavailable] 工作区不存在：{requested}")
    task_ws, candidates = _task_workspace_info(state, state_path)
    if not task_ws:
        raise GovernanceError(
            "[quality_code_scope_workspace_unbound] 任务没有唯一工作区绑定；"
            f"候选={candidates or '无'}。请用 start --workspace 创建任务，"
            "或在任务仓库内登记证据后再接入代码报告。")
    if requested != task_ws:
        raise GovernanceError(
            "[quality_code_scope_workspace_mismatch] 报告工作区 "
            f"{requested!r} 与任务绑定工作区 {task_ws!r} 不一致；"
            "不能用另一个仓库的代码快照冒充任务代码范围。")
    if not str(state.get("workspace") or ""):
        state["workspace"] = task_ws
        state["workspace_source"] = "derived"
        _history(state, "task_workspace_bound",
                 {"workspace": task_ws, "source": "derived"})
    workspace = task_ws
    report_abs = os.path.realpath(os.path.abspath(report_file))
    task_dir = os.path.realpath(os.path.dirname(os.path.abspath(state_path)))
    if (report_abs == workspace or report_abs.startswith(workspace + os.sep)) \
            and not (report_abs == task_dir
                     or report_abs.startswith(task_dir + os.sep)):
        raise GovernanceError(
            "[verify_code_scope_report_in_workspace] verify 报告文件必须放在任务"
            "目录或工作区外；放在代码工作区内会使报告自身变更无法与代码范围"
            "区分。")
    # 只排除治理文件与报告文件本身；不排除已登记证据文件——证据如果位于
    # 工作区内且发生变更，同样属于代码范围，不能借“已登记为证据”从快照消失。
    excludes = CS.governance_paths(state_path)
    excludes.append(report_abs)
    snapshot = CS.capture_code_snapshot(workspace, exclude_paths=sorted(set(excludes)))
    if not snapshot.get("available"):
        raise GovernanceError(
            "[verify_code_scope_unavailable] 无法读取工作区代码快照："
            f"{snapshot.get('reason') or 'unknown'}")
    if str(snapshot.get("git_head") or "") != commit:
        raise GovernanceError(
            "[quality_code_scope_commit_mismatch] 报告 commit="
            f"{commit or '-'} 与工作区 HEAD={snapshot.get('git_head') or '-'} 不一致。")
    if str(snapshot.get("digest") or "") != declared_sha:
        raise GovernanceError(
            "[quality_code_scope_mismatch] 报告 code_scope_sha256 与当前工作区快照"
            "不一致；报告不是针对当前代码生成的。")
    diff_sha = str(subject.get("diff_sha256") or "")
    if diff_sha and diff_sha != declared_sha:
        raise GovernanceError(
            "[quality_code_scope_mismatch] subject.diff_sha256 与 code_scope_sha256 "
            "不一致；一个报告只能绑定一个代码快照。")
    dirty = _snapshot_dirty(snapshot)
    if "dirty" in subject and bool(subject.get("dirty")) != dirty:
        raise GovernanceError(
            "[quality_code_scope_dirty_mismatch] subject.dirty 与工作区实际状态不一致。")
    root = str(snapshot.get("git_root") or "")
    declared = [str(item) for item in (files or [])]
    if snapshot.get("files") and not declared:
        raise GovernanceError(
            "[verify_code_scope_missing] 工作区有变更时，subject.files 必须列出"
            "覆盖范围。")
    for entry in snapshot.get("files") or []:
        for key in ("path", "old_path"):
            value = str(entry.get(key) or "")
            if value and not _path_covered(value, declared):
                raise GovernanceError(
                    "[quality_code_scope_files_incomplete] 工作区变更 "
                    f"{value} 不在报告声明的 files 范围内。")
    changed = set()
    for entry in snapshot.get("files") or []:
        for key in ("path", "old_path"):
            if entry.get(key):
                changed.add(str(entry[key]).replace("\\", "/"))
    for item in declared:
        rel = str(item).replace("\\", "/").strip("/")
        if rel in changed:
            continue
        target = os.path.abspath(os.path.join(root, rel))
        if os.path.commonpath([root, target]) != root or not os.path.exists(target):
            raise GovernanceError(
                "[quality_code_scope_files_unknown] 报告声明的路径不存在且不在变更集："
                f"{item}")
    snapshot["dirty"] = dirty
    # 这是 Copilot ingest 时的重算快照，不是“报告生成时刻”的证明。报告声明
    # 的 generation-time 摘要保存在 subject.code_scope_sha256，并已进入
    # report_sha256；此处显式标注来源，避免把 ingest 重算冒充生成时快照。
    snapshot.update({
        "workspace": task_ws,
        "workspace_source": str(state.get("workspace_source") or "derived"),
        "capture_source": "copilot-ingest-recompute",
        "captured_at": now_iso(),
        "report_id": str(block.get("report_id") or ""),
        "report_sha256": str(block.get("report_sha256") or ""),
        "report_file_sha256": str(block.get("file_sha256") or ""),
        "report_generated_at": str(block.get("generated_at") or ""),
        "declared_sha256": declared_sha,
        "task_id": str(state.get("task_id") or ""),
    })
    return snapshot


def _code_scope_status(report: dict, state: dict | None = None,
                       cache: dict | None = None) -> dict:
    """当前工作区是否仍等于报告绑定的代码快照。

    `cache` 为单次命令内的快照复用容器（见 `CS.capture_code_snapshot`）。
    """
    subject = report.get("subject") or {}
    snapshot = report.get("code_scope")
    if not isinstance(snapshot, dict) or not snapshot:
        if _quality_code_declared(subject) and \
                report.get("ingest_version") == QUALITY_INGEST_VERSION:
            return {"status": "missing", "reasons": ["quality_code_scope_missing"]}
        return {"status": "legacy" if _quality_code_declared(subject)
                else "not_required", "reasons": []}
    if not snapshot.get("available") or not snapshot.get("digest"):
        return {"status": "unavailable",
                "reasons": ["quality_code_scope_unavailable"]}
    stored_sha = str(report.get("code_scope_sha256") or "")
    snapshot_ws = _canonical_workspace(snapshot.get("workspace") or "")
    if state is not None:
        task_ws, candidates = _task_workspace_info(state)
        if not task_ws:
            return {"status": "unbound",
                    "reasons": ["quality_code_scope_workspace_unbound"]}
        if snapshot_ws != task_ws:
            return {"status": "workspace_mismatch",
                    "reasons": ["quality_code_scope_workspace_mismatch"]}
        if str(snapshot.get("task_id") or "") != str(
                state.get("task_id") or ""):
            return {"status": "inconsistent",
                    "reasons": ["quality_code_scope_inconsistent"]}
    if str(snapshot.get("report_id") or "") != str(
            report.get("report_id") or "") or \
            str(snapshot.get("report_sha256") or "") != str(
            report.get("report_sha256") or "") or \
            str(snapshot.get("declared_sha256") or "") != stored_sha or \
            stored_sha != str(snapshot.get("digest") or "") or \
            str(subject.get("code_scope_sha256") or "") != stored_sha or \
            str(subject.get("commit") or "") != str(snapshot.get("git_head") or ""):
        return {"status": "inconsistent",
                "reasons": ["quality_code_scope_inconsistent"]}
    current = CS.capture_code_snapshot(
        snapshot_ws,
        exclude_paths=sorted(set(snapshot.get("excluded_paths") or [])),
        base=str(snapshot.get("git_base") or ""), cache=cache)
    if not current.get("available"):
        return {"status": "unavailable",
                "reasons": ["quality_code_scope_unavailable"]}
    if str(current.get("digest") or "") != stored_sha:
        return {"status": "drift", "reasons": ["quality_code_scope_drift"]}
    return {"status": "current", "reasons": [], "digest": stored_sha,
            "git_head": current.get("git_head") or "",
            "workspace": snapshot_ws,
            "capture_source": snapshot.get("capture_source") or "",
            "captured_at": snapshot.get("captured_at") or ""}


def _supersede_plan(old: dict, block: dict) -> dict:
    """按“旧未解决 W/R + 新报告 + 明确解决声明”计算继承与解决关系。"""
    old_unresolved = _report_unresolved(old)
    old_map = {(entry["kind"], entry["id"]): entry for entry in old_unresolved}
    resolved_w = [str(value) for value in block.get("resolved_warning_ids") or []]
    resolved_r = [str(value) for value in
                  block.get("resolved_residual_risk_ids") or []]
    resolved_keys = {("warning", value) for value in resolved_w} | \
        {("residual_risk", value) for value in resolved_r}
    unknown = sorted(key for key in resolved_keys if key not in old_map)
    if unknown:
        raise GovernanceError(
            "[pww_supersede_coverage_incomplete] 解决声明包含不属于旧报告的 id："
            + "、".join(value for _kind, value in unknown))
    own = {}
    for kind, key in (("warning", "warnings"), ("residual_risk", "residual_risks")):
        for item in block.get(key) or []:
            rid = str(item.get("id") or "")
            if rid:
                own[(kind, rid)] = _unresolved_entry(
                    kind, item, str(block.get("report_id") or ""),
                    str(block.get("subject_digest") or ""))
    contradiction = sorted(key for key in resolved_keys if key in own)
    if contradiction:
        raise GovernanceError(
            "[pww_supersede_coverage_incomplete] 同一 id 既声明已解决又列入新报告："
            + "、".join(value for _kind, value in contradiction))
    carried: list[dict] = []
    resolved: list[dict] = []
    verdict = str(block.get("verdict") or "")
    for key, entry in old_map.items():
        if key in own:
            carried.append(entry)
        elif key in resolved_keys:
            if str(block.get("subject_digest") or "") != \
                    str(entry.get("origin_subject_digest") or ""):
                raise GovernanceError(
                    "[pww_supersede_subject_mismatch] 报告声称解决 "
                    f"{entry['id']}，但 subject 与警告来源报告不一致；"
                    "不能自动证明该范围已重新验证。")
            resolved.append(entry)
        elif verdict not in ("pass", PWW):
            # 非 PASS/PWW 报告不能收口；旧未解决项继续留在链上，防止借
            # FAIL/PARTIAL 中转后由后续 PASS 洗掉。
            carried.append(entry)
        else:
            raise GovernanceError(
                "[pww_supersede_coverage_incomplete] 旧报告未解决的 "
                f"{entry['id']} 既未继承到新报告，也未声明解决。")
    new_entries = carried + [entry for key, entry in own.items()
                             if key not in {(e["kind"], e["id"]) for e in carried}]
    if str(block.get("verdict") or "") == "pass" and new_entries:
        raise GovernanceError(
            "[pww_superseded_unresolved] PASS 报告仍继承未解决警告，不能作为通过结论。")
    return {
        "old_unresolved": old_unresolved,
        "new_unresolved": new_entries,
        "carried": [entry["id"] for entry in carried],
        "resolved": [entry["id"] for entry in resolved],
        "resolved_warning_ids": resolved_w,
        "resolved_residual_risk_ids": resolved_r,
    }


def _validate_supersede_chain(state: dict, active: dict) -> list[str]:
    """G2 复核 active 报告的替代关系与 unresolved 是否自洽。"""
    if active.get("ingest_version") != QUALITY_INGEST_VERSION:
        # v6.7.0 状态没有显式替代关系，按历史兼容保留，不伪造新规则。
        return []
    reports = [item for item in state.get("quality_reports") or []
               if isinstance(item, dict)]
    by_id = {str(item.get("report_id") or ""): item for item in reports}
    target_id = str(active.get("supersedes_report_id") or "")
    older = [item for item in reports
             if str(item.get("report_id") or "") !=
             str(active.get("report_id") or "")]
    if not target_id:
        if older:
            return ["pww_supersede_missing"]
        return []
    target = by_id.get(target_id)
    if target is None or str(target.get("superseded_by") or "") != \
            str(active.get("report_id") or "") or target.get("superseded_at") is None:
        return ["pww_supersede_invalid_target"]
    if not _quality_report_structurally_valid(target):
        return ["pww_supersede_target_tampered"]
    try:
        target_sha = _sha256_text(canonical_json(_quality_report_payload(target)))
    except (TypeError, ValueError, KeyError):
        return ["pww_supersede_target_tampered"]
    if target_sha != str(target.get("report_sha256") or ""):
        return ["pww_supersede_target_tampered"]
    block = {
        "report_id": active.get("report_id"),
        "verdict": active.get("verdict"),
        "warnings": active.get("warnings") or [],
        "residual_risks": active.get("residual_risks") or [],
        "subject_digest": active.get("subject_digest"),
        "resolved_warning_ids": active.get("resolved_warning_ids") or [],
        "resolved_residual_risk_ids": active.get("resolved_residual_risk_ids") or [],
    }
    try:
        plan = _supersede_plan(target, block)
    except GovernanceError as exc:
        match = re.match(r"^\[([a-z0-9_]+)\]", str(exc))
        return [match.group(1) if match else "pww_supersede_coverage_incomplete"]
    expected = {(entry["kind"], entry["id"], entry["origin_report_id"],
                 entry["origin_subject_digest"])
                for entry in plan["new_unresolved"]}
    stored_entries = active.get("unresolved")
    if not isinstance(stored_entries, list):
        stored_entries = _report_unresolved(active)
    stored = {(str(entry.get("kind") or ""), str(entry.get("id") or ""),
               str(entry.get("origin_report_id") or ""),
               str(entry.get("origin_subject_digest") or ""))
              for entry in stored_entries if isinstance(entry, dict)}
    if expected != stored:
        return ["pww_supersede_inconsistent"]
    return []


def _acceptance_criterion(state: dict) -> dict | None:
    for criterion in (state.get("contract") or {}).get("criteria") or []:
        if isinstance(criterion.get("acceptance"), dict):
            return criterion
    return None


def _next_criterion_id(state: dict) -> str:
    used = set()
    for criterion in (state.get("contract") or {}).get("criteria") or []:
        match = CRITERION_ID_RE.match(str(criterion.get("criterion_id") or ""))
        if match:
            used.add(int(match.group(0)[1:]))
    number = 1
    while number in used:
        number += 1
    if number > 999:
        raise GovernanceError("完成条件 id 已用尽（C1–C999）。")
    return f"C{number}"


def _refresh_contract_digests(state: dict) -> None:
    """合同条件集合变化后同步 fingerprint，保证 G2 与复核绑定口径一致。"""
    contract = state.setdefault("contract", {})
    criteria = contract.get("criteria") or []
    contract["criteria_sha256"] = criteria_digest(criteria)
    contract["criteria_state"] = "confirmed"
    state["contract_sha256"] = _contract_fingerprint(
        state.get("goal") or "", contract.get("done_criteria_text") or "",
        criteria, contract.get("test_policy") or {})


def _ensure_acceptance_criterion(state: dict, report: dict,
                                 ids: list[str] | None = None) -> dict:
    required = list(ids) if ids is not None else _quality_required_ids(report)
    summary = "、".join(required)
    description = (f"用户明确接受质量报告 {report['report_id']} 的未解决警告与"
                   f"残余风险（{summary}）；Copilot 只登记用户明确表达，"
                   "执行方不得代替用户接受。")
    criterion = _acceptance_criterion(state)
    at = now_iso()
    if criterion is None:
        criterion = {
            "criterion_id": _next_criterion_id(state),
            "description": description,
            "status": "pending",
            "evidence_refs": [],
            "updated_at": at,
            "updated_iteration": _current_iteration_id(state),
            "history": [],
        }
        (state.setdefault("contract", {}).setdefault("criteria", [])
         ).append(criterion)
        _history(state, "pww_acceptance_condition_created",
                 {"criterion_id": criterion["criterion_id"],
                  "report_id": report["report_id"],
                  "required_ids": required})
    else:
        previous = (criterion.get("acceptance") or {}).get("report_id")
        criterion["description"] = description
        criterion["status"] = "pending"
        criterion["evidence_refs"] = []
        criterion["updated_at"] = at
        _history(state, "pww_acceptance_condition_updated",
                 {"criterion_id": criterion["criterion_id"],
                  "previous_report_id": previous,
                  "report_id": report["report_id"],
                  "required_ids": required})
    criterion["acceptance"] = {
        "report_id": report["report_id"],
        "report_sha256": report["report_sha256"],
        "required_ids": required,
        "accepted_ids": [],
        "status": "pending",
        "accepted_by": "",
        "accepted_at": "",
        "quote": "",
        "binding_sha256": "",
    }
    _refresh_contract_digests(state)
    return criterion


def _retire_acceptance_criterion(state: dict) -> None:
    criterion = _acceptance_criterion(state)
    if criterion is None:
        return
    criteria = state.setdefault("contract", {}).setdefault("criteria", [])
    criteria[:] = [c for c in criteria if c is not criterion]
    _refresh_contract_digests(state)
    _history(state, "pww_acceptance_condition_retired",
             {"criterion_id": criterion.get("criterion_id"),
              "report_id": (criterion.get("acceptance") or {}).get("report_id")})


def _quality_binding_payload(state: dict, report: dict, *,
                             include_subject: bool,
                             include_code_scope: bool = True,
                             include_identity: bool = False) -> dict:
    payload = {
        "task_id": state.get("task_id"),
        "goal_sha256": state.get("goal_sha256"),
        "contract_sha256": state.get("contract_sha256"),
        "criteria_sha256": (state.get("contract") or {}).get("criteria_sha256"),
        "report_sha256": report.get("report_sha256"),
        "warnings": [item.get("id") for item in report.get("warnings") or []],
        "residual_risks": [item.get("id") for item in report.get("residual_risks") or []],
    }
    if include_identity:
        payload["report_id"] = report.get("report_id")
        payload["workspace"] = report.get("workspace") or ""
    if include_subject:
        payload["subject_digest"] = report.get("subject_digest")
        if include_code_scope:
            payload["code_scope_sha256"] = report.get("code_scope_sha256") or ""
    return payload


def _quality_binding_candidates(state: dict, report: dict) -> set[str]:
    """当前可接受的绑定摘要。

    v6.7.1 报告绑定 subject、代码范围、report_id 与 workspace；无
    `ingest_version` 的 v6.7.0 历史报告没有这些字段，同时兼容“旧绑定已含
    subject_digest”的两种过渡口径，避免历史 PWW 因升级被误判为 stale。
    """
    if report.get("ingest_version") == QUALITY_INGEST_VERSION:
        return {_sha256_text(canonical_json(_quality_binding_payload(
            state, report, include_subject=True, include_identity=True)))}
    legacy = {_sha256_text(canonical_json(_quality_binding_payload(
        state, report, include_subject=False)))}
    if str(report.get("subject_digest") or ""):
        legacy.add(_sha256_text(canonical_json(_quality_binding_payload(
            state, report, include_subject=True, include_code_scope=False))))
        legacy.add(_sha256_text(canonical_json(_quality_binding_payload(
            state, report, include_subject=True))))
    return legacy


def _quality_binding(state: dict, report: dict) -> str:
    """接受记录与任务当前目标/合同/条件/报告/警告/代码范围的绑定摘要。"""
    candidates = _quality_binding_candidates(state, report)
    versioned = report.get("ingest_version") == QUALITY_INGEST_VERSION
    current = _sha256_text(canonical_json(_quality_binding_payload(
        state, report, include_subject=versioned,
        include_identity=versioned)))
    # 优先返回当前规范摘要；旧报告若只匹配过渡摘要，返回该过渡值。
    return current if current in candidates else sorted(candidates)[0]


def _quality_binding_current(state: dict, report: dict) -> bool:
    if not _quality_report_structurally_valid(report):
        return False
    # 重开后旧报告必须重新验收：即使目标/合同文本未变，旧结论也不能直接复用。
    if report.get("reopened_at"):
        return False
    stored = str(report.get("binding_sha256") or "")
    return bool(stored) and stored in _quality_binding_candidates(state, report)


def active_quality_reports(state: dict) -> list[dict]:
    return [report for report in state.get("quality_reports") or []
            if isinstance(report, dict) and not report.get("superseded_by")]


def latest_quality_report(state: dict) -> dict | None:
    reports = active_quality_reports(state)
    return reports[-1] if reports else None


def quality_summary(state: dict, *, cache: dict | None = None) -> dict:
    report = latest_quality_report(state)
    if report is None:
        closure = state.get("closure") or {}
        return {
            "verdict": "unrecorded",
            "report_id": "",
            "acceptance_status": "not_required",
            "accepted_ids": [],
            "binding_current": True,
            "closed_verdict": closure.get("verdict"),
            "note": "本任务没有接入 verify 结构化质量报告；不据此推断任何接受状态。",
        }
    if not _quality_report_structurally_valid(report):
        return {
            "verdict": report.get("verdict"),
            "raw_verdict": report.get("raw_verdict"),
            "report_id": report.get("report_id"),
            "acceptance_status": "invalid",
            "accepted_ids": [],
            "accepted_by": "",
            "accepted_at": "",
            "warnings": [],
            "residual_risks": [],
            "unresolved_ids": [],
            "supersedes_report_id": report.get("supersedes_report_id") or "",
            "code_scope_sha256": report.get("code_scope_sha256") or "",
            "code_scope_declared": False,
            "workspace": report.get("workspace") or state.get("workspace") or "",
            "workspace_source": state.get("workspace_source") or "",
            "code_scope_status": "invalid",
            "binding_current": False,
            "note": "质量报告结构被改写，按 fail-closed 拒绝收口。",
        }
    try:
        digest_ok = _sha256_text(canonical_json(
            _quality_report_payload(report))) == str(
            report.get("report_sha256") or "")
    except (TypeError, ValueError, KeyError, AttributeError):
        digest_ok = False
    binding_current = digest_ok and _quality_binding_current(state, report)
    scope = _code_scope_status(report, state, cache)
    acceptance = report.get("acceptance")
    if not isinstance(acceptance, dict):
        acceptance = {}
    status = str(report.get("acceptance_status") or "")
    if report.get("verdict") == PWW and not binding_current:
        status = "stale"
    return {
        "verdict": report.get("verdict"),
        "raw_verdict": report.get("raw_verdict"),
        "report_id": report.get("report_id"),
        "acceptance_status": status,
        "accepted_ids": list(acceptance.get("accepted_ids") or []),
        "accepted_by": acceptance.get("accepted_by") or "",
        "accepted_at": acceptance.get("accepted_at") or "",
        "warnings": report.get("warnings") or [],
        "residual_risks": report.get("residual_risks") or [],
        "unresolved_ids": [entry["id"] for entry in _report_unresolved(report)],
        "supersedes_report_id": report.get("supersedes_report_id") or "",
        "code_scope_sha256": report.get("code_scope_sha256") or "",
        "code_scope_declared": _quality_code_declared(report.get("subject") or {}),
        "code_scope_status": scope["status"],
        "code_scope_capture_source": (report.get("code_scope") or {}).get(
            "capture_source") or "",
        "code_scope_captured_at": (report.get("code_scope") or {}).get(
            "captured_at") or "",
        "workspace": report.get("workspace") or state.get("workspace") or "",
        "workspace_source": state.get("workspace_source") or "",
        "binding_current": binding_current,
    }


def ingest_quality_report(path: str, report_file: str, *,
                          note: str = "", summary: str = "",
                          workspace: str = "") -> dict:
    """接入 verify 报告：PWW 自动登记用户接受条件；重复接入幂等。"""
    state = load_state(path)
    _assert_open(state, "接入质量报告", allow_blocked=True)
    block = parse_quality_report_file(report_file)
    if str(workspace or "").strip() and \
            not _quality_code_declared(block.get("subject") or {}):
        raise GovernanceError(
            "[verify_code_scope_missing] 已提供 --workspace，但 subject 未声明 "
            "commit/files/diff_sha256/code_scope_sha256；代码任务必须绑定代码快照。"
            "纯文档报告请省略 --workspace。")
    state_reports = state.setdefault("quality_reports", [])
    reports = [item for item in state_reports if isinstance(item, dict)]
    existing = next((report for report in reports
                     if report.get("report_sha256") == block["report_sha256"]), None)
    if existing is not None:
        if existing.get("superseded_by"):
            raise GovernanceError(
                "[verify_report_superseded] 该报告已被 "
                f"{existing.get('superseded_by')} 替代，不能重新接入旧结论；"
                "请对当前目标重新验收并使用新的 report_id。")
        if (summary or "").strip():
            # 同一保存轮次里补齐执行说明；仍不改动既有质量报告与接受记录。
            state.setdefault("reports", []).append({
                "kind": "note",
                "summary": summary.strip()[:MAX_REPORT_CHARS],
                "report_sha256": _sha256_text(summary.strip()),
                "blocked": False, "reason": "",
                "iteration_id": _current_iteration_id(state),
                "created": now_iso(),
            })
            _history(state, "report_recorded",
                     {"sha256": _sha256_text(summary.strip()), "blocked": False})
            save_state(path, state)
        return {"idempotent": True, "report": existing,
                "criterion": _acceptance_criterion(state), "state": state}
    conflict = next((report for report in reports
                     if report.get("report_id") == block["report_id"]), None)
    if conflict is not None:
        raise GovernanceError(
            f"[verify_report_id_reused] report_id={block['report_id']} 已用于另一份"
            "质量结论；新结论必须使用新的 report_id，不能复用旧 ID 覆盖历史。")
    active = active_quality_reports(state)
    if len(active) > 1:
        raise GovernanceError(
            "[quality_report_state_inconsistent] 同时存在多份未替代的质量报告；"
            "先修复状态再接入新报告。")
    target = active[-1] if active else None
    supersedes = str(block.get("supersedes_report_id") or "")
    if target is None:
        if supersedes:
            raise GovernanceError(
                f"[pww_supersede_invalid_target] 当前没有生效报告可被 {supersedes} 替代。")
        plan = _supersede_plan({"verdict": "pass", "warnings": [],
                                "residual_risks": [], "acceptance": {}}, block)
    else:
        if not supersedes:
            raise GovernanceError(
                "[pww_supersede_missing] 已存在生效报告 "
                f"{target.get('report_id')}；新报告必须用 supersedes_report_id "
                "明确替代目标。")
        if supersedes != str(target.get("report_id") or ""):
            raise GovernanceError(
                f"[pww_supersede_invalid_target] supersedes_report_id={supersedes} "
                f"不是当前生效报告 {target.get('report_id')}。")
        plan = _supersede_plan(target, block)
    if block["verdict"] == PWW:
        unmet = [str(c.get("criterion_id") or "")
                 for c in (state.get("contract") or {}).get("criteria") or []
                 if str(c.get("status") or "pending") != "met"
                 and not isinstance(c.get("acceptance"), dict)]
        if unmet:
            raise GovernanceError(
                "[verify_pww_criteria_unmet] Copilot 合同完成条件尚未核销："
                + "、".join(unmet)
                + "；必需完成条件未核销时不得使用 PASS_WITH_WARNINGS。")
    code_scope = _capture_quality_code_scope(
        state, path, report_file, block, workspace)
    at = now_iso()
    report = {
        "report_id": block["report_id"],
        "schema": QUALITY_SCHEMA,
        "ingest_version": QUALITY_INGEST_VERSION,
        "raw_verdict": block["raw_verdict"],
        "verdict": block["verdict"],
        "warnings": block["warnings"],
        "residual_risks": block["residual_risks"],
        "unverified": block["unverified"],
        "acceptance_required": block["acceptance_required"],
        "acceptance_status": block["acceptance_status"],
        "checks": block["checks"],
        "subject": block["subject"],
        "subject_digest": block["subject_digest"],
        "generated_at": block["generated_at"],
        "report_sha256": block["report_sha256"],
        "file_sha256": block["file_sha256"],
        "source_path": block["source_path"],
        "note": (note or "").strip()[:2000],
        "task_id": state.get("task_id"),
        "goal_sha256": state.get("goal_sha256"),
        "contract_sha256": state.get("contract_sha256"),
        "criteria_sha256": (state.get("contract") or {}).get("criteria_sha256"),
        "iteration_id": _current_iteration_id(state),
        "ingested_at": at,
        "superseded_by": None,
        "superseded_at": None,
        "supersedes_report_id": supersedes,
        "resolved_warning_ids": plan["resolved_warning_ids"],
        "resolved_residual_risk_ids": plan["resolved_residual_risk_ids"],
        "unresolved": plan["new_unresolved"],
        "supersede": ({
            "target_report_id": target.get("report_id"),
            "target_subject_digest": target.get("subject_digest"),
            "subject_match": str(target.get("subject_digest") or "") ==
                             str(block.get("subject_digest") or ""),
            "carried_ids": plan["carried"],
            "resolved_ids": plan["resolved"],
            "at": at,
        } if target is not None else None),
        "workspace": (code_scope or {}).get("workspace") or "",
        "workspace_source": str(state.get("workspace_source") or ""),
        "code_scope": code_scope,
        "code_scope_sha256": (code_scope or {}).get("digest") or "",
        "criterion_id": "",
        "binding_sha256": "",
        "acceptance": {
            "status": block["acceptance_status"],
            "accepted_ids": [],
            "accepted_by": "",
            "accepted_at": "",
            "quote": "",
            "binding_sha256": "",
            "code_scope_sha256": (code_scope or {}).get("digest") or "",
        },
        "acceptance_evidence_id": "",
    }
    if target is not None:
        target["superseded_by"] = report["report_id"]
        target["superseded_at"] = at
        if target.get("acceptance_status") in ("pending", "partial", "accepted",
                                               "stale"):
            target["acceptance_status"] = "superseded"
        if isinstance(target.get("acceptance"), dict):
            target["acceptance"]["status"] = "superseded"
    state_reports.append(report)
    unresolved = plan["new_unresolved"]
    unresolved_ids = [entry["id"] for entry in unresolved]
    if block["verdict"] == PWW and set(unresolved_ids) != \
            set(_quality_required_ids(report)):
        raise GovernanceError(
            "[pww_supersede_inconsistent] PWW 的未解决集合与自身 W/R 不一致；"
            "请检查替代覆盖声明。")
    # 只有 PWW 才产生用户接受条件；FAIL/PARTIAL/BLOCKED 带警告时仍记录
    # unresolved 以维持替代链，但不能生成无法执行的“接受”工作。
    if block["verdict"] == PWW and unresolved:
        criterion = _ensure_acceptance_criterion(state, report, unresolved_ids)
        report["criterion_id"] = criterion["criterion_id"]
    else:
        _retire_acceptance_criterion(state)
        criterion = None
    report["goal_sha256"] = state.get("goal_sha256")
    report["contract_sha256"] = state.get("contract_sha256")
    report["criteria_sha256"] = (state.get("contract") or {}).get("criteria_sha256")
    report["binding_sha256"] = _quality_binding(state, report)
    if criterion is not None:
        criterion["acceptance"]["binding_sha256"] = report["binding_sha256"]
    state.setdefault("reports", []).append({
        "kind": "quality",
        "summary": (f"质量报告 {report['report_id']}：{block['raw_verdict']}"
                    + (f"；待接受 {'、'.join(unresolved_ids)}"
                       if unresolved_ids else "")),
        "report_sha256": report["report_sha256"],
        "blocked": False,
        "reason": "",
        "iteration_id": report["iteration_id"],
        "created": at,
    })
    if (summary or "").strip():
        state.setdefault("reports", []).append({
            "kind": "execution",
            "summary": summary.strip()[:MAX_REPORT_CHARS],
            "report_sha256": _sha256_text(summary.strip()),
            "blocked": False, "reason": "",
            "iteration_id": report["iteration_id"],
            "created": at,
        })
    _history(state, "quality_report_ingested", {
        "report_id": report["report_id"],
        "verdict": report["verdict"],
        "acceptance_required": block["acceptance_required"],
        "required_ids": unresolved_ids,
        "subject_digest": report["subject_digest"],
        "supersedes_report_id": supersedes,
        "carried_ids": plan["carried"],
        "resolved_ids": plan["resolved"],
        "code_scope_sha256": report["code_scope_sha256"],
        "binding_sha256": report["binding_sha256"],
    })
    save_state(path, state)
    return {"idempotent": False, "report": report, "criterion": criterion,
            "state": state}


def _id_order(ids: list[str]) -> list[str]:
    def key(value: str):
        prefix = 0 if str(value).startswith("W") else 1
        return (prefix, int(re.sub(r"\D", "", str(value)) or 0))
    return sorted(set(ids), key=key)


def _acceptance_evidence(state: dict, report: dict, accepted: list[str],
                         accepted_at: str, quote: str) -> tuple[str, dict]:
    payload = {
        "schema": QUALITY_SCHEMA,
        "report_id": report.get("report_id"),
        "report_sha256": report.get("report_sha256"),
        "verdict": report.get("verdict"),
        "warnings": report.get("warnings") or [],
        "residual_risks": report.get("residual_risks") or [],
        "accepted_ids": _id_order(accepted),
        "accepted_by": "user",
        "accepted_at": accepted_at,
        "quote": quote[:2000],
        "binding_sha256": report.get("binding_sha256"),
        "code_scope_sha256": report.get("code_scope_sha256") or "",
        "supersedes_report_id": report.get("supersedes_report_id") or "",
        "unresolved_ids": [entry["id"] for entry in _report_unresolved(report)],
    }
    text = canonical_json(payload)
    eid = next_evidence_id(state)
    entry = register_evidence(state, eid, kind="verify-report", text=text,
                              note=f"PWW 用户接受：{report.get('report_id')}")
    return eid, entry


def accept_pww(path: str, *, report_id: str = "", ids: list[str] | None = None,
               accepted_by: str = "", quote: str = "", note: str = "") -> dict:
    """登记用户对 PWW 的明确接受；执行方/子代理/verify 不得代为接受。"""
    state = load_state(path)
    _assert_open(state, "登记用户接受", allow_blocked=True)
    if str(accepted_by or "").strip().lower() != "user":
        raise GovernanceError(
            "[acceptance_requires_user] 只有用户明确接受才能登记；"
            "--by 必须是 user，执行方/子代理/verify/Copilot 不得代替用户接受。")
    quote = (quote or "").strip()
    if not quote:
        raise GovernanceError(
            "[acceptance_quote_required] 必须记录用户接受的原话（--quote），"
            "不能只写调用方结论。")
    active = active_quality_reports(state)
    target_id = str(report_id or "").strip()
    if target_id:
        report = next((item for item in active
                       if str(item.get("report_id") or "") == target_id), None)
        if report is None:
            raise GovernanceError(
                f"[pww_report_not_found] 没有生效中的质量报告 {target_id}；"
                "旧报告被新报告替代后不能继续接受。")
    else:
        if len(active) != 1:
            raise GovernanceError(
                "[pww_report_required] 存在多份生效质量报告时必须用 --report 指定。")
        report = active[0]
    if report.get("verdict") != PWW:
        raise GovernanceError(
            f"[pww_not_applicable] 报告 {report.get('report_id')} 的 verdict="
            f"{report.get('verdict')}，不接受 PWW 用户接受登记。")
    if not _quality_report_structurally_valid(report):
        raise GovernanceError(
            f"[quality_report_state_invalid] 报告 {report.get('report_id')} 结构被"
            "改写；质量结论不可信，必须重新验收。")
    if _sha256_text(canonical_json(_quality_report_payload(report))) != \
            str(report.get("report_sha256") or ""):
        raise GovernanceError(
            f"[quality_report_tampered] 报告 {report.get('report_id')} 内容摘要与登记"
            "不一致；质量结论不可信，必须重新验收后再接受。")
    current = _quality_binding(state, report)
    stored_binding = str(report.get("binding_sha256") or "")
    if stored_binding in _quality_binding_candidates(state, report):
        # legacy 过渡口径：保留已登记的绑定值，避免幂等/部分接受合并失效。
        current = stored_binding
    if str(report.get("acceptance_status") or "") == "stale":
        raise GovernanceError(
            "[pww_reverify_required] 任务重开后旧接受已失效；必须重新验收并接入新的"
            "质量报告，再取得用户明确接受。")
    if not _quality_binding_current(state, report):
        # 不落盘：校验失败时状态必须保持不变；stale 由查询/门禁按当前绑定派生。
        raise GovernanceError(
            "[pww_acceptance_stale] 目标、合同、条件或报告已变化，原接受绑定失效；"
            "请重新验收并接入新的质量报告后再接受。")
    scope = _code_scope_status(report, state)
    if scope["reasons"]:
        raise GovernanceError(
            f"[{scope['reasons'][0]}] 报告绑定的代码范围不可用或已变化；"
            "必须重新验收并生成绑定当前代码的新报告后再接受。")
    criterion = _acceptance_criterion(state)
    if criterion is None:
        raise GovernanceError(
            "[pww_acceptance_condition_missing] 任务没有 PWW 接受条件；"
            "请先用 report --verify-report 重新接入当前质量报告。")
    required = _quality_required_ids(report)
    given = _id_order([str(value).strip() for value in (ids or [])
                       if str(value).strip()])
    if not given:
        raise GovernanceError(
            "[acceptance_ids_required] 必须用 --accept 列出用户接受的警告/残余风险 id。")
    unknown = [value for value in given if value not in required]
    if unknown:
        raise GovernanceError(
            f"[acceptance_ids_unknown] 报告 {report.get('report_id')} 没有这些 id："
            + "、".join(unknown))
    previous = report.get("acceptance")
    if not isinstance(previous, dict):
        previous = {}
    previous_status = str(previous.get("status") or "")
    if previous_status in ("partial", "accepted") and \
            str(previous.get("binding_sha256") or "") == current and \
            set(given) <= set(previous.get("accepted_ids") or []):
        # 幂等：同一报告、同一绑定、范围未扩大时不重复追加证据与接受事件。
        accepted_previous = set(previous.get("accepted_ids") or [])
        return {"report": report, "criterion": criterion,
                "status": previous_status, "required_ids": required,
                "pending_ids": [value for value in required
                                if value not in accepted_previous],
                "idempotent": True}
    accepted = set(given)
    if previous.get("status") in ("partial", "accepted") and \
            str(previous.get("binding_sha256") or "") == current:
        accepted |= set(previous.get("accepted_ids") or [])
    status = "accepted" if accepted == set(required) else "partial"
    at = now_iso()
    record = {
        "status": status,
        "accepted_ids": _id_order(list(accepted)),
        "accepted_by": "user",
        "origin": "user-declared",
        "accepted_at": at,
        "quote": quote[:2000],
        "report_id": report.get("report_id"),
        "binding_sha256": current,
        "code_scope_sha256": report.get("code_scope_sha256") or "",
        "note": (note or "").strip()[:2000],
    }
    report["acceptance"] = record
    history = report.setdefault("acceptance_history", [])
    history.append(json.loads(json.dumps(record)))
    report["acceptance_status"] = status
    criterion["acceptance"].update({
        "status": status,
        "accepted_ids": record["accepted_ids"],
        "accepted_by": "user",
        "accepted_at": at,
        "quote": quote[:2000],
        "binding_sha256": current,
        "code_scope_sha256": report.get("code_scope_sha256") or "",
    })
    report["criterion_id"] = criterion.get("criterion_id") or ""
    if status == "accepted":
        eid, entry = _acceptance_evidence(state, report, record["accepted_ids"],
                                          at, quote)
        report["acceptance_evidence_id"] = eid
        criterion["status"] = "met"
        criterion["evidence_refs"] = [{"id": eid, "sha256": entry["sha256"]}]
        criterion["updated_at"] = at
        criterion["updated_iteration"] = _current_iteration_id(state)
        criterion.setdefault("history", []).append({
            "status": "met", "at": at,
            "evidence_refs": criterion["evidence_refs"], "source": "accept",
        })
    else:
        criterion["status"] = "pending"
        criterion["evidence_refs"] = []
        criterion["updated_at"] = at
    state.setdefault("decisions", []).append({
        "text": (f"用户接受 {report.get('report_id')} 的 "
                 + "、".join(record["accepted_ids"]) + f"：{quote}")[:MAX_REPORT_CHARS],
        "source": "user",
        "kind": "pww-acceptance",
        "at": at,
    })
    _history(state, "pww_acceptance_recorded", {
        "report_id": report.get("report_id"),
        "status": status,
        "accepted_ids": record["accepted_ids"],
        "pending_ids": [value for value in required
                        if value not in set(record["accepted_ids"])],
        "criterion_id": criterion.get("criterion_id"),
    })
    save_state(path, state)
    return {"report": report, "criterion": criterion, "status": status,
            "required_ids": required,
            "pending_ids": [value for value in required
                            if value not in set(record["accepted_ids"])]}


def _gate_quality(state: dict, *, cache: dict | None = None) -> dict:
    """G2 的 PWW 接受检查：与条件列表独立，合同修订删条件也不能绕过。"""
    reports = [item for item in state.get("quality_reports") or []
               if isinstance(item, dict)]
    active = active_quality_reports(state)
    report = active[-1] if active else None
    if report is None:
        ingested_events = [event for event in state.get("history") or []
                           if isinstance(event, dict)
                           and event.get("event") == "quality_report_ingested"]
        if reports or ingested_events:
            return {"passed": False,
                    "quality": quality_summary(state, cache=cache),
                    "reasons": [{
                "code": "quality_report_state_inconsistent",
                "detail": "历史记录了质量报告接入，但当前没有生效报告；"
                          "状态被删除或写入不完整，按 fail-closed 拒绝收口。"}]}
        return {"passed": True, "reasons": [], "quality": quality_summary(state, cache=cache)}
    reasons: list[dict] = []
    if len(active) > 1:
        reasons.append({
            "code": "quality_report_state_inconsistent",
            "detail": "同时存在多份未替代的质量报告；状态被手工改写或写入不完整，"
                      "按 fail-closed 拒绝收口。"})
    try:
        recomputed = _sha256_text(canonical_json(_quality_report_payload(report)))
    except (TypeError, ValueError, KeyError, AttributeError):
        recomputed = ""
        reasons.append({
            "code": "quality_report_state_invalid",
            "detail": f"报告 {report.get('report_id')} 结构不完整，无法重算摘要；"
                      "按 fail-closed 拒绝收口。"})
    if not _quality_report_structurally_valid(report):
        return {"passed": False, "reasons": reasons + [{
            "code": "quality_report_state_invalid",
            "detail": f"报告 {report.get('report_id')} 的 warnings/residual_risks/"
                      "unverified/unresolved 结构被改写；按 fail-closed 拒绝收口。"}],
            "quality": quality_summary(state, cache=cache)}
    if recomputed and recomputed != str(report.get("report_sha256") or ""):
        reasons.append({
            "code": "quality_report_tampered",
            "detail": f"报告 {report.get('report_id')} 内容摘要与登记不一致；"
                      "质量结论不可信，必须重新验收。"})
    for code in _validate_supersede_chain(state, report):
        reasons.append({
            "code": code,
            "detail": (f"报告 {report.get('report_id')} 的替代关系不合法或覆盖不完整；"
                       "不能据此解除旧 PWW 的接受要求。")})
    scope = _code_scope_status(report, state, cache)
    for code in scope["reasons"]:
        reasons.append({
            "code": code,
            "detail": (f"报告 {report.get('report_id')} 的代码范围不可读、与登记不一致"
                       "或当前工作区已漂移；必须重新验收。")})
    unresolved_ids = {entry["id"] for entry in _report_unresolved(report)}
    verdict = str(report.get("verdict") or "")
    if verdict == "pass":
        if not _quality_binding_current(state, report):
            reasons.append({
                "code": "quality_binding_stale",
                "detail": (f"PASS 报告 {report.get('report_id')} 的目标/合同/条件/"
                           "工作区绑定已变化；该结论不覆盖当前任务，必须重新验收。")})
        checks = report.get("checks")
        if not isinstance(checks, dict) or any(
                checks.get(key) is not True for key in _QUALITY_CHECKS):
            reasons.append({
                "code": "quality_precondition_failed",
                "detail": "PASS 报告的四项 checks 不全是 true；不得按通过收口。"})
        if unresolved_ids:
            reasons.append({
                "code": "pww_superseded_unresolved",
                "detail": ("PASS 报告仍继承未解决警告/残余风险："
                           + "、".join(sorted(unresolved_ids))
                           + "；不能按通过收口。")})
        return {"passed": not reasons, "reasons": reasons,
                "quality": quality_summary(state, cache=cache)}
    if verdict != PWW:
        reasons.append({
            "code": "quality_verdict_not_pass",
            "detail": (f"最新质量报告 {report.get('report_id')} 的原始结论是 "
                       f"{str(report.get('raw_verdict') or verdict).upper()}；"
                       "先修复并重新验收，不能按收口处理。")})
        return {"passed": not reasons, "reasons": reasons,
                "quality": quality_summary(state, cache=cache)}
    current = _quality_binding(state, report)
    if not _quality_binding_current(state, report):
        reasons.append({
            "code": "pww_acceptance_stale",
            "detail": (f"报告 {report.get('report_id')} 的目标/合同/条件/代码范围已变化，"
                       "原接受不继承；请重新验收并接入新报告。")})
    acceptance = report.get("acceptance")
    if not isinstance(acceptance, dict):
        acceptance = {}
    required = set(_quality_required_ids(report))
    accepted = set(acceptance.get("accepted_ids") or [])
    status = str(report.get("acceptance_status") or "")
    if unresolved_ids != (required - accepted):
        reasons.append({
            "code": "pww_supersede_inconsistent",
            "detail": "报告的未解决集合与“全部 W/R - 已接受”不一致；"
                      "替代关系或接受记录被改写。"})
    if status == "superseded":
        reasons.append({"code": "pww_report_superseded",
                        "detail": "生效报告已被替代；不能使用旧接受记录。"})
    elif status == "stale":
        reasons.append({"code": "pww_reverify_required",
                        "detail": ("任务重开或目标变化后旧接受已失效；"
                                   "必须重新验收并取得用户再次接受。")})
    elif not required:
        reasons.append({"code": "pww_warning_set_missing",
                        "detail": "PWW 报告没有可接受的警告/残余风险 id。"})
    elif accepted != required or status != "accepted":
        missing = [value for value in _quality_required_ids(report)
                   if value not in accepted]
        if missing and accepted:
            reasons.append({
                "code": "pww_acceptance_partial",
                "detail": ("用户只接受了部分警告，仍未接受：" + "、".join(missing)
                           + "；任务保持未完成。")})
        else:
            reasons.append({
                "code": "pww_acceptance_pending",
                "detail": ("PWW 需要用户明确接受全部警告与残余风险："
                           + "、".join(_quality_required_ids(report))
                           + "；未接受前 G2 拒绝收口。")})
    if (accepted or status in ("partial", "accepted")) and (
            str(acceptance.get("accepted_by") or "") != "user"
            or not str(acceptance.get("accepted_at") or "")):
        reasons.append({
            "code": "pww_acceptance_not_user",
            "detail": "接受记录缺少用户明确表达（accepted_by=user 与接受时间）。"})
    # 接受记录必须与条件、证据和接受历史一致：防止只改一处字段伪造“已接受”。
    if status in ("partial", "accepted"):
        history = report.get("acceptance_history")
        legacy_missing_history = (
            report.get("ingest_version") != QUALITY_INGEST_VERSION
            and not (isinstance(history, list) and history))
        if not legacy_missing_history:
            last = history[-1] if isinstance(history, list) and history else None
            if not isinstance(last, dict) or \
                    str(last.get("status") or "") != status or \
                    set(last.get("accepted_ids") or []) != accepted:
                reasons.append({
                    "code": "pww_acceptance_record_inconsistent",
                    "detail": "接受状态与 acceptance_history 不一致；接受记录不可信。"})
    if status == "accepted":
        evidence_id = str(report.get("acceptance_evidence_id") or "")
        entry = _evidence_entry(state, evidence_id)
        criterion = _acceptance_criterion(state)
        refs = {str(ref.get("id") or "") for ref in
                (criterion or {}).get("evidence_refs") or []}
        meta = (criterion or {}).get("acceptance")
        if entry is None or entry.get("dropped") or not isinstance(meta, dict) \
                or str(meta.get("report_id") or "") != str(report.get("report_id") or "") \
                or str(criterion.get("status") or "") != "met" \
                or evidence_id not in refs:
            reasons.append({
                "code": "pww_acceptance_record_inconsistent",
                "detail": "完整接受缺少绑定的接受证据/条件核销；接受记录不可信。"})
    return {"passed": not reasons, "reasons": reasons,
            "quality": quality_summary(state, cache=cache)}


# ── 证据 ───────────────────────────────────────────────────────────────────
def _evidence_entry(state: dict, eid: str) -> dict | None:
    for entry in state.get("evidence") or []:
        if str(entry.get("id") or "") == (eid or "").strip():
            return entry
    return None


def register_evidence(state: dict, eid: str, *, kind: str = "",
                      text: str | None = None, file_path: str | None = None,
                      note: str = "") -> dict:
    # 证据是既成事实；登记不解除阻塞，也不跳过 G2 的文件重读校验。
    _assert_open(state, "登记证据", allow_blocked=True)
    eid = (eid or "").strip()
    if not EVIDENCE_ID_RE.match(eid):
        raise GovernanceError(f"证据 id {eid!r} 非法（应形如 E1）。")
    if bool(file_path) == (text is not None):
        raise GovernanceError("证据登记必须且只能提供 --file 或 --text 之一。")
    if file_path:
        path = os.path.abspath(file_path)
        if not os.path.isfile(path):
            raise GovernanceError(f"证据文件不存在或不是普通文件：{path}")
        digest = _sha256_file(path)
        size = os.path.getsize(path)
        hint = path
    else:
        raw = text or ""
        digest = _sha256_text(raw)
        size = len(raw.encode("utf-8"))
        hint = "<inline>"
    for entry in state.get("evidence") or []:
        if str(entry.get("id") or "") != eid:
            continue
        if entry.get("dropped"):
            raise GovernanceError(f"证据 {eid} 已归档；请使用新的 id。")
        if str(entry.get("sha256") or "") != digest:
            raise GovernanceError(
                f"证据 {eid} 已登记且摘要不同：历史证据不得被静默替换；请使用新 id。")
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
    state.setdefault("evidence", []).append(entry)
    _history(state, "evidence_registered", {"id": eid, "sha256": digest,
                                            "kind": entry["kind"]})
    return entry


def drop_evidence(state: dict, eid: str, reason: str) -> dict:
    _assert_open(state, "归档证据")
    reason = (reason or "").strip()
    if not reason:
        raise GovernanceError("归档证据必须说明原因。")
    entry = _evidence_entry(state, eid)
    if entry is None:
        raise GovernanceError(f"不存在证据 {eid}。")
    if entry.get("dropped"):
        return entry
    entry["dropped"] = True
    entry["dropped_reason"] = reason[:500]
    _history(state, "evidence_dropped", {"id": eid, "reason": reason[:200]})
    return entry


def next_evidence_id(state: dict) -> str:
    used = {str(e.get("id") or "") for e in state.get("evidence") or []}
    for number in range(1, 10000):
        candidate = f"E{number}"
        if candidate not in used:
            return candidate
    raise GovernanceError("证据 id 已用尽（E1–E9999）。")


# ── 迭代 / 无进展 ──────────────────────────────────────────────────────────
def _current_iteration_id(state: dict) -> str:
    seq = int(state.get("iteration_seq") or 0)
    return f"I{seq}" if seq else "I0"


def begin_iteration(state: dict, *, note: str = "") -> dict:
    # 上一轮迭代在开新一轮时结束：审计里不应同时存在多条 phase=RUNNING 的迭代。
    for previous in state.get("iterations") or []:
        if isinstance(previous, dict) and not previous.get("ended"):
            previous["ended"] = now_iso()
            previous["phase"] = "ENDED"
    seq = int(state.get("iteration_seq") or 0) + 1
    state["iteration_seq"] = seq
    entry = {
        "iteration_id": f"I{seq}",
        "seq": seq,
        "started": now_iso(),
        "ended": None,
        "phase": "RUNNING",
        "note": (note or "").strip()[:1000],
        "actions": [a["id"] for a in state.get("actions") or []
                    if str(a.get("status") or "") in ("pending", "running")],
        "evidence_ids": [e["id"] for e in state.get("evidence") or []
                         if not e.get("dropped")],
    }
    state.setdefault("iterations", []).append(entry)
    _history(state, "iteration_started", {"iteration_id": entry["iteration_id"]})
    return entry


def progress_digest(state: dict) -> str:
    return _sha256_text(canonical_json({
        "actions": sorted([[a.get("id"), a.get("status"),
                            _sha256_text(str(a.get("result") or "")),
                            sorted(a.get("evidence_ids") or [])]
                           for a in state.get("actions") or []]),
        "criteria": sorted([[c.get("criterion_id"), c.get("status"),
                             sorted(r.get("id") for r in c.get("evidence_refs") or [])]
                            for c in (state.get("contract") or {}).get("criteria") or []]),
        "evidence": sorted([[e.get("id"), e.get("sha256")]
                            for e in state.get("evidence") or []]),
        "closure": [bool((state.get("closure") or {}).get("closed")),
                    ((state.get("closure") or {}).get("gate") or {}).get("passed")],
    }))


# ── G1–G3 / 收口 ────────────────────────────────────────────────────────────
def _gate_actions(state: dict) -> dict:
    reasons = []
    for action in _tracked_actions(state):
        status = str(action.get("status") or "")
        if status not in TERMINAL:
            reasons.append({"code": "action_open",
                            "detail": f"{action.get('id')} 仍为 {status}"})
        elif status == "dropped" and not str(action.get("result") or "").strip():
            reasons.append({"code": "dropped_without_reason",
                            "detail": f"{action.get('id')} 已放弃但未说明原因"})
    return {"passed": not reasons, "reasons": reasons}


def _gate_criteria(state: dict, *, cache: dict | None = None) -> dict:
    contract = state.get("contract") or {}
    criteria = contract.get("criteria") or []
    reasons = []
    expected = _contract_fingerprint(state.get("goal") or "",
                                     contract.get("done_criteria_text") or "",
                                     criteria,
                                     contract.get("test_policy") or {})
    if str(state.get("contract_sha256") or "") != expected:
        reasons.append({"code": "contract_fingerprint_mismatch",
                        "detail": "合同指纹与当前目标/合同正文/完成条件不一致。"})
    if not criteria:
        reasons.append({"code": "criteria_missing",
                        "detail": "完成条件为空，无法逐条核销。"})
    if contract.get("criteria_state") != "confirmed":
        reasons.append({"code": "criteria_unconfirmed",
                        "detail": "完成条件未确认。"})
    elif str(contract.get("criteria_sha256") or "") != criteria_digest(criteria):
        reasons.append({"code": "criteria_drift",
                        "detail": "完成条件集合与确认时的摘要不一致。"})
    evidence = {str(e.get("id") or ""): e for e in state.get("evidence") or []}
    task_id = str(state.get("task_id") or "")
    for criterion in criteria:
        cid = str(criterion.get("criterion_id") or "")
        status = str(criterion.get("status") or "")
        if status != "met":
            reasons.append({
                "code": "criterion_pending" if status == "pending" else "criterion_not_met",
                "detail": f"{cid} 当前状态 {status or 'pending'}"})
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
                if not os.path.isfile(hint):
                    reasons.append({"code": "evidence_file_missing",
                                    "detail": f"{eid} 证据文件已不存在或不是普通文件：{hint}"})
                    continue
                try:
                    # 同一文件被多条条件引用时只重读一次；键里带 mtime/size，
                    # 进程内文件被改写仍会重新计算，不把旧摘要当成本次结果。
                    stat = os.stat(hint)
                    key = ("evidence_sha256", hint, stat.st_mtime_ns,
                           stat.st_size)
                    actual = cache.get(key) if cache is not None else None
                    if actual is None:
                        actual = _sha256_file(hint)
                        if cache is not None:
                            cache[key] = actual
                    if actual != str(entry.get("sha256") or ""):
                        reasons.append({"code": "evidence_tampered",
                                        "detail": f"{eid} 文件内容已被改动"})
                except OSError as exc:
                    reasons.append({"code": "evidence_unreadable",
                                    "detail": f"{eid} 无法重读：{exc}"})
    return {"passed": not reasons, "reasons": reasons}


def _gate_stalled(state: dict) -> dict:
    reasons = [{"code": "stalled", "detail": f"{a.get('id')}：{reason}"}
               for a, reason in stalled_actions(state)]
    return {"passed": not reasons, "reasons": reasons}


def final_evidence_digest(state: dict) -> str:
    """最终证据摘要：把合同、条件、工作项与证据绑定成一个复核目标。"""
    return _sha256_text(canonical_json({
        "task_id": state.get("task_id"),
        "contract_sha256": state.get("contract_sha256"),
        "criteria": sorted([[c.get("criterion_id"), c.get("status"),
                             sorted([[r.get("id"), r.get("sha256")]
                                     for r in c.get("evidence_refs") or []])]
                            for c in (state.get("contract") or {}).get("criteria") or []]),
        "actions": sorted([[a.get("id"), a.get("status"),
                            _sha256_text(str(a.get("result") or "")),
                            sorted(a.get("evidence_ids") or [])]
                           for a in state.get("actions") or []
                           # 历史兼容：v7 及更早的高风险任务可能留有
                           # origin=g4-review 的工作项。排除它们可以保持旧状态
                           # 的摘要稳定；新任务不再产生该类工作项。
                           if str(a.get("origin") or "") != "g4-review"]),
        "evidence": sorted([[e.get("id"), e.get("sha256"), bool(e.get("dropped"))]
                            for e in state.get("evidence") or []]),
    }))


capture_code_snapshot = CS.capture_code_snapshot
governance_paths = CS.governance_paths
_evidence_paths = CS.evidence_paths


def review_admission(state: dict, *, triggers: list[str] | None = None,
                     reason: str = "", target_sha256: str = "",
                     code_scope_sha256: str = "",
                     files: list[dict] | None = None,
                     scope: str = "full", include_gates: bool = True,
                     user_request: bool = False, review_requested: bool = False,
                     workspace: str = "") -> dict:
    """SOL 主动介入准入建议（只读，不写状态）。

    这是“要不要调用 SOL”的**单一确定性入口**：先用本地 G1–G3 与台账事实判断
    确定性门禁是否已通过，再结合任务类型与认知复杂度给出 `CONSULT` / `PLAN`
    的默认认知建议，或用户要求的 `REVIEW` 建议。SOL 是 advisory，不构成收口
    门禁，也不证明宿主只读权限。

    - `decision`：风险等级策略建议（`not_needed` / `optional` / `recommended`），
      只反映风险与触发因素，保持历史输出兼容。
    - `advice`：生命周期判断（`none` / `reuse` / `consult` / `plan` / `review`）＋
      建议阶段、新信号、第二轮触发与命令提示。
    - `recommended_action`：`advice.action` 的兼容视图；非简单任务在启动/规划
      阶段会得到 `recommended`（默认认知判断），简单任务始终 `not_needed`。
    """
    import sol_review as SR

    risk = str(state.get("risk") or "")
    state_triggers = [str(t.get("name") or "")
                      for t in state.get("admission_triggers") or []
                      if isinstance(t, dict) and t.get("name")]
    trigger_values = list(triggers or []) + state_triggers
    decision = SR.admission(risk=risk, triggers=trigger_values, reason=reason)
    calls = [item for item in iter_sol_calls(state)
             if item.get("status") == "completed"]
    reviews = [item for item in calls if sol_call_mode(item) == "REVIEW"]
    by_mode = {mode: len([item for item in calls
                          if sol_ref_mode(item) == mode])
               for mode in SOL_MODES}
    target = str(target_sha256 or final_evidence_digest(state))
    check = closure_check(state) if include_gates else None
    advice = RP.sol_advice(
        state, calls=calls, triggers=trigger_values,
        gates_passed=bool(check and check["passed"]),
        user_request=user_request, review_requested=review_requested,
        workspace=str(workspace or state.get("workspace") or ""))
    result = {
        **decision,
        "task_kind": str(state.get("task_kind") or RP.DEFAULT_TASK_KIND),
        "target_sha256": target,
        "sol_calls_total": len(calls),
        "sol_calls_by_mode": by_mode,
        "sol_reviews_total": len(reviews),
        "last_sol_review": ({
            "call_id": sol_call_id(reviews[-1]),
            "verdict": reviews[-1].get("verdict"),
            "materials_sha256": reviews[-1].get("materials_sha256"),
            "prompt_version": reviews[-1].get("prompt_version"),
            "created_at": reviews[-1].get("created_at"),
        } if reviews else None),
        "last_sol_call": ({
            "call_id": sol_call_id(calls[-1]),
            "mode": sol_call_mode(calls[-1]),
            "materials_sha256": calls[-1].get("materials_sha256"),
            "prompt_version": calls[-1].get("prompt_version"),
            "created_at": calls[-1].get("created_at"),
        } if calls else None),
        "ledger": RP.ledger_summary(state),
        "code_scope_checked": bool(code_scope_sha256 or files),
        "advice": advice,
        # 兼容视图：旧的 not_needed / optional / recommended 三值保持不变，
        # 但判断依据从“只有风险 + 门禁”升级为“复杂度 + 覆盖 + 门禁”。
        "recommended_action": advice["recommended_action"],
        "recommended_mode": advice["mode"],
        "recommended_phase": advice["phase"],
        "recommended_stage": advice.get("stage") or "",
    }
    if not include_gates:
        result["recommended_reason"] = advice["reason"]
        return result
    gate_codes = {
        name: sorted({str(item.get("code") or "") for item in check["gates"][name]["reasons"]})
        for name in ("G1", "G2", "G3")
    }
    open_codes = [code for codes in gate_codes.values() for code in codes if code]
    result["gates"] = {
        "passed": bool(check["passed"]),
        "codes": gate_codes,
    }
    result["recommended_reason"] = advice["reason"]
    if open_codes and advice["action"] in ("none", "reuse"):
        result["deterministic_gaps"] = sorted(set(open_codes))
    return result


# ── SOL 调用记录：单一权威流 `sol_calls` + 历史只读兼容 ──────────────────────
SOL_CALLS_KEY = "sol_calls"
LEGACY_SOL_KEYS = ("sol_reviews", "sol_advisories")
# v9：SOL 默认认知引擎——CONSULT / PLAN 是一等模式，REVIEW 仍只在用户明确
# 要求时执行；THINK 是 v8.4–v8.5 的历史模式名，只读兼容并归入 CONSULT。
SOL_MODES = ("CONSULT", "PLAN", "REVIEW")
LEGACY_SOL_MODES = ("THINK",)
RECORD_SOL_MODES = SOL_MODES + LEGACY_SOL_MODES


def sol_call_id(record: dict) -> str:
    """统一记录 ID：新记录用 `call_id`；历史记录沿用当时的主键。"""
    for key in ("call_id", "review_id", "advisory_id"):
        value = str(record.get(key) or "")
        if value:
            return value
    return ""


def sol_call_mode(record: dict) -> str:
    """记录中实际存储的模式（含历史 THINK）；缺失时按存放位置判定。"""
    mode = str(record.get("mode") or "").upper()
    if mode in RECORD_SOL_MODES:
        return mode
    return "CONSULT" if record.get("advisory_id") else "REVIEW"


def sol_ref_mode(record: dict) -> str:
    """规范模式：历史 THINK 归入 CONSULT，用于覆盖与计数。"""
    mode = sol_call_mode(record)
    if mode == "REVIEW":
        return "REVIEW"
    if mode == "PLAN":
        return "PLAN"
    return "CONSULT"


def iter_sol_calls(state: dict) -> list[dict]:
    """统一 SOL 调用记录流（含失败记录）。

    `sol_calls` 是唯一权威来源。v8.2 之前分别存放在 `sol_reviews` /
    `sol_advisories` 的历史记录继续只读可读：不迁移、不改写、不重复写入，
    合并时按记录 ID 去重并按创建时间排序。
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for key in (SOL_CALLS_KEY,) + LEGACY_SOL_KEYS:
        for item in state.get(key) or []:
            if not isinstance(item, dict):
                continue
            ident = sol_call_id(item)
            if ident:
                if ident in seen:
                    continue
                seen.add(ident)
            rows.append(item)
    rows.sort(key=lambda item: str(item.get("created_at")
                                   or item.get("received") or ""))
    return rows


def list_sol_calls(state: dict, *, mode: str = "") -> list[dict]:
    """只读返回统一 SOL 调用记录；`mode` 为空时返回全部模式。

    `mode=CONSULT` 同时命中历史 THINK 记录；其他模式按存储值精确匹配。
    """
    rows = iter_sol_calls(state)
    if not mode:
        return rows
    wanted = str(mode).upper()
    if wanted == "CONSULT":
        return [item for item in rows if sol_ref_mode(item) == "CONSULT"]
    return [item for item in rows if sol_ref_mode(item) == wanted]


def list_sol_reviews(state: dict) -> list[dict]:
    """只读返回 REVIEW 模式记录（含失败记录与历史兼容记录）。"""
    return list_sol_calls(state, mode="REVIEW")


def list_sol_consult_calls(state: dict) -> list[dict]:
    """只读返回 CONSULT 记录（含历史 THINK 与失败记录）。"""
    return list_sol_calls(state, mode="CONSULT")


def list_sol_plan_calls(state: dict) -> list[dict]:
    """只读返回 PLAN 记录（含失败记录）。"""
    return list_sol_calls(state, mode="PLAN")


def list_sol_think_calls(state: dict) -> list[dict]:
    """历史名称：v9 起等同 `list_sol_consult_calls`，保留给既有调用方。"""
    return list_sol_consult_calls(state)


def record_sol_call(path: str, call: dict, *, mode: str = "",
                    materials_sha256: str = "", trigger: str = "",
                    reason: str = "") -> dict:
    """登记一次已完成的 SOL 调用（CONSULT / PLAN / REVIEW 共用）。

    SOL 是建议层：REVIEW 的 findings 以 `advisory` 进入 Finding 台账，主代理
    逐条核实后修复、定向验证或给出反证；CONSULT / PLAN 只留审计记录，不进入
    台账、不改变完成条件、不影响 G1–G3。历史 THINK 记录仍可写入（兼容旧调用
    方），读取时归入 CONSULT。
    """
    state = load_state(path)
    _assert_open(state, "登记 SOL 调用", allow_blocked=True)
    record = {**call}
    resolved = str(mode or record.get("mode") or "CONSULT").upper()
    if resolved not in RECORD_SOL_MODES:
        raise GovernanceError(f"未知 SOL 模式：{resolved}")
    record["mode"] = resolved
    record["call_id"] = str(record.get("call_id") or sol_call_id(record)
                            or "SC" + secrets.token_hex(12))
    record["status"] = "completed"
    # 用回复摘要作为台账去重键：同一份 SOL 输出重复合并时只计一次 occurrence。
    if not record.get("result_sha256"):
        record["result_sha256"] = str(record.get("reply_sha256") or "")
    if not record.get("received"):
        record["received"] = str(record.get("completed_at") or now_iso())
    record["materials_sha256"] = str(materials_sha256
                                     or record.get("materials_sha256") or "")
    record["trigger"] = str(trigger or "")
    record["reason"] = str(reason or "")[:1000]
    record["question"] = str(record.get("question") or "")[:4000]
    record["stage"] = str(record.get("stage") or "")[:40]
    record["progress_digest"] = str(record.get("progress_digest") or "")
    # 采纳状态：未登记时为空，由 record_sol_acceptance 显式回填。
    if not isinstance(record.get("acceptance"), dict):
        record["acceptance"] = {"status": "pending"}
    record["created_at"] = str(record.get("completed_at") or now_iso())
    detail = {
        "call_id": record["call_id"],
        "mode": resolved,
        "materials_sha256": record["materials_sha256"],
        "prompt_version": str(record.get("prompt_version") or ""),
        "trigger": record["trigger"],
        "stage": record["stage"],
    }
    if resolved == "REVIEW":
        merge = RP.merge_review(state, record, authoritative=False)
        record["finding_refs"] = merge.get("refs") or []
        detail.update({
            "verdict": record.get("verdict") or "",
            "findings": record.get("findings_count") or {},
            "merged": {"created": merge.get("created") or [],
                       "matched": merge.get("matched") or []},
        })
    else:
        detail.update({
            "judgment": str(record.get("judgment") or "")[:200],
            "tradeoffs": len(record.get("tradeoffs") or []),
            "risks": len(record.get("risks") or []),
            "priority": len(record.get("priority") or []),
            "verify": len(record.get("verify") or []),
        })
    state.setdefault(SOL_CALLS_KEY, []).append(record)
    _history(state, "sol_call_completed", detail)
    save_state(path, state)
    return record


def _find_sol_call(state: dict, call_id: str) -> dict:
    target = str(call_id or "").strip()
    if not target:
        raise GovernanceError("必须提供 SOL 记录的 call_id。")
    for item in state.get(SOL_CALLS_KEY) or []:
        if isinstance(item, dict) and sol_call_id(item) == target:
            return item
    raise GovernanceError(
        f"当前任务的 sol_calls 中不存在记录：{target}；历史只读记录不接受采纳回填。")


ACCEPTANCE_STATUSES = ("accepted", "modified", "rejected", "pending")
# 采纳记录结构版本：显式标记区分新旧记录，不按日期或字段是否填满推断。
# v1（无 schema 字段）与 v2 都可读取；质量指标只以 v2 已决记录为分母，
# 并把排除的旧记录单独披露。
ACCEPTANCE_SCHEMA = "copilot-sol-acceptance/v2"
ACCEPTANCE_EVIDENCE_STATES = ("linked", "partial", "unresolved",
                              "archived_only", "source_only", "none",
                              "unrecorded")
ACCEPTANCE_HINTS = {
    "actual_decision_missing":
        "缺少实际决策说明：建议用 --decision 记录 ZCode 最终采取的做法"
        "（不是 SOL 原建议）。",
    "rationale_missing":
        "缺少采纳理由：建议用 --rationale（省略时回落 --reason）说明采纳、"
        "修改或拒绝的依据。",
    "modified_delta_missing":
        "modified 缺少与原建议的差异：建议用 --delta 说明调整了什么。",
    "evidence_missing":
        "暂无证据关联：可如实保留；如需追溯，可引用已登记 E 编号、报告章节"
        "或文件路径。",
    "evidence_unregistered":
        "引用的 E 编号尚未在本任务登记：请核实编号或先登记证据；未登记引用"
        "只作为待核引用，不进入正式证据链。",
    "evidence_archived":
        "引用的 E 编号已归档：请核实是否应改用新的证据编号。",
}


def _normalize_acceptance_evidence_refs(refs) -> list[str]:
    """规范化 E 编号引用：支持重复传参与逗号分隔，去空去重。

    格式错误是调用方输入错误（不是证据门禁）；合法但未登记的编号由
    `resolve_acceptance_evidence` 标为待核引用，不阻断登记。
    """
    out: list[str] = []
    for raw in refs or []:
        if raw is None:
            continue
        for piece in str(raw).split(","):
            eid = piece.strip().upper()
            if not eid:
                continue
            if not EVIDENCE_ID_RE.match(eid):
                raise GovernanceError(f"证据引用 {eid!r} 非法（应形如 E1）。")
            if eid not in out:
                out.append(eid)
    if len(out) > 50:
        raise GovernanceError("证据引用过多（最多 50 条）。")
    return out


def _normalize_acceptance_source_refs(refs) -> list[str]:
    """来源定位（报告章节、文件路径等）：只记录，不做证据核验。"""
    out: list[str] = []
    for raw in refs or []:
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        if len(text) > 300:
            raise GovernanceError("来源引用过长（最多 300 字符）。")
        if text not in out:
            out.append(text)
    if len(out) > 20:
        raise GovernanceError("来源引用过多（最多 20 条）。")
    return out


def resolve_acceptance_evidence(state: dict, evidence_refs: list[str]) -> dict:
    """登记时解析证据引用：已登记 / 未登记 / 已归档。

    只核对本任务证据登记表，不做内容核验、不自动补登记；未登记与已归档的
    编号保留为待核引用，不进入正式证据链。
    """
    linked: list[str] = []
    unresolved: list[str] = []
    archived: list[str] = []
    for eid in evidence_refs:
        entry = _evidence_entry(state, eid)
        if entry is None:
            unresolved.append(eid)
        elif entry.get("dropped"):
            archived.append(eid)
        else:
            linked.append(eid)
    return {"linked": linked, "unresolved": unresolved, "archived": archived}


def acceptance_evidence_status(entry: dict) -> str:
    """由登记时的逐条解析结果派生汇总状态（不持久化为第二事实来源）。"""
    if not isinstance(entry, dict) or not entry.get("evidence_resolution"):
        refs = (entry or {}).get("evidence_refs")
        if refs is None:
            return "unrecorded"
        if not refs:
            return "source_only" if (entry or {}).get("source_refs") else "none"
    res = entry.get("evidence_resolution") or {}
    linked = list(res.get("linked") or [])
    unresolved = list(res.get("unresolved") or [])
    archived = list(res.get("archived") or [])
    if linked and (unresolved or archived):
        return "partial"
    if linked:
        return "linked"
    if unresolved:
        return "unresolved"
    if archived:
        return "archived_only"
    return "source_only" if entry.get("source_refs") else "none"


def acceptance_rationale(entry: dict) -> str:
    """有效理由：新字段优先，历史记录回落到 reason。"""
    return str(entry.get("rationale") or entry.get("reason") or "").strip()


def acceptance_is_complete(entry: dict) -> bool:
    """完整率口径：只看决策记录（决策 + 理由 + modified 差异），不看证据。"""
    if not isinstance(entry, dict):
        return False
    if not str(entry.get("decision") or "").strip():
        return False
    if not acceptance_rationale(entry):
        return False
    if str(entry.get("status") or "") == "modified" \
            and not str(entry.get("delta") or "").strip():
        return False
    return True


def _acceptance_advice_ref(record: dict) -> dict:
    """原建议的可追溯引用：沿 call_id 定位已有回复，不复制正文。"""
    return {
        "call_id": sol_call_id(record),
        "mode": sol_call_mode(record),
        "stage": str(record.get("stage") or ""),
        "schema": str(record.get("schema") or ""),
        "judgment": str(record.get("judgment")
                        or record.get("summary") or "")[:400],
        "reply_path": str(record.get("reply_path") or ""),
        "reply_sha256": str(record.get("reply_sha256") or ""),
        "materials_sha256": str(record.get("materials_sha256") or ""),
    }


def _acceptance_warnings(entry: dict, resolution: dict,
                         source_refs: list[str]) -> list[str]:
    codes: list[str] = []
    if not str(entry.get("decision") or "").strip():
        codes.append("actual_decision_missing")
    if not acceptance_rationale(entry):
        codes.append("rationale_missing")
    if entry.get("status") == "modified" \
            and not str(entry.get("delta") or "").strip():
        codes.append("modified_delta_missing")
    if not entry.get("evidence_refs") and not source_refs:
        codes.append("evidence_missing")
    if resolution.get("unresolved"):
        codes.append("evidence_unregistered")
    if resolution.get("archived"):
        codes.append("evidence_archived")
    return codes


def record_sol_acceptance(path: str, call_id: str, *, status: str,
                          note: str = "", reason: str = "",
                          decision: str = "", rationale: str = "",
                          delta: str = "", evidence_refs: list | None = None,
                          source_refs: list | None = None,
                          evidence_note: str = "",
                          decided_by: str = "zcode") -> dict:
    """回填主代理对 SOL 建议的采纳结论（accepted / modified / rejected）。

    采纳结论是事实记录：用于统计与恢复时复用已确认结论，不改变任务阶段、
    不改变 G1–G3、也不是用户授权。

    v2 增补 `decision`（ZCode 实际做法）、`rationale`（缺省回落 `reason`）、
    `delta`（modified 与原建议的差异）、`evidence_refs`（E 编号）、
    `source_refs`（报告章节/文件路径）与 `evidence_note`；原建议只存
    `advice_ref` 引用。缺项只产生非阻断提示，登记始终成功。
    """
    state = load_state(path)
    _assert_open(state, "登记 SOL 采纳结论", allow_blocked=True)
    value = str(status or "").strip().lower()
    if value not in ACCEPTANCE_STATUSES or value == "pending":
        raise GovernanceError(
            "采纳结论必须是 accepted / modified / rejected 之一。")
    record = _find_sol_call(state, call_id)
    if str(record.get("status") or "") != "completed":
        raise GovernanceError("只能对已完成的 SOL 调用登记采纳结论。")
    refs = _normalize_acceptance_evidence_refs(evidence_refs)
    sources = _normalize_acceptance_source_refs(source_refs)
    resolution = resolve_acceptance_evidence(state, refs)
    previous = record.get("acceptance")
    if not isinstance(previous, dict):
        previous = {}
    stamp = now_iso()
    entry = {
        "schema": ACCEPTANCE_SCHEMA,
        "status": value,
        "decision": str(decision or "").strip()[:4000],
        "rationale": str(rationale or reason or "").strip()[:1000],
        "delta": str(delta or "").strip()[:1000],
        "note": str(note or "")[:4000],
        "reason": str(reason or "")[:1000],
        "evidence_refs": refs,
        "evidence_resolution": resolution,
        "source_refs": sources,
        "evidence_note": str(evidence_note or "").strip()[:2000],
        "advice_ref": _acceptance_advice_ref(record),
        "decided_by": str(decided_by or "zcode")[:40],
        "at": stamp,
        "first_registered_at": str(previous.get("first_registered_at")
                                   or previous.get("at") or stamp),
        # 只计 v2 登记次数：升级前的次数不可可靠重建，不冒充全量。
        "registration_count": int(previous.get("registration_count") or 0) + 1,
        "basis": "zcode-judgment",
    }
    entry["warnings"] = _acceptance_warnings(entry, resolution, sources)
    # 当前记录以最近一次登记为准；每次登记在追加历史中保留完整快照，
    # 旧记录不被改写，也不隐式继承上一次的字段。
    record["acceptance"] = entry
    _history(state, "sol_call_acceptance", {
        "call_id": sol_call_id(record),
        "mode": sol_call_mode(record),
        "status": value,
        "decision_recorded": bool(entry["decision"]),
        "rationale_recorded": bool(entry["rationale"]),
        "evidence_refs": list(refs),
        "evidence_status": acceptance_evidence_status(entry),
        "warnings": list(entry["warnings"]),
        "acceptance": dict(entry),
    })
    save_state(path, state)
    return {
        "call_id": sol_call_id(record),
        "acceptance": entry,
        "evidence_status": acceptance_evidence_status(entry),
        "warnings": list(entry["warnings"]),
        "hints": [ACCEPTANCE_HINTS[code] for code in entry["warnings"]
                  if code in ACCEPTANCE_HINTS],
    }


def acceptance_quality(calls: list[dict]) -> dict:
    """SOL 采纳记录质量（只读）：完整性、证据关联与排除量分别披露。

    分母是 v2 已决采纳记录（accepted / modified / rejected）；pending 与无
    schema 的历史记录不计入分母，另以 `legacy_records` 披露。这是观察口径，
    不是调用次数、采纳率或证据数量的 KPI，也不影响任何门禁。
    """
    registered: list[dict] = []
    for item in calls:
        if str(item.get("status") or "") != "completed":
            continue
        acc = item.get("acceptance")
        if not isinstance(acc, dict):
            continue
        if str(acc.get("status") or "pending") not in (
                "accepted", "modified", "rejected"):
            continue
        registered.append(acc)
    current = [a for a in registered
               if str(a.get("schema") or "") == ACCEPTANCE_SCHEMA]
    complete = [a for a in current if acceptance_is_complete(a)]
    linked = [a for a in current
              if (a.get("evidence_resolution") or {}).get("linked")]
    unresolved = [a for a in current
                  if (a.get("evidence_resolution") or {}).get("unresolved")]
    archived = [a for a in current
                if (a.get("evidence_resolution") or {}).get("archived")]
    modified = [a for a in current if a.get("status") == "modified"]

    def _rate(count: int) -> float | None:
        return round(count / len(current), 3) if current else None

    return {
        "schema": "copilot-sol-acceptance-quality/v1",
        "records": len(current),
        "legacy_records": len(registered) - len(current),
        "complete": len(complete),
        "complete_rate": _rate(len(complete)),
        "evidence_linked": len(linked),
        "evidence_link_rate": _rate(len(linked)),
        "source_only": len([a for a in current
                            if acceptance_evidence_status(a) == "source_only"]),
        "no_evidence": len([a for a in current
                            if acceptance_evidence_status(a) == "none"]),
        "unresolved_records": len(unresolved),
        "archived_records": len(archived),
        "modified": len(modified),
        "modified_with_delta": len([a for a in modified
                                    if str(a.get("delta") or "").strip()]),
        "note": "登记时口径：完整率只看决策/理由/差异，证据关联率只看已登记 "
                "E（不代表内容已核验）；未登记或归档引用不进入正式证据链，"
                "旧版记录不计入分母。",
    }


def record_sol_call_failure(path: str, *, mode: str = "CONSULT",
                            error_code: str = "", detail: str = "",
                            materials_sha256: str = "",
                            model: str = "") -> dict:
    """登记一次失败的 SOL 调用；失败不改变任务阶段，也不阻断主代理继续。"""
    state = load_state(path)
    resolved = str(mode or "CONSULT").upper()
    if resolved not in RECORD_SOL_MODES:
        raise GovernanceError(f"未知 SOL 模式：{resolved}")
    record = {
        "schema": "copilot-sol-call-failure/v1",
        "call_id": "SC" + secrets.token_hex(12),
        "task_id": str(state.get("task_id") or ""),
        "mode": resolved,
        "status": "failed",
        "error": str(error_code or "sol_error"),
        "detail": str(detail or "")[:2000],
        "materials_sha256": str(materials_sha256 or ""),
        "model": model,
        "created_at": now_iso(),
    }
    state.setdefault(SOL_CALLS_KEY, []).append(record)
    _history(state, "sol_call_failed", {
        "call_id": record["call_id"],
        "mode": resolved,
        "error": record["error"],
        "materials_sha256": record["materials_sha256"],
    })
    save_state(path, state)
    return record


def record_sol_review(path: str, review: dict, *,
                      materials_sha256: str = "",
                      trigger: str = "", reason: str = "") -> dict:
    """历史名称：等同 `record_sol_call(..., mode="REVIEW")`，保留给既有调用方。"""
    return record_sol_call(path, review, mode="REVIEW",
                           materials_sha256=materials_sha256,
                           trigger=trigger, reason=reason)


def record_sol_advisory(path: str, advisory: dict, *,
                        materials_sha256: str = "",
                        trigger: str = "", reason: str = "") -> dict:
    """历史名称：等同 `record_sol_call(mode=CONSULT|历史模式)`，保留给既有调用方。"""
    return record_sol_call(path, advisory,
                           mode=str(advisory.get("mode") or "CONSULT"),
                           materials_sha256=materials_sha256,
                           trigger=trigger, reason=reason)


def record_sol_review_failure(path: str, *, error_code: str, detail: str = "",
                              materials_sha256: str = "",
                              model: str = "") -> dict:
    """历史名称：等同 `record_sol_call_failure(mode="REVIEW")`。"""
    return record_sol_call_failure(path, mode="REVIEW", error_code=error_code,
                                   detail=detail,
                                   materials_sha256=materials_sha256, model=model)


def record_sol_advisory_failure(path: str, *, mode: str = "CONSULT",
                                error_code: str = "", detail: str = "",
                                materials_sha256: str = "",
                                model: str = "") -> dict:
    """历史名称：等同 `record_sol_call_failure(mode=CONSULT|历史模式)`。"""
    return record_sol_call_failure(path, mode=mode, error_code=error_code,
                                   detail=detail,
                                   materials_sha256=materials_sha256, model=model)



def list_findings(state: dict) -> dict:
    """只读返回 Finding 台账摘要与条目。"""
    return RP.ledger_summary(state)


def record_finding_status(path: str, finding_id: str, status: str, *,
                          evidence_ids: list[str] | None = None,
                          note: str = "") -> dict:
    """推进 Finding 生命周期（本地、可审计、不触发复核）。

    `fixed`/`verified` 必须有证据；`closed` 只能从 verified 进入。定向测试
    结果登记为证据即可验证局部修复，不需要重新派发完整 Reviewer。
    """
    state = load_state(path)
    _assert_open(state, "更新 finding 台账", allow_blocked=True)
    if evidence_ids:
        # 与 record/criterion 同一口径：文件型证据重新读取并核对摘要，
        # 不允许用已改写/删除的文件充当“定向验证证据”。
        _validate_evidence_refs(state, list(evidence_ids))
    try:
        item = RP.transition(state, finding_id, status,
                             evidence_ids=evidence_ids, note=note)
    except RP.ReviewPolicyError as exc:
        raise GovernanceError(str(exc)) from None
    _history(state, "finding_status_changed", {
        "finding_id": str(item.get("finding_id") or ""),
        "status": str(item.get("status") or ""),
        "severity": str(item.get("severity") or ""),
        "evidence_ids": [str(item) for item in (evidence_ids or [])],
        "note": str(note or "")[:500],
    })
    save_state(path, state)
    return item


EXECUTION_BINDING_SCHEMA = "copilot-execution-binding/v1"


def create_execution_binding(path: str, *, work_item_id: str,
                             test_commands: list[str] | None = None,
                             workspace: str = "",
                             require_test_result: bool = False,
                             require_artifact_integrity: bool = False) -> dict:
    """在执行方启动前固化一次 execution binding（task/work_item/命令/nonce）。

    绑定不是代码快照：Executor 之后可以写代码；它证明“本轮执行会话是在这次
    任务派发之后新建的”，并防止复用同工作区旧 completed 子会话。
    """
    state = load_state(path)
    _assert_open(state, "创建 execution binding")
    wid = str(work_item_id or "").strip()
    dispatch = state.get("native_dispatch")
    if not isinstance(dispatch, dict):
        raise GovernanceError("状态没有 native_dispatch；先 drive 生成派发。")
    action = next((a for a in dispatch.get("actions") or []
                   if isinstance(a, dict)
                   and str(a.get("work_item_id") or "") == wid), None)
    if action is None:
        raise GovernanceError(f"dispatch 中没有工作项 {wid!r}；不能创建执行绑定。")
    commands = [str(item).strip() for item in (test_commands or [])
                if str(item).strip()]
    policy = (state.get("contract") or {}).get("test_policy") or {}
    policy_captured = bool(policy.get("captured"))
    if str(state.get("risk") or "").lower() == "high" and not policy_captured:
        raise GovernanceError(
            "risk=high 的 execution binding 必须来自合同 test_policy；"
            "不接受调用方自选测试命令的 caller binding。")
    contract_commands = [str(item) for item in policy.get("commands") or []]
    if policy_captured and contract_commands:
        if commands and commands != contract_commands:
            raise GovernanceError(
                "测试命令与合同登记不一致；合同要求的测试不能由执行方事后更换："
                f"{commands} vs {contract_commands}")
        commands = contract_commands
    if policy_captured and policy.get("required") and not commands:
        raise GovernanceError(
            "合同要求运行测试，但 binding 没有任何测试命令；请在合同 test_policy "
            "中先确定命令，再创建执行绑定。")
    require_result = bool(require_test_result) or bool(
        policy.get("require_test_result") if policy_captured else False)
    require_artifacts = bool(require_artifact_integrity) or bool(
        policy.get("require_artifact_integrity") if policy_captured else False)
    ws = os.path.realpath(os.path.abspath(os.path.expanduser(
        workspace or os.getcwd())))
    binding = {
        "schema": EXECUTION_BINDING_SCHEMA,
        "binding_id": "EB" + secrets.token_hex(12),
        "nonce": secrets.token_hex(16),
        "task_id": str(state.get("task_id") or ""),
        "contract_sha256": str(state.get("contract_sha256") or ""),
        "work_item_id": wid,
        "dispatch_id": str(action.get("dispatch_id") or ""),
        "workspace": ws,
        "test_commands": commands,
        "require_test_result": require_result,
        "require_artifact_integrity": require_artifacts,
        # 合同策略随 binding 冻结：调用方只能加严，不能在执行后关闭合同要求的层。
        "requirement_source": "contract" if policy_captured else "caller",
        "contract_test_policy": {
            key: policy.get(key) for key in
            ("required", "commands", "report", "artifacts",
             "require_test_result", "require_artifact_integrity")
        } if policy_captured else {},
        "contract_report": str(policy.get("report") or "") if policy_captured else "",
        "contract_artifacts": [str(item) for item in policy.get("artifacts") or []]
        if policy_captured else [],
        "created_at": now_iso(),
        "created_by": "zcode-native",
        "consumed": False,
        "consumed_by_result_sha256": "",
        "consumed_at": "",
    }
    state.setdefault("execution_bindings", []).append(binding)
    _history(state, "execution_binding_created", {
        "binding_id": binding["binding_id"],
        "work_item_id": wid,
        "test_command_count": len(commands),
    })
    save_state(path, state)
    return binding


def find_execution_binding(state: dict, binding_id: str) -> dict | None:
    for binding in state.get("execution_bindings") or []:
        if isinstance(binding, dict) and \
                str(binding.get("binding_id") or "") == str(binding_id or ""):
            return binding
    return None


def execution_binding_problems(state: dict, binding_id: str,
                               *, work_item_id: str = "",
                               test_commands: list[str] | None = None,
                               workspace: str = "") -> list[str]:
    """校验 execution binding 与本次执行对象一致且未被消费。"""
    binding = find_execution_binding(state, binding_id)
    if binding is None:
        return ["execution_binding_missing"]
    problems: list[str] = []
    if str(binding.get("schema") or "") != EXECUTION_BINDING_SCHEMA:
        problems.append("execution_binding_schema")
    if not str(binding.get("nonce") or ""):
        problems.append("execution_binding_nonce_missing")
    if str(binding.get("task_id") or "") != str(state.get("task_id") or ""):
        problems.append("execution_binding_cross_task")
    if str(binding.get("contract_sha256") or "") != str(
            state.get("contract_sha256") or ""):
        problems.append("execution_binding_contract_changed")
    if work_item_id and str(binding.get("work_item_id") or "") != work_item_id:
        problems.append("execution_binding_work_item_mismatch")
    if binding.get("consumed"):
        problems.append("execution_binding_consumed")
    expected = [str(item) for item in binding.get("test_commands") or []]
    if expected and [str(item) for item in (test_commands or [])] != expected:
        problems.append("execution_binding_command_mismatch")
    ws = os.path.realpath(os.path.abspath(os.path.expanduser(
        workspace or binding.get("workspace") or "")))
    if ws and str(binding.get("workspace") or "") and \
            ws != str(binding.get("workspace") or ""):
        problems.append("execution_binding_workspace_mismatch")
    return problems


def _finding_reason_detail(item: dict) -> str:
    fid = str(item.get("finding_id") or "-")
    severity = str(item.get("severity") or "")
    status = str(item.get("status") or "open")
    where = str(item.get("file") or "")
    if item.get("line") is not None:
        where += f":{item.get('line')}"
    summary = str(item.get("summary") or "")[:160]
    action = {
        "open": "先修复并用定向测试/证据验证，不要直接重跑完整复核",
        "regressed": "已修复问题再次出现：先修复并给出复现证据",
        "fixed": "已修复但尚未定向验证：运行对应测试并登记证据后标记 verified",
    }.get(status, "先闭合该 finding")
    return (f"Finding {fid}（{severity}，{status}）仍未闭合"
            + (f"：{where} " if where else "：")
            + f"{summary}；{action}。")


_TEMP_EVIDENCE_PREFIXES = ("/tmp/", "/var/tmp/", "/dev/shm/")


def evidence_warnings(state: dict, *, task_dir: str = "") -> list[str]:
    """证据卫生提醒：重复登记、临时目录与未登记文件。

    非阻断：同一文件登记两次、不同时点快照内容相同都可能是刻意保留，G1–G3 不应
    因此失败。但这些情况会让收口后的证据包难以解释，所以在收口检查里提示一次，
    由 ZCode 决定是否补 note、归档或保留。
    """
    warnings: list[str] = []
    live = [e for e in state.get("evidence") or []
            if isinstance(e, dict) and not e.get("dropped")]
    by_path: dict[str, list[str]] = {}
    by_sha: dict[str, list[str]] = {}
    for entry in live:
        eid = str(entry.get("id") or "")
        hint = str(entry.get("path_hint") or "")
        if hint and hint != "<inline>":
            by_path.setdefault(hint, []).append(eid)
            if hint.startswith(_TEMP_EVIDENCE_PREFIXES):
                warnings.append(f"{eid} 的证据文件位于临时目录，可能被系统清理：{hint}")
        sha = str(entry.get("sha256") or "")
        if sha:
            by_sha.setdefault(sha, []).append(eid)
    for hint, ids in sorted(by_path.items()):
        if len(ids) > 1:
            warnings.append(f"{'/'.join(ids)} 指向同一证据文件：{hint}（重复登记）")
    for _sha, ids in sorted(by_sha.items()):
        if len(ids) > 1:
            warnings.append(f"{'/'.join(ids)} 内容完全相同（sha256 相同）："
                            "若为不同时点的快照，请在 note 里说明差异")
    if task_dir:
        evidence_dir = os.path.join(task_dir, "evidence")
        try:
            names = sorted(os.listdir(evidence_dir)) if os.path.isdir(evidence_dir) else []
        except OSError:
            names = []
            warnings.append(f"无法读取证据目录，未检查未登记文件：{evidence_dir}")
        # 归档（dropped）证据的文件仍留在磁盘上，属于已登记材料，不应报“未登记”。
        registered = {str(e.get("path_hint") or "")
                      for e in state.get("evidence") or [] if isinstance(e, dict)}
        for name in names:
            full = os.path.join(evidence_dir, name)
            if os.path.isdir(full) or full in registered:
                continue
            warnings.append(f"evidence/ 下有未登记文件：{name}"
                            "（确认是否需要登记或归档）")
    # 执行信封 evidence[] 与 live 证据的漂移：证据在回填后被更正或归档时，
    # 执行正文可能仍引用旧快照（gromore A13 的实际 major）。非阻断提示。
    live_state = {
        str(e.get("id") or ""): (str(e.get("sha256") or ""), bool(e.get("dropped")))
        for e in state.get("evidence") or [] if isinstance(e, dict)
    }
    for rec in state.get("native_results") or []:
        if not isinstance(rec, dict):
            continue
        wid = str(rec.get("work_item_id") or "")
        for item in rec.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            eid = str(item.get("id") or "")
            if not eid:
                continue
            current = live_state.get(eid)
            if current is None:
                warnings.append(
                    f"{wid} 的执行结果引用了未登记证据 {eid}；执行前请先登记")
                continue
            live_sha, dropped = current
            if dropped:
                warnings.append(
                    f"{wid} 的执行结果引用已归档证据 {eid}；请确认执行正文是否仍成立")
            elif live_sha != str(item.get("sha256") or ""):
                warnings.append(
                    f"{wid} 的执行结果引用 {eid} 的旧摘要（live 已更新）；"
                    "证据更正后应重新回填执行结果，或在复核结论中说明")
    return warnings


def _parse_time(value: object):
    text = str(value or "").strip()
    if not text:
        return None
    # 宿主元数据用 Z 后缀，Python 3.10 的 fromisoformat 只认 +00:00。
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _seconds_between(start: object, end: object) -> float | None:
    first, second = _parse_time(start), _parse_time(end)
    if first is None or second is None:
        return None
    return round((second - first).total_seconds(), 1)



def review_metrics(state: dict, *, task_dir: str = "") -> dict:
    """复核与台账指标（只读，不改状态）。

    v8 起 G4 原生只读复核已退役：活动指标只统计 SOL 单轮复核、复核准入、
    Finding 台账与正式 REVIEW。历史状态里的 `native_reviews` /
    `review_attempts` / `g4_attestations` 只读保留，不计入活动指标。
    """
    # 统一 SOL 调用流：`sol_calls` 权威 + 历史 sol_reviews / sol_advisories 只读兼容，
    # 指标口径只有一套，不因历史存放位置不同而重复计数。
    sol_calls = iter_sol_calls(state)
    sol_reviews = [r for r in sol_calls if sol_call_mode(r) == "REVIEW"]
    consult_calls = [r for r in sol_calls if sol_ref_mode(r) == "CONSULT"]
    plan_calls = [r for r in sol_calls if sol_ref_mode(r) == "PLAN"]
    cognitive_calls = consult_calls + plan_calls
    admissions = [item for item in state.get("review_admissions") or []
                  if isinstance(item, dict)]
    admission_triggers: dict[str, int] = {}
    admission_tiers: dict[str, int] = {}
    for item in admissions:
        tier = str(item.get("tier") or "")
        if tier:
            admission_tiers[tier] = admission_tiers.get(tier, 0) + 1
        for trigger in item.get("triggers") or []:
            value = str(trigger or "")
            if value:
                admission_triggers[value] = admission_triggers.get(value, 0) + 1
    invalid_attempts: dict[str, int] = {"execution": 0, "review": 0,
                                        "review_build": 0}
    attempt_codes: dict[str, int] = {}
    if task_dir:
        path = os.path.join(task_dir, "review-attempts.jsonl")
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(entry, dict):
                            continue
                        kind = str(entry.get("kind") or "")
                        if kind in invalid_attempts:
                            invalid_attempts[kind] += 1
                        code = str(entry.get("code") or "")
                        if code:
                            attempt_codes[code] = attempt_codes.get(code, 0) + 1
            except OSError:
                pass
    closure = state.get("closure") or {}
    formal_records = [r for r in state.get("formal_reviews") or []
                      if isinstance(r, dict)]
    formal_ids = {str(r.get("review_id") or "") for r in formal_records} - {""}
    superseded_ids = {
        old for r in formal_records
        if (old := str(r.get("supersedes") or "")) and old in formal_ids
    }
    formal_amendments = sum(len(r.get("amendments") or [])
                            for r in formal_records)
    ledger = RP.ledger_summary(state)
    ledger.pop("items", None)
    stops = [item for item in state.get("review_stops") or []
             if isinstance(item, dict)]

    def _acceptance_counts(calls: list[dict]) -> dict:
        counts = {status: 0 for status in ACCEPTANCE_STATUSES}
        for item in calls:
            if str(item.get("status") or "") != "completed":
                continue
            value = str((item.get("acceptance") or {}).get("status")
                        or "pending").lower()
            if value not in counts:
                value = "pending"
            counts[value] += 1
        return counts

    completed_cognitive = [r for r in cognitive_calls
                           if str(r.get("status") or "") == "completed"]
    adoption = _acceptance_counts(cognitive_calls)
    # 重复调用：带 trigger/reason 的认知调用（同模式第二轮的决策价值声明）。
    repeated = [r for r in completed_cognitive
                if str(r.get("trigger") or "").strip()]
    usage_fields: dict[str, int] = {}
    latency_ms = 0.0
    latency_samples = 0
    for item in completed_cognitive:
        usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                usage_fields[str(key)] = usage_fields.get(str(key), 0) + int(value)
        # 调用记录用 `latency_ms`；`duration_ms` 是早期字段，读取时一并兼容。
        duration = item.get("latency_ms")
        if duration is None:
            duration = item.get("duration_ms")
        if isinstance(duration, (int, float)):
            latency_ms += float(duration)
            latency_samples += 1
    complexity = RP.task_complexity(
        state, triggers=[str(t.get("name") or "")
                         for t in state.get("admission_triggers") or []
                         if isinstance(t, dict)],
        workspace=str(state.get("workspace") or ""))
    non_simple = bool(complexity.get("non_simple"))
    return {
        "schema": "copilot-review-metrics/v2",
        "task_id": str(state.get("task_id") or ""),
        "risk": str(state.get("risk") or ""),
        "task_kind": str(state.get("task_kind") or RP.DEFAULT_TASK_KIND),
        "task_kind_source": str(state.get("task_kind_source") or "default"),
        "complexity": {"level": complexity["level"],
                       "signals": complexity["signal_codes"]},
        "execution_backend": str(state.get("execution_backend") or ""),
        "closed": bool(closure.get("closed")),
        "review_admissions_total": len(admissions),
        "review_admission_tiers": dict(sorted(admission_tiers.items())),
        "review_admission_triggers": dict(sorted(admission_triggers.items())),
        "review_stops_total": len(stops),
        "formal_reviews_total": len(formal_records),
        "formal_review_amendments_total": formal_amendments,
        "superseded_formal_reviews_total": len(superseded_ids),
        "active_formal_reviews_total": len(formal_records) - len(superseded_ids),
        "invalid_envelopes": invalid_attempts,
        "invalid_envelope_codes": dict(sorted(attempt_codes.items())),
        "finding_ledger": ledger,
        "rereported_findings": len(ledger.get("rereported") or []),
        "task_seconds": _seconds_between(
            state.get("created"), closure.get("closed_at") or state.get("updated")),
        "reopen_count": len(closure.get("reopen_history") or []),
        "sol": {
            "calls_total": len(sol_calls),
            "by_mode": {
                mode: len([r for r in sol_calls
                           if sol_ref_mode(r) == mode])
                for mode in SOL_MODES},
            "total": len(sol_reviews),
            "completed": len([r for r in sol_reviews
                              if r.get("status") == "completed"]),
            "failed": len([r for r in sol_reviews
                           if r.get("status") == "failed"]),
            "verdicts": {
                verdict: len([r for r in sol_reviews
                              if r.get("status") == "completed"
                              and r.get("verdict") == verdict])
                for verdict in ("pass", "partial", "blocked")},
            "findings": {
                severity: sum(int((r.get("findings_count") or {}).get(severity)
                                  or 0)
                              for r in sol_reviews
                              if r.get("status") == "completed")
                for severity in ("blocking", "major", "minor", "info")},
            "single_round": True,
        },
        "sol_cognitive": {
            "non_simple": bool(non_simple),
            "default_covered": bool(non_simple
                                    and any(str(r.get("status") or "") == "completed"
                                            for r in cognitive_calls)),
            "consult_total": len(consult_calls),
            "consult_completed": len([r for r in consult_calls
                                      if r.get("status") == "completed"]),
            "plan_total": len(plan_calls),
            "plan_completed": len([r for r in plan_calls
                                   if r.get("status") == "completed"]),
            "failed": len([r for r in cognitive_calls
                           if r.get("status") == "failed"]),
            "repeated": len(repeated),
            "repeat_rate": (round(len(repeated) / len(completed_cognitive), 3)
                            if completed_cognitive else 0.0),
            "acceptance": adoption,
            "adopted": adoption["accepted"] + adoption["modified"],
            "acceptance_quality": acceptance_quality(cognitive_calls),
            "usage": dict(sorted(usage_fields.items())),
            "latency_ms_avg": (round(latency_ms / latency_samples, 1)
                               if latency_samples else None),
            "latency_unavailable": latency_samples == 0,
            "modes": {
                mode: len([r for r in cognitive_calls
                           if r.get("status") == "completed"
                           and sol_call_mode(r) == mode])
                for mode in ("CONSULT", "PLAN", "THINK")},
            "advisory_only": True,
        },
        "sol_think": {
            "total": len(consult_calls),
            "completed": len([r for r in consult_calls
                              if r.get("status") == "completed"]),
            "failed": len([r for r in consult_calls
                           if r.get("status") == "failed"]),
            "modes": {
                mode: len([r for r in consult_calls
                           if r.get("status") == "completed"
                           and str(r.get("mode") or "").upper() == mode])
                for mode in ("THINK", "CONSULT")},
            "advisory_only": True,
        },
        "g4": {
            "retired": True,
            "historical_native_reviews": len(state.get("native_reviews") or []),
            "historical_review_attempts": len(state.get("review_attempts") or []),
            "historical_attestations": len(state.get("g4_attestations") or []),
        },
        "note": "活动复核只有 SOL 单轮复核（一次请求、零工具、advisory）。G4 原生"
                "复核已退役；历史 native_reviews/review_attempts/g4_attestations "
                "只读保留，不参与新任务准入或收口。",
    }


def _gate_findings(state: dict) -> dict:
    """Finding 台账门禁：未闭合 blocking/major 阻断收口（所有风险等级）。

    G4 退役后，台账不再由高风险专用复核统一检查：advisory 复核（SOL）报告
    的 blocking/major 仍进入台账，主代理必须修复并用定向验证推进生命周期，
    或在给出反证后显式驳回；未闭合条目在任何风险等级下都阻断收口。
    """
    reasons = []
    for item in RP.blocking_findings(state):
        status = str(item.get("status") or "open")
        code = ("native_review_finding_unverified" if status == "fixed" else
                "native_review_finding_regressed" if status == "regressed" else
                "native_review_finding_open")
        reasons.append({
            "code": code,
            "detail": _finding_reason_detail(item),
            "finding_id": str(item.get("finding_id") or ""),
        })
    return {"passed": not reasons, "reasons": reasons}


def closure_check(state: dict, *, task_dir: str = "",
                  cache: dict | None = None) -> dict:
    """三道门收口检查。`cache` 只在单次命令内复用证据摘要。"""
    if cache is None:
        cache = {}
    criteria = _gate_criteria(state, cache=cache)
    quality = _gate_quality(state, cache=cache)
    findings = _gate_findings(state)
    risks = {
        "G1": _gate_actions(state),
        "G2": {"passed": criteria["passed"] and quality["passed"]
               and findings["passed"],
               "reasons": criteria["reasons"] + quality["reasons"]
               + findings["reasons"]},
        "G3": _gate_stalled(state),
    }
    reasons = []
    for name in ("G1", "G2", "G3"):
        for reason in risks[name]["reasons"]:
            reasons.append({"gate": name, **reason})
    return {"passed": not reasons, "iteration_id": _current_iteration_id(state),
            "gates": risks, "reasons": reasons,
            "quality": quality["quality"],
            "warnings": contract_source_warnings(state)
            + evidence_warnings(state, task_dir=task_dir)}


def close_task(path: str, *, by: str = "zcode-native",
               cache: dict | None = None) -> dict:
    if cache is None:
        cache = {}
    state = load_state(path)
    if bool((state.get("closure") or {}).get("closed")):
        return state["closure"]
    if any(not b.get("resolved_at") for b in state.get("blocks") or []):
        raise GovernanceError("存在未解除的阻塞：先 unblock 再收口。")
    check = closure_check(state, task_dir=os.path.dirname(os.path.abspath(path)),
                          cache=cache)
    if not check["passed"]:
        summary = "; ".join(f"{r.get('gate')}:{r.get('code')}" for r in check["reasons"])
        raise GovernanceError(f"G1–G3 未通过，不能收口：{summary}")
    if _reopened_without_progress(state):
        # 重开后的 PASS 必须由重开之后的新进展支撑：否则直接 close 会绕过
        # evaluate 的同一判定，让“用户认为旧结论不成立”变成一句空话。
        raise GovernanceError(
            "任务重开后尚未产生新的实质进展（新证据、工作项结果或条件重核销）："
            "旧 PASS 不能直接复用；先完成重开要求的工作再收口。")
    quality = quality_summary(state, cache=cache)
    # 原始质量结论与任务关闭状态分离：用户接受 PWW 不把 verdict 改写为 PASS。
    verdict = PWW if quality.get("verdict") == PWW else "pass"
    closure_quality = {
        "verdict": quality.get("verdict"),
        "raw_verdict": quality.get("raw_verdict"),
        "report_id": quality.get("report_id") or "",
        "acceptance_status": quality.get("acceptance_status"),
        "accepted_ids": list(quality.get("accepted_ids") or []),
        "accepted_by": quality.get("accepted_by") or "",
        "accepted_at": quality.get("accepted_at") or "",
        "binding_current": quality.get("binding_current"),
    }
    state["closure"].update({
        "closed": True,
        "closed_by": by,
        "verdict": verdict,
        "quality": closure_quality,
        "iteration_id": _current_iteration_id(state),
        "closed_at": now_iso(),
        "gate": {"passed": True, "iteration_id": _current_iteration_id(state),
                 "reasons": []},
    })
    _history(state, "task_closed", {
        "verdict": verdict, "quality_verdict": quality.get("verdict"),
        "report_id": quality.get("report_id") or "",
        "accepted_ids": closure_quality["accepted_ids"], "by": by})
    # 收口时结束当前迭代：否则状态里会留下 phase=RUNNING、ended=null 的迭代，
    # 与已 CLOSED 的任务自相矛盾。历史记录只增不改，这里只补 ended/phase。
    for iteration in state.get("iterations") or []:
        if isinstance(iteration, dict) and not iteration.get("ended"):
            iteration["ended"] = state["closure"]["closed_at"]
            iteration["phase"] = "CLOSED"
    save_state(path, state)
    return state["closure"]


def reopen_task(path: str, reason: str, *, authorized_by: str = "") -> dict:
    state = load_state(path)
    reason = (reason or "").strip()
    authorized_by = (authorized_by or "").strip()
    if authorized_by != "user":
        raise GovernanceError(
            "重开已收口任务必须有用户授权：显式传入 --authorized-by user，并写明原因。")
    if not reason:
        raise GovernanceError("重开任务必须说明用户授权原因。")
    if not bool((state.get("closure") or {}).get("closed")):
        raise GovernanceError("任务未收口，无需重开。")
    # 快照必须在改写 closure 之前完成：old 若引用同一个 dict，reopen_history
    # 里的 previous 会指回 closure 自身，落盘时触发循环引用。
    old = json.loads(json.dumps(state.get("closure") or {}))
    state["closure"].update({
        "closed": False,
        "closed_by": None,
        "verdict": None,
        "quality": None,
        "iteration_id": None,
        "closed_at": None,
        "gate": {"passed": None, "iteration_id": None, "reasons": []},
    })
    state["closure"].setdefault("reopen_history", []).append({
        "reason": reason, "at": now_iso(), "authorized_by": authorized_by,
        # 与合同修订同一口径：authorized_by 是调用方自报，不是可验证凭证；
        # reason 记录用户原话/要求，供事后审计核对授权范围。
        "authorization_source": "caller-declared",
        "previous": old,
    })
    # 重开意味着还会有新的工作/证据/代码；旧质量结论（含普通 PASS）不能直接
    # 复用。标记 reopened_at 让 binding 立即失效，必须对当前状态重新验收。
    for report in active_quality_reports(state):
        report["reopened_at"] = now_iso()
        if str(report.get("verdict") or "") != PWW:
            continue
        if str(report.get("acceptance_status") or "") in ("accepted", "partial"):
            report["acceptance_status"] = "stale"
            acceptance = report.get("acceptance")
            if isinstance(acceptance, dict):
                acceptance["status"] = "stale"
            _history(state, "pww_acceptance_invalidated",
                     {"report_id": report.get("report_id"),
                      "reason": "task_reopened"})
    criterion = _acceptance_criterion(state)
    if criterion is not None:
        if str(criterion.get("status") or "") == "met":
            criterion["status"] = "pending"
            criterion["evidence_refs"] = []
            criterion["updated_at"] = now_iso()
            _history(state, "pww_acceptance_condition_reopened",
                     {"criterion_id": criterion.get("criterion_id")})
        if isinstance(criterion.get("acceptance"), dict):
            criterion["acceptance"]["status"] = "pending"
        # 接受条件回到未闭合：它自动派生的包装工作项也要恢复，否则重开后
        # 没有任何可执行项却永远差一条。
        _sync_criterion_work_items(state, str(criterion.get("criterion_id") or ""))
    # 重开后任务重新进入活动状态：上一轮已随收口标为 CLOSED，不能改回 RUNNING
    # （会出现“同一轮既已收口又在进行”）。开一条新迭代承接后续工作。
    begin_iteration(state, note=f"重开：{reason[:80]}")
    # 基线在重开的全部状态改写之后采集：重开本身（迭代、接受条件回退）不算进展，
    # 之后的证据、工作项结果或条件重核销才算。
    history_entry = state["closure"]["reopen_history"][-1]
    history_entry["baseline"] = _progress_snapshot(state)
    _history(state, "task_reopened", {"reason": reason[:200],
                                      "authorized_by": authorized_by})
    save_state(path, state)
    return state["closure"]


# ── 阶段求值 / PARTIAL 推进 ────────────────────────────────────────────────
def _unmet_criteria(state: dict) -> list[dict]:
    return [c for c in (state.get("contract") or {}).get("criteria") or []
            if str(c.get("status") or "pending") != "met"]


def _derive_id(state: dict, base: str) -> str:
    used = {str(a.get("id") or "") for a in state.get("actions") or []}
    if base not in used:
        return base
    for index in range(2, 100):
        candidate = f"{base}-R{index}"
        if candidate not in used:
            return candidate
    raise GovernanceError(f"派生工作项 id 已用尽：{base}")


def derive_work(state: dict, *, reason: str = "partial",
                cache: dict | None = None) -> list[dict]:
    """按未闭合条件 / 停摆项 / Finding 缺口本地派生工作项；不调用任何模型。

    无进展保护：同一根因被反复派生仍未闭合时，额外派生 AUTO-REPLAN，要求分析根因
    并选择实质不同的替代路线；不把「再试一次同样的路径」当作推进。
    """
    created: list[dict] = []
    now = now_iso()
    boundaries = str((state.get("contract") or {}).get("boundaries") or "").strip()
    base_constraints = ["不得越过原始授权范围；不得自行修改目标、完成条件或业务规则。",
                        "只做缺口需要的最小改动，不顺手重构无关内容。"]
    if boundaries:
        base_constraints.insert(0, f"原合同边界：{boundaries}")

    def previous_attempts(base_id: str) -> list[str]:
        # AUTO-FIX-F1 / AUTO-VERIFY-F1 按 finding 家族累计，避免修复反复失败时
        # 无限产生 -R2/-R3。
        prefix = base_id
        rows = []
        for item in state.get("actions") or []:
            aid = str(item.get("id") or "")
            if aid == prefix or aid.startswith(prefix + "-R"):
                rows.append(f"{aid}[{item.get('status')}] "
                            f"{str(item.get('result') or '')[:160]}")
        return rows

    def add(base_id: str, title: str, goal: str, expected: str,
            *, origin: str = "partial-derived", extra: dict | None = None) -> None:
        if len(created) >= MAX_DERIVED_PER_CALL:
            return
        # 同一缺口已有可领取/进行中的工作项时不再派生第二条。重复调用 drive 或
        # 多个治理入口同时求值时，不能把 AUTO-C1 无限复制成 -R2/-R3。
        for item in state.get("actions") or []:
            if not isinstance(item, dict):
                continue
            aid = str(item.get("id") or "")
            if (aid == base_id or aid.startswith(base_id + "-R")) and \
                    str(item.get("status") or "") in ("pending", "running"):
                return
        attempts = previous_attempts(base_id) if base_id.startswith(
            ("AUTO-C", "AUTO-R-", "AUTO-FIX-", "AUTO-VERIFY-")) else []
        if len(attempts) >= 3:
            # 替代路线正在执行时不再派生同路径尝试：等它产出结果。
            # 替代路线已结束但缺口仍未闭合时，不再把下一步留给通用 AUTO-NEXT：
            # 派生新的 REPLAN，要求说明上一路线为何未闭合并再换一条实质不同的路线。
            # 只有“已结束的 REPLAN”不会掩盖缺口，未闭合条件仍会持续派生工作项。
            replans = [a for a in state.get("actions") or []
                       if str(a.get("id") or "").startswith("AUTO-REPLAN")]
            if any(str(a.get("status") or "") in ("pending", "running")
                   for a in replans):
                return
            prior_note = ""
            if replans:
                prior_note = ("上一替代路线未闭合：" + "；".join(
                    f"{a.get('id')}[{a.get('status')}] {str(a.get('result') or '')[:160]}"
                    for a in replans[-3:]) +
                    "。请先说明它为何仍未闭合，再选择实质不同的新路线。")
            replan = {
                "id": _derive_id(state, "AUTO-REPLAN"),
                "title": "无进展：重新分析根因并选择替代路线",
                "assignee": "zcode",
                "goal": (f"{base_id} 已派生 {len(attempts)} 次仍未闭合。已尝试："
                         + "；".join(attempts) + "。" + prior_note +
                         "请分析根因，选择与已失败方案实质不同的替代路线；"
                         "不得机械重复同一失败路径，也不得降低验收标准。"),
                "constraints": "\n".join(base_constraints)[:2000],
                "expected_evidence": "根因分析、已尝试方案、失败原因、替代方案与新的执行结果",
                "depends_on": [],
                "status": "pending",
                "result": "",
                "evidence_ids": [],
                "fail_streak": 0,
                "origin": "replan",
                "derived_reason": reason,
                "created": now,
                "updated": now,
            }
            state.setdefault("actions", []).append(replan)
            created.append(replan)
            _history(state, "work_derived", {"id": replan["id"],
                                             "origin": "replan",
                                             "reason": reason})
            # 只派生替代路线，不再追加第 4 次同路径尝试。
            return
        aid = _derive_id(state, base_id)
        action = {
            "id": aid,
            "title": title[:300],
            "assignee": "zcode",
            "goal": goal[:4000],
            "constraints": "\n".join(base_constraints)[:2000],
            "expected_evidence": expected[:2000],
            "depends_on": [],
            "status": "pending",
            "result": "",
            "evidence_ids": [],
            "fail_streak": 0,
            "origin": origin,
            "derived_reason": reason,
            "created": now,
            "updated": now,
        }
        if extra:
            action.update(extra)
        state.setdefault("actions", []).append(action)
        created.append(action)
        _history(state, "work_derived", {"id": aid, "origin": origin,
                                         "reason": reason})

    contract = state.get("contract") or {}
    if not contract.get("criteria") or contract.get("criteria_state") != "confirmed":
        add("AUTO-CONTRACT", "补齐任务合同与完成条件",
            "为当前任务补齐可核销的完成条件与边界，并写回状态；"
            "没有完成条件就无法产生 PASS。",
            "更新后的合同/完成条件文本与 criteria 摘要",
            origin="contract-required")
        return created

    unmet = _unmet_criteria(state)
    accept_derived = False
    blocked_cids = _blocked_criteria(state)
    # 已被其它会话领取的工作项覆盖的条件，不再派生 AUTO-C<n>：否则两个会话会
    # 对同一条件各执行一遍。租约到期后这里自动恢复派生。
    claimed_actions = {str(a.get("id") or ""): a for a in _tracked_actions(state)}
    claimed_cids: set[str] = set()
    for aid in active_claims(state):
        action = claimed_actions.get(aid)
        if action is not None:
            claimed_cids.update(_action_criteria(action))
    for criterion in unmet[:MAX_DERIVED_PER_CALL]:
        cid = str(criterion.get("criterion_id") or "")
        # 已随工作项一起声明的阻塞条件不再派生：缺授权/决策时重复派生只会
        # 制造无法执行的工作单。
        if cid in blocked_cids or cid in claimed_cids:
            continue
        acceptance = criterion.get("acceptance")
        if isinstance(acceptance, dict):
            # 接受条件只能对应生效中的 PWW 报告且绑定仍当前；否则由 AUTO-QUALITY
            # 负责重新验收，不能派生无法执行的 AUTO-ACCEPT。
            report_id = str(acceptance.get("report_id") or "")
            active = next((item for item in active_quality_reports(state)
                           if str(item.get("report_id") or "") == report_id), None)
            if active is None or active.get("verdict") != PWW or \
                    not _quality_binding_current(state, active):
                continue
            if str(active.get("acceptance_status") or "") in ("stale",
                                                              "superseded"):
                continue
            if _code_scope_status(active, state, cache).get("reasons"):
                continue
            accept_derived = True
            # 接受条件不能由执行方“核销”：只能把用户明确表达交给 accept。
            if any(str(a.get("id") or "").startswith("AUTO-ACCEPT")
                   and str(a.get("status") or "") in ("pending", "running")
                   for a in state.get("actions") or []):
                continue
            required = "、".join(acceptance.get("required_ids") or [])
            add("AUTO-ACCEPT", "请求用户明确接受 PWW 警告",
                f"完成条件 {cid} 是 PWW 用户接受条件（报告 "
                f"{acceptance.get('report_id') or ''}，待接受 {required}）。"
                "请把原始 verdict、全部 W/R 标识、残余风险与未验证项如实呈现给用户，"
                "取得用户明确接受后，用 accept --by user --accept <id列表> "
                "--quote <用户原话> 登记；执行方、子代理、verify 与 Copilot 都不得"
                "代替用户接受，也不得用 criterion 直接核销该条件。",
                "用户明确接受的原话、接受范围（全部 W/R id）、接受时间；"
                "accept 成功回执与 G2 通过结果",
                origin="pww-acceptance")
            continue
        add(f"AUTO-{cid}", f"闭合完成条件 {cid}",
            f"使完成条件 {cid}「{criterion.get('description') or ''}」闭合："
            "执行所需工作，登记覆盖该条件的原始证据，并用 criterion 记录 met。",
            f"覆盖 {cid} 的原始产物（命令输出 / 文件内容 / 日志 / diff / 测试结果），"
            "登记为证据后引用",
            extra={"target_criteria": [cid]})

    stalled = stalled_actions(state)
    for action, why in stalled[:MAX_DERIVED_PER_CALL]:
        if len(created) >= MAX_DERIVED_PER_CALL:
            break
        aid = str(action.get("id") or "")
        # 失败/停摆的 AUTO-C / AUTO-FIX / AUTO-VERIFY 工作项已由各自缺口分支
        # 重新派生（AUTO-C1-R2 / AUTO-FIX-F1-R2），不再额外派生恢复项，避免
        # 同一缺口出现两份工作单。
        if aid.startswith(("AUTO-C", "AUTO-FIX-", "AUTO-VERIFY-")):
            continue
        # 恢复项按根因家族归一：AUTO-R-W1-R2（甚至旧的 AUTO-R-AUTO-R-W1）都属于
        # 同一个缺口。不归一就会层层套出 AUTO-R-AUTO-R-…，3 次上限永远不触发。
        base_id = aid
        while base_id.startswith("AUTO-R-"):
            base_id = base_id[len("AUTO-R-"):]
        base_id = re.sub(r"-R[0-9]+$", "", base_id) or aid
        add(f"AUTO-R-{base_id}", f"处理停摆工作项 {aid}",
            f"处理工作项 {aid}（{why}）：分析失败根因，重试、调整方案或说明真实阻塞；"
            "不得机械重复同一失败路径。",
            "失败原因分析 + 新的执行结果/证据；若确属外部阻塞，用 block 记录原因与所需条件",
            origin="recovery")

    check = closure_check(state, cache=cache)
    acceptance_only = {"pww_acceptance_pending", "pww_acceptance_partial"}
    quality_codes = sorted({
        str(r.get("code") or "") for r in check.get("reasons") or []
        if str(r.get("code") or "").startswith(("pww_", "quality_"))
        and not (accept_derived and str(r.get("code") or "") in acceptance_only)})
    if quality_codes and not any(
            str(a.get("id") or "").startswith("AUTO-QUALITY")
            and str(a.get("status") or "") in ("pending", "running")
            for a in state.get("actions") or []):
        add("AUTO-QUALITY", "重新验收并接入质量报告",
            "G2 质量门禁未通过（" + "、".join(quality_codes) + "）。请对当前目标与代码"
            "重新运行 verify；代码类报告先执行 code_scope_capture.py --workspace <repo> "
            "--state <state> 并把 commit/code_scope_sha256/files_paths 写入 subject。"
            "接入时用 report --verify-report <报告> --workspace <repo>，该路径必须与任务"
            "绑定工作区一致（未绑定时先 start --workspace 或登记任务仓库内证据），并用 "
            "supersedes_report_id 指向当前生效报告；旧未解决 W/R 必须继承到新 PWW 或用 "
            "resolved_warning_ids/resolved_residual_risk_ids 在 subject 一致时声明解决。"
            "不得自行接受 PWW，用户明确接受后才能 accept。",
            "新的 verify 报告、替代/解决声明、代码快照摘要与 G2 通过结果",
            origin="quality-gate")
    # 未闭合的 blocking/major 先修复，已修复未验证的做定向测试验证。G4 退役后
    # 这里不再派生复核工作项：SOL 复核是 advisory，是否调用由主代理按风险决定，
    # 不能变成一条阻塞收口的工作单。
    unresolved_findings = RP.blocking_findings(state)
    for item in unresolved_findings[:MAX_DERIVED_PER_CALL]:
        fid = str(item.get("finding_id") or "")
        status = str(item.get("status") or "open")
        where = str(item.get("file") or "")
        if item.get("line") is not None:
            where += f":{item.get('line')}"
        summary = str(item.get("summary") or "")[:600]
        severity = str(item.get("severity") or "")
        if status == "fixed":
            add(f"AUTO-VERIFY-{fid}",
                f"定向验证已修复的 {fid}",
                f"{fid}（{severity}）已标记 fixed 但尚未验证：{summary}。"
                "只运行能判定该缺陷是否消除的定向测试/检查，登记命令输出或日志为"
                "证据，再用 `finding --id %s --status verified --evidence <E>` 标记。"
                "不需要为此重新派发完整 Reviewer。若问题仍复现，标记 regressed 并继续修复。" % fid,
                "定向测试/检查的真实输出与 `finding` verified 回执；或仍复现的复现证据",
                origin="finding-verify")
            continue
        add(f"AUTO-FIX-{fid}", f"修复 {fid}（{severity}）",
            f"闭合复核 finding {fid}：{where} {summary}。先修根因，再用定向测试/证据"
            "证明修复；把修复证据登记为 evidence 后标记 fixed，并运行定向验证。"
            "同类问题请一并检查，不要只修表面位置。",
            "修复后的 diff、定向测试输出与 `finding` fixed/verified 回执",
            origin="finding-fix")

    if not created:
        failing = [a for a in state.get("actions") or []
                   if int(a.get("fail_streak") or 0) >= 3]
        if failing:
            add("AUTO-REPLAN", "重新分析失败根因并选择替代路线",
                "连续失败的工作项：" + ", ".join(str(a.get("id")) for a in failing) +
                "。请分析根因，选择与已失败方案实质不同的替代路线；"
                "不要重复同一失败路径，也不得降低验收标准。",
                "根因分析、已尝试方案、失败原因、替代方案与新的执行结果",
                origin="replan")
        else:
            add("AUTO-NEXT", "明确下一步最小可执行动作",
                "当前没有可领取的工作项，但任务尚未 PASS。请重新核对目标、完成条件、"
                "证据与真实阻塞；能自行推进的，直接执行并回填；确实缺少授权/用户决策/"
                "资源/外部条件时，用 block 记录原因与所需条件。",
                "新的可执行结果与证据，或明确的 block 记录",
                origin="partial-next")
    return created


def _blocked_items_summary(state: dict) -> list[dict]:
    mapping = _item_block_map(state)
    out = []
    for action in _blocked_work_items(state):
        aid = str(action.get("id") or "")
        block = mapping.get(aid) or {}
        out.append({
            "id": aid,
            "title": str(action.get("title") or ""),
            "block_id": str(block.get("block_id") or ""),
            "reason": str(block.get("reason") or ""),
            "needs": str(block.get("needs") or ""),
        })
    return out


def _dependency_blocked_ids(state: dict) -> set[str]:
    """项级阻塞 + 因依赖被阻塞工作项而无法执行的下游工作项。"""
    blocked = set(_item_block_map(state))
    actions = {str(a.get("id") or ""): a for a in _tracked_actions(state)}
    changed = True
    while changed:
        changed = False
        for aid, action in actions.items():
            if aid in blocked:
                continue
            if str(action.get("status") or "") in TERMINAL:
                continue
            deps = [str(d) for d in action.get("depends_on") or []]
            if any(dep in blocked for dep in deps):
                blocked.add(aid)
                changed = True
    return blocked


def _all_remaining_blocked(state: dict) -> bool:
    """剩余活动工作项全部受阻（直接阻塞或其依赖被阻塞），没有可执行工作。"""
    blocked_ids = _dependency_blocked_ids(state)
    active = [a for a in _tracked_actions(state)
              if str(a.get("status") or "") not in TERMINAL]
    return bool(active) and all(str(a.get("id") or "") in blocked_ids
                                for a in active)


def _epoch(value: object) -> float | None:
    """ISO 时间戳 → epoch；无时区的历史值按 UTC 处理，保证可比较。"""
    stamp = _parse_time(value)
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _progress_snapshot(state: dict) -> dict:
    """重开时的进展基线：用计数而不是时间戳，避免同秒内重开/推进被判成“有进展”。"""
    return {
        "evidence": len([item for item in state.get("evidence") or []
                         if isinstance(item, dict)]),
        "terminal_actions": sorted(
            str(item.get("id") or "") for item in _tracked_actions(state)
            if str(item.get("status") or "") in TERMINAL),
        "criteria": {str(item.get("criterion_id") or ""):
                     str(item.get("status") or "")
                     for item in ((state.get("contract") or {}).get("criteria") or [])
                     if isinstance(item, dict)},
    }


def _reopened_without_progress(state: dict) -> bool:
    """最近一次 reopen 之后是否产生了新的实质进展。

    重开表示用户认为旧结论已不成立；如果之后既没有新证据、没有新的工作项结果，
    也没有条件重新核销，那么“条件与门禁仍全部通过”只能用旧结论解释，不能据此
    再次 PASS（否则 reopen → 立即 PASS，等于绕过重开）。任一进展即放行。
    """
    history = (state.get("closure") or {}).get("reopen_history") or []
    if not history:
        return False
    entry = history[-1] if isinstance(history[-1], dict) else {}
    baseline = entry.get("baseline")
    if isinstance(baseline, dict):
        current = _progress_snapshot(state)
        if current["evidence"] > int(baseline.get("evidence") or 0):
            return False
        if set(current["terminal_actions"]) - set(
                baseline.get("terminal_actions") or []):
            return False
        if current["criteria"] != (baseline.get("criteria") or {}):
            return False
        return True
    # 旧状态没有基线：退回时间戳比较，但要求严格晚于重开时刻。
    since = _epoch(entry.get("at"))
    if since is None:
        return False
    stamps: list[object] = []
    for item in state.get("evidence") or []:
        if isinstance(item, dict):
            stamps.append(item.get("captured_at"))
    for item in _tracked_actions(state):
        if str(item.get("status") or "") in TERMINAL:
            stamps.append(item.get("updated"))
    for item in ((state.get("contract") or {}).get("criteria") or []):
        if isinstance(item, dict):
            stamps.append(item.get("updated_at"))
    for value in stamps:
        when = _epoch(value)
        if when is not None and when > since:
            return False
    return True


def _evaluate_core(state: dict, *, cache: dict | None = None) -> dict:
    """本地阶段求值；返回 phase、work、gate、next_commands 与说明。"""
    if (state.get("closure") or {}).get("closed") is True:
        return {
            "phase": "CLOSED", "reason": "任务已收口", "actions": [],
            "stalled": [], "gate": (state.get("closure") or {}).get("gate"),
            "criteria": _criteria_summary(state),
            "next_commands": ["任务已收口；如需继续，先由用户授权 reopen。"],
        }
    task_blocks = _task_blocks(state)
    if task_blocks:
        block = task_blocks[0]
        return {
            "phase": "BLOCKED",
            "reason": block.get("reason") or "存在未解除的任务级阻塞",
            "needs": block.get("needs") or "",
            "block_id": block.get("block_id"),
            "blocked_items": _blocked_items_summary(state),
            "actions": [], "stalled": [], "gate": None,
            "criteria": _criteria_summary(state),
            "next_commands": [
                "copilot_dispatch.py unblock --state <state> --reason <依据>",
                "或由用户补齐所需授权/决策/资源后解除阻塞",
                "提示：只缺单项授权时应当用 block --work-item 登记，"
                "不要用任务级阻塞冻结其余工作",
            ],
        }

    ready = ready_actions(state)
    stalled = stalled_actions(state)
    criteria = _criteria_summary(state)
    contract = state.get("contract") or {}
    if not contract.get("criteria") or contract.get("criteria_state") != "confirmed":
        return {
            "phase": "PARTIAL", "reason": "合同缺少已确认的完成条件",
            "actions": [], "stalled": [], "gate": None, "criteria": criteria,
            "next_commands": ["copilot_dispatch.py contract --state <state> --amend "
                              "--criteria-file <criteria.json>"],
        }
    if ready:
        return {
            "phase": "RUNNING", "reason": "存在可领取的工作项",
            "actions": [a.get("id") for a in ready], "stalled": [],
            "blocked_items": _blocked_items_summary(state),
            "gate": None, "criteria": criteria,
            "next_commands": ["copilot_auto.py drive --state <state>",
                              "或 copilot_dispatch.py next --state <state>"],
        }
    blocked_items = _blocked_items_summary(state)
    unmet = _unmet_criteria(state)
    blocked_cids = _blocked_criteria(state)
    open_unmet = [c for c in unmet
                  if str(c.get("criterion_id") or "") not in blocked_cids]
    if blocked_items and _all_remaining_blocked(state) and not open_unmet:
        # 只剩被项级阻塞的工作、且没有被卡住的未闭合条件：这才是真正的整体
        # BLOCKED。还有可推进的未闭合条件时先派生 AUTO-C，宁可继续也不提前停。
        direct = {item["id"] for item in blocked_items}
        downstream = sorted(_dependency_blocked_ids(state) - direct)
        needs = "；".join(f"{item['id']}: {item['needs']}" for item in blocked_items)
        return {
            "phase": "BLOCKED",
            "reason": "所有剩余工作均受阻：" + "、".join(
                item["id"] for item in blocked_items)
                + (f"（下游依赖无法开始：{'、'.join(downstream)}）"
                   if downstream else ""),
            "needs": needs,
            "block_id": blocked_items[0]["block_id"],
            "blocked_items": blocked_items,
            "dependency_blocked": downstream,
            "actions": [], "stalled": [], "gate": None, "criteria": criteria,
            "next_commands": [
                "copilot_dispatch.py unblock --state <state> --block-id <id> "
                "--reason <依据>",
                "或由用户补齐所需授权/决策/资源后逐条解除阻塞",
            ],
        }
    if open_unmet:
        open_cids = [str(c.get("criterion_id") or "") for c in open_unmet]
        all_blocked = _all_remaining_blocked(state)
        if all_blocked:
            # 剩余工作已全部阻塞，但仍有条件没有归属到任何阻塞项。这不是“可执行
            # 工作”，而是归属缺口：必须用 block --criterion 把条件挂到承载它的
            # 工作项上，否则每一轮都会派生一条执行不了的 AUTO-C。
            reason = ("剩余工作全部受阻，但完成条件 " + "、".join(open_cids)
                      + " 尚未归属任何阻塞项")
            next_commands = [
                "copilot_dispatch.py block --state <state> --work-item "
                + (blocked_items[0]["id"] if blocked_items else "<承载该条件的已阻塞工作项>")
                + " --criterion " + open_cids[0]
                + " --reason <为何该条件由该项承载> --needs <解除条件>",
                "若该条件其实另有可执行路线，先解除对应对的阻塞再执行，不要直接标 met",
            ]
        else:
            reason = "仍有未闭合完成条件"
            next_commands = ["copilot_auto.py drive --state <state>（本地派生下一步并退出 EXECUTE）"]
        return {
            "phase": "PARTIAL", "reason": reason,
            "actions": [], "stalled": [{"id": a.get("id"), "reason": why}
                                       for a, why in stalled],
            "blocked_items": blocked_items,
            "unattributed_criteria": open_cids,
            "gate": None, "criteria": criteria,
            "next_commands": next_commands,
        }
    if stalled:
        return {
            "phase": "PARTIAL", "reason": "存在停摆工作项",
            "actions": [], "stalled": [{"id": a.get("id"), "reason": why}
                                       for a, why in stalled],
            "blocked_items": blocked_items,
            "gate": None, "criteria": criteria,
            "next_commands": ["copilot_auto.py drive --state <state>（本地派生恢复工作项）"],
        }
    check = closure_check(state, cache=cache)
    if check["passed"] and not _reopened_without_progress(state):
        quality = quality_summary(state, cache=cache)
        reason = "完成条件与 G1–G3 全部通过"
        if quality.get("verdict") == PWW:
            reason = ("G1–G3 通过，质量结论为已接受的 PASS_WITH_WARNINGS（"
                      + "、".join(quality.get("accepted_ids") or []) + "）")
        commands = ["copilot_auto.py drive --state <state>（收口）",
                    "或 copilot_dispatch.py closure --state <state> --close"]
        return {
            "phase": "PASS", "reason": reason,
            "actions": [], "stalled": [], "blocked_items": blocked_items,
            "gate": check, "criteria": criteria,
            "next_commands": commands,
        }
    if check["passed"]:
        # 重开后条件与门禁仍全部通过，但还没有任何新进展：旧 PASS 属于重开前的
        # 结论，不能原样复用。要求先产生新的证据/工作项结果/条件重核销再收口。
        return {
            "phase": "PARTIAL", "reason": "任务重开后尚未产生新的实质进展：旧 PASS 不能直接复用",
            "actions": [], "stalled": [], "blocked_items": blocked_items,
            "gate": check, "criteria": criteria,
            "next_commands": ["copilot_auto.py drive --state <state>（先完成重开要求的工作，"
                              "再重新核对条件并收口）"],
        }
    return {
        "phase": "PARTIAL", "reason": "G1–G3 未通过",
        "actions": [], "stalled": [], "blocked_items": blocked_items,
        "gate": check, "criteria": criteria,
        "next_commands": ["copilot_auto.py drive --state <state>（按门禁缺口派生工作项）"],
    }


def _dry_derive_ids(state: dict, *, cache: dict | None = None) -> list[str]:
    """在状态副本上试派生，返回 derive_work 会创建的工作项 id（不落盘）。"""
    try:
        clone = json.loads(json.dumps(state))
        created = derive_work(clone, cache=cache)
    except (GovernanceError, ValueError, TypeError):
        return []
    return [str(item.get("id") or "") for item in created if item.get("id")]


def continuation(state: dict, *, cache: dict | None = None,
                 evaluation: dict | None = None) -> dict:
    """续跑判定（只读）：任务是否还有必须继续执行的下一步。

    `required=True` 表示本次回填/报告之后还有可执行动作（执行工作项、派生下一步
    或收口），不得把它当作任务终点；`required=False` 只有三种情况：已收口、
    已完成待收口以外无动作（PASS 归入必须收口）、或所有剩余工作真实阻塞。

    判定只使用本地状态，不调用任何模型。
    """
    ev = evaluation if evaluation is not None else evaluate(state, cache=cache)
    phase = str(ev.get("phase") or "")
    base = {
        "phase": phase,
        "required": False,
        "action": "none",
        "executable": [],
        "derivable": [],
        "blocked_items": ev.get("blocked_items") or [],
        "dependency_blocked": ev.get("dependency_blocked") or [],
        "unattributed_criteria": ev.get("unattributed_criteria") or [],
        "claimed_by_others": [],
        "next_command": "",
        "reason": "",
    }
    if phase == "CLOSED":
        base["reason"] = "任务已收口，无需继续"
        return base
    if phase == "BLOCKED":
        blocked = base["blocked_items"]
        base["action"] = "wait"
        base["reason"] = ("所有剩余工作真实阻塞："
                          + "、".join(item["id"] for item in blocked)
                          if blocked else "存在任务级阻塞")
        base["next_command"] = ("copilot_dispatch.py unblock --state <state> "
                                "--block-id <id> --reason <依据>")
        return base
    if phase == "PASS":
        base.update({
            "required": True,
            "action": "close",
            "next_command": "copilot_auto.py drive --state <state>（G1–G3 已通过，立即收口）",
            "reason": "完成条件与 G1–G3 已通过：收口是下一步，不是等待用户",
        })
        return base
    ready = [str(a.get("id") or "") for a in ready_actions(state)]
    if ready:
        base.update({
            "required": True,
            "action": "execute",
            "executable": ready,
            "next_command": "copilot_auto.py drive --state <state>",
            "reason": "存在可领取工作项：继续执行，不要结束回合",
        })
        return base
    # 其它会话的有效领取：先算出来，避免把“对方正在做”误判成本会话可派生。
    mine = claim_owner()
    actions = {str(a.get("id") or ""): a for a in _tracked_actions(state)}
    others = []
    for aid, claim in active_claims(state).items():
        action = actions.get(aid)
        if action is None or str(action.get("status") or "") in TERMINAL:
            continue
        if str(claim.get("owner") or "") == mine:
            continue
        others.append(aid)
    derivable = _dry_derive_ids(state, cache=cache)
    # 只剩通用的 AUTO-NEXT 兜底时，说明具体缺口已被对方领取/阻塞；此时派生
    # AUTO-NEXT 只会让两个会话各自重新规划同一任务，应等待对方回填或租约到期。
    if derivable and not (others and set(derivable) <= {"AUTO-NEXT"}):
        hint = ""
        if base["unattributed_criteria"]:
            hint = ("；条件 " + "、".join(base["unattributed_criteria"]) +
                    " 未归属任何阻塞项：若确由已阻塞工作承载，先用 block --criterion "
                    "归属，再 drive 判断是否整体受阻")
        base.update({
            "required": True,
            "action": "derive",
            "derivable": derivable,
            "next_command": "copilot_auto.py drive --state <state>（派生下一步并执行）",
            "reason": "任务仍处于 PARTIAL 且可派生下一步工作项：继续推进" + hint,
        })
        return base
    # 没有本会话可领取/可派生的下一步，但工作项已被其它会话领取：等待该会话
    # 回填或租约到期。这不是整体 BLOCKED（不需要用户授权），也不是任务终点；
    # 不重复执行同一工作项，也不把租约当成永久锁。
    if others:
        base.update({
            "action": "wait",
            "claimed_by_others": sorted(others),
            "next_command": ("copilot_stop_guard.py status（查看领取方）；或等租约到期后 "
                             "重新 drive"),
            "reason": ("工作项已被其它会话领取：" + "、".join(sorted(others))
                       + "；等待其回填或租约到期，避免并发重复执行"),
        })
        return base
    base.update({
        "required": True,
        "action": "replan",
        "next_command": "copilot_dispatch.py next --state <state>",
        "reason": ("PARTIAL 但没有可直接派生的下一步：必须重新分析路线、补证据或"
                   "登记真实阻塞，不能把无进展当成任务终点"),
    })
    return base



def sol_advice_summary(state: dict, *, gates_passed: bool = False,
                       user_request: bool = False, review_requested: bool = False,
                       triggers: list[str] | None = None) -> dict:
    """SOL 默认认知建议（只读）：任务类型 + 复杂度 + 覆盖 + 新信号，不改变
    阶段与门禁。

    简单任务返回 `action=none`；非简单任务在启动阶段返回 `consult`、在形成现场
    事实后返回 `plan`；已有有效结论且无升级型新信号时返回 `reuse`；`review` 只在
    用户明确要求时给出。不设调用次数上限。
    """
    state_triggers = [str(t.get("name") or "")
                      for t in state.get("admission_triggers") or []
                      if isinstance(t, dict) and t.get("name")]
    return RP.sol_advice(
        state,
        calls=[item for item in iter_sol_calls(state)
               if isinstance(item, dict) and item.get("status") == "completed"],
        triggers=list(triggers or []) + state_triggers,
        gates_passed=gates_passed,
        user_request=user_request,
        review_requested=review_requested,
        workspace=str(state.get("workspace") or ""))


def evaluate(state: dict, *, cache: dict | None = None) -> dict:
    """本地阶段求值；附带非阻断提醒（不影响 phase 与 G1–G3）。"""
    result = _evaluate_core(state, cache=cache)
    warnings = contract_source_warnings(state)
    if warnings:
        result["warnings"] = warnings
    phase = result.get("phase")
    if phase in ("RUNNING", "PARTIAL", "PASS"):
        # SOL 建议只是提示：不派生工作项、不改变阶段、不影响门禁。
        advice = sol_advice_summary(state, gates_passed=(phase == "PASS"))
        result["sol"] = advice
        if advice.get("action") in ("consult", "plan", "review", "think"):
            prefix = "可选" if advice.get("recommended_action") == "optional" else "建议"
            command = advice.get("command") or ""
            if command:
                mode = advice.get("mode") or advice.get("action", "").upper()
                result.setdefault("next_commands", []).insert(
                    0, f"{prefix}：{command}（SOL {mode}，advisory）")
    # 续跑判定：忽略它不会改变状态，但阶段报告与工作项回填必须据此继续。
    result["continuation"] = continuation(state, cache=cache, evaluation=result)
    return result


def _criteria_summary(state: dict) -> dict:
    criteria = (state.get("contract") or {}).get("criteria") or []
    return {
        "total": len(criteria),
        "met": sum(1 for c in criteria if str(c.get("status") or "") == "met"),
        "unmet": [str(c.get("criterion_id")) for c in criteria
                  if str(c.get("status") or "") != "met"],
    }


def _assert_open(state: dict, action: str, *, allow_blocked: bool = False) -> None:
    """CLOSED 一律拒绝；BLOCKED 只拒绝改变范围/授权/收口的动作。

    事实性进展（证据、工作项结果、条件核销、报告、判断、正式 REVIEW 登记/修订）
    在 BLOCKED 下允许登记，因为它们不解除阻塞、不绕过 G1–G3，也不改变合同或
    授权；`close_task` 仍单独拒绝未解除的阻塞。破坏性操作（`drop_evidence`）
    与可能改变范围的操作（`plan_state`、`add_decision`）保持拒绝。

    `amend_contract` 是单独的显式合同修订通道，BLOCKED 下仍允许；它会更新合同
    指纹并使旧正式 REVIEW 授权失效，因此不属于本函数的普通写入白名单。
    """
    if bool((state.get("closure") or {}).get("closed")):
        raise GovernanceError(
            f"任务已收口（CLOSED），不能{action}；如需继续请由用户授权 reopen。")
    if not allow_blocked and any(not b.get("resolved_at")
                                 for b in state.get("blocks") or []):
        raise GovernanceError(
            f"任务处于 BLOCKED，不能{action}；先解除阻塞或由用户补齐条件。")


# ── 渲染 / 历史只读 ────────────────────────────────────────────────────────
def render_status(state: dict, evaluation: dict | None = None, *,
                  cache: dict | None = None) -> str:
    ev = evaluation or evaluate(state, cache=cache)
    criteria = ev.get("criteria") or _criteria_summary(state)
    lines = [
        f"任务 {state.get('task_id')} · 阶段 {ev.get('phase')} · "
        f"风险 {state.get('risk')} · 后端 {state.get('execution_backend')}",
        f"目标：{state.get('goal')}",
        f"完成条件：{criteria.get('met')}/{criteria.get('total')} met"
        + (f"；未闭合 {', '.join(criteria.get('unmet') or [])}"
           if criteria.get("unmet") else ""),
    ]
    sol_calls = [item for item in iter_sol_calls(state)
                 if isinstance(item, dict)]
    completed_calls = [item for item in sol_calls
                       if str(item.get("status") or "") == "completed"]
    kind = str(state.get("task_kind") or RP.DEFAULT_TASK_KIND)
    counts = {mode: len([item for item in completed_calls
                         if sol_ref_mode(item) == mode])
              for mode in SOL_MODES}
    accepted = len([item for item in completed_calls
                    if str((item.get("acceptance") or {}).get("status") or "")
                    in ("accepted", "modified")])
    lines.append(
        f"SOL：{kind}（{state.get('task_kind_source') or 'default'}）· "
        f"CONSULT {counts['CONSULT']} / PLAN {counts['PLAN']} / "
        f"REVIEW {counts['REVIEW']} · 采纳 {accepted}"
        + (f" · 失败 {len([i for i in sol_calls if i.get('status') == 'failed'])}"
           if any(i.get("status") == "failed" for i in sol_calls) else ""))
    quality = quality_summary(state, cache=cache)
    if quality.get("verdict") == PWW:
        accepted = "、".join(quality.get("accepted_ids") or []) or "无"
        lines.append(
            f"质量结论：PASS_WITH_WARNINGS（接受状态 {quality.get('acceptance_status')}；"
            f"已接受 {accepted}）报告 {quality.get('report_id')}")
    elif quality.get("verdict") and quality.get("verdict") != "unrecorded":
        lines.append(f"质量结论：{str(quality.get('raw_verdict') or quality.get('verdict')).upper()}"
                     f"（报告 {quality.get('report_id')}）")
    elif bool((state.get("closure") or {}).get("closed")):
        lines.append("质量结论：未登记（历史收口不推断接受状态）")
    actions = state.get("actions") or []
    if actions:
        done = sum(1 for a in actions if str(a.get("status") or "") in TERMINAL)
        lines.append(f"工作项：{done}/{len(actions)} 终态")
        for action in actions:
            lines.append(f"- {action.get('id')} [{action.get('status')}] "
                         f"{action.get('title')}")
    claims = active_claims(state)
    if claims:
        lines.append("调度领取（短租约；不是执行证明）：")
        for aid, claim in sorted(claims.items()):
            handoff = int(claim.get("handoff_count") or 0)
            lines.append(f"- {aid} 由 {claim.get('owner') or '-'} 领取 · "
                         f"到期 {claim.get('expires_at') or '-'} · 第 {handoff} 次交办")
            if handoff >= 3:
                lines.append("  警告：同一工作项已多次交办仍未回填；先确认是否真的受阻，"
                             "必要时 record failed 并换路线（AUTO-REPLAN）。")
    blocks = _active_blocks(state)
    if blocks:
        for block in blocks:
            scope = ("任务级" if not block.get("work_items")
                     else "工作项 " + "、".join(block.get("work_items") or []))
            lines.append(f"阻塞[{scope}]：{block.get('reason')}"
                         f"（需要：{block.get('needs')}）")
    reason = ev.get("gate") or ev.get("reason")
    if ev.get("gate") is not None:
        gate = ev["gate"]
        if isinstance(gate, dict) and "gates" in gate:
            lines.append("三道门：" + " ".join(
                f"{name}={'PASS' if gate['gates'][name].get('passed') else 'FAIL'}"
                for name in ("G1", "G2", "G3")))
            for item in gate.get("reasons") or []:
                lines.append(f"  - {item.get('gate')}:{item.get('code')} {item.get('detail')}")
    elif reason:
        lines.append(f"说明：{reason}")
    warnings = ev.get("warnings") or []
    if warnings:
        lines.append("提醒（不阻断）：")
        lines.extend(f"- {item}" for item in warnings)
    cont = ev.get("continuation") or {}
    if cont:
        if cont.get("required"):
            lines.append(f"续跑：必须继续（{cont.get('action')}）——{cont.get('reason')}")
        elif cont.get("action") == "wait":
            lines.append(f"续跑：等待外部条件——{cont.get('reason')}")
        if cont.get("blocked_items"):
            lines.append("被阻塞工作项：")
            lines.extend(
                f"- {item.get('id')}（{item.get('block_id')}）："
                f"{item.get('reason')}（需要：{item.get('needs')}）"
                for item in cont.get("blocked_items") or [])
        if cont.get("dependency_blocked"):
            lines.append("等待阻塞解除后才能开始：" +
                         "、".join(cont.get("dependency_blocked") or []))
        if cont.get("claimed_by_others"):
            lines.append("由其它会话领取（等待回填/租约到期）：" +
                         "、".join(cont.get("claimed_by_others") or []))
    next_commands = ev.get("next_commands") or []
    if next_commands:
        lines.append("下一步：")
        lines.extend(f"- {cmd}" for cmd in next_commands)
        if cont.get("required"):
            lines.append("- 以上动作未执行前，不得结束回合或以报告收束任务。")
    return "\n".join(lines)


def legacy_status(path: str = "", audit_log: str = "", ledger: str = "") -> dict:
    """只读展示 v1/v2 SOL 历史状态、旧审计与旧 REVIEW 台账。"""
    out: dict = {"engine": "legacy-sol", "retired": True, "state": None,
                 "audit": None, "ledger": None}
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            raise GovernanceError(f"无法读取历史状态 {path}：{exc}") from None
        if not is_legacy_state(data):
            raise GovernanceError(f"{path} 不是 v1/v2 SOL 历史状态。")
        rounds = data.get("rounds") or []
        out["state"] = {
            "path": os.path.abspath(path),
            "version": data.get("version"),
            "task_id": data.get("task_id"),
            "goal": data.get("goal"),
            "mode": data.get("mode"),
            "execution_backend": data.get("execution_backend"),
            "round_seq": data.get("round_seq"),
            "rounds": [{
                "round_id": r.get("round_id"),
                "verdict": r.get("verdict"),
                "decision": r.get("decision"),
                "model_identity_ok": r.get("model_identity_ok"),
                "call_id": r.get("call_id"),
            } for r in rounds if isinstance(r, dict)],
            "pending_call": data.get("pending_call"),
            "quota": data.get("quota"),
            "closure": data.get("closure"),
            "evidence_count": len(data.get("evidence") or []),
            "action_count": len(data.get("actions") or []),
            "note": "v6 不驱动该状态；历史轮次与凭证保留，不得据此继续自动决策。",
        }
    if audit_log:
        out["audit"] = read_legacy_audit(audit_log)
    if ledger:
        out["ledger"] = read_legacy_ledger(ledger)
    return out


def read_legacy_audit(path: str) -> dict:
    """读取旧 call_gpt 审计 JSONL：只统计与列出，不重放请求。"""
    if not os.path.exists(path):
        raise GovernanceError(f"历史审计日志不存在：{path}")
    requests = 0
    results = 0
    models: dict[str, int] = {}
    calls: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
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
                event = str(row.get("event") or "")
                if event == "call_requested":
                    requests += 1
                elif event == "call_result":
                    results += 1
                call = row.get("call") if isinstance(row.get("call"), dict) else row
                model = str(call.get("model_requested") or call.get("model") or "").strip()
                if model:
                    models[model] = models.get(model, 0) + 1
                if len(calls) < 50:
                    calls.append({
                        "event": event,
                        "call_id": call.get("call_id"),
                        "model": model or None,
                        "status": call.get("status"),
                    })
    except OSError as exc:
        raise GovernanceError(f"无法读取历史审计 {path}：{exc}") from None
    return {"path": os.path.abspath(path), "requests": requests, "results": results,
            "models": models, "recent": calls,
            "note": "历史审计只读；v6 不发起 SOL 调用。"}


def read_legacy_ledger(path: str) -> dict:
    """读取旧 GPT REVIEW 台账：只统计轮次与裁决，不重新判断。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise GovernanceError(f"无法读取历史 REVIEW 台账 {path}：{exc}") from None
    if not isinstance(data, dict):
        raise GovernanceError(f"历史 REVIEW 台账顶层必须是对象：{path}")
    rounds = data.get("rounds") or []
    return {
        "path": os.path.abspath(path),
        "subject": data.get("subject"),
        "rounds": [{
            "round_id": r.get("round_id"),
            "verdict": r.get("verdict"),
            "call_id": r.get("call_id"),
            "created": r.get("created"),
        } for r in rounds if isinstance(r, dict)],
        "note": "历史 REVIEW 台账只读；v6 不以其裁决驱动任务。",
    }

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
* 判定：**完全本地**。PASS 由完成条件 + 证据 + G1–G4 机械核销；PARTIAL 表示
  仍有可执行工作；BLOCKED 只表示缺少授权、用户决策、资源或真实外部限制。

本模块不调用任何模型、不发起 HTTP 请求、不读取 Provider/凭据配置。工作项不是
授权：超出用户原始授权、不可逆、生产数据、付费、推送与部署仍需用户明确授权。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import tempfile
from datetime import datetime

import code_scope as CS

SCHEMA = "copilot-governance/v1"
VERSION = 1

RISKS = ("normal", "high")
BACKENDS = ("legacy", "native")
# 新计划只允许 zcode（主代理直接执行）与 deepseek（主代理派发原生子代理）。
# reasonix 仅用于读取历史状态，不再接受为新工作项执行方。
ASSIGNEES = ("zcode", "deepseek")
LEGACY_ASSIGNEES = ("zcode", "deepseek", "reasonix")
STATUSES = ("pending", "running", "done", "failed", "blocked", "dropped")
TERMINAL = {"done", "dropped"}
CRITERION_STATUSES = ("pending", "met", "not_met")
VERDICTS = ("pass", "partial", "blocked")
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
def _default_contract() -> dict:
    return {
        "done_criteria_text": "",
        "boundaries": "",
        "criteria": [],
        "criteria_sha256": "",
        "criteria_state": "draft",
        "captured": None,
        "history": [],
    }


def _default_closure() -> dict:
    return {
        "closed": False,
        "closed_by": None,
        "verdict": None,
        "iteration_id": None,
        "closed_at": None,
        "gate": {"passed": None, "iteration_id": None, "reasons": []},
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
        "risk": "normal",
        "execution_backend": "legacy",
        "created": now_iso(),
        "updated": now_iso(),
        "save_seq": 0,
        "iteration_seq": 0,
        "actions": [],
        "plans": [],
        "iterations": [],
        "judgments": [],
        "blocks": [],
        "evidence": [],
        "reports": [],
        "decisions": [],
        "contract": _default_contract(),
        "native_dispatch": None,
        "native_results": [],
        "native_reviews": [],
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


def log_attempt(state_path: str, kind: str, code: str, message: str,
                envelope: dict | None = None) -> str:
    """把被拒/无法生成的信封尝试追加到任务目录 `review-attempts.jsonl`。

    无效信封是复盘“为什么多轮”的关键证据，但过去只打印到终端。这里只记定位
    字段（时间、类型、错误码、工作项、摘要），不写信封正文或证据内容；失败时
    只打印警告，不影响原流程。生成阶段（`native_agent.py review`）被拒也走同一
    通道，`review-metrics` 的无效信封计数才完整。
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
                          criteria: list[dict]) -> str:
    return _sha256_text(canonical_json({
        "goal": (goal or "").strip(),
        "contract": (contract_text or "").strip(),
        "criteria": criteria_digest(criteria),
    }))


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


def start_state(path: str, goal: str, *, contract: str = "", criteria: object = None,
                risk: str = "normal", execution_backend: str = "legacy",
                task_id: str = "", mode: str = "", note: str = "",
                contract_source: str = "") -> dict:
    if os.path.exists(path):
        raise GovernanceError(f"任务状态已存在：{path}；续跑请直接使用，勿重建。")
    goal = (goal or "").strip()
    if not goal:
        raise GovernanceError("目标为空：任务状态必须绑定目标。")
    task_id = (task_id or "").strip() or "task-" + os.urandom(8).hex()
    if not TASK_ID_RE.match(task_id):
        raise GovernanceError(
            f"task_id={task_id!r} 非法：只允许字母、数字、点、下划线、连字符（≤64）。")
    risk = str(risk or "normal").strip().lower()
    if risk not in RISKS:
        raise GovernanceError(f"risk={risk!r} 非法；只能是 {' / '.join(RISKS)}。")
    backend = str(execution_backend or "legacy").strip().lower()
    if backend not in BACKENDS:
        raise GovernanceError(
            f"execution_backend={execution_backend!r} 非法；只能是 {' / '.join(BACKENDS)}。")
    if risk == "high" and backend != "native":
        # G4 需要的复核信封只能由原生派发链路产生（复核绑定 dispatch 与执行结果）。
        # legacy 后端没有 dispatch，永远无法回填复核，会让高风险任务停在 PARTIAL
        # 直到派生上限。与其留下必然无法收口的任务，不如在 start 时失败关闭。
        raise GovernanceError(
            "risk=high 需要绑定的原生只读复核（G4），而复核信封只能通过 native "
            "派发链路产生；请用 --backend native 创建高风险任务。")
    raw_criteria = criteria if criteria else _criteria_from_text(contract)
    items = _normalize_criteria(raw_criteria)
    # 去掉初始化辅助字段；初始引用在合同确认后由 criterion 显式登记。
    for item in items:
        item.pop("_initial_refs", None)
    if not items:
        raise GovernanceError(
            "完成条件为空：v6 的 PASS 必须逐条核销，请用 --criteria-file 或合同"
            "「## 完成条件」列表给出至少一条条件。")
    state = _default_state()
    state.update({
        "task_id": task_id,
        "goal": goal,
        "goal_sha256": _sha256_text(goal),
        "mode": (mode or "").strip().upper(),
        "note": (note or "").strip(),
        "risk": risk,
        "execution_backend": backend,
        "contract_sha256": _contract_fingerprint(goal, contract, items),
    })
    state["contract"].update({
        "done_criteria_text": (contract or "").strip(),
        "boundaries": _section_text(contract, "边界"),
        "criteria": items,
        "criteria_sha256": criteria_digest(items),
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
                                     "execution_backend": backend})
    save_state(path, state, expect={"absent": True})
    return state


def _history(state: dict, event: str, detail: dict | None = None) -> None:
    state.setdefault("history", []).append({
        "event": str(event), "at": now_iso(),
        "detail": detail or {},
    })


def amend_contract(path: str, *, contract: str = "",
                   criteria: object = None,
                   contract_source: str = "") -> dict:
    """用户/任务范围内修订合同与完成条件；保留同描述条件的既有核销记录。

    BLOCKED 时允许修订：阻塞常常正是因为合同条款不可执行，禁止修订只会逼出
    “先假装解除、改完再重新登记”的无效往返。修订不会解除阻塞，任务仍在
    BLOCKED 直到阻塞被真实解决。
    """
    state = load_state(path)
    _assert_open(state, "修订合同", allow_blocked=True)
    current = state.get("contract") or {}
    body = (contract or current.get("done_criteria_text") or "").strip()
    raw = criteria if criteria else _criteria_from_text(body)
    items = _normalize_criteria(raw)
    if not items:
        raise GovernanceError("修订后完成条件为空；PASS 需要至少一条可核销条件。")
    old = {str(c.get("description") or ""): c
           for c in current.get("criteria") or []}
    for item in items:
        item.pop("_initial_refs", None)
        previous = old.get(item["description"])
        if previous and str(previous.get("status") or "") == "met":
            item["status"] = "met"
            item["evidence_refs"] = previous.get("evidence_refs") or []
            item["updated_at"] = previous.get("updated_at")
            item["updated_iteration"] = previous.get("updated_iteration")
            item["history"] = previous.get("history") or []
    sha = criteria_digest(items)
    state["contract"].update({
        "done_criteria_text": body,
        "boundaries": _section_text(body, "边界") or current.get("boundaries") or "",
        "criteria": items,
        "criteria_sha256": sha,
        "criteria_state": "confirmed",
        "captured": now_iso(),
    })
    state["contract"].setdefault("history", []).append({
        "event": "contract_amended", "at": now_iso(), "by": "zcode",
        "criteria_sha256": sha,
        "criteria": [c["criterion_id"] for c in items],
    })
    state["contract_sha256"] = _contract_fingerprint(
        state.get("goal") or "", body, items)
    source = _contract_source_record(contract_source)
    if source:
        state["contract"]["source"] = source
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
        out.append({
            "id": aid,
            "title": title[:300],
            "assignee": assignee,
            "goal": goal[:4000],
            "constraints": str(item.get("constraints") or "").strip()[:2000],
            "expected_evidence": expected[:2000],
            "depends_on": deps,
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
    plan_sha = _sha256_text(canonical_json({"summary": summary, "actions": actions}))
    for existing in state.get("plans") or []:
        if str(existing.get("sha256") or "") == plan_sha:
            return {"plan": existing, "actions": [], "idempotent": True}
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


def ready_actions(state: dict) -> list[dict]:
    if bool((state.get("closure") or {}).get("closed")):
        return []
    if any(not b.get("resolved_at") for b in state.get("blocks") or []):
        return []
    done = {a["id"] for a in state.get("actions") or []
            if str(a.get("status") or "") == "done"}
    out = []
    for action in state.get("actions") or []:
        if str(action.get("status") or "") != "pending":
            continue
        if all(dep in done for dep in action.get("depends_on") or []):
            out.append(action)
    return out


def stalled_actions(state: dict) -> list[tuple[dict, str]]:
    out: list[tuple[dict, str]] = []
    by_id = {a["id"]: a for a in state.get("actions") or []}
    for action in state.get("actions") or []:
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
            if not os.path.exists(hint):
                raise GovernanceError(f"证据 {eid} 的文件不存在：{hint}")
            try:
                actual = _sha256_file(hint)
            except OSError as exc:
                raise GovernanceError(f"证据 {eid} 无法重读：{exc}") from None
            if actual != expected:
                raise GovernanceError(f"证据 {eid} 的文件摘要与登记不一致；拒绝引用。")
        if eid not in [r["id"] for r in out]:
            out.append({"id": eid, "sha256": expected})
    return out


def record_action(path: str, aid: str, status: str, *, result: str = "",
                  evidence_ids: list[str] | None = None) -> dict:
    state = load_state(path)
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
    if status == "failed":
        action["fail_streak"] = int(action.get("fail_streak") or 0) + 1
    elif status == "done":
        action["fail_streak"] = 0
    _history(state, "action_recorded", {"id": aid, "status": status,
                                        "evidence_ids": action["evidence_ids"]})
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
    _history(state, "action_rerun", {"id": aid, "previous": current,
                                     "reason": (reason or "").strip()[:500]})
    save_state(path, state)
    return action


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
    save_state(path, state)
    return criterion


def _criterion(state: dict, cid: str) -> dict:
    for criterion in (state.get("contract") or {}).get("criteria") or []:
        if str(criterion.get("criterion_id") or "") == (cid or "").strip():
            return criterion
    raise GovernanceError(f"不存在完成条件：{cid}。")


def record_judgment(path: str, status: str, summary: str, *,
                    evidence_ids: list[str] | None = None) -> dict:
    """记录 ZCode 的自我判断；只作审计，不替代本地四门。"""
    state = load_state(path)
    # 判断只作审计，不参与四门；BLOCKED 下保留真实判断记录。
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


def add_block(path: str, reason: str, needs: str) -> dict:
    state = load_state(path)
    # 已有阻塞时仍要允许登记第二个真实阻塞；否则只能改写旧记录或丢失事实。
    _assert_open(state, "记录阻塞", allow_blocked=True)
    reason = (reason or "").strip()
    needs = (needs or "").strip()
    if not reason or not needs:
        raise GovernanceError("BLOCKED 必须同时说明原因（--reason）与所需条件（--needs）。")
    block = {
        "block_id": "B" + _sha256_text(
            canonical_json([state.get("task_id"), len(state.get("blocks") or []),
                            reason]))[:16],
        "reason": reason[:MAX_REPORT_CHARS],
        "needs": needs[:MAX_REPORT_CHARS],
        "created": now_iso(),
        "resolved_at": None,
        "resolved_by": None,
        "resolution": "",
    }
    state.setdefault("blocks", []).append(block)
    _history(state, "block_added", {"block_id": block["block_id"], "reason": reason[:200]})
    save_state(path, state)
    return block


def resolve_block(path: str, reason: str, block_id: str = "") -> dict:
    state = load_state(path)
    if bool((state.get("closure") or {}).get("closed")):
        raise GovernanceError("任务已收口（CLOSED），不能解除阻塞；请先 reopen。")
    reason = (reason or "").strip()
    if not reason:
        raise GovernanceError("解除阻塞必须说明依据（--reason）。")
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
        block["resolved_by"] = "zcode"
        block["resolution"] = reason[:MAX_REPORT_CHARS]
    ids = [str(b.get("block_id") or "") for b in target]
    _history(state, "block_resolved", {"count": len(target), "block_ids": ids,
                                       "reason": reason[:200]})
    save_state(path, state)
    return {"resolved": len(target), "block_ids": ids, "reason": reason}


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
        if not os.path.exists(path):
            raise GovernanceError(f"证据文件不存在：{path}")
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


# ── 四门 / 收口 ────────────────────────────────────────────────────────────
def _gate_actions(state: dict) -> dict:
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


def _gate_criteria(state: dict) -> dict:
    contract = state.get("contract") or {}
    criteria = contract.get("criteria") or []
    reasons = []
    expected = _contract_fingerprint(state.get("goal") or "",
                                     contract.get("done_criteria_text") or "",
                                     criteria)
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
                if not os.path.exists(hint):
                    reasons.append({"code": "evidence_file_missing",
                                    "detail": f"{eid} 证据文件已不存在：{hint}"})
                    continue
                try:
                    if _sha256_file(hint) != str(entry.get("sha256") or ""):
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


def _gate_code_scope(state: dict) -> dict:
    """G2 的代码范围检查：高风险任务的最终 G4 attempt 必须覆盖当前工作区。

    G2 原本只核销合同/条件/证据；未登记为 evidence 的源码变化不会进入
    `final_evidence_digest`。这里把最近一次已消费 attempt 的代码快照纳入 G2，
    保证“G2 看到的范围”与 Reviewer 派发范围、G4 验收范围一致。
    """
    if str(state.get("risk") or "").strip().lower() != "high":
        return {"passed": True, "reasons": []}
    reviews = [r for r in state.get("native_reviews") or []
               if isinstance(r, dict)]
    candidate = None
    for review in reviews:
        # 使用与 G4 相同的“首条合格复核”，而不是无关的最近 attempt。
        ok, _code = _eligible_review(review, state)
        if ok:
            candidate = review
            break
    if candidate is None and reviews:
        # 没有合格复核时仍检查最近一份复核的 attempt，让代码漂移在 G2 可见。
        candidate = max(enumerate(reviews), key=lambda pair: (
            str(pair[1].get("received") or ""), pair[0]))[1]
    attempt = find_review_attempt(
        state, str((candidate or {}).get("review_attempt_id") or ""))
    if attempt is None or not attempt.get("consumed"):
        # 没有可检查的已消费 attempt 时由 G4 报 native_review_missing；这里不重复。
        return {"passed": True, "reasons": []}
    reasons = []
    for code in code_scope_problems(attempt, state, check_current=True):
        detail = {
            "native_review_code_scope_missing":
                "G4 attempt 没有冻结代码变更范围；无法证明 Reviewer 看过未登记"
                "为证据的源码。请用 review-context --create-attempt 重新派发。",
            "native_review_code_scope_unavailable":
                "无法重新读取 G4 attempt 的工作区（不是 git 仓库或 git 不可用）；"
                "代码范围不可证明，按 fail-closed 拒绝。",
            "native_review_code_scope_drift":
                "G4 之后工作区源码/未跟踪文件发生了变化；原复核不再覆盖当前代码。"
                "必须新建 attempt 重做完整复核。",
        }.get(code, code)
        reasons.append({"code": code, "detail": detail})
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
                           # G4 复核工作项本身不进入目标摘要：复核开始时它还处于
                           # pending，结论回填后变 done；若算进去，合法复核会因自身
                           # 状态变化而永远“过期”。其他工作项/证据/条件变化仍会
                           # 改变摘要，使旧复核失效（fail-closed）。
                           if str(a.get("origin") or "") != "g4-review"]),
        "evidence": sorted([[e.get("id"), e.get("sha256"), bool(e.get("dropped"))]
                            for e in state.get("evidence") or []]),
    }))


def _execution_current_problems(state: dict, execution_sha: str,
                               work_item_id: str) -> list[str]:
    """复核绑定的执行结果必须是该工作项当前生效的最近一次结果。

    只看 `execution_result_sha256` 会允许 rerun 前的旧执行结果：旧结果文本与
    当前 action 结果不同，但 `final_evidence_digest` 仍只绑定当前 action，
    Reviewer 实际看到的却是旧结果。这里要求执行记录是最近一次、action 为 done、
    且 action.result 与 execution.summary/result 一致。
    """
    exec_sha = str(execution_sha or "").strip().lower()
    if not exec_sha:
        return []
    records = [r for r in state.get("native_results") or []
               if isinstance(r, dict)
               and str(r.get("work_item_id") or "") == work_item_id]
    if not records:
        return ["native_review_execution_missing"]
    if str(records[-1].get("result_sha256") or "") != exec_sha:
        return ["native_review_execution_superseded"]
    action = next((a for a in state.get("actions") or []
                   if isinstance(a, dict)
                   and str(a.get("id") or "") == work_item_id), None)
    if action is None or str(action.get("status") or "") != "done":
        return ["native_review_execution_not_current"]
    execution = next((r for r in records
                      if str(r.get("result_sha256") or "") == exec_sha), None)
    if execution is None:
        return ["native_review_execution_missing"]
    expected = (str(execution.get("summary") or "") + "\n\n"
                + str(execution.get("result") or "")).strip()[:MAX_RESULT_CHARS]
    if str(action.get("result") or "").strip() != expected:
        return ["native_review_execution_not_current"]
    return []


def _review_criteria_state_digest(state: dict) -> str:
    """复核 attempt 冻结的条件状态：条件 id、状态与证据引用。"""
    return _sha256_text(canonical_json(sorted([
        [c.get("criterion_id"), c.get("status"),
         sorted([[r.get("id"), r.get("sha256")]
                 for r in c.get("evidence_refs") or []])]
        for c in (state.get("contract") or {}).get("criteria") or []
        if isinstance(c, dict)
    ])))


def find_review_attempt(state: dict, attempt_id: str) -> dict | None:
    """按 attempt id 查找已登记的复核派发记录。"""
    target = str(attempt_id or "").strip()
    if not target:
        return None
    for attempt in state.get("review_attempts") or []:
        if isinstance(attempt, dict) and \
                str(attempt.get("review_attempt_id") or "") == target:
            return attempt
    return None


capture_code_snapshot = CS.capture_code_snapshot
code_scope_problems = CS.code_scope_problems
governance_paths = CS.governance_paths
_evidence_paths = CS.evidence_paths


def _review_evidence_snapshot(state: dict) -> list[dict]:
    """派发前固化文件证据内容：复核期间证据文件被改动时必须失败关闭。

    `final_evidence_digest` 绑定的是登记时的摘要；文件在派发后、ingest 前被改写
    不会改变摘要，却会让 Reviewer 实际读到的内容与快照不一致。这里在 attempt
    创建时重读每条 live 文件证据，既要求它仍与登记摘要一致，也把实际摘要写入
    attempt；ingest 与 G4 收口都会再次核对。
    """
    snapshot: list[dict] = []
    for entry in state.get("evidence") or []:
        if not isinstance(entry, dict) or entry.get("dropped"):
            continue
        hint = str(entry.get("path_hint") or "").strip()
        if not hint or hint == "<inline>" or not os.path.isabs(hint):
            continue
        eid = str(entry.get("id") or "")
        registered = str(entry.get("sha256") or "").lower()
        try:
            actual = _sha256_file(hint)
        except OSError as exc:
            raise GovernanceError(
                f"证据 {eid} 无法重读（{hint}）：{exc}；不能创建复核 attempt。") from None
        if registered and actual != registered:
            raise GovernanceError(
                f"证据 {eid} 的文件内容已与登记摘要不一致：{hint}；"
                "先重新登记证据或修正文件，再创建复核 attempt。")
        snapshot.append({"id": eid, "path": hint, "sha256": actual})
    return snapshot


def create_review_attempt(path: str, *, work_item_id: str,
                          execution_result_sha256: str,
                          dispatch_id: str = "", plan_id: str = "",
                          plan_sha256: str = "", review_scope: str = "full",
                          based_on_review_sha256: str = "",
                          evidence_manifest: list[str] | None = None,
                          workspace: str = "", base: str = "") -> dict:
    """在派发 Reviewer 前固化一次不可变的复核 attempt 快照。

    快照由宿主（本函数）按当前状态生成：target 是 `final_evidence_digest`，
    并绑定 task/contract/plan/dispatch/work item/execution result。Reviewer
    会话和信封只能声明该 attempt；旧 session 或旧输出不能创建新的快照。
    """
    state = load_state(path)
    _assert_open(state, "创建复核 attempt")
    wid = str(work_item_id or "").strip()
    exec_sha = str(execution_result_sha256 or "").strip().lower()
    execution = None
    for record in state.get("native_results") or []:
        if isinstance(record, dict) and \
                str(record.get("result_sha256") or "") == exec_sha:
            execution = record
            break
    if execution is None:
        raise GovernanceError(
            f"复核 attempt 绑定的执行结果 {exec_sha[:16] or '-'}… 尚未入账。")
    if str(execution.get("work_item_id") or "") != wid:
        raise GovernanceError("复核 attempt 的 work_item 与执行结果不一致。")
    dispatch = state.get("native_dispatch")
    if not isinstance(dispatch, dict):
        raise GovernanceError("状态没有 native_dispatch；不能创建复核 attempt。")
    did = str(dispatch_id or execution.get("dispatch_id") or "").strip()
    if did != str(execution.get("dispatch_id") or ""):
        raise GovernanceError("复核 attempt 的 dispatch_id 与执行结果不一致。")
    pid = str(plan_id or execution.get("plan_id") or "").strip()
    psha = str(plan_sha256 or execution.get("plan_sha256") or "").strip().lower()
    if pid != str(execution.get("plan_id") or "") or \
            psha != str(execution.get("plan_sha256") or "").lower():
        raise GovernanceError("复核 attempt 的 plan 绑定与执行结果不一致。")
    if pid != str(dispatch.get("plan_id") or "") or \
            psha != str(dispatch.get("plan_sha256") or "").lower():
        raise GovernanceError("复核 attempt 的 plan 绑定与当前 dispatch 不一致。")
    action = next((a for a in dispatch.get("actions") or []
                   if isinstance(a, dict)
                   and str(a.get("work_item_id") or "") == wid), None)
    if action is None or str(action.get("dispatch_id") or "") != did:
        raise GovernanceError("复核 attempt 的 dispatch 工作项不存在或已漂移。")
    execution_problems = _execution_current_problems(state, exec_sha, wid)
    if execution_problems:
        raise GovernanceError(
            "复核 attempt 绑定的执行结果不是该工作项当前生效结果（"
            + ", ".join(execution_problems)
            + "）；请绑定最近一次回填的 execution，不能用 rerun 前的旧结果。")
    scope = str(review_scope or "full").strip().lower()
    if scope not in ("full", "incremental"):
        raise GovernanceError(f"review_scope={scope!r} 非法；只能是 full / incremental。")
    based_on = str(based_on_review_sha256 or "").strip().lower()
    if scope == "incremental" and not based_on:
        raise GovernanceError("incremental 复核 attempt 必须绑定基线复核。")
    manifest = [str(eid).strip() for eid in (evidence_manifest or [])
                if str(eid).strip()]
    code_snapshot = capture_code_snapshot(
        workspace, exclude_paths=_evidence_paths(state) + governance_paths(path),
        base=base)
    evidence_roots = CS.evidence_roots(state)
    if evidence_roots and code_snapshot.get("git_root") and \
            code_snapshot.get("git_root") not in evidence_roots:
        raise GovernanceError(
            "复核 attempt 的 workspace 与任务证据所属仓库不一致：workspace="
            f"{code_snapshot.get('git_root')!r}，证据仓库={evidence_roots}。"
            "请把 --workspace 指向实际任务仓库，避免对错误仓库做 G4。")
    if not code_snapshot.get("available") and str(
            state.get("risk") or "").lower() == "high":
        raise GovernanceError(
            "高风险 G4 复核需要有 git 工作区来证明 Reviewer 审查的代码范围；"
            f"当前 workspace={code_snapshot.get('workspace')!r} 原因 "
            f"{code_snapshot.get('reason') or 'unknown'}。请在任务仓库内运行，"
            "或用 review-context --workspace 指定仓库根。")
    target = final_evidence_digest(state)
    attempt = {
        "review_attempt_id": "RA" + secrets.token_hex(12),
        "task_id": str(state.get("task_id") or ""),
        "contract_sha256": str(state.get("contract_sha256") or ""),
        "plan_id": pid,
        "plan_sha256": psha,
        "dispatch_id": did,
        "work_item_id": wid,
        "execution_result_sha256": exec_sha,
        "target_sha256": target,
        "criteria_sha256": _review_criteria_state_digest(state),
        "code_snapshot": code_snapshot,
        "code_scope_sha256": code_snapshot.get("digest") or "",
        "evidence_snapshot": _review_evidence_snapshot(state),
        "review_scope": scope,
        "based_on_review_sha256": based_on,
        "evidence_manifest": manifest,
        "created_at": now_iso(),
        "created_by": "zcode-native",
        "consumed": False,
        "consumed_by_result_sha256": "",
        "consumed_at": "",
        "reviewer": {},
    }
    state.setdefault("review_attempts", []).append(attempt)
    _history(state, "review_attempt_created", {
        "review_attempt_id": attempt["review_attempt_id"],
        "work_item_id": wid,
        "dispatch_id": did,
        "target_sha256": target,
        "review_scope": scope,
    })
    save_state(path, state)
    return attempt


def review_attempt_problems(review: dict, state: dict, *,
                            check_current: bool = True) -> list[str]:
    """检查复核记录是否属于当前有效 attempt，且快照仍与当前状态一致。"""
    aid = str((review or {}).get("review_attempt_id") or "").strip()
    if not aid:
        return ["native_review_attempt_missing"]
    attempt = find_review_attempt(state, aid)
    if attempt is None:
        return ["native_review_attempt_foreign"]
    problems: list[str] = []
    for field in ("task_id", "contract_sha256", "plan_id", "plan_sha256",
                  "dispatch_id", "work_item_id", "execution_result_sha256"):
        if str(review.get(field) or "") != str(attempt.get(field) or ""):
            problems.append("native_review_attempt_binding_mismatch")
            break
    if str(review.get("review_attempt_created_at") or "") != str(
            attempt.get("created_at") or ""):
        problems.append("native_review_attempt_binding_mismatch")
    if str(review.get("review_scope") or "full").strip().lower() != str(
            attempt.get("review_scope") or "full"):
        problems.append("native_review_attempt_binding_mismatch")
    if sorted(str(x) for x in (review.get("evidence_manifest") or [])) != sorted(
            str(x) for x in (attempt.get("evidence_manifest") or [])):
        problems.append("native_review_attempt_binding_mismatch")
    if str(review.get("target_sha256") or "") != str(
            attempt.get("target_sha256") or ""):
        problems.append("native_review_attempt_target_mismatch")
    if check_current and str(attempt.get("target_sha256") or "") != \
            final_evidence_digest(state):
        problems.append("native_review_stale")
    if check_current and str(attempt.get("criteria_sha256") or "") != \
            _review_criteria_state_digest(state):
        problems.append("native_review_attempt_binding_mismatch")
    # 证据文件在派发后、ingest 前（或 ingest 后、收口前）被改动时，目标摘要
    # 可能完全不变；必须按 attempt 固化的内容快照重新核对。
    for item in attempt.get("evidence_snapshot") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        expected = str(item.get("sha256") or "")
        if not path or not expected:
            continue
        try:
            actual = _sha256_file(path)
        except OSError:
            problems.append("native_review_attempt_workspace_drift")
            break
        if actual != expected:
            problems.append("native_review_attempt_workspace_drift")
            break
    if check_current:
        problems.extend(code_scope_problems(attempt, state, check_current=True))
        problems.extend(_execution_current_problems(
            state, str(attempt.get("execution_result_sha256") or ""),
            str(attempt.get("work_item_id") or "")))
    if attempt.get("consumed"):
        if str(attempt.get("consumed_by_result_sha256") or "") != str(
                review.get("result_sha256") or ""):
            problems.append("native_review_attempt_consumed")
    bound = attempt.get("reviewer") or {}
    reviewer = (review.get("reviewer") or {}) if isinstance(
        review.get("reviewer"), dict) else {}
    for key in ("agent_id", "child_session_id"):
        if str(bound.get(key) or "") and str(bound.get(key) or "") != str(
                reviewer.get(key) or ""):
            problems.append("native_review_attempt_session_mismatch")
    snapshot = attempt.get("code_snapshot") or {}
    expected_root = str(snapshot.get("git_root") or snapshot.get("workspace") or "")
    host = review.get("host")
    host_root = str((host.get("workspace_root") if isinstance(host, dict)
                     else "") or "").strip()
    if expected_root and isinstance(host, dict):
        if not host_root:
            problems.append("native_review_attempt_workspace_missing")
        elif os.path.realpath(host_root) != os.path.realpath(expected_root):
            problems.append("native_review_attempt_workspace_mismatch")
    # 去重但保留首个原因，便于门禁输出稳定。
    out: list[str] = []
    for code in problems:
        if code not in out:
            out.append(code)
    return out


def _review_trust_problems(review: dict, state: dict, *, check_target: bool,
                           target: str = "") -> list[str]:
    """复核记录的可信性检查（可选是否要求绑定当前最终证据）。

    增量复核的基线需要“除过期以外全部可信”：它绑定的是旧的最终证据，不能
    按当前摘要要求它；其余身份、宿主记录与结论条件必须同样成立。
    """
    problems: list[str] = []
    if not isinstance(review, dict):
        return ["native_review_invalid"]
    if str(review.get("task_id") or "") != str(state.get("task_id") or ""):
        problems.append("native_review_foreign")
    if str(review.get("contract_sha256") or "") != str(state.get("contract_sha256") or ""):
        problems.append("native_review_contract_mismatch")
    if str(review.get("verdict") or "").strip().lower() != "pass":
        problems.append("native_review_not_pass")
    findings = review.get("findings") or []
    if any(isinstance(f, dict) and str(f.get("severity") or "") == "blocking"
           for f in findings):
        problems.append("native_review_blocking")
    if review.get("unverified"):
        problems.append("native_review_unverified")
    if review.get("changed_files"):
        problems.append("native_review_changed_files")
    if review.get("g4_eligible") is not True:
        problems.append("native_review_not_g4_eligible")
    else:
        # G4 必须绑定派发前创建的 attempt；仅有一个“生成时算出的 target”
        # 不能证明 Reviewer 实际审查的是同一快照。
        problems.extend(review_attempt_problems(
            review, state, check_current=check_target))
        # ingest 是 attempt 的唯一消费点：未经 ingest 消费的记录不能作为门禁
        # 依据，否则手工写入 native_reviews 就能绕过会话/时间绑定。
        attempt = find_review_attempt(
            state, str(review.get("review_attempt_id") or ""))
        if attempt is not None and not attempt.get("consumed"):
            problems.append("native_review_attempt_not_consumed")
    if check_target:
        expected = target or final_evidence_digest(state)
        bound = str(review.get("target_sha256")
                    or review.get("execution_result_sha256") or "")
        if bound != expected:
            problems.append("native_review_stale")
    reviewer = review.get("reviewer") or {}
    if str(reviewer.get("profile_id") or "") != "reviewer":
        problems.append("native_review_profile_invalid")
    if not str(reviewer.get("agent_id") or "").strip() or not str(
            reviewer.get("child_session_id") or "").strip():
        problems.append("native_review_profile_invalid")
    # G4 收口重新核对宿主记录的绑定，而不是只信任状态里的 g4_eligible 布尔值：
    # 复核记录必须带上 ingest 时从 metadata/rollout 读出的宿主事实，且摘要未变、
    # 与 Reviewer 自报身份一致。首次核对发生在 ingest（verify_review_host_record），
    # 这里保证记录确实来自该路径、并检测记录被改写。
    host = review.get("host")
    if not isinstance(host, dict) or not host:
        problems.append("native_review_host_missing")
    elif str(review.get("host_sha256") or "") != _sha256_text(canonical_json(host)):
        problems.append("native_review_host_changed")
    else:
        for key in ("agent_id", "child_session_id"):
            if str(host.get(key) or "").strip() != str(reviewer.get(key) or "").strip():
                problems.append("native_review_host_identity_mismatch")
        if not host.get("tool_calls"):
            problems.append("native_review_host_no_tool_calls")
    return problems


def _incremental_review_problems(review: dict, state: dict) -> str:
    """增量复核的覆盖复核：基线可信 + 变化量被本轮逐条覆盖。

    返回空串表示通过；否则返回原因码。这里是 ingest 校验的收口期复算，
    不依赖 Reviewer 自述：覆盖快照与实际状态都从本地记录重新计算。
    """
    import native_bridge as NB  # 延迟导入：避免状态引擎与信封层在导入期耦合

    based_on = str(review.get("based_on_review_sha256") or "").strip()
    if not based_on:
        return "native_review_incremental_base_missing"
    base = None
    for record in state.get("native_reviews") or []:
        if isinstance(record, dict) and str(record.get("result_sha256") or "") == based_on:
            base = record
            break
    if base is None:
        return "native_review_incremental_base_missing"
    if _review_trust_problems(base, state, check_target=False):
        return "native_review_incremental_base_invalid"
    base_attempt = find_review_attempt(
        state, str(base.get("review_attempt_id") or ""))
    if code_scope_problems(base_attempt, state, check_current=True):
        return "native_review_incremental_code_changed"
    base_coverage = base.get("coverage")
    if not isinstance(base_coverage, dict) or not base_coverage:
        return "native_review_incremental_base_coverage_missing"
    current = NB.review_coverage(state)
    changed_evidence = {
        eid for eid, sha in current["evidence"].items()
        if (base_coverage.get("evidence") or {}).get(eid) != sha}
    changed_actions = {
        aid for aid, fp in current["actions"].items()
        if (base_coverage.get("actions") or {}).get(aid) != fp}
    changed_criteria = {
        cid for cid, fp in current["criteria"].items()
        if (base_coverage.get("criteria") or {}).get(cid) != fp}
    if changed_criteria:
        return "native_review_incremental_criteria_changed"
    if any(aid != str(review.get("work_item_id") or "") for aid in changed_actions):
        return "native_review_incremental_scope_too_narrow"
    manifest = {str(eid) for eid in review.get("evidence_manifest") or []}
    if changed_evidence - manifest:
        return "native_review_incremental_evidence_uncovered"
    if not changed_evidence and not changed_actions:
        # 与 native_bridge 的增量校验保持同一口径：没有变化就没有可增量覆盖的
        # 对象，应直接复用基线结论而不是生成一条空增量记录。
        return "native_review_incremental_without_change"
    return ""


def _eligible_review(review: dict, state: dict, *,
                     target: str = "") -> tuple[bool, str]:
    """检查一条原生复核记录是否可作为 G4 依据；失败返回原因码。"""
    problems = _review_trust_problems(review, state, check_target=True,
                                      target=target or final_evidence_digest(state))
    if problems:
        return False, problems[0]
    if str(review.get("review_scope") or "full").strip().lower() == "incremental":
        code = _incremental_review_problems(review, state)
        if code:
            return False, code
    return True, ""


def _coverage_drift_detail(review: dict, state: dict) -> str:
    """比较复核记录里的覆盖快照与当前状态，列出目标摘要失效的具体来源。

    只看诊断信息：真正判定仍由摘要相等决定（fail-closed）。没有快照的旧记录
    无法判断变化项，如实说明。
    """
    coverage = review.get("coverage")
    if not isinstance(coverage, dict) or not coverage:
        return "复核记录没有覆盖快照（旧记录），无法列出变化项；请做一次完整复核"
    import native_bridge as NB  # 延迟导入：与 review_coverage 保持同一口径

    current = NB.review_coverage(state)
    parts: list[str] = []
    for key, label in (("criteria", "条件"), ("actions", "工作项"),
                       ("evidence", "证据")):
        before = coverage.get(key)
        if not isinstance(before, dict):
            continue
        now = current.get(key) or {}
        added = sorted(i for i in now if i not in before)
        removed = sorted(i for i in before if i not in now)
        changed = sorted(i for i in now if i in before and before[i] != now[i])
        bits = []
        if added:
            bits.append("新增 " + ", ".join(added))
        if removed:
            bits.append("移除 " + ", ".join(removed))
        if changed:
            bits.append("变更 " + ", ".join(changed))
        if bits:
            parts.append(f"{label}：" + "；".join(bits))
    if not parts:
        return "覆盖快照与当前状态一致；差异来自收口动作或记录改写，请重新复核"
    return "；".join(parts)[:400]


def _review_reason_detail(review: dict, state: dict, code: str) -> str:
    """G4 拒绝原因的可操作说明：说清是哪一条复核、为什么不可用、下一步做什么。"""
    label = f"复核 {review.get('work_item_id') or '-'}"
    if code == "native_review_stale":
        attempt = find_review_attempt(
            state, str(review.get("review_attempt_id") or ""))
        if attempt is not None and str(attempt.get("target_sha256") or "") != \
                final_evidence_digest(state):
            return (f"{label} 不可用（attempt 快照已过期，目标摘要已过期）："
                    f"{_coverage_drift_detail(review, state)}。"
                    "旧 Reviewer 输出不能重新封装为新摘要结论，必须新建 attempt 重做复核。")
        return (f"{label} 不可用（目标摘要已过期）："
                f"{_coverage_drift_detail(review, state)}。"
                "需要基于当前证据做完整复核，或用可用的 G4 基线做增量复核。")
    if code == "native_review_contract_mismatch":
        return (f"{label} 不可用（合同指纹不一致）：复核后合同目标/正文/完成条件"
                "发生过改动，旧结论不再覆盖当前合同；请对新合同做完整复核。")
    if code == "native_review_attempt_missing":
        return (f"{label} 不可用（缺少 review attempt）：旧复核没有派发前快照绑定；"
                "请用 review-context --create-attempt 重新派发并生成新信封。")
    if code == "native_review_attempt_consumed":
        return (f"{label} 不可用（attempt 已被消费）：该 attempt 已绑定另一份复核结果；"
                "重试必须新建 attempt，不能复用旧输出。")
    if code == "native_review_attempt_workspace_missing":
        return (f"{label} 不可用（缺少 Reviewer 工作区）：宿主 metadata 没有 "
                "workspaceRoot，无法证明 Reviewer 在 attempt 冻结的工作区运行。")
    if code == "native_review_attempt_workspace_mismatch":
        return (f"{label} 不可用（Reviewer 工作区不匹配）：Reviewer 实际工作区与 "
                "attempt 冻结的代码根不一致；不能把其他工作区的结论当成本任务复核。")
    if code == "native_review_attempt_session_mismatch":
        return (f"{label} 不可用（session 不匹配）：复核结果不是该 attempt 绑定的 "
                "Reviewer session 产生的；必须用原 session 或新建 attempt。")
    if code == "native_review_execution_superseded":
        return (f"{label} 不可用（绑定的是旧执行结果）：该工作项已 rerun 并有更新的 "
                "execution；旧结果文本与当前 action 不一致。请绑定最近一次 execution 重新复核。")
    if code == "native_review_execution_not_current":
        return (f"{label} 不可用（执行结果与当前 action 不一致）：execution 的 "
                "summary/result 无法对应当前 action.result；不能把旧执行结果当作当前代码。")
    if code == "native_review_execution_missing":
        return (f"{label} 不可用（找不到绑定 execution）：状态中没有该工作项的执行记录。")
    if code == "native_review_code_scope_missing":
        return (f"{label} 不可用（缺少代码范围快照）：旧 attempt 只绑定证据文件，"
                "无法证明 Reviewer 看过未登记为证据的源码；请用 review-context "
                "--create-attempt 重新派发并复核。")
    if code == "native_review_code_scope_unavailable":
        return (f"{label} 不可用（代码范围不可读取）：attempt 的工作区不是 git "
                "仓库或 git 不可用；无法证明实际审查范围，按 fail-closed 拒绝。")
    if code == "native_review_code_scope_drift":
        return (f"{label} 不可用（代码范围已变化）：G4 attempt 之后工作区源码、"
                "未跟踪文件或文件状态发生变化；旧结论不再覆盖当前代码，必须新建 "
                "attempt 重做完整复核。")
    if code == "native_review_attempt_workspace_drift":
        return (f"{label} 不可用（证据文件在复核期间被改动）：attempt 固化的文件 "
                "内容与当前不一致；旧结论不再覆盖当前内容，必须新建 attempt 重做复核。")
    if code == "native_review_attempt_not_consumed":
        return (f"{label} 不可用（attempt 未经 ingest 消费）：复核记录缺少宿主回填 "
                "绑定；请用 copilot_auto.py ingest --review-file 正式回填。")
    if code == "native_review_attempt_foreign":
        return (f"{label} 不可用（attempt 不属于当前任务）：不得把其他任务/其他 "
                "attempt 的复核输出贴到本任务；请在本任务内新建 attempt。")
    if code.startswith("native_review_attempt_"):
        return (f"{label} 不可用（{code}）：attempt 与信封的 task/plan/dispatch/"
                "work item/execution/target 绑定不一致；请重新创建 attempt。")
    return f"{label} 不可用（{code}）"


def _gate_review(state: dict) -> dict:
    risk = str(state.get("risk") or "").strip().lower()
    if risk == "normal":
        return {"passed": True, "reasons": []}
    if risk != "high":
        return {"passed": False, "reasons": [{
            "code": "native_review_missing",
            "detail": "任务 risk 缺失或非法；G4 按 fail-closed 拒绝。"}]}
    reviews = [r for r in state.get("native_reviews") or [] if isinstance(r, dict)]
    target = final_evidence_digest(state)
    if not reviews:
        return {"passed": False, "reasons": [{
            "code": "native_review_missing",
            "detail": "高风险任务需要绑定的原生独立复核；当前没有复核记录。",
            "required_target": target}]}
    reasons = []
    best = None
    for review in reviews:
        ok, code = _eligible_review(review, state, target=target)
        if ok:
            best = review
            break
        reasons.append({"code": code,
                        "detail": _review_reason_detail(review, state, code),
                        "required_target": target})
    if best is not None:
        return {"passed": True, "reasons": [], "review": best}
    return {"passed": False, "reasons": reasons}


_TEMP_EVIDENCE_PREFIXES = ("/tmp/", "/var/tmp/", "/dev/shm/")


def evidence_warnings(state: dict, *, task_dir: str = "") -> list[str]:
    """证据卫生提醒：重复登记、临时目录与未登记文件。

    非阻断：同一文件登记两次、不同时点快照内容相同都可能是刻意保留，四门不应
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


def _finding_keys(review: dict) -> list[tuple]:
    keys = []
    for finding in review.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        keys.append((str(review.get("work_item_id") or ""),
                     str(finding.get("severity") or ""),
                     str(finding.get("file") or ""),
                     finding.get("line"),
                     str(finding.get("summary") or "")))
    return keys


def review_metrics(state: dict, *, task_dir: str = "") -> dict:
    """真实复核效率指标（只读，不改状态）。

    口径说明：
    * 有效复核 = 已回填到 `native_reviews` 的记录；被拒信封只能从任务目录的
      `review-attempts.jsonl` 统计，二者分开计数，不把尝试当轮次。
    * 重复 finding 按 (工作项, severity, file, line, summary) 完全相同计；同一
      根因的多处表现应合并成一条 finding，否则会在这里显形。
    * 重复命令/文件按同一工作项跨复核记录出现次数计；增量复核本应减少这类重复。
    * 正式 REVIEW、原生独立复核、G4 资格分别统计，不混为一个数字。
    """
    reviews = [r for r in state.get("native_reviews") or [] if isinstance(r, dict)]
    reviews.sort(key=lambda r: str(r.get("received") or ""))
    by_item: dict[str, list[dict]] = {}
    for review in reviews:
        wid = str(review.get("work_item_id") or "")
        by_item.setdefault(wid, []).append(review)

    total_findings = 0
    finding_counts: dict[tuple, int] = {}
    blocking_total = 0
    rounds_with_blocking = 0
    scope_counts: dict[str, int] = {}
    g4_reviews = 0
    package_chars: list[int] = []
    command_rounds: dict[str, int] = {}
    file_rounds: dict[str, int] = {}
    for review in reviews:
        scope = str(review.get("review_scope") or "full")
        scope_counts[scope] = scope_counts.get(scope, 0) + 1
        if review.get("g4_eligible") is True:
            g4_reviews += 1
        findings = [f for f in review.get("findings") or [] if isinstance(f, dict)]
        total_findings += len(findings)
        blocking = sum(1 for f in findings
                       if str(f.get("severity") or "") == "blocking")
        blocking_total += blocking
        if blocking:
            rounds_with_blocking += 1
        for key in _finding_keys(review):
            finding_counts[key] = finding_counts.get(key, 0) + 1
        package_chars.append(len(canonical_json({
            k: review.get(k) for k in (
                "findings", "files_inspected", "commands_run", "unverified",
                "residual_risks", "evidence_manifest")})))
        wid = str(review.get("work_item_id") or "")
        for command in set(str(c) for c in review.get("commands_run") or [] if c):
            command_rounds[f"{wid}\0{command}"] = \
                command_rounds.get(f"{wid}\0{command}", 0) + 1
        for path in set(str(p) for p in review.get("files_inspected") or [] if p):
            file_rounds[f"{wid}\0{path}"] = file_rounds.get(f"{wid}\0{path}", 0) + 1

    duplicate_findings = sum(count - 1 for count in finding_counts.values()
                             if count > 1)
    repeated_commands = sorted(key.split("\0", 1)[1]
                               for key, count in command_rounds.items() if count > 1)
    repeated_files = sorted(key.split("\0", 1)[1]
                            for key, count in file_rounds.items() if count > 1)

    items: list[dict] = []
    rounds_to_pass: list[int] = []
    first_round_findings: list[int] = []
    for wid, records in sorted(by_item.items()):
        rounds = len(records)
        first_findings = len(records[0].get("findings") or []) if records else 0
        first_round_findings.append(first_findings)
        pass_round = None
        for idx, record in enumerate(records, 1):
            if str(record.get("verdict") or "").lower() == "pass":
                pass_round = idx
                break
        if pass_round is not None:
            rounds_to_pass.append(pass_round)
        empty_rounds = sum(
            1 for record in records[1:]
            if not (record.get("findings") or [])
            and str(record.get("verdict") or "").lower() != "pass")
        items.append({
            "work_item_id": wid,
            "rounds": rounds,
            "first_round_findings": first_findings,
            "pass_round": pass_round,
            "empty_rounds": empty_rounds,
            "verdicts": [str(r.get("verdict") or "") for r in records],
            "time_to_pass_seconds": (
                _seconds_between(records[0].get("received"),
                                 records[pass_round - 1].get("received"))
                if pass_round else None),
        })

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
    return {
        "schema": "copilot-review-metrics/v1",
        "task_id": str(state.get("task_id") or ""),
        "risk": str(state.get("risk") or ""),
        "execution_backend": str(state.get("execution_backend") or ""),
        "closed": bool(closure.get("closed")),
        "native_reviews_total": len(reviews),
        "g4_reviews": g4_reviews,
        "formal_reviews_total": len(formal_records),
        "formal_review_amendments_total": formal_amendments,
        "superseded_formal_reviews_total": len(superseded_ids),
        "active_formal_reviews_total": len(formal_records) - len(superseded_ids),
        "invalid_envelopes": invalid_attempts,
        "invalid_envelope_codes": dict(sorted(attempt_codes.items())),
        "review_scopes": dict(sorted(scope_counts.items())),
        "work_items": items,
        "work_items_with_reviews": len(items),
        "avg_review_rounds": (round(sum(i["rounds"] for i in items) / len(items), 2)
                              if items else 0),
        "avg_first_round_findings": (
            round(sum(first_round_findings) / len(first_round_findings), 2)
            if first_round_findings else 0),
        "avg_rounds_to_pass": (
            round(sum(rounds_to_pass) / len(rounds_to_pass), 2)
            if rounds_to_pass else None),
        "findings_total": total_findings,
        "blocking_findings_total": blocking_total,
        "rounds_with_blocking": rounds_with_blocking,
        "duplicate_findings": duplicate_findings,
        "duplicate_finding_ratio": (
            round(duplicate_findings / total_findings, 3)
            if total_findings else 0),
        "repeated_commands": repeated_commands[:50],
        "repeated_command_count": len(repeated_commands),
        "repeated_files": repeated_files[:50],
        "repeated_file_count": len(repeated_files),
        "avg_package_chars": (
            round(sum(package_chars) / len(package_chars)) if package_chars else 0),
        "max_package_chars": max(package_chars) if package_chars else 0,
        "task_seconds": _seconds_between(
            state.get("created"), closure.get("closed_at") or state.get("updated")),
        "reopen_count": len(closure.get("reopen_history") or []),
        "note": "有效复核按 native_reviews 统计；被拒信封按 review-attempts.jsonl "
                "统计。正式 REVIEW、G4 与原生复核分别计数，不互相替代；"
                "正式 REVIEW 的 amendment 与 supersede 单独统计，不重复计为新的 PASS。",
    }


def closure_check(state: dict, *, task_dir: str = "") -> dict:
    criteria = _gate_criteria(state)
    code_scope = _gate_code_scope(state)
    risks = {
        "G1": _gate_actions(state),
        "G2": {"passed": criteria["passed"] and code_scope["passed"],
               "reasons": criteria["reasons"] + code_scope["reasons"]},
        "G3": _gate_stalled(state),
        "G4": _gate_review(state),
    }
    reasons = []
    for name in ("G1", "G2", "G3", "G4"):
        for reason in risks[name]["reasons"]:
            reasons.append({"gate": name, **reason})
    return {"passed": not reasons, "iteration_id": _current_iteration_id(state),
            "gates": risks, "reasons": reasons,
            "warnings": contract_source_warnings(state)
            + evidence_warnings(state, task_dir=task_dir)}


def close_task(path: str, *, by: str = "zcode-native") -> dict:
    state = load_state(path)
    if bool((state.get("closure") or {}).get("closed")):
        return state["closure"]
    if any(not b.get("resolved_at") for b in state.get("blocks") or []):
        raise GovernanceError("存在未解除的阻塞：先 unblock 再收口。")
    check = closure_check(state, task_dir=os.path.dirname(os.path.abspath(path)))
    if not check["passed"]:
        summary = "; ".join(f"{r.get('gate')}:{r.get('code')}" for r in check["reasons"])
        raise GovernanceError(f"四门未通过，不能收口：{summary}")
    state["closure"].update({
        "closed": True,
        "closed_by": by,
        "verdict": "pass",
        "iteration_id": _current_iteration_id(state),
        "closed_at": now_iso(),
        "gate": {"passed": True, "iteration_id": _current_iteration_id(state),
                 "reasons": []},
    })
    _history(state, "task_closed", {"verdict": "pass", "by": by})
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
        "iteration_id": None,
        "closed_at": None,
        "gate": {"passed": None, "iteration_id": None, "reasons": []},
    })
    state["closure"].setdefault("reopen_history", []).append({
        "reason": reason, "at": now_iso(), "authorized_by": authorized_by,
        "previous": old,
    })
    # 重开后任务重新进入活动状态：上一轮已随收口标为 CLOSED，不能改回 RUNNING
    # （会出现“同一轮既已收口又在进行”）。开一条新迭代承接后续工作。
    begin_iteration(state, note=f"重开：{reason[:80]}")
    _history(state, "task_reopened", {"reason": reason[:200],
                                      "authorized_by": authorized_by})
    save_state(path, state)
    return state["closure"]


# ── 阶段求值 / PARTIAL 推进 ────────────────────────────────────────────────
def _unmet_criteria(state: dict) -> list[dict]:
    return [c for c in (state.get("contract") or {}).get("criteria") or []
            if str(c.get("status") or "pending") != "met"]


def _active_blocks(state: dict) -> list[dict]:
    return [b for b in state.get("blocks") or [] if not b.get("resolved_at")]


def _derive_id(state: dict, base: str) -> str:
    used = {str(a.get("id") or "") for a in state.get("actions") or []}
    if base not in used:
        return base
    for index in range(2, 100):
        candidate = f"{base}-R{index}"
        if candidate not in used:
            return candidate
    raise GovernanceError(f"派生工作项 id 已用尽：{base}")


def derive_work(state: dict, *, reason: str = "partial") -> list[dict]:
    """按未闭合条件 / 停摆项 / G4 缺口本地派生工作项；不调用任何模型。

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
        # AUTO-G4 与 AUTO-G4-INGEST 是同一条 G4 缺口的两种工作项形态，累计尝试次数
        # 时按家族合并；否则反复回填失败会一直派生 INGEST 而不升级到替代路线。
        prefix = "AUTO-G4" if base_id.startswith("AUTO-G4") else base_id
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
        attempts = previous_attempts(base_id) if base_id.startswith(
            ("AUTO-C", "AUTO-R-", "AUTO-G4")) else []
        if len(attempts) >= 3 and not any(
                str(a.get("id") or "") == "AUTO-REPLAN"
                and str(a.get("status") or "") in ("pending", "running")
                for a in state.get("actions") or []):
            replan = {
                "id": _derive_id(state, "AUTO-REPLAN"),
                "title": "无进展：重新分析根因并选择替代路线",
                "assignee": "zcode",
                "goal": (f"{base_id} 已派生 {len(attempts)} 次仍未闭合。已尝试："
                         + "；".join(attempts) +
                         "。请分析根因，选择与已失败方案实质不同的替代路线；"
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
    for criterion in unmet[:MAX_DERIVED_PER_CALL]:
        cid = str(criterion.get("criterion_id") or "")
        add(f"AUTO-{cid}", f"闭合完成条件 {cid}",
            f"使完成条件 {cid}「{criterion.get('description') or ''}」闭合："
            "执行所需工作，登记覆盖该条件的原始证据，并用 criterion 记录 met。",
            f"覆盖 {cid} 的原始产物（命令输出 / 文件内容 / 日志 / diff / 测试结果），"
            "登记为证据后引用")

    stalled = stalled_actions(state)
    for action, why in stalled[:MAX_DERIVED_PER_CALL]:
        if len(created) >= MAX_DERIVED_PER_CALL:
            break
        aid = str(action.get("id") or "")
        # 失败/停摆的 AUTO-C 工作项已由未闭合条件分支重新派生（AUTO-C1-R2），
        # 不再额外派生一条恢复项，避免同一缺口出现两份工作单。
        if aid.startswith("AUTO-C"):
            continue
        add(f"AUTO-R-{aid}", f"处理停摆工作项 {aid}",
            f"处理工作项 {aid}（{why}）：分析失败根因，重试、调整方案或说明真实阻塞；"
            "不得机械重复同一失败路径。",
            "失败原因分析 + 新的执行结果/证据；若确属外部阻塞，用 block 记录原因与所需条件",
            origin="recovery")

    check = closure_check(state)
    g4_reasons = [r for r in check.get("reasons") or [] if r.get("gate") == "G4"]
    if g4_reasons and str(state.get("risk") or "").lower() == "high":
        done_g4 = [a for a in state.get("actions") or []
                   if str(a.get("origin") or "") == "g4-review"
                   and str(a.get("status") or "") == "done"]
        if done_g4:
            # 复核做过但 G4 仍未通过：多半是信封未回填、绑定过期或被拒绝。
            add("AUTO-G4-INGEST", "回填或重新生成 G4 复核信封",
                "已有标记完成的 G4 复核工作项，但门禁仍缺可用复核记录。请检查："
                "复核信封是否已用 ingest 回填、是否绑定当前最终证据摘要、是否带有效的 "
                "review_attempt_id（未消费、目标未过期、Reviewer 会话晚于 attempt "
                "且 prompt 含该 id）、是否因 blocking/unverified/changed_files 被拒绝。"
                "旧版无 attempt 的 PASS 不能复用；绑定过期或已消费时必须新建 attempt "
                "并重新复核，不得把旧结论当作当前通过的证据。",
                "回填成功的复核记录（g4_eligible=true、review_attempt_id 已消费且 "
                "target_sha256 等于当前摘要）或新的复核信封与拒绝原因分析",
                origin="g4-review")
        else:
            add("AUTO-G4", "完成高风险 G4 原生独立复核",
                "先把所有完成条件核销、证据登记完整并确认摘要稳定，再做一次最终独立只读"
                "复核：复核真实文件、diff、测试日志与原始证据，绑定 task/contract/"
                "work_item/execution_result_sha256；派发前用 review-context "
                "--create-attempt 固化快照并把 review_attempt_id 写入 Reviewer prompt，"
                "用 native_agent.py review --attempt 生成信封，再用 ingest 回填。若已有"
                "可用基线复核（PASS、g4_eligible、带覆盖快照且通过当前 attempt/宿主/"
                "证据信任检查），用 --based-on <基线复核.json> 做增量复核，只覆盖基线"
                "之后新增/变更的证据；条件、合同或基线信任变化时仍须完整复核。PASS 后"
                "把 minor 记录为 findings、把已核实非缺陷风险记录为 residual_risks，"
                "不要仅为清理它们修改证据并重开完整复核。",
                "复核信封（verdict=pass、无 blocking、unverified 为空、changed_files 为空、"
                "g4_eligible=true、review_attempt_id 有效）与最终证据摘要绑定；"
                "review-context 中 pending criteria 与证据漂移 warnings 为空"
                "（增量复核的旧 PASS warning 需说明处置）",
                origin="g4-review")

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


def _evaluate_core(state: dict) -> dict:
    """本地阶段求值；返回 phase、work、gate、next_commands 与说明。"""
    if bool((state.get("closure") or {}).get("closed")):
        return {
            "phase": "CLOSED", "reason": "任务已收口", "actions": [],
            "stalled": [], "gate": (state.get("closure") or {}).get("gate"),
            "criteria": _criteria_summary(state),
            "next_commands": ["任务已收口；如需继续，先由用户授权 reopen。"],
        }
    blocks = _active_blocks(state)
    if blocks:
        block = blocks[0]
        return {
            "phase": "BLOCKED",
            "reason": block.get("reason") or "存在未解除的阻塞",
            "needs": block.get("needs") or "",
            "block_id": block.get("block_id"),
            "actions": [], "stalled": [], "gate": None,
            "criteria": _criteria_summary(state),
            "next_commands": [
                f"copilot_dispatch.py unblock --state <state> --reason <依据>",
                "或由用户补齐所需授权/决策/资源后解除阻塞",
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
            "gate": None, "criteria": criteria,
            "next_commands": ["copilot_auto.py drive --state <state>",
                              "或 copilot_dispatch.py next --state <state>"],
        }
    unmet = _unmet_criteria(state)
    if unmet:
        return {
            "phase": "PARTIAL", "reason": "仍有未闭合完成条件",
            "actions": [], "stalled": [{"id": a.get("id"), "reason": why}
                                       for a, why in stalled],
            "gate": None, "criteria": criteria,
            "next_commands": ["copilot_auto.py drive --state <state>（本地派生下一步并退出 EXECUTE）"],
        }
    if stalled:
        return {
            "phase": "PARTIAL", "reason": "存在停摆工作项",
            "actions": [], "stalled": [{"id": a.get("id"), "reason": why}
                                       for a, why in stalled],
            "gate": None, "criteria": criteria,
            "next_commands": ["copilot_auto.py drive --state <state>（本地派生恢复工作项）"],
        }
    check = closure_check(state)
    if check["passed"]:
        return {
            "phase": "PASS", "reason": "完成条件与四门全部通过",
            "actions": [], "stalled": [], "gate": check, "criteria": criteria,
            "next_commands": ["copilot_auto.py drive --state <state>（收口）",
                              "或 copilot_dispatch.py closure --state <state> --close"],
        }
    return {
        "phase": "PARTIAL", "reason": "四门未通过",
        "actions": [], "stalled": [], "gate": check, "criteria": criteria,
        "next_commands": ["copilot_auto.py drive --state <state>（按门禁缺口派生工作项）"],
    }


def evaluate(state: dict) -> dict:
    """本地阶段求值；附带非阻断提醒（不影响 phase 与四门）。"""
    result = _evaluate_core(state)
    warnings = contract_source_warnings(state)
    if warnings:
        result["warnings"] = warnings
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
    在 BLOCKED 下允许登记，因为它们不解除阻塞、不绕过 G1–G4，也不改变合同或
    授权；`close_task` 仍单独拒绝未解除的阻塞。破坏性操作（`drop_evidence`）
    与可能改变范围的操作（`plan_state`、`add_decision`）保持拒绝。

    `amend_contract` 是单独的显式合同修订通道，BLOCKED 下仍允许；它会更新合同
    指纹并使旧正式 REVIEW 授权/G4 绑定失效，因此不属于本函数的普通写入白名单。
    """
    if bool((state.get("closure") or {}).get("closed")):
        raise GovernanceError(
            f"任务已收口（CLOSED），不能{action}；如需继续请由用户授权 reopen。")
    if not allow_blocked and any(not b.get("resolved_at")
                                 for b in state.get("blocks") or []):
        raise GovernanceError(
            f"任务处于 BLOCKED，不能{action}；先解除阻塞或由用户补齐条件。")


# ── 渲染 / 历史只读 ────────────────────────────────────────────────────────
def render_status(state: dict, evaluation: dict | None = None) -> str:
    ev = evaluation or evaluate(state)
    criteria = ev.get("criteria") or _criteria_summary(state)
    lines = [
        f"任务 {state.get('task_id')} · 阶段 {ev.get('phase')} · "
        f"风险 {state.get('risk')} · 后端 {state.get('execution_backend')}",
        f"目标：{state.get('goal')}",
        f"完成条件：{criteria.get('met')}/{criteria.get('total')} met"
        + (f"；未闭合 {', '.join(criteria.get('unmet') or [])}"
           if criteria.get("unmet") else ""),
    ]
    actions = state.get("actions") or []
    if actions:
        done = sum(1 for a in actions if str(a.get("status") or "") in TERMINAL)
        lines.append(f"工作项：{done}/{len(actions)} 终态")
        for action in actions:
            lines.append(f"- {action.get('id')} [{action.get('status')}] "
                         f"{action.get('title')}")
    blocks = _active_blocks(state)
    if blocks:
        lines.append(f"阻塞：{blocks[0].get('reason')}（需要：{blocks[0].get('needs')}）")
    reason = ev.get("gate") or ev.get("reason")
    if ev.get("gate") is not None:
        gate = ev["gate"]
        if isinstance(gate, dict) and "gates" in gate:
            lines.append("四门：" + " ".join(
                f"{name}={'PASS' if gate['gates'][name].get('passed') else 'FAIL'}"
                for name in ("G1", "G2", "G3", "G4")))
            for item in gate.get("reasons") or []:
                lines.append(f"  - {item.get('gate')}:{item.get('code')} {item.get('detail')}")
    elif reason:
        lines.append(f"说明：{reason}")
    warnings = ev.get("warnings") or []
    if warnings:
        lines.append("提醒（不阻断）：")
        lines.extend(f"- {item}" for item in warnings)
    next_commands = ev.get("next_commands") or []
    if next_commands:
        lines.append("下一步：")
        lines.extend(f"- {cmd}" for cmd in next_commands)
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

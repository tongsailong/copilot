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
import tempfile
from datetime import datetime

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


def start_state(path: str, goal: str, *, contract: str = "", criteria: object = None,
                risk: str = "normal", execution_backend: str = "legacy",
                task_id: str = "", mode: str = "", note: str = "") -> dict:
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
                   criteria: object = None) -> dict:
    """用户/任务范围内修订合同与完成条件；保留同描述条件的既有核销记录。"""
    state = load_state(path)
    _assert_open(state, "修订合同")
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
    _assert_open(state, "记录正式 REVIEW 授权")
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


def record_formal_review(path: str, *, verdict: str, summary: str,
                         review_ref: str = "", reviewer: str = "native") -> dict:
    """在用户授权范围内登记一次正式 REVIEW 结果（不是自动门禁）。"""
    state = load_state(path)
    _assert_open(state, "登记正式 REVIEW")
    authorization = state.get("formal_review_authorization") or {}
    if not authorization or str(authorization.get("authorized_by") or "") != "user":
        raise GovernanceError(
            "没有用户对正式 REVIEW 的明确授权；普通开发不得伪造正式 REVIEW。")
    if str(authorization.get("contract_sha256") or "") != str(
            state.get("contract_sha256") or ""):
        raise GovernanceError(
            "正式 REVIEW 授权绑定的是旧合同；合同已修订，需要用户重新授权。")
    verdict = str(verdict or "").strip().lower()
    if verdict not in VERDICTS:
        raise GovernanceError(
            f"formal review verdict={verdict!r} 非法；只能是 {' / '.join(VERDICTS)}。")
    summary = (summary or "").strip()
    if not summary:
        raise GovernanceError("正式 REVIEW 必须记录结论说明。")
    entry = {
        "review_id": "FR" + _sha256_text(
            canonical_json([state.get("task_id"), len(state.get("formal_reviews") or []),
                            summary]))[:16],
        "verdict": verdict,
        "summary": summary[:MAX_REPORT_CHARS],
        "review_ref": (review_ref or "").strip()[:1000],
        "reviewer": (reviewer or "native").strip()[:100],
        "authorization": authorization,
        "target_sha256": final_evidence_digest(state),
        "created": now_iso(),
    }
    state.setdefault("formal_reviews", []).append(entry)
    _history(state, "formal_review_recorded", {"verdict": verdict,
                                               "review_id": entry["review_id"]})
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
    _assert_open(state, "回填工作项")
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


def record_criterion(path: str, cid: str, status: str, *,
                     evidence_ids: list[str] | None = None) -> dict:
    state = load_state(path)
    _assert_open(state, "核销完成条件")
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
    _assert_open(state, "记录判断")
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
    _assert_open(state, "记录阻塞")
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


def resolve_block(path: str, reason: str) -> dict:
    state = load_state(path)
    if bool((state.get("closure") or {}).get("closed")):
        raise GovernanceError("任务已收口（CLOSED），不能解除阻塞；请先 reopen。")
    reason = (reason or "").strip()
    if not reason:
        raise GovernanceError("解除阻塞必须说明依据（--reason）。")
    active = [b for b in state.get("blocks") or [] if not b.get("resolved_at")]
    if not active:
        raise GovernanceError("当前没有未解除的阻塞。")
    for block in active:
        block["resolved_at"] = now_iso()
        block["resolved_by"] = "zcode"
        block["resolution"] = reason[:MAX_REPORT_CHARS]
    _history(state, "block_resolved", {"count": len(active), "reason": reason[:200]})
    save_state(path, state)
    return {"resolved": len(active), "reason": reason}


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
    _assert_open(state, "登记报告")
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
    _assert_open(state, "登记证据")
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


def _eligible_review(review: dict, state: dict) -> tuple[bool, str]:
    """检查一条原生复核记录是否可作为 G4 依据；失败返回原因码。"""
    if not isinstance(review, dict):
        return False, "native_review_invalid"
    if str(review.get("task_id") or "") != str(state.get("task_id") or ""):
        return False, "native_review_foreign"
    if str(review.get("contract_sha256") or "") != str(state.get("contract_sha256") or ""):
        return False, "native_review_contract_mismatch"
    if str(review.get("verdict") or "").strip().lower() != "pass":
        return False, "native_review_not_pass"
    findings = review.get("findings") or []
    if any(isinstance(f, dict) and str(f.get("severity") or "") == "blocking"
           for f in findings):
        return False, "native_review_blocking"
    if review.get("unverified"):
        return False, "native_review_unverified"
    if review.get("changed_files"):
        return False, "native_review_changed_files"
    if review.get("g4_eligible") is not True:
        return False, "native_review_not_g4_eligible"
    target = str(review.get("target_sha256") or review.get("execution_result_sha256") or "")
    if target != final_evidence_digest(state):
        return False, "native_review_stale"
    reviewer = review.get("reviewer") or {}
    if str(reviewer.get("profile_id") or "") != "reviewer":
        return False, "native_review_profile_invalid"
    if not str(reviewer.get("agent_id") or "").strip() or not str(
            reviewer.get("child_session_id") or "").strip():
        return False, "native_review_profile_invalid"
    # G4 收口重新核对宿主记录的绑定，而不是只信任状态里的 g4_eligible 布尔值：
    # 复核记录必须带上 ingest 时从 metadata/rollout 读出的宿主事实，且摘要未变、
    # 与 Reviewer 自报身份一致。首次核对发生在 ingest（verify_review_host_record），
    # 这里保证记录确实来自该路径、并检测记录被改写。
    host = review.get("host")
    if not isinstance(host, dict) or not host:
        return False, "native_review_host_missing"
    if str(review.get("host_sha256") or "") != _sha256_text(canonical_json(host)):
        return False, "native_review_host_changed"
    for key in ("agent_id", "child_session_id"):
        if str(host.get(key) or "").strip() != str(reviewer.get(key) or "").strip():
            return False, "native_review_host_identity_mismatch"
    if not host.get("tool_calls"):
        return False, "native_review_host_no_tool_calls"
    return True, ""


def _gate_review(state: dict) -> dict:
    risk = str(state.get("risk") or "").strip().lower()
    if risk == "normal":
        return {"passed": True, "reasons": []}
    if risk != "high":
        return {"passed": False, "reasons": [{
            "code": "native_review_missing",
            "detail": "任务 risk 缺失或非法；G4 按 fail-closed 拒绝。"}]}
    reviews = [r for r in state.get("native_reviews") or [] if isinstance(r, dict)]
    if not reviews:
        return {"passed": False, "reasons": [{
            "code": "native_review_missing",
            "detail": "高风险任务需要绑定的原生独立复核；当前没有复核记录。",
            "required_target": final_evidence_digest(state)}]}
    reasons = []
    best = None
    for review in reviews:
        ok, code = _eligible_review(review, state)
        if ok:
            best = review
            break
        reasons.append({"code": code,
                        "detail": f"复核 {review.get('work_item_id') or '-'} 不可用",
                        "required_target": final_evidence_digest(state)})
    if best is not None:
        return {"passed": True, "reasons": [], "review": best}
    return {"passed": False, "reasons": reasons}


def closure_check(state: dict) -> dict:
    risks = {
        "G1": _gate_actions(state),
        "G2": _gate_criteria(state),
        "G3": _gate_stalled(state),
        "G4": _gate_review(state),
    }
    reasons = []
    for name in ("G1", "G2", "G3", "G4"):
        for reason in risks[name]["reasons"]:
            reasons.append({"gate": name, **reason})
    return {"passed": not reasons, "iteration_id": _current_iteration_id(state),
            "gates": risks, "reasons": reasons}


def close_task(path: str, *, by: str = "zcode-native") -> dict:
    state = load_state(path)
    if bool((state.get("closure") or {}).get("closed")):
        return state["closure"]
    if any(not b.get("resolved_at") for b in state.get("blocks") or []):
        raise GovernanceError("存在未解除的阻塞：先 unblock 再收口。")
    check = closure_check(state)
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
                "复核信封是否已用 ingest 回填、是否绑定当前最终证据摘要、"
                "是否因 blocking/unverified/changed_files 被拒绝。绑定过期时重新复核，"
                "不得把旧结论当作当前通过的证据。",
                "回填成功的复核记录（g4_eligible=true 且 target_sha256 等于当前摘要）"
                "或新的复核信封与拒绝原因分析",
                origin="g4-review")
        else:
            add("AUTO-G4", "完成高风险 G4 原生独立复核",
                "对当前最终证据摘要组织一次独立只读 Reviewer 复核：复核真实文件、diff、"
                "测试日志与原始证据，绑定 task/contract/work_item/execution_result_sha256；"
                "用 native_agent.py review 生成信封，再用 ingest 回填。",
                "复核信封（verdict=pass、无 blocking、unverified 为空、changed_files 为空、"
                "g4_eligible=true）与最终证据摘要绑定",
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


def evaluate(state: dict) -> dict:
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


def _criteria_summary(state: dict) -> dict:
    criteria = (state.get("contract") or {}).get("criteria") or []
    return {
        "total": len(criteria),
        "met": sum(1 for c in criteria if str(c.get("status") or "") == "met"),
        "unmet": [str(c.get("criterion_id")) for c in criteria
                  if str(c.get("status") or "") != "met"],
    }


def _assert_open(state: dict, action: str) -> None:
    if bool((state.get("closure") or {}).get("closed")):
        raise GovernanceError(
            f"任务已收口（CLOSED），不能{action}；如需继续请由用户授权 reopen。")
    if any(not b.get("resolved_at") for b in state.get("blocks") or []):
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

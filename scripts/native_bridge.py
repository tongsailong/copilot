#!/usr/bin/env python3
"""Agent Bridge v2：ZCode 本地计划 → 原生执行 → 治理状态。

本模块只做**协议结构与绑定校验**，不调度子代理、不调用模型、不改任务状态：

* DISPATCH：把 `governance.py` 中已登记的计划工作项交给宿主，绑定
  task / contract / plan（不再绑定任何远程模型轮次）；
* RESULT：执行方回传实际结果、原始证据文件及其 SHA-256、运行元数据。

独立模型复核由认知零工具单轮复核（`cognition_call.py`）承担。v7 及更早的原生复核
信封（`copilot-native-review/*`）与 G4 资格链已随 G4 退役；本模块不再接受或校验
复核信封，历史复核记录只作为状态里的审计数据保留。

安全边界
--------
1. 所有信封都绑定 task_id / contract_sha256 / plan_id / plan_sha256，执行还
   绑定 dispatch_id / work_item_id。
2. 执行结果必须由当前 dispatch 的工作项产生；证据文件必须存在且 SHA-256 一致。
3. 本模块的校验是**本地完整性与绑定校验**，不是密码学签名，也不是宿主可信
   身份证明。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from datetime import datetime

DISPATCH_SCHEMA = "copilot-native-dispatch/v2"
RESULT_SCHEMA = "copilot-native-result/v2"
REVIEW_SCHEMA = "copilot-native-review/v2"
LEGACY_SCHEMAS = ("copilot-native-dispatch/v1", "copilot-native-result/v1",
                  "copilot-native-review/v1")

EXECUTION_STATUSES = ("done", "failed", "blocked", "dropped")
NATIVE_ASSIGNEES = ("zcode", "deepseek")
EXECUTOR_KINDS = ("main", "subagent")
EVIDENCE_ID_RE = re.compile(r"^E[1-9][0-9]{0,3}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class BridgeError(ValueError):
    """可预期的协议/绑定失败。code 供 CLI、测试和审计稳定引用。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)


def _fail(code: str, message: str) -> None:
    raise BridgeError(code, message)


def normalize_profile_policy(snapshot: object) -> dict:
    """把宿主 profileSnapshot 归一化成**有效权限策略**记录（执行方身份元数据）。

    Profile 名称、来源与路径只作为记录事实保留，不参与“是否只读”的判定。
    """
    data = snapshot if isinstance(snapshot, dict) else {}

    def _names(key: str) -> list[str]:
        value = data.get(key)
        return [str(item) for item in value] if isinstance(value, list) else []

    # skills/mcpServers 保留原值：任何非空值（含类型错误的字符串/对象）都算
    # 启用，不能像列表那样被静默归一化成“空”。
    return {
        "name": str(data.get("name") or "").strip(),
        "source": str(data.get("source") or "").strip(),
        "path": str(data.get("path") or "").strip(),
        "tools": _names("tools"),
        "disallowedTools": _names("disallowedTools"),
        "permissionMode": str(data.get("permissionMode") or "").strip(),
        "skills": data.get("skills"),
        "mcpServers": data.get("mcpServers"),
    }


def _profile_policy_or_none(obj: dict) -> dict | None:
    value = obj.get("profile")
    return value if isinstance(value, dict) else None


def _obj(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        _fail("schema_invalid", f"{name} 必须是对象（实际 {type(value).__name__}）。")
    return value


def _list(value: object, name: str) -> list:
    if not isinstance(value, list):
        _fail("schema_invalid", f"{name} 必须是数组（实际 {type(value).__name__}）。")
    return value


def _text(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        _fail("schema_invalid", f"{name} 必须是字符串。")
    text = value.strip()
    if not text and not allow_empty:
        _fail("schema_invalid", f"{name} 不能为空。")
    return text


def _int(value: object, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        _fail("schema_invalid", f"{name} 必须是 >= {minimum} 的整数。")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if not _SHA256_RE.match(text):
        _fail("hash_invalid", f"{name} 必须是 64 位小写 SHA-256。")
    return text


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def envelope_sha256(envelope: dict) -> str:
    """信封内容摘要：排除 result_sha256 自身，防止自指。"""
    body = {k: v for k, v in envelope.items() if k != "result_sha256"}
    return sha256_text(canonical_json(body))


def classify_native_result(envelope: object) -> str:
    """返回 execution；原生复核信封已退役，未知 schema 失败关闭。"""
    obj = _obj(envelope, "native result")
    schema = str(obj.get("schema") or "").strip()
    if schema == RESULT_SCHEMA:
        return "execution"
    if schema == REVIEW_SCHEMA:
        _fail("native_review_retired",
              "原生复核信封已随 G4 退役；独立复核请用 cognition-review（认知零工具单轮）。")
    if schema in LEGACY_SCHEMAS:
        _fail("schema_retired",
              f"{schema} 属于 v1 SOL 通道，已退役；历史记录请用 legacy-status 只读查看。")
    if schema == DISPATCH_SCHEMA:
        _fail("schema_invalid", "dispatch 信封不能作为结果回填。")
    _fail("schema_invalid", f"未知 native result schema：{schema or '-'}。")


def _state_action(state: dict, work_item_id: str) -> dict:
    for action in state.get("actions") or []:
        if isinstance(action, dict) and str(action.get("id") or "") == work_item_id:
            return action
    _fail("work_item_unknown", f"状态中不存在工作项 {work_item_id}。")
    return {}


def _require_state_credential(state: dict) -> tuple[str, str]:
    task_id = str(state.get("task_id") or "").strip()
    contract = str(state.get("contract_sha256") or "").strip()
    if not task_id or not contract:
        _fail("credential_missing",
              "状态缺少 task_id / contract_sha256，不能使用 Agent Bridge。")
    return task_id, contract


def _plan_record(state: dict, plan_id: str) -> dict:
    for plan in state.get("plans") or []:
        if isinstance(plan, dict) and str(plan.get("plan_id") or "") == plan_id:
            return plan
    _fail("plan_unknown", f"状态中没有计划 {plan_id}。")
    return {}


def _plan_payload(state: dict, plan: dict) -> dict:
    """按计划登记顺序重建计划正文，用于重算摘要、发现状态被改写。"""
    by_id = {str(a.get("id") or ""): a for a in state.get("actions") or []
             if isinstance(a, dict)}
    actions = []
    for aid in plan.get("action_ids") or []:
        action = by_id.get(str(aid))
        if not isinstance(action, dict):
            _fail("plan_drift", f"计划 {plan.get('plan_id')} 的工作项 {aid} 已不存在。")
        actions.append({
            "id": str(action.get("id") or ""),
            "title": str(action.get("title") or ""),
            "assignee": str(action.get("assignee") or ""),
            "goal": str(action.get("goal") or ""),
            "constraints": str(action.get("constraints") or ""),
            "expected_evidence": str(action.get("expected_evidence") or ""),
            "depends_on": [str(d) for d in action.get("depends_on") or []],
        })
    return {"summary": str(plan.get("summary") or ""), "actions": actions}


def _plan_sha(state: dict, plan: dict) -> str:
    return sha256_text(canonical_json(_plan_payload(state, plan)))


def _check_plan(state: dict, plan_id: object, plan_sha: object) -> dict:
    pid = _text(plan_id, "plan_id")
    sha = _sha(plan_sha, "plan_sha256")
    plan = _plan_record(state, pid)
    if str(plan.get("sha256") or "") != sha:
        _fail("plan_mismatch", f"计划 {pid} 的摘要与登记不一致。")
    actual = _plan_sha(state, plan)
    if actual != sha:
        _fail("plan_drift",
              f"计划 {pid} 的工作项在登记后被改写；拒绝按漂移后的计划执行。")
    return plan


def _latest_plan(state: dict) -> dict:
    plans = [p for p in state.get("plans") or [] if isinstance(p, dict)]
    if not plans:
        _fail("plan_missing", "状态没有已登记计划；请先 plan 再生成派发。")
    return plans[-1]


def _dispatch_ids(task_id: str, plan_id: str, plan_sha: str, wid: str) -> tuple[str, str]:
    binding = [task_id, plan_id, plan_sha, wid]
    return ("d-" + sha256_text(canonical_json(binding))[:24],
            "ik-" + sha256_text(canonical_json(binding + ["idempotency"]))[:32])


def build_dispatch(state: dict, actions: list[dict], plan: dict | None = None,
                   existing: dict | None = None, *, created_at: str = "") -> dict:
    """生成绑定本地计划的 dispatch 信封。

    `actions` 必须来自 `ready_actions(state)`（或同样经过状态校验的工作项）。
    每个工作项得到稳定且唯一的 `dispatch_id` / `idempotency_key`。
    """
    if state.get("execution_backend") != "native":
        _fail("backend_not_native",
              "只有 execution_backend=native 的任务才能生成原生 dispatch。")
    task_id, contract = _require_state_credential(state)
    plan = plan or _latest_plan(state)
    plan_id = _text(plan.get("plan_id"), "plan.plan_id")
    plan_sha = _sha(plan.get("sha256"), "plan.sha256")
    _check_plan(state, plan_id, plan_sha)
    if not isinstance(actions, list) or not actions:
        _fail("dispatch_empty", "没有可派发的工作项；不能生成空 dispatch。")
    iteration_id = f"I{int(state.get('iteration_seq') or 0)}"

    items: list[dict] = []
    seen: set[str] = set()
    for idx, action in enumerate(actions, 1):
        if not isinstance(action, dict):
            _fail("schema_invalid", f"actions[{idx}] 不是对象。")
        wid = _text(action.get("id"), f"actions[{idx}].id")
        if wid in seen:
            _fail("dispatch_duplicate", f"工作项 {wid} 在同一 dispatch 中重复。")
        seen.add(wid)
        state_action = _state_action(state, wid)
        if str(state_action.get("plan_id") or "") != plan_id:
            _fail("plan_mismatch", f"{wid} 不属于计划 {plan_id}。")
        item_did, idem = _dispatch_ids(task_id, plan_id, plan_sha, wid)
        item = {
            "work_item_id": wid,
            "dispatch_id": item_did,
            "idempotency_key": idem,
            "title": str(action.get("title") or "").strip(),
            "assignee": str(action.get("assignee") or "zcode").strip().lower(),
            "goal": str(action.get("goal") or "").strip(),
            "constraints": str(action.get("constraints") or "").strip(),
            "expected_evidence": str(action.get("expected_evidence") or "").strip(),
            "depends_on": [str(d).strip() for d in (action.get("depends_on") or [])
                           if str(d).strip()],
        }
        if not item["title"]:
            _fail("schema_invalid", f"{wid} 缺少 title。")
        if item["assignee"] not in NATIVE_ASSIGNEES:
            _fail("assignee_unsupported",
                  f"{wid} 的 assignee={item['assignee']!r} 不能走原生后端。")
        items.append(item)
    if isinstance(existing, dict):
        prev = {str(i.get("work_item_id") or ""): i
                for i in existing.get("actions") or [] if isinstance(i, dict)}
        for item in items:
            old = prev.get(item["work_item_id"])
            if isinstance(old, dict) and str(old.get("dispatch_id") or "") not in (
                    "", item["dispatch_id"]):
                _fail("dispatch_id_drift",
                      f"{item['work_item_id']} 既有 dispatch_id 与当前计划绑定不一致；"
                      "拒绝覆盖。")

    batch = "b-" + sha256_text(canonical_json(
        [task_id, plan_id, plan_sha, sorted(i["dispatch_id"] for i in items)]))[:24]
    return {
        "schema": DISPATCH_SCHEMA,
        "dispatch_id": batch,
        "task_id": task_id,
        "contract_sha256": contract,
        "plan_id": plan_id,
        "plan_sha256": plan_sha,
        "iteration_id": iteration_id,
        "created_at": created_at or _now(),
        "actions": items,
    }


def validate_dispatch(envelope: object, state: dict, *,
                      require_state: bool = True) -> dict:
    """校验 dispatch 结构、计划绑定，以及工作项是否被改写。"""
    env = _obj(envelope, "dispatch")
    schema = str(env.get("schema") or "").strip()
    if schema in LEGACY_SCHEMAS:
        _fail("schema_retired",
              f"{schema} 属于 v1 认知通道，已退役；不能用于 v6 派发。")
    if schema != DISPATCH_SCHEMA:
        _fail("schema_invalid", f"dispatch schema 必须是 {DISPATCH_SCHEMA}。")
    task_id, contract = _require_state_credential(state)
    if str(env.get("task_id") or "").strip() != task_id:
        _fail("task_mismatch", "dispatch.task_id 与当前任务不一致。")
    if str(env.get("contract_sha256") or "").strip() != contract:
        _fail("contract_mismatch", "dispatch.contract_sha256 与当前合同不一致。")
    plan = _check_plan(state, env.get("plan_id"), env.get("plan_sha256"))
    plan_id = str(plan["plan_id"])
    plan_sha = str(plan["sha256"])
    _text(env.get("dispatch_id"), "dispatch.dispatch_id")
    _text(env.get("created_at"), "dispatch.created_at", allow_empty=True)
    raw_actions = _list(env.get("actions"), "dispatch.actions")
    if not raw_actions:
        _fail("dispatch_empty", "dispatch.actions 不能为空。")
    seen_wid: set[str] = set()
    seen_did: set[str] = set()
    seen_key: set[str] = set()
    out_actions: list[dict] = []
    for idx, raw in enumerate(raw_actions, 1):
        item = _obj(raw, f"dispatch.actions[{idx}]")
        wid = _text(item.get("work_item_id"), f"dispatch.actions[{idx}].work_item_id")
        did = _text(item.get("dispatch_id"), f"dispatch.actions[{idx}].dispatch_id")
        key = _text(item.get("idempotency_key"),
                    f"dispatch.actions[{idx}].idempotency_key")
        if wid in seen_wid:
            _fail("dispatch_duplicate", f"dispatch 内工作项 {wid} 重复。")
        if did in seen_did:
            _fail("dispatch_duplicate", f"dispatch 内 dispatch_id {did} 重复。")
        if key in seen_key:
            _fail("dispatch_duplicate", f"dispatch 内 idempotency_key {key} 重复。")
        seen_wid.add(wid)
        seen_did.add(did)
        seen_key.add(key)
        action = _state_action(state, wid) if require_state else None
        if require_state:
            if str(action.get("plan_id") or "") != plan_id:
                _fail("plan_mismatch", f"{wid} 不属于计划 {plan_id}。")
        expect_did, expect_key = _dispatch_ids(task_id, plan_id, plan_sha, wid)
        if did != expect_did or key != expect_key:
            _fail("dispatch_id_drift",
                  f"{wid} 的 dispatch_id / idempotency_key 与计划绑定不一致。")
        fields = {
            "title": _text(item.get("title"), f"dispatch.actions[{idx}].title"),
            "assignee": _text(item.get("assignee"),
                              f"dispatch.actions[{idx}].assignee").lower(),
            "goal": _text(item.get("goal"), f"dispatch.actions[{idx}].goal",
                          allow_empty=True),
            "constraints": _text(item.get("constraints"),
                                 f"dispatch.actions[{idx}].constraints",
                                 allow_empty=True),
            "expected_evidence": _text(item.get("expected_evidence"),
                                       f"dispatch.actions[{idx}].expected_evidence",
                                       allow_empty=True),
            "depends_on": [str(d).strip() for d in
                           _list(item.get("depends_on") or [],
                                 f"dispatch.actions[{idx}].depends_on")
                           if str(d).strip()],
        }
        if fields["assignee"] not in NATIVE_ASSIGNEES:
            _fail("assignee_unsupported",
                  f"{wid} 的 assignee={fields['assignee']!r} 不能走原生后端。")
        if require_state:
            for key_name, value in fields.items():
                actual = ([str(d).strip() for d in action.get("depends_on") or []]
                          if key_name == "depends_on"
                          else str(action.get(key_name) or "").strip())
                if actual != value:
                    _fail("dispatch_action_mismatch",
                          f"{wid} 的 {key_name} 与已登记工作项不一致；"
                          "主代理不得改写计划内容。")
        out_actions.append({"work_item_id": wid, "dispatch_id": did,
                            "idempotency_key": key, **fields})
    return {
        "schema": DISPATCH_SCHEMA,
        "dispatch_id": str(env["dispatch_id"]).strip(),
        "task_id": task_id,
        "contract_sha256": contract,
        "plan_id": plan_id,
        "plan_sha256": plan_sha,
        "iteration_id": str(env.get("iteration_id") or "").strip(),
        "created_at": str(env.get("created_at") or ""),
        "actions": out_actions,
    }


def _validate_executor(executor: object, name: str) -> dict:
    obj = _obj(executor, name)
    kind = _text(obj.get("kind"), f"{name}.kind").lower()
    if kind not in EXECUTOR_KINDS:
        _fail("schema_invalid", f"{name}.kind 只能是 {' / '.join(EXECUTOR_KINDS)}。")
    agent_id = str(obj.get("agent_id") or "").strip()
    child = str(obj.get("child_session_id") or "").strip()
    profile = str(obj.get("profile_id") or "").strip()
    model = str(obj.get("model_reported") or "").strip()
    # C2：正式 native 执行必须可追溯到宿主实际运行记录。主代理没有 Agent ID /
    # Child Session ID，无法提供这两项身份，因此不得冒充 native 执行方。
    if kind != "subagent":
        _fail("executor_identity_missing",
              "native 执行结果必须由原生子代理产生（kind=subagent），"
              "并提供 agent_id / child_session_id / profile_id；"
              "主代理没有可核对的 Agent/Child Session 身份。")
    if not agent_id or not child or not profile:
        _fail("executor_identity_missing",
              "子代理执行结果必须提供 agent_id / child_session_id / profile_id。")
    # 执行方 Profile 快照作为运行元数据记录（身份可追溯）；G4 退役后不再用
    # “是否等价于只读复核”作为执行方准入门槛。
    policy = _profile_policy_or_none(obj)
    if policy is None:
        _fail("executor_profile_invalid",
              "执行结果缺少 executor.profile（宿主 profileSnapshot 派生的有效"
              "工具策略）；执行方身份不完整。")
    return {"kind": "subagent", "agent_id": agent_id,
            "child_session_id": child, "profile_id": profile,
            "profile": normalize_profile_policy(policy),
            "model_reported": model or None}


def _validate_timing(timing: object) -> dict:
    obj = _obj(timing, "timing")
    duration = _int(obj.get("duration_ms"), "timing.duration_ms")
    return {
        "started_at": str(obj.get("started_at") or "").strip(),
        "finished_at": str(obj.get("finished_at") or "").strip(),
        "duration_ms": duration,
    }


def _validate_usage(usage: object) -> dict:
    if usage is None:
        return {}
    obj = _obj(usage, "usage")
    out = {}
    for key in ("total_tokens", "input_tokens", "output_tokens", "tool_use_count"):
        if key not in obj:
            continue
        out[key] = _int(obj[key], f"usage.{key}")
    return out


def _validate_evidence(raw: object, state: dict, *, check_files: bool) -> list[dict]:
    items = _list(raw, "evidence")
    if not items:
        _fail("evidence_missing", "执行结果必须至少包含一条原始证据文件。")
    task_id = str(state.get("task_id") or "")
    existing = {str(e.get("id") or ""): e for e in (state.get("evidence") or [])
                if isinstance(e, dict)}
    out: list[dict] = []
    seen: set[str] = set()
    for idx, item in enumerate(items, 1):
        obj = _obj(item, f"evidence[{idx}]")
        eid = _text(obj.get("id"), f"evidence[{idx}].id")
        if not EVIDENCE_ID_RE.match(eid):
            _fail("evidence_invalid", f"evidence[{idx}].id={eid!r} 非法（应形如 E1）。")
        if eid in seen:
            _fail("evidence_duplicate", f"证据 id {eid} 在结果中重复。")
        seen.add(eid)
        path = _text(obj.get("file"), f"evidence[{idx}].file")
        if not os.path.isabs(path):
            _fail("evidence_invalid", f"{eid} 的证据文件必须是绝对路径：{path}")
        digest = _sha(obj.get("sha256"), f"evidence[{idx}].sha256")
        if check_files:
            if not os.path.isfile(path):
                _fail("evidence_missing", f"{eid} 的证据文件不存在或不是普通文件：{path}")
            try:
                actual = sha256_file(path)
            except OSError as exc:
                _fail("evidence_unreadable", f"{eid} 无法读取：{exc}")
            if actual != digest:
                _fail("evidence_sha_mismatch",
                      f"{eid} 的文件 SHA-256 与结果声明不一致（可能被篡改）。")
        old = existing.get(eid)
        if isinstance(old, dict):
            if str(old.get("task_id") or "") != task_id:
                _fail("evidence_foreign", f"证据 {eid} 不属于任务 {task_id}。")
            if str(old.get("sha256") or "") != digest:
                _fail("evidence_conflict",
                      f"证据 {eid} 已登记且摘要不同；不得静默替换，请使用新 id。")
        kind = str(obj.get("kind") or "file").strip()[:64]
        note = str(obj.get("note") or "").strip()[:500]
        out.append({"id": eid, "file": path, "sha256": digest,
                    "kind": kind or "file", "note": note})
    return out


def validate_execution_result(envelope: object, dispatch: dict | None, state: dict, *,
                              check_files: bool = True) -> dict:
    """校验执行结果：计划绑定、结果摘要、证据文件、执行方元数据。"""
    env = _obj(envelope, "execution result")
    schema = str(env.get("schema") or "").strip()
    if schema in LEGACY_SCHEMAS:
        _fail("schema_retired",
              f"{schema} 属于 v1 认知通道，已退役；不能用于 v6 回填。")
    if schema != RESULT_SCHEMA:
        _fail("schema_invalid", f"execution schema 必须是 {RESULT_SCHEMA}。")
    if not isinstance(dispatch, dict):
        _fail("dispatch_missing", "状态没有 native_dispatch；不能回填执行结果。")
    task_id, contract = _require_state_credential(state)
    if str(env.get("task_id") or "").strip() != task_id:
        _fail("task_mismatch", "执行结果 task_id 与当前任务不一致。")
    if str(env.get("contract_sha256") or "").strip() != contract:
        _fail("contract_mismatch", "执行结果 contract_sha256 与当前合同不一致。")
    plan_id = _text(env.get("plan_id"), "execution.plan_id")
    plan_sha = _sha(env.get("plan_sha256"), "execution.plan_sha256")
    if plan_id != str(dispatch.get("plan_id") or ""):
        _fail("plan_mismatch", "执行结果 plan_id 与当前 dispatch 不一致。")
    if plan_sha != str(dispatch.get("plan_sha256") or "").lower():
        _fail("plan_mismatch", "执行结果 plan_sha256 与当前 dispatch 不一致。")
    _check_plan(state, plan_id, plan_sha)
    wid = _text(env.get("work_item_id"), "execution.work_item_id")
    did = _text(env.get("dispatch_id"), "execution.dispatch_id")
    items = {str(i.get("work_item_id") or ""): i
             for i in dispatch.get("actions") or [] if isinstance(i, dict)}
    item = items.get(wid)
    if not isinstance(item, dict):
        _fail("work_item_unknown", f"当前 dispatch 不含工作项 {wid}。")
    if str(item.get("dispatch_id") or "") != did:
        _fail("dispatch_mismatch",
              f"执行结果 dispatch_id={did} 与工作项 {wid} 的派发 ID 不一致。")
    idem_key = _text(env.get("idempotency_key"), "execution.idempotency_key")
    if idem_key != str(item.get("idempotency_key") or ""):
        _fail("idempotency_mismatch",
              f"执行结果 idempotency_key 与工作项 {wid} 的派发键不一致。")
    iteration_id = str(env.get("iteration_id") or "").strip()
    if iteration_id != str(dispatch.get("iteration_id") or ""):
        _fail("iteration_mismatch", "执行结果 iteration_id 与当前 dispatch 不一致。")
    action = _state_action(state, wid)
    if str(action.get("status") or "") in ("done", "dropped"):
        old_shas = {
            str(rec.get("result_sha256") or "")
            for rec in state.get("native_results") or []
            if isinstance(rec, dict) and str(rec.get("work_item_id") or "") == wid
        }
        declared = str(env.get("result_sha256") or "").lower()
        if declared not in old_shas:
            _fail("work_item_terminal",
                  f"{wid} 已终结（{action.get('status')}）；不同结果不得覆盖历史。")
    status = _text(env.get("status"), "execution.status").lower()
    if status not in EXECUTION_STATUSES:
        _fail("invalid_status",
              f"执行结果 status={status!r} 非法；只能是 {', '.join(EXECUTION_STATUSES)}。")
    summary = _text(env.get("summary"), "execution.summary")
    result = _text(env.get("result"), "execution.result")
    executor = _validate_executor(env.get("executor"), "execution.executor")
    timing = _validate_timing(env.get("timing"))
    usage = _validate_usage(env.get("usage"))
    evidence = _validate_evidence(env.get("evidence"), state, check_files=check_files)
    test_commands = [str(item).strip() for item in
                     _list(env.get("test_commands") or [],
                           "execution.test_commands")
                     if str(item).strip()]
    test_artifacts = [str(item).strip() for item in
                      _list(env.get("test_artifacts") or [],
                            "execution.test_artifacts")
                      if str(item).strip()]
    known_evidence = {item["id"] for item in evidence}
    for eid in test_artifacts:
        if eid not in known_evidence:
            _fail("test_artifact_unknown",
                  f"test_artifacts 引用了不存在的证据 {eid}。")
    executor_metadata_path = str(
        env.get("executor_metadata_path") or "").strip()
    if executor_metadata_path:
        if not os.path.isabs(executor_metadata_path) or not os.path.isfile(
                executor_metadata_path):
            _fail("executor_metadata_invalid",
                  "execution.executor_metadata_path 必须是存在的绝对路径。")
    binding = _validate_execution_binding(
        env.get("execution_binding"), state, work_item_id=wid,
        task_id=task_id, test_commands=test_commands)
    if binding:
        # 合同声明的测试层必须在执行结果里真实出现；G4 退役后没有第二个
        # test_execution 模块兜底，不能只靠 binding 上的布尔值。
        if binding.get("require_test_result") and not (
                test_commands or test_artifacts):
            _fail("execution_test_result_missing",
                  "合同要求结构化测试结果，但执行信封没有测试命令或测试产物。")
        if binding.get("require_test_result") and test_commands and not \
                test_artifacts:
            _fail("execution_test_artifact_missing",
                  "声明了测试命令但没有登记 test_artifacts；"
                  "不能证明测试实际产生了可复核结果。")
        if binding.get("require_artifact_integrity") and not test_artifacts:
            _fail("execution_test_artifact_missing",
                  "合同要求测试产物完整性，但执行信封没有登记 test_artifacts。")
        _test_artifact_problems(binding, evidence, test_artifacts)
        _contract_evidence_problems(binding, evidence)
    code_scope = str(env.get("code_scope_sha256") or "").strip().lower()
    if code_scope and not _SHA256_RE.match(code_scope):
        _fail("code_scope_invalid",
              "execution.code_scope_sha256 必须是 64 位十六进制。")
    declared_sha = _sha(env.get("result_sha256"), "execution.result_sha256")
    actual_sha = envelope_sha256(env)
    if declared_sha != actual_sha:
        _fail("result_sha_mismatch",
              "执行结果 result_sha256 与信封内容不一致（可能被篡改）。")
    return {
        "schema": RESULT_SCHEMA,
        "task_id": task_id,
        "contract_sha256": contract,
        "plan_id": plan_id,
        "plan_sha256": plan_sha,
        "iteration_id": iteration_id,
        "dispatch_id": did,
        "idempotency_key": idem_key,
        "work_item_id": wid,
        "status": status,
        "summary": summary,
        "result": result,
        "evidence": evidence,
        "executor": executor,
        "executor_metadata_path": executor_metadata_path,
        "timing": timing,
        "usage": usage,
        "test_commands": test_commands,
        "test_artifacts": sorted(set(test_artifacts)),
        "execution_binding": binding,
        "code_scope_sha256": code_scope,
        "code_scope_excluded_paths": [
            str(item) for item in env.get("code_scope_excluded_paths") or []],
        "result_sha256": declared_sha,
    }


def _binding_epoch(binding: dict) -> float:
    text = str(binding.get("created_at") or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, OSError):
        _fail("execution_binding_invalid",
              "execution binding 缺少可解析的 created_at。")


def _produced_after_binding(path: str, binding: dict) -> bool:
    """产物 mtime 必须在 binding 之后（容忍 2 秒文件系统精度）。"""
    try:
        return os.path.getmtime(path) + 2 >= _binding_epoch(binding)
    except OSError:
        return False


def _test_artifact_problems(binding: dict, evidence: list[dict],
                            test_artifacts: list[str]) -> None:
    """测试产物必须来自本次执行窗口，而不是复用的旧文件。"""
    by_id = {str(item.get("id") or ""): item for item in evidence}
    for eid in test_artifacts:
        entry = by_id.get(eid)
        if entry is None:
            continue  # 已由 test_artifact_unknown 处理
        path = str(entry.get("file") or "")
        if not path or not _produced_after_binding(path, binding):
            _fail("test_artifact_stale",
                  f"测试产物 {eid} 的文件时间早于 execution binding；"
                  "不能证明来自本次执行。")


def _contract_evidence_problems(binding: dict, evidence: list[dict]) -> None:
    """合同要求的报告/产物必须作为本次执行的文件证据出现。

    G4 退役时删除了 `test_execution.py`；该模块原先承担四层测试证据校验。
    这里保留其最关键的完整性条件：路径在工作区内、文件存在、且已在本次信封
    登记（登记时已计算 SHA-256），否则合同测试要求只是文字声明。
    """
    workspace = os.path.realpath(str(binding.get("workspace") or ""))
    files = {os.path.realpath(str(item.get("file")))
             for item in evidence
             if isinstance(item, dict) and item.get("file")}
    required: list[tuple[str, str]] = []
    report = str(binding.get("contract_report") or "").strip()
    if report:
        required.append(("report", report))
    for item in binding.get("contract_artifacts") or []:
        required.append(("artifact", str(item)))
    for kind, raw in required:
        if not raw.strip():
            continue
        path = raw if os.path.isabs(raw) else os.path.join(workspace, raw)
        real = os.path.realpath(path)
        try:
            inside = bool(workspace) and os.path.commonpath(
                [workspace, real]) == workspace
        except ValueError:
            inside = False
        if not inside:
            _fail("contract_artifact_outside_workspace",
                  f"合同 {kind} 路径不在任务工作区内：{raw}")
        if not os.path.isfile(real):
            _fail("contract_artifact_missing",
                  f"合同 {kind} 文件不存在：{raw}")
        if real not in files:
            _fail("contract_artifact_unbound",
                  f"合同 {kind} 未作为本次执行的文件证据登记：{raw}")
        if not _produced_after_binding(real, binding):
            _fail("contract_artifact_stale",
                  f"合同 {kind} 文件时间早于 execution binding：{raw}")


def _validate_execution_binding(raw: object, state: dict, *, work_item_id: str,
                                task_id: str,
                                test_commands: list[str]) -> dict:
    """校验 execution 信封自带的 binding 引用与状态中的绑定一致。

    绑定可以缺席（旧信封兼容），但一旦声明就必须属于本任务/工作项、未被消费，
    且 envelope 的测试命令与绑定的预期命令完全一致。身份时间窗口由
    test_execution 用 metadata.prompt 与 session 时间核验。
    """
    if raw in (None, {}):
        return {}
    obj = _obj(raw, "execution.execution_binding")
    binding_id = _text(obj.get("binding_id"),
                       "execution.execution_binding.binding_id")
    nonce = _text(obj.get("nonce"), "execution.execution_binding.nonce")
    if not re.fullmatch(r"EB[0-9a-f]{24}", binding_id):
        _fail("execution_binding_invalid", f"binding_id 格式非法：{binding_id}")
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        _fail("execution_binding_invalid", f"binding nonce 格式非法：{nonce}")
    stored = next((item for item in state.get("execution_bindings") or []
                   if isinstance(item, dict)
                   and str(item.get("binding_id") or "") == binding_id), None)
    if stored is None:
        _fail("execution_binding_missing",
              f"状态中没有 execution binding {binding_id}。")
    if str(stored.get("nonce") or "") != nonce:
        _fail("execution_binding_nonce_mismatch",
              "execution binding nonce 与状态记录不一致。")
    if stored.get("consumed"):
        _fail("execution_binding_consumed",
              "execution binding 已被其他执行结果消费；不能复用旧 binding。")
    if str(stored.get("task_id") or "") != task_id:
        _fail("execution_binding_cross_task", "execution binding 属于其他任务。")
    contract = str(state.get("contract_sha256") or "")
    if str(stored.get("contract_sha256") or "") != contract:
        _fail("execution_binding_contract_changed",
              "任务合同在 binding 创建后已变化；请重新创建 execution binding。")
    dispatch = state.get("native_dispatch") or {}
    dispatch_id = ""
    for action in dispatch.get("actions") or []:
        if isinstance(action, dict) and str(
                action.get("work_item_id") or "") == work_item_id:
            dispatch_id = str(action.get("dispatch_id") or "")
            break
    if dispatch_id and str(stored.get("dispatch_id") or "") != dispatch_id:
        _fail("execution_binding_dispatch_mismatch",
              "execution binding 的 dispatch 与当前派发不一致。")
    if str(stored.get("work_item_id") or "") != work_item_id:
        _fail("execution_binding_work_item_mismatch",
              "execution binding 的工作项与执行结果不一致。")
    expected = [str(item) for item in stored.get("test_commands") or []]
    if expected and expected != test_commands:
        _fail("execution_binding_command_mismatch",
              "执行测试命令与 binding 的预期命令不一致；不能事后更换测试命令。")
    workspace = _text(obj.get("workspace"),
                      "execution.execution_binding.workspace")
    if not os.path.isabs(workspace):
        _fail("execution_binding_invalid",
              f"binding workspace 必须是绝对路径：{workspace}")
    if os.path.realpath(workspace) != str(stored.get("workspace") or ""):
        _fail("execution_binding_workspace_mismatch",
              "execution binding 的工作区与执行信封不一致。")
    policy = (state.get("contract") or {}).get("test_policy") or {}
    if policy.get("captured"):
        # 合同要求的验证层只能加严：stored binding 若被改写为更低要求，拒绝回填。
        if bool(policy.get("require_test_result")) and not bool(
                stored.get("require_test_result")):
            _fail("execution_binding_policy_weakened",
                  "合同要求结构化测试结果，但 binding 关闭了该层。")
        if bool(policy.get("require_artifact_integrity")) and not bool(
                stored.get("require_artifact_integrity")):
            _fail("execution_binding_policy_weakened",
                  "合同要求产物完整性，但 binding 关闭了该层。")
        contract_commands = [str(item) for item in policy.get("commands") or []]
        if contract_commands and expected != contract_commands:
            _fail("execution_binding_command_mismatch",
                  "binding 的测试命令与合同登记不一致。")
        # 报告/产物/策略快照也必须与当前合同一致：只保留 require 布尔而清空
        # contract_report/contract_artifacts 会让 TE 跳过合同要求的产物。
        contract_policy = {key: policy.get(key) for key in
                           ("required", "commands", "report", "artifacts",
                            "require_test_result",
                            "require_artifact_integrity")}
        stored_policy = stored.get("contract_test_policy") or {}
        if (stored.get("requirement_source") != "contract"
                or canonical_json(stored_policy) != canonical_json(
                    contract_policy)
                or str(stored.get("contract_report") or "")
                != str(policy.get("report") or "")
                or sorted(str(item) for item in
                          stored.get("contract_artifacts") or [])
                != sorted(str(item) for item in policy.get("artifacts") or [])):
            _fail("execution_binding_policy_weakened",
                  "binding 的合同测试策略/报告/产物与当前合同不一致。")
    return {"binding_id": binding_id, "nonce": nonce,
            "created_at": str(stored.get("created_at") or ""),
            "workspace": str(stored.get("workspace") or ""),
            "test_commands": expected,
            "require_test_result": bool(stored.get("require_test_result")),
            "require_artifact_integrity": bool(
                stored.get("require_artifact_integrity")),
            "requirement_source": str(stored.get("requirement_source") or ""),
            "contract_test_policy": stored.get("contract_test_policy") or {},
            "contract_report": str(stored.get("contract_report") or ""),
            "contract_artifacts": list(stored.get("contract_artifacts") or [])}

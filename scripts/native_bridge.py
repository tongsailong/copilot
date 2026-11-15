#!/usr/bin/env python3
"""Agent Bridge v2：ZCode 本地计划 → 原生执行/复核 → 治理状态。

本模块只做**协议结构与绑定校验**，不调度子代理、不调用模型、不改任务状态：

* DISPATCH：把 `governance.py` 中已登记的计划工作项交给宿主，绑定
  task / contract / plan（不再绑定任何远程模型轮次）；
* RESULT：执行方回传实际结果、原始证据文件及其 SHA-256、运行元数据；
* REVIEW：独立 Reviewer 回传复核结论，绑定它检查过的执行结果 SHA；正式 G4
  资格由 `g4_attest`/`test_execution`/`g4_shadow` 新链独立核验，不由 Reviewer
  自述，也不由本模块计算。

安全边界
--------
1. 所有信封都绑定 task_id / contract_sha256 / plan_id / plan_sha256；执行与
   复核还绑定 dispatch_id / work_item_id。
2. 执行结果必须由当前 dispatch 的工作项产生；证据文件必须存在且 SHA-256 一致。
3. 复核必须晚于对应执行结果，且 Reviewer 不能是执行方本人（Agent ID /
   Child Session ID 任一相同即拒绝）。
4. Reviewer 不得改动文件；`changed_files` 非空即视为污染并拒绝。
5. PASS 不得带 blocking finding 或未验证事项。
6. 本模块的校验是**本地完整性与绑定校验**，不是密码学签名，也不是宿主可信
   身份证明；本模块的 `g4_eligible` 只是信封结构预检，正式资格在 ingest 时由
   `g4_attest` 读取宿主 metadata/SQLite 后派生，治理层收口只接受 candidate 来源。
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
SCHEMAS = (DISPATCH_SCHEMA, RESULT_SCHEMA, REVIEW_SCHEMA)
LEGACY_SCHEMAS = ("copilot-native-dispatch/v1", "copilot-native-result/v1",
                  "copilot-native-review/v1")

EXECUTION_STATUSES = ("done", "failed", "blocked", "dropped")
REVIEW_VERDICTS = ("pass", "partial", "blocked")
# full：完整复核；incremental：在一条既有合格复核之上只复核新增/变更证据。
REVIEW_SCOPES = ("full", "incremental")
FINDING_SEVERITIES = ("blocking", "major", "minor", "info")
# v6.3：finding 生命周期与稳定 ID。Reviewer 首轮给出的 ID 在后续轮次复用，
# closed 的问题只有新证据才能以 `regressed` 重开，宿主据此统计重复 finding。
FINDING_STATUSES = ("open", "resolved", "regressed")
NATIVE_ASSIGNEES = ("zcode", "deepseek")
EXECUTOR_KINDS = ("main", "subagent")
# 独立复核的宿主权限契约：只读工具精确集合 + 必须显式禁用的写入/执行工具 +
# 可接受的权限模式。判定只看宿主实际施加的有效策略，不看 Profile 名称、来源
# 或所在路径；`g4_attest` 从 metadata.profileSnapshot 独立复核同一契约。
REVIEWER_TOOLS = ("Read", "Grep", "Glob")
REVIEWER_DISALLOWED = ("Write", "Edit", "Bash")
REVIEWER_PERMISSION_MODES = ("default", "plan")

EVIDENCE_ID_RE = re.compile(r"^E[1-9][0-9]{0,3}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REVIEW_ATTEMPT_RE = re.compile(r"^RA[0-9a-f]{24}$")
_FINDING_ID_RE = re.compile(r"^F[1-9][0-9]{0,3}$")
# 这些路径不得作为 Reviewer 的 inspected 文件：设备/管道/目录不是可复核内容。
_NON_REGULAR_PREFIXES = ("/dev/", "/proc/", "/sys/")


class BridgeError(ValueError):
    """可预期的协议/绑定失败。code 供 CLI、测试和审计稳定引用。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)


def _fail(code: str, message: str) -> None:
    raise BridgeError(code, message)


def normalize_profile_policy(snapshot: object) -> dict:
    """把宿主 profileSnapshot 归一化成 G4 判定用的**有效权限策略**。

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


def reviewer_policy_problems(policy: object) -> list[tuple[str, str]]:
    """返回只读复核策略的偏离项；空列表表示等价于只读复核。

    `tools=[]` 在宿主语义里等于“继承父工具”，一律拒绝；`tools` 必须精确等于
    只读集合，多一个 Bash/Write/MCP 工具都不行。
    """
    data = normalize_profile_policy(policy)
    problems: list[tuple[str, str]] = []
    tools = data["tools"]
    if not tools:
        problems.append(("profile_tools_empty",
                         "空工具集会被宿主解释为继承父工具"))
    elif set(tools) != set(REVIEWER_TOOLS):
        problems.append(("profile_tools_not_exact",
                         f"tools={tools} 必须精确等于 {list(REVIEWER_TOOLS)}"))
    disallowed = set(data["disallowedTools"])
    missing = [name for name in REVIEWER_DISALLOWED if name not in disallowed]
    if missing:
        problems.append(("profile_disallowed_missing", ",".join(missing)))
    mode = data["permissionMode"]
    if mode and mode not in REVIEWER_PERMISSION_MODES:
        problems.append(("profile_permission_mode", f"permissionMode={mode!r}"))
    if data["skills"]:
        problems.append(("profile_skills_not_allowed",
                         f"skills={data['skills']}"))
    if data["mcpServers"]:
        problems.append(("profile_mcp_servers",
                         f"mcpServers={data['mcpServers']}"))
    return problems


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
    """返回 execution / review；未知 schema 失败关闭。"""
    obj = _obj(envelope, "native result")
    schema = str(obj.get("schema") or "").strip()
    if schema == RESULT_SCHEMA:
        return "execution"
    if schema == REVIEW_SCHEMA:
        return "review"
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
              f"{schema} 属于 v1 SOL 通道，已退役；不能用于 v6 派发。")
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
    # 只读 Profile 不得承担执行工作项。判定按宿主快照的有效策略，不按名称：
    # 任一只读工具集都跑不了工程执行，名字叫什么都一样。`executor.profile`
    # 字段必填（缺失失败关闭）；字段存在但未声明 tools 时视为“继承父工具/未知”，
    # 按宿主语义不构成只读，因此不拒绝——只读判定只认精确的 Read/Grep/Glob。
    # 权威复核仍由 `test_execution` 读取宿主 metadata 后重做。
    policy = _profile_policy_or_none(obj)
    if policy is None:
        _fail("executor_profile_invalid",
              "执行结果缺少 executor.profile（宿主 profileSnapshot 派生的有效"
              "工具策略）；没有策略就无法证明执行方不是只读 Profile。")
    if not reviewer_policy_problems(policy):
        _fail("executor_profile_readonly",
              "执行方 Profile 的有效工具策略等价于只读复核"
              "（tools 仅 Read/Grep/Glob）；只读 Profile 不得承担工程执行。")
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



def _validate_files_inspected(raw: object, *, check_files: bool) -> list[str]:
    """校验自报的 inspected 文件清单；允许为空。

    v6.1 起复核命令与实际读取由宿主 rollout 派生，`files_inspected` 只是补充
    声明：只做路径合法性检查，不再要求至少一条或多条声明匹配。G4 的证据覆盖
    仍由宿主实际工具调用计算，不能靠这里放行。
    """
    items = _list(raw or [], "files_inspected")
    out: list[str] = []
    for idx, value in enumerate(items, 1):
        path = _text(value, f"files_inspected[{idx}]")
        if not os.path.isabs(path):
            _fail("review_inspection_invalid",
                  f"files_inspected[{idx}] 必须是绝对路径：{path}")
        if path.startswith(_NON_REGULAR_PREFIXES):
            _fail("review_inspection_invalid",
                  f"files_inspected[{idx}] 不是普通文件：{path}")
        if check_files and not os.path.isfile(path):
            _fail("review_inspection_invalid",
                  f"Reviewer 声称检查的文件不存在：{path}")
        if path not in out:
            out.append(path)
    return out


def _validate_commands(raw: object) -> list[str]:
    """新只读 Reviewer 没有 Bash：任何自述命令都不能作为 G4 证据。"""
    commands = _list(raw or [], "review.commands_run")
    if commands:
        _fail("review_command_unexpected",
              "Reviewer 没有 Bash 权限；commands_run 必须为空。")
    return []


def _optional_text(obj: dict, key: str, name: str) -> str:
    value = obj.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        _fail("schema_invalid", f"{name}.{key} 必须是字符串。")
    return value.strip()


def _validate_findings(raw: object, *, check_files: bool) -> list[dict]:
    items = _list(raw or [], "findings")
    out: list[dict] = []
    seen_ids: dict[str, int] = {}
    seen_bodies: dict[tuple, int] = {}
    for idx, value in enumerate(items, 1):
        name = f"findings[{idx}]"
        obj = _obj(value, name)
        severity = _text(obj.get("severity"), f"{name}.severity").lower()
        if severity not in FINDING_SEVERITIES:
            _fail("review_finding_invalid",
                  f"{name}.severity 只能是 {', '.join(FINDING_SEVERITIES)}。")
        summary = _text(obj.get("summary"), f"{name}.summary")
        file = str(obj.get("file") or "").strip()
        evidence = str(obj.get("evidence") or "").strip()
        if severity in ("blocking", "major"):
            if not file or not evidence:
                _fail("review_finding_invalid",
                      f"{severity} finding 必须给出 file 与 evidence（具体证据）。")
        if file and check_files and os.path.isabs(file) and not os.path.exists(file):
            _fail("review_finding_invalid",
                  f"{name}.file 不存在：{file}")
        line = obj.get("line")
        if line is not None:
            line = _int(line, f"{name}.line", minimum=1)
        # v6.3 可选元数据：稳定 ID 与生命周期用于跨轮去重和指标，root_cause 用于
        # 把同一根因的多处表现聚成一条，category 只做归类。缺失时保持旧行为。
        fid = _optional_text(obj, "id", name).upper()
        if fid:
            if not _FINDING_ID_RE.match(fid):
                _fail("review_finding_invalid",
                      f"{name}.id 必须是 F1–F9999 形式（实际 {fid!r}）。")
            if fid in seen_ids:
                _fail("review_finding_duplicate",
                      f"{name}.id={fid} 与 findings[{seen_ids[fid]}] 重复；"
                      f"同一 finding 只登记一次。")
            seen_ids[fid] = idx
        root_cause = _optional_text(obj, "root_cause", name)
        category = _optional_text(obj, "category", name)
        reproduce = _optional_text(obj, "reproduce", name)
        status = _optional_text(obj, "status", name).lower() or "open"
        if status not in FINDING_STATUSES:
            _fail("review_finding_invalid",
                  f"{name}.status 只能是 {', '.join(FINDING_STATUSES)}。")
        related_raw = obj.get("related")
        related: list[str] = []
        for rel_idx, rel in enumerate(_list(related_raw or [], f"{name}.related"), 1):
            rel_id = _text(rel, f"{name}.related[{rel_idx}]").upper()
            if not _FINDING_ID_RE.match(rel_id):
                _fail("review_finding_invalid",
                      f"{name}.related[{rel_idx}] 必须是 F1–F9999 形式。")
            if rel_id == fid:
                _fail("review_finding_invalid",
                      f"{name}.related 不能引用自身 {rel_id}。")
            if rel_id not in related:
                related.append(rel_id)
        # 同一轮把同一问题写两遍只会放大 finding 数量，不增加信息量。
        body = (severity, file, line, summary)
        if body in seen_bodies:
            _fail("review_finding_duplicate",
                  f"{name} 与 findings[{seen_bodies[body]}] 是同一问题"
                  f"（severity/file/line/summary 相同）；请合并并引用 ID。")
        seen_bodies[body] = idx
        out.append({"severity": severity, "summary": summary,
                    "file": file, "line": line, "evidence": evidence,
                    "id": fid, "root_cause": root_cause, "category": category,
                    "status": status, "related": related,
                    "reproduce": reproduce})
    return out


def _validate_reviewer(reviewer: object) -> dict:
    obj = _obj(reviewer, "reviewer")
    kind = _text(obj.get("kind") or "subagent", "reviewer.kind").lower()
    if kind != "subagent":
        _fail("reviewer_invalid", "Reviewer 必须是独立子代理（kind=subagent）。")
    agent_id = _text(obj.get("agent_id"), "reviewer.agent_id")
    child = _text(obj.get("child_session_id"), "reviewer.child_session_id")
    profile = _text(obj.get("profile_id"), "reviewer.profile_id")
    profile_policy = _profile_policy_or_none(obj)
    if profile_policy is None:
        _fail("reviewer_profile_invalid",
              "Reviewer 信封缺少 reviewer.profile（宿主 profileSnapshot 派生的"
              "有效工具策略）；Profile 名称、来源或路径都不能代替权限证据。")
    problems = reviewer_policy_problems(profile_policy)
    if problems:
        code, detail = problems[0]
        _fail("reviewer_profile_not_readonly",
              f"Reviewer 的有效工具策略不满足只读复核（{code}: {detail}）。")
    model = _text(obj.get("model_reported"), "reviewer.model_reported")
    return {
        "kind": "subagent",
        "agent_id": agent_id,
        "child_session_id": child,
        "profile_id": profile,
        "profile": normalize_profile_policy(profile_policy),
        "model_reported": model,
    }



def validate_execution_result(envelope: object, dispatch: dict | None, state: dict, *,
                              check_files: bool = True) -> dict:
    """校验执行结果：计划绑定、结果摘要、证据文件、执行方元数据。"""
    env = _obj(envelope, "execution result")
    schema = str(env.get("schema") or "").strip()
    if schema in LEGACY_SCHEMAS:
        _fail("schema_retired",
              f"{schema} 属于 v1 SOL 通道，已退役；不能用于 v6 回填。")
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
        "result_sha256": declared_sha,
    }


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


def _validate_host_record(raw: object, *, required: bool) -> dict:
    """校验宿主运行记录引用；G4 复核必须有可重新核对的宿主文件。"""
    if raw is None:
        if required:
            _fail("g4_host_record_missing",
                  "g4_eligible=true 的复核必须附宿主 metadata/outputFile/rollout 记录。")
        return {}
    obj = _obj(raw, "host")
    out: dict = {}
    for key in ("metadata_path", "output_file", "rollout_path"):
        value = str(obj.get(key) or "").strip()
        if not value:
            if key == "metadata_path" and required:
                _fail("g4_host_record_invalid",
                      "host.metadata_path 缺失：G4 复核必须能重新核对宿主记录。")
            out[key] = ""
            continue
        path = os.path.abspath(os.path.expanduser(value))
        if not os.path.isabs(path):
            _fail("g4_host_record_invalid", f"host.{key} 必须是绝对路径：{path}")
        if required and key == "metadata_path" and not os.path.isfile(path):
            _fail("g4_host_record_invalid", f"host.{key} 不存在：{path}")
        out[key] = path
    # rollout/outputFile 被宿主清理时，G4 不合格的信封可标注持久化来源
    # （db.message.part / session_entry）；这些字段只是溯源标记，不改变资格。
    for key in ("tool_call_source", "model_source", "output_source"):
        out[key] = str(obj.get(key) or "").strip()[:80]
    out["output_sha256"] = str(obj.get("output_sha256") or "").strip().lower()
    if out["output_sha256"] and not re.fullmatch(r"[0-9a-f]{64}",
                                                 out["output_sha256"]):
        _fail("g4_host_record_invalid", "host.output_sha256 格式非法")
    host_db = str(obj.get("host_db") or "").strip()
    if host_db:
        host_db = os.path.abspath(os.path.expanduser(host_db))
        if not os.path.isabs(host_db):
            _fail("g4_host_record_invalid", f"host.host_db 必须是绝对路径：{host_db}")
    out["host_db"] = host_db
    calls = _list(obj.get("tool_calls"), "host.tool_calls")
    normalized = []
    for idx, call in enumerate(calls, 1):
        item = _obj(call, f"host.tool_calls[{idx}]")
        name = _text(item.get("name"), f"host.tool_calls[{idx}].name")
        if name not in REVIEWER_TOOLS:
            _fail("g4_tool_not_readonly",
                  f"G4 复核出现非只读工具 {name!r}；只允许 {', '.join(REVIEWER_TOOLS)}。")
        normalized.append({"name": name,
                           "count": _int(item.get("count") or 0,
                                         f"host.tool_calls[{idx}].count")})
    out["tool_calls"] = normalized
    out["agent_id"] = str(obj.get("agent_id") or "").strip()
    out["child_session_id"] = str(obj.get("child_session_id") or "").strip()
    # G4 快照绑定的宿主侧证据：会话创建时间与 prompt 摘要。只接受字符串，
    # 具体时间顺序与值是否与 metadata 一致由 native_agent 重新核对。
    for key in ("session_created_at", "session_completed_at", "prompt_sha256",
                "workspace_root"):
        value = obj.get(key)
        if value is not None:
            out[key] = str(value).strip()
    return out


def _action_fingerprint(action: dict) -> str:
    """工作项覆盖指纹：与 final_evidence_digest 的动作项口径保持一致。"""
    return sha256_text(canonical_json([
        action.get("status"),
        sha256_text(str(action.get("result") or "")),
        sorted(action.get("evidence_ids") or []),
    ]))


def review_coverage(state: dict) -> dict:
    """复核覆盖快照：证据摘要、工作项指纹与条件状态。

    增量复核只允许在“未覆盖的变化”都能被本轮逐条点出时使用；这份快照在
    ingest 时固化，后续据它计算变化集，避免把旧结论套到新状态上。
    """
    evidence: dict[str, str] = {}
    for entry in state.get("evidence") or []:
        if isinstance(entry, dict) and str(entry.get("id") or ""):
            # 归档（dropped）也是覆盖状态的一部分：若不进入指纹，基线后归档
            # 证据 + 改动工作项结果会被误判为“无需覆盖新证据”的增量复核。
            evidence[str(entry["id"])] = "%s:%s" % (
                str(entry.get("sha256") or ""),
                "dropped" if entry.get("dropped") else "live")
    actions: dict[str, str] = {}
    for action in state.get("actions") or []:
        if not isinstance(action, dict):
            continue
        if str(action.get("origin") or "") == "g4-review":
            continue
        actions[str(action.get("id") or "")] = _action_fingerprint(action)
    criteria: dict[str, str] = {}
    for criterion in (state.get("contract") or {}).get("criteria") or []:
        if not isinstance(criterion, dict):
            continue
        criteria[str(criterion.get("criterion_id") or "")] = sha256_text(
            canonical_json([criterion.get("status"),
                            sorted([[r.get("id"), r.get("sha256")]
                                    for r in criterion.get("evidence_refs") or []])]))
    return {"evidence": evidence, "actions": actions, "criteria": criteria}


def execution_evidence_drift(execution: dict, state: dict) -> list[dict]:
    """执行信封 evidence[] 与当前 live 证据之间的漂移（非阻断提示）。

    执行结果登记后，证据可能被更正、重新捕获或归档；执行正文仍引用旧快照时，
    复核必须能看见这层不一致，而不是靠人读正文发现。返回空列表表示一致。
    """
    live = {str(entry.get("id") or ""): entry
            for entry in state.get("evidence") or [] if isinstance(entry, dict)}
    out: list[dict] = []
    for entry in execution.get("evidence") or []:
        if not isinstance(entry, dict):
            continue
        eid = str(entry.get("id") or "")
        if not eid:
            continue
        current = live.get(eid)
        execution_sha = str(entry.get("sha256") or "")
        if current is None:
            out.append({"id": eid, "problem": "not_registered",
                        "execution_sha256": execution_sha, "live_sha256": ""})
            continue
        live_sha = str(current.get("sha256") or "")
        if live_sha != execution_sha:
            out.append({"id": eid, "problem": "sha_differs",
                        "execution_sha256": execution_sha, "live_sha256": live_sha})
        elif current.get("dropped"):
            out.append({"id": eid, "problem": "dropped",
                        "execution_sha256": execution_sha, "live_sha256": live_sha})
    return out


def _validate_review_scope(env: dict, state: dict, *, contract: str,
                           work_item_id: str) -> tuple[str, str, list[str]]:
    """校验复核范围；增量复核必须绑定一条可用基线并覆盖全部变化。"""
    scope = str(env.get("review_scope") or "full").strip().lower()
    if scope not in REVIEW_SCOPES:
        _fail("review_scope_invalid",
              f"review_scope 只能是 {', '.join(REVIEW_SCOPES)}（实际 {scope!r}）。")
    if scope == "full":
        if env.get("based_on_review_sha256") or env.get("evidence_manifest"):
            _fail("review_scope_invalid",
                  "review_scope=full 不应带 based_on_review_sha256 / "
                  "evidence_manifest；增量复核请显式声明 review_scope=incremental。")
        return scope, "", []
    based_on = _sha(env.get("based_on_review_sha256"),
                    "review.based_on_review_sha256")
    base = None
    for record in state.get("native_reviews") or []:
        if isinstance(record, dict) and str(record.get("result_sha256") or "") == based_on:
            base = record
            break
    if base is None:
        _fail("incremental_base_missing",
              f"增量复核的基线 {based_on[:16]}… 不在本任务复核记录中；"
              "请先回填基线复核，或改用完整复核。")
    if str(base.get("contract_sha256") or "") != contract:
        _fail("incremental_base_contract_mismatch",
              "基线复核属于旧合同；合同已变更，必须做完整复核。")
    if str(base.get("verdict") or "").strip().lower() != "pass" or base.get(
            "g4_eligible") is not True:
        _fail("incremental_base_ineligible",
              "基线复核不是可用的 G4 PASS 结论；不能作为增量基础。")
    if any(isinstance(f, dict) and str(f.get("severity") or "") == "blocking"
           for f in base.get("findings") or []):
        _fail("incremental_base_ineligible", "基线复核仍有 blocking finding。")
    # 基线的最终信任由治理层统一判定：旧版无 attempt 的 PASS、宿主记录被
    # 改写、attempt 未消费或证据文件漂移都不能作为增量基础。这里只做同一
    # 口径的早期拒绝，避免 ingest 接受一条收口必败的增量复核。
    try:
        import governance as G  # 延迟导入：避免状态引擎与信封层在导入期耦合
    except ImportError:
        G = None
    if G is not None:
        trust = G._review_trust_problems(base, state, check_target=False)
        if trust:
            _fail("incremental_base_ineligible",
                  "基线复核未通过当前 G4 信任检查：" + ", ".join(trust)
                  + "；请对当前快照做一次完整复核。")
        base_attempt = G.find_review_attempt(
            state, str(base.get("review_attempt_id") or ""))
        code_problems = G.code_scope_problems(
            base_attempt, state, check_current=True) if base_attempt else \
            ["native_review_code_scope_missing"]
        if code_problems:
            _fail("incremental_code_changed",
                  "基线复核之后工作区代码范围已变化（"
                  + ", ".join(code_problems)
                  + "）；增量复核只覆盖证据，不能覆盖源码变更，请做完整复核。")
    base_coverage = base.get("coverage")
    if not isinstance(base_coverage, dict) or not base_coverage:
        _fail("incremental_base_coverage_missing",
              "基线复核缺少覆盖快照（旧记录）；请做一次完整复核后再增量。")
    current = review_coverage(state)
    base_evidence = base_coverage.get("evidence") or {}
    base_actions = base_coverage.get("actions") or {}
    base_criteria = base_coverage.get("criteria") or {}
    changed_evidence = sorted(
        eid for eid, sha in current["evidence"].items()
        if base_evidence.get(eid) != sha)
    changed_actions = sorted(
        aid for aid, fp in current["actions"].items()
        if base_actions.get(aid) != fp)
    changed_criteria = sorted(
        cid for cid, fp in current["criteria"].items()
        if base_criteria.get(cid) != fp)
    if changed_criteria:
        _fail("incremental_criteria_changed",
              "完成条件或其核销状态在基线与本轮之间发生变化（"
              f"{', '.join(changed_criteria)}）；改动范围超出增量复核，"
              "必须做完整复核。")
    manifest = [str(item).strip()
                for item in _list(env.get("evidence_manifest") or [],
                                  "review.evidence_manifest")
                if str(item).strip()]
    unknown = [eid for eid in manifest if eid not in current["evidence"]]
    if unknown:
        _fail("incremental_manifest_unknown",
              f"evidence_manifest 含未登记的证据 id：{', '.join(unknown)}。")
    if changed_evidence:
        if not manifest:
            _fail("incremental_manifest_missing",
                  "本轮新增/变更证据必须逐条列入 evidence_manifest："
                  f"{', '.join(changed_evidence)}。")
        missing = [eid for eid in changed_evidence if eid not in manifest]
        if missing:
            _fail("incremental_evidence_uncovered",
                  "增量复核未覆盖新增/变更证据：" + ", ".join(missing) +
                  "；请检查这些证据或改做完整复核。")
    others = [aid for aid in changed_actions if aid != work_item_id]
    if others:
        _fail("incremental_scope_too_narrow",
              "基线之后还有其他工作项的结论变化（" + ", ".join(others) +
              "）；增量复核只覆盖被复核的工作项，请做完整复核。")
    if not changed_evidence and not changed_actions:
        _fail("incremental_without_change",
              "相对基线没有任何变化，无需增量复核；如需复述结论请复用基线。")
    return scope, based_on, manifest


def validate_review_result(envelope: object, dispatch: dict | None, execution: dict,
                           state: dict, *, check_files: bool = True) -> dict:
    """校验独立复核：绑定执行结果 SHA、Reviewer 独立性、只读性与 PASS 门槛。"""
    env = _obj(envelope, "review result")
    schema = str(env.get("schema") or "").strip()
    if schema in LEGACY_SCHEMAS:
        _fail("schema_retired",
              f"{schema} 属于 v1 SOL 通道，已退役；不能用于 v6 复核。")
    if schema != REVIEW_SCHEMA:
        _fail("schema_invalid", f"review schema 必须是 {REVIEW_SCHEMA}。")
    if not isinstance(dispatch, dict):
        _fail("dispatch_missing", "状态没有 native_dispatch；不能回填复核结果。")
    task_id, contract = _require_state_credential(state)
    if str(env.get("task_id") or "").strip() != task_id:
        _fail("task_mismatch", "复核结果 task_id 与当前任务不一致。")
    if str(env.get("contract_sha256") or "").strip() != contract:
        _fail("contract_mismatch", "复核结果 contract_sha256 与当前合同不一致。")
    plan_id = _text(env.get("plan_id"), "review.plan_id")
    plan_sha = _sha(env.get("plan_sha256"), "review.plan_sha256")
    if plan_id != str(dispatch.get("plan_id") or ""):
        _fail("plan_mismatch", "复核结果 plan_id 与当前 dispatch 不一致。")
    if plan_sha != str(dispatch.get("plan_sha256") or "").lower():
        _fail("plan_mismatch", "复核结果 plan_sha256 与当前 dispatch 不一致。")
    _check_plan(state, plan_id, plan_sha)
    wid = _text(env.get("work_item_id"), "review.work_item_id")
    did = _text(env.get("dispatch_id"), "review.dispatch_id")
    items = {str(i.get("work_item_id") or ""): i
             for i in dispatch.get("actions") or [] if isinstance(i, dict)}
    item = items.get(wid)
    if not isinstance(item, dict):
        _fail("work_item_unknown", f"当前 dispatch 不含工作项 {wid}。")
    if str(item.get("dispatch_id") or "") != did:
        _fail("dispatch_mismatch",
              f"复核结果 dispatch_id={did} 与工作项 {wid} 的派发 ID 不一致。")
    if str(execution.get("work_item_id") or "") != wid:
        _fail("review_binding_mismatch", "复核工作项与被执行工作项不一致。")
    if str(execution.get("dispatch_id") or "") != did:
        _fail("review_binding_mismatch", "复核 dispatch_id 与执行结果不一致。")
    exec_sha = _sha(execution.get("result_sha256"), "execution.result_sha256")
    bound_sha = _sha(env.get("execution_result_sha256"),
                     "review.execution_result_sha256")
    if bound_sha != exec_sha:
        _fail("review_binding_mismatch",
              "复核绑定的 execution_result_sha256 与已入账执行结果不一致。")
    reviewer = _validate_reviewer(env.get("reviewer"))
    executor = execution.get("executor") or {}
    if not isinstance(executor, dict):
        _fail("schema_invalid", "执行结果缺少 executor 元数据。")
    for key in ("agent_id", "child_session_id"):
        exec_value = str(executor.get(key) or "").strip()
        rev_value = str(reviewer.get(key) or "").strip()
        if exec_value and rev_value and exec_value == rev_value:
            _fail("executor_reviewer_same",
                  f"Reviewer 与执行方的 {key} 相同；独立复核必须使用独立子会话。")
    if _profile_policy_or_none(executor) is not None and not \
            reviewer_policy_problems(executor.get("profile")):
        _fail("executor_profile_readonly", "执行方不得使用只读 Profile。")
    changed = _list(env.get("changed_files") or [], "review.changed_files")
    if changed:
        _fail("review_contaminated",
              "Reviewer 报告了 changed_files；只读复核不得改动工作区。")
    files = _validate_files_inspected(env.get("files_inspected"),
                                      check_files=check_files)
    commands = _validate_commands(env.get("commands_run"))
    findings = _validate_findings(env.get("findings"), check_files=check_files)
    unverified_raw = _list(env.get("unverified") or [], "review.unverified")
    unverified = [str(x).strip() for x in unverified_raw if str(x).strip()]
    risks_raw = _list(env.get("residual_risks") or [], "review.residual_risks")
    residual = [str(x).strip() for x in risks_raw if str(x).strip()]
    verdict = _text(env.get("verdict"), "review.verdict").lower()
    if verdict not in REVIEW_VERDICTS:
        _fail("invalid_verdict",
              f"复核 verdict={verdict!r} 只能是 {', '.join(REVIEW_VERDICTS)}。")
    g4_eligible = env.get("g4_eligible")
    if not isinstance(g4_eligible, bool):
        _fail("schema_invalid", "review.g4_eligible 必须是布尔值。")
    attempt_id = str(env.get("review_attempt_id") or "").strip()
    attempt_created = str(env.get("review_attempt_created_at") or "").strip()
    if attempt_id and not _REVIEW_ATTEMPT_RE.match(attempt_id):
        _fail("review_attempt_invalid",
              "review.review_attempt_id 必须是 RA + 24 位小写十六进制。")
    if g4_eligible and not attempt_id:
        _fail("review_attempt_missing",
              "G4 复核必须绑定派发前创建的 review_attempt_id；"
              "不能用信封生成时的 target 代替实际评审快照。")
    if g4_eligible and not attempt_created:
        _fail("review_attempt_invalid",
              "G4 复核必须携带 review_attempt_created_at。")
    target_sha = ""
    if g4_eligible:
        target_sha = _sha(env.get("target_sha256"), "review.target_sha256")
        if verdict != "pass":
            _fail("g4_requires_pass", "g4_eligible=true 的复核必须 verdict=pass。")
        if unverified:
            _fail("g4_with_unverified", "G4 复核仍有未验证事项；不能标记可用。")
        if any(f["severity"] == "blocking" for f in findings):
            _fail("g4_with_blocking", "G4 复核存在 blocking finding；不能标记可用。")
    host = _validate_host_record(env.get("host"), required=g4_eligible)
    scope, based_on, manifest = _validate_review_scope(
        env, state, contract=contract, work_item_id=wid)
    if verdict == "pass" and any(f["severity"] == "blocking" for f in findings):
        _fail("pass_with_blocking_finding",
              "复核 verdict=pass 但存在 blocking finding；必须改判或补齐。")
    if verdict == "pass" and unverified:
        _fail("pass_with_unverified",
              "复核 verdict=pass 但仍有未验证事项；不得猜测 PASS。")
    declared_sha = _sha(env.get("result_sha256"), "review.result_sha256")
    actual_sha = envelope_sha256(env)
    if declared_sha != actual_sha:
        _fail("result_sha_mismatch",
              "复核结果 result_sha256 与信封内容不一致（可能被篡改）。")
    record = {
        "schema": REVIEW_SCHEMA,
        "task_id": task_id,
        "contract_sha256": contract,
        "plan_id": plan_id,
        "plan_sha256": plan_sha,
        "dispatch_id": did,
        "work_item_id": wid,
        "execution_result_sha256": exec_sha,
        "target_sha256": target_sha,
        "review_attempt_id": attempt_id,
        "review_attempt_created_at": attempt_created,
        "reviewer": reviewer,
        "host": host,
        "verdict": verdict,
        "findings": findings,
        "files_inspected": files,
        "commands_run": commands,
        "changed_files": [],
        "unverified": unverified,
        "residual_risks": residual,
        "review_scope": scope,
        "based_on_review_sha256": based_on,
        "evidence_manifest": manifest,
        # 覆盖快照固化在复核记录上，供后续增量复核计算“哪些证据/工作项/条件
        # 在基线之后发生了变化”，而不是重新猜测历史状态。
        "coverage": review_coverage(state),
        # 执行证据与 live 证据的漂移：非阻断，但收口与复核需要看见。
        "execution_evidence_unbound": execution_evidence_drift(execution, state),
        "g4_eligible": g4_eligible,
        "g4_reasons": [str(item).strip() for item in
                       _list(env.get("g4_reasons") or [], "review.g4_reasons")
                       if str(item).strip()],
        "result_sha256": declared_sha,
    }
    declared = [str(item).strip()
                for item in _list(env.get("declared_files_unverified") or [],
                                  "review.declared_files_unverified")
                if str(item).strip()]
    if declared:
        # Reviewer 自报但宿主记录里没有实际检查痕迹的文件：保留在台账里供审计，
        # 不影响 G4 判定（G4 只看宿主派生出的实际访问记录）。
        record["declared_files_unverified"] = declared
    return record

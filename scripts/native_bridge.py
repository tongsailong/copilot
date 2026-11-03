#!/usr/bin/env python3
"""Agent Bridge：SOL 安排 → ZCode 原生执行/复核 → Copilot 状态回填。

本模块只做**协议结构与绑定校验**，不调度子代理、不调用模型、不改任务状态：

* DISPATCH：把已验证 SOL 轮次里的工作项交给宿主，保留原字段并加唯一
  `dispatch_id` / `idempotency_key`；
* RESULT：执行方回传实际结果、原始证据文件及其 SHA-256、运行元数据；
* REVIEW：独立 Reviewer 回传复核结论，并绑定它检查过的执行结果 SHA。

安全边界
--------
1. 所有信封都绑定 task_id / round_id / contract_sha256；执行与复核还绑定
   dispatch_id / work_item_id。
2. 执行结果必须由当前 dispatch 的工作项产生；证据文件必须存在且 SHA-256 一致。
3. 复核必须晚于对应执行结果，且 Reviewer 不能是执行方本人（Agent ID /
   Child Session ID 任一相同即拒绝）。
4. Reviewer 不得改动文件；`changed_files` 非空即视为污染并拒绝。
5. PASS 不得带 blocking finding 或未验证事项；`g4_eligible` 必须为 false。
6. 本模块的校验是**本地完整性与绑定校验**，不是密码学签名，也不是宿主可信
   身份证明；普通原生复核的 PASS 不等于 G4。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime

DISPATCH_SCHEMA = "copilot-native-dispatch/v1"
RESULT_SCHEMA = "copilot-native-result/v1"
REVIEW_SCHEMA = "copilot-native-review/v1"
SCHEMAS = (DISPATCH_SCHEMA, RESULT_SCHEMA, REVIEW_SCHEMA)

EXECUTION_STATUSES = ("done", "failed", "blocked", "dropped")
REVIEW_VERDICTS = ("pass", "partial", "blocked")
FINDING_SEVERITIES = ("blocking", "major", "minor", "info")
NATIVE_ASSIGNEES = ("zcode", "deepseek")
EXECUTOR_KINDS = ("main", "subagent")

EVIDENCE_ID_RE = re.compile(r"^E[1-9][0-9]{0,3}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Reviewer 可以用 Bash 读取现场，但只允许这些只读前缀。真正的写保护仍由
# Reviewer Profile 的 instructions + 宿主工具权限负责；这里是第二道失败关闭。
_READONLY_COMMAND_PREFIXES = (
    "git diff", "git status", "git log", "git show", "git grep",
    "git rev-parse", "git ls-files", "rg ", "grep ", "cat ",
    "head ", "tail ", "wc ", "ls ", "find ", "stat ", "sha256sum ",
    "diff ", "cmp ", "sort ",
)
# 分号/管道是只读复核里常见的组合形式（`find ... | sort; stat ...`）。这里按
# **引号感知**拆分后逐段校验，只把顶层 `;` / `|` 当作分隔符；`&&`、重定向、
# 命令替换、反引号、换行一律拒绝。拆分不改变只读边界：每一段仍走同一白名单。
_COMMAND_SHELL_META = "&><`$\n\r"
_COMMAND_DENY_RE = re.compile(
    r"(?:^|\s)(?:-delete|-exec|-execdir|-ok|-fprint|-fprint0|-fprintf|-fls|"
    r"--output|--output-file|--ext-diff|--textconv|--open-files-in-pager|"
    r"--pre|--pre-glob|tee|truncate|chmod|chown|rm|mv|cp|install|dd)(?:\s|=|$)")
# sort 默认只读，但 -o/--output 会写文件，--compress-program 会执行外部程序。
_SORT_WRITE_RE = re.compile(
    r"(?:^|\s)(?:-o|--output(?:=|\s)|--compress-program(?:=|\s)|"
    r"-T|--temporary-directory(?:=|\s))")
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
    if schema == DISPATCH_SCHEMA:
        _fail("schema_invalid", "dispatch 信封不能用 --native-result 回填。")
    _fail("schema_invalid", f"未知 native result schema：{schema or '-'}。")


def _state_action(state: dict, work_item_id: str) -> dict:
    for action in state.get("actions") or []:
        if isinstance(action, dict) and str(action.get("id") or "") == work_item_id:
            return action
    _fail("work_item_unknown", f"状态中不存在工作项 {work_item_id}。")
    return {}


def _verified_rounds(state: dict) -> list[dict]:
    return [rnd for rnd in (state.get("rounds") or [])
            if isinstance(rnd, dict) and rnd.get("model_identity_ok") is True]


def _latest_verified_round(state: dict) -> dict:
    rounds = _verified_rounds(state)
    if not rounds:
        _fail("round_unverified", "没有通过模型身份验证的 SOL 轮次，不能生成/校验原生安排。")
    return rounds[-1]


def _require_state_credential(state: dict) -> tuple[str, str]:
    task_id = str(state.get("task_id") or "").strip()
    contract = str(state.get("contract_sha256") or "").strip()
    if not task_id or not contract:
        _fail("credential_missing", "状态缺少 task_id / contract_sha256，不能使用 Agent Bridge。")
    return task_id, contract


def _check_dispatch_action(item: dict, action: dict, name: str,
                           verified_rounds: set[str]) -> dict:
    """校验 dispatch 项没有改写 SOL 工作项的目标/约束/证据/执行方。"""
    wid = _text(item.get("work_item_id"), f"{name}.work_item_id")
    _text(item.get("dispatch_id"), f"{name}.dispatch_id")
    _text(item.get("idempotency_key"), f"{name}.idempotency_key")
    title = _text(item.get("title"), f"{name}.title")
    assignee = _text(item.get("assignee"), f"{name}.assignee").lower()
    if assignee not in NATIVE_ASSIGNEES:
        _fail("assignee_unsupported",
              f"{wid} 的 assignee={assignee!r} 不能走原生后端；只允许 "
              f"{' / '.join(NATIVE_ASSIGNEES)}。")
    goal = _text(item.get("goal"), f"{name}.goal", allow_empty=True)
    constraints = _text(item.get("constraints"), f"{name}.constraints", allow_empty=True)
    expected = _text(item.get("expected_evidence"), f"{name}.expected_evidence",
                     allow_empty=True)
    deps = _list(item.get("depends_on") or [], f"{name}.depends_on")
    deps = [str(d).strip() for d in deps if str(d).strip()]
    expected_action = {
        "title": title,
        "assignee": assignee,
        "goal": goal,
        "constraints": constraints,
        "expected_evidence": expected,
        "depends_on": deps,
    }
    for key, value in expected_action.items():
        if key == "depends_on":
            actual = [str(d).strip() for d in (action.get("depends_on") or [])]
        else:
            actual = str(action.get(key) or "").strip()
        if actual != value:
            _fail("dispatch_action_mismatch",
                  f"{wid} 的 {key} 与已验证 SOL 工作项不一致；主代理不得改写安排。")
    source_round = str(action.get("source_round_id") or "").strip()
    if not source_round or source_round not in verified_rounds:
        _fail("work_item_unverified",
              f"{wid} 没有绑定已验证 SOL 轮次（source_round_id={source_round or '-'}），"
              "不能进入原生执行。")
    return {"work_item_id": wid, **expected_action,
            "dispatch_id": str(item["dispatch_id"]).strip(),
            "idempotency_key": str(item["idempotency_key"]).strip()}


def build_dispatch(state: dict, actions: list[dict], round_info: dict,
                   existing: dict | None = None, *, created_at: str = "") -> dict:
    """生成一个绑定已验证 SOL 轮次的 dispatch 信封。

    `actions` 必须来自 `ready_actions(state)`（或同样经过状态校验的工作项）。
    每个工作项得到稳定且唯一的 `dispatch_id` / `idempotency_key`；同一轮重复
    生成时复用既有 ID，保证幂等。`existing` 是状态中的 `native_dispatch`。
    """
    if state.get("execution_backend") != "native":
        _fail("backend_not_native",
              "只有 execution_backend=native 的任务才能生成原生 dispatch。")
    task_id, contract = _require_state_credential(state)
    rid = _text(round_info.get("round_id"), "round_info.round_id")
    plan_call = _text(round_info.get("plan_call_id"), "round_info.plan_call_id")
    plan_reply = _sha(round_info.get("plan_reply_sha256"), "round_info.plan_reply_sha256")
    latest = _latest_verified_round(state)
    if rid != str(latest.get("round_id") or ""):
        _fail("round_mismatch",
              f"dispatch 轮次 {rid} 不是最近已验证轮次 {latest.get('round_id')}。")
    if plan_call != str(latest.get("call_id") or ""):
        _fail("plan_call_mismatch",
              f"plan_call_id {plan_call} 与最近已验证调用 "
              f"{latest.get('call_id') or '-'} 不一致。")
    if plan_reply != str(latest.get("reply_sha256") or "").lower():
        _fail("plan_reply_mismatch",
              "plan_reply_sha256 与最近已验证 SOL 回复摘要不一致。")
    if not isinstance(actions, list) or not actions:
        _fail("dispatch_empty", "没有可派发的工作项；不能生成空 dispatch。")

    previous_items: dict = {}
    if isinstance(existing, dict) and (
            str(existing.get("round_id") or "") == rid
            and str(existing.get("contract_sha256") or "") == contract
            and str(existing.get("plan_call_id") or "") == plan_call):
        raw_items = existing.get("items")
        if isinstance(raw_items, dict):
            previous_items = {str(k): v for k, v in raw_items.items()
                              if isinstance(v, dict)}

    verified_rounds = {str(r.get("round_id") or "") for r in _verified_rounds(state)}
    items: list[dict] = []
    seen: set[str] = set()
    for idx, action in enumerate(actions, 1):
        if not isinstance(action, dict):
            _fail("schema_invalid", f"actions[{idx}] 不是对象。")
        wid = _text(action.get("id"), f"actions[{idx}].id")
        if wid in seen:
            _fail("dispatch_duplicate", f"工作项 {wid} 在同一 dispatch 中重复。")
        seen.add(wid)
        binding = [task_id, rid, contract, plan_call, wid]
        item_dispatch_id = "d-" + sha256_text(canonical_json(binding))[:24]
        idempotency_key = "ik-" + sha256_text(
            canonical_json(binding + ["idempotency"]))[:32]
        prev = previous_items.get(wid)
        if isinstance(prev, dict):
            # 同轮同合同同计划：沿用既有 ID；若既有 ID 与当前绑定不一致，说明
            # 状态被改写，失败关闭而不是悄悄生成第二份派发。
            if str(prev.get("dispatch_id") or "") not in ("", item_dispatch_id):
                _fail("dispatch_id_drift",
                      f"{wid} 既有 dispatch_id 与当前绑定不一致；拒绝覆盖。")
            if str(prev.get("idempotency_key") or "") not in ("", idempotency_key):
                _fail("idempotency_drift",
                      f"{wid} 既有 idempotency_key 与当前绑定不一致；拒绝覆盖。")
        item = {
            "work_item_id": wid,
            "dispatch_id": item_dispatch_id,
            "idempotency_key": idempotency_key,
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
        # 复核没有改写权限：这里只校验字段一致性，不修改 action。
        _check_dispatch_action({**item, "work_item_id": wid}, action,
                               f"actions[{idx}]", verified_rounds)
        items.append(item)

    all_ids = sorted({i["dispatch_id"] for i in items})
    batch_id = "b-" + sha256_text(canonical_json(
        [task_id, rid, contract, plan_call, all_ids]))[:24]
    envelope = {
        "schema": DISPATCH_SCHEMA,
        "dispatch_id": batch_id,
        "task_id": task_id,
        "round_id": rid,
        "contract_sha256": contract,
        "plan_call_id": plan_call,
        "plan_reply_sha256": plan_reply,
        "created_at": created_at or _now(),
        "actions": items,
    }
    return envelope


def dispatch_state(envelope: dict, previous: dict | None = None) -> dict:
    """把 dispatch 信封转成状态中的 `native_dispatch`；保留既有结果/复核记录。"""
    env = validate_dispatch(envelope, _state_for_validation(envelope), require_state=False)
    # validate_dispatch 在 require_state=False 时只做结构校验；上面传入的伪状态
    # 不会用于写盘。真正的状态绑定由调用方在生成时保证。
    rid = env["round_id"]
    prev = previous if isinstance(previous, dict) else {}
    same_binding = (str(prev.get("round_id") or "") == rid
                    and str(prev.get("contract_sha256") or "")
                    == env["contract_sha256"]
                    and str(prev.get("plan_call_id") or "") == env["plan_call_id"])
    old_items = prev.get("items") if same_binding else {}
    if not isinstance(old_items, dict):
        old_items = {}
    items: dict[str, dict] = {}
    for action in env["actions"]:
        wid = action["work_item_id"]
        old = old_items.get(wid) if isinstance(old_items.get(wid), dict) else {}
        items[wid] = {
            "dispatch_id": action["dispatch_id"],
            "idempotency_key": action["idempotency_key"],
            "assignee": action["assignee"],
            "status": str(old.get("status") or "dispatched"),
            "results": list(old.get("results") or []),
            "latest_result_sha256": old.get("latest_result_sha256"),
            "review_result_sha256": old.get("review_result_sha256"),
            "review_verdict": old.get("review_verdict"),
            "created_at": str(old.get("created_at") or _now()),
            "updated_at": _now(),
        }
    # 同轮旧项保留（例如依赖尚未满足、本次不在 ready 列表里的工作项）。
    for wid, old in old_items.items():
        if wid not in items and isinstance(old, dict):
            items[wid] = dict(old)
    return {
        "dispatch_id": env["dispatch_id"],
        "task_id": env["task_id"],
        "round_id": env["round_id"],
        "contract_sha256": env["contract_sha256"],
        "plan_call_id": env["plan_call_id"],
        "plan_reply_sha256": env["plan_reply_sha256"],
        "created_at": env["created_at"],
        "updated_at": _now(),
        "items": items,
    }


def _state_for_validation(envelope: dict) -> dict:
    """仅用于 dispatch_state 的结构校验占位；不提供任何现场事实。"""
    actions = []
    for item in envelope.get("actions") or []:
        if isinstance(item, dict):
            actions.append({
                "id": item.get("work_item_id"),
                "title": item.get("title"),
                "assignee": item.get("assignee"),
                "goal": item.get("goal"),
                "constraints": item.get("constraints"),
                "expected_evidence": item.get("expected_evidence"),
                "depends_on": item.get("depends_on") or [],
                "source_round_id": envelope.get("round_id"),
            })
    return {
        "task_id": envelope.get("task_id"),
        "contract_sha256": envelope.get("contract_sha256"),
        "execution_backend": "native",
        "rounds": [{
            "round_id": envelope.get("round_id"),
            "call_id": envelope.get("plan_call_id"),
            "reply_sha256": envelope.get("plan_reply_sha256"),
            "model_identity_ok": True,
        }],
        "actions": actions,
    }


def validate_dispatch(envelope: object, state: dict, *,
                      require_state: bool = True) -> dict:
    """校验 dispatch 结构、轮次/计划绑定，以及工作项是否被改写。"""
    env = _obj(envelope, "dispatch")
    if str(env.get("schema") or "").strip() != DISPATCH_SCHEMA:
        _fail("schema_invalid", f"dispatch schema 必须是 {DISPATCH_SCHEMA}。")
    task_id, contract = _require_state_credential(state)
    if str(env.get("task_id") or "").strip() != task_id:
        _fail("task_mismatch", "dispatch.task_id 与当前任务不一致。")
    if str(env.get("contract_sha256") or "").strip() != contract:
        _fail("contract_mismatch", "dispatch.contract_sha256 与当前合同不一致。")
    rid = _text(env.get("round_id"), "dispatch.round_id")
    latest = _latest_verified_round(state)
    verified_rounds = {str(r.get("round_id") or "") for r in _verified_rounds(state)}
    if rid != str(latest.get("round_id") or ""):
        _fail("round_mismatch", f"dispatch.round_id={rid} 不是最近已验证轮次。")
    if str(env.get("plan_call_id") or "").strip() != str(latest.get("call_id") or ""):
        _fail("plan_call_mismatch", "dispatch.plan_call_id 与最近已验证调用不一致。")
    if str(env.get("plan_reply_sha256") or "").lower() != str(
            latest.get("reply_sha256") or "").lower():
        _fail("plan_reply_mismatch", "dispatch.plan_reply_sha256 与已验证回复不一致。")
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
        if require_state:
            action = _state_action(state, wid)
            normalized = _check_dispatch_action(
                item, action, f"dispatch.actions[{idx}]", verified_rounds)
        else:
            normalized = _check_dispatch_action(item, {
                "id": wid,
                "title": item.get("title"),
                "assignee": item.get("assignee"),
                "goal": item.get("goal"),
                "constraints": item.get("constraints"),
                "expected_evidence": item.get("expected_evidence"),
                "depends_on": item.get("depends_on") or [],
                "source_round_id": rid,
            }, f"dispatch.actions[{idx}]", verified_rounds)
        out_actions.append(normalized)
    return {
        "schema": DISPATCH_SCHEMA,
        "dispatch_id": str(env["dispatch_id"]).strip(),
        "task_id": task_id,
        "round_id": rid,
        "contract_sha256": contract,
        "plan_call_id": str(env["plan_call_id"]).strip(),
        "plan_reply_sha256": str(env["plan_reply_sha256"]).lower(),
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
    if profile == "reviewer":
        _fail("reviewer_as_executor", "reviewer Profile 不得承担执行工作项。")
    return {"kind": "subagent", "agent_id": agent_id,
            "child_session_id": child, "profile_id": profile,
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
    """校验执行结果：绑定、结果摘要、证据文件、执行方元数据。"""
    env = _obj(envelope, "execution result")
    if str(env.get("schema") or "").strip() != RESULT_SCHEMA:
        _fail("schema_invalid", f"execution schema 必须是 {RESULT_SCHEMA}。")
    if not isinstance(dispatch, dict):
        _fail("dispatch_missing", "状态没有 native_dispatch；不能回填执行结果。")
    task_id, contract = _require_state_credential(state)
    if str(env.get("task_id") or "").strip() != task_id:
        _fail("task_mismatch", "执行结果 task_id 与当前任务不一致。")
    if str(env.get("contract_sha256") or "").strip() != contract:
        _fail("contract_mismatch", "执行结果 contract_sha256 与当前合同不一致。")
    rid = _text(env.get("round_id"), "execution.round_id")
    if rid != str(dispatch.get("round_id") or ""):
        _fail("round_mismatch", "执行结果 round_id 与当前 dispatch 不一致。")
    if str(env.get("contract_sha256") or "").strip() != str(
            dispatch.get("contract_sha256") or ""):
        _fail("contract_mismatch", "执行结果合同与当前 dispatch 不一致。")
    wid = _text(env.get("work_item_id"), "execution.work_item_id")
    did = _text(env.get("dispatch_id"), "execution.dispatch_id")
    items = dispatch.get("items")
    item = items.get(wid) if isinstance(items, dict) else None
    if not isinstance(item, dict):
        _fail("work_item_unknown", f"当前 dispatch 不含工作项 {wid}。")
    if str(item.get("dispatch_id") or "") != did:
        _fail("dispatch_mismatch",
              f"执行结果 dispatch_id={did} 与工作项 {wid} 的派发 ID 不一致。")
    # C2：执行结果必须携带完整计划/幂等绑定，且与当前 dispatch、已验证 SOL
    # 轮次三方一致。只有 task/round/contract 不足以证明结果属于本次派发。
    plan_call = _text(env.get("plan_call_id"), "execution.plan_call_id")
    plan_reply = _sha(env.get("plan_reply_sha256"), "execution.plan_reply_sha256")
    idem_key = _text(env.get("idempotency_key"), "execution.idempotency_key")
    if plan_call != str(dispatch.get("plan_call_id") or ""):
        _fail("plan_call_mismatch",
              "执行结果 plan_call_id 与当前 dispatch 不一致。")
    if plan_reply != str(dispatch.get("plan_reply_sha256") or "").lower():
        _fail("plan_reply_mismatch",
              "执行结果 plan_reply_sha256 与当前 dispatch 不一致。")
    latest = _latest_verified_round(state)
    if plan_call != str(latest.get("call_id") or ""):
        _fail("plan_call_mismatch",
              "执行结果 plan_call_id 与最近已验证 SOL 调用不一致。")
    if plan_reply != str(latest.get("reply_sha256") or "").lower():
        _fail("plan_reply_mismatch",
              "执行结果 plan_reply_sha256 与最近已验证 SOL 回复摘要不一致。")
    if idem_key != str(item.get("idempotency_key") or ""):
        _fail("idempotency_mismatch",
              f"执行结果 idempotency_key 与工作项 {wid} 的派发键不一致。")
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
    declared_sha = _sha(env.get("result_sha256"), "execution.result_sha256")
    actual_sha = envelope_sha256(env)
    if declared_sha != actual_sha:
        _fail("result_sha_mismatch",
              "执行结果 result_sha256 与信封内容不一致（可能被篡改）。")
    # 同一执行结果重复回填是幂等的；同一工作项的不同结果是否允许由 integration
    # 层按 failed/blocked 重试语义处理，协议层只检查终态。
    return {
        "schema": RESULT_SCHEMA,
        "task_id": task_id,
        "round_id": rid,
        "contract_sha256": contract,
        "plan_call_id": plan_call,
        "plan_reply_sha256": plan_reply,
        "dispatch_id": did,
        "idempotency_key": idem_key,
        "work_item_id": wid,
        "status": status,
        "summary": summary,
        "result": result,
        "evidence": evidence,
        "executor": executor,
        "timing": timing,
        "usage": usage,
        "result_sha256": declared_sha,
    }


def _validate_files_inspected(raw: object, *, check_files: bool) -> list[str]:
    items = _list(raw, "files_inspected")
    if not items:
        _fail("review_inspection_missing",
              "Reviewer 必须列出至少一个实际检查过的文件；不能只复述执行方自评。")
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


def _is_readonly_prefix(segment: str) -> bool:
    for prefix in _READONLY_COMMAND_PREFIXES:
        if prefix.endswith(" "):
            if segment == prefix.rstrip() or segment.startswith(prefix):
                return True
        elif segment == prefix or segment.startswith(prefix + " "):
            return True
    return False


def _split_readonly_segments(cmd: str, name: str) -> list[str]:
    """把顶层 `;` / `|` 拆成独立命令段；引号内的分隔符保持原样。

    只做拆分与拒绝，不做 shell 展开：`&&`、重定向、反引号、`$`、换行和未闭合
    引号都直接失败关闭。拆分后的每一段仍由 `_validate_commands` 逐段套用只读
    前缀与破坏性参数校验。
    """
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    idx = 0
    while idx < len(cmd):
        ch = cmd[idx]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
                buf.append(ch)
                idx += 1
                continue
            if ch == "\\":
                if idx + 1 >= len(cmd):
                    _fail("review_command_not_readonly",
                          f"{name} 以未转义的反斜杠结尾：{cmd}")
                nxt = cmd[idx + 1]
                if nxt in "\n\r":
                    _fail("review_command_not_readonly",
                          f"{name} 使用反斜杠续行，拒绝隐藏命令边界：{cmd}")
                buf.append(ch)
                buf.append(nxt)
                idx += 2
                continue
            if ch in (";", "|"):
                if ch == "|" and idx + 1 < len(cmd) and cmd[idx + 1] == "|":
                    _fail("review_command_not_readonly",
                          f"{name} 含 `||` 控制操作符：{cmd}")
                segment = "".join(buf).strip()
                if not segment:
                    _fail("review_command_not_readonly",
                          f"{name} 含空命令片段（多余分隔符）：{cmd}")
                segments.append(segment)
                buf = []
                idx += 1
                continue
            if ch in _COMMAND_SHELL_META:
                _fail("review_command_not_readonly",
                      f"{name} 含 shell 元字符 {ch!r}，可能改变工作区：{cmd}")
            buf.append(ch)
            idx += 1
            continue
        if quote == "'":
            if ch in "\n\r":
                _fail("review_command_not_readonly",
                      f"{name} 的单引号字符串含换行：{cmd}")
            buf.append(ch)
            if ch == "'":
                quote = None
            idx += 1
            continue
        # 双引号内仍禁止命令替换；反斜杠只转义下一个字符（不含换行）。
        if ch in "\n\r":
            _fail("review_command_not_readonly",
                  f"{name} 的双引号字符串含换行：{cmd}")
        if ch == "\\":
            if idx + 1 >= len(cmd):
                _fail("review_command_not_readonly",
                      f"{name} 以未转义的反斜杠结尾：{cmd}")
            nxt = cmd[idx + 1]
            if nxt in "\n\r":
                _fail("review_command_not_readonly",
                      f"{name} 使用反斜杠续行，拒绝隐藏命令边界：{cmd}")
            buf.append(ch)
            buf.append(nxt)
            idx += 2
            continue
        if ch in "`$":
            _fail("review_command_not_readonly",
                  f"{name} 含命令替换 {ch!r}：{cmd}")
        buf.append(ch)
        if ch == '"':
            quote = None
        idx += 1
    if quote is not None:
        _fail("review_command_not_readonly",
              f"{name} 的引号未闭合：{cmd}")
    segment = "".join(buf).strip()
    if not segment:
        _fail("review_command_not_readonly",
              f"{name} 为空或只有空白：{cmd}")
    segments.append(segment)
    return segments


def _validate_commands(raw: object) -> list[str]:
    items = _list(raw or [], "commands_run")
    out: list[str] = []
    for idx, value in enumerate(items, 1):
        cmd = _text(value, f"commands_run[{idx}]")
        name = f"commands_run[{idx}]"
        for segment in _split_readonly_segments(cmd, name):
            if not _is_readonly_prefix(segment):
                _fail("review_command_not_readonly",
                      f"{name} 的片段不在只读命令白名单：{segment}（原命令：{cmd}）")
            if _COMMAND_DENY_RE.search(segment):
                _fail("review_command_not_readonly",
                      f"{name} 的片段含破坏性参数：{segment}（原命令：{cmd}）")
            if segment.startswith("sort ") and _SORT_WRITE_RE.search(segment):
                _fail("review_command_not_readonly",
                      f"{name} 的 sort 含写文件/执行外部程序参数：{segment}")
        # 保留原始命令文本：review 信封的 result_sha256 绑定的是 Reviewer 原始输出。
        out.append(cmd)
    return out


def _validate_findings(raw: object, *, check_files: bool) -> list[dict]:
    items = _list(raw or [], "findings")
    out: list[dict] = []
    for idx, value in enumerate(items, 1):
        obj = _obj(value, f"findings[{idx}]")
        severity = _text(obj.get("severity"), f"findings[{idx}].severity").lower()
        if severity not in FINDING_SEVERITIES:
            _fail("review_finding_invalid",
                  f"findings[{idx}].severity 只能是 {', '.join(FINDING_SEVERITIES)}。")
        summary = _text(obj.get("summary"), f"findings[{idx}].summary")
        file = str(obj.get("file") or "").strip()
        evidence = str(obj.get("evidence") or "").strip()
        if severity in ("blocking", "major"):
            if not file or not evidence:
                _fail("review_finding_invalid",
                      f"{severity} finding 必须给出 file 与 evidence（具体证据）。")
        if file and check_files and os.path.isabs(file) and not os.path.exists(file):
            _fail("review_finding_invalid",
                  f"findings[{idx}].file 不存在：{file}")
        line = obj.get("line")
        if line is not None:
            line = _int(line, f"findings[{idx}].line", minimum=1)
        out.append({"severity": severity, "summary": summary,
                    "file": file, "line": line, "evidence": evidence})
    return out


def _validate_reviewer(reviewer: object) -> dict:
    obj = _obj(reviewer, "reviewer")
    kind = _text(obj.get("kind") or "subagent", "reviewer.kind").lower()
    if kind != "subagent":
        _fail("reviewer_invalid", "Reviewer 必须是独立子代理（kind=subagent）。")
    agent_id = _text(obj.get("agent_id"), "reviewer.agent_id")
    child = _text(obj.get("child_session_id"), "reviewer.child_session_id")
    profile = _text(obj.get("profile_id"), "reviewer.profile_id")
    if profile != "reviewer":
        _fail("reviewer_profile_invalid",
              f"Reviewer 必须使用专用 reviewer Profile（实际 {profile!r}）。")
    model = _text(obj.get("model_reported"), "reviewer.model_reported")
    return {
        "kind": "subagent",
        "agent_id": agent_id,
        "child_session_id": child,
        "profile_id": profile,
        "model_reported": model,
    }


def validate_review_result(envelope: object, dispatch: dict | None, execution: dict,
                           state: dict, *, check_files: bool = True) -> dict:
    """校验独立复核：绑定执行结果 SHA、Reviewer 独立性、只读性与 PASS 门槛。"""
    env = _obj(envelope, "review result")
    if str(env.get("schema") or "").strip() != REVIEW_SCHEMA:
        _fail("schema_invalid", f"review schema 必须是 {REVIEW_SCHEMA}。")
    if not isinstance(dispatch, dict):
        _fail("dispatch_missing", "状态没有 native_dispatch；不能回填复核结果。")
    task_id, contract = _require_state_credential(state)
    if str(env.get("task_id") or "").strip() != task_id:
        _fail("task_mismatch", "复核结果 task_id 与当前任务不一致。")
    if str(env.get("contract_sha256") or "").strip() != contract:
        _fail("contract_mismatch", "复核结果 contract_sha256 与当前合同不一致。")
    rid = _text(env.get("round_id"), "review.round_id")
    if rid != str(dispatch.get("round_id") or ""):
        _fail("round_mismatch", "复核结果 round_id 与当前 dispatch 不一致。")
    wid = _text(env.get("work_item_id"), "review.work_item_id")
    did = _text(env.get("dispatch_id"), "review.dispatch_id")
    items = dispatch.get("items")
    item = items.get(wid) if isinstance(items, dict) else None
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
    if str(executor.get("profile_id") or "") == "reviewer":
        _fail("reviewer_as_executor", "执行方不得使用 reviewer Profile。")
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
    if env.get("g4_eligible") is not False:
        _fail("g4_eligible_forbidden",
              "普通原生复核的 g4_eligible 必须显式为 false；不得冒充 G4。")
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
    return {
        "schema": REVIEW_SCHEMA,
        "task_id": task_id,
        "round_id": rid,
        "contract_sha256": contract,
        "dispatch_id": did,
        "work_item_id": wid,
        "execution_result_sha256": exec_sha,
        "reviewer": reviewer,
        "verdict": verdict,
        "findings": findings,
        "files_inspected": files,
        "commands_run": commands,
        "changed_files": [],
        "unverified": unverified,
        "residual_risks": residual,
        "g4_eligible": False,
        "result_sha256": declared_sha,
    }

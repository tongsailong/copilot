#!/usr/bin/env python3
"""从 ZCode 实际运行记录生成 Native Bridge 执行/复核信封。

本脚本不派发 Agent、不调用模型；除 `review-context --create-attempt` 会让治理层
写入一条不可变 review attempt 外，不写任务状态。它只做一件事：把**宿主已经
产生**的原生运行记录（Agent 的 `metadata.json`、rollout 模型记录、实际证据
文件、Reviewer 的原始结论）转换成 `copilot-native-result/v2` /
`copilot-native-review/v2` 信封，并由脚本自己计算所有身份绑定、证据摘要和
`result_sha256`。

为什么需要它
------------
主代理如果手工拼信封，就可能把 Agent ID、Child Session ID、模型名、证据
SHA-256 或计划绑定写错/编造。这里把这些字段从实际记录中提取，并对不上就
失败关闭：

* 执行方身份只来自 `agent-*/metadata.json`；
* 实际模型来自宿主持久化记录（Executor 可回退 rollout；Reviewer 只读 DB）；
* 证据 SHA-256 由脚本读取证据文件计算，不接受调用方传入；
* `plan_id` / `plan_sha256` / `dispatch_id` / `idempotency_key` 只从本地计划
  与 dispatch 复制；
* 复核身份只来自 Reviewer 的 metadata，Reviewer 原始结论只提供
  verdict/findings/files_inspected 等判断内容；
* 复核实际调用的工具只来自宿主 `tool_usage`/`part`：Reviewer 无 Bash 权限，
  `commands_run` 恒为空，调用计数必须与 metadata 相符；
* 官方 G4 资格由 `g4_attest` 对新链宿主记录独立核验；本脚本生成的
  `g4_eligible` 只是信封结构预检，Reviewer 不能自述正式资格。

仍然不是密码学证明
------------------
这些记录仍可能被能写本机文件的人伪造；本脚本保证的是“不靠主代理记忆或手抄
身份”，不是宿主签名。G4 仍是本地可信度最高的原生复核，不等于外部签名。

用法
----
    native_agent.py execution --state STATE --dispatch DISPATCH.json \
      --work-item T1 --metadata .../metadata.json \
      --status done --summary "一句话结果" --result-file result.md \
      --evidence E1=/abs/raw.log [--evidence E2=/abs/test.log] \
      --output T1.execution.json

    native_agent.py review --state STATE --dispatch DISPATCH.json \
      --execution T1.execution.json --metadata .../metadata.json \
      --attempt RA... --input reviewer.raw.json --output T1.review.json

    native_agent.py review-context --state STATE --dispatch DISPATCH.json \
      --execution T1.execution.json --work-item T1 --create-attempt \
      --output T1.context.json

前两个信封子命令在写文件前都会调用 `native_bridge` 的正式校验；校验失败不会
产生输出文件。`review-context --create-attempt` 会写入治理层生成的复核 attempt。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import governance as G
import host_records
import review_output  # noqa: E402
import native_bridge as NB  # noqa: E402

EXIT_OK, EXIT_USAGE = 0, 2
MAX_JSON_BYTES = 4 << 20
MAX_RESULT_CHARS = 200_000
_EVIDENCE_RE = re.compile(r"^(E[1-9][0-9]{0,3})=(.+)$")
_MODEL_SCAN_LINES = 200
# Reviewer Profile 只启用这三个只读工具；任何其他工具（包括 Bash）出现在
# 实际记录里都说明复核越界（或 Profile 被改写），必须拒绝生成可接纳的复核信封。
_REVIEW_TOOL_NAMES = frozenset({"Read", "Grep", "Glob"})


class AgentRecordError(Exception):
    """运行记录缺失、格式不符或与状态不一致。"""


def _load_json(path: str) -> object:
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise AgentRecordError(f"文件不存在：{path}")
    if os.path.getsize(path) > MAX_JSON_BYTES:
        raise AgentRecordError(f"文件过大（>{MAX_JSON_BYTES} 字节）：{path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise AgentRecordError(f"无法解析 JSON {path}：{exc}") from None


def _read_text(path: str, *, limit: int = MAX_RESULT_CHARS) -> str:
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise AgentRecordError(f"文件不存在或不是普通文件：{path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read(limit + 1)
    except OSError as exc:
        raise AgentRecordError(f"无法读取 {path}：{exc}") from None
    if len(text) > limit:
        raise AgentRecordError(f"文件超过 {limit} 字符：{path}")
    return text


def _text(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AgentRecordError(f"运行记录缺少 {name}。")
    return text


def _metadata_path(raw: str) -> str:
    path = os.path.abspath(os.path.expanduser(raw))
    if os.path.isdir(path):
        path = os.path.join(path, "metadata.json")
    if not os.path.isfile(path):
        raise AgentRecordError(f"Agent metadata 不存在：{path}")
    return path


def _load_metadata(raw: str) -> dict:
    path = _metadata_path(raw)
    data = _load_json(path)
    if not isinstance(data, dict):
        raise AgentRecordError(f"Agent metadata 顶层必须是对象：{path}")
    agent_id = _text(data.get("agentId"), "metadata.agentId")
    child = _text(data.get("childSessionId"), "metadata.childSessionId")
    profile = _text(data.get("profileId"), "metadata.profileId")
    snapshot = data.get("profileSnapshot")
    if not isinstance(snapshot, dict):
        raise AgentRecordError("metadata.profileSnapshot 缺失；无法证明实际加载的 Profile。")
    snapshot_name = _text(snapshot.get("name"), "metadata.profileSnapshot.name")
    if snapshot_name != profile:
        raise AgentRecordError(
            f"metadata.profileId={profile!r} 与 profileSnapshot.name="
            f"{snapshot_name!r} 不一致；拒绝猜测实际 Profile。")
    recorded_file = str(data.get("metadataFile") or "").strip()
    if recorded_file and os.path.abspath(os.path.expanduser(recorded_file)) != path:
        raise AgentRecordError(
            "metadata.metadataFile 与传入路径不一致；拒绝使用被复制的记录。")
    if not isinstance(data.get("status"), str) or not data["status"].strip():
        raise AgentRecordError("metadata.status 缺失；无法确认 Agent 实际结束状态。")
    return {
        "path": path,
        "agent_id": agent_id,
        "child_session_id": child,
        "profile_id": profile,
        "profile_snapshot": snapshot,
        "status": data["status"].strip(),
        "data": data,
    }


def _rollout_path(meta: dict, override: str = "") -> str:
    child = meta["child_session_id"]
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".zcode", "cli", "rollout",
                        f"model-io-{child}.jsonl")


def _rollout_model(meta: dict, override: str = "") -> str:
    """从 rollout 读取实际 provider/model；不信任调用方自报的模型名。"""
    child = meta["child_session_id"]
    path = _rollout_path(meta, override)
    if not os.path.isfile(path):
        raise AgentRecordError(
            f"找不到 Agent {child} 的 rollout 模型记录：{path}；"
            "没有实际模型记录时不得填写 model_reported。")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for idx, line in enumerate(fh):
                if idx >= _MODEL_SCAN_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                session = str(row.get("sessionId") or "").strip()
                if session and session != child:
                    raise AgentRecordError(
                        f"rollout {path} 的 sessionId={session!r} 与 Child Session "
                        f"{child!r} 不一致；拒绝把别的会话模型当作本 Agent 模型。")
                model = row.get("model")
                if not isinstance(model, dict):
                    continue
                provider = str(model.get("providerId") or "").strip()
                model_id = str(model.get("modelId") or "").strip()
                if provider and model_id:
                    return f"{provider}/{model_id}"
    except OSError as exc:
        raise AgentRecordError(f"无法读取 rollout {path}：{exc}") from None
    raise AgentRecordError(
        f"rollout {path} 前 {_MODEL_SCAN_LINES} 行没有 providerId/modelId；"
        "无法确认实际模型。")


def _absolute_file(value: str) -> str:
    if not value or "\n" in value:
        return ""
    path = os.path.abspath(os.path.expanduser(value))
    return path if os.path.isfile(path) else ""


_PAYLOAD_PATH_KEYS = ("file_path", "path", "pattern")


def _merge_inspected_files(declared: object,
                           calls: list[tuple[str, str, dict]]) -> list[str]:
    """合并自报与宿主实际的读取清单。

    自报文件只在真实存在时保留；宿主读取（Read/Grep/Glob 的路径参数）一律并入。
    G4 的证据覆盖仍单独按实际调用计算，这份清单只作说明，不构成放行依据。
    """
    out: list[str] = []
    if isinstance(declared, list):
        for item in declared:
            path = _absolute_file(str(item or "").strip())
            if path and path not in out:
                out.append(path)
    for _tool, _command, payload in calls:
        candidates: list[str] = []
        for key in _PAYLOAD_PATH_KEYS:
            value = payload.get(key)
            if isinstance(value, str):
                path = _absolute_file(value)
                if path:
                    candidates.append(path)
        for path in candidates:
            if path not in out:
                out.append(path)
    return out


def _declared_files_unverified(declared: object,
                               calls: list[tuple[str, str, dict]]) -> list[str]:
    """自报读过、但实际调用里找不到对应访问的文件（记录用，不阻断复核）。"""
    if not isinstance(declared, list):
        return []
    blobs = _call_blobs(calls)
    missing: list[str] = []
    for item in declared:
        path = str(item or "").strip()
        if not path:
            continue
        if not any(_mentions_path(path, blob) for blob in blobs):
            missing.append(path)
    return missing


def _usage(meta: dict) -> dict:
    data = meta["data"]
    raw = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    out: dict[str, int] = {}
    mapping = {
        "total_tokens": ("totalTokens", "total_tokens"),
        "input_tokens": ("inputTokens", "input_tokens"),
        "output_tokens": ("outputTokens", "output_tokens"),
    }
    for target, keys in mapping.items():
        for key in keys:
            value = raw.get(key, data.get(key))
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                out[target] = value
                break
    tool_count = data.get("totalToolUseCount")
    if isinstance(tool_count, int) and not isinstance(tool_count, bool) and tool_count >= 0:
        out["tool_use_count"] = tool_count
    return out


def _timing(meta: dict) -> dict:
    data = meta["data"]
    duration = data.get("totalDurationMs")
    if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
        raise AgentRecordError("metadata.totalDurationMs 缺失或非法；无法记录实际耗时。")
    return {
        "started_at": str(data.get("createdAt") or "").strip(),
        "finished_at": str(data.get("completedAt") or data.get("updatedAt") or "").strip(),
        "duration_ms": duration,
    }


def _parse_evidence(raw_items: list[str]) -> list[dict]:
    if not raw_items:
        raise AgentRecordError("至少提供一条 --evidence E1=/abs/path。")
    out: list[dict] = []
    seen: set[str] = set()
    for raw in raw_items:
        match = _EVIDENCE_RE.match(str(raw or "").strip())
        if not match:
            raise AgentRecordError(f"--evidence 格式必须是 E1=/abs/path：{raw!r}")
        eid, path = match.group(1), os.path.abspath(os.path.expanduser(match.group(2)))
        if eid in seen:
            raise AgentRecordError(f"证据 id {eid} 重复。")
        seen.add(eid)
        if path.startswith(("/dev/", "/proc/", "/sys/")):
            raise AgentRecordError(f"证据必须是普通文件，不能是设备/管道：{path}")
        if not os.path.isfile(path):
            raise AgentRecordError(f"证据文件不存在或不是普通文件：{path}")
        out.append({
            "id": eid,
            "file": path,
            "sha256": NB.sha256_file(path),
            "kind": "file",
            "note": "",
        })
    return out


def _dispatch_for_state(state: dict, dispatch_path: str) -> tuple[dict, dict]:
    env = _load_json(dispatch_path)
    if not isinstance(env, dict):
        raise AgentRecordError("dispatch 文件顶层必须是对象。")
    try:
        NB.validate_dispatch(env, state)
    except NB.BridgeError as exc:
        raise AgentRecordError(
            f"dispatch 与当前状态不一致 [{exc.code}]：{exc}") from None
    native = state.get("native_dispatch")
    if not isinstance(native, dict):
        raise AgentRecordError("状态没有 native_dispatch；请先让 drive 生成派发。")
    return env, native


def _find_action(dispatch_env: dict, work_item: str) -> dict:
    for action in dispatch_env.get("actions") or []:
        if isinstance(action, dict) and str(action.get("work_item_id") or "") == work_item:
            return action
    raise AgentRecordError(f"dispatch 不含工作项 {work_item}。")


def bind_execution(args) -> dict:
    """在执行方启动前固化 execution binding，返回 nonce 供写入 Executor prompt。"""
    state_path = os.path.abspath(args.state)
    binding = G.create_execution_binding(
        state_path, work_item_id=str(args.work_item),
        test_commands=list(getattr(args, "test_command", []) or []),
        workspace=str(getattr(args, "workspace", "") or "") or os.getcwd(),
        require_test_result=bool(getattr(args, "require_test_result", False)),
        require_artifact_integrity=bool(
            getattr(args, "require_artifact_integrity", False)))
    return {
        "schema": "copilot-execution-binding-result/v1",
        "task_id": binding["task_id"],
        "binding_id": binding["binding_id"],
        "nonce": binding["nonce"],
        "work_item_id": binding["work_item_id"],
        "workspace": binding["workspace"],
        "test_commands": binding["test_commands"],
        "require_test_result": binding["require_test_result"],
        "require_artifact_integrity": binding["require_artifact_integrity"],
        "created_at": binding["created_at"],
        "prompt_requirement": (
            "Executor 的 prompt 必须包含该 nonce 原文；会话创建时间必须晚于 "
            "binding.created_at。"),
    }


def build_execution(args) -> dict:
    state = G.load_state(os.path.abspath(args.state))
    dispatch_env, native = _dispatch_for_state(state, args.dispatch)
    action = _find_action(dispatch_env, args.work_item)
    meta = _load_metadata(args.metadata)
    if not NB.reviewer_policy_problems(
            NB.normalize_profile_policy(meta["profile_snapshot"])):
        raise AgentRecordError(
            f"执行方 Profile {meta['profile_id']!r} 的有效工具策略等价于只读复核"
            "（tools 仅 Read/Grep/Glob）；只读 Profile 不得承担工程执行。")
    if meta["status"] != "completed":
        raise AgentRecordError(
            f"Executor metadata.status={meta['status']!r}，不是 completed；"
            "不得把未完成的运行记录当作正式执行结果。")
    rollout_path = _rollout_path(meta, args.model_rollout)
    if os.path.isfile(rollout_path):
        model = _rollout_model(meta, args.model_rollout)
        model_source = "rollout"
    else:
        selection = host_records.model_selection(
            str(getattr(args, "db", "") or "") or os.path.expanduser(
                "~/.zcode/cli/db/db.sqlite"), meta["child_session_id"])
        if not selection:
            raise AgentRecordError(
                "[executor_model_missing] rollout 已清理且宿主 session_entry "
                "没有模型记录；无法确认执行方实际模型。")
        model = f"{selection['provider_id']}/{selection['model_id']}"
        model_source = selection["source"]
    if args.result_file and args.result_text:
        raise AgentRecordError("--result-file 与 --result-text 只能提供一个。")
    if args.result_file:
        result = _read_text(args.result_file)
    else:
        result = str(args.result_text or "")
    result = result.strip()
    if not result:
        raise AgentRecordError("执行结果正文为空；请提供 --result-file 或 --result-text。")
    summary = _text(args.summary, "--summary")
    binding_id = str(getattr(args, "binding", "") or "").strip()
    if not binding_id and str(state.get("risk") or "").lower() == "high":
        raise AgentRecordError(
            "[execution_binding_required] 高风险任务必须先运行 bind-execution，"
            "并让 Executor prompt 携带 nonce；不能用旧会话生成执行信封。")
    binding_env = {}
    if binding_id:
        binding = G.find_execution_binding(state, binding_id)
        if binding is None:
            raise AgentRecordError(
                f"状态中没有 execution binding {binding_id!r}；先运行 bind-execution。")
        binding_env = {
            "binding_id": str(binding["binding_id"]),
            "nonce": str(binding["nonce"]),
            "created_at": str(binding["created_at"]),
            "workspace": str(binding["workspace"]),
            "test_commands": [str(item) for item in
                              binding.get("test_commands") or []],
        }
    env = {
        "schema": NB.RESULT_SCHEMA,
        "task_id": state["task_id"],
        "contract_sha256": state["contract_sha256"],
        "plan_id": dispatch_env["plan_id"],
        "plan_sha256": dispatch_env["plan_sha256"],
        "iteration_id": dispatch_env.get("iteration_id") or "",
        "dispatch_id": action["dispatch_id"],
        "idempotency_key": action["idempotency_key"],
        "work_item_id": action["work_item_id"],
        "status": args.status,
        "summary": summary,
        "result": result,
        "evidence": _parse_evidence(args.evidence),
        "executor_metadata_path": meta["path"],
        "executor": {
            "kind": "subagent",
            "agent_id": meta["agent_id"],
            "child_session_id": meta["child_session_id"],
            "profile_id": meta["profile_id"],
            "profile": NB.normalize_profile_policy(meta["profile_snapshot"]),
            "model_reported": model,
            "model_source": model_source,
        },
        "timing": _timing(meta),
        "usage": _usage(meta),
    }
    commands = [str(item).strip() for item in
                (getattr(args, "test_command", None) or [])
                if str(item).strip()]
    artifacts = [str(item).strip() for item in
                 (getattr(args, "test_artifact", None) or [])
                 if str(item).strip()]
    known = {item["id"] for item in env["evidence"]}
    for eid in artifacts:
        if eid not in known:
            raise AgentRecordError(
                f"--test-artifact {eid} 不在本次 --evidence 中；测试产物必须是"
                "本次执行登记的文件证据。")
    workspace = str(getattr(args, "workspace", "") or "").strip() \
        or os.getcwd()
    excluded = list(G._evidence_paths(state)) + list(G.governance_paths(args.state))
    for item in env["evidence"]:
        if isinstance(item, dict) and item.get("file"):
            excluded.append(str(item["file"]))
    for extra in (getattr(args, "output", ""), getattr(args, "result_file", "")):
        if str(extra or "").strip():
            excluded.append(os.path.abspath(os.path.expanduser(str(extra))))
    snapshot = G.capture_code_snapshot(
        workspace, exclude_paths=excluded,
        base=str(getattr(args, "base", "") or ""))
    env["test_commands"] = commands
    env["test_artifacts"] = sorted(set(artifacts))
    env["execution_binding"] = binding_env
    env["code_scope_sha256"] = str(snapshot.get("digest") or "") \
        if snapshot.get("available") else ""
    env["result_sha256"] = NB.envelope_sha256(env)
    try:
        NB.validate_execution_result(env, native, state)
    except NB.BridgeError as exc:
        raise AgentRecordError(
            f"生成的信封未通过正式校验 [{exc.code}]：{exc}") from None
    return env


def _review_input(path: str) -> dict:
    """读取调用方保存的 Reviewer 结论副本（只用于与宿主实际输出交叉核对）。"""
    data = _load_json(path)
    if not isinstance(data, dict):
        raise AgentRecordError("Reviewer 原始结论顶层必须是对象。")
    return _review_fields(data)


REVIEW_FIELDS = ("verdict", "findings", "files_inspected", "commands_run",
                 "changed_files", "unverified", "residual_risks")


def _review_fields(data: dict) -> dict:
    out = {key: data.get(key) for key in REVIEW_FIELDS if key in data}
    if "verdict" not in out:
        raise AgentRecordError("Reviewer 原始结论缺少 verdict。")
    return out


def _extract_review_json(text: str, path: str) -> dict:
    """按统一 review_output 契约解析宿主输出；失败关闭并保留错误码。"""
    try:
        return review_output.parse_review_output(text)
    except review_output.ReviewOutputError as exc:
        code = exc.code
        detail = exc.detail or str(exc)
        if code == "review_output_ambiguous":
            raise AgentRecordError(
                f"[review_output_ambiguous] {path}：{detail}；无法确定正式结论。"
            ) from None
        if code == "review_output_outside_text":
            raise AgentRecordError(
                f"[review_output_outside_text] {path}：{detail}。") from None
        raise AgentRecordError(
            f"[review_output_unparsable] {path}：{code} {detail}；"
            "请让 Reviewer 以单个 JSON 对象输出结论。") from None


def _review_host_output(meta: dict, db: str = "") -> tuple[dict, dict]:
    """读取 Reviewer 实际输出；outputFile 被清理时回退到持久化 message/part。

    结论只能以宿主记录为准：调用方 `--input` 是可复制的文件。`outputFile` 由宿主
    在 Agent 结束时写入，且必须与 metadata.json 同目录；文件缺失时只接受 DB 中
    该 child session 最后一条 assistant 文本（文本 SHA 可复算），两者都存在时
    必须一致。无法取得时失败关闭，不用调用方副本代替。
    """
    raw = str((meta.get("data") or {}).get("outputFile") or "").strip()
    path = os.path.abspath(os.path.expanduser(raw)) if raw else ""
    db = db or os.path.expanduser("~/.zcode/cli/db/db.sqlite")
    file_present = bool(path) and os.path.isfile(path)
    if file_present and os.path.dirname(os.path.realpath(path)) != \
            os.path.dirname(os.path.realpath(meta["path"])):
        raise AgentRecordError(
            f"[review_output_misplaced] outputFile 不在 metadata 同目录：{path}；"
            "拒绝使用外部（可能被替换的）文件作为复核结论。")
    db_output = host_records.assistant_output(
        db, str(meta.get("child_session_id") or ""))
    if file_present:
        text = _read_text(path)
        info = {"source": "outputFile", "path": path,
                "sha256": NB.sha256_text(text),
                "message_id": "", "part_ids": [], "db_sha256": ""}
        if db_output:
            if db_output["sha256"] != info["sha256"]:
                raise AgentRecordError(
                    "[review_output_db_mismatch] outputFile 与宿主 message/part "
                    f"文本不一致：file={info['sha256'][:16]} "
                    f"db={db_output['sha256'][:16]}；拒绝取证。")
            info.update({"message_id": db_output["message_id"],
                         "part_ids": db_output["part_ids"],
                         "db_sha256": db_output["sha256"]})
    elif db_output:
        text = db_output["text"]
        info = {"source": "db.message.part", "path": path,
                "sha256": db_output["sha256"],
                "message_id": db_output["message_id"],
                "part_ids": db_output["part_ids"],
                "db_sha256": db_output["sha256"]}
    else:
        raise AgentRecordError(
            "[review_output_missing] metadata/outputFile 与宿主 message/part 都没有 "
            "Reviewer 最终输出；不得用调用方副本代替正式复核结论。")
    data = _extract_review_json(text.strip(), info["path"] or "db:message/part")
    if not isinstance(data, dict):
        raise AgentRecordError("Reviewer 实际输出顶层必须是对象。")
    return data, info


def _bind_review_output(data: dict, execution: dict, dispatch_env: dict,
                        state: dict) -> None:
    """宿主输出里必须带有本次执行对象绑定；缺失或指向别处一律拒绝。"""
    expected = {
        "task_id": str(state.get("task_id") or ""),
        "plan_id": str(dispatch_env.get("plan_id") or ""),
        "dispatch_id": str(execution.get("dispatch_id") or ""),
        "work_item_id": str(execution.get("work_item_id") or ""),
        "execution_result_sha256": str(execution.get("result_sha256") or ""),
    }
    for key in ("execution_result_sha256", "work_item_id"):
        if not str(data.get(key) or "").strip():
            raise AgentRecordError(
                f"[review_output_unbound] Reviewer 实际输出缺少 {key}：无法证明复核"
                "对象就是本次执行结果。")
    schema = str(data.get("schema") or "").strip()
    if schema and schema != NB.REVIEW_SCHEMA:
        raise AgentRecordError(
            f"[review_output_unbound] 宿主输出 schema={schema!r}，不是 {NB.REVIEW_SCHEMA}。")
    for key, want in expected.items():
        have = str(data.get(key) or "").strip()
        if have and have != want:
            raise AgentRecordError(
                f"[review_output_binding_mismatch] 宿主输出 {key}={have!r} 与本次复核"
                f"对象 {want!r} 不一致；拒绝把其他任务的复核结论贴到当前执行结果。")


def _assert_input_matches_host(fields: dict, host: dict) -> None:
    """调用方副本与宿主实际输出必须逐字段一致；不一致即拒绝（不改用副本）。"""
    if not fields:
        return
    for key in REVIEW_FIELDS:
        if key not in host:
            continue
        left = json.dumps(fields.get(key), ensure_ascii=False, sort_keys=True)
        right = json.dumps(host.get(key), ensure_ascii=False, sort_keys=True)
        if left != right:
            raise AgentRecordError(
                f"[review_input_mismatch] 调用方 --input 的 {key} 与宿主实际输出不一致；"
                "复核结论只以宿主记录为准，拒绝使用被改写的副本。")


_TOKEN_SPLIT_RE = re.compile(r"[\s'\"`=,;:()\[\]{}<>|]+")


def _path_components(value: str) -> list[str]:
    """拆路径组件：去掉空段与 `.`（两者都不改变指向），保留 `..` 供调用方拒绝。"""
    return [part for part in value.split("/") if part not in ("", ".")]


def _mentions_path(path: str, blob: str) -> bool:
    """blob 是否在**路径组件边界**上引用了 path。

    单纯子串匹配会把 `x.py.bak`、`old/x.py` 之类的读取记录算成读过 `x.py`。
    先保留声明与读取各自的绝对/相对属性，再按形式比较：
    1. 读取是绝对路径：声明也是绝对路径时要求组件**完全相同**（`/repo-a/docs/x`
       不能由 `/foreign/repo-a/docs/x` 满足）；声明是相对路径时允许读取以它结尾
       （工作区相对路径映射到绝对读取）。
    2. 读取是相对路径：允许它以完整声明路径结尾；也允许它是声明的**直接**相对
       形式（组件更少且直接父目录同名）。`..` 无法在无工作目录时核实，一律不算。
    因此目录插层（`docs/archive/report.md`）、不同绝对根（`/repo-b/docs/report.md`）、
    `x.py.bak` 与 `..` 形式都不命中；`./docs/report.md` 与 `docs/report.md` 等价。
    只按单个路径 token 比较，不把「父目录名 + 文件名分处两个参数」拼成一次读取。
    """
    claim_abs = path.strip().startswith("/")
    claim = _path_components(path)
    if not claim:
        return False
    parent = claim[-2] if len(claim) >= 2 else ""
    for token in _TOKEN_SPLIT_RE.split(blob):
        read_abs = token.strip().startswith("/")
        parts = _path_components(token)
        if not parts or ".." in parts:
            continue
        if read_abs:
            if claim_abs:
                if parts == claim:
                    return True
            elif len(parts) >= len(claim) and parts[-len(claim):] == claim:
                return True
            continue
        # 相对读取：以声明结尾，或是组件更少的直接相对形式。
        if len(parts) >= len(claim) and parts[-len(claim):] == claim:
            return True
        if len(parts) >= 2 and len(parts) < len(claim) and claim[-len(parts):] == parts:
            if not parent or parts[-2] == parent:
                return True
    return False


def _call_blobs(calls: list[tuple[str, str, dict]]) -> list[str]:
    return [json.dumps(payload, ensure_ascii=False)
            for _tool, _command, payload in calls]


def _db_review_tool_calls(meta: dict, db: str) -> list[tuple[str, str, dict]]:
    """从宿主 `tool_usage`/`part` 读取 Reviewer 工具调用。

    Reviewer 无 Bash 权限；缺失、计数不符或出现非只读工具时失败关闭。
    """
    child = meta["child_session_id"]
    total = meta["data"].get("totalToolUseCount")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise AgentRecordError(
            "[review_tool_count_missing] metadata.totalToolUseCount 缺失或非法；"
            "无法证明复核工具调用记录完整。")
    db = db or os.path.expanduser("~/.zcode/cli/db/db.sqlite")
    try:
        con = host_records.connect_ro(db)
    except (OSError, sqlite3.Error) as exc:
        raise AgentRecordError(
            f"[review_host_db_unavailable] 无法读取宿主数据库：{exc}") from None
    try:
        rows = con.execute(
            "select tool_call_id, tool_name from tool_usage "
            "where session_id=? order by started_at, rowid", (child,)).fetchall()
        parts = con.execute(
            "select data from part where session_id=?", (child,)).fetchall()
    except sqlite3.Error as exc:
        raise AgentRecordError(
            f"[review_host_db_unavailable] 读取工具记录失败：{exc}") from None
    finally:
        con.close()
    payloads: dict[str, dict] = {}
    for (raw,) in parts:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("type") != "tool":
            continue
        call_id = str(data.get("callID") or "")
        state = data.get("state") if isinstance(data.get("state"), dict) else {}
        payload = state.get("input") if isinstance(state.get("input"), dict) else {}
        if call_id:
            payloads[call_id] = payload
    calls: list[tuple[str, str, dict]] = []
    seen: set[str] = set()
    for call_id, name in rows:
        cid = str(call_id or "")
        tool = str(name or "").strip()
        if tool not in _REVIEW_TOOL_NAMES:
            raise AgentRecordError(
                f"[review_tool_not_allowed] 复核实际调用了未授权工具 {tool!r}；"
                f"Reviewer 只允许 {sorted(_REVIEW_TOOL_NAMES)}。")
        if cid:
            if cid in seen:
                raise AgentRecordError(
                    f"[review_rollout_duplicate_call] 宿主记录出现重复 tool call id {cid!r}。")
            seen.add(cid)
        payload = payloads.get(cid)
        if not isinstance(payload, dict):
            raise AgentRecordError(
                f"[review_toolcalls_invalid] 宿主 part 缺少 {tool} 的 input：{cid}")
        calls.append((tool, "", payload))
    if len(calls) != total:
        raise AgentRecordError(
            f"[review_toolcalls_incomplete] 宿主记录 {len(calls)} 次工具调用，但 "
            f"metadata.totalToolUseCount={total}；拒绝复核信封。")
    return calls


def _db_model(meta: dict, db: str) -> dict:
    db = db or os.path.expanduser("~/.zcode/cli/db/db.sqlite")
    selection = host_records.model_selection(db, meta["child_session_id"])
    if not selection:
        raise AgentRecordError(
            f"[review_model_missing] rollout 已清理且宿主 session_entry 没有模型记录："
            f"{meta['child_session_id']}；无法确认实际模型。")
    return selection

def _incremental_base(state: dict, path: str) -> dict:
    """读取已回填的基线复核记录（按 result_sha256 匹配状态）。"""
    data = _load_json(path)
    if not isinstance(data, dict):
        raise AgentRecordError(
            "--based-on 必须是已回填的复核信封 JSON 文件（完整对象）。")
    sha = str(data.get("result_sha256") or "").strip().lower()
    if not sha:
        raise AgentRecordError(
            "[incremental_base_missing] --based-on 文件缺少 result_sha256。")
    for record in state.get("native_reviews") or []:
        if isinstance(record, dict) and str(record.get("result_sha256") or "") == sha:
            if str(record.get("verdict") or "").strip().lower() != "pass" or \
                    record.get("g4_eligible") is not True:
                # 与 native_bridge 的增量校验同一前提：增量只用于高风险 G4
                # 链路，基线必须是可用的 G4 PASS；否则先做完整复核，避免
                # 生成一条注定在 ingest 被拒的信封。
                raise AgentRecordError(
                    "[incremental_base_ineligible] 基线复核不是可用的 G4 PASS"
                    " 结论，不能作为增量基础；请先做一次完整复核。")
            trust = G._review_trust_problems(record, state, check_target=False)
            if trust:
                # 旧版无 attempt 的 PASS、宿主记录被改写、attempt 未消费或
                # 证据文件漂移都会让基线不再可信；增量不能建立在不可信基线上。
                raise AgentRecordError(
                    "[incremental_base_ineligible] 基线复核未通过当前 G4 信任检查："
                    + ", ".join(trust)
                    + "；请对当前快照做一次完整复核，不要复用旧 PASS。")
            base_attempt = G.find_review_attempt(
                state, str(record.get("review_attempt_id") or ""))
            code_problems = G.code_scope_problems(
                base_attempt, state, check_current=True) if base_attempt else \
                ["native_review_code_scope_missing"]
            if code_problems:
                raise AgentRecordError(
                    "[incremental_code_changed] 基线复核之后工作区代码范围已变化（"
                    + ", ".join(code_problems)
                    + "）；增量复核只覆盖证据，不能覆盖源码变更，请做完整复核。")
            return record
    raise AgentRecordError(
        f"[incremental_base_missing] --based-on 指向的复核 {sha[:16]}… 尚未回填到本任务；"
        "先 ingest 基线复核，或改用完整复核。")


def _incremental_evidence(state: dict, base: dict) -> list[str]:
    """基线之后新增或内容变化的证据 id；没有覆盖快照时失败关闭。"""
    coverage = base.get("coverage")
    if not isinstance(coverage, dict) or not coverage:
        raise AgentRecordError(
            "[incremental_base_coverage_missing] 基线复核没有覆盖快照（旧版本记录）；"
            "请先做一次完整复核，之后的复核才能增量复用。")
    base_evidence = coverage.get("evidence") or {}
    current = NB.review_coverage(state)
    changed = sorted(eid for eid, sha in current["evidence"].items()
                     if base_evidence.get(eid) != sha)
    if not changed:
        raise AgentRecordError(
            "[incremental_without_change] 相对基线没有新增/变更证据；"
            "直接复用基线复核结论即可，不需要重复复核。")
    return changed


def build_review(args) -> dict:
    state = G.load_state(os.path.abspath(args.state))
    dispatch_env, native = _dispatch_for_state(state, args.dispatch)
    execution = _load_json(args.execution)
    if not isinstance(execution, dict):
        raise AgentRecordError("execution 文件顶层必须是对象。")
    schema = str(execution.get("schema") or "")
    if schema in NB.LEGACY_SCHEMAS:
        raise AgentRecordError(
            f"{schema} 属于 v1 SOL 通道，已退役；历史记录请用 legacy-status 查看。")
    if schema != NB.RESULT_SCHEMA:
        raise AgentRecordError(
            f"--execution 必须是 {NB.RESULT_SCHEMA} 信封。")
    exec_sha = str(execution.get("result_sha256") or "").strip().lower()
    if not exec_sha or NB.envelope_sha256(execution) != exec_sha:
        raise AgentRecordError(
            "execution.result_sha256 与信封内容不一致；复核不得绑定被改写的执行结果。")
    recorded = [
        rec for rec in (state.get("native_results") or [])
        if isinstance(rec, dict)
        and str(rec.get("result_sha256") or "") == exec_sha
    ]
    if not recorded:
        raise AgentRecordError(
            "execution 尚未入账；请先用 auto ingest 回填 execution，再生成复核。")
    if str(execution.get("work_item_id") or "") != str(args.work_item):
        raise AgentRecordError(
            f"execution 工作项 {execution.get('work_item_id')!r} 与 "
            f"--work-item {args.work_item!r} 不一致。")
    risk = str(state.get("risk") or "").strip().lower()
    attempt_id = str(getattr(args, "attempt", "") or "").strip()
    attempt: dict | None = None
    if attempt_id:
        attempt = G.find_review_attempt(state, attempt_id)
        if attempt is None:
            raise AgentRecordError(
                f"[review_attempt_foreign] 状态中没有复核 attempt {attempt_id!r}；"
                "不得把其他任务或其他尝试的信封贴到本任务。")
        if attempt.get("consumed"):
            raise AgentRecordError(
                f"[review_attempt_consumed] attempt {attempt_id} 已被消费；"
                "重试必须重新创建 attempt，不能复用旧 Reviewer 输出。")
        expected = {
            "task_id": str(state.get("task_id") or ""),
            "contract_sha256": str(state.get("contract_sha256") or ""),
            "plan_id": str(dispatch_env.get("plan_id") or ""),
            "plan_sha256": str(dispatch_env.get("plan_sha256") or "").lower(),
            "dispatch_id": str(execution.get("dispatch_id") or ""),
            "work_item_id": str(execution.get("work_item_id") or ""),
            "execution_result_sha256": exec_sha,
        }
        for key, want in expected.items():
            if str(attempt.get(key) or "").lower() != want.lower():
                raise AgentRecordError(
                    f"[review_attempt_binding_mismatch] attempt {attempt_id} 的 "
                    f"{key} 与本次复核对象不一致；不能把旧 attempt 套到新派发上。")
        if str(attempt.get("target_sha256") or "") != G.final_evidence_digest(state):
            raise AgentRecordError(
                f"[review_attempt_stale] attempt {attempt_id} 固化的目标摘要已过期；"
                "派发后代码/证据/条件发生变化，必须创建新的 attempt 并重新复核，"
                "不能用旧 Reviewer 输出重新封装。")
        code_problems = G.code_scope_problems(attempt, state, check_current=True)
        if code_problems:
            raise AgentRecordError(
                f"[{code_problems[0]}] attempt {attempt_id} 的代码范围与当前工作区"
                "不一致或不可读取；Reviewer 实际审查的代码无法与派发快照对应，"
                "必须新建 attempt 重新复核。")
    elif risk == "high":
        raise AgentRecordError(
            "[review_attempt_missing] 高风险 G4 复核必须先创建 review attempt；"
            "用 review-context --create-attempt 固化派发快照后再生成信封。")
    meta = _load_metadata(args.metadata)
    reviewer_policy = NB.normalize_profile_policy(meta["profile_snapshot"])
    policy_problems = NB.reviewer_policy_problems(reviewer_policy)
    if policy_problems:
        code, detail = policy_problems[0]
        raise AgentRecordError(
            f"[{code}] Reviewer Profile {meta['profile_id']!r} 的宿主有效工具策略"
            f"不满足只读复核：{detail}。独立复核必须由宿主只读子代理完成，"
            "不能用 Profile 名称或 Prompt 声明代替权限证据。")
    if meta["status"] != "completed":
        raise AgentRecordError(
            f"Reviewer metadata.status={meta['status']!r}，不是 completed；"
            "不得把未完成的复核记录当作正式结论。")
    db_path = str(getattr(args, "db", "") or "") or os.path.expanduser(
        "~/.zcode/cli/db/db.sqlite")
    # 新链只从持久化宿主 DB 取证；rollout 不再是 G4 依赖。
    model_info = _db_model(meta, db_path)
    model = f"{model_info['provider_id']}/{model_info['model_id']}"
    model_source = model_info["source"]
    calls = _db_review_tool_calls(meta, db_path)
    tool_call_source = "db.tool_usage+part"
    host_output, output_info = _review_host_output(meta, db_path)
    _bind_review_output(host_output, execution, dispatch_env, state)
    raw = _review_fields(host_output)
    if getattr(args, "input", ""):
        _assert_input_matches_host(_review_input(args.input), raw)
    # 复核范围：默认完整；--based-on 指定一条已回填的 PASS 复核做增量复核。
    scope = "full"
    based_on = ""
    manifest: list[str] = []
    based_on_path = str(getattr(args, "based_on", "") or "").strip()
    if based_on_path:
        base = _incremental_base(state, based_on_path)
        manifest = _incremental_evidence(state, base)
        scope = "incremental"
        based_on = str(base.get("result_sha256") or "")
    files = _merge_inspected_files(raw.get("files_inspected"), calls)
    undeclared = _declared_files_unverified(raw.get("files_inspected"), calls)
    if attempt is not None:
        if scope != str(attempt.get("review_scope") or ""):
            raise AgentRecordError(
                f"[review_attempt_scope_mismatch] attempt {attempt_id} 固化的 scope="
                f"{attempt.get('review_scope')!r}，本次信封计算为 {scope!r}；"
                "请按 attempt 创建时的范围复核。")
        if based_on != str(attempt.get("based_on_review_sha256") or ""):
            raise AgentRecordError(
                f"[review_attempt_scope_mismatch] attempt {attempt_id} 绑定的基线复核"
                f" {attempt.get('based_on_review_sha256') or '-'} 与本次不一致。")
        if sorted(manifest) != sorted(str(x) for x in
                                      attempt.get("evidence_manifest") or []):
            raise AgentRecordError(
                f"[review_attempt_scope_mismatch] attempt {attempt_id} 固化的证据清单"
                f"与当前计算不一致（{manifest} vs "
                f"{attempt.get('evidence_manifest') or []}）；新建 attempt 重做。")
        target_sha = str(attempt.get("target_sha256") or "")
    else:
        target_sha = G.final_evidence_digest(state)
    # 正式 G4 由 g4_attest + test_execution 决定；这里只做信封结构预检。
    problems: list[str] = []
    counts: dict[str, int] = {}
    for name, _command, _payload in calls:
        counts[name] = counts.get(name, 0) + 1
    host = {
        "metadata_path": meta["path"],
        "output_file": os.path.abspath(
            os.path.expanduser(str(meta["data"].get("outputFile") or ""))),
        "rollout_path": "",
        "tool_call_source": tool_call_source,
        "model_source": model_source,
        "output_sha256": str(output_info.get("sha256") or ""),
        "output_source": str(output_info.get("source") or ""),
        "output_message_id": str(output_info.get("message_id") or ""),
        "output_part_ids": list(output_info.get("part_ids") or []),
        # 宿主库路径随记录固化：outputFile 被清理后候选回退读库时必须用同一库，
        # 否则非默认 --db 场景会静默拿不到原始输出。
        "host_db": os.path.abspath(os.path.expanduser(db_path)),
        "tool_calls": [{"name": name, "count": count}
                       for name, count in sorted(counts.items())],
        "agent_id": meta["agent_id"],
        "child_session_id": meta["child_session_id"],
        "workspace_root": str(meta["data"].get("workspaceRoot") or "").strip(),
        # 会话时间与 prompt 摘要随宿主记录固化；ingest 会重新读取 metadata
        # 对照，避免旧会话通过重建信封冒充新 attempt 的复核方。
        "session_created_at": str(meta["data"].get("createdAt") or "").strip(),
        "session_completed_at": str(
            meta["data"].get("completedAt")
            or meta["data"].get("updatedAt") or "").strip(),
        "prompt_sha256": NB.sha256_text(
            str(meta["data"].get("prompt") or "")),
    }
    env = {
        "schema": NB.REVIEW_SCHEMA,
        "task_id": state["task_id"],
        "contract_sha256": state["contract_sha256"],
        "plan_id": dispatch_env["plan_id"],
        "plan_sha256": dispatch_env["plan_sha256"],
        "dispatch_id": execution["dispatch_id"],
        "work_item_id": execution["work_item_id"],
        "execution_result_sha256": exec_sha,
        "target_sha256": target_sha,
        "review_attempt_id": attempt_id if attempt is not None else "",
        "review_attempt_created_at": str(
            attempt.get("created_at") or "") if attempt is not None else "",
        "review_scope": scope,
        "based_on_review_sha256": based_on,
        "evidence_manifest": manifest,
        "reviewer": {
            "kind": "subagent",
            "agent_id": meta["agent_id"],
            "child_session_id": meta["child_session_id"],
            # Profile 名称只是记录事实；独立性由 agent/session 身份与宿主
            # 有效权限策略共同证明，G4 不依赖任何固定 Profile 名称或路径。
            "profile_id": meta["profile_id"],
            "profile": reviewer_policy,
            "model_reported": model,
        },
        "host": host,
        "verdict": raw.get("verdict"),
        "findings": raw.get("findings") or [],
        # Reviewer 无 Bash 权限（commands_run 恒为空）；files_inspected 由宿主
        # 工具记录派生，Reviewer 的自述只作补充。
        "files_inspected": files,
        "commands_run": [],
        "declared_files_unverified": undeclared,
        "changed_files": raw.get("changed_files") or [],
        "unverified": raw.get("unverified") or [],
        "residual_risks": raw.get("residual_risks") or [],
        # 结构预检：只有高风险且结论本身可放行时才标 eligible；partial/
        # unverified/blocking 复核仍可生成信封供 ingest 记录，但不能伪装 G4。
        "g4_eligible": bool(risk == "high"
                            and str(raw.get("verdict") or "").strip().lower()
                            == "pass"
                            and not raw.get("unverified")
                            and not any(
                                isinstance(f, dict)
                                and str(f.get("severity") or "").strip().lower()
                                == "blocking"
                                for f in raw.get("findings") or [])),
        "g4_reasons": [],
    }
    env["result_sha256"] = NB.envelope_sha256(env)
    try:
        NB.validate_review_result(env, native, execution, state)
    except NB.BridgeError as exc:
        raise AgentRecordError(
            f"生成的信封未通过正式校验 [{exc.code}]：{exc}") from None
    return env


def _execution_evidence_drift(execution: dict, state: dict) -> list[dict]:
    """执行信封 evidence[] 与当前 live 证据的漂移（与 native_bridge 同一实现）。"""
    return NB.execution_evidence_drift(execution, state)


def build_review_context(args) -> dict:
    """生成派发 Reviewer 所需的上下文清单。

    默认只读，不写状态；`--create-attempt` 会调用治理层创建并保存一条不可变
    review attempt（高风险 G4 专用）。输出绑定值、当前证据、执行证据漂移、
    上轮 findings 与建议的复核 scope，让主代理一次把「要复核什么、绑定值是什么、
    哪些问题还没闭合」交给 Reviewer，减少因上下文缺失造成的重复取证。
    """
    state = G.load_state(os.path.abspath(args.state))
    dispatch_env, _native = _dispatch_for_state(state, args.dispatch)
    execution = _load_json(args.execution)
    if not isinstance(execution, dict):
        raise AgentRecordError("execution 文件顶层必须是对象。")
    if str(execution.get("schema") or "") != NB.RESULT_SCHEMA:
        raise AgentRecordError(
            f"--execution 必须是 {NB.RESULT_SCHEMA} 信封。")
    exec_sha = str(execution.get("result_sha256") or "").strip().lower()
    if not exec_sha or NB.envelope_sha256(execution) != exec_sha:
        raise AgentRecordError(
            "execution.result_sha256 与信封内容不一致；不能作为复核上下文。")
    if str(execution.get("work_item_id") or "") != str(args.work_item):
        raise AgentRecordError(
            f"execution 工作项 {execution.get('work_item_id')!r} 与 "
            f"--work-item {args.work_item!r} 不一致。")
    recorded = [
        rec for rec in (state.get("native_results") or [])
        if isinstance(rec, dict)
        and str(rec.get("result_sha256") or "") == exec_sha
    ]
    if not recorded:
        raise AgentRecordError(
            "execution 尚未入账；请先用 auto ingest 回填 execution，再生成上下文。")
    workspace = str(getattr(args, "workspace", "") or "").strip() or os.getcwd()
    code_base = str(getattr(args, "base", "") or "").strip()
    code_snapshot = G.capture_code_snapshot(
        workspace, exclude_paths=G._evidence_paths(state)
        + G.governance_paths(args.state), base=code_base)

    coverage = NB.review_coverage(state)
    evidence = []
    for entry in state.get("evidence") or []:
        if not isinstance(entry, dict):
            continue
        evidence.append({
            "id": str(entry.get("id") or ""),
            "live": not bool(entry.get("dropped")),
            "sha256": str(entry.get("sha256") or ""),
            "path_hint": str(entry.get("path_hint") or ""),
            "note": str(entry.get("note") or ""),
        })
    criteria = [
        {"criterion_id": str(c.get("criterion_id") or ""),
         "description": str(c.get("description") or ""),
         "status": str(c.get("status") or "")}
        for c in (state.get("contract") or {}).get("criteria") or []
        if isinstance(c, dict)
    ]
    scope, based_on, manifest, reason = "full", "", [], "没有提供基线复核"
    previous = None
    previous_target = ""
    previous_path = str(getattr(args, "previous_review", "") or "").strip()
    if previous_path:
        prev = _load_json(previous_path)
        if not isinstance(prev, dict):
            raise AgentRecordError("--previous-review 顶层必须是对象。")
        prev_sha = str(prev.get("result_sha256") or "").strip().lower()
        base = None
        for record in state.get("native_reviews") or []:
            if isinstance(record, dict) and \
                    str(record.get("result_sha256") or "") == prev_sha:
                base = record
                break
        if base is None:
            raise AgentRecordError(
                f"--previous-review 指向的复核 {prev_sha[:16]}… 尚未回填到本任务。")
        previous_target = str(base.get("target_sha256") or "")
        base_trust = G._review_trust_problems(base, state, check_target=False)
        base_attempt = G.find_review_attempt(
            state, str(base.get("review_attempt_id") or ""))
        base_code = str((base_attempt or {}).get("code_scope_sha256") or "")
        previous = {
            "result_sha256": prev_sha,
            "verdict": str(base.get("verdict") or ""),
            "g4_eligible": base.get("g4_eligible") is True,
            "trust_problems": base_trust,
            "target_sha256": previous_target,
            "code_scope_sha256": base_code,
            "findings": [f for f in (base.get("findings") or [])
                         if isinstance(f, dict)],
            "unverified": list(base.get("unverified") or []),
        }
        if base_trust:
            reason = ("基线复核未通过当前 G4 信任检查（"
                      + ", ".join(base_trust)
                      + "）；旧 PASS 不能作为增量基础或复用依据，本轮必须完整复核")
        elif not isinstance(base.get("coverage"), dict) or not base.get("coverage"):
            reason = "基线复核没有覆盖快照；本轮必须完整复核"
        elif not base_code or base_code != str(code_snapshot.get("digest") or ""):
            reason = ("基线之后工作区代码范围已变化；增量复核只覆盖新增/变更证据，"
                      "不覆盖源码，本轮必须完整复核")
        else:
            base_cov = base.get("coverage") or {}
            base_evidence = base_cov.get("evidence") or {}
            base_actions = base_cov.get("actions") or {}
            base_criteria = base_cov.get("criteria") or {}
            changed = sorted(
                eid for eid, sha in coverage["evidence"].items()
                if base_evidence.get(eid) != sha)
            changed_actions = sorted(
                aid for aid, fp in coverage["actions"].items()
                if base_actions.get(aid) != fp)
            changed_criteria = sorted(
                cid for cid, fp in coverage["criteria"].items()
                if base_criteria.get(cid) != fp)
            if changed_criteria:
                # 条件核销会改变目标摘要，复用基线会在收口时判 native_review_stale。
                reason = ("完成条件状态已变化（" + ", ".join(changed_criteria)
                          + "）；基线的目标摘要过期，本轮必须完整复核")
            elif [aid for aid in changed_actions if aid != args.work_item]:
                reason = "其他工作项结论已变化；增量复核不覆盖，本轮必须完整复核"
            elif not changed and not changed_actions:
                reason = "相对基线没有新增/变更证据；可直接复用基线结论"
            else:
                scope = "incremental"
                based_on = prev_sha
                manifest = changed
                reason = "基线是 G4 PASS 且只有证据变化；本轮做增量复核"
    drift = _execution_evidence_drift(execution, state)
    warnings: list[str] = []
    if not code_snapshot.get("available"):
        warnings.append(
            "当前 workspace 不是可读取的 git 工作区（"
            f"{code_snapshot.get('reason') or 'unknown'}）：无法冻结未登记为证据的"
            "源码范围；高风险 G4 将拒绝创建 attempt。")
    pending = [c["criterion_id"] for c in criteria if c["status"] != "met"]
    if pending:
        warnings.append(
            "完成条件 " + ", ".join(pending) + " 尚未核销：核销会改变目标摘要，"
            "本轮复核在收口时会被判为过期（native_review_stale）；"
            "先用 criterion --status met 核销，再做最终 G4 复核。")
    if drift:
        warnings.append(
            "执行信封引用的证据已变化/归档："
            + ", ".join(f"{item['id']}({item['problem']})" for item in drift)
            + "；先修正证据或重新回填执行结果再复核。")
    current_target = G.final_evidence_digest(state)
    if previous and not previous.get("trust_problems") and \
            previous.get("g4_eligible") and previous_target and \
            previous_target != current_target:
        warnings.append(
            "上轮 PASS 绑定的是旧目标摘要（"
            f"{previous_target[:12]}… → {current_target[:12]}…）：若变化只是 "
            "minor/info 处置，记录为 findings/residual_risks 并停止，不要为清理它们"
            "重开完整复核；若发生实质代码/证据/合同变化，按新范围重新完整或增量复核。")
    attempt = None
    if getattr(args, "create_attempt", False):
        if str(state.get("risk") or "").strip().lower() != "high":
            raise AgentRecordError(
                "[review_attempt_not_applicable] 只有高风险 G4 任务需要 review "
                "attempt；普通风险复核不需要该绑定。")
        if pending:
            raise AgentRecordError(
                "[review_attempt_unstable] 完成条件尚未全部核销："
                + ", ".join(pending) + "；核销会改变目标摘要，先核销并稳定证据，"
                "再创建最终复核 attempt。")
        if drift:
            raise AgentRecordError(
                "[review_attempt_unstable] 执行信封引用的证据已漂移："
                + ", ".join(f"{item['id']}({item['problem']})" for item in drift)
                + "；先修正证据或重新回填执行结果，再创建 attempt。")
        if previous is not None and previous.get("g4_eligible") and \
                not previous.get("trust_problems") and \
                previous.get("code_scope_sha256") == code_snapshot.get("digest") and \
                previous_target == current_target and scope == "full" and not manifest:
            raise AgentRecordError(
                "[review_attempt_not_needed] 当前快照与上轮 G4 PASS 相同且没有"
                "新增/变更证据；直接复用上轮结论，不需要重新派发复核。")
        attempt = G.create_review_attempt(
            os.path.abspath(args.state),
            work_item_id=str(args.work_item),
            execution_result_sha256=exec_sha,
            review_scope=scope,
            based_on_review_sha256=based_on,
            evidence_manifest=manifest,
            workspace=workspace, base=code_base,
        )
    return {
        "schema": "copilot-review-context/v1",
        "task_id": state["task_id"],
        "contract_sha256": state["contract_sha256"],
        "plan_id": dispatch_env.get("plan_id") or "",
        "plan_sha256": dispatch_env.get("plan_sha256") or "",
        "dispatch_id": execution.get("dispatch_id") or "",
        "work_item_id": execution.get("work_item_id") or "",
        "execution_result_sha256": exec_sha,
        "target_sha256": (attempt or {}).get("target_sha256") or current_target,
        "review_attempt_id": (attempt or {}).get("review_attempt_id") or "",
        "review_attempt_created_at": (attempt or {}).get("created_at") or "",
        "code_snapshot": (attempt or {}).get("code_snapshot") or code_snapshot,
        "code_scope_sha256": (attempt or {}).get("code_scope_sha256")
        or code_snapshot.get("digest") or "",
        "review_scope": scope,
        "based_on_review_sha256": based_on,
        "evidence_manifest": manifest,
        "scope_reason": reason,
        "evidence": evidence,
        "criteria": criteria,
        "warnings": warnings,
        "execution_evidence_unbound": drift,
        "previous_review": previous,
        "note": ("attempt 已创建：派发 Reviewer 时把 review_attempt_id 写入 prompt；"
                 "信封必须用 review --attempt 生成。"
                 if attempt else
                 "ingest 会重新校验 scope、绑定与证据覆盖；本清单只用于准备派发。"),
    }


def _write_json(path: str, payload: dict) -> None:
    path = os.path.abspath(path)
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise AgentRecordError(f"无法写入 {path}：{exc}") from None


def _emit(payload: dict, output: str) -> int:
    if output:
        _write_json(output, payload)
    else:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="从 ZCode 实际运行记录生成 Native Bridge 正式信封")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("bind-execution",
                       help="在启动 Executor 前创建 execution binding（task/work_item/nonce）")
    p.add_argument("--state", required=True)
    p.add_argument("--work-item", required=True)
    p.add_argument("--test-command", action="append", default=[],
                   help="本轮预期测试命令；可多次；Executor 必须原样执行")
    p.add_argument("--workspace", default="",
                   help="执行工作区（默认当前目录）")
    p.add_argument("--require-test-result", action="store_true",
                   help="要求结构化测试结果（当前支持 JUnit XML）")
    p.add_argument("--require-artifact-integrity", action="store_true",
                   help="要求测试产物完整性")
    p.add_argument("--output", default="")

    p = sub.add_parser("execution", help="生成 copilot-native-result/v2 执行信封")
    p.add_argument("--state", required=True)
    p.add_argument("--dispatch", required=True,
                   help="drive 输出的 native_dispatch 信封（JSON）")
    p.add_argument("--work-item", required=True)
    p.add_argument("--metadata", required=True,
                   help="ZCode 原生 Agent 的 metadata.json（或 agent 目录）")
    p.add_argument("--status", required=True,
                   choices=("done", "failed", "blocked", "dropped"))
    p.add_argument("--summary", required=True)
    p.add_argument("--result-file", default="")
    p.add_argument("--result-text", default="")
    p.add_argument("--evidence", action="append", default=[],
                   help="E1=/abs/path；可多次；SHA-256 由脚本计算")
    p.add_argument("--test-command", action="append", default=[],
                   help="真实执行过的测试命令；可多次；会按宿主 Bash 记录核对")
    p.add_argument("--test-artifact", action="append", default=[],
                   help="测试产物对应的 --evidence id；可多次")
    p.add_argument("--workspace", default="",
                   help="执行代码快照的工作区（默认当前目录）")
    p.add_argument("--base", default="",
                   help="任务开始 commit；与 review-context --base 使用同一基准")
    p.add_argument("--binding", default="",
                   help="bind-execution 返回的 binding_id；高风险任务必填")
    p.add_argument("--db", default=os.path.expanduser(
        "~/.zcode/cli/db/db.sqlite"),
        help="宿主 SQLite；rollout 被清理时从 session_entry 读取模型")
    p.add_argument("--model-rollout", default="",
                   help="仅离线测试/非默认 rollout 路径使用；仍校验 sessionId")
    p.add_argument("--output", default="")

    p = sub.add_parser("review", help="生成 copilot-native-review/v2 复核信封")
    p.add_argument("--state", required=True)
    p.add_argument("--dispatch", required=True)
    p.add_argument("--execution", required=True,
                   help="已入账的 copilot-native-result/v2 执行信封")
    p.add_argument("--work-item", required=True)
    p.add_argument("--metadata", required=True,
                   help="Reviewer 原生 Agent 的 metadata.json（或 agent 目录）")
    p.add_argument("--input", default="",
                   help="可选：调用方保存的 Reviewer 结论副本，仅用于与宿主实际输出"
                        "交叉核对；结论只以宿主 outputFile 为准")
    p.add_argument("--based-on", default="",
                   help="增量复核：已回填的基线复核信封 JSON；只复核基线之后"
                        "新增/变更的证据，要求基线含覆盖快照")
    p.add_argument("--attempt", default="",
                   help="派发前由 review-context --create-attempt 创建的 "
                        "review_attempt_id；高风险 G4 必填")
    p.add_argument("--db", default=os.path.expanduser(
        "~/.zcode/cli/db/db.sqlite"),
        help="宿主 SQLite；从 message/part/session_entry 取证")
    p.add_argument("--output", default="")

    p = sub.add_parser("review-context",
                       help="生成派发 Reviewer 的绑定/证据/上轮 findings 清单")
    p.add_argument("--state", required=True)
    p.add_argument("--dispatch", required=True)
    p.add_argument("--execution", required=True,
                   help="已入账的 copilot-native-result/v2 执行信封")
    p.add_argument("--work-item", required=True)
    p.add_argument("--previous-review", default="",
                   help="可选：上一轮已回填的复核信封，用于建议增量复核范围")
    p.add_argument("--create-attempt", action="store_true",
                   help="派发前固化本次复核 attempt；高风险 G4 必须先创建")
    p.add_argument("--workspace", default="",
                   help="任务实际工作区（默认当前目录）；G4 代码快照的 git 根")
    p.add_argument("--base", default="",
                   help="可选：任务开始时的 git commit；用于把已提交的任务代码纳入"
                        "代码快照（默认只比较工作区与 HEAD）")
    p.add_argument("--output", default="")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "bind-execution":
            payload = bind_execution(args)
            return _emit(payload, args.output)
        if args.cmd == "execution":
            payload = build_execution(args)
            return _emit(payload, args.output)
        if args.cmd == "review":
            payload = build_review(args)
            return _emit(payload, args.output)
        payload = build_review_context(args)
        return _emit(payload, args.output)
    except (AgentRecordError, G.GovernanceError, OSError) as exc:
        # 生成阶段被拒也是“无效信封”：落进同一份 review-attempts.jsonl，
        # 否则 review-metrics 只统计 ingest 阶段，低估真实返工。
        if args.cmd == "review" and getattr(args, "state", ""):
            match = re.search(r"\[([a-z][a-z0-9_]*)\]", str(exc))
            attempt = {"work_item_id": str(getattr(args, "work_item", "") or "")}
            raw_exec = str(getattr(args, "execution", "") or "")
            if raw_exec:
                # 尽量带上执行摘要，让指标能按工作项/执行结果归因；
                # 读取失败不影响记录错误本身。
                try:
                    data = _load_json(raw_exec)
                except (AgentRecordError, G.GovernanceError, OSError, ValueError):
                    data = None
                if isinstance(data, dict):
                    attempt["execution_result_sha256"] = str(
                        data.get("result_sha256") or "")
            G.log_attempt(args.state, "review_build",
                          match.group(1) if match else "review_build_failed",
                          str(exc), attempt)
        print(f"native-agent: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())

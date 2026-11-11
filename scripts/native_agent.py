#!/usr/bin/env python3
"""从 ZCode 实际运行记录生成 Native Bridge 执行/复核信封。

本脚本不派发 Agent、不调用模型、不写任务状态。它只做一件事：把**宿主已经
产生**的原生运行记录（Agent 的 `metadata.json`、rollout 模型记录、实际证据
文件、Reviewer 的原始结论）转换成 `copilot-native-result/v1` /
`copilot-native-review/v1` 信封，并由脚本自己计算所有身份绑定、证据摘要和
`result_sha256`。

为什么需要它
------------
主代理如果手工拼信封，就可能把 Agent ID、Child Session ID、模型名、证据
SHA-256 或计划绑定写错/编造。这里把这些字段从实际记录中提取，并对不上就
失败关闭：

* 执行方身份只来自 `agent-*/metadata.json`；
* 实际模型只来自 `~/.zcode/cli/rollout/model-io-<childSessionId>.jsonl`；
* 证据 SHA-256 由脚本读取证据文件计算，不接受调用方传入；
* `plan_call_id` / `plan_reply_sha256` / `dispatch_id` / `idempotency_key`
  只从 dispatch 与已验证状态复制；
* 复核身份只来自 Reviewer 的 metadata，Reviewer 原始结论只提供
  verdict/findings/files_inspected/commands_run 等判断内容；
* 复核实际调用的工具与 Bash 命令只来自 rollout：工具白名单、只读校验、
  `commands_run` 必须与实际调用一致、调用计数必须与 metadata 相符。

仍然不是密码学证明
------------------
这些记录仍可能被能写本机文件的人伪造；本脚本保证的是“不靠主代理记忆或手抄
身份”，不是宿主签名。普通原生复核的 PASS 依然不等于 G4。

用法
----
    native_agent.py execution --state STATE --dispatch DISPATCH.json \
      --work-item T1 --metadata .../metadata.json \
      --status done --summary "一句话结果" --result-file result.md \
      --evidence E1=/abs/raw.log [--evidence E2=/abs/test.log] \
      --output T1.execution.json

    native_agent.py review --state STATE --dispatch DISPATCH.json \
      --execution T1.execution.json --metadata .../metadata.json \
      --input reviewer.raw.json --output T1.review.json

两个子命令在写文件前都会调用 `native_bridge` 的正式校验；校验失败不会产生
输出文件。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import copilot_dispatch as D  # noqa: E402
import native_bridge as NB  # noqa: E402

EXIT_OK, EXIT_USAGE = 0, 2
MAX_JSON_BYTES = 4 << 20
MAX_RESULT_CHARS = 200_000
_EVIDENCE_RE = re.compile(r"^(E[1-9][0-9]{0,3})=(.+)$")
_MODEL_SCAN_LINES = 200
# Reviewer Profile 只启用这四个工具；任何其他工具出现在实际记录里都说明
# 复核越界（或 Profile 被改写），必须拒绝生成可接纳的复核信封。
_REVIEW_TOOL_NAMES = frozenset({"Read", "Grep", "Glob", "Bash"})


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


def _review_tool_calls(meta: dict, override: str = "") -> list[tuple[str, str, dict]]:
    """读取 Reviewer 实际工具调用记录，并校验只读边界与记录完整性。

    返回 `(tool_name, bash_command, input_payload)` 列表；非 Bash 调用的命令为空串。
    这里只读宿主已经落盘的 rollout，不执行任何 Agent 或模型调用。调用记录
    缺失、损坏、计数不一致或出现非只读命令时失败关闭：不能把“自报安全”当成
    “实际只读”。
    """
    child = meta["child_session_id"]
    path = _rollout_path(meta, override)
    if not os.path.isfile(path):
        raise AgentRecordError(
            f"[review_rollout_missing] 找不到 Agent {child} 的 rollout 调用记录：{path}；"
            "没有实际调用记录时不得生成复核信封。")
    total = meta["data"].get("totalToolUseCount")
    if (not isinstance(total, int) or isinstance(total, bool) or total < 0):
        raise AgentRecordError(
            "[review_tool_count_missing] metadata.totalToolUseCount 缺失或非法；无法证明复核工具调用记录完整。")
    calls: list[tuple[str, str, dict]] = []
    seen: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for idx, line in enumerate(fh, 1):
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
                        f"[review_session_mismatch] rollout {path} 第 {idx} 行 sessionId={session!r} 与 "
                        f"Child Session {child!r} 不一致；拒绝把别的会话调用当作本复核。")
                response = row.get("response")
                if response is None:
                    continue
                if not isinstance(response, dict):
                    raise AgentRecordError(
                        f"[review_toolcalls_invalid] rollout {path} 第 {idx} 行 response "
                        "不是对象；无法校验工具调用记录，拒绝复核信封。")
                raw_calls = response.get("toolCalls")
                if raw_calls is None:
                    continue
                if not isinstance(raw_calls, list):
                    raise AgentRecordError(
                        f"[review_toolcalls_invalid] rollout {path} 第 {idx} 行 response.toolCalls 不是数组。")
                for pos, call in enumerate(raw_calls, 1):
                    if not isinstance(call, dict):
                        raise AgentRecordError(
                            f"[review_toolcalls_invalid] rollout {path} 第 {idx} 行 toolCalls[{pos}] 不是对象。")
                    name = str(call.get("name") or "").strip()
                    if name not in _REVIEW_TOOL_NAMES:
                        raise AgentRecordError(
                            f"[review_tool_not_allowed] 复核实际调用了未授权工具 {name!r}；Reviewer 只允许 "
                            f"{sorted(_REVIEW_TOOL_NAMES)}。")
                    cid = str(call.get("id") or "").strip()
                    if cid:
                        if cid in seen:
                            raise AgentRecordError(
                                f"[review_rollout_duplicate_call] rollout {path} 出现重复 tool call id {cid!r}；"
                                "调用记录不唯一，无法据条目数判断完整性。")
                        seen.add(cid)
                    payload = call.get("input")
                    if not isinstance(payload, dict):
                        raise AgentRecordError(
                            f"[review_toolcalls_invalid] rollout {path} 第 {idx} 行 {name} 的 input 不是对象。")
                    command = ""
                    if name == "Bash":
                        command = str(payload.get("command") or "").strip()
                        if not command:
                            raise AgentRecordError(
                                f"[review_toolcalls_invalid] rollout {path} 第 {idx} 行 Bash 调用缺少 command。")
                    calls.append((name, command, payload))
    except OSError as exc:
        raise AgentRecordError(f"无法读取 rollout {path}：{exc}") from None
    if len(calls) != total:
        raise AgentRecordError(
            f"[review_toolcalls_incomplete] rollout {path} 记录 {len(calls)} 次工具调用，但 "
            f"metadata.totalToolUseCount={total}；调用记录不完整，拒绝复核信封。")
    bash_commands = [cmd for _, cmd, _ in calls if cmd]
    if bash_commands:
        try:
            NB.validate_review_commands(bash_commands)
        except NB.BridgeError as exc:
            raise AgentRecordError(
                f"Reviewer 实际执行的 Bash 命令未通过只读校验 [{exc.code}]：{exc}"
            ) from None
    return calls


def _assert_reported_commands_executed(reported: object,
                                       calls: list[tuple[str, str, dict]]) -> None:
    """自报 `commands_run` 必须有实际调用支持；不反向要求完整复述。"""
    if not isinstance(reported, list):
        return
    executed = {cmd for _, cmd, _ in calls if cmd}
    for raw in reported:
        cmd = str(raw or "").strip()
        if cmd and cmd not in executed:
            raise AgentRecordError(
                f"[review_command_not_executed] Reviewer 自报执行了 rollout 中不存在的命令：{cmd}；"
                "禁止只报安全命令而实际执行其他命令。")


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


def build_execution(args) -> dict:
    state = D.load_state(os.path.abspath(args.state))
    dispatch_env, native = _dispatch_for_state(state, args.dispatch)
    action = _find_action(dispatch_env, args.work_item)
    meta = _load_metadata(args.metadata)
    if meta["profile_id"] == "reviewer":
        raise AgentRecordError("reviewer Profile 不得作为 Executor。")
    if meta["status"] != "completed":
        raise AgentRecordError(
            f"Executor metadata.status={meta['status']!r}，不是 completed；"
            "不得把未完成的运行记录当作正式执行结果。")
    model = _rollout_model(meta, args.model_rollout)
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
    env = {
        "schema": NB.RESULT_SCHEMA,
        "task_id": state["task_id"],
        "round_id": dispatch_env["round_id"],
        "contract_sha256": state["contract_sha256"],
        "plan_call_id": dispatch_env["plan_call_id"],
        "plan_reply_sha256": dispatch_env["plan_reply_sha256"],
        "dispatch_id": action["dispatch_id"],
        "idempotency_key": action["idempotency_key"],
        "work_item_id": action["work_item_id"],
        "status": args.status,
        "summary": summary,
        "result": result,
        "evidence": _parse_evidence(args.evidence),
        "executor": {
            "kind": "subagent",
            "agent_id": meta["agent_id"],
            "child_session_id": meta["child_session_id"],
            "profile_id": meta["profile_id"],
            "model_reported": model,
        },
        "timing": _timing(meta),
        "usage": _usage(meta),
    }
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


def _review_host_output(meta: dict) -> dict:
    """读取宿主为 Reviewer 落盘的**实际输出**（metadata.outputFile）。

    结论只能以宿主记录为准：调用方 `--input` 是可复制的文件，改一个词就能把
    `partial` 改成 `pass`。`outputFile` 由宿主在 Agent 结束时写入，且必须与
    metadata.json 同目录（拒绝被 metadata 指向任意外部文件）。
    """
    raw = str((meta.get("data") or {}).get("outputFile") or "").strip()
    if not raw:
        raise AgentRecordError(
            "[review_output_missing] 宿主 metadata 没有 outputFile：无法取得 Reviewer "
            "实际输出；不得用调用方保存的副本代替正式复核结论。")
    path = os.path.abspath(os.path.expanduser(raw))
    if os.path.dirname(path) != os.path.dirname(meta["path"]):
        raise AgentRecordError(
            f"[review_output_misplaced] outputFile 不在 metadata 同目录：{path}；"
            "拒绝使用外部（可能被替换的）文件作为复核结论。")
    text = _read_text(path).strip()
    try:
        data = json.loads(text)
    except ValueError:
        # Profile 要求纯 JSON；兼容宿主把整段输出包在单个 ```json 围栏里的情况，
        # 仍只接受围栏内的唯一对象，允许前后空白，不解析任何叙述文字。
        fenced = re.fullmatch(r"```(?:json)?\s*\n(?P<body>.*)\n```", text, re.S)
        if not fenced:
            raise AgentRecordError(
                f"[review_output_unparsable] 无法把宿主输出解析为 JSON：{path}") from None
        try:
            data = json.loads(fenced.group("body"))
        except ValueError:
            raise AgentRecordError(
                f"[review_output_unparsable] 宿主输出的 JSON 围栏无法解析：{path}") from None
    if not isinstance(data, dict):
        raise AgentRecordError("Reviewer 实际输出顶层必须是对象。")
    return data


def _bind_review_output(data: dict, execution: dict, dispatch_env: dict,
                        state: dict) -> None:
    """宿主输出里必须带有本次执行对象绑定；缺失或指向别处一律拒绝。"""
    expected = {
        "task_id": str(state.get("task_id") or ""),
        "round_id": str(dispatch_env.get("round_id") or ""),
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


def _assert_files_inspected(files: object, calls: list[tuple[str, str, dict]]) -> None:
    """`files_inspected` 必须能在实际工具调用里找到对应访问。

    只校验路径存在（native_bridge 层）不足以证明 Reviewer 真的看过内容：把别人的
    复核结论配上几个真实路径也能通过。这里要求每个自报文件至少被一次实际调用的
    参数（Read/Grep 的路径、Bash 命令里的路径或其文件名）引用；不要求必须出现
    `Read`——Bash 读取与 git diff 同样是有效访问。Glob/列目录不算读过内容，
    但仍要求该文件出现在调用参数里。

    仅凭文件名相同不算：自报 `docs/report.md` 时，读取路径必须以它结尾（组件边界），
    或是它的直接相对形式（组件更少且直接父目录同名）。目录插层
    （`docs/archive/report.md`）、不同绝对根（`/repo-b/docs/report.md`）与
    `docs/report.md.bak` 都不算命中；父目录名与文件名分处两个参数也不算一次读取。
    这避免借别处的同名文件冒领，也避免 `ls docs; cat other/report.md` 这类复合命令
    拼凑过关（Bash 按 `;`/`|`/换行拆分后逐段比较）。
    """
    if not isinstance(files, list):
        return
    blobs: list[str] = []
    for _tool, command, payload in calls:
        if command:
            # Bash 只按命令段比对：整条命令拼在一起会让 `ls docs; cat other/report.md`
            # 同时出现目录名与文件名，等于把两段互不相干的访问拼成一次读取。
            blobs.extend(part for part in re.split(r"[;|\n]", command) if part.strip())
        else:
            blobs.append(json.dumps(payload, ensure_ascii=False))
    for raw in files:
        path = str(raw or "").strip().rstrip("/")
        if not path:
            continue
        if not any(_mentions_path(path, blob) for blob in blobs):
            raise AgentRecordError(
                f"[review_file_not_inspected] 自报检查过 {path}，但实际工具调用中"
                "没有任何访问该路径的记录；不能把未打开的文件算作已复核。")


def build_review(args) -> dict:
    state = D.load_state(os.path.abspath(args.state))
    dispatch_env, native = _dispatch_for_state(state, args.dispatch)
    execution = _load_json(args.execution)
    if not isinstance(execution, dict):
        raise AgentRecordError("execution 文件顶层必须是对象。")
    if str(execution.get("schema") or "") != NB.RESULT_SCHEMA:
        raise AgentRecordError("--execution 必须是 copilot-native-result/v1 信封。")
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
            "execution 尚未入账；请先用 drive 回填 execution，再生成复核。")
    if str(execution.get("work_item_id") or "") != str(args.work_item):
        raise AgentRecordError(
            f"execution 工作项 {execution.get('work_item_id')!r} 与 "
            f"--work-item {args.work_item!r} 不一致。")
    meta = _load_metadata(args.metadata)
    if meta["profile_id"] != "reviewer":
        raise AgentRecordError(
            f"Reviewer metadata.profileId={meta['profile_id']!r}，不是 reviewer；"
            "不得用其他 Profile 冒充独立 Reviewer。")
    if meta["status"] != "completed":
        raise AgentRecordError(
            f"Reviewer metadata.status={meta['status']!r}，不是 completed；"
            "不得把未完成的复核记录当作正式结论。")
    model = _rollout_model(meta, args.model_rollout)
    host_output = _review_host_output(meta)
    _bind_review_output(host_output, execution, dispatch_env, state)
    raw = _review_fields(host_output)
    if getattr(args, "input", ""):
        _assert_input_matches_host(_review_input(args.input), raw)
    env = {
        "schema": NB.REVIEW_SCHEMA,
        "task_id": state["task_id"],
        "round_id": dispatch_env["round_id"],
        "contract_sha256": state["contract_sha256"],
        "dispatch_id": execution["dispatch_id"],
        "work_item_id": execution["work_item_id"],
        "execution_result_sha256": exec_sha,
        "reviewer": {
            "kind": "subagent",
            "agent_id": meta["agent_id"],
            "child_session_id": meta["child_session_id"],
            "profile_id": "reviewer",
            "model_reported": model,
        },
        "verdict": raw.get("verdict"),
        "findings": raw.get("findings") or [],
        "files_inspected": raw.get("files_inspected") or [],
        "commands_run": raw.get("commands_run") or [],
        "changed_files": raw.get("changed_files") or [],
        "unverified": raw.get("unverified") or [],
        "residual_risks": raw.get("residual_risks") or [],
        "g4_eligible": False,
    }
    env["result_sha256"] = NB.envelope_sha256(env)
    try:
        NB.validate_review_result(env, native, execution, state)
    except NB.BridgeError as exc:
        raise AgentRecordError(
            f"生成的信封未通过正式校验 [{exc.code}]：{exc}") from None
    calls = _review_tool_calls(meta, args.model_rollout)
    _assert_reported_commands_executed(raw.get("commands_run"), calls)
    _assert_files_inspected(raw.get("files_inspected"), calls)
    return env


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

    p = sub.add_parser("execution", help="生成 copilot-native-result/v1 执行信封")
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
    p.add_argument("--model-rollout", default="",
                   help="仅离线测试/非默认 rollout 路径使用；仍校验 sessionId")
    p.add_argument("--output", default="")

    p = sub.add_parser("review", help="生成 copilot-native-review/v1 复核信封")
    p.add_argument("--state", required=True)
    p.add_argument("--dispatch", required=True)
    p.add_argument("--execution", required=True,
                   help="已入账的 copilot-native-result/v1 执行信封")
    p.add_argument("--work-item", required=True)
    p.add_argument("--metadata", required=True,
                   help="Reviewer 原生 Agent 的 metadata.json（或 agent 目录）")
    p.add_argument("--input", required=True,
                   help="Reviewer 原始结论 JSON（verdict/findings/...）")
    p.add_argument("--model-rollout", default="")
    p.add_argument("--output", default="")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "execution":
            payload = build_execution(args)
        else:
            payload = build_review(args)
        return _emit(payload, args.output)
    except (AgentRecordError, D.DispatchError, OSError) as exc:
        print(f"native-agent: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())

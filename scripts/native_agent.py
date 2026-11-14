#!/usr/bin/env python3
"""从 ZCode 实际运行记录生成 Native Bridge 执行/复核信封。

本脚本不派发 Agent、不调用模型、不写任务状态。它只做一件事：把**宿主已经
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
* 实际模型只来自 `~/.zcode/cli/rollout/model-io-<childSessionId>.jsonl`；
* 证据 SHA-256 由脚本读取证据文件计算，不接受调用方传入；
* `plan_id` / `plan_sha256` / `dispatch_id` / `idempotency_key` 只从本地计划
  与 dispatch 复制；
* 复核身份只来自 Reviewer 的 metadata，Reviewer 原始结论只提供
  verdict/findings/files_inspected/commands_run 等判断内容；
* 复核实际调用的工具与 Bash 命令只来自 rollout：工具白名单、只读校验、
  `commands_run` 必须与实际调用一致、调用计数必须与 metadata 相符；
* `g4_eligible` 由本脚本按宿主记录计算（Profile 工具集、实际只读调用、独立
  Agent/Child Session、最终证据摘要绑定），Reviewer 不能自述。

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
      --input reviewer.raw.json --output T1.review.json

两个子命令在写文件前都会调用 `native_bridge` 的正式校验；校验失败不会产生
输出文件。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import governance as G  # noqa: E402
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


def _actual_commands(calls: list[tuple[str, str, dict]]) -> list[str]:
    """实际执行过的 Bash 命令（保持首次出现顺序，去重）。

    v6.1 起复核信封的 `commands_run` 由宿主 rollout 派生，不再采信 Reviewer
    自述：自述命令既可能漏报也可能多报，宿主记录才是可用证据。
    """
    out: list[str] = []
    for _tool, command, _payload in calls:
        if command and command not in out:
            out.append(command)
    return out


_PAYLOAD_PATH_KEYS = ("file_path", "path", "pattern")


def _absolute_file(value: str) -> str:
    if not value or "\n" in value:
        return ""
    path = os.path.abspath(os.path.expanduser(value))
    return path if os.path.isfile(path) else ""


def _absolute_dir(value: str) -> str:
    if not value or "\n" in value:
        return ""
    path = os.path.abspath(os.path.expanduser(value))
    return path if os.path.isdir(path) else ""


def _join_dir(directory: str, token: str) -> str:
    """把相对文件 token 解析到已知目录；未知目录或选项 token 返回空。"""
    if not directory or not token or token.startswith("-"):
        return ""
    candidate = token if os.path.isabs(token) else os.path.join(directory, token)
    return _absolute_file(candidate)


def _command_contexts(command: str) -> list[tuple[str, list[str], str]]:
    """按顶层 `;`/`|`/`&&` 切段，返回 (工作目录, argv, 原始片段)。

    真实复核常用 `cd <dir>; cat report.md`、`cd <dir> && head x` 或
    `git -C <dir> show ...` 在子目录取证。此前只解析绝对路径，导致
    `report-final-v4.md` 这类相对读取被判成“未访问证据”（honor G4 第 6 次
    尝试）。这里跟踪段内 `cd` 与 `git -C` 建立的目录，仅用于把相对 token
    解析成绝对路径，不改变只读校验。
    """
    contexts: list[tuple[str, list[str], str]] = []
    cwd = ""
    for part in re.split(r"[;|\n]|&&", command):
        text = part.strip()
        if not text:
            continue
        try:
            argv = shlex.split(text, posix=True)
        except ValueError:
            contexts.append((cwd, [], text))
            continue
        head = os.path.basename(argv[0]) if argv else ""
        if head == "cd" and len(argv) >= 2:
            target = argv[1]
            resolved = _absolute_dir(
                target if os.path.isabs(target)
                else os.path.join(cwd or os.getcwd(), target))
            if resolved:
                cwd = resolved
                contexts.append((cwd, argv, text))
                continue
        elif head == "git":
            idx = 1
            while idx + 1 < len(argv) and argv[idx] == "-C":
                target = argv[idx + 1]
                resolved = _absolute_dir(
                    target if os.path.isabs(target)
                    else os.path.join(cwd or os.getcwd(), target))
                if resolved:
                    cwd = resolved
                idx += 2
        contexts.append((cwd, argv, text))
    return contexts


def _command_paths(command: str) -> list[str]:
    """从 Bash 命令里提取真实存在的文件路径（仅用于补充 inspected 清单）。"""
    out: list[str] = []
    for cwd, argv, _text in _command_contexts(command):
        for token in argv:
            path = _absolute_file(token) or _join_dir(cwd, token)
            if path and path not in out:
                out.append(path)
    return out


def _merge_inspected_files(declared: object,
                           calls: list[tuple[str, str, dict]]) -> list[str]:
    """合并自报与宿主实际的读取清单。

    自报文件只在真实存在时保留；宿主读取（Read/Grep/Glob 的路径参数、Bash
    命令里的绝对文件）一律并入。G4 的证据覆盖仍单独按实际调用计算，这份清单
    只作说明，不构成放行依据。
    """
    out: list[str] = []
    if isinstance(declared, list):
        for item in declared:
            path = _absolute_file(str(item or "").strip())
            if path and path not in out:
                out.append(path)
    for _tool, command, payload in calls:
        candidates: list[str] = []
        if command:
            candidates.extend(_command_paths(command))
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


def build_execution(args) -> dict:
    state = G.load_state(os.path.abspath(args.state))
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


_FENCED_JSON_RE = re.compile(
    r"```[ \t]*(?P<tag>[A-Za-z0-9_+.-]*)[ \t]*\r?\n?(?P<body>.*?)```", re.S)


def _extract_review_json(text: str, path: str) -> dict:
    """从宿主输出里取出唯一的 JSON 对象。

    v6.1 起不再要求“纯 JSON 且无任何叙述”：模型偶尔会把对象包在围栏里或前后
    加一句说明，这不应让整轮复核作废。v6.3 进一步容忍真实输出里的围栏变体：
    语言标记大小写/带空格、开头围栏后没有换行、CRLF 行尾、前后有说明文字。
    但不做模糊解析：只接受 (1) 整段就是 JSON，或 (2) 恰好一个围栏且围栏内是
    单个 JSON 对象。多个可解析对象或无法解析时失败关闭，避免从叙述里猜结论；
    围栏外的说明文字不参与解析（结论只取那唯一一个可解析对象）。
    """
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        return data
    parsed = []
    for match in _FENCED_JSON_RE.finditer(text):
        try:
            candidate = json.loads(match.group("body").strip())
        except ValueError:
            continue
        if isinstance(candidate, dict):
            parsed.append(candidate)
    if len(parsed) == 1:
        return parsed[0]
    if len(parsed) > 1:
        raise AgentRecordError(
            f"[review_output_ambiguous] 宿主输出含 {len(parsed)} 个 JSON 对象；"
            "无法确定正式结论，拒绝复核信封。")
    raise AgentRecordError(
        f"[review_output_unparsable] 无法把宿主输出解析为 JSON 对象：{path}；"
        "请让 Reviewer 以单个 JSON 对象输出结论。")


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
    data = _extract_review_json(_read_text(path).strip(), path)
    if not isinstance(data, dict):
        raise AgentRecordError("Reviewer 实际输出顶层必须是对象。")
    return data


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


def _review_profile_problems(meta: dict) -> list[str]:
    """按宿主快照核对 Reviewer Profile 的工具边界。"""
    snapshot = meta["profile_snapshot"]
    tools = snapshot.get("tools")
    disallowed = snapshot.get("disallowedTools")
    problems: list[str] = []
    if not isinstance(tools, list) or not tools:
        problems.append("profile_tools_missing")
    else:
        extra = sorted({str(t) for t in tools} - set(_REVIEW_TOOL_NAMES))
        if extra:
            problems.append("profile_tools_not_readonly:" + ",".join(extra))
    if isinstance(disallowed, list):
        # disallowedTools 是「被禁用的工具」；Reviewer Profile 必须显式禁用写工具。
        # （旧实现把只读工具出现在这里当成放宽，语义正好相反。）
        disabled = {str(t) for t in disallowed}
        missing = [t for t in ("Write", "Edit") if t not in disabled]
        if missing:
            problems.append("profile_disallowed_missing:" + ",".join(missing))
    return problems


def _call_blobs(calls: list[tuple[str, str, dict]]) -> list[str]:
    blobs: list[str] = []
    for _tool, command, payload in calls:
        if command:
            for cwd, argv, text in _command_contexts(command):
                resolved = [p for p in (_join_dir(cwd, token) for token in argv) if p]
                if resolved:
                    # 追加解析后的绝对路径：相对读取（`cd <dir>; head x.md`）与
                    # 绝对证据路径因此能在组件边界上命中，避免假阴性。
                    text = f"{text} {' '.join(resolved)}"
                blobs.append(text)
        else:
            blobs.append(json.dumps(payload, ensure_ascii=False))
    return blobs


def _evidence_not_inspected(state: dict,
                            calls: list[tuple[str, str, dict]],
                            only_ids: set[str] | None = None) -> list[str]:
    """G4 复核必须实际访问当前任务的文件证据。

    `only_ids` 非空时只要求这些证据（增量复核只对基线之后新增/变更的证据
    负责）；完整复核要求全部未归档证据。无论哪种模式都按宿主实际调用判定，
    不看自述清单。
    """
    blobs = _call_blobs(calls)
    missing: list[str] = []
    for entry in state.get("evidence") or []:
        if not isinstance(entry, dict) or entry.get("dropped"):
            continue
        eid = str(entry.get("id") or "")
        if only_ids is not None and eid not in only_ids:
            continue
        hint = str(entry.get("path_hint") or "")
        if not hint or hint == "<inline>":
            continue
        if not any(_mentions_path(hint, blob) for blob in blobs):
            missing.append(hint)
    return missing


def _g4_problems(meta: dict, calls: list[tuple[str, str, dict]], raw: dict,
                 execution: dict, state: dict, target_sha: str,
                 evidence_ids: set[str] | None = None) -> list[str]:
    """按宿主记录计算 G4 资格；任何一项不满足都不得标记 g4_eligible。"""
    problems = _review_profile_problems(meta)
    if not calls:
        problems.append("review_no_tool_calls")
    if meta["status"] != "completed":
        problems.append("reviewer_not_completed")
    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict != "pass":
        problems.append("review_verdict_not_pass")
    findings = raw.get("findings") or []
    if isinstance(findings, list) and any(
            isinstance(f, dict) and str(f.get("severity") or "") == "blocking"
            for f in findings):
        problems.append("blocking_findings")
    if raw.get("unverified"):
        problems.append("unverified_items")
    if raw.get("changed_files"):
        problems.append("changed_files")
    executor = execution.get("executor") or {}
    for key in ("agent_id", "child_session_id"):
        exec_value = str(executor.get(key) or "").strip()
        rev_value = str(meta.get(key) or "").strip()
        if exec_value and rev_value and exec_value == rev_value:
            problems.append(f"reviewer_not_independent:{key}")
    if str(state.get("risk") or "").lower() != "high":
        problems.append("risk_not_high")
    if target_sha != G.final_evidence_digest(state):
        problems.append("target_stale")
    if _evidence_not_inspected(state, calls, evidence_ids):
        problems.append("evidence_not_inspected")
    return problems


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
    calls = _review_tool_calls(meta, args.model_rollout)
    # 复核范围：默认完整；--based-on 指定一条已回填的 PASS 复核做增量复核。
    scope = "full"
    based_on = ""
    manifest: list[str] = []
    delta_ids: set[str] | None = None
    based_on_path = str(getattr(args, "based_on", "") or "").strip()
    if based_on_path:
        base = _incremental_base(state, based_on_path)
        manifest = _incremental_evidence(state, base)
        scope = "incremental"
        based_on = str(base.get("result_sha256") or "")
        delta_ids = set(manifest)
    files = _merge_inspected_files(raw.get("files_inspected"), calls)
    commands = _actual_commands(calls)
    undeclared = _declared_files_unverified(raw.get("files_inspected"), calls)
    target_sha = G.final_evidence_digest(state)
    problems = _g4_problems(meta, calls, raw, execution, state, target_sha,
                            delta_ids)
    counts: dict[str, int] = {}
    for name, _command, _payload in calls:
        counts[name] = counts.get(name, 0) + 1
    host = {
        "metadata_path": meta["path"],
        "output_file": os.path.abspath(
            os.path.expanduser(str(meta["data"].get("outputFile") or ""))),
        "rollout_path": os.path.abspath(_rollout_path(meta, args.model_rollout)),
        "tool_calls": [{"name": name, "count": count}
                       for name, count in sorted(counts.items())],
        "agent_id": meta["agent_id"],
        "child_session_id": meta["child_session_id"],
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
        "review_scope": scope,
        "based_on_review_sha256": based_on,
        "evidence_manifest": manifest,
        "reviewer": {
            "kind": "subagent",
            "agent_id": meta["agent_id"],
            "child_session_id": meta["child_session_id"],
            "profile_id": "reviewer",
            "model_reported": model,
        },
        "host": host,
        "verdict": raw.get("verdict"),
        "findings": raw.get("findings") or [],
        # commands_run/files_inspected 由宿主 rollout 派生；Reviewer 的自述只作补充。
        "files_inspected": files,
        "commands_run": commands,
        "declared_files_unverified": undeclared,
        "changed_files": raw.get("changed_files") or [],
        "unverified": raw.get("unverified") or [],
        "residual_risks": raw.get("residual_risks") or [],
        "g4_eligible": not problems,
        "g4_reasons": problems,
    }
    env["result_sha256"] = NB.envelope_sha256(env)
    try:
        NB.validate_review_result(env, native, execution, state)
    except NB.BridgeError as exc:
        raise AgentRecordError(
            f"生成的信封未通过正式校验 [{exc.code}]：{exc}") from None
    return env


def verify_review_host_record(envelope: dict) -> list[str]:
    """回填前重新核对 G4 复核引用的宿主记录；返回问题列表（空表示可用）。

    只在 `g4_eligible=true` 时调用。重新读取 metadata 与 rollout，确认：
    Profile 仍是 reviewer 且工具只读、Agent/Child Session 未被改指、实际工具
    调用计数仍与 metadata 一致、没有出现非只读命令。任何一项失败都拒绝 G4。
    """
    if envelope.get("g4_eligible") is not True:
        return ["g4_not_eligible"]
    host = envelope.get("host") or {}
    if not isinstance(host, dict):
        return ["host_record_invalid"]
    try:
        meta = _load_metadata(str(host.get("metadata_path") or ""))
    except AgentRecordError as exc:
        return [f"host_metadata_unreadable:{exc}"]
    problems = []
    if meta["profile_id"] != "reviewer":
        problems.append("host_profile_changed")
    if meta["agent_id"] != str((envelope.get("reviewer") or {}).get("agent_id") or ""):
        problems.append("host_agent_mismatch")
    if meta["child_session_id"] != str(
            (envelope.get("reviewer") or {}).get("child_session_id") or ""):
        problems.append("host_session_mismatch")
    if os.path.abspath(str(host.get("output_file") or "")) != os.path.abspath(
            str(meta["data"].get("outputFile") or "")):
        problems.append("host_output_mismatch")
    try:
        calls = _review_tool_calls(meta, str(host.get("rollout_path") or ""))
    except AgentRecordError as exc:
        return problems + [f"host_rollout_unreadable:{exc}"]
    if not calls:
        problems.append("host_no_tool_calls")
    expected = {str(c.get("name")): int(c.get("count") or 0)
                for c in host.get("tool_calls") or [] if isinstance(c, dict)}
    actual: dict[str, int] = {}
    for name, _command, _payload in calls:
        actual[name] = actual.get(name, 0) + 1
    if expected != actual:
        problems.append("host_tool_calls_changed")
    if _review_profile_problems(meta):
        problems.extend(_review_profile_problems(meta))
    # 信封的结论必须与宿主落盘的原文一致：只核对宿主文件存在与调用计数，
    # 手工伪造一份“pass”信封仍可能被当成 G4。宿主 outputFile 是结论来源，
    # ingest 时重新解析并对照 verdict/findings/unverified/changed_files。
    try:
        raw = _review_host_output(meta)
    except AgentRecordError as exc:
        problems.append(f"host_output_unreadable:{exc}")
    else:
        if str(raw.get("verdict") or "").strip().lower() != str(
                envelope.get("verdict") or "").strip().lower():
            problems.append("host_verdict_changed")
        if NB.canonical_json(raw.get("findings") or []) != NB.canonical_json(
                envelope.get("findings") or []):
            problems.append("host_findings_changed")
        if NB.canonical_json(raw.get("unverified") or []) != NB.canonical_json(
                envelope.get("unverified") or []):
            problems.append("host_unverified_changed")
        if NB.canonical_json(raw.get("changed_files") or []) != NB.canonical_json(
                envelope.get("changed_files") or []):
            problems.append("host_changed_files_changed")
    return problems


def build_preflight(args) -> dict:
    """派发前校验一份 Reviewer 命令清单，避免整轮复核因一条命令作废。

    只做静态只读校验，不执行命令；实际执行的命令仍由宿主 rollout 逐条复核。
    真实记录里 ad-strategy 第 2/7 轮、honor G4 多次尝试都是“其余部分都对，只因
    一条命令不在白名单或输出格式不合法而整轮作废”，预检把这类失败提前到派发前。
    """
    commands: list[str] = [str(c).strip() for c in (args.command or [])
                           if str(c).strip()]
    path = str(getattr(args, "file", "") or "").strip()
    if path:
        text = _read_text(path)
        stripped = text.lstrip()
        if stripped.startswith("["):
            try:
                data = json.loads(stripped)
            except ValueError as exc:
                raise AgentRecordError(
                    f"--file 不是合法 JSON 数组：{exc}") from None
            if not isinstance(data, list):
                raise AgentRecordError("--file 的 JSON 顶层必须是字符串数组。")
            commands.extend(str(item).strip() for item in data if str(item).strip())
        else:
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    commands.append(line)
    if not commands:
        raise AgentRecordError(
            "preflight-commands 需要 --command 或 --file 提供至少一条命令。")
    problems: list[dict] = []
    for idx, command in enumerate(commands, 1):
        try:
            NB.validate_review_commands([command])
        except NB.BridgeError as exc:
            problems.append({"index": idx, "command": command,
                             "code": exc.code, "message": str(exc)})
    return {
        "schema": "copilot-native-preflight/v1",
        "ok": not problems,
        "checked": len(commands),
        "problems": problems,
        "note": "预检只证明命令通过只读白名单；实际执行的命令仍由宿主 rollout "
                "逐条复核，写操作与输出格式问题仍需 Reviewer 自己守住。",
    }


def _execution_evidence_drift(execution: dict, state: dict) -> list[dict]:
    """执行信封 evidence[] 与当前 live 证据的漂移（与 native_bridge 同一实现）。"""
    return NB.execution_evidence_drift(execution, state)


def build_review_context(args) -> dict:
    """生成派发 Reviewer 所需的上下文清单（只读，不写状态）。

    输出绑定值、当前证据、执行证据漂移、上轮 findings 与建议的复核 scope，让
    主代理一次把「要复核什么、绑定值是什么、哪些问题还没闭合」交给 Reviewer，
    减少因上下文缺失造成的重复取证。
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
        previous = {
            "result_sha256": prev_sha,
            "verdict": str(base.get("verdict") or ""),
            "g4_eligible": base.get("g4_eligible") is True,
            "findings": [f for f in (base.get("findings") or [])
                         if isinstance(f, dict)],
            "unverified": list(base.get("unverified") or []),
        }
        if str(base.get("verdict") or "").strip().lower() != "pass" or \
                base.get("g4_eligible") is not True:
            reason = "基线复核不是可用的 G4 PASS；本轮必须完整复核"
        elif not isinstance(base.get("coverage"), dict) or not base.get("coverage"):
            reason = "基线复核没有覆盖快照；本轮必须完整复核"
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
    return {
        "schema": "copilot-review-context/v1",
        "task_id": state["task_id"],
        "contract_sha256": state["contract_sha256"],
        "plan_id": dispatch_env.get("plan_id") or "",
        "plan_sha256": dispatch_env.get("plan_sha256") or "",
        "dispatch_id": execution.get("dispatch_id") or "",
        "work_item_id": execution.get("work_item_id") or "",
        "execution_result_sha256": exec_sha,
        "target_sha256": G.final_evidence_digest(state),
        "review_scope": scope,
        "based_on_review_sha256": based_on,
        "evidence_manifest": manifest,
        "scope_reason": reason,
        "evidence": evidence,
        "criteria": criteria,
        "warnings": warnings,
        "execution_evidence_unbound": drift,
        "previous_review": previous,
        "note": "ingest 会重新校验 scope、绑定与证据覆盖；本清单只用于准备派发。",
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
    p.add_argument("--model-rollout", default="")
    p.add_argument("--output", default="")

    p = sub.add_parser("preflight-commands",
                       help="派发前校验 Reviewer 只读命令清单（不执行命令）")
    p.add_argument("--file", default="",
                   help="命令文件：每行一条，或 JSON 字符串数组")
    p.add_argument("--command", action="append", default=[],
                   help="单条命令；可多次")
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
    p.add_argument("--output", default="")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "execution":
            payload = build_execution(args)
            return _emit(payload, args.output)
        if args.cmd == "review":
            payload = build_review(args)
            return _emit(payload, args.output)
        if args.cmd == "preflight-commands":
            payload = build_preflight(args)
            code = _emit(payload, args.output)
            return code if payload["ok"] else EXIT_USAGE
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

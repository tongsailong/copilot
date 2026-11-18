#!/usr/bin/env python3
"""从 ZCode 实际运行记录生成 Native Bridge 执行信封。

本脚本不派发 Agent、不调用模型，也不写任务状态。它只做一件事：把**宿主已经
产生**的原生运行记录（Agent 的 `metadata.json`、rollout/DB 模型记录、实际证据
文件）转换成 `copilot-native-result/v2` 信封，并由脚本自己计算所有身份绑定、
证据摘要和 `result_sha256`。

为什么需要它
------------
主代理如果手工拼信封，就可能把 Agent ID、Child Session ID、模型名、证据
SHA-256 或计划绑定写错/编造。这里把这些字段从实际记录中提取，并对不上就
失败关闭：

* 执行方身份只来自 `agent-*/metadata.json`；
* 实际模型来自宿主持久化记录（Executor 可回退 rollout）；
* 证据 SHA-256 由脚本读取证据文件计算，不接受调用方传入；
* `plan_id` / `plan_sha256` / `dispatch_id` / `idempotency_key` 只从本地计划
  与 dispatch 复制。

v8 起 G4 原生只读复核已退役：本脚本不再生成 `copilot-native-review/v2` 复核
信封，也不提供 `review` / `review-context` / `capability` 子命令。独立模型复核
由 SOL 零工具单轮复核（`sol_review.py`）承担。

仍然不是密码学证明
------------------
这些记录仍可能被能写本机文件的人伪造；本脚本保证的是“不靠主代理记忆或手抄
身份”，不是宿主签名。

用法
----
    native_agent.py execution --state STATE --dispatch DISPATCH.json \
      --work-item T1 --metadata .../metadata.json \
      --status done --summary "一句话结果" --result-file result.md \
      --evidence E1=/abs/raw.log [--evidence E2=/abs/test.log] \
      --output T1.execution.json

    native_agent.py bind-execution --state STATE --work-item T1

写文件前会调用 `native_bridge` 的正式校验；校验失败不会产生输出文件。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import governance as G
import host_records
import native_bridge as NB  # noqa: E402

EXIT_OK, EXIT_USAGE = 0, 2
MAX_JSON_BYTES = 4 << 20
MAX_RESULT_CHARS = 200_000
_EVIDENCE_RE = re.compile(r"^(E[1-9][0-9]{0,3})=(.+)$")
_MODEL_SCAN_LINES = 200


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


def _parse_iso(value: object, label: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise AgentRecordError(f"{label} 缺失；无法核验执行会话时间窗口。")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise AgentRecordError(f"{label} 不是合法 ISO 时间：{text!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _validate_binding_identity(binding: dict, meta: dict) -> None:
    """核验 binding 确实约束了这次执行会话，而不是复用旧 completed 会话。

    `bind-execution` 的 nonce 必须出现在 Executor prompt 中，且会话创建时间必须
    晚于 binding 创建时间。缺少任一证据都失败关闭，不用“看起来是新的”替代。
    """
    data = meta.get("data") or {}
    prompt = data.get("prompt")
    nonce = str(binding.get("nonce") or "")
    if not nonce:
        raise AgentRecordError(
            "[execution_binding_nonce_missing] execution binding 缺少 nonce；"
            "不能证明执行会话与本次绑定关联。")
    if not isinstance(prompt, str) or nonce not in prompt:
        raise AgentRecordError(
            "[execution_binding_prompt_missing] Executor metadata.prompt 未包含 "
            "binding nonce；不能证明该会话在本次绑定之后创建。")
    session_created = _parse_iso(data.get("createdAt"),
                                 "metadata.createdAt")
    binding_created = _parse_iso(binding.get("created_at"),
                                 "execution_binding.created_at")
    if session_created < binding_created:
        raise AgentRecordError(
            "[execution_binding_session_too_old] 执行会话创建时间早于 binding；"
            "不能复用旧 completed 会话生成执行信封。")


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


_PAYLOAD_PATH_KEYS = ("file_path", "path", "pattern")


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


def _check_output_path(value: str, workspace: str, state_path: str) -> None:
    """--output 在执行/复核快照之后写出，不能落在未被快照排除的工作区路径。"""
    text = str(value or "").strip()
    if not text:
        return
    workspace_root = G.CS.git_root(workspace) or os.path.abspath(workspace)
    absolute = os.path.abspath(os.path.expanduser(text))
    try:
        inside = os.path.commonpath([workspace_root, absolute]) == workspace_root
    except ValueError:
        inside = False
    if not inside:
        return
    task_dir = os.path.dirname(os.path.abspath(state_path))
    rel_task = os.path.relpath(task_dir, workspace_root)
    task_excluded = rel_task != "." and (
        rel_task == ".copilot-tasks" or rel_task.startswith(".copilot-tasks" + os.sep)
        or rel_task == ".audit" or rel_task.startswith(".audit" + os.sep))
    if task_excluded and (absolute == task_dir
                          or absolute.startswith(task_dir + os.sep)):
        return
    raise AgentRecordError(
        "--output 不能写在工作区内：执行/复核快照生成时该文件尚不存在，之后会出现"
        "代码范围不一致。请写到工作区外，或把状态放在 `.copilot-tasks/` / `.audit/`"
        "任务目录下。")


def build_execution(args) -> dict:
    state = G.load_state(os.path.abspath(args.state))
    dispatch_env, native = _dispatch_for_state(state, args.dispatch)
    action = _find_action(dispatch_env, args.work_item)
    meta = _load_metadata(args.metadata)
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
        _validate_binding_identity(binding, meta)
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
    _check_output_path(getattr(args, "output", ""), workspace, args.state)
    evidence_files = list(G._evidence_paths(state))
    for item in env["evidence"]:
        if isinstance(item, dict) and item.get("file"):
            evidence_files.append(str(item["file"]))
    excluded = G.CS.safe_path_excludes(
        evidence_files, workspace, str(getattr(args, "base", "") or "")) \
        + list(G.governance_paths(args.state))
    snapshot = G.capture_code_snapshot(
        workspace, exclude_paths=excluded,
        base=str(getattr(args, "base", "") or ""))
    env["test_commands"] = commands
    env["test_artifacts"] = sorted(set(artifacts))
    env["execution_binding"] = binding_env
    env["code_scope_sha256"] = str(snapshot.get("digest") or "") \
        if snapshot.get("available") else ""
    env["code_scope_excluded_paths"] = sorted(set(excluded))
    env["result_sha256"] = NB.envelope_sha256(env)
    try:
        NB.validate_execution_result(env, native, state)
    except NB.BridgeError as exc:
        raise AgentRecordError(
            f"生成的信封未通过正式校验 [{exc.code}]：{exc}") from None
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
        description="从 ZCode 实际运行记录生成 Native Bridge 执行信封")
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
                   help="任务开始 commit；用于把已提交的任务代码纳入代码快照")
    p.add_argument("--binding", default="",
                   help="bind-execution 返回的 binding_id；高风险任务必填")
    p.add_argument("--db", default=os.path.expanduser(
        "~/.zcode/cli/db/db.sqlite"),
        help="宿主 SQLite；rollout 被清理时从 session_entry 读取模型")
    p.add_argument("--model-rollout", default="",
                   help="仅离线测试/非默认 rollout 路径使用；仍校验 sessionId")
    p.add_argument("--output", default="")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "bind-execution":
            payload = bind_execution(args)
            return _emit(payload, args.output)
        payload = build_execution(args)
        return _emit(payload, args.output)
    except (AgentRecordError, G.GovernanceError, OSError) as exc:
        print(f"native-agent: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())

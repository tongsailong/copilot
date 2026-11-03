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
  verdict/findings/files_inspected/commands_run 等判断内容。

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


def _rollout_model(meta: dict, override: str = "") -> str:
    """从 rollout 读取实际 provider/model；不信任调用方自报的模型名。"""
    child = meta["child_session_id"]
    if override:
        path = os.path.abspath(os.path.expanduser(override))
    else:
        path = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "rollout",
                            f"model-io-{child}.jsonl")
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
    data = _load_json(path)
    if not isinstance(data, dict):
        raise AgentRecordError("Reviewer 原始结论顶层必须是对象。")
    allowed = ("verdict", "findings", "files_inspected", "commands_run",
               "changed_files", "unverified", "residual_risks")
    out = {key: data.get(key) for key in allowed if key in data}
    if "verdict" not in out:
        raise AgentRecordError("Reviewer 原始结论缺少 verdict。")
    return out


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
    raw = _review_input(args.input)
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

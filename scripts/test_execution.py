#!/usr/bin/env python3
"""真实测试执行证据：把宿主 Bash 记录、退出码、产物与代码快照绑定起来。

只读 SQLite（mode=ro）与 execution/attempt 状态；不执行命令、不调用模型。
数据来源：

* `tool_usage`：session/trace/turn/tool_call_id/status/exit_code/时间/输出字节；
* `part`：同一 callID 的 `state.input.command` 与 `state.output`；
* execution 信封：executor.child_session_id、test_commands、test_artifacts、
  可选 code_scope_sha256；
* attempt：冻结的 code_scope_sha256 与 evidence_snapshot。

状态：`verified` / `unverified` / `failed`。缺少宿主字段时只标 unverified，
不把模型自述的“测试通过”当成退出码或产物证据。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import code_scope
import host_records
import native_bridge as NB
from lib_common import sha256_text
from urllib.parse import quote

SCHEMA = "copilot-test-execution/v1"


_canonical = code_scope.canonical_json
_sha_text = sha256_text


def _mentions_token(text: str, token: str) -> bool:
    """逐字匹配 nonce，避免六位子串或相邻十六进制字符被当成绑定。"""
    if not token:
        return False
    pattern = r"(?<![0-9A-Za-z])" + re.escape(token) + r"(?![0-9A-Za-z])"
    return bool(re.search(pattern, text or ""))


def _iso(ms: object) -> str:
    if not isinstance(ms, int) or isinstance(ms, bool):
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000.0, timezone.utc).astimezone() \
            .isoformat(timespec="milliseconds")
    except (OverflowError, OSError, ValueError):
        return ""


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _metadata_path(execution: dict) -> str:
    path = str(execution.get("executor_metadata_path") or "").strip()
    if path:
        return path
    executor = execution.get("executor") or {}
    agent = str(executor.get("agent_id") or "").strip()
    child = str(executor.get("child_session_id") or "").strip()
    if not agent or not child:
        return ""
    root = os.path.expanduser(os.path.join("~", ".zcode", "cli", "agents"))
    for candidate in glob.glob(os.path.join(root, "*", agent, "metadata.json")):
        try:
            with open(candidate, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and str(data.get("agentId") or "") == agent \
                and str(data.get("childSessionId") or "") == child:
            return candidate
    return ""


def _load_metadata(path: str) -> dict:
    if not path or not os.path.isfile(path):
        raise OSError(f"executor metadata 不存在：{path or '-'}")
    path = os.path.abspath(path)
    with open(path, "rb") as fh:
        raw = fh.read()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("executor metadata 不是对象")
    recorded = str(data.get("metadataFile") or "").strip()
    recorded_mismatch = bool(
        recorded and os.path.abspath(os.path.expanduser(recorded)) != path)
    return {"path": path, "sha256": hashlib.sha256(raw).hexdigest(),
            "data": data, "metadata_file_missing": not bool(recorded),
            "metadata_file_mismatch": recorded_mismatch}


def _session_row(db: str, session_id: str) -> tuple | None:
    row = host_records.session_row(db, session_id)
    if row is None:
        return None
    return (row["parent_id"], row["task_type"], row["trace_id"])


def _connect(db: str) -> sqlite3.Connection:
    return host_records.connect_ro(db)


def collect_bash_calls(db: str, session_id: str) -> list[dict]:
    """按 tool_call_id 合并 tool_usage 与 part，返回该 session 的 Bash 调用。"""
    con = _connect(db)
    try:
        rows = con.execute(
            "select tool_call_id, tool_name, status, exit_code, started_at, "
            "completed_at, output_bytes, error_code, error_message, trace_id, "
            "turn_id from tool_usage where session_id=? and tool_name='Bash' "
            "order by started_at, rowid", (session_id,)).fetchall()
        parts = con.execute(
            "select data from part where session_id=?", (session_id,)).fetchall()
    finally:
        con.close()
    by_call: dict[str, dict] = {}
    for (raw,) in parts:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("type") != "tool" \
                or data.get("tool") != "Bash":
            continue
        call_id = str(data.get("callID") or "")
        if not call_id:
            continue
        state = data.get("state") or {}
        if not isinstance(state, dict):
            state = {}
        by_call[call_id] = state
    out: list[dict] = []
    for (call_id, tool, status, exit_code, started, completed, output_bytes,
         error_code, error_message, trace_id, turn_id) in rows:
        state = by_call.get(str(call_id)) or {}
        payload = state.get("input") if isinstance(state.get("input"), dict) else {}
        command = str(payload.get("command") or "")
        output = state.get("output")
        out.append({
            "tool_call_id": str(call_id),
            "session_id": str(session_id),
            "trace_id": str(trace_id or ""),
            "turn_id": str(turn_id or ""),
            "command": command,
            "command_sha256": _sha_text(command),
            "exit_code": exit_code if isinstance(exit_code, int)
            and not isinstance(exit_code, bool) else None,
            "execution_status": str(status or ""),
            "started_at": _iso(started),
            "completed_at": _iso(completed),
            "started_at_ms": started,
            "completed_at_ms": completed,
            "output_bytes": output_bytes if isinstance(output_bytes, int) else None,
            "output_sha256": _sha_text(output) if isinstance(output, str) else "",
            "error_code": str(error_code or ""),
            "error_message": str(error_message or ""),
        })
    return out


def _artifact_checks(execution: dict, attempt: dict) -> tuple[list, list, list]:
    artifacts: list[dict] = []
    problems: list[str] = []
    limitations: list[str] = []
    evidence = {str(item.get("id") or ""): item
                for item in execution.get("evidence") or []
                if isinstance(item, dict)}
    frozen = {str(item.get("id") or ""): item
              for item in attempt.get("evidence_snapshot") or []
              if isinstance(item, dict)}
    declared = [str(item) for item in execution.get("test_artifacts") or []
                if str(item).strip()]
    if not declared:
        problems.append("test_artifacts_not_declared")
    for eid in declared:
        item = evidence.get(eid)
        if not isinstance(item, dict):
            problems.append(f"test_artifact_not_in_execution:{eid}")
            continue
        path = str(item.get("file") or item.get("path") or "")
        recorded = str(item.get("sha256") or "").lower()
        frozen_item = frozen.get(eid)
        if not isinstance(frozen_item, dict):
            problems.append(f"test_artifact_not_frozen:{eid}")
            continue
        if str(frozen_item.get("sha256") or "").lower() != recorded:
            problems.append(f"test_artifact_frozen_sha_mismatch:{eid}")
        if not os.path.isfile(path):
            problems.append(f"test_artifact_missing:{eid}")
            current = ""
        else:
            with open(path, "rb") as fh:
                current = hashlib.sha256(fh.read()).hexdigest()
            if current != recorded:
                problems.append(f"test_artifact_drift:{eid}")
        artifacts.append({"id": eid, "path": path, "sha256": recorded,
                          "current_sha256": current,
                          "frozen_sha256": str(frozen_item.get("sha256") or "")})
    if not artifacts and not problems:
        limitations.append("没有测试产物证据，无法证明测试输出/报告存在")
    return artifacts, problems, limitations


def _junit_summary(path: str) -> dict | None:
    """解析 JUnit XML；返回真实 tests/failures/errors/skipped 与用例数。

    只接受结构化报告，不把任意文本当测试结果；解析失败返回 None。
    """
    if not path or not os.path.isfile(path) or not path.lower().endswith(".xml"):
        return None
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None

    def _tag(element) -> str:
        return str(element.tag or "").split("}")[-1]

    suites = [root] if _tag(root) == "testsuite" else [
        item for item in root.iter() if _tag(item) == "testsuite"]
    if not suites:
        return None

    def _int(node, name: str) -> int:
        try:
            value = node.attrib.get(name)
            return int(value) if value is not None else 0
        except (TypeError, ValueError):
            return 0

    tests = sum(_int(node, "tests") for node in suites)
    failures = sum(_int(node, "failures") for node in suites)
    errors = sum(_int(node, "errors") for node in suites)
    skipped = sum(_int(node, "skipped") for node in suites)
    cases = [item for item in root.iter() if _tag(item) == "testcase"]
    failure_cases = [item for item in cases
                     if any(_tag(child) == "failure" for child in list(item))]
    error_cases = [item for item in cases
                   if any(_tag(child) == "error" for child in list(item))]
    skipped_cases = [item for item in cases
                     if any(_tag(child) == "skipped" for child in list(item))]
    # 子元素比汇总属性更接近事实：属性缺省或偏小时按用例数补齐。
    failures = max(failures, len(failure_cases))
    errors = max(errors, len(error_cases))
    skipped = max(skipped, len(skipped_cases))
    if tests <= 0 and cases:
        tests = len(cases)
    if tests <= 0 or not cases:
        return None
    if tests != len(cases):
        # 汇总 tests 与真实 testcase 数不一致时无法证明“全部测试已报告”。
        return None
    if failures + errors + skipped > tests:
        return None
    return {
        "tests": tests, "failures": failures, "errors": errors,
        "skipped": skipped,
        "passed": max(0, tests - failures - errors - skipped),
        "testcases": len(cases),
        "failed_cases": [str(item.attrib.get("name") or "")
                         for item in failure_cases + error_cases][:20],
    }


def build(execution: dict, attempt: dict, *, db: str,
          binding: dict | None = None,
          require_binding: bool = False,
          expected_contract_sha256: str = "") -> dict:
    """构造确定性 test_execution_binding；不抛异常，用状态表达证据缺口。

    `binding` 是本任务状态中的 execution binding（由宿主在主代理侧创建）；
    `require_binding=True` 时缺失即失败，避免用旧 completed 子会话冒充本轮执行。
    """
    task_id = str(execution.get("task_id") or "")
    work_item = str(execution.get("work_item_id") or "")
    executor = execution.get("executor") or {}
    session = str(executor.get("child_session_id") or "")
    commands = [str(item) for item in execution.get("test_commands") or []
                if str(item).strip()]
    artifacts, artifact_problems, limitations = _artifact_checks(
        execution, attempt)
    problems: list[str] = list(artifact_problems)
    calls: list[dict] = []
    if not session:
        problems.append("executor_session_missing")
    else:
        try:
            calls = collect_bash_calls(db, session)
        except (OSError, sqlite3.Error) as exc:
            problems.append(f"host_db_unavailable:{type(exc).__name__}")
    if not commands:
        problems.append("test_commands_not_declared")
    matched: list[dict] = []
    for command in commands:
        candidates = [item for item in calls if item["command"] == command]
        if not candidates:
            problems.append(f"test_command_not_found:{_sha_text(command)[:12]}")
            continue
        if len(candidates) > 1:
            problems.append(
                f"test_command_ambiguous:{_sha_text(command)[:12]}")
            candidates = candidates[-1:]
        call = candidates[-1]
        if call["execution_status"] != "completed":
            problems.append(
                f"test_command_not_completed:{call['tool_call_id']}")
        if call["exit_code"] is None:
            problems.append(f"test_exit_code_missing:{call['tool_call_id']}")
        elif call["exit_code"] != 0:
            problems.append(
                f"test_exit_code_nonzero:{call['tool_call_id']}:{call['exit_code']}")
        matched.append({
            "execution_evidence_id": "T" + str(len(matched) + 1),
            "tool_call_id": call["tool_call_id"],
            "session_id": call["session_id"],
            "trace_id": call["trace_id"],
            "turn_id": call["turn_id"],
            "command": command,
            "command_sha256": call["command_sha256"],
            "exit_code": call["exit_code"],
            "execution_status": call["execution_status"],
            "started_at": call["started_at"],
            "started_at_ms": call["started_at_ms"],
            "completed_at": call["completed_at"],
            "output_bytes": call["output_bytes"],
            "output_sha256": call["output_sha256"],
        })
    # 产物必须是本轮测试之后生成的：旧报告即使 SHA 未变也不能冒充本轮结果。
    starts = [item.get("started_at_ms") for item in matched
              if isinstance(item.get("started_at_ms"), int)]
    if starts:
        earliest = min(starts)
        for item in artifacts:
            try:
                mtime_ms = int(os.path.getmtime(str(item.get("path") or ""))
                               * 1000)
            except OSError:
                continue
            if mtime_ms + 1000 < earliest:
                problems.append(f"test_artifact_stale:{item['id']}")
    executor_meta = {}
    session_row = None
    try:
        meta_path = _metadata_path(execution)
        if not meta_path:
            problems.append("executor_metadata_missing")
        elif not os.path.isfile(meta_path):
            problems.append("executor_metadata_missing")
        else:
            executor_meta = _load_metadata(meta_path)
            data = executor_meta["data"]
            if executor_meta["metadata_file_missing"]:
                problems.append("executor_metadata_file_missing")
            if executor_meta["metadata_file_mismatch"]:
                problems.append("executor_metadata_file_mismatch")
            executor = execution.get("executor") or {}
            if str(data.get("agentId") or "") != str(
                    executor.get("agent_id") or ""):
                problems.append("executor_metadata_agent_mismatch")
            if str(data.get("childSessionId") or "") != str(
                    executor.get("child_session_id") or ""):
                problems.append("executor_metadata_session_mismatch")
            if str(data.get("status") or "") != "completed":
                problems.append("executor_not_completed")
            # Profile 自洽：metadata 的 profileId 必须与快照名一致；执行方
            # 不得使用只读 Profile——按宿主有效工具策略判定，与名称无关。
            profile = str(data.get("profileId") or "").strip()
            snap_profile = (data.get("profileSnapshot") or {}).get("name") \
                if isinstance(data.get("profileSnapshot"), dict) else ""
            if not profile or str(snap_profile or "") != profile:
                problems.append("executor_profile_mismatch")
            if not NB.reviewer_policy_problems(
                    NB.normalize_profile_policy(data.get("profileSnapshot"))):
                problems.append("executor_profile_readonly")
            snapshot = attempt.get("code_snapshot") or {}
            workspace = ""
            if isinstance(snapshot, dict):
                workspace = str(snapshot.get("workspace") or "")
            workspace = workspace or str(attempt.get("workspace") or "")
            meta_cwd = str(data.get("cwd") or "").strip()
            meta_root = str(data.get("workspaceRoot") or "").strip()
            if not workspace:
                problems.append("attempt_workspace_missing")
            elif not (meta_cwd or meta_root):
                problems.append("executor_workspace_missing")
            else:
                target_ws = os.path.realpath(os.path.expanduser(workspace))
                root = os.path.realpath(os.path.expanduser(meta_root)) \
                    if meta_root else ""
                cwd = os.path.realpath(os.path.expanduser(meta_cwd)) \
                    if meta_cwd else ""
                # workspaceRoot 是会话根，必须等于 attempt 工作区；cwd 允许
                # 位于工作区之下（在子目录启动），但不能越出工作区。
                if root and root != target_ws:
                    problems.append("executor_workspace_mismatch")
                elif not root and cwd:
                    if not (cwd == target_ws or cwd.startswith(target_ws + os.sep)):
                        problems.append("executor_workspace_mismatch")
            session_row = _session_row(
                db, str(executor.get("child_session_id") or "")) \
                if session else None
            if session_row is None and session:
                problems.append("executor_session_missing")
            elif session_row is not None:
                parent, kind, trace = session_row
                if str(kind or "") != "subagent_child":
                    problems.append("executor_session_kind")
                if str(data.get("parentSessionId") or "") != str(parent or ""):
                    problems.append("executor_session_parent_mismatch")
                call_traces = {str(item.get("trace_id") or "")
                               for item in matched if item.get("trace_id")}
                if call_traces and str(trace or "") not in call_traces:
                    problems.append("executor_session_trace_mismatch")
            created = _parse_time(data.get("createdAt"))
            completed = _parse_time(data.get("completedAt"))
            attempt_created = _parse_time(attempt.get("created_at"))
            if created is None or completed is None:
                problems.append("executor_metadata_time_missing")
            else:
                for call in matched:
                    start = _parse_time(call.get("started_at"))
                    done = _parse_time(call.get("completed_at"))
                    if start is None or done is None:
                        problems.append("executor_call_time_missing")
                        continue
                    if start < created or done > completed:
                        problems.append("test_execution_time_outside_session")
                    if attempt_created is not None and done > attempt_created:
                        problems.append("test_execution_after_attempt")
                if attempt_created is not None and completed > attempt_created:
                    problems.append("executor_metadata_after_attempt")
    except (OSError, ValueError) as exc:
        problems.append(f"executor_metadata_unreadable:{type(exc).__name__}")
    attempt_scope = str(attempt.get("code_scope_sha256") or "")
    execution_scope = str(execution.get("code_scope_sha256") or "")
    if not execution_scope:
        problems.append("execution_code_scope_missing")
    elif attempt_scope and execution_scope != attempt_scope:
        problems.append("execution_code_scope_mismatch")
    if not attempt_scope:
        problems.append("attempt_code_scope_missing")
    # execution binding：证明执行会话晚于本轮派发，且命令/工作项/工作区一致。
    env_binding = execution.get("execution_binding") or {}
    if isinstance(env_binding, dict):
        env_binding_id = str(env_binding.get("binding_id") or "")
        env_nonce = str(env_binding.get("nonce") or "")
    else:
        env_binding_id = env_nonce = ""
    if not isinstance(binding, dict) or not binding:
        if require_binding:
            problems.append("execution_binding_missing")
    else:
        if str(binding.get("binding_id") or "") != env_binding_id:
            problems.append("execution_binding_id_mismatch")
        if env_nonce and str(binding.get("nonce") or "") != env_nonce:
            problems.append("execution_binding_nonce_mismatch")
        if str(binding.get("task_id") or "") != task_id:
            problems.append("execution_binding_cross_task")
        if expected_contract_sha256 and str(
                binding.get("contract_sha256") or "") != str(
                    expected_contract_sha256):
            # 合同在 binding 创建后被修订：旧 binding（含 N/A 策略）不能继续证明
            # 当前合同要求的测试。
            problems.append("execution_binding_contract_changed")
        if str(binding.get("work_item_id") or "") != work_item:
            problems.append("execution_binding_work_item_mismatch")
        if not binding.get("consumed"):
            problems.append("execution_binding_not_consumed")
        elif str(binding.get("consumed_by_result_sha256") or "") != str(
                execution.get("result_sha256") or ""):
            problems.append("execution_binding_consumed_by_other")
        expected_commands = [str(item) for item in
                             binding.get("test_commands") or []]
        if expected_commands and expected_commands != commands:
            problems.append("execution_binding_command_mismatch")
        workspace = ""
        snapshot = attempt.get("code_snapshot") or {}
        if isinstance(snapshot, dict):
            workspace = str(snapshot.get("workspace") or "")
        workspace = workspace or str(attempt.get("workspace") or "")
        bound_ws = str(binding.get("workspace") or "")
        if workspace and bound_ws and os.path.realpath(
                os.path.expanduser(workspace)) != bound_ws:
            problems.append("execution_binding_workspace_mismatch")
        binding_created = _parse_time(binding.get("created_at"))
        meta_data = executor_meta.get("data") if executor_meta else {}
        prompt = str((meta_data or {}).get("prompt") or "")
        if env_nonce:
            if not _mentions_token(prompt, env_nonce):
                problems.append("execution_binding_prompt_unbound")
        elif require_binding:
            problems.append("execution_binding_nonce_missing")
        session_created = _parse_time((meta_data or {}).get("createdAt"))
        if binding_created is None:
            problems.append("execution_binding_time_missing")
        elif session_created is not None and session_created < binding_created:
            problems.append("execution_binding_session_precedes")
    if not task_id:
        problems.append("task_id_missing")
    if not work_item:
        problems.append("work_item_missing")
    failed_markers = ("exit_code_nonzero", "test_artifact_drift",
                      "test_artifact_frozen_sha_mismatch",
                      "contract_artifact_missing", "contract_report_missing",
                      "contract_path_outside_workspace",
                      "execution_binding_contract_changed",
                      "test_artifact_stale",
                      "execution_code_scope_mismatch",
                      "executor_metadata_agent_mismatch",
                      "executor_metadata_session_mismatch",
                      "executor_session_kind", "executor_session_parent_mismatch",
                      "executor_session_trace_mismatch",
                      "test_execution_time_outside_session",
                      "test_execution_after_attempt",
                      "executor_metadata_after_attempt",
                      "executor_profile_mismatch", "executor_profile_readonly",
                      "executor_workspace_mismatch",
                      "executor_metadata_file_mismatch",
                      "execution_binding_missing",
                      "execution_binding_id_mismatch",
                      "execution_binding_nonce_mismatch",
                      "execution_binding_cross_task",
                      "execution_binding_work_item_mismatch",
                      "execution_binding_consumed_by_other",
                      "execution_binding_command_mismatch",
                      "execution_binding_workspace_mismatch",
                      "execution_binding_prompt_unbound",
                      "execution_binding_nonce_missing",
                      "execution_binding_session_precedes")
    expected_commands = [str(item) for item in
                         (binding or {}).get("test_commands") or []] \
        if isinstance(binding, dict) else []
    command_execution_verified = bool(commands) and len(matched) == len(commands) \
        and all(item["execution_status"] == "completed"
                and item["exit_code"] == 0 for item in matched)
    expected_test_verified = bool(expected_commands) and \
        expected_commands == commands
    reports = []
    for artifact in artifacts:
        summary = _junit_summary(str(artifact.get("path") or ""))
        if summary is not None:
            summary["artifact_id"] = artifact.get("id", "")
            reports.append(summary)
    # 合同冻结的产物/报告必须出现在本次声明并绑定真实文件；执行后不能换集合。
    contract_report = str((binding or {}).get("contract_report") or "") \
        if isinstance(binding, dict) else ""
    contract_artifacts = [str(item) for item in
                          (binding or {}).get("contract_artifacts") or []] \
        if isinstance(binding, dict) else []
    bound_workspace = str((binding or {}).get("workspace") or "") \
        if isinstance(binding, dict) else ""
    artifact_paths = {os.path.realpath(str(item.get("path") or ""))
                      for item in artifacts}

    def _contract_path(raw: str) -> str:
        value = os.path.expanduser(raw)
        if not os.path.isabs(value):
            value = os.path.join(bound_workspace, value)
        return os.path.realpath(value)

    allowed_roots = [os.path.realpath(bound_workspace)] if bound_workspace else []
    snapshot_ws = str((attempt.get("code_snapshot") or {}).get("workspace") or "")
    if snapshot_ws:
        allowed_roots.append(os.path.realpath(snapshot_ws))

    def _inside_allowed(path: str) -> bool:
        return any(os.path.commonpath([root, path]) == root
                   for root in allowed_roots) if allowed_roots else False

    for raw in contract_artifacts + ([contract_report] if contract_report else []):
        if not _inside_allowed(_contract_path(raw)):
            problems.append(f"contract_path_outside_workspace:{raw}")
    if contract_artifacts:
        for raw in contract_artifacts:
            if _contract_path(raw) not in artifact_paths:
                problems.append(f"contract_artifact_missing:{raw}")
    if contract_report:
        if _contract_path(contract_report) not in artifact_paths:
            problems.append(f"contract_report_missing:{contract_report}")
    test_result = None
    if reports:
        test_result = {
            "artifacts": [item["artifact_id"] for item in reports],
            "report_count": len(reports),
            "tests": sum(item["tests"] for item in reports),
            "failures": sum(item["failures"] for item in reports),
            "errors": sum(item["errors"] for item in reports),
            "skipped": sum(item["skipped"] for item in reports),
            "passed": sum(item["passed"] for item in reports),
            "testcases": sum(item["testcases"] for item in reports),
            "failed_cases": [case for item in reports
                             for case in item["failed_cases"]][:20],
        }
    test_result_verified = bool(
        reports and test_result["failures"] == 0
        and test_result["errors"] == 0
        and not test_result["failed_cases"]
        and test_result["tests"] > 0
        and test_result["testcases"] > 0
        and test_result["passed"] > 0
        and all(item["testcases"] > 0 and item["passed"] > 0
                for item in reports))
    artifact_integrity_verified = bool(artifacts) and not any(
        "artifact" in problem for problem in problems)
    require_result = bool(isinstance(binding, dict)
                          and binding.get("require_test_result"))
    require_artifacts = bool(artifacts) or bool(
        isinstance(binding, dict) and binding.get("require_artifact_integrity"))
    layers_required = {
        "command_execution_verified": bool(commands),
        "expected_test_verified": bool(expected_commands),
        "test_result_verified": require_result,
        "artifact_integrity_verified": require_artifacts,
    }
    required: list[bool] = []
    if commands:
        required.append(command_execution_verified)
    if expected_commands:
        required.append(expected_test_verified)
    if require_result:
        required.append(test_result_verified)
        if test_result is None:
            limitations.append("任务要求结构化测试结果，但未提供可解析的 JUnit XML")
    if require_artifacts:
        required.append(artifact_integrity_verified)
    policy = (binding or {}).get("contract_test_policy") or {} \
        if isinstance(binding, dict) else {}
    contract_no_test = bool(
        isinstance(binding, dict)
        and binding.get("requirement_source") == "contract"
        and policy and policy.get("required") is False
        and not commands and not expected_commands and not artifacts
        and not require_result and not require_artifacts)
    no_test_markers = ("test_commands_not_declared",
                       "test_artifacts_not_declared")
    effective_problems = [item for item in problems
                          if item not in no_test_markers]
    if any(item["execution_status"] != "completed"
           or item["exit_code"] is None for item in matched):
        status = "unverified"
    elif any(marker in problem for problem in problems
             for marker in failed_markers):
        status = "failed"
    elif contract_no_test and not effective_problems:
        # 合同明确不要求测试：A–D 层均为 NOT APPLICABLE，不把 exit 0 当测试通过。
        problems = effective_problems
        status = "not_applicable"
    elif problems or not all(required) or not required:
        status = "unverified"
    else:
        status = "verified"
    body = {
        "schema": SCHEMA,
        "status": status,
        "attempt_id": str(attempt.get("review_attempt_id") or ""),
        "task_id": task_id,
        "work_item_id": work_item,
        "executor_session_id": session,
        "execution_binding_id": env_binding_id,
        "executor_metadata_path": executor_meta.get("path", ""),
        "executor_metadata_sha256": executor_meta.get("sha256", ""),
        "session_parent_id": str(session_row[0] or "") if session_row else "",
        "session_trace_id": str(session_row[2] or "") if session_row else "",
        "commands": matched,
        "artifacts": artifacts,
        "code_scope_sha256": attempt_scope,
        "execution_code_scope_sha256": execution_scope,
        "problems": sorted(set(problems)),
        "limitations": limitations,
        "checks": {
            "command_count": len(matched),
            "declared_command_count": len(commands),
            "artifact_count": len(artifacts),
            "all_exit_zero": bool(matched) and all(
                item["exit_code"] == 0 for item in matched),
            "code_scope_bound": bool(execution_scope)
            and execution_scope == attempt_scope,
        },
        "layers": {
            "command_execution_verified": command_execution_verified,
            "expected_test_verified": expected_test_verified,
            "test_result_verified": test_result_verified,
            "artifact_integrity_verified": artifact_integrity_verified,
        },
        "layers_required": layers_required,
        "required_layers": len(required),
        "test_result": test_result,
    }
    body["binding_id"] = _sha_text(_canonical({
        "schema": body["schema"],
        "status": body["status"],
        "attempt_id": body["attempt_id"],
        "task_id": body["task_id"],
        "work_item_id": body["work_item_id"],
        "executor_session_id": body["executor_session_id"],
        "execution_binding_id": body["execution_binding_id"],
        "commands": body["commands"],
        "artifacts": body["artifacts"],
        "code_scope_sha256": body["code_scope_sha256"],
        "execution_code_scope_sha256": body["execution_code_scope_sha256"],
        "problems": body["problems"],
    }))
    return body

#!/usr/bin/env python3
"""native_agent.py 回归：身份/模型/证据/SHA 只从实际运行记录提取。

这里的 metadata.json、rollout 和证据都是离线测试夹具，不调用任何模型，也不
代表真实 Agent 运行。测试重点是：调用方不能手填身份或摘要；记录不一致时
必须失败关闭。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)

from test_native_bridge import NativeBridgeBase  # noqa: E402

import copilot_auto as A  # noqa: E402
import copilot_dispatch as D  # noqa: E402
import native_agent as G  # noqa: E402
import native_bridge as NB  # noqa: E402


class NativeAgentBase(NativeBridgeBase):
    def _write_json(self, path: str, payload: object) -> str:
        path = os.path.join(self.dir, path)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        return path

    def _metadata(self, name: str, *, agent_id: str, child: str,
                  profile: str, status: str = "completed",
                  model_record: dict | None = None,
                  snapshot_name: str | None = None,
                  metadata_file: str | None = None,
                  usage: dict | None = None,
                  tool_calls: list[dict] | None = None,
                  total_tool_uses: int | None = None,
                  output: object = None) -> tuple[str, str]:
        path = os.path.join(self.dir, f"{name}.metadata.json")
        if total_tool_uses is None:
            total_tool_uses = len(tool_calls) if tool_calls is not None else 3
        payload = {
            "agentId": agent_id,
            "childSessionId": child,
            "profileId": profile,
            "profileSnapshot": {"name": snapshot_name or profile},
            "status": status,
            "createdAt": "2026-11-02T10:00:00.000Z",
            "completedAt": "2026-11-02T10:00:05.000Z",
            "totalDurationMs": 5000,
            "totalToolUseCount": total_tool_uses,
            "usage": usage or {
                "inputTokens": 100, "outputTokens": 20, "totalTokens": 120,
            },
        }
        if metadata_file is not None:
            payload["metadataFile"] = metadata_file
        if output is not None:
            output_path = os.path.join(self.dir, f"{name}.output.json")
            with open(output_path, "w", encoding="utf-8") as fh:
                json.dump(output, fh, ensure_ascii=False, indent=2)
            payload["outputFile"] = output_path
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        rollout = os.path.join(self.dir, f"model-io-{child}.jsonl")
        row = {
            "sessionId": child,
            "model": model_record or {
                "providerId": "deepseek", "modelId": "deepseek-v4-pro"},
        }
        with open(rollout, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
            for idx, call in enumerate(tool_calls or [], 1):
                entry = dict(call)
                entry.setdefault("id", f"call-{name}-{idx}")
                fh.write(json.dumps({
                    "sessionId": child,
                    "response": {"toolCalls": [entry]},
                }) + "\n")
        return path, rollout

    def _dispatch_file(self) -> tuple[dict, str]:
        env, state = self.emit()
        path = self._write_json("dispatch.json", env)
        return state, path

    def _execution_args(self, state: dict, dispatch_path: str, metadata: str,
                        rollout: str, **overrides) -> argparse.Namespace:
        result_path = os.path.join(self.dir, "result.txt")
        with open(result_path, "w", encoding="utf-8") as fh:
            fh.write("T1 actual result\n")
        values = {
            "state": self.state_path,
            "dispatch": dispatch_path,
            "work_item": "T1",
            "metadata": metadata,
            "status": "done",
            "summary": "T1 完成",
            "result_file": result_path,
            "result_text": "",
            "evidence": [f"E1={self.evidence_path}"],
            "model_rollout": rollout,
            "output": "",
        }
        values.update(overrides)
        return argparse.Namespace(**values)


class ExecutionEnvelopeTests(NativeAgentBase):
    def setUp(self):
        super().setUp()
        self.state, self.dispatch_path = self._dispatch_file()
        self.metadata, self.rollout = self._metadata(
            "executor", agent_id="agent-exec", child="sess_subagent_agent-exec",
            profile="general-purpose")

    def test_builds_complete_traceable_execution_envelope(self):
        env = G.build_execution(self._execution_args(
            self.state, self.dispatch_path, self.metadata, self.rollout))
        action = self.emit()[0]["actions"][0]
        self.assertEqual(env["task_id"], self.state["task_id"])
        self.assertEqual(env["round_id"], self.state["rounds"][-1]["round_id"])
        self.assertEqual(env["plan_call_id"], self.state["rounds"][-1]["call_id"])
        self.assertEqual(env["plan_reply_sha256"],
                         self.state["rounds"][-1]["reply_sha256"])
        self.assertEqual(env["dispatch_id"], action["dispatch_id"])
        self.assertEqual(env["idempotency_key"], action["idempotency_key"])
        self.assertEqual(env["work_item_id"], "T1")
        self.assertEqual(env["executor"]["agent_id"], "agent-exec")
        self.assertEqual(env["executor"]["child_session_id"],
                         "sess_subagent_agent-exec")
        self.assertEqual(env["executor"]["profile_id"], "general-purpose")
        self.assertEqual(env["executor"]["model_reported"],
                         "deepseek/deepseek-v4-pro")
        self.assertEqual(env["evidence"][0]["sha256"],
                         NB.sha256_file(self.evidence_path))
        self.assertEqual(env["timing"]["duration_ms"], 5000)
        self.assertEqual(env["usage"], {
            "total_tokens": 120, "input_tokens": 100,
            "output_tokens": 20, "tool_use_count": 3})
        # 生成后仍能通过正式校验。
        NB.validate_execution_result(
            env, self.state["native_dispatch"], self.state)

    def test_does_not_accept_caller_identity_or_hash(self):
        # 调用方只能提供结果正文与证据路径；身份/摘要一律由记录计算。
        args = self._execution_args(
            self.state, self.dispatch_path, self.metadata, self.rollout)
        env = G.build_execution(args)
        self.assertNotIn("agent_id", env)
        self.assertNotIn("executor_agent_id", env)
        self.assertEqual(env["executor"]["agent_id"], "agent-exec")

    def test_rejects_unfinished_metadata(self):
        metadata, rollout = self._metadata(
            "unfinished", agent_id="agent-x", child="sess_subagent_agent-x",
            profile="general-purpose", status="running")
        with self.assertRaises(G.AgentRecordError):
            G.build_execution(self._execution_args(
                self.state, self.dispatch_path, metadata, rollout))

    def test_rejects_profile_snapshot_mismatch(self):
        metadata, rollout = self._metadata(
            "mismatch", agent_id="agent-y", child="sess_subagent_agent-y",
            profile="general-purpose", snapshot_name="reviewer")
        with self.assertRaises(G.AgentRecordError):
            G.build_execution(self._execution_args(
                self.state, self.dispatch_path, metadata, rollout))

    def test_rejects_rollout_from_another_session(self):
        metadata, rollout = self._metadata(
            "other-session", agent_id="agent-z", child="sess_subagent_agent-z",
            profile="general-purpose")
        with open(rollout, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "sessionId": "sess_subagent_agent-someone-else",
                "model": {"providerId": "deepseek", "modelId": "deepseek-v4-pro"},
            }) + "\n")
        with self.assertRaises(G.AgentRecordError):
            G.build_execution(self._execution_args(
                self.state, self.dispatch_path, metadata, rollout))

    def test_rejects_metadata_file_path_copy(self):
        metadata, rollout = self._metadata(
            "copied", agent_id="agent-c", child="sess_subagent_agent-c",
            profile="general-purpose",
            metadata_file=os.path.join(self.dir, "elsewhere.json"))
        with self.assertRaises(G.AgentRecordError):
            G.build_execution(self._execution_args(
                self.state, self.dispatch_path, metadata, rollout))

    def test_cli_writes_output_with_restricted_mode(self):
        args = self._execution_args(
            self.state, self.dispatch_path, self.metadata, self.rollout)
        output = os.path.join(self.dir, "T1.execution.json")
        rc = G.main([
            "execution", "--state", args.state, "--dispatch", args.dispatch,
            "--work-item", args.work_item, "--metadata", args.metadata,
            "--status", args.status, "--summary", args.summary,
            "--result-file", args.result_file,
            "--evidence", args.evidence[0], "--model-rollout", args.model_rollout,
            "--output", output,
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(output))
        self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
        with open(output, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["schema"], NB.RESULT_SCHEMA)
        self.assertEqual(payload["executor"]["agent_id"], "agent-exec")

    def test_evidence_hash_is_recomputed_not_trusted(self):
        env = G.build_execution(self._execution_args(
            self.state, self.dispatch_path, self.metadata, self.rollout))
        with open(self.evidence_path, "a", encoding="utf-8") as fh:
            fh.write("TAMPERED-AFTER-ENVELOPE\n")
        with self.assertRaises(NB.BridgeError) as ctx:
            NB.validate_execution_result(
                env, self.state["native_dispatch"], self.state)
        self.assertEqual(ctx.exception.code, "evidence_sha_mismatch")


class ReviewEnvelopeTests(NativeAgentBase):
    def setUp(self):
        super().setUp()
        self.state, self.dispatch_path = self._dispatch_file()
        self.exec_metadata, self.exec_rollout = self._metadata(
            "executor", agent_id="agent-exec", child="sess_subagent_agent-exec",
            profile="general-purpose")
        self.execution = G.build_execution(self._execution_args(
            self.state, self.dispatch_path, self.exec_metadata,
            self.exec_rollout))
        A._apply_native_execution(self.state_path, self.state, self.execution)
        self.state = D.load_state(self.state_path)
        self.reported_command = (
            "find /tmp -maxdepth 1 -printf '%p\\n' | sort; "
            "stat -c '%a %n' /tmp")
        # 宿主实际输出（outputFile）才是正式结论来源；--input 只是副本，需与它一致。
        self.conclusion = {
            "verdict": "pass",
            "findings": [],
            "files_inspected": [self.evidence_path],
            "commands_run": [self.reported_command],
            "changed_files": [],
            "unverified": [],
            "residual_risks": [],
        }
        self.rev_metadata, self.rev_rollout = self._metadata(
            "reviewer", agent_id="agent-rev", child="sess_subagent_agent-rev",
            profile="reviewer", output={
                **self.conclusion,
                "schema": "copilot-native-review/v1",
                "task_id": self.state["task_id"],
                "work_item_id": "T1",
                "execution_result_sha256": self.execution["result_sha256"],
                "reviewer": {"agent_id": "forged", "profile_id": "general-purpose"},
            }, tool_calls=[
                {"name": "Read", "input": {"file_path": self.evidence_path}},
                {"name": "Bash", "input": {"command": self.reported_command}},
            ])
        self.review_input = self._write_json("review.raw.json", {
            **self.conclusion,
            # 调用方伪造的绑定/身份必须被忽略，而不是被采信。
            "reviewer": {"agent_id": "forged", "profile_id": "general-purpose"},
            "execution_result_sha256": "0" * 64,
        })

    def _sync_host_output(self, metadata: str, payload_path: str) -> None:
        """把宿主输出改成与 `--input` 相同，模拟「正常一致」的调用方副本。"""
        with open(metadata, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        output = str(data.get("outputFile") or "")
        if not output:
            return
        with open(payload_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict):
            payload = {
                "schema": "copilot-native-review/v1",
                "work_item_id": "T1",
                "execution_result_sha256": self.execution["result_sha256"],
                **payload,
            }
        with open(output, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    def _review_args(self, **overrides) -> argparse.Namespace:
        execution_path = self._write_json("execution.json", self.execution)
        values = {
            "state": self.state_path,
            "dispatch": self.dispatch_path,
            "execution": execution_path,
            "work_item": "T1",
            "metadata": self.rev_metadata,
            "input": self.review_input,
            "model_rollout": self.rev_rollout,
            "output": "",
        }
        values.update(overrides)
        if "input" in overrides:
            # 用 --input 覆盖结论时，宿主输出同步成同一份内容（正常一致场景）；
            # 需要测不一致时直接用 _write_json 另写宿主输出。
            self._sync_host_output(values["metadata"], values["input"])
        return argparse.Namespace(**values)

    def _reviewer_with(self, *, tool_calls: list[dict],
                       total_tool_uses: int | None = None) -> tuple[str, str]:
        return self._metadata(
            "reviewer-extra", agent_id="agent-rev2",
            child="sess_subagent_agent-rev2", profile="reviewer",
            output={
                **self.conclusion,
                "work_item_id": "T1",
                "execution_result_sha256": self.execution["result_sha256"],
            },
            tool_calls=tool_calls, total_tool_uses=total_tool_uses)

    def test_builds_review_envelope_from_runtime_records(self):
        env = G.build_review(self._review_args())
        self.assertEqual(env["task_id"], self.state["task_id"])
        self.assertEqual(env["round_id"], self.state["rounds"][-1]["round_id"])
        self.assertEqual(env["dispatch_id"], self.execution["dispatch_id"])
        self.assertEqual(env["work_item_id"], "T1")
        self.assertEqual(env["execution_result_sha256"],
                         self.execution["result_sha256"])
        self.assertEqual(env["reviewer"]["agent_id"], "agent-rev")
        self.assertEqual(env["reviewer"]["child_session_id"],
                         "sess_subagent_agent-rev")
        self.assertEqual(env["reviewer"]["profile_id"], "reviewer")
        self.assertEqual(env["reviewer"]["model_reported"],
                         "deepseek/deepseek-v4-pro")
        self.assertIs(env["g4_eligible"], False)
        self.assertEqual(env["commands_run"],
                         ["find /tmp -maxdepth 1 -printf '%p\\n' | sort; "
                          "stat -c '%a %n' /tmp"])
        NB.validate_review_result(
            env, self.state["native_dispatch"], self.execution, self.state)

    def test_ignores_forged_identity_and_binding(self):
        env = G.build_review(self._review_args())
        self.assertEqual(env["reviewer"]["agent_id"], "agent-rev")
        self.assertNotEqual(env["execution_result_sha256"], "0" * 64)

    def test_rejects_missing_host_output(self):
        # 没有宿主 outputFile 时不得退回调用方副本。
        metadata, rollout = self._metadata(
            "reviewer-no-output", agent_id="agent-r3",
            child="sess_subagent_agent-r3", profile="reviewer",
            tool_calls=[{"name": "Read",
                         "input": {"file_path": self.evidence_path}}])
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args(metadata=metadata,
                                             model_rollout=rollout))
        self.assertIn("review_output_missing", str(ctx.exception))

    def test_rejects_caller_copy_that_rewrites_host_verdict(self):
        # 宿主实际输出 partial，调用方副本写成 pass：必须拒绝，不能采信副本。
        with open(self.rev_metadata, "r", encoding="utf-8") as fh:
            output_path = json.load(fh)["outputFile"]
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump({**self.conclusion, "verdict": "partial",
                       "unverified": ["关键日志缺失"],
                       "work_item_id": "T1",
                       "execution_result_sha256":
                           self.execution["result_sha256"]}, fh)
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args())
        self.assertIn("review_input_mismatch", str(ctx.exception))

    def test_rejects_host_output_bound_to_other_execution(self):
        with open(self.rev_metadata, "r", encoding="utf-8") as fh:
            output_path = json.load(fh)["outputFile"]
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump({**self.conclusion, "work_item_id": "T1",
                       "execution_result_sha256": "f" * 64}, fh)
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args())
        self.assertIn("review_output_binding_mismatch", str(ctx.exception))

    def test_rejects_output_file_outside_metadata_dir(self):
        outside = os.path.join(os.path.dirname(self.dir), "elsewhere-output.json")
        with open(outside, "w", encoding="utf-8") as fh:
            json.dump(self.conclusion, fh)
        path = os.path.join(self.dir, "reviewer-moved.metadata.json")
        with open(self.rev_metadata, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        payload["outputFile"] = outside
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args(metadata=path))
        self.assertIn("review_output_misplaced", str(ctx.exception))

    def test_rejects_claimed_file_without_actual_access(self):
        # 自报了没被任何实际调用访问过的文件，属于未复核即声明。
        other = os.path.join(self.dir, "never-opened.txt")
        with open(other, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        metadata, rollout = self._reviewer_with(tool_calls=[
            {"name": "Read", "input": {"file_path": self.evidence_path}},
            {"name": "Bash", "input": {"command": self.reported_command}}])
        with open(metadata, "r", encoding="utf-8") as fh:
            output_path = json.load(fh)["outputFile"]
        claimed = {**self.conclusion,
                   "files_inspected": [self.evidence_path, other],
                   "work_item_id": "T1",
                   "execution_result_sha256": self.execution["result_sha256"]}
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(claimed, fh)
        copy_path = self._write_json("review.claimed.json", claimed)
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args(metadata=metadata,
                                             model_rollout=rollout,
                                             input=copy_path))
        self.assertIn("review_file_not_inspected", str(ctx.exception))

    def test_rejects_claimed_file_with_common_basename_elsewhere(self):
        """同名不同目录不算访问过：docs/report.md 不能借 report.md 的读取记录过关。"""
        decoy_dir = os.path.join(self.dir, "docs")
        os.makedirs(decoy_dir, exist_ok=True)
        decoy = os.path.join(decoy_dir, "report.md")
        with open(decoy, "w", encoding="utf-8") as fh:
            fh.write("decoy\n")
        actual = os.path.join(self.dir, "report.md")
        with open(actual, "w", encoding="utf-8") as fh:
            fh.write("actual\n")
        metadata, rollout = self._reviewer_with(tool_calls=[
            {"name": "Read", "input": {"file_path": actual}},
            {"name": "Bash", "input": {"command": self.reported_command}}])
        with open(metadata, "r", encoding="utf-8") as fh:
            output_path = json.load(fh)["outputFile"]
        claimed = {**self.conclusion,
                   "files_inspected": [actual, decoy],
                   "work_item_id": "T1",
                   "execution_result_sha256": self.execution["result_sha256"]}
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(claimed, fh)
        copy_path = self._write_json("review.decoy.json", claimed)
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args(metadata=metadata,
                                             model_rollout=rollout,
                                             input=copy_path))
        self.assertIn("review_file_not_inspected", str(ctx.exception))

    def test_rejects_basename_plus_dirname_split_across_bash_segments(self):
        """目录名与文件名分处两条 Bash 命令时不算读过：不能拼凑两块无关访问。"""
        docs_dir = os.path.join(self.dir, "docs")
        other_dir = os.path.join(self.dir, "other")
        os.makedirs(docs_dir, exist_ok=True)
        os.makedirs(other_dir, exist_ok=True)
        decoy = os.path.join(docs_dir, "report.md")
        actual = os.path.join(other_dir, "report.md")
        for path in (decoy, actual):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(path + "\n")
        commands = ["ls docs; cat other/report.md"]
        metadata, rollout = self._reviewer_with(tool_calls=[
            {"name": "Bash", "input": {"command": commands[0]}}])
        with open(metadata, "r", encoding="utf-8") as fh:
            output_path = json.load(fh)["outputFile"]
        claimed = {**self.conclusion,
                   "files_inspected": [actual, decoy],
                   "commands_run": commands,
                   "work_item_id": "T1",
                   "execution_result_sha256": self.execution["result_sha256"]}
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(claimed, fh)
        copy_path = self._write_json("review.split.json", claimed)
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args(metadata=metadata,
                                             model_rollout=rollout,
                                             input=copy_path))
        self.assertIn("review_file_not_inspected", str(ctx.exception))

    def test_rejects_claimed_file_matched_only_as_path_prefix(self):
        """读过 x.py.bak 不能算读过 x.py：路径必须按边界匹配，不能靠子串。"""
        backup = self.evidence_path + ".bak"
        with open(backup, "w", encoding="utf-8") as fh:
            fh.write("backup\n")
        metadata, rollout = self._reviewer_with(tool_calls=[
            {"name": "Read", "input": {"file_path": backup}},
            {"name": "Bash", "input": {"command": self.reported_command}}])
        with open(metadata, "r", encoding="utf-8") as fh:
            output_path = json.load(fh)["outputFile"]
        claimed = {**self.conclusion,
                   "files_inspected": [self.evidence_path],
                   "work_item_id": "T1",
                   "execution_result_sha256": self.execution["result_sha256"]}
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(claimed, fh)
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args(metadata=metadata,
                                             model_rollout=rollout))
        self.assertIn("review_file_not_inspected", str(ctx.exception))

    def test_rejects_non_object_response_row(self):
        """rollout 里 response 不是对象时结构化拒绝，不能抛裸 AttributeError。"""
        metadata, rollout = self._reviewer_with(tool_calls=[
            {"name": "Read", "input": {"file_path": self.evidence_path}},
            {"name": "Bash", "input": {"command": self.reported_command}}])
        with open(rollout, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "sessionId": "sess_subagent_agent-rev2",
                "response": "truncated-record"}) + "\n")
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args(metadata=metadata,
                                             model_rollout=rollout))
        self.assertIn("review_toolcalls_invalid", str(ctx.exception))

    def test_allows_relative_read_for_absolute_claim(self):
        """合法场景不误拒：相对路径读（含 `./` 形式）时，自报绝对路径仍应通过。"""
        parent = os.path.basename(os.path.dirname(self.evidence_path))
        relative = os.path.join(parent, os.path.basename(self.evidence_path))
        for form in (relative, "./" + relative):
            with self.subTest(read=form):
                metadata, rollout = self._reviewer_with(tool_calls=[
                    {"name": "Read", "input": {"file_path": form}},
                    {"name": "Bash", "input": {"command": self.reported_command}}])
                env = G.build_review(self._review_args(metadata=metadata,
                                                       model_rollout=rollout))
                self.assertIn(self.evidence_path, str(env.get("files_inspected")))

    def test_rejects_nested_directory_for_claimed_path(self):
        """读过 docs/archive/report.md 不能算读过 docs/report.md：中间插层不是同一路径。"""
        parent = os.path.dirname(self.evidence_path)
        base = os.path.basename(self.evidence_path)
        nested_dir = os.path.join(parent, "archive")
        os.makedirs(nested_dir, exist_ok=True)
        nested = os.path.join(nested_dir, base)
        with open(nested, "w", encoding="utf-8") as fh:
            fh.write("nested\n")
        metadata, rollout = self._reviewer_with(tool_calls=[
            {"name": "Read", "input": {"file_path": nested}},
            {"name": "Bash", "input": {"command": self.reported_command}}])
        with open(metadata, "r", encoding="utf-8") as fh:
            output_path = json.load(fh)["outputFile"]
        claimed = {**self.conclusion,
                   "files_inspected": [self.evidence_path],
                   "work_item_id": "T1",
                   "execution_result_sha256": self.execution["result_sha256"]}
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(claimed, fh)
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args(metadata=metadata,
                                             model_rollout=rollout))
        self.assertIn("review_file_not_inspected", str(ctx.exception))

    def test_mentions_path_accepts_boundary_forms_and_rejects_foreign_roots(self):
        """完整/直接相对路径命中；不同绝对根、目录插层与 .bak 不命中。"""
        self.assertTrue(G._mentions_path("docs/report.md", "cat docs/report.md"))
        self.assertTrue(G._mentions_path("/repo-a/docs/report.md", "cat docs/report.md"))
        self.assertTrue(G._mentions_path("docs/report.md", "cat /repo-a/docs/report.md"))
        self.assertFalse(G._mentions_path("/repo-a/docs/report.md",
                                          "cat /repo-b/docs/report.md"))
        self.assertFalse(G._mentions_path("docs/report.md", "cat docs/archive/report.md"))
        self.assertFalse(G._mentions_path("docs/report.md", "cat other/report.md"))
        self.assertFalse(G._mentions_path("docs/report.md", "cat docs/report.md.bak"))
        # 分处两个参数不构成一次读取（`cat docs report.md` 读的是目录与另一文件）。
        self.assertFalse(G._mentions_path("docs/report.md", "cat docs report.md"))

    def test_mentions_path_keeps_absolute_relative_distinction(self):
        """绝对声明不能被另一个绝对根或另一条绝对路径满足；./ 等价形式仍通过。"""
        claim = "/repo-a/docs/report.md"
        self.assertFalse(G._mentions_path(claim, "cat /docs/report.md"))
        self.assertFalse(G._mentions_path(claim, "cat /foreign/repo-a/docs/report.md"))
        self.assertTrue(G._mentions_path(claim, "cat ./docs/report.md"))
        self.assertTrue(G._mentions_path(claim, "cat /repo-a/docs/report.md"))
        self.assertTrue(G._mentions_path("docs/report.md", "cat ./docs/report.md"))
        # `..` 无法在无工作目录时核实：一律不算命中。
        self.assertFalse(G._mentions_path(claim, "cat ../docs/report.md"))

    def test_rejects_non_reviewer_profile(self):
        metadata, rollout = self._metadata(
            "wrong-profile", agent_id="agent-w", child="sess_subagent_agent-w",
            profile="general-purpose")
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args(metadata=metadata,
                                             model_rollout=rollout))
        self.assertIn("不是 reviewer", str(ctx.exception))

    def test_rejects_execution_not_ingested(self):
        path = os.path.join(self.dir, "not-ingested.json")
        state = D.load_state(self.state_path)
        state["native_results"] = []
        D.save_state(path, state)
        with self.assertRaises(G.AgentRecordError):
            G.build_review(self._review_args(state=path))

    def test_rejects_tampered_execution_envelope(self):
        bad = dict(self.execution)
        bad["summary"] = "被改写的执行摘要"
        path = self._write_json("tampered.execution.json", bad)
        with self.assertRaises(G.AgentRecordError):
            G.build_review(self._review_args(execution=path))

    def test_rejects_unsafe_reviewer_command(self):
        bad = self._write_json("review.bad.json", {
            "verdict": "pass",
            "findings": [],
            "files_inspected": [self.evidence_path],
            "commands_run": ["ls; rm -rf /tmp/workspace"],
            "changed_files": [],
            "unverified": [],
            "residual_risks": [],
        })
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args(input=bad))
        self.assertIn("review_command_not_readonly", str(ctx.exception))

    def test_rejects_self_reported_command_without_actual_call(self):
        metadata, rollout = self._reviewer_with(tool_calls=[
            {"name": "Read", "input": {"file_path": self.evidence_path}}])
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args(metadata=metadata,
                                             model_rollout=rollout))
        self.assertIn("review_command_not_executed", str(ctx.exception))

    def test_rejects_non_readonly_actual_command(self):
        command = "rm -rf /tmp/workspace"
        metadata, rollout = self._reviewer_with(tool_calls=[
            {"name": "Bash", "input": {"command": command}}])
        bad = self._write_json("review.bad-actual.json", {
            "verdict": "blocked", "findings": [],
            "files_inspected": [self.evidence_path],
            "commands_run": [command], "changed_files": [],
            "unverified": [], "residual_risks": [],
        })
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args(metadata=metadata,
                                             model_rollout=rollout, input=bad))
        self.assertIn("review_command_not_readonly", str(ctx.exception))

    def test_rejects_tool_outside_reviewer_profile(self):
        metadata, rollout = self._reviewer_with(tool_calls=[
            {"name": "Edit", "input": {"file_path": self.evidence_path,
                                       "old_string": "a", "new_string": "b"}}])
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args(metadata=metadata,
                                             model_rollout=rollout))
        self.assertIn("review_tool_not_allowed", str(ctx.exception))

    def test_rejects_incomplete_tool_call_record(self):
        metadata, rollout = self._reviewer_with(
            tool_calls=[{"name": "Read",
                         "input": {"file_path": self.evidence_path}}],
            total_tool_uses=2)
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args(metadata=metadata,
                                             model_rollout=rollout))
        self.assertIn("review_toolcalls_incomplete", str(ctx.exception))

    def test_rejects_duplicate_tool_call_ids(self):
        metadata, rollout = self._reviewer_with(tool_calls=[
            {"id": "dup", "name": "Read",
             "input": {"file_path": self.evidence_path}},
            {"id": "dup", "name": "Bash",
             "input": {"command": self.reported_command}}])
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args(metadata=metadata,
                                             model_rollout=rollout))
        self.assertIn("review_rollout_duplicate_call", str(ctx.exception))

    def test_allows_extra_actual_readonly_commands(self):
        metadata, rollout = self._reviewer_with(tool_calls=[
            {"name": "Read", "input": {"file_path": self.evidence_path}},
            {"name": "Bash", "input": {"command": self.reported_command}},
            {"name": "Bash", "input": {"command": "ls -la /tmp"}}])
        env = G.build_review(self._review_args(metadata=metadata,
                                               model_rollout=rollout))
        # 自报不必复述每次调用，但每条自报都必须有实际调用支持。
        self.assertEqual(env["commands_run"], [self.reported_command])

    def test_rejects_missing_rollout(self):
        metadata, rollout = self._reviewer_with(tool_calls=[
            {"name": "Bash", "input": {"command": self.reported_command}}])
        os.remove(rollout)
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args(metadata=metadata,
                                             model_rollout=rollout))
        # 模型记录读取先于调用记录校验，同样失败关闭；错误需点明 rollout。
        self.assertIn("rollout", str(ctx.exception))

    def test_rejects_rollout_from_another_session(self):
        metadata, rollout = self._reviewer_with(tool_calls=[
            {"name": "Bash", "input": {"command": self.reported_command}}])
        # 首行（模型记录）属于本会话，后续调用行却来自别的会话：模型读取
        # 不会发现，必须由实际调用校验拒绝。
        with open(rollout, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "sessionId": "sess_subagent_agent-someone-else",
                "response": {"toolCalls": [
                    {"id": "c-other", "name": "Bash",
                     "input": {"command": "ls -la /tmp"}}]},
            }) + "\n")
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args(metadata=metadata,
                                             model_rollout=rollout))
        self.assertIn("review_session_mismatch", str(ctx.exception))

    def test_rejects_metadata_without_tool_count(self):
        metadata, rollout = self._reviewer_with(tool_calls=[
            {"name": "Bash", "input": {"command": self.reported_command}}])
        with open(metadata, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        payload.pop("totalToolUseCount", None)
        with open(metadata, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        with self.assertRaises(G.AgentRecordError) as ctx:
            G.build_review(self._review_args(metadata=metadata,
                                             model_rollout=rollout))
        self.assertIn("review_tool_count_missing", str(ctx.exception))

    def test_does_not_write_output_on_validation_failure(self):
        output = os.path.join(self.dir, "must-not-exist.json")
        bad = self._write_json("review.bad2.json", {
            "verdict": "pass",
            "findings": [],
            "files_inspected": [self.evidence_path],
            "commands_run": ["cat /tmp/x > /tmp/y"],
            "changed_files": [],
            "unverified": [],
            "residual_risks": [],
        })
        args = self._review_args(input=bad, output=output)
        rc = G.main([
            "review", "--state", args.state, "--dispatch", args.dispatch,
            "--execution", args.execution, "--work-item", args.work_item,
            "--metadata", args.metadata, "--input", args.input,
            "--model-rollout", args.model_rollout, "--output", output,
        ])
        self.assertEqual(rc, G.EXIT_USAGE)
        self.assertFalse(os.path.exists(output))

    def test_does_not_write_output_when_actual_tool_calls_invalid(self):
        # 失败点在实际调用校验（非 bridge 校验），同样不得留下半个信封。
        output = os.path.join(self.dir, "must-not-exist-2.json")
        metadata, rollout = self._reviewer_with(tool_calls=[
            {"name": "Edit", "input": {"file_path": self.evidence_path,
                                       "old_string": "a", "new_string": "b"}}])
        args = self._review_args(metadata=metadata, model_rollout=rollout,
                                 output=output)
        rc = G.main([
            "review", "--state", args.state, "--dispatch", args.dispatch,
            "--execution", args.execution, "--work-item", args.work_item,
            "--metadata", args.metadata, "--input", args.input,
            "--model-rollout", args.model_rollout, "--output", output,
        ])
        self.assertEqual(rc, G.EXIT_USAGE)
        self.assertFalse(os.path.exists(output))


if __name__ == "__main__":
    unittest.main()

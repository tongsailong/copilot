#!/usr/bin/env python3
"""Agent Bridge 回归：协议绑定、执行/复核分离、只读 Reviewer、旧状态兼容。

不联网、不调用真实模型。这里的状态与信封是测试构造的 fake 数据；静态 Profile
测试只验证 frontmatter/文件位置，不冒充真实 Agent 运行时身份或模型质量。
"""
from __future__ import annotations

import copy
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
LEGACY = os.path.dirname(HERE)
SCRIPTS = os.path.join(os.path.dirname(LEGACY), "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, LEGACY)
sys.path.insert(0, HERE)

from test_e2e_fake_upstream import Upstream, plan, success  # noqa: E402

import copilot_auto as A  # noqa: E402
import copilot_dispatch as D  # noqa: E402
import native_bridge as NB  # noqa: E402

AUTO = os.path.join(LEGACY, "copilot_auto.py")
DISPATCH = os.path.join(LEGACY, "copilot_dispatch.py")


class NativeBridgeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state_path = os.path.join(self.dir, "state.json")
        self.evidence_path = os.path.join(self.dir, "raw.log")
        with open(self.evidence_path, "w", encoding="utf-8") as fh:
            fh.write("NATIVE-EVIDENCE-MARKER: ok\nexit=0\n")
        self.state = self._make_state()

    def _make_state(self, *, backend: str = "native", risk: str = "normal",
                    task_id: str = "native-task", with_action: bool = True,
                    path: str | None = None) -> dict:
        state_path = path or self.state_path
        D.start(
            state_path, "Agent Bridge 测试", "PLAN",
            contract="## 完成条件\n- 工作项完成\n边界：只读测试",
            task_id=task_id, criteria=["工作项完成"], risk=risk,
            execution_backend=backend)
        state = D.load_state(state_path)
        rid = f"{task_id}-r1"
        state["rounds"].append({
            "round_id": rid, "call_id": f"{task_id}-call-1",
            "reply_sha256": NB.sha256_text(f"{task_id}-reply"),
            "model_identity_ok": True, "mode": "PLAN",
            "decision": "continue", "verdict": "partial",
            "ts": D.now_iso(),
        })
        state["round_seq"] = max(int(state.get("round_seq") or 0),
                                len(state["rounds"]))
        if with_action:
            state["actions"].append({
                "id": "T1", "title": "实际执行 T1", "assignee": "zcode",
                "goal": "完成 T1 目标", "constraints": "仅测试目录",
                "expected_evidence": "原始日志", "depends_on": [],
                "status": "pending", "result": "", "evidence_ids": [],
                "source_round_id": rid, "created": D.now_iso(),
                "updated": D.now_iso(),
            })
        D.save_state(state_path, state)
        return D.load_state(state_path)

    def emit(self, state: dict | None = None) -> tuple[dict, dict]:
        env, state = A._emit_native_dispatch(
            self.state_path, state or self.state, persist=True)
        return env, state

    def execution(self, state: dict, dispatch: dict, *,
                  status: str = "done", executor: dict | None = None,
                  result_sha_tamper: bool = False) -> dict:
        action = dispatch["actions"][0]
        digest = NB.sha256_file(self.evidence_path)
        env = {
            "schema": NB.RESULT_SCHEMA,
            "task_id": state["task_id"],
            "round_id": dispatch["round_id"],
            "contract_sha256": state["contract_sha256"],
            "plan_call_id": dispatch["plan_call_id"],
            "plan_reply_sha256": dispatch["plan_reply_sha256"],
            "dispatch_id": action["dispatch_id"],
            "idempotency_key": action["idempotency_key"],
            "work_item_id": action["work_item_id"],
            "status": status,
            "summary": f"{action['work_item_id']} 已完成",
            "result": "实际执行输出：NATIVE-EVIDENCE-MARKER",
            "evidence": [{
                "id": "E1", "file": self.evidence_path,
                "sha256": digest, "kind": "test-log",
                "note": "T1 原始日志",
            }],
            "executor": executor or {
                "kind": "subagent", "agent_id": "agent-executor",
                "child_session_id": "child-executor",
                "profile_id": "general-purpose",
                "model_reported": "deepseek/deepseek-v4-pro"},
            "timing": {"started_at": "2026-11-02T10:00:00+08:00",
                       "finished_at": "2026-11-02T10:00:01+08:00",
                       "duration_ms": 1000},
            "usage": {"total_tokens": 12, "tool_use_count": 1},
        }
        env["result_sha256"] = NB.envelope_sha256(env)
        if result_sha_tamper:
            env["summary"] = "被篡改的摘要"
        return env

    def review(self, state: dict, dispatch: dict, execution: dict, *,
               verdict: str = "pass", findings: list[dict] | None = None,
               unverified: list[str] | None = None,
               changed_files: list[str] | None = None,
               reviewer: dict | None = None,
               files_inspected: list[str] | None = None,
               commands_run: list[str] | None = None,
               g4_eligible: bool = False) -> dict:
        action = dispatch["actions"][0]
        env = {
            "schema": NB.REVIEW_SCHEMA,
            "task_id": state["task_id"],
            "round_id": dispatch["round_id"],
            "contract_sha256": state["contract_sha256"],
            "dispatch_id": action["dispatch_id"],
            "work_item_id": action["work_item_id"],
            "execution_result_sha256": execution["result_sha256"],
            "reviewer": reviewer or {
                "kind": "subagent", "agent_id": "agent-reviewer",
                "child_session_id": "child-reviewer",
                "profile_id": "reviewer",
                "model_reported": "deepseek/deepseek-v4-pro",
            },
            "verdict": verdict,
            "findings": findings or [],
            "files_inspected": files_inspected or [self.evidence_path],
            "commands_run": commands_run or [],
            "changed_files": changed_files or [],
            "unverified": unverified or [],
            "residual_risks": [],
            "g4_eligible": g4_eligible,
        }
        env["result_sha256"] = NB.envelope_sha256(env)
        return env

    def assert_bridge_error(self, code: str, fn, *args, **kwargs):
        with self.assertRaises(NB.BridgeError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))


class DispatchTests(NativeBridgeBase):
    def test_dispatch_binds_verified_round_and_preserves_fields(self):
        original = copy.deepcopy(self.state["actions"][0])
        env, state = self.emit()
        self.assertEqual(env["schema"], NB.DISPATCH_SCHEMA)
        self.assertEqual(env["task_id"], self.state["task_id"])
        self.assertEqual(env["round_id"], self.state["rounds"][-1]["round_id"])
        self.assertEqual(env["contract_sha256"], self.state["contract_sha256"])
        self.assertEqual(env["plan_call_id"], self.state["rounds"][-1]["call_id"])
        self.assertEqual(env["plan_reply_sha256"],
                         self.state["rounds"][-1]["reply_sha256"])
        action = env["actions"][0]
        for key in ("title", "assignee", "goal", "constraints",
                    "expected_evidence", "depends_on"):
            self.assertEqual(action[key], original[key])
        self.assertTrue(action["dispatch_id"].startswith("d-"))
        self.assertTrue(action["idempotency_key"].startswith("ik-"))
        # 第二次生成必须复用同一工作项派发 ID 与幂等键。
        env2, state2 = self.emit(state)
        self.assertEqual(env2["actions"][0]["dispatch_id"], action["dispatch_id"])
        self.assertEqual(env2["actions"][0]["idempotency_key"],
                         action["idempotency_key"])
        self.assertEqual(list(state2["native_dispatch"]["items"]), ["T1"])

    def test_dispatch_rejects_legacy_backend(self):
        path = os.path.join(self.dir, "legacy-dispatch.json")
        state = self._make_state(backend="legacy", task_id="legacy-dispatch",
                                 path=path)
        self.assert_bridge_error(
            "backend_not_native", NB.build_dispatch, state,
            state["actions"], A._native_round_info(state))
        with self.assertRaises(A.AutoError):
            A._emit_native_dispatch(path, state, persist=False)

    def test_dispatch_rejects_unverified_action(self):
        self.state["actions"][0]["source_round_id"] = ""
        self.assert_bridge_error(
            "work_item_unverified", NB.build_dispatch, self.state,
            self.state["actions"], A._native_round_info(self.state))

    def test_dispatch_rejects_action_field_rewrite(self):
        env, state = self.emit()
        bad = copy.deepcopy(env)
        bad["actions"][0]["goal"] = "被主代理改写的新目标"
        self.assert_bridge_error(
            "dispatch_action_mismatch", NB.validate_dispatch, bad, state)

    def test_dispatch_rejects_missing_sol_plan_credential(self):
        state = copy.deepcopy(self.state)
        state["rounds"] = []
        self.assert_bridge_error(
            "round_unverified", NB.build_dispatch, state, state["actions"], {
                "round_id": "r", "plan_call_id": "call",
                "plan_reply_sha256": "a" * 64})

    def test_dispatch_rejects_missing_task_credential(self):
        state = copy.deepcopy(self.state)
        state["task_id"] = ""
        self.assert_bridge_error(
            "credential_missing", NB.build_dispatch, state, state["actions"],
            A._native_round_info(self.state))

    def test_dispatch_rejects_wrong_task_round_contract_and_plan(self):
        env, state = self.emit()
        cases = [
            ("task_mismatch", "task_id", "other-task"),
            ("round_mismatch", "round_id", "other-round"),
            ("contract_mismatch", "contract_sha256", "0" * 64),
            ("plan_call_mismatch", "plan_call_id", "other-call"),
            ("plan_reply_mismatch", "plan_reply_sha256", "1" * 64),
        ]
        for code, field, value in cases:
            with self.subTest(field=field):
                bad = copy.deepcopy(env)
                bad[field] = value
                self.assert_bridge_error(code, NB.validate_dispatch, bad, state)

    def test_dispatch_rejects_duplicate_ids(self):
        env, state = self.emit()
        bad = copy.deepcopy(env)
        bad["actions"].append(copy.deepcopy(bad["actions"][0]))
        self.assert_bridge_error("dispatch_duplicate", NB.validate_dispatch, bad, state)


class ExecutionTests(NativeBridgeBase):
    def setUp(self):
        super().setUp()
        self.dispatch, self.state = self.emit()
        self.native = self.state["native_dispatch"]

    def test_execution_accepts_bound_result_and_registers_evidence(self):
        result = self.execution(self.state, self.dispatch)
        info = A._apply_native_execution(self.state_path, self.state, result)
        self.assertFalse(info["duplicate"])
        state = D.load_state(self.state_path)
        self.assertEqual(state["actions"][0]["status"], "done")
        self.assertEqual(state["actions"][0]["evidence_ids"], ["E1"])
        self.assertEqual(len(state["native_results"]), 1)
        self.assertEqual(state["reports"][-1]["kind"], "execution")
        self.assertFalse(state["reports"][-1]["g4_eligible"])

    def test_execution_is_idempotent_on_same_result_sha(self):
        result = self.execution(self.state, self.dispatch)
        A._apply_native_execution(self.state_path, self.state, result)
        state = D.load_state(self.state_path)
        before = copy.deepcopy(state["reports"])
        again = A._apply_native_execution(self.state_path, state, result)
        self.assertTrue(again["duplicate"])
        self.assertEqual(D.load_state(self.state_path)["reports"], before)

    def test_execution_rejects_wrong_binding(self):
        result = self.execution(self.state, self.dispatch)
        cases = [
            ("task_mismatch", "task_id", "other-task"),
            ("round_mismatch", "round_id", "other-round"),
            ("contract_mismatch", "contract_sha256", "2" * 64),
            ("plan_call_mismatch", "plan_call_id", "other-call"),
            ("plan_reply_mismatch", "plan_reply_sha256", "4" * 64),
            ("idempotency_mismatch", "idempotency_key", "ik-wrong"),
            ("dispatch_mismatch", "dispatch_id", "d-wrong"),
            ("work_item_unknown", "work_item_id", "T9"),
        ]
        for code, field, value in cases:
            with self.subTest(field=field):
                bad = copy.deepcopy(result)
                bad[field] = value
                bad["result_sha256"] = NB.envelope_sha256(bad)
                self.assert_bridge_error(
                    code, NB.validate_execution_result, bad, self.native, self.state)

    def test_execution_rejects_result_sha_tamper(self):
        result = self.execution(self.state, self.dispatch, result_sha_tamper=True)
        self.assert_bridge_error(
            "result_sha_mismatch", NB.validate_execution_result,
            result, self.native, self.state)

    def test_execution_rejects_evidence_tamper(self):
        result = self.execution(self.state, self.dispatch)
        with open(self.evidence_path, "a", encoding="utf-8") as fh:
            fh.write("TAMPERED\n")
        self.assert_bridge_error(
            "evidence_sha_mismatch", NB.validate_execution_result,
            result, self.native, self.state)

    def test_execution_rejects_reviewer_as_executor(self):
        result = self.execution(self.state, self.dispatch, executor={
            "kind": "subagent", "agent_id": "a", "child_session_id": "c",
            "profile_id": "reviewer"})
        self.assert_bridge_error(
            "reviewer_as_executor", NB.validate_execution_result,
            result, self.native, self.state)

    def test_execution_requires_subagent_identity(self):
        result = self.execution(self.state, self.dispatch, executor={
            "kind": "subagent", "profile_id": "general-purpose"})
        self.assert_bridge_error(
            "executor_identity_missing", NB.validate_execution_result,
            result, self.native, self.state)

    def test_execution_rejects_main_without_runtime_identity(self):
        result = self.execution(self.state, self.dispatch, executor={
            "kind": "main", "profile_id": "main",
            "model_reported": "deepseek/deepseek-v4-pro"})
        self.assert_bridge_error(
            "executor_identity_missing", NB.validate_execution_result,
            result, self.native, self.state)

    def test_execution_requires_plan_and_idempotency_binding(self):
        for field in ("plan_call_id", "plan_reply_sha256", "idempotency_key"):
            with self.subTest(field=field):
                result = self.execution(self.state, self.dispatch)
                result.pop(field)
                result["result_sha256"] = NB.envelope_sha256(result)
                self.assert_bridge_error(
                    "schema_invalid", NB.validate_execution_result,
                    result, self.native, self.state)


class ReviewTests(NativeBridgeBase):
    def setUp(self):
        super().setUp()
        self.dispatch, self.state = self.emit()
        self.result = self.execution(self.state, self.dispatch)
        A._apply_native_execution(self.state_path, self.state, self.result)
        self.state = D.load_state(self.state_path)
        self.native = self.state["native_dispatch"]

    def test_review_requires_execution_first(self):
        state = copy.deepcopy(self.state)
        state["native_results"] = []
        review = self.review(state, self.dispatch, self.result)
        with self.assertRaises(A.AutoError) as ctx:
            A._apply_native_review(self.state_path, state, review)
        self.assertIn("尚未入账", str(ctx.exception))

    def test_review_binds_exact_execution_sha(self):
        review = self.review(self.state, self.dispatch, self.result)
        bad = copy.deepcopy(review)
        bad["execution_result_sha256"] = "3" * 64
        bad["result_sha256"] = NB.envelope_sha256(bad)
        self.assert_bridge_error(
            "review_binding_mismatch", NB.validate_review_result,
            bad, self.native, self.result, self.state)

    def test_review_rejects_executor_same_identity(self):
        sub_exec = {
            "kind": "subagent", "agent_id": "agent-shared",
            "child_session_id": "child-shared", "profile_id": "general-purpose"}
        result = self.execution(self.state, self.dispatch, executor=sub_exec)
        # 重新入账该执行结果，确保复核绑定的是新的 executor 元数据。
        state = copy.deepcopy(self.state)
        state["actions"][0]["status"] = "pending"
        state["native_results"] = []
        A._apply_native_execution(self.state_path, state, result)
        state = D.load_state(self.state_path)
        review = self.review(state, self.dispatch, result, reviewer={
            "kind": "subagent", "agent_id": "agent-shared",
            "child_session_id": "child-shared", "profile_id": "reviewer",
            "model_reported": "deepseek/deepseek-v4-pro"})
        self.assert_bridge_error(
            "executor_reviewer_same", NB.validate_review_result,
            review, state["native_dispatch"], result, state)

    def test_review_rejects_changed_files(self):
        review = self.review(self.state, self.dispatch, self.result,
                             changed_files=["/tmp/changed.py"])
        self.assert_bridge_error(
            "review_contaminated", NB.validate_review_result,
            review, self.native, self.result, self.state)

    def test_review_rejects_pass_with_blocking_finding(self):
        review = self.review(self.state, self.dispatch, self.result, findings=[{
            "severity": "blocking", "summary": "仍有缺陷",
            "file": self.evidence_path, "line": 1,
            "evidence": "raw.log 显示 exit=1"}])
        self.assert_bridge_error(
            "pass_with_blocking_finding", NB.validate_review_result,
            review, self.native, self.result, self.state)

    def test_review_rejects_pass_with_unverified(self):
        review = self.review(self.state, self.dispatch, self.result,
                             unverified=["未运行真实设备验收"])
        self.assert_bridge_error(
            "pass_with_unverified", NB.validate_review_result,
            review, self.native, self.result, self.state)

    def test_review_requires_g4_false(self):
        review = self.review(self.state, self.dispatch, self.result,
                             g4_eligible=True)
        self.assert_bridge_error(
            "g4_eligible_forbidden", NB.validate_review_result,
            review, self.native, self.result, self.state)

    def test_review_rejects_write_commands(self):
        for command in (
                "rm -rf /tmp/workspace",
                "sed -n '1w /tmp/out' /tmp/input",
                "find /tmp -fprint /tmp/out",
                "rg --pre 'rm -rf /tmp/x' pattern /tmp",
                "git grep -O 'rm -rf /tmp/x' pattern /tmp",
                "git grep -Osh -c 'id' pattern /tmp"):
            with self.subTest(command=command):
                review = self.review(self.state, self.dispatch, self.result,
                                     commands_run=[command])
                self.assert_bridge_error(
                    "review_command_not_readonly", NB.validate_review_result,
                    review, self.native, self.result, self.state)

    def test_review_rejects_quote_hidden_write_arguments(self):
        # 引号/缩写/clustered 形式在 shell 展开后仍会写文件或执行外部程序；
        # 只靠纯文本正则会被引号骗过，因此每段还要按 argv 再查一次。
        for command in (
                'git grep "-O" \'rm -rf /tmp/x\' pattern /tmp',
                "git grep '-Osh' -c 'id' pattern /tmp",
                "git grep --open 'rm -rf /tmp/x' pattern /tmp",
                "git grep --ope=/bin/echo pattern /tmp",
                "git grep --op=/bin/echo pattern /tmp",
                "git grep --textc pattern /tmp",
                "git grep --tex=pattern /tmp",
                "rg --hostname-bin=/bin/echo pattern /tmp",
                "rg --search-zip pattern /tmp",
                "rg -z pattern /tmp",
                "rg -nz pattern /tmp",
                'git diff "--output=/tmp/x"',
                "git log '--output=/tmp/x'",
                'sort "-o" "/tmp/out" /tmp/input',
                "sort --o=/tmp/out /tmp/input",
                "sort --ou=/tmp/out /tmp/input",
                "sort --outpu=/tmp/out /tmp/input",
                "sort --comp=/bin/sh /tmp/input",
                "sort --temp=/tmp/scratch /tmp/input",
                "sort -no /tmp/out /tmp/input",
                "sort -ro /tmp/out /tmp/input",
                "sort -rTo /tmp/out /tmp/input",
                "find /tmp '-fprint' /tmp/out",
                "find /tmp -- -delete",
                "find -- /tmp -exec rm {} \\;",
                "find /tmp -print -- -fprint /tmp/out",
                "find /tmp -name -- -o -execdir rm {} \\;",
                "find /tmp {-,-}delete",
                "find /tmp -{delete,print}",
                "uniq /tmp/sample-*",
                "uniq /tmp/sample-?",
                "uniq /tmp/[ab]*",
                "ls /tmp/*",
                "uniq /tmp/input /tmp/out",
                "uniq -- /tmp/input /tmp/out",
                "uniq -f 2 /tmp/input /tmp/out",
                "git -c core.pager=sh show HEAD",
                "git -C /tmp -c core.pager=sh show HEAD",
                "git -C /tmp commit -m x",
                "git -C /tmp push",
                "git -C"):
            with self.subTest(command=command):
                review = self.review(self.state, self.dispatch, self.result,
                                     commands_run=[command])
                self.assert_bridge_error(
                    "review_command_not_readonly", NB.validate_review_result,
                    review, self.native, self.result, self.state)

    def test_review_accepts_compound_readonly_commands(self):
        commands = [
            "find /tmp -maxdepth 2 -printf '%M %s %p\\n' | sort; "
            "stat -c '%a %n' /tmp",
            "grep -E 'foo|bar;baz' /tmp/input",
            'grep -E "foo|bar" /tmp/input',
            "cat /tmp/input | head -n 5",
            "sort",
            "cd /tmp; git status --porcelain",
            "ls -la /tmp 2>/dev/null | head -n 5",
            "sort -k2 /tmp/input >/dev/null",
            "grep -c x /tmp/input 2>/dev/null",
            "cat /tmp/input > /dev/null",
            "sort -k 2 /tmp/input 2>/dev/null | uniq",
            # 空格被转义：`2` 属于普通参数 `/tmp/input 2`，不是 fd 编号。
            "cat /tmp/input\\ 2>/dev/null /tmp/other",
            # 转义的反斜杠 + 裸空格：`2` 仍是 fd 编号，正常消费。
            "cat /tmp/input\\\\ 2>/dev/null",
            # U+00A0 不是 shell 分隔符，属于普通参数字符，必须原样保留。
            "cat /tmp/input\u00a0 2>/dev/null",
            "cat /tmp/input\u00a0",
            "grep -o 'x' /tmp/input | sort | uniq -c | sort -rn | head -n 20",
            "git grep -n 'pattern' -- scripts/",
            "git diff HEAD -- scripts/native_bridge.py",
            "sort -k1,1 /tmp/input | uniq -c",
            "uniq -f 2 /tmp/input",
            "uniq -s 1 -w 3 /tmp/input",
            "uniq /tmp/input -",
            "git grep -eO pattern -- scripts/",
            "git grep --only-matching pattern -- scripts/",
            "git grep -e '-O' pattern /tmp",
            "git grep -- -O",
            "rg -n pattern /tmp",
            "rg -e --hostname-bin /tmp",
            "sort --check /tmp/input",
            "sort -t: -k1,1 /tmp/input",
            "sort -t -o /tmp/input",
            "sort -t-o /tmp/input",
            "uniq /tmp/input /dev/stdout",
            "grep -F '{}' /tmp/input",
            "grep -F 'a*b' /tmp/input",
            "ls '/tmp/*.py'",
            "git -C /tmp show --stat HEAD",
            "git -C '/tmp/dir' log --oneline -2",
            "git -C /tmp -C /tmp show HEAD",
        ]
        review = self.review(self.state, self.dispatch, self.result,
                             commands_run=commands)
        validated = NB.validate_review_result(
            review, self.native, self.result, self.state)
        # 原始命令文本必须保留：review 信封摘要绑定的是 Reviewer 的原始输出。
        self.assertEqual(validated["commands_run"], commands)

    def test_review_redirect_preserves_escaped_whitespace_argument(self):
        """消费 `/dev/null` 重定向不得删除普通 argv 字符。

        空格被反斜杠转义时 `2` 是普通参数的一部分（bash 实测 argv 为
        `['cat', '/tmp/input 2', '/tmp/other']`）；只有未转义空白前的 `2`
        才是文件描述符编号。这里同时固定校验结果与实际 shell argv 一致。
        """
        for command, argv in (
                ("cat /tmp/input\\ 2>/dev/null /tmp/other",
                 ["cat", "/tmp/input 2", "/tmp/other"]),
                ("cat /tmp/input 2>/dev/null", ["cat", "/tmp/input"]),
                ("cat /tmp/input 2 >/dev/null", ["cat", "/tmp/input", "2"]),
                ("sort -k2 /tmp/input >/dev/null",
                 ["sort", "-k2", "/tmp/input"]),
                # U+00A0 是词内字符：保持原样，且不因 `.strip()` 被裁掉。
                ("cat /tmp/input\u00a0 2>/dev/null", ["cat", "/tmp/input\u00a0"]),
                ("cat /tmp/input\u00a0", ["cat", "/tmp/input\u00a0"]),
        ):
            with self.subTest(command=command):
                review = self.review(self.state, self.dispatch, self.result,
                                     commands_run=[command])
                NB.validate_review_result(
                    review, self.native, self.result, self.state)
                segments = NB._split_readonly_segments(command, "commands_run[0]")
                self.assertEqual(shlex.split(" ".join(segments)), argv)

    def test_review_rejects_unsafe_compound_commands(self):
        for command in (
                "ls; rm -rf /tmp/workspace",
                "find /tmp -delete | sort",
                "cat /tmp/input | tee /tmp/out",
                "cat /tmp/input > /tmp/out",
                "cat /tmp/input < /tmp/in",
                "ls && ls",
                "cat /tmp/input || ls",
                "cat $(id)",
                "cat `id`",
                "cat \"$(id)\"",
                "sort -o /tmp/out /tmp/input",
                "sort --compress-program=sh /tmp/input",
                "cd /tmp; rm -rf /tmp/workspace",
                "cat /tmp/input 2>/dev/null > /tmp/out",
                "2>/dev/null",
                # 紧贴参数的重定向：`2` 属于 `-k2`，整体替换会让校验 argv 与实际不一致。
                "sort -k2>/dev/null -o /tmp/out /tmp/input",
                "sort -k2 >/dev/null -o /tmp/out /tmp/input",
                "cat /tmp/input>/dev/null",
                "uniq /tmp/input>/dev/null",
                "cat /tmp/input >/dev/nullx",
                # 转义空白后的重定向仍必须是合法 `/dev/null` 目标。
                "cat /tmp/input\\ 2>/dev/nullx",
                # 反斜杠转义的 `2` 不是 fd 编号，也不是合法 token 边界。
                "cat /tmp/\\2>/dev/null",
                # 非 Shell 空白（U+00A0、U+000B）不是分词边界：紧贴 `2` 时按
                # 「紧贴参数」拒绝，不能把它当 fd 删掉普通参数字符。
                "cat /tmp/input\u00a02>/dev/null",
                "cat /tmp/input\u000b2>/dev/null",
                # 非 Shell 空白紧贴 `>` 同样从严拒绝，避免校验 argv 与 Bash 解析歧义。
                "cat /tmp/input\u00a0>/dev/null",
                "ls;",
                ";ls",
                "ls;;stat /tmp",
                "cat /tmp/input | | head",
                "cat 'unterminated",
                "ls\nrm -rf /tmp/workspace"):
            with self.subTest(command=command):
                review = self.review(self.state, self.dispatch, self.result,
                                     commands_run=[command])
                self.assert_bridge_error(
                    "review_command_not_readonly", NB.validate_review_result,
                    review, self.native, self.result, self.state)

    def test_review_requires_real_inspected_file(self):
        review = self.review(
            self.state, self.dispatch, self.result,
            files_inspected=[os.path.join(self.dir, "missing.py")])
        self.assert_bridge_error(
            "review_inspection_invalid", NB.validate_review_result,
            review, self.native, self.result, self.state)

    def test_review_is_separate_and_single_use(self):
        review = self.review(self.state, self.dispatch, self.result)
        info = A._apply_native_review(self.state_path, self.state, review)
        self.assertFalse(info["duplicate"])
        state = D.load_state(self.state_path)
        self.assertEqual(state["review_channel"], "native")
        self.assertEqual(state["review_round"], self.dispatch["round_id"])
        self.assertEqual(state["review_consumed"], [self.result["result_sha256"]])
        self.assertEqual([r["kind"] for r in state["reports"]],
                         ["execution", "review"])
        self.assertEqual(state["reports"][-1]["review_of"],
                         self.result["result_sha256"])
        self.assertFalse(state["reports"][-1]["g4_eligible"])
        # 同一执行结果的第二份不同复核必须拒绝，不能覆盖第一次结论。
        other = self.review(state, self.dispatch, self.result, verdict="partial")
        with self.assertRaises(A.AutoError):
            A._apply_native_review(self.state_path, state, other)
        # 完全相同的复核重复回填是幂等的。
        state = D.load_state(self.state_path)
        again = A._apply_native_review(self.state_path, state, review)
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(D.load_state(self.state_path)["native_reviews"]), 1)

    def test_different_reviewer_cannot_replace_first_review(self):
        first = self.review(self.state, self.dispatch, self.result)
        A._apply_native_review(self.state_path, self.state, first)
        state = D.load_state(self.state_path)
        before = copy.deepcopy(state["native_reviews"])
        second = self.review(state, self.dispatch, self.result, verdict="pass",
                             reviewer={
                                 "kind": "subagent",
                                 "agent_id": "agent-second-reviewer",
                                 "child_session_id": "child-second-reviewer",
                                 "profile_id": "reviewer",
                                 "model_reported": "deepseek/deepseek-v4-pro"})
        with self.assertRaises(A.AutoError):
            A._apply_native_review(self.state_path, state, second)
        after = D.load_state(self.state_path)
        self.assertEqual(after["native_reviews"], before)
        self.assertEqual(after["review_consumed"],
                         [self.result["result_sha256"]])

    def test_native_review_does_not_open_g4(self):
        path = os.path.join(self.dir, "high-native.json")
        state = self._make_state(risk="high", task_id="high-native", path=path)
        dispatch, state = A._emit_native_dispatch(path, state, persist=True)
        result = self.execution(state, dispatch)
        A._apply_native_execution(path, state, result)
        state = D.load_state(path)
        review = self.review(state, dispatch, result)
        A._apply_native_review(path, state, review)
        state = D.load_state(path)
        gate = D._gate_review(state)
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reasons"][0]["code"], "native_review_missing")


class StateAndCliTests(NativeBridgeBase):
    def test_old_v2_state_gets_legacy_defaults_idempotently(self):
        path = os.path.join(self.dir, "legacy-state.json")
        D.start(path, "旧状态", "PLAN", task_id="legacy-state")
        with open(path, encoding="utf-8") as fh:
            raw = json.loads(fh.read())
        for key in ("execution_backend", "native_dispatch", "review_channel",
                    "review_round", "review_consumed", "native_results",
                    "native_reviews"):
            raw.pop(key, None)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, ensure_ascii=False, indent=2)
        state = D.load_state(path)
        self.assertEqual(state["execution_backend"], "legacy")
        self.assertIsNone(state["native_dispatch"])
        self.assertEqual(state["review_consumed"], [])
        D.save_state(path, state)
        again = D.load_state(path)
        self.assertEqual(again["execution_backend"], "legacy")
        self.assertEqual(again["native_results"], [])

    def test_drive_dry_run_emits_native_dispatch_only_for_native(self):
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(
            [sys.executable, "-B", AUTO, "drive",
             "--state", self.state_path, "--dry-run"],
            capture_output=True, text=True, timeout=30, env=env, cwd=self.dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "execute")
        self.assertEqual(payload["execution_backend"], "native")
        self.assertEqual(payload["native_dispatch"]["schema"],
                         NB.DISPATCH_SCHEMA)

        legacy_path = os.path.join(self.dir, "legacy.json")
        D.start(legacy_path, "legacy", "PLAN", task_id="legacy-cli")
        state = D.load_state(legacy_path)
        rid = "legacy-cli-r1"
        state["rounds"].append({
            "round_id": rid, "call_id": "legacy-cli-call",
            "reply_sha256": NB.sha256_text("legacy-reply"),
            "model_identity_ok": True, "mode": "PLAN",
            "decision": "continue", "verdict": "partial",
        })
        state["actions"].append({
            "id": "T1", "title": "legacy t1", "assignee": "zcode",
            "goal": "g", "constraints": "", "expected_evidence": "",
            "depends_on": [], "status": "pending", "result": "",
            "evidence_ids": [], "source_round_id": rid,
        })
        D.save_state(legacy_path, state)
        proc2 = subprocess.run(
            [sys.executable, "-B", AUTO, "drive",
             "--state", legacy_path, "--dry-run"],
            capture_output=True, text=True, timeout=30, env=env, cwd=self.dir)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        payload2 = json.loads(proc2.stdout)
        self.assertEqual(payload2["status"], "execute")
        self.assertNotIn("native_dispatch", payload2)
        self.assertNotIn("execution_backend", payload2)

    def test_cli_rejects_cross_channel_result(self):
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # legacy 任务拒绝 --native-result
        legacy_path = os.path.join(self.dir, "legacy2.json")
        D.start(legacy_path, "legacy", "PLAN", task_id="legacy2")
        proc = subprocess.run(
            [sys.executable, "-B", AUTO, "ingest", "--state", legacy_path,
             "--native-result", os.path.join(self.dir, "missing.json")],
            capture_output=True, text=True, timeout=30, env=env, cwd=self.dir)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("execution_backend=legacy", proc.stderr)
        # native 任务拒绝 --result-file
        proc2 = subprocess.run(
            [sys.executable, "-B", AUTO, "ingest", "--state", self.state_path,
             "--result-file", os.path.join(self.dir, "missing.json")],
            capture_output=True, text=True, timeout=30, env=env, cwd=self.dir)
        self.assertEqual(proc2.returncode, 2)
        self.assertIn("execution_backend=native", proc2.stderr)
        # drive 不再承担 native 回填：明确指向 ingest。
        proc3 = subprocess.run(
            [sys.executable, "-B", AUTO, "drive", "--state", self.state_path,
             "--native-result", os.path.join(self.dir, "missing.json")],
            capture_output=True, text=True, timeout=30, env=env, cwd=self.dir)
        self.assertEqual(proc3.returncode, 2)
        self.assertIn("ingest", proc3.stderr)

    def test_cli_native_execution_ingest_uses_fake_sol_only(self):
        """ingest 纯本地回填；只有随后显式 drive 才产生一次 Fake Upstream 请求。"""
        upstream = Upstream()
        upstream.start()
        self.addCleanup(upstream.close)
        upstream.push(success(plan({
            "decision": "continue",
            "summary": "根据 T1 真实回填继续 T2",
            "actions": [{
                "id": "T2", "title": "T2 假 SOL 安排",
                "assignee": "zcode", "goal": "继续测试",
                "constraints": "仅测试", "expected_evidence": "日志",
                "depends_on": ["T1"],
            }],
        })))
        dispatch, state = self.emit()
        result = self.execution(state, dispatch)
        result_path = os.path.join(self.dir, "execution.json")
        with open(result_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False)
        env = dict(os.environ)
        env.update({
            "CUSTOM_127_API_KEY": "sk-test-native-not-real",
            "COPILOT_AUDIT_TEST_MODE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        audit = os.path.join(self.dir, "audit.jsonl")
        proc = subprocess.run(
            [sys.executable, "-B", AUTO, "ingest",
             "--state", self.state_path,
             "--audit-log", audit,
             "--native-result", result_path],
            capture_output=True, text=True, timeout=30, env=env, cwd=self.dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ingested = json.loads(proc.stdout)
        self.assertEqual(ingested["status"], "ingested")
        self.assertEqual(ingested["native_result"]["kind"], "execution")
        self.assertFalse(ingested["native_result"]["duplicate"])
        self.assertEqual(ingested["http_requests"], 0)
        self.assertEqual(ingested["phase"], "REVIEW_PENDING")
        self.assertEqual(upstream.requests, [])
        saved = D.load_state(self.state_path)
        self.assertEqual(len(saved["native_results"]), 1)
        self.assertIsNone(saved["pending_call"])

        proc2 = subprocess.run(
            [sys.executable, "-B", AUTO, "drive",
             "--state", self.state_path,
             "--audit-log", audit,
             "--work-dir", os.path.join(self.dir, "auto"),
             "--base-url", upstream.url, "--timeout", "5",
             "--max-output-tokens", "4096"],
            capture_output=True, text=True, timeout=40, env=env, cwd=self.dir)
        # 复核未回填：即使执行已完成，也不得把 SOL 暴露给下一轮。
        self.assertEqual(proc2.returncode, 11, proc2.stderr)
        payload = json.loads(proc2.stdout)
        self.assertEqual(payload["status"], "review_pending")
        self.assertEqual(upstream.requests, [])
        self.assertEqual([r["kind"] for r in D.load_state(self.state_path)["reports"]],
                         ["execution"])


class ReviewerProfileTests(unittest.TestCase):
    def test_profile_is_read_only_and_inherits_model(self):
        # v5 使用全局 ~/.zcode/agents/reviewer.md；v6.5 已退役该文件，改由工作区级
        # Profile 承载同一只读语义。两者都不存在时跳过（不是实现回归）。
        candidates = [
            os.path.join(os.path.expanduser("~"), ".zcode", "agents",
                         "reviewer.md"),
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), ".zcode", "agents", "reviewer.md"),
        ]
        path = next((p for p in candidates if os.path.isfile(p)), "")
        if not path:
            self.skipTest("v5 Reviewer 全局 Profile 已退役，工作区 Profile 缺失")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        parts = text.split("---")
        self.assertGreaterEqual(len(parts), 3)
        meta = yaml.safe_load(parts[1])
        self.assertEqual(meta["name"], "reviewer")
        # Reviewer 必须跟随主代理模型路由，不得再钉死某个 Provider/模型。
        self.assertEqual(meta["model"], "inherit")
        self.assertNotIn("thoughtLevel", meta)
        self.assertIn("Read", meta["tools"])
        self.assertIn("Grep", meta["tools"])
        self.assertIn("Glob", meta["tools"])
        self.assertNotIn("Write", meta["tools"])
        self.assertNotIn("Edit", meta["tools"])
        self.assertIn("Write", meta["disallowedTools"])
        self.assertIn("Edit", meta["disallowedTools"])
        # v5 允许只读 Bash；v6.5 起工作区 Profile 不再提供 Bash。
        if ".zcode" in path:
            self.assertIn("Bash", meta["disallowedTools"])
            self.assertFalse(meta["injectAgentsMd"])
            self.assertIn("Do not claim G4 status", parts[2])
        else:
            self.assertTrue(meta["injectAgentsMd"])
            self.assertIn("g4_eligible", parts[2])
        self.assertIn("read-only", parts[2].lower())


if __name__ == "__main__":
    unittest.main()

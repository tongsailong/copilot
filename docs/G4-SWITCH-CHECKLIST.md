# G4 正式切换验收与旧 Reviewer 退役记录

更新：2026-11-15 CST。本清单只记录可在本机复核的事实；未执行项不得标记 PASS。

## 结论

**正式 G4 已切换到新链，旧 Reviewer 体系已退役。** 阶段九在最终源码与最终运行时上完成
真实 Executor → 原生只读 Reviewer → `g4_attest` → `test_execution` → `g4_shadow` →
候选 → 正式 ingest → 一次性消费 → closure 的完整演练；旧全局 Reviewer Profile、Bash
白名单、rollout 强依赖与重复校验入口已删除。仍保留的真实限制见文末。

## 逐项

| # | 条件 | 状态 | 证据与限制 |
|---|---|---|---|
| 1 | 正式 G4 由新链派生 | PASS | `copilot_auto._ingest_review` 仅在 `risk=high` 且 shadow `attested`、`new_g4_eligible=true` 时写 `g4_eligible=true`/`g4_source="candidate"`；信封自报 `g4_eligible` 不生效。阶段九 E2E：正式 `g4_eligible=True`、`g4_source=candidate`、`g4_reasons=[]`。 |
| 2 | 旧 G4 记录不被新放行接受 | PASS | `governance._review_trust_problems` 要求 `g4_source=candidate` 且 `candidate_g4.eligible=true`；否则 `native_review_legacy_g4_source` / `native_review_candidate_not_eligible`。定向测试与 T4 场景覆盖。 |
| 3 | 宿主记录独立取证 | PASS | `g4_attest` 读取 metadata + SQLite `tool_usage`/`part`/`session_entry`：Profile 精确 Read/Grep/Glob、会话/prompt/工作区、工具计数、原始输出 SHA 与 findings。阶段九 E2E attestation `813a58b5…`，status `attested`，agreement `consistent`。 |
| 4 | 原始输出持久化 | PASS | raw 2857 字节 SHA `0d67f731…` 原子写入 `review-outputs/`，attestation 记录 path/SHA/bytes/source/parser；`verify_stored.ok=true`，篡改 verdict 后 `ok=False`。 |
| 5 | 测试退出码来自宿主 | PASS | Executor child `sess_subagent_agent_b9618acf…` 在 binding `EBace3fd…`（nonce 写入 prompt）之后新建，`bash run-tests.sh` 记录 `exit_code=0`、tool_call/trace/turn 完整。 |
| 6 | 测试证据四层 | PASS | `layers` A/B/C/D 全 true（命令/退出码/会话、合同命令、JUnit 计数、产物 SHA 与 mtime）；合同 `test_policy` 冻结命令、report、artifacts 与两个 require 层。 |
| 7 | 高风险必须显式 test_policy | PASS | `start_state`/`amend_contract`/`create_execution_binding` 在 `risk=high` 且合同无 captured 策略时拒绝；`{}` 不能清空已有策略；定向测试覆盖。 |
| 7b | 合同测试要求不可被调用方放宽 | PASS | `test_policy` 进入合同指纹；binding 从合同派生并记录 `requirement_source`/`contract_*`；`native_bridge`/`test_execution` 逐项比对，篡改 → `execution_binding_policy_weakened`/`execution_binding_contract_changed`。 |
| 8 | 测试层 N/A 仅当前合同允许 | PASS | `G.contract_no_test_binding` 要求当前合同有 captured 策略、`required=false`、binding 来自合同且合同 SHA 一致；伪造 contract-source binding 或 amend 后旧 binding 不放行。 |
| 9 | execution binding 一次性消费 | PASS | 阶段九 E2E binding 消费给 execution `07c38ce5…`；重复执行结果复用同一 binding 被 `execution_binding_consumed` 拒绝。 |
| 10 | attempt 一次性消费 | PASS | 正式复核消费 attempt `RAe3297a8b84ab27d69d138ced` 并记录 `consumed_by_result_sha256`；候选再写 `candidate_consumed_by_*`。重放/跨任务/未消费不放行。 |
| 11 | 代码范围快照 | PASS | execution、attempt 与 TE 的 `code_scope_sha256` 三方一致（`e44bc7ef…`），包含未提交 `src/calc.py`；空快照/漂移/越界/submodule 异常 fail-closed。 |
| 12 | 真实 ZCode 子代理端到端 | PASS | `task-stage9e-e2e`：Executor `agent_b9618acf…`、Reviewer `agent_8eb506b1…`（worker profile `reviewer`，4×Read）、review `0174ba43…`、候选 `eligible=true`、shadow `attested` + eligible、`drive` 直接 `CLOSED/pass`。 |
| 13 | Reviewer 只读 Profile 真实加载 | PASS | 阶段九新 headless 进程：Skill `v6.5.0` 加载，实际 `Read(.zcode/agents/reviewer.md)`，tools `[Read,Grep,Glob]`，`global_profile_exists=false`；真实 Reviewer 会话仅 Read。 |
| 14 | 旧全局 Profile 退役 | PASS | 源码 `agents/reviewer.md` 与运行时 `~/.zcode/agents/reviewer.md` 已删除；`sync_runtime` 不再分发 Reviewer；`~/.zcode/agents` 为空。 |
| 15 | Bash 白名单退役 | PASS | `native_bridge` Reviewer Bash 白名单与 `native_agent` Bash 工具许可已删除；`commands_run` 非空直接 `review_command_unexpected`；旧 `preflight-commands` 已移除。 |
| 16 | rollout 强依赖退役 | PASS | Reviewer 模型/工具/输出只从宿主 DB；`build_review` 不再读 rollout，host `rollout_path=""`、`tool_call_source=db.tool_usage+part`。 |
| 17 | 重复 G4 校验入口退役 | PASS | `verify_review_host_record`、`_g4_problems`、`_review_tool_calls`、`_evidence_not_inspected`、`_attempt_session_problems`、`_review_profile_problems` 已删除；正式资格唯一由 `g4_attest`/`test_execution`/`g4_shadow` 决定。 |
| 18 | 统一输出解析 | PASS | `review_output`（`review-json/v2`）是唯一语法解析；`native_agent` 与 `g4_attest` 都调用它；多围栏/叙述/重复键/非对象拒绝。 |
| 19 | 宿主输出回退与篡改拒绝 | PASS | outputFile 优先，缺失回退 DB `message/part`，两者都在时必须 SHA 一致；阶段九 `verify_stored` 正例/篡改反例通过。 |
| 20 | 原子性与故障注入 | PASS | 复核/执行各自单次 `save_state`；保存失败不留下候选/attempt/binding 单边消费；状态损坏只记录不伪 PASS。T4 `ingest_interrupted` 等场景通过。 |
| 21 | 历史证据只读保留 | PASS | 阶段七/八 attestation 与旧台账仍可 `verify_stored` 读取；旧记录无 findings 摘要时显式 limitation。未删除历史审计。 |
| 22 | 活动测试 | PASS | `541 tests OK`（38.8s）；删除的是旧链专属用例，新增/改造用例覆盖正式切换、旧来源拒绝、Bash 退役、Profile 退役。 |
| 23 | 历史回归 | PASS | `853 tests OK (skipped=31)`；v5 全局 Profile 存在性用例在 v6.5 退役后改用工作区 Profile 语义。 |
| 24 | T0–T4 隔离验收 | PASS | T0 0.6s / T1 18.6s / T2 39.4s / T3 91.5s；34/34 场景通过（新增 formal candidate、legacy source、Reviewer Bash、Profile 退役）。 |
| 25 | 运行时同步 | PASS | `sync_runtime.py --apply` 写入 15 个受管理文件；`--check` 缺失 0 / 不同 0；8 个未受管理运行时文件保留未清理；基线 25 个文件。 |
| 26 | 真实 E2E 在最终运行时/源码 | PASS | `task-stage9e-e2e` 使用最终源码脚本与最终运行时 Skill；Reviewer 使用工作区 Profile；closure 由 `drive` 自动完成，无手工改状态。 |

## 退役清单（已执行）

1. 源码 `agents/reviewer.md` 与运行时 `~/.zcode/agents/reviewer.md`。
2. `native_bridge` Reviewer Bash 只读白名单、命令派生与 `validate_review_commands`。
3. `native_agent` 的 Reviewer rollout 读取、旧会话/宿主记录复核、重复证据计算、
   `preflight-commands`。
4. `copilot_auto._ingest_review` 对 `verify_review_host_record` 的调用与旧宿主复核分支。
5. `sync_runtime.py` 的 Reviewer 分发目标与基线条目。
6. 旧链专属测试（旧 Bash/preflight、rollout fallback、旧会话信封）改为新链语义或删除。

保留：工作区通用只读 Profile `.zcode/agents/independent-readonly.md`（Read/Grep/Glob，
v6.6.1 起取代 `reviewer.md`；纯 JSON 输出要求随派发 prompt）、ZCode 原生执行/复核子代理
能力、执行方 Bash、`g4_attest`/`test_execution`/`g4_shadow`、execution binding、attempt、
原始输出持久化、历史 attestation 与旧台账。

## 回退

- 回退以普通 `git revert <切换提交>` 落地，保留已消费 marker、新 attestation 与历史状态；
  不删除分支、不 force push、不改写共享历史。
- 回退后旧正式 G4 代码恢复，但已删除的旧全局 Profile 不会自动恢复；需要时从回退提交
  重新取出并单独授权下发。运行时 Skill 用 `sync_runtime.py --apply` 回到回退版本。
- 回退不改变任务授权、业务目标或历史审计只读保留原则。

## 残余风险（诚实边界）

- **无签名**：metadata/SQLite/状态文件不是密码学签名；能改写本机文件的人仍可伪造。
- **Profile 白名单是宿主约束**：未做“恶意尝试 Bash 后拒绝”的主动渗透测试；宿主按
  `tools`/`disallowedTools` 生效。
- **真实模型输出**：本轮真实 Reviewer 正常输出小写 `pass`；大写归一化仍只有单元测试。
- **历史增量基线**：阶段四及更早 attestation 无 `target_source`，需完整复核生成新基线。
- **执行退出码绑定**：依赖宿主 `tool_usage.exit_code`；宿主库被整体替换不在防护范围。
- **Profile 来源/路径不再门禁（v6.6.0）**：`g4_attest` 已移除
  `allowed_profile_sources`、`require_workspace_profile` 与 CLI
  `--allowed-profile-source`/`--allow-user-profile`；只读边界按宿主
  `profileSnapshot` 的有效工具策略判定，Profile 名称/来源/路径只作记录事实
  （见 `docs/DECISIONS.md` 第 47 条）。专用只读 Profile 文件仍然保留：宿主 3.18.2
  没有派发期工具白名单，删掉它就无法证明只读。
- **收口不重读 raw 文件**：G4 gate 核对 `host_sha256` 与 candidate 字段；raw 文件在
  ingest 后被改写不会被 gate 自动发现，需用 `verify_stored` 或独立审计复核。

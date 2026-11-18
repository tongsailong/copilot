# agent-bridge：原生执行信封（v2）

本文件说明 Native Bridge：**ZCode 原生 Agent 执行 → Copilot 校验回填**。
v1 信封（SOL 通道时代）已退役，出现即 `schema_retired`。

> **v8.0.0 起 G4 原生只读复核正式退役**：`copilot-native-review/*` 信封、
> `native_agent.py review` / `review-context` / `capability`、review attempt 与
> `g4_attest`/`g4_shadow`/`test_execution` 已删除。独立复核请使用
> `copilot_dispatch.py sol-review`（零工具、单轮、advisory）；历史复核记录只读保留。
> v8.1.0 起 SOL 统一 `gpt-6.2-sol` + `reasoning.effort=high`，并新增建议性
> `sol-advisory --mode CONSULT|PLAN`（同样零工具、单轮、不入门禁）。

## 1. 角色与边界

| 角色 | 职责 | 约束 |
|---|---|---|
| Executor | 执行单个工作项，产出结果与原始证据 | 必须是原生子代理（`kind=subagent`），带 Agent/Child Session 身份与 profile 快照 |
| SOL（REVIEW / CONSULT / PLAN） | 独立复核与认知辅助（advisory） | 由 `sol_review.py` 零工具单轮调用；不读写文件、不证明宿主权限、不构成收口门禁 |

Copilot 不派发 Agent、不调用模型、不写别人的会话。它只校验宿主已产生的记录并回填状态。
`native_agent.py` 从真实 `metadata.json`、宿主 SQLite（`tool_usage`/`part`/`session_entry`）
与证据文件生成信封，避免主代理手抄身份。

## 1.1 内置子代理能力与 Profile 发现（ZCode 3.18.2）

以本机安装的 ZCode 3.18.2 为准，宿主内置子代理只有两个（没有 Plan Agent）：

| Agent | 宿主工具集 | AGENTS.md | MCP | 可写 |
|---|---|---|---|---|
| `Explore` | `Bash,Glob,Grep,Read,WebFetch,WebSearch,TodoWrite` | `injectAgentsMd=false` | 不继承（显式工具集不含 `mcp__*`） | 是（Bash） |
| `general-purpose` | `["*"]` | `injectAgentsMd=true` | 继承 | 是 |

依据：

- 宿主源码把 `Explore` 的允许工具固定为上述列表；`Agent` 工具参数只有
  `description/prompt/subagent_type/run_in_background`，**没有按派发覆盖工具的参数**。
- 真实 `metadata.json` 的 `profileSnapshot.tools` 与 `tool_usage` 一致；子代理会话中
  **completed** 的 `Bash` 记录为 `read_only=0, side_effect_scope=system`（失败调用可能
  为 `NULL`），`Write/Edit/ApplyPatch` 也会记为工作区写入。因此“含 Bash”不是理论风险，
  而是宿主无法证明只读。宿主 3.18.2 的写工具集合为 `Write/Edit/ApplyPatch/Bash`，
  执行类 `js*` 与代理类 `Agent/Task` 同样能改文件。
- `general-purpose` 的 `tools=["*"]` 会继承主会话 MCP；真实子会话中可见
  `mcp__huashu-chrome__*` 等调用。它不能当作只读复核者。
- 没有 OS 级只读沙箱：`Read` 没有路径限制；`profileSnapshot` 只是宿主施加的工具白名单。
  后台派发（`run_in_background`/自动 background）只改变调度与回传方式，**不改变工具集**，
  因此“后台 Explore”仍含 Bash，不能当作只读模式。

**Profile 发现与加载**（`rCi` 于会话 bootstrap 执行）：

1. 来源：`~/.zcode/agents`（user）→ `<会话工作目录>/.zcode/agents`（project）→
   `additionalDirectories` 下同名目录（带 namespace）；后加载覆盖前者，project 优先于 user，
   user/project 同名可覆盖内置。`agents-state.json` 的 `disabledAgentIds` 可禁用 user Profile。
2. **加载时机是会话/进程启动**；Profile 文件新建、改名或删除不会热更新到已启动会话。
   Desktop 若通过 `subagentsReadRuntimeConfig` 返回 `kind=built-in-fallback`，本会话只有
   内置 Agent，不会扫描工作区目录。
3. 未知 `subagent_type` 返回可恢复错误 `UNKNOWN_AGENT_TYPE` 并列出可用类型，
   **不会静默回退** general-purpose；只有省略 `subagent_type` 才等同于 general-purpose。
4. 项目级 Profile 的真实快照通常不含 `permissionMode`（宿主不记录），判定必须允许缺省；
   `tools`/`disallowedTools`/`path`/`source`/`systemPrompt` 会记录。

**上下文与结果回传**：子代理使用独立上下文；内置 `Explore` 不注入 AGENTS.md，
`general-purpose` 会注入。前台 `Agent` 调用只返回一段 hand-back 文本（含 `agentId` 与
usage），**不返回** status/childSessionId/totalToolUseCount；后三者与最终输出必须从
`metadata.json`、DB `message`/`part`/`tool_usage` 读取。后台派发通过完成通知/`TaskOutput`
取回，不能假设自动重试；`SendMessage` 可续同一 child session，但复核链路每轮必须新建
会话（宿主会重置 `totalToolUseCount`）。

**历史症状的根因**（真实宿主记录，非推测）：

- `profile_tools_not_exact` 来自实际派发的 Profile 不满足精确只读：旧 user `reviewer`
  含 `Bash`（153 次真实快照）、内置 `Explore`（153 次）、`general-purpose`（109 次）。
  判定只看有效工具策略，与名称/路径无关，所以这是正确拒绝，不是名称匹配 bug。
- “新会话没发现只读 Profile”通常是：会话工作目录不是仓库根、会话在 Profile 新建/改名之前
  启动（沿用启动期缓存）、或 Desktop runtime config 回退为 built-in-only。新会话应先确认
  `metadata.profileId`/`profileSnapshot.source` 与可用类型，再决定是否需要重新派发。

**plan 模式会让有效工具策略变宽（真实记录）**：父会话 `planEnabled=true` 时，子会话
`runtime/execution_state` 记录 `planEnabled=true`，宿主会给子代理注入 `Bash` 并可真实执行
（一个 `independent-readonly` 子会话执行过 `git status --short`，`status=completed,
read_only=0`）。因此“精确 Read/Grep/Glob”不能只看 `profileSnapshot`：`capability` 与
`g4_attest` 都读取子会话 `runtime/execution_state`，要求 `planEnabled=false`；记录缺失、
字段缺失或非布尔一律 fail-closed（`host_session_state_missing` /
`host_session_state_unknown` / `host_session_plan_mode`）。普通 `yolo`/`build` 模式（非
plan）下 Profile 白名单仍生效：真实子会话请求 `Bash` 返回 `tool_not_found`。

**Profile 来源（宿主加载器 `rCi`）**：`~/.zcode/agents` → `source:"user"`，
`<会话工作目录>/.zcode/agents` → `source:"project"`，additionalDirectories 下同名目录也
是 `project`（带 namespace）；内置只有 `general-purpose`（`tools=["*"]`）与 `Explore`
（`tools=[Bash,Glob,Grep,Read,WebFetch,WebSearch,TodoWrite]`），两者 `source:"built-in"`。
`source` 由加载目录赋值，不是 frontmatter 可改字段。`.zcode/agents/independent-readonly.md`
因此是**项目级自定义 Agent**，不是内置子代理。

**RespondToCoordinator（宿主控制平面工具）**：宿主给所有子代理无条件注入
`RespondToCoordinator`（`sj`），schema 只有 `{summary, message}`，handler 要求
`runtimeScope="subagent"` 并通过 `coordinatorResponsePort.respond` 写入父运行时队列；
权限 `agent.message.respond`、`sideEffectScope="session"`、`riskLevel="low"`、
`needsApproval=false`。它不能读写文件或执行命令。`brc` 最后用 `x3n` 无条件追加它，
即使 Profile 的 `disallowedTools` 也无法移除。它不在声明白名单内，历史复核代码也
不把它加入只读白名单；按“实际工具集字面精确等于 Read/Grep/Glob”的口径，任何子代理都无法满足
字面精确，按“无额外文件/命令能力”的口径不影响只读性。

**历史结论（v6.12.0，G4 时代）**：内置 `Explore` 与 `general-purpose` 都不满足 G4 的
“不存在 `Write/Edit/Bash` 能力”要求；项目/用户/插件自定义 Profile 即使工具集精确只读，
也不是原定架构允许的载体（`profile_source_not_builtin`）。宿主没有内置精确只读 Agent，
`Agent` 工具也没有派发期 `tools` 参数，因此 v6.12.0–v7.0.0 的正式 G4 结论一直是
**BLOCKED**（`host_capability_missing`）。

**v8.0.0 决策**：不再为这一能力缺口维护 G4 体系。G4 的宿主只读载体要求、权限快照准入、
plan 模式校验、独立取证与 `capability` 诊断全部退役；普通子代理（含 `Explore` 的 Bash）
只按普通能力使用。独立质量审查由 SOL 零工具单轮复核按风险提供，它是 advisory，不声称
宿主只读权限。

## 2. 信封 v2

### 2.1 dispatch（`copilot-native-dispatch/v2`）

由 `copilot_auto.py drive` 自动生成（`--backend native` 且工作项属于已登记计划）：

```json
{
  "schema": "copilot-native-dispatch/v2",
  "dispatch_id": "b-<24 hex>",
  "task_id": "task-...",
  "contract_sha256": "<64 hex>",
  "plan_id": "p-<16 hex>",
  "plan_sha256": "<64 hex>",
  "iteration_id": "I3",
  "created_at": "ISO8601",
  "actions": [{
    "work_item_id": "A1",
    "dispatch_id": "d-<24 hex>",
    "idempotency_key": "ik-<24 hex>",
    "title": "...", "assignee": "zcode", "goal": "...",
    "constraints": "...", "expected_evidence": "...", "depends_on": []
  }]
}
```

- `dispatch_id` 与 `idempotency_key` 由 `task_id + plan_id + plan_sha256 + work_item_id`
  确定性派生：同一计划同一工作项重复派发得到相同 id（幂等），计划变化则 id 变化。
- 只有 `execution_backend=native` 才能生成；`legacy` 后端直接 `backend_not_native`。
- 不做空派发（`dispatch_empty`）；同批不得重复工作项/id。
- 既有 dispatch 的 id 与当前绑定不一致时拒绝覆盖（`dispatch_id_drift`）。

### 2.2 execution（`copilot-native-result/v2`）

```json
{
  "schema": "copilot-native-result/v2",
  "task_id": "...", "contract_sha256": "...",
  "plan_id": "...", "plan_sha256": "...", "iteration_id": "I3",
  "dispatch_id": "d-...", "idempotency_key": "ik-...",
  "work_item_id": "A1",
  "status": "done|failed|blocked|dropped",
  "summary": "一句话结果",
  "result": "实际结果（文件/命令/diff 摘要）",
  "evidence": [{"id": "E1", "file": "/abs/path", "sha256": "<64 hex>", "kind": "file"}],
  "executor": {"kind": "subagent", "agent_id": "...", "child_session_id": "...",
               "profile_id": "<实际 Profile 名>",
               "profile": {"name": "...", "source": "built-in|project|user",
                            "path": "", "tools": ["*"], "disallowedTools": [],
                            "permissionMode": ""},
               "model_reported": "..."},
  "timing": {"duration_ms": 1200},
  "usage": {"total_tokens": 321},
  "result_sha256": "<信封自身摘要>"
}
```

校验要点：

- `task_id` / `contract_sha256` / `plan_id` / `plan_sha256` / `dispatch_id` /
  `idempotency_key` / `iteration_id` 必须与当前 dispatch 一致。
- `result_sha256` 必须等于 `envelope_sha256(信封去掉该字段)`；被改动即 `result_sha_mismatch`。
- 证据至少一条；文件必须是绝对路径、存在、可读；SHA-256 必须与文件实际内容一致
  （`evidence_sha_mismatch`），且证据属于本任务（`evidence_foreign`）。
- 已终态（done/dropped）的工作项不能被不同结果覆盖（`work_item_terminal`）；相同
  `result_sha256` 重复回填是幂等成功。修复后需要重新验证同一工作项时，先用
  `copilot_dispatch.py rerun --state ... --id <工作项> --reason <原因>` 把它重开为
  `pending`（旧结果保留在 `rerun_history`），再回填新的执行结果；不要为了让新结果
  通过而伪造新工作项或改写历史。
- 执行方必须提供 `executor.profile` 有效策略（缺失 → `executor_profile_invalid`），
  且不得等价于只读复核（`executor_profile_readonly`）；与 Profile 名称无关。
- **信封/输出文件不要写进代码工作区**：执行信封与 `--output` 若位于代码工作区，会在执行
  快照生成后才出现，attempt 复核时包含它而执行侧没有，最终得到
  `native_review_code_scope_mismatch` / `execution_code_scope_mismatch`。新版执行信封
  固化 `code_scope_excluded_paths` 供 attempt 复用；未被快照排除的工作区内 `--output`
  会被直接拒绝。`--result-file` 若位于工作区内且未被排除，会进入执行代码快照，必须在
  复核前保持稳定。把 `A1.execution.json`、`A1.review.json` 与输出写到工作区外或
  `.copilot-tasks/` 任务目录。

### 2.3 review（已退役）

`copilot-native-review/v2` 信封与复核 ingest 已随 G4 退役；`native_bridge` 遇到该 schema
返回 `native_review_retired`（v1 返回 `schema_retired`）。历史复核记录只保存在任务状态的
`native_reviews` 中，供审计追溯，不再参与门禁或派生。

## 3. 历史兼容

v7 及更早的状态可能包含 `native_reviews` / `review_attempts` / `g4_attestations` 与
`g4_eligible` / `g4_source` / `candidate_g4` 字段，历史 review attempt 还带
`code_snapshot`（冻结的代码范围）。这些字段原样保留，只读查看：
`legacy-status`、`review-metrics`（标记 `g4.retired`）与状态文件本身。任何新代码路径不得
读取它们做准入、派生或收口判断，也不得批量改写为 PASS。

## 4. `native_agent.py` 用法

```bash
# 派发前创建 execution binding：合同测试策略 + 工作区 + nonce
python3 -B scripts/native_agent.py bind-execution --state T/state.json \
  --work-item A1 --workspace "$(git rev-parse --show-toplevel)"

# 执行信封：身份/证据/摘要全部由脚本从真实记录提取
python3 -B scripts/native_agent.py execution --state T/state.json \
  --dispatch T/state.dispatch.json --work-item A1 \
  --metadata <agent-exec>/metadata.json --status done --summary "完成" \
  --result-file result.md --evidence E1=/abs/raw.log --output A1.execution.json

```

- `bind-execution` 从合同 `test_policy` 派生测试命令与 `require_test_result`/
  `require_artifact_integrity`：调用方只能加严，不能换掉合同命令或关闭合同要求的层；
  合同显式 `required=false` 时对应层为 NOT APPLICABLE（`test_execution.status=
  not_applicable`），普通任务不被强制跑无意义测试。影子与候选只在**当前合同**
  有 captured 策略、`required=false`、binding `requirement_source=contract` 且
  `contract_sha256` 等于当前合同、策略/报告/产物与当前合同一致时接受 N/A；
  amend 后旧 binding 或伪造 contract-source binding 不放行。
- 合同 `report`/`artifacts` 路径必须位于 workspace 内，且必须出现在本次声明的测试
  产物中；缺失或越界判 `failed`。产物 mtime 必须晚于本轮测试命令启动。
- 子命令在写文件前调用 `native_bridge` 的正式校验；失败不产生输出文件。
- `review` / `review-context` / `capability` 子命令已随 G4 退役；`bind-execution` 与
  `execution` 只负责执行绑定与执行信封。
- 证据 SHA-256 由脚本读取文件计算，不接受调用方传入。
- `--evidence` 形如 `E1=/abs/path`，可重复。

## 5. 回填与 idempotency

```bash
python3 -B scripts/copilot_auto.py ingest --state T/state.json --result-file A1.execution.json
```

- 执行回填：校验通过后把工作项置为信封 `status`、写 `result` 与 `evidence_ids`，并把规范化
  信封加入 `native_results`。
- 复核不再经 `ingest`：独立复核使用 `sol-review`；`--review-file` 参数已移除。
- 同一 `result_sha256` 重复回填幂等跳过；不同结果覆盖已终态工作项被拒绝。
- 被拒绝时退出码 12，stderr 含错误码，状态不被部分写入。

## 6. 错误码速查

| 类别 | 错误码 |
|---|---|
| schema | `schema_invalid`、`schema_retired`、`hash_invalid` |
| 任务/合同 | `task_mismatch`、`contract_mismatch`、`credential_missing` |
| 计划/派发 | `plan_missing`、`plan_unknown`、`plan_mismatch`、`plan_drift`、`dispatch_missing`、`dispatch_empty`、`dispatch_duplicate`、`dispatch_mismatch`、`dispatch_id_drift`、`idempotency_mismatch`、`iteration_mismatch`、`work_item_unknown`、`work_item_terminal` |
| 执行 | `backend_not_native`、`assignee_unsupported`、`invalid_status`、`executor_identity_missing`、`executor_profile_invalid` |
| 证据 | `evidence_missing`、`evidence_invalid`、`evidence_duplicate`、`evidence_unreadable`、`evidence_sha_mismatch`、`evidence_foreign`、`evidence_conflict` |
| 复核（历史） | `native_review_retired`（复核信封已退役） |

v6/v7 的复核与 G4 错误码（`reviewer_*`、`g4_*`、`native_review_attempt_*`、
`incremental_*` 等）只出现在历史记录与日志中，不再由当前代码产生。错误码只用于定位与
修复，不提供绕过开关；修正记录后重新生成信封。

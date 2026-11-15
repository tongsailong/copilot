# agent-bridge：原生执行与复核信封（v2）

本文件说明 v6 的 Native Bridge：**ZCode 原生 Agent 执行 / Reviewer 复核 → Copilot 校验回填**。
v1 信封（SOL 通道时代）已退役，出现即 `schema_retired`。

## 1. 角色与边界

| 角色 | 职责 | 约束 |
|---|---|---|
| Executor | 执行单个工作项，产出结果与原始证据 | 必须是原生子代理（`kind=subagent`），带 Agent/Child Session 身份；不得使用 `reviewer` Profile |
| Reviewer | 独立只读复核，给出 verdict/findings | 必须是 `kind=subagent`、`profile_id=reviewer`、工具仅 `Read/Grep/Glob`；不得修改文件、不得与执行方共用 Agent/Child Session |

Copilot 不派发 Agent、不调用模型、不写别人的会话。它只校验宿主已产生的记录并回填状态。
`native_agent.py` 从真实 `metadata.json`、宿主 SQLite（`tool_usage`/`part`/`session_entry`）
与证据文件生成信封，避免主代理手抄身份。

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
               "profile_id": "executor", "model_reported": "..."},
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
- 执行方不得使用 `reviewer` Profile（`reviewer_as_executor`）。

### 2.3 review（`copilot-native-review/v2`）

```json
{
  "schema": "copilot-native-review/v2",
  "task_id": "...", "contract_sha256": "...",
  "plan_id": "...", "plan_sha256": "...",
  "dispatch_id": "d-...", "work_item_id": "A1",
  "execution_result_sha256": "<被执行结果的摘要>",
  "target_sha256": "<attempt 冻结的 final_evidence_digest>",
  "review_attempt_id": "RA<24 hex>",
  "review_attempt_created_at": "<attempt 创建时间>",
  "review_scope": "full|incremental",
  "based_on_review_sha256": "<增量复核的基线；full 时为空>",
  "evidence_manifest": ["E7", "E8"],
  "reviewer": {"kind": "subagent", "agent_id": "agent-review",
               "child_session_id": "child-review", "profile_id": "reviewer",
               "model_reported": "..."},
  "host": {"metadata_path": "/abs/metadata.json",
           "output_file": "/abs/review-output.json",
           "rollout_path": "",
           "host_db": "/home/<user>/.zcode/cli/db/db.sqlite",
           "tool_calls": [{"name": "Read", "count": 2}, {"name": "Grep", "count": 1}],
           "agent_id": "agent-review", "child_session_id": "child-review",
           "session_created_at": "<metadata.createdAt>",
           "session_completed_at": "<metadata.completedAt>",
           "prompt_sha256": "<metadata.prompt 的摘要>",
           "workspace_root": "<metadata.workspaceRoot>"},
  "verdict": "pass|partial|blocked",
  "findings": [{"id": "F1", "severity": "blocking|major|minor|info", "file": "...",
                "line": 1, "summary": "...", "evidence": "...",
                "root_cause": "...", "category": "...", "status": "open",
                "related": ["F2"], "reproduce": "..."}],
  "files_inspected": ["/abs/path"],
  "commands_run": [],
  "changed_files": [],
  "unverified": [],
  "residual_risks": [],
  "g4_eligible": true,
  "result_sha256": "<信封自身摘要>"
}
```

`g4_source` 与 `candidate_g4` 由 `copilot_auto._ingest_review` 写入状态记录，不是
Reviewer 信封字段。

校验要点：

- 必须绑定一条**已回填**的执行结果（`execution_result_sha256`）；先 ingest 执行信封。
- G4（`g4_eligible=true`）必须带 `review_attempt_id` / `review_attempt_created_at`，且
  attempt 必须是 `review-context --create-attempt` 在本任务派发前创建的
  （`review_attempt_missing`、`review_attempt_invalid`）。attempt 由宿主冻结目标摘要、
  文件证据内容与 git 工作区代码范围；`review --attempt` 拒绝旧 attempt、已消费 attempt、
  目标过期或代码范围已漂移的 attempt。
- `work_item_id` / `dispatch_id` / 执行摘要必须与执行结果一致（`review_binding_mismatch`）。
- Reviewer 必须是独立子代理（`reviewer_invalid`），Profile 必须是 `reviewer`
  （`reviewer_profile_invalid`），且与执行方不同 Agent / Child Session
  （`executor_reviewer_same`、`review_contaminated`）。
- Reviewer **没有 Bash 权限**：`commands_run` 必须为空，出现任何 Bash 命令直接
  `review_command_unexpected`。`files_inspected` 由 `native_agent.py` 合并自报与宿主
  `tool_usage`/`part` 的 Read/Grep/Glob 路径；`declared_files_unverified` 非空时写入
  复核记录供审计。
- finding 元数据：`id`（`F1`–`F9999`，本轮唯一）与 `status`（`open|resolved|regressed`，
  缺省 `open`）可选；`root_cause`/`category`/`related`/`reproduce` 可选。同一轮重复 ID
  或完全相同的 `(severity,file,line,summary)` 会被拒绝（`review_finding_duplicate`），
  用于强制按根因合并而不是重复计数。
- 宿主输出解析（`review-json/v2`，`scripts/review_output.py`）：整段文本必须是一个
  JSON 对象，或**恰好一个** fenced 代码块且围栏外只允许空白；多个围栏/多个对象、
  围栏外叙述、重复键、顶层非对象一律拒收（`review_output_ambiguous` /
  `review_output_outside_text` / `review_output_unparseable` /
  `review_output_not_object`）。不再接受“围栏外带说明文字”的旧格式；工作区
  Reviewer Profile 要求只输出纯 JSON，解析器版本记录在 `PARSER_VERSION`。
- `files_inspected` 若填写，路径必须存在且是普通文件（`review_inspection_invalid`）。
- `g4_eligible=true` 时必须：`verdict=pass`、无 blocking、`unverified` 为空、
  `changed_files` 为空，并附完整宿主记录（`g4_requires_pass`、`g4_with_unverified`、
  `g4_with_blocking`、`g4_host_record_missing`）。
- 非 G4 复核若 `verdict=pass` 但有 blocking/unverified，同样拒绝
  （`pass_with_blocking_finding` / `pass_with_unverified`）。
- 增量复核（`review_scope=incremental`）必须带 `based_on_review_sha256`
  （本任务已回填的 PASS、`g4_eligible=true`、含覆盖快照的复核）与
  `evidence_manifest`；基线之后新增/变更的证据必须全部列入清单，条件状态与
  其他工作项不得变化，否则拒绝并要求完整复核（`incremental_*` 原因码）。

## 3. 宿主记录与新链 G4 资格

阶段九起正式 `g4_eligible` **不再由 `native_agent.py review` 计算**，也不接受 Reviewer
或信封自述。`native_agent.py review` 生成的 `g4_eligible` 只是信封结构预检；正式资格由
`copilot_auto._ingest_review` 的新链决定：

```
execution binding → 真实 Executor 会话 → 测试执行/证据
→ review attempt → 真实 Reviewer 会话 → 原始输出持久化
→ g4_attest.attest（独立宿主核验）→ test_execution.build
→ g4_shadow（候选判定）→ 正式 g4_eligible/g4_source → 一次性消费 → 收口
```

`g4_attest.attest` 只读宿主 `metadata.json` 与 SQLite，独立核对：

- Reviewer Profile 工具集精确 `Read/Grep/Glob`、`disallowedTools` 含写工具与 Bash、
  `permissionMode=default`、无 skills/MCP、工作区级 Profile 内容匹配；
- Agent/Child Session/父会话/trace、会话开始/结束时间晚于 attempt、prompt 携带
  attempt id、`workspaceRoot` 等于 attempt 代码根；
- `tool_usage`/`part` 的每次调用均只读、计数与 `totalToolUseCount` 一致，无 Bash；
- 原始输出（`outputFile` 优先，缺失时回退 `message/part`，两者都在时 SHA 必须一致）
  通过 `review-json/v2` 严格解析，verdict/findings/unverified/changed_files 与信封一致；
- 证据文件 SHA、inspect 记录、敏感路径/越界读取、attempt 的 `code_snapshot` 代码范围
  快照（含 submodule/嵌套仓库）；
- 高风险任务必须有 execution binding、真实 Executor 会话与测试执行层级。

`test_execution.build` 在合同 `test_policy` 约束下判定：
`command_execution_verified`、`expected_test_verified`、`test_result_verified`（JUnit）、
`artifact_integrity_verified` 四层；只有当前合同显式 `required=false` 且 binding 来自
合同、策略/报告/产物一致时才接受 `not_applicable`。

`g4_shadow` 汇总上述结果：`status=attested` 且 `new_g4_eligible=true` 才是候选可用。
`copilot_auto._ingest_review` 仅在 `risk=high` 且 shadow attested/eligible 时写入正式
`g4_eligible=true`、`g4_source="candidate"`，并消费 attempt；否则正式为 false，旧信封
自报 `g4_eligible=true` 不生效。`governance._review_trust_problems` 还会拒绝没有
`g4_source="candidate"` 或 `candidate_g4.eligible!=true` 的旧记录
（`native_review_legacy_g4_source` / `native_review_candidate_not_eligible`）。

`g4_attest.attest` 与 `verify_stored` 把原始输出持久化到任务目录 `review-outputs/`，
记录 `raw_output{path,sha256,bytes,source,parser}`；收口/复核时可只凭持久化输出重算
SHA、解析与 findings 摘要，不依赖宿主 DB 或 metadata。旧记录没有 findings 摘要时只做
raw 复核并记录 limitation。

**局限**：这些检查保证“不靠主代理记忆或手抄身份”，不是宿主签名；能写本机文件的人仍可伪造。
G4 是本地可得的最强原生复核，不等于密码学证明。

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

# 派发前创建 attempt：冻结任务/计划/执行/目标摘要/证据/代码范围
python3 -B scripts/native_agent.py review-context --state T/state.json \
  --dispatch T/state.dispatch.json --execution A1.execution.json \
  --work-item A1 --create-attempt --workspace "$(git rev-parse --show-toplevel)" \
  [--base <任务开始 commit>] --output A1.context.json

# 复核信封：必须绑定同一 attempt；高风险缺 attempt 直接拒绝
python3 -B scripts/native_agent.py review --state T/state.json \
  --dispatch T/state.dispatch.json --execution A1.execution.json \
  --work-item A1 --attempt RA... --metadata <agent-review>/metadata.json \
  --input reviewer.raw.json --output A1.review.json

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
- `review-context --create-attempt` 只允许高风险 G4；存在未核销完成条件、执行证据漂移或
  工作区不是 git 仓库时拒绝创建，避免把即将过期/不可证明的快照交给 Reviewer。它把
  attempt id 与代码范围写入输出；派发时必须把该 id 放进 Reviewer prompt。
- `review` 只从宿主 DB 读取 Reviewer 的模型、工具调用与最终输出；不再依赖 rollout，
  也不再接受 Bash 命令。会话时间、prompt 与工作区绑定由 `g4_attest` 在 ingest 时
  独立复核；旧会话/旧工作区无法冒充新 attempt。
- `review-context` 输出 `execution_evidence_unbound`（执行证据摘要与 live 证据
  不一致/已归档/未登记）、`evidence_manifest` 与建议 scope；ingest 会重新校验。
- 证据 SHA-256 由脚本读取文件计算，不接受调用方传入。
- `--evidence` 形如 `E1=/abs/path`，可重复。

## 5. 回填与 idempotency

```bash
python3 -B scripts/copilot_auto.py ingest --state T/state.json --result-file A1.execution.json
python3 -B scripts/copilot_auto.py ingest --state T/state.json --review-file A1.review.json
```

- 执行回填：校验通过后把工作项置为信封 `status`、写 `result` 与 `evidence_ids`，并把规范化
  信封加入 `native_results`。
- 复核回填：加入 `native_reviews`，G4 资格与 attempt 重新核对通过后写入；同一份写入把
  `review_attempts` 中对应 attempt 标记为已消费（记录 `consumed_by_result_sha256` 与
  Reviewer 会话），被拒时不消费。
- 同一 `result_sha256` 重复回填幂等跳过；不同结果覆盖已终态工作项被拒绝。
- 被拒绝时退出码 12，stderr 含错误码，状态不被部分写入。

## 6. 错误码速查

| 类别 | 错误码 |
|---|---|
| schema | `schema_invalid`、`schema_retired`、`hash_invalid` |
| 任务/合同 | `task_mismatch`、`contract_mismatch`、`credential_missing` |
| 计划/派发 | `plan_missing`、`plan_unknown`、`plan_mismatch`、`plan_drift`、`dispatch_missing`、`dispatch_empty`、`dispatch_duplicate`、`dispatch_mismatch`、`dispatch_id_drift`、`idempotency_mismatch`、`iteration_mismatch`、`work_item_unknown`、`work_item_terminal` |
| 执行 | `backend_not_native`、`assignee_unsupported`、`invalid_status`、`executor_identity_missing`、`reviewer_as_executor` |
| 证据 | `evidence_missing`、`evidence_invalid`、`evidence_duplicate`、`evidence_unreadable`、`evidence_sha_mismatch`、`evidence_foreign`、`evidence_conflict` |
| 复核 | `reviewer_invalid`、`reviewer_profile_invalid`、`review_binding_mismatch`、`executor_reviewer_same`、`review_contaminated`、`invalid_verdict`、`review_attempt_missing`、`review_attempt_invalid`、`review_finding_invalid`、`review_finding_duplicate`、`review_inspection_invalid`、`review_command_unexpected`、`review_output_ambiguous`、`review_output_unparsable`、`review_scope_invalid`、`incremental_base_missing`、`incremental_base_ineligible`、`incremental_manifest_missing`、`incremental_evidence_uncovered`、`incremental_criteria_changed`、`incremental_scope_too_narrow` |
| G4 | `g4_host_record_missing`、`g4_host_record_invalid`、`g4_tool_calls_missing`、`g4_tool_not_readonly`、`g4_not_eligible`、`g4_requires_pass`、`g4_with_unverified`、`g4_with_blocking`、`pass_with_blocking_finding`、`pass_with_unverified`、`host_profile_changed`、`host_session_time_changed`、`host_prompt_changed`、`review_attempt_prompt_unbound`、`review_attempt_session_precedes_attempt`、`native_review_attempt_*`、`native_review_attempt_workspace_drift`、`native_review_code_scope_*`、`incremental_code_changed` 等 |

`g4_host_record_*`、`g4_tool_calls_*`、`host_profile_changed` 等旧宿主校验码只出现在
v6.4 及更早的历史记录；v6.5 正式路径使用 `g4_attest` 的 reason/difference 与
`native_review_legacy_g4_source`/`native_review_candidate_not_eligible`。错误码只用于
定位与修复，不提供绕过开关；修正记录后重新生成信封。

# Agent Bridge：SOL 安排 → ZCode 原生执行/复核 → Copilot 回填

本文件是阶段 0–3.6 的 Agent Bridge 协议说明。它只覆盖**协议、校验、状态字段和人工排程入口**，
不包含阶段 4 的 Reviewer 自动派发或 SOL 自动闭环。

## 1. 边界与角色

- **SOL（`gpt-6.1-sol`, reasoning high）**：唯一工程决策大脑。工作项必须来自已验证的 SOL
  回复；`task_id` / `round_id` / `contract_sha256` / `plan_call_id` / `plan_reply_sha256`
  都从该轮凭证取得。
- **ZCode 主代理**：读取 dispatch，使用宿主原生工具或原生 `Agent` 派发实际执行；回填执行结果；
  需要时再派发独立 Reviewer。主代理不得改写 SOL 的目标、约束、预期证据、执行方或依赖。
- **执行方**：正式 native 执行必须使用原生子代理（`kind=subagent`，通常复用
  `general-purpose`），以便绑定 `agent_id` / `child_session_id` / 实际模型。主代理仍可
  读 dispatch、派发子代理和回填结果，但不能用自己的身份冒充正式执行方。不得使用
  `reviewer` Profile 执行工作项。
- **原生 Reviewer**：独立子会话，Profile 固定为 `reviewer`，只读工具
  `Read` / `Grep` / `Glob` + 受限只读 `Bash`；不写文件、不提交、不发布。
- **Copilot 状态层**：保存任务合同、轮次、工作项、证据、报告、四门与 PASS/CLOSED 锁。
  Bridge 只回填状态；它不调度模型、不发起真实调用、不改变 G1–G4。

**可信边界**：Bridge 的 SHA-256 是本地完整性和绑定校验，不是密码学签名，也不是宿主可信身份
证明。`model_reported`、`Agent ID`、`Child Session ID` 不能作为模型身份的独立凭证；即使
Agent 自报模型名，也不能证明上游实际模型。任何普通原生 Reviewer 的 PASS 都不等于 G4。

## 2. 启用方式

原生后端是**显式 opt-in**；旧任务和旧状态默认仍是 `legacy`：

```bash
python3 ~/.zcode/skills/copilot/scripts/copilot_dispatch.py start \
  --state /tmp/copilot/dispatch.json --goal-file /tmp/copilot/goal.md \
  --contract-file /tmp/copilot/contract.md --task-id <task> --mode PLAN \
  --execution-backend native
```

- `start` 默认 `--execution-backend legacy`，不需要原生流程时行为不变。
- 旧状态缺少 `execution_backend` 时升级为 `legacy`；缺少的
  `native_dispatch` / `review_channel` / `review_round` / `review_consumed` /
  `native_results` / `native_reviews` 按空值补齐。重复加载/保存是幂等的。
- native 状态拒绝 `ingest --result-file`；legacy 状态拒绝 `ingest --native-result`
  （`drive --native-result` 已停用，只保留迁移提示）。两个渠道不能混用，避免把人工
  manifest 和原生报告当成同一份执行结果。
- native 默认预算写入状态：`budget.max_requests=6`、`budget.max_total_tokens=500000`、
  `budget.projection=true`；可用 `--max-requests / --max-total-tokens /
  --no-budget-projection` 覆盖。旧 native 状态未配置时按同一默认值执行。

## 3. 状态字段

| 字段 | 含义 |
|---|---|
| `execution_backend` | `legacy`（默认）或 `native` |
| `native_dispatch` | 当前已验证轮次的 dispatch 状态，含 `items`、批次 `dispatch_id` 和计划绑定 |
| `native_results` | 已入账执行结果索引（含 `result_sha256`、证据 id、执行方元数据、报告摘要） |
| `native_reviews` | 已入账复核索引（绑定 `execution_result_sha256`，含 verdict/findings/元数据） |
| `review_channel` | `null` / `native` / `gpt_review`；同一轮不得让两个渠道同时拥有复核权威 |
| `review_round` | 原生复核所属轮次 |
| `review_consumed` | 已被原生复核消费的执行结果 SHA 列表；每个执行结果只允许一份原生复核 |
| `budget.max_requests` / `budget.max_total_tokens` / `budget.projection` | 请求数硬上限、累计 Token 阈值与保守预估开关（native 默认 6 / 500000 / true） |
| `budget.usage` | 从审计重建的用量快照（输入/输出/总量/请求数/未知 usage 调用数）；权威来源仍是审计 |

执行报告和复核报告分别写入 `reports`，通过 `kind=execution` / `kind=review` 区分；
复核报告还带 `review_of`、`verdict`、`g4_eligible=false`。执行方自评不会被合并进复核报告。

## 4. 信封

所有信封是 JSON 对象；`result_sha256` 是对**排除自身**的规范化 JSON（键排序、紧凑分隔符）做
SHA-256。校验时重新计算，任何字段变化都会导致 `result_sha_mismatch`。

### 4.1 Dispatch：`copilot-native-dispatch/v1`

```json
{
  "schema": "copilot-native-dispatch/v1",
  "dispatch_id": "b-...",
  "task_id": "task-1",
  "round_id": "task-1-r1",
  "contract_sha256": "<64 hex>",
  "plan_call_id": "call-...",
  "plan_reply_sha256": "<64 hex>",
  "created_at": "2026-11-02T10:00:00+08:00",
  "actions": [
    {
      "work_item_id": "T1",
      "dispatch_id": "d-...",
      "idempotency_key": "ik-...",
      "title": "SOL 原始标题",
      "assignee": "zcode",
      "goal": "SOL 原始目标",
      "constraints": "SOL 原始约束",
      "expected_evidence": "SOL 原始预期证据",
      "depends_on": []
    }
  ]
}
```

- `actions` 必须来自 `ready_actions(state)`，且每项带 `source_round_id`；
  该轮必须在已验证轮次中。
- `assignee` 只允许 `zcode` / `deepseek`；`reasonix` 不能进入原生后端。
- `dispatch_id` / `idempotency_key` 由 `task_id + round_id + contract_sha256 +
  plan_call_id + work_item_id` 稳定派生；同一轮重复生成复用同一 ID，不会产生第二份派发。
- 校验会逐字段比对状态中 SOL 工作项的 `title` / `assignee` / `goal` / `constraints` /
  `expected_evidence` / `depends_on`；任何改写都返回 `dispatch_action_mismatch`。
- dispatch 信封本身不能用 `--native-result` 回填。

### 4.2 Execution：`copilot-native-result/v1`

```json
{
  "schema": "copilot-native-result/v1",
  "task_id": "task-1",
  "round_id": "task-1-r1",
  "contract_sha256": "<64 hex>",
  "plan_call_id": "call-...",
  "plan_reply_sha256": "<64 hex>",
  "dispatch_id": "d-...",
  "idempotency_key": "ik-...",
  "work_item_id": "T1",
  "status": "done | failed | blocked | dropped",
  "summary": "一句话结果",
  "result": "实际输出/交付说明",
  "evidence": [
    {
      "id": "E1",
      "file": "/absolute/path/raw.log",
      "sha256": "<64 hex>",
      "kind": "test-output",
      "note": "原始命令输出"
    }
  ],
  "executor": {
    "kind": "subagent",
    "agent_id": "agent-...",
    "child_session_id": "sess_subagent_agent-...",
    "profile_id": "general-purpose",
    "model_reported": "provider/model"
  },
  "timing": {"started_at": "...", "finished_at": "...", "duration_ms": 1000},
  "usage": {"total_tokens": 12, "tool_use_count": 1},
  "result_sha256": "<64 hex>"
}
```

- 执行结果必须同时绑定 `plan_call_id` / `plan_reply_sha256` / `idempotency_key`，
  并分别与当前 dispatch 和最近已验证 SOL 轮次一致；缺少任一项返回
  `schema_invalid`，不一致返回 `plan_call_mismatch` / `plan_reply_mismatch` /
  `idempotency_mismatch`。
- `executor` 必须是 `kind=subagent`，并提供 `agent_id` / `child_session_id` /
  `profile_id`；主代理没有可核对的 Agent/Child Session 身份，不能冒充正式执行方。
  身份与模型由 `native_agent.py` 从宿主 `metadata.json` 与 rollout 读取，不接受
  调用方手填。
- 至少一条证据；文件必须是绝对路径、普通文件、实际存在，且 SHA-256 与声明一致。
- 同一任务内证据 id 不得重复；已登记且摘要不同会拒绝静默替换。
- `profile_id=reviewer` 会被拒绝。
- 执行结果先校验、再登记证据、回填工作项、写执行报告、更新 dispatch item，最后原子保存；
  任一校验失败不会产生半更新。
- 同一 `result_sha256` 重复回填是幂等的（返回 `duplicate=true`，不重复写报告）。
  已终态工作项不接受不同结果 SHA。

### 4.3 Review：`copilot-native-review/v1`

```json
{
  "schema": "copilot-native-review/v1",
  "task_id": "task-1",
  "round_id": "task-1-r1",
  "contract_sha256": "<64 hex>",
  "dispatch_id": "d-...",
  "work_item_id": "T1",
  "execution_result_sha256": "<被复核执行结果的 result_sha256>",
  "reviewer": {
    "kind": "subagent",
    "agent_id": "agent-...",
    "child_session_id": "child-...",
    "profile_id": "reviewer",
    "model_reported": "provider/model"
  },
  "verdict": "pass | partial | blocked",
  "findings": [
    {
      "severity": "blocking | major | minor | info",
      "summary": "具体缺陷/观察",
      "file": "/absolute/path",
      "line": 1,
      "evidence": "具体 diff/log/hash 证据"
    }
  ],
  "files_inspected": ["/absolute/path"],
  "commands_run": ["git diff -- path"],
  "changed_files": [],
  "unverified": [],
  "residual_risks": [],
  "g4_eligible": false,
  "result_sha256": "<64 hex>"
}
```

- 复核必须先有已入账的执行结果；`execution_result_sha256` 必须等于该执行结果的信封摘要。
- Reviewer 必须是 `kind=subagent`、`profile_id=reviewer`，且 Agent ID / Child Session ID
  不能与执行方任一相同；`model_reported` 必须来自实际 rollout 记录，不能由主代理
  手填或猜测（`native_agent.py` 负责提取）。
- `changed_files` 必须为空；非空直接判为工作区污染。
- 至少检查一个实际存在的普通文件；`/dev`、`/proc`、`/sys` 路径拒绝。
- `commands_run` 只允许只读前缀：`git diff/status/log/show/grep/rev-parse/ls-files`、
  `rg`、`grep`、`cat`、`head`、`tail`、`wc`、`ls`、`find`
  （拒绝 `-delete` / `-exec` / `-fprint` / `-fprintf` / `-fls`）、`stat`、
  `sha256sum`、`diff`、`cmp`、`sort`（拒绝 `-o` / `--output` /
  `--compress-program` / `-T`）。
- 顶层 `;` / `|` 允许用于组合只读命令，但会按引号感知拆分后**逐段**套用同一
  白名单与破坏性参数校验；`&&`、`||`、重定向、反引号、`$()`、换行和未闭合
  引号一律失败关闭。拆分不改变 Reviewer 原始命令文本，`result_sha256` 仍绑定
  原始输出。这是第二道失败关闭，不是操作系统沙箱。
- `blocking` / `major` finding 必须给 `file` 和 `evidence`。
- `pass` 不能带 blocking finding 或任何 `unverified` 项；不能猜 PASS。
- `g4_eligible` 必须显式为 `false`；普通原生复核不改变 G4。
- 同一执行结果只接受一份复核；第二份不同复核（即使来自另一个 Reviewer）会拒绝，
  不能覆盖第一份结论。完全相同的复核信封重复回填是幂等的。

## 5. 校验失败码

常见稳定错误码：

- 绑定：`task_mismatch`、`round_mismatch`、`contract_mismatch`、`plan_call_mismatch`、
  `plan_reply_mismatch`、`idempotency_mismatch`、`dispatch_mismatch`、
  `work_item_unknown`、`review_binding_mismatch`。
- 工作项：`dispatch_empty`、`dispatch_duplicate`、`dispatch_action_mismatch`、
  `work_item_unverified`、`work_item_terminal`、`assignee_unsupported`。
- 执行：`evidence_missing`、`evidence_invalid`、`evidence_duplicate`、
  `evidence_sha_mismatch`、`evidence_conflict`、`evidence_foreign`、
  `executor_identity_missing`、`reviewer_as_executor`。
- 复核：`review_before_execution`、`executor_reviewer_same`、`review_contaminated`、
  `review_inspection_missing`、`review_inspection_invalid`、`reviewer_invalid`、
  `reviewer_profile_invalid`、`review_command_not_readonly`、`review_finding_invalid`、
  `pass_with_blocking_finding`、`pass_with_unverified`、`g4_eligible_forbidden`。
- 完整性：`schema_invalid`、`hash_invalid`、`result_sha_mismatch`。

CLI 会把 `BridgeError.code` 包进 `AutoError` 并返回退出码 2；失败时不写状态。

## 6. 操作顺序

1. 用 `copilot_dispatch.py start --execution-backend native` 建立 native 任务，完成合同与
   SOL 轮次入账。
2. `copilot_auto.py drive` 到 `EXECUTE` 时返回 `native_dispatch`。主代理按 `actions` 执行，
   保留 SOL 原始字段，不自行改写。执行必须由原生子代理完成；把宿主返回的 Agent
   `metadata.json` 原样保存在任务目录。
3. 用 `native_agent.py execution` 从实际运行记录生成 `copilot-native-result/v1`
   信封；证据 SHA-256、执行方身份、实际模型、计划/幂等绑定和 `result_sha256`
   都由脚本计算，不接受手填：
   ```bash
   python3 ~/.zcode/skills/copilot/scripts/native_agent.py execution \
     --state /tmp/copilot/dispatch.json --dispatch /tmp/copilot/native-dispatch.json \
     --work-item T1 --metadata /path/to/agent-.../metadata.json \
     --status done --summary "一句话结果" --result-file /tmp/copilot/T1.result.md \
     --evidence E1=/tmp/copilot/raw.log --output /tmp/copilot/T1.execution.json

   python3 ~/.zcode/skills/copilot/scripts/copilot_auto.py ingest \
     --state /tmp/copilot/dispatch.json --native-result /tmp/copilot/T1.execution.json
   ```
   执行回填是纯本地操作：0 请求、不递增轮次、不创建 pending；阶段进入
   `REVIEW_PENDING`。此后必须显式运行 `drive` 才会请求 SOL。
4. 需要复核时，派发 `reviewer` 子代理并保存它的 `metadata.json`。Reviewer 只返回
   verdict/findings/files_inspected/commands_run 等判断内容；用 helper 填入 Reviewer
   身份、实际模型、执行结果绑定和复核摘要：
   ```bash
   python3 ~/.zcode/skills/copilot/scripts/native_agent.py review \
     --state /tmp/copilot/dispatch.json --dispatch /tmp/copilot/native-dispatch.json \
     --execution /tmp/copilot/T1.execution.json --work-item T1 \
     --metadata /path/to/reviewer-agent-.../metadata.json \
     --input /tmp/copilot/T1.reviewer.raw.json --output /tmp/copilot/T1.review.json

   python3 ~/.zcode/skills/copilot/scripts/copilot_auto.py ingest \
     --state /tmp/copilot/dispatch.json --native-result /tmp/copilot/T1.review.json
   ```
   复核 ingest 同样 0 请求。只有执行与复核都回填、且绑定校验通过后，显式 `drive`
   才能让 SOL 做最终判断；复核有 blocking finding 时进入 REJUDGE，CLOSED 前
   `closure --close` 会再次检查复核完成度。
5. `ingest --dry-run --native-result ...` 只做只读校验，不登记证据、不写报告、不调用 GPT。
6. 执行失败/阻塞照实回填 `status=failed|blocked`，报告为执行报告；不会自动切换到 GPT
   REVIEW 或另一个复核渠道。复核 `partial|blocked` 同样只作为复核报告留痕，不自动重试。
7. 下一轮由 SOL 根据真实回填与新证据重新安排；不要用同一个执行结果重复复核，也不要把
   Reviewer 的 PASS 当成任务 PASS 或 G4。
8. 请求/Token 预算：native 默认每任务 6 次请求（按审计 `call_requested` 在出站前判定，
   硬上限）与 500k 累计 Token（按 `usage` 核算，响应后才知道，不是严格上界；另按上次
   调用总量做保守预估拦截）。预算从审计重建，进程重启不归零；耗尽时 `drive` 返回
   `budget_exhausted`，`call_gpt.py` 返回退出码 12。

## 7. Reviewer Profile

- 用户级路径：`~/.zcode/agents/reviewer.md`。
- `model: inherit`：Reviewer 的 Provider/模型/推理级别继承自主代理当前会话，不做固定
  模型钉死，因此主代理更换模型后 Reviewer 自动跟随；这不是运行时身份凭证，
  `model_reported` 仍以实际 rollout 记录为准。
- `tools: [Read, Grep, Glob, Bash]`，`disallowedTools: [Write, Edit]`；
  Bash 仅用于只读检查，真实写保护仍依赖宿主权限与 bridge 白名单。
- `injectAgentsMd: true`，`maxTurns: 30`，`permissionMode: default`。
- Profile 正文要求 Reviewer 独立检查真实代码/diff/日志/原始证据，重要 finding 给
  文件位置与具体证据，无法核实的写 `unverified`，不得猜 PASS，不得声称 G4。
- 测试只做静态解析与文件位置断言；不发起真实 Agent 调用，因此不证明模型实际可用性、
  真实身份或复核质量。

## 8. G4 与阶段 4 边界

**G4 没有因为本 Bridge 而放宽。** 当前 `closure-check` 的 G4 仍只接受宿主可信绑定的原生
独立复核记录；Bridge 产出的普通 Reviewer 记录、`g4_eligible=false`、GPT REVIEW 台账、
Agent 自报身份都不满足 G4。高风险任务仍返回 `native_review_missing` 并停在 BLOCKED。

本阶段**未实现**：

- Reviewer 自动派发、自动读取 dispatch 并生成复核信封；
- SOL 自动闭环、自动重判、自动收口；
- 把 Bridge 记录升级为 G4 可信记录；
- 任何 MCP / daemon / Workflow / LangGraph / 第二套 Agent 调度框架。

因此阶段 0–3.6 的交付是：协议、Profile、emit/ingest、从实际运行记录生成信封、
状态兼容和测试；阶段 4 仍需单独授权与实现。

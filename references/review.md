# review：v6 复核、正式 REVIEW 与 G4

本文说明 v6 中三类容易混淆的“复核”，以及各自的触发条件、证据要求与失败处理。

**一句话区分：**

- **普通自检**：ZCode 自己做，不登记为复核，不构成任何门禁依据。
- **正式 REVIEW**：用户明确要求时才做，是一个治理记录，**不是普通开发的强制前置门**。
- **G4 复核**：高风险任务收口必需的原生独立只读复核，fail-closed，任何模型判断都不能替代。

## 1. 正式 REVIEW（opt-in）

### 触发

只有在**用户明确要求**正式 REVIEW（或明确要求独立审核/第二意见）时才进入。普通开发、
跨文件改动、任务复杂、存在审核模式都不构成触发条件。

### 记录授权

```bash
copilot_dispatch.py authorize-review --state <state> --authorized-by user \
  --reason "用户要求交付前正式复核" --scope "当前任务合同"
```

- `--authorized-by` 必须显式传入且只能是 `user`（没有默认值）；它是调用方对“用户在该
  会话中明确要求”的声明，不是可验证凭证，治理层只能拒绝未声明或非 user 的登记。
- 必须写明用户要求与范围；授权记录绑定 `task_id` 与合同指纹，合同修订后需重新授权。
- 授权在同一任务合同内持续有效，不逐轮重新确认；范围或风险实质变化时重新确认。
- 记录授权**不会**自动执行 REVIEW，也不产生任何状态门禁。

### 登记结论

```bash
copilot_dispatch.py formal-review --state <state> --verdict pass \
  --summary "复核结论" --review-ref <产物路径或 SHA> --reviewer native
```

- 没有用户授权记录、或授权绑定的是旧合同时直接拒绝：不得伪造正式 REVIEW，也不得用
  普通自检冒充。注意这是登记层校验，不构成对调用方诚实性的密码学保证。
- 记录包含 `review_id`、verdict、摘要、引用、复核者、授权快照与当时的
  `target_sha256`，写入 `history`，只作审计与追溯。
- 正式 REVIEW 结论**不参与** G1–G4 判定；它不能替代 G4，也不能让普通任务跳过证据要求。

### 独立性

正式 REVIEW 若使用原生 Reviewer，必须满足 `agent-bridge.md` 的独立性约束：不同 Agent、
不同 Child Session、只读工具集、独立取证与独立报告、不继承执行方判断。无法保证独立性时，
如实说明是“主代理自检”或“同一上下文复核”，不得标记为正式 REVIEW。

## 2. 原生独立复核（普通）

用于需要第二意见但不构成 G4 的场景（例如用户要求独立复核，或执行方希望交叉验证）：

- 由 `reviewer` Profile 的只读子代理执行（工具仅 `Read`/`Grep`/`Glob`/`Bash`）。
- 检查真实文件、diff、测试日志与原始证据，给出 verdict 与 findings（含文件位置与证据）。
- 结论可用 `native_agent.py review` 生成信封并回填；`g4_eligible` 由宿主记录决定。
- **普通复核 PASS 不等于 G4**；`g4_eligible=false` 的结论只能作参考。

## 3. G4：高风险收口门禁

### 触发

`start --risk high --backend native` 的任务。`risk` 缺失或非法时 G4 **fail-closed**
（不是默认放行）；`risk=high` 配 `legacy` 后端在 `start` 阶段即被拒绝。

### 通过条件（全部满足）

状态中至少存在一条原生复核记录满足：

| 条件 | 含义 |
|---|---|
| `task_id` / `contract_sha256` 一致 | 复核属于本任务与当前合同 |
| `verdict=pass` | 结论为通过 |
| 无 `blocking` finding | 没有阻断性缺陷 |
| `unverified` 为空 | 没有未验证项 |
| `changed_files` 为空 | 复核方未改动任何文件 |
| `g4_eligible=true` | 宿主记录判定其具备 G4 资格 |
| `target_sha256` == 当前 `final_evidence_digest` | 复核针对当前最终证据，不是过期结论 |
| `reviewer.profile_id == "reviewer"` | 复核方是 Reviewer Profile |
| `host` 存在且 `host_sha256` 与内容一致 | ingest 固化的宿主记录未被改写 |
| `host.agent_id` / `child_session_id` 与 `reviewer` 一致 | 宿主记录的复核方身份与自报一致 |
| `host.tool_calls` 非空 | 宿主记录里有实际只读工具调用 |

任何一项不满足都会记录原因码（`native_review_missing` / `native_review_not_pass` /
`native_review_blocking` / `native_review_unverified` / `native_review_changed_files` /
`native_review_not_g4_eligible` / `native_review_stale` / `native_review_profile_invalid` /
`native_review_foreign` / `native_review_contract_mismatch` / `native_review_host_missing` /
`native_review_host_changed` / `native_review_host_identity_mismatch` /
`native_review_host_no_tool_calls`），G4 失败，任务不能 CLOSED。

### 复核目标与“过期”

`final_evidence_digest` 绑定合同指纹、条件与证据、工作项结果与证据。复核完成后：

- `origin=g4-review` 的工作项自身状态变化**不**改变摘要，避免合法结论因自身回填而失效；
- 其他工作项、条件、证据（含摘要或归档状态）任何变化都会改变摘要，使旧复核 `stale`。
  此时必须重新复核，不得复用旧结论。

### 失败处理

- G4 未过时 `evaluate` 判定 PARTIAL，`derive_work` 派生：
  - 没有完成的 G4 工作项 → `AUTO-G4`（组织一次复核）；
  - 已有完成标记但门禁仍缺 → `AUTO-G4-INGEST`（检查回填、绑定与拒绝原因）。
- 这些工作项由 ZCode 主代理执行，不在计划内，所以 `drive` 不会为它们生成 dispatch；但
  G4 复核信封必须绑定一次原生派发与执行结果，因此高风险任务只能用 `--backend native`
  启动（`start` 会拒绝 `--risk high --backend legacy`，避免留下无法收口的任务）。
- G4 失败不是 BLOCKED：继续派生并复核，直到通过或出现真实外部阻塞。

## 4. 缺陷分级与处置

复核 findings 使用统一分级：

| 级别 | 含义 | 处置 |
|---|---|---|
| `blocking` | 违反目标、约束、安全或证据完整性 | 必须修复后重新复核；G4 不允许存在 |
| `major` | 实质缺陷但可局部修复 | 修复或明确记录取舍后再收口 |
| `minor` | 不影响结论的小问题 | 记录；可随后续工作处理 |
| `note` | 观察项 | 不阻断 |

- 执行方必须基于证据回应每条 blocking/major：修复、或用具体反证说明不成立并保留分歧。
- 不采纳意见时给出反证；没有新证据不重复同一轮复核。
- 不得为了通过而降低断言、删除测试或放宽门禁。

## 5. 与状态机的关系

- 正式 REVIEW 与普通复核都不改变阶段判定；阶段只由 `evaluate` 按合同、工作项、证据与
  四门计算。
- G4 是 G1–G4 中的一项，只在高风险任务生效；普通任务的 G4 直接通过，但这不是“降低标准”，
  而是风险分级。
- `closure --close` 在任一门失败时拒绝并列出原因码；不存在“强制收口”开关。

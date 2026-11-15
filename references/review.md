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

### 修订与 supersede

正式 REVIEW 的“结论”和“报告引用”分开管理：

```bash
# 非实质修订：证据编号、引用链接、文字口径；目标摘要必须未变
copilot_dispatch.py formal-review --state <state> --amend FR<id> \
  --review-ref <新引用> --reason "仅更正证据编号，结论未变"

# 实质变化：证据/目标摘要变化后重新验证；旧记录原样保留
copilot_dispatch.py formal-review --state <state> --verdict pass \
  --summary "新证据下复核通过" --supersedes FR<id> --reason "证据实质变化"
```

- `--amend` 不生成新的 PASS，不改变原 verdict；原记录、授权快照与 `target_sha256` 不变，
  只在原记录下追加 `amendments[]`。
- 目标摘要变化时 `--amend` 直接拒绝：amendment 不能继承旧 PASS；必须用 `--supersedes`
  登记新结论，旧记录由 `review-metrics` 标为 superseded。
- 即使修订看起来只是“证据编号”，只要它新增/更正/归档了证据条目并改变了
  `final_evidence_digest`，就按实质变化处理，走 supersede；amendment 只用于目标摘要
  未变的引用、链接与文字口径修订。这样同一结论不会再被重复登记为新的 PASS，但可能
  表现为“1 条原始 PASS + 若干 supersede”，而不是所有修订都塞进 amendment。
- `--supersedes` 仍走原有授权与合同绑定校验；不能用 amendment/supersede 修改原始授权快照。
- `review-metrics` 分别输出 `formal_reviews_total`、`formal_review_amendments_total`、
  `superseded_formal_reviews_total` 与 `active_formal_reviews_total`，不把 amendment 计为
  新的 PASS。

### 独立性

正式 REVIEW 若使用原生 Reviewer，必须满足 `agent-bridge.md` 的独立性约束：不同 Agent、
不同 Child Session、只读工具集、独立取证与独立报告、不继承执行方判断。无法保证独立性时，
如实说明是“主代理自检”或“同一上下文复核”，不得标记为正式 REVIEW。

## 2. 原生独立复核（按需）

复核是**按需工具**，不是每个任务的必经流程。默认策略：

| 任务类型 | 默认做法 |
|---|---|
| 普通任务（单文件/小改动、能本地测试） | ZCode 自检：读 diff、跑针对性测试、核对完成条件；不派 Reviewer |
| 复杂但非高风险（跨模块、行为变化、需要第二意见） | ZCode 自检 + 必要时一个只读子代理，聚焦目标符合性、关键改动、测试结果与真实风险 |
| 高风险（安全、权限、关键业务链路、全局机制） | G1–G4：独立上下文、只读约束、宿主记录与最终证据绑定 |
| 用户明确要求正式 REVIEW | 另按第 1 节登记，与风险分级无关 |

不因“改了很多文件”“任务复杂”“有审核模式”机械派 Reviewer，也不要求每个复核都
生成完整 G4 信封。普通复核的价值是发现 ZCode 自检看不到的问题，不是重复一遍自检。

### 复核收敛流程

1. **ZCode 充分自检**：先自己完成针对性测试、diff 检查与目标核对，把能本地发现
   的问题修完；不要把未消化的半成品交给复核。
2. **稳定最终快照**：核销完成条件、登记完整证据、确认 `final_evidence_digest` 不再变化；
   最终 G4 复核必须晚于这些动作。派发前读取 `review-context` 的 `warnings` /
   `scope_reason`，有未闭合条件或证据漂移时先解决，不要让 Reviewer 基于即将过期的摘要工作。
3. **首次全面复核**：一次覆盖当前工作项/任务的全部重要方面，findings 带
   severity、文件位置、具体证据与影响；同时报告 blocking / major / minor / info。
4. **集中修复**：按首次复核的全部 blocking/major 与共同根因一起修，不要只修第一条
   再重跑一轮。
5. **针对性复核**：只验证修复项、新引入的问题、受影响区域与完成条件；能复用且仍
   有效的证据直接复用，不重复同一检查。
6. **收口**：完成条件与门禁满足即结束，不追加“再确认一轮”的形式复核。

目标是一次全面复核 + 一次针对性复核收敛（这是性能目标，不是硬上限）。超过两轮
仍未收敛时，先判断是**首次复核不全面**、**修复不彻底**、**新增缺陷**还是**复核
机制本身**（命令/绑定/证据流程）的问题，再决定补证据、换路线或改机制；不要用
“再来一轮”掩盖非收敛原因。

### 首轮完整性协议

首轮是这次工作项唯一一次全面检查的机会，应把预算花在覆盖上：

1. 从 diff、工作项与任务证据清单**枚举变更面**（改动文件、产物、日志、完成条件）。
2. 读**受影响的代码**：调用方、消费方、配置、编码该行为的测试，而不只看改动行。
3. **按根因归组**：一个根因一条 finding；同一根因的多处表现用 `related` 引用，不拆成
   多条、更不拆成多轮。
4. **检查同类缺陷的其他位置**：一个 handler 缺保护，就在本轮检查兄弟 handler。
5. finding 使用稳定 ID（`F1..Fn`）与可选 `root_cause` / `category` / `related` /
   `reproduce`；后续轮次复用同一 ID，宿主与指标据此统计重复 finding。
6. 把输出分成四类：真实 blocking/major 缺陷、证据缺口（`unverified`）、非阻塞建议
   （`minor`/`info`）、已核实但非缺陷的风险（`residual_risks`）。

finding 必须有可达的缺陷机制、实际影响与可核实证据（文件+行、diff hunk、日志片段或
哈希）；没有代码路径或证据的推测不得升级为 blocking。已确认的第二个缺陷不得因为
第一个已经导致不通过而留到下一轮。

### 后续轮次与停止

- **复用 ID 与状态**：`resolved` 仅表示当前快照已验证修复，`open` 表示仍复现，
  `regressed` 仅在有新证据表明已修复问题再次出现时使用。
- **不重报已关闭问题**：没有新证据的同一观察是噪声，不是 finding。
- **只在输入变化时重跑**：文件内容、证据、合同或测试命令变了才重新执行；仍有效的
  结果直接复用并说明复用了哪些证据。
- **PASS 即停止**：合同与完成条件满足、无 blocking、`unverified` 为空即返回 `pass`；
  不为 `minor`/`info` 或可选改进重开。把 `minor` 记录为 findings、把已核实非缺陷风险记录
  为 `residual_risks`，不要为了让 finding 归零而修改已通过的证据再重开完整复核。
- **PASS 绑定快照**：如果 PASS 之后出现实质代码、证据、合同或风险变化，原 PASS 不再覆盖
  新快照；按新范围重新完整或增量复核。不得用“及时停止”作为旧结论覆盖新代码的理由。
- **每轮新会话**：不要续跑同一 Reviewer 会话；宿主按 child session 统计 `tool_usage`，
  续跑会重置 `totalToolUseCount` 并使信封因 `review_toolcalls_incomplete` 被拒。

### 派发前预检与上下文

- Reviewer 没有 Bash 权限，不再需要、也不再提供命令白名单预检；`commands_run` 必须为空。
- `native_agent.py review-context --state … --dispatch … --execution … --work-item …
  [--previous-review prev.json]`：输出绑定值、当前证据（含 live/dropped）、完成条件、
  执行证据漂移 `execution_evidence_unbound`、上轮 findings 与建议 scope。把这份清单和
  工作项要求一起交给 Reviewer，减少“上下文没给全 → 重新取证”。
- 漂移提示与 ingest 校验都不阻断合同本身：证据被更正后应重新回填执行结果，或在复核
  结论中说明；不得用旧摘要继续声称已覆盖。

### 记录与边界

- 由工作区级 `reviewer` Profile 的只读子代理执行（工具仅 `Read`/`Grep`/`Glob`，无 Bash）。
- 检查真实文件、diff、测试日志与原始证据，给出 verdict 与 findings（含文件位置与证据）。
- 实际读取由 `native_agent.py` 从宿主 `tool_usage`/`part` 合并，不要求 Reviewer 手工
  复述；`commands_run` 恒为空。结论可用 `native_agent.py review` 生成信封并回填；
  `g4_eligible` 只是信封结构预检，正式资格由新链 `g4_attest`/`g4_shadow` 决定。
- **普通复核 PASS 不等于 G4**；`g4_eligible=false` 的结论只能作参考。
- 失败处理：复核信封被拒时先看原因码——绑定/过期/只读边界是流程问题，修流程后
  重试；finding 是实质问题，修复后复核。不要因为复核失败就把任务标为 BLOCKED；被拒
  尝试会追加到任务目录 `review-attempts.jsonl` 供复盘统计。
- **过期诊断**：`native_review_stale` 会在原因里列出失效来源（哪些条件状态、工作项、
  证据相对复核快照新增/变更/移除了），据此决定是补一次完整复核还是用可用基线做增量；
  没有覆盖快照的旧记录会明确提示无法列出变化项。
- **不要写自指条件**：如果某条完成条件本身要求“存在某条 G4 复核信封”，那么把该条件
  标为 `met` 会改变 `final_evidence_digest`，使刚通过的复核立即过期。完成条件应描述
  可独立核验的产物或行为；复核结论由 G4 门禁本身证明，不写进条件。

### 增量复核

同一条工作项在**首次完整复核 PASS 之后**又产生新的证据（例如修复后补了测试日志），
可以用 `native_agent.py review --based-on <基线复核.json>` 做增量复核：

- 基线必须是已回填的 PASS 复核，`g4_eligible=true`，且记录里带覆盖快照；
  因此增量复核只适用于**高风险 G4 链路**（普通风险复核不产生 `g4_eligible=true`
  的基线，按需做一次完整复核即可，不需要增量）；
- 只要求检查基线之后新增/变更的证据（`evidence_manifest`）；证据被归档也算变化；
- 完成条件、合同或其他工作项的结论不得在基线之后变化，否则拒绝增量、要求完整复核；
- 收口时 `governance` 会重算覆盖，确认变化量全部被增量复核覆盖。

增量复核减少的是重复阅读，不是降低标准：未覆盖的变化一律 fail-closed。

## 3. G4：高风险收口门禁

### 链路

正式 G4 由新链派生，旧 Reviewer 信封自述不生效：

1. `bind-execution` 固化合同 `test_policy`、工作区与 nonce；
2. 真实 Executor 子会话执行并产出证据；`execution` 绑定执行记录与代码快照；
3. `review-context --create-attempt` 冻结目标/证据/代码范围；真实 Reviewer 子会话只读复核；
4. `g4_attest.attest` 独立核对宿主 DB、Profile、会话、证据与原始输出；
5. `test_execution.build` 判定四层测试证据；`g4_shadow` 汇总候选资格；
6. `copilot_auto._ingest_review` 只在 shadow `attested` 且 `new_g4_eligible=true` 时
   写入正式 `g4_eligible=true`、`g4_source="candidate"` 并一次性消费 attempt；
7. 收口重算 G1–G4；任何证据不足、绑定不符或门禁未过都失败关闭。

高风险任务必须在合同里显式声明 `test_policy`；`start_state`/`amend_contract` 与
`create_execution_binding` 都会拒绝没有 captured 策略的高风险任务，没有策略时新链
不把 `not_applicable` 当作测试通过。合同策略进入合同指纹，调用方只能加严。

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
| `g4_eligible=true` 且 `g4_source="candidate"` | 由新链 attestation 派生，不是信封自述 |
| `candidate_g4.eligible=true` | `g4_shadow`/`_finalize_candidate` 判定候选可用 |
| `review_attempt_id` 有效且未被重放 | 复核绑定派发前创建、只消费一次的 attempt |
| 代码范围快照等于当前 git 工作区 | 未登记为证据的源码、未跟踪/删除/重命名文件也在范围内 |
| Reviewer `workspaceRoot` == attempt 代码根 | Reviewer 确实在任务工作区运行 |
| `target_sha256` == attempt 快照 == 当前 `final_evidence_digest` | 复核针对当前最终证据，不是过期结论 |
| Reviewer 会话晚于 attempt，prompt 携带 attempt id | 旧会话不能冒充新 attempt 的复核方 |
| 证据文件内容仍等于 attempt 快照 | 复核期间文件被改动时旧结论失效 |
| `reviewer.profile_id == "reviewer"` | 复核方是 Reviewer Profile |
| `host` 存在且 `host_sha256` 与内容一致 | ingest 固化的宿主记录未被改写 |
| `g4_attest` 独立核验宿主 DB | Profile 精确只读、会话/prompt/工作区绑定、工具调用与原始输出一致 |
| `test_execution` 四层通过 | 合同要求的命令/结果/产物完整性已验证（或合同显式 N/A） |

任何一项不满足都会记录原因码；旧宿主记录相关代码（`native_review_host_*`、
`host_*`）只用于历史记录，新链改用 `g4_attest` 原因码（`native_review_missing` / `native_review_not_pass` /
`native_review_blocking` / `native_review_unverified` / `native_review_changed_files` /
`native_review_not_g4_eligible` / `native_review_stale` / `native_review_profile_invalid` /
`native_review_foreign` / `native_review_contract_mismatch` / `native_review_host_missing` /
`native_review_host_changed` / `native_review_host_identity_mismatch` /
`native_review_host_no_tool_calls` / `native_review_attempt_missing` /
`native_review_attempt_foreign` / `native_review_attempt_binding_mismatch` /
`native_review_attempt_target_mismatch` / `native_review_attempt_consumed` /
`native_review_attempt_session_mismatch` / `native_review_attempt_workspace_missing` /
`native_review_attempt_workspace_mismatch` / `native_review_attempt_workspace_drift` /
`native_review_attempt_not_consumed` / `native_review_code_scope_missing` /
`native_review_code_scope_unavailable` / `native_review_code_scope_drift` /
`native_review_legacy_g4_source` / `native_review_candidate_not_eligible`），G4 失败，
任务不能 CLOSED。

增量复核（`review_scope=incremental`）同样要满足上表全部条件，另外收口时会重算
覆盖：基线必须可信、条件与其他工作项在基线之后未变化、新增/变更证据全部列入
`evidence_manifest`（`native_review_incremental_*`）。任何一项不成立即失败关闭，
改做完整复核即可，不存在“增量所以放宽”的路径。

修复后重新验证同一工作项时，用 `copilot_dispatch.py rerun` 把已终结工作项重开为
`pending`（旧结果保留在 `rerun_history`），再生成新的执行/复核信封；不要新建一个
语义重复的工作项来绕过 `work_item_terminal`。

### review attempt：派发快照绑定

G4 不能只依赖“生成信封时重新算一次 `final_evidence_digest`”。否则旧 Reviewer 输出可以在
代码变化后重新封装成新摘要的有效结论。高风险复核必须走 attempt：

```bash
# 1. 派发前：固化当前快照（完成条件已核销、证据无漂移时才允许）
python3 -B scripts/native_agent.py review-context ... --work-item A1 \
  --create-attempt --output context.json
# context.json 里有 review_attempt_id / review_attempt_created_at

# 2. 把 attempt id 写入 Reviewer prompt，再派发只读子代理

# 3. 生成信封时必须绑定同一 attempt
python3 -B scripts/native_agent.py review ... --work-item A1 --attempt RA...
```

- attempt 在派发前由宿主创建，冻结 task / contract / plan / dispatch / work item /
  execution result、`target_sha256`、条件摘要、文件证据内容摘要，以及 git 工作区的
  代码范围（tracked/untracked/新增/删除/重命名，逐文件 SHA-256）；创建后不可修改。
  已登记证据由证据快照单独绑定，代码快照不重复纳入。
- `review --attempt` 要求：attempt 属于本任务、未被消费、所有绑定与当前执行一致、
  attempt 的目标仍是当前 `final_evidence_digest`；宿主 metadata 的 `createdAt` 不早于
  attempt 创建时间、prompt 携带 attempt id、`workspaceRoot` 等于 attempt 代码根，
  由 `g4_attest` 在 ingest 时独立核验。
- ingest 是 attempt 的唯一消费点：先复核宿主记录与 attempt，再在同一份状态写入中标记
  `consumed` 并记录 Reviewer 会话。被拒的 attempt 不消费，修正后可重试；已消费的 attempt
  不能用于另一份结果。
- G4 收口再次核对：attempt 存在、已消费给本条复核、Reviewer 会话一致、目标摘要等于
  attempt 快照与当前状态；任何不一致都失败关闭（`native_review_attempt_*` /
  `native_review_stale`）。
- 文件证据在派发后、ingest 前或收口前被改写，即使 `final_evidence_digest` 未变，也按
  `native_review_attempt_workspace_drift` 拒绝；必须新建 attempt 重做复核。
- 工作区源码/未跟踪文件在派发后、ingest 前或收口前变化（即使没有登记为 evidence），按
  `native_review_code_scope_drift` 拒绝；G2 也检查最近一次已消费 attempt 的代码快照。
  增量复核只在代码范围未变化时可用；代码变化必须完整复核。
- 旧输出重新封装、跨任务复用 attempt、同一 attempt 重放、旧会话冒充新 attempt 都不会
  因为宿主能重算当前 SHA 而变成有效 G4。

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

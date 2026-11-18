# review：SOL 三模式（REVIEW / CONSULT / PLAN）、正式 REVIEW 与已退役的 G4

本文说明当前几类“复核与认知辅助”，以及各自的触发条件、证据要求与失败处理；G4 历史记录
只读保留。

**一句话区分：**

- **普通自检**：ZCode 自己做，不登记为复核，不构成任何门禁依据。
- **SOL 复核（REVIEW）**：按需一次性调用 `gpt-6.2-sol`（零工具、单轮、high）；结论是建议，
  不是门禁。
- **SOL 认知辅助（CONSULT / PLAN）**：可选的一次性问题分析或规划建议，结论只写入
  SOL 记录（`sol_calls`，历史 `sol_advisories` / `sol_reviews` 只读兼容），不进入台账、
  不影响门禁。
- **正式 REVIEW**：用户明确要求时才做，是一个治理记录，**不是普通开发的强制前置门**。
- ~~**G4 复核**~~：v8.0.0 已正式退役，不再是门禁；历史记录只读保留用于审计。

## 0. SOL 零工具单轮调用（v8.2：REVIEW / CONSULT / PLAN 融合）

### 统一入口与共享上下文

- 三种模式共用一条管线：`copilot_dispatch.py` 的 `_run_sol_channel` → `sol_review.run_sol`
  → `governance.record_sol_call`；模式差异只在提示词尾部与结果字段。
- 提示词由 `sol_review.build_prompt` 统一构造，自动注入同一份任务目标、合同正文（
  `done_criteria_text` 与边界）、完成条件、工作项状态与证据登记摘要（ID、类型、摘要前缀、
  大小、支持的条件、归档标记，上限 80 条 / 6000 字符）。主代理只需提供本次增量的材料与
  具体问题，不必重复粘贴门禁已有事实。
- `review-admission` 是“要不要调用 SOL”的单一确定性入口：先算本地任务复杂度（只用状态
  事实）与已有 SOL 覆盖，再结合 G1–G3 给出建议；`decision` 保留风险策略原义以便兼容旧输出。
- 简单任务与普通任务默认不调用 SOL；没有自动触发、没有固定轮次。

### 主动介入策略（v8.3）

**简单任务 0 调用；中高复杂任务主动调用 1 次；只有出现新的重大不确定性才追加调用。**

| 复杂度 | 信号数 | 默认动作 |
|---|---|---|
| `low` | 0 | SOL 0 调用（进入 PASS 也不因阶段调用） |
| `medium` | 1 | 形成计划/证据后建议一次 `PLAN`（无计划）或 `CONSULT`（有计划） |
| `high` | ≥2 | 同上，建议优先在规划/方案阶段收敛判断 |

信号（`complexity.signal_codes`）：`risk-high`、`declared-trigger`（security / permissions /
core-logic / data-integrity / architecture / global-execution / production / release）、
`multi-module`（证据跨 ≥2 个模块目录）、`multi-action`（计划工作项 ≥4）、
`interdependent`（依赖边 ≥2）、`replan-history`（AUTO-REPLAN）、`reopened`、
`user-request`（用户明确要求）。

- **时机**：在形成计划或登记证据（“已形成初步理解”）之后，不要在没有任何现场上下文时调用；
  `review-admission` 与 `drive`/`status` 会打印建议与完整命令，但**不派生工作项、不改变阶段**。
- **覆盖**：同一合同（`contract_sha256` 相同）已有 CONSULT/PLAN（或 REVIEW）结论，且没有
  升级型新信号时返回 `reuse`，直接复用结论。
- **追加**：只有升级型信号（`risk-high` / `replan-history` / `reopened`）新出现，或合同被
  修订（合同指纹变化＝新范围）才允许再调用一次，并给 `--force --trigger
  <new-risk|scope-changed> --reason ...`。证据目录增加、工作项增减属于结构变化，不算新的
  认知对象——避免“每次都再问一遍”。
- **收口**：G1–G3 通过不触发调用；仅高风险任务或出现新信号时给一次 REVIEW 建议
  （advisory）。`drive` 命中 PASS 会立即收口，因此收口前的复核建议在 `status` /
  `review-admission` 中给出，顺序是：最终验证 → `review-admission` →（需要时 SOL）→ 收口。
- **用户明确要求**：直接调用，不受复杂度判断限制；`review-admission --user-request` 把该事实
  写入建议（`user-request` 信号，最高优先级）。
- **BLOCKED / CLOSED 不建议调用**：阻塞只能由用户补齐授权/决策/资源解除，SOL 不提供本地
  事实；已收口的任务需要继续时先由用户授权 `reopen`。
- **记录快照**：SOL 记录写入 `risk` / `complexity` / `complexity_signals`，追加判断只比较
  调用时刻的信号快照，不需要重新推理，也不会因升级前的旧记录重复付费。

### 模型与推理强度

- 统一 `gpt-6.2-sol`（Responses API `/v1/responses`，既有本机 CPA `127` 出口）。
- 所有模式统一 `reasoning.effort="high"`、`reasoning.mode="standard"`：不按角色降级，
  不接受 `low`/`medium`/`xhigh`/`max`，也不允许配置缺失时静默回退。
- 不发送 GPT-6.2 推理模型不支持的采样参数（`temperature`/`top_p`/`top_logprobs`/
  `logprobs`），不请求 `include` 的 logprobs；保持单请求（`store=false`，不使用
  `previous_response_id` 多轮会话）。
- 上游回显 `model`、`reasoning.effort`、`reasoning.mode` 必须与请求一致；不一致按身份/
  参数覆盖事件拒绝消费（退出码 9）。回显存在且一致时记录 `reasoning_verified=true`；
  上游未回显时如实记为未验证，不推测为成功。
- 审计与任务记录保留 `request_id` / `trace_id`（CPA trace）/ `response_id`、usage 与延迟。

### 触发（REVIEW）

风险驱动，不强制：简单任务不调用；普通功能按需；多模块改造、广告/归因/数据链路、安全与
生产高风险建议调用一次；用户明确要求 REVIEW 时调用一次。`review-admission` 只给建议
（`not_needed` / `optional` / `recommended`）与建议模式，不写状态、不阻塞。

### 调用

```bash
python3 -B scripts/copilot_dispatch.py sol-review --state <state.json> \
  --materials <materials.md>
```

- `--materials` 由主代理一次性准备：目标、完成条件、相关规则/决策、完整 Diff、关键文件、
  调用链、测试结果、未解决问题与风险点。命令会自动注入合同/目标/条件；超过 400,000 字符
  拒绝而不是静默截断。
- 请求发往既有本机 CPA `127` 出口的 `gpt-6.2-sol`，`reasoning.effort=high`、
  `reasoning.mode=standard`；不新增 Provider、账户或密钥。
- **零工具**：请求体不含 `tools`/`functions`/`parallel_tool_calls`，`tool_choice="none"`；
  上游出现 `message`/`reasoning` 之外的 output 条目、回显非空 `tools`/`tool_usage`，即判
  安全事件并拒绝。
- `--dry-run` 只构建并校验请求（零工具、模型、推理强度、材料大小），并输出
  `prompt_version`、`cache_key_components`、`evidence_digest` 与注入的证据/工作项数量；
  不发请求、不写状态。

### 单轮与失败

- 一次调用只发一次请求。相同主体摘要再次调用直接复用已有结论，不重复付费。
- **复用键**（`review_subject_sha256`）：提示词版本 `PROMPT_VERSION`、模型与推理强度、
  任务目标、合同正文与边界、完成条件描述、证据集合摘要、主代理材料；辅助分析额外绑定
  模式与问题。条件从 pending 变 met 不失效；合同、证据集合、提示词版本或材料变化即失效，
  不会复用过期判断。
- 再次复核必须 `--force --trigger <user-request|scope-changed|new-risk|materials-omitted>
  --reason <说明>`，并换新材料。
- 超时、429/5xx、网络、截断、响应格式、模型身份、推理强度/模式回显不符：记录真实原因与
  `sol_calls` 失败记录（历史状态仍读 `sol_reviews`），返回非零退出码；不自动重试、不换
  Provider、不派原生 Reviewer 顶替。失败不产生阻塞、不派生 SOL 工作项，主代理继续开发；
  只有用户明确要求必须完成独立复核时才按 BLOCKED 报告。

### 输出

- `copilot-sol-review/v1`：`verdict`（pass/partial/blocked）、`findings`
  （blocking/major/minor/info，含文件/行/依据/建议）、`unverified`、
  `materials_reviewed`。PASS 与 blocking/major 或未验证项冲突时降级 PARTIAL。
- findings 以 `advisory` 进入 Finding 台账：主代理修复后 `finding --status fixed`，
  运行定向测试后 `--status verified`；确认误报时 `--status dropped --note <反证>`。
- 原始输出存任务目录 `sol-reviews/`，审计 `sol-audit.jsonl` 仅脱敏元数据（三模式共用）。

### 与门禁的关系

SOL 没有工具、不能核验宿主权限，也不证明文件读取；SOL 的 PASS 不会写入
`native_reviews`，不会产生 `g4_eligible`，不改变 G1–G3 的结果。高风险任务建议执行一次，
但 SOL 未执行、失败或给出 partial 都不阻断主代理继续推进（用户明确要求必须完成独立复核
而 SOL 不可用时除外）。

## 0.5 SOL 认知辅助：CONSULT / PLAN（v8.1）

### 适用

- **CONSULT**：复杂技术问题分析、疑难故障诊断建议、多方案比较、关键技术风险识别。
- **PLAN**：复杂任务规划建议、可行性分析、依赖与风险分析、替代路线建议。
- **不适用**：简单修改、常规文档、已有明确方案的重复工作、能直接从源码或日志得到答案的
  问题。不设固定轮次，不重复提交相同上下文与相同问题。

### 调用

```bash
python3 -B scripts/copilot_dispatch.py sol-advisory --state <state.json> \
  --mode CONSULT --materials <materials.md> --question "该方案的关键风险是什么？"
```

- `--mode` 只接受 `CONSULT` / `PLAN`；REVIEW 走 `sol-review`。
- `--question` 由主代理给出具体问题；材料与合同/目标/条件会一起注入。
- 零工具、单轮、`gpt-6.2-sol` + `high`，与 REVIEW 使用同一通道与同一安全约束。
- `--dry-run` 校验请求（模式、模型、推理强度、零工具、材料大小）；`--list` 只读查看记录。

### 输出

- `copilot-sol-advisory/v1`：`mode`、`summary`、`analysis`，加 `options`（CONSULT）或
  `steps`（PLAN，含 `depends_on`）、`risks`、`alternatives`、`recommendation`、
  `unverified`。schema 或 mode 与请求不一致、字段类型不符时拒绝消费。
- 结果写入状态 `sol_calls`（历史状态为 `sol_advisories`），原始输出存
  `sol-advisories/`，审计写同一份 `sol-audit.jsonl`（仅脱敏元数据）。
- **治理边界**：不进入 Finding 台账、不改变完成条件、不影响 G1–G3、不阻断任务；是否采纳、
  如何实施由 ZCode 主代理决定。相同主体摘要（材料 + 模式 + 问题 + 证据集合）再次调用直接
  复用结论。
- **单轮与失败**：与 REVIEW 相同；辅助分析失败只登记真实原因，主代理继续工作。

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
- 正式 REVIEW 结论**不参与** G1–G3 判定；它不能让任务跳过证据要求。

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

v8 起原生 Reviewer 已退役，正式 REVIEW 的独立意见只能来自 SOL 零工具单轮复核：材料由主
代理一次性准备，请求体不含工具定义（`tool_choice="none"`），一次调用只发一次请求，模型
回显必须与请求一致。无法真实调用 SOL 时，如实说明是“主代理自检”或“同一上下文复核”，
不得标记为正式 REVIEW，也不得伪造 SOL 结论。历史原生 Reviewer 结论只读保留，不参与新任务。

## 2. 独立复核流程（SOL）

### 风险建议（SOL）

| 任务类型 | 建议 |
|---|---|
| 简单文案、小 Bug、简单 UI | 不调用 |
| 普通功能开发 | 按需 |
| 多模块重大改造 / 复杂核心业务 | 建议一次 |
| 广告、归因、数据链路 | 建议一次 |
| 安全/权限逻辑、生产高风险 | 按风险一次 |
| 用户明确要求 REVIEW | 一次 |

`review-admission` 只返回建议（`not_needed` / `optional` / `recommended`），不写状态、
不是门禁。高风险任务返回 `recommended` + `scope=high-risk`；`recommended_action` 会先看
本地 G1–G3：门禁仍有缺口时是 `not_needed`，门禁全绿后才是风险建议。

### Finding 台账与定向复验

- 每条实质 finding 进入 `finding_ledger`，使用稳定 `F1..Fn`；同一根因换一种表述仍映射到
  同一 ID（显式 `id` / `root_cause` / 文件+摘要相似度），已关闭问题没有新证据只记为
  重复报告。
- 生命周期 `open → fixed → verified → closed`；`fixed` 只表示已改，`verified` 才表示修复
  被定向验证。`fixed`/`verified` 必须有证据，`closed` 只能从 `verified` 进入。
- 未闭合 blocking/major 阻断 G2（所有风险等级）；minor/info 不阻断。已修复未验证派生
  `AUTO-VERIFY-<F>`：只跑定向测试并登记证据，不重新派发完整复核。
- SOL findings 是 advisory：主代理修复并定向验证，误报可用
  `finding --status dropped --note <反证>` 驳回；v7 及更早的 authoritative 记录仍只能
  修复并定向验证。

### 复核收敛流程

充分自检 → 稳定最终执行结果与证据 → 按风险调用一次 SOL（如需要）→ 集中修复 →
定向验证 → 收口。SOL 不是收口前提；主代理判断材料是否具备、意见是否有增量价值。
目标 1–2 轮收敛是性能目标，不是硬上限；不得为减少轮次而漏报或降低标准。

### 首轮完整性协议

- 一次看全：枚举变更面、读取受影响调用方与测试、按根因归组、检查同类缺陷的其他位置；
  finding 使用稳定 ID（`F1..Fn`）与可选 `root_cause`/`category`/`related`。
- 材料必须真实完整并对应最终代码；材料缺失时 SOL 应标记 `unverified`，不能推测已检查
  未提供的代码。主代理不得为了让评测结果好看而删减材料。
- PASS 即停止：合同与条件满足、无 blocking、`unverified` 为空即停；minor/info 记录为
  findings，不驱动重开。

### 后续轮次与停止

- 同一材料摘要直接复用已有结论，不重复付费。
- 只有用户明确要求、变更范围实质变化、出现新的关键风险或首次材料存在重大遗漏时，才用
  `--force --trigger <user-request|scope-changed|new-risk|materials-omitted> --reason ...`
  再调用一次；必须换新材料。
- 执行方必须基于证据回应每条 blocking/major：修复，或用具体反证说明不成立并保留分歧。
- 不采纳意见时给出反证；没有新证据不重复同一轮复核。不得为了通过而降低断言、删除测试
  或放宽门禁。

### 记录与边界

- 成功结果统一写入 `sol_calls`（历史任务状态中的 `sol_reviews` / `sol_advisories` 继续
  只读兼容，不迁移、不改写、不重复计数），原始输出存 `sol-reviews/` 或 `sol-advisories/`，
  审计统一写 `sol-audit.jsonl`，只记脱敏元数据。
- 失败把真实错误码与材料摘要写入 `sol_calls`（`status=failed`）与 `history`；不自动
  重试、不换 Provider、不派原生 Reviewer 顶替。
- SOL 结论不改变阶段判定；阶段只由 `evaluate` 按合同、工作项、证据与门禁计算。

## 3. G4：已退役的历史机制（v8.0.0）

**G4 原生只读复核已正式退役。** 以下内容只用于解释历史状态与只读数据，不是当前操作指南：

- **已删除**：`g4_attest.py`、`g4_shadow.py`、`test_execution.py`、
  `native_agent.py review/review-context/capability`、`copilot-native-review/*` 信封、
  review attempt 创建与 ingest、`AUTO-G4*` 派生、`host_capability_missing` /
  `profile_source_not_builtin` 等宿主能力拒绝码。
- **不再产生**：`g4_eligible`、`g4_source`、`candidate_g4`、G4 PASS、G4 BLOCKED。
  `native_review_retired` 仍保留，用于拒绝历史复核信封。
- **历史只读**：旧状态里的 `native_reviews` / `review_attempts` / `g4_attestations` 与
  `authority=authoritative` 字段原样保留，只能通过 `legacy-status` 或状态文件只读查看；
  不参与新任务准入、派生或收口，也不得批量改写为 PASS。
- **安全职责已转移**：原 G4 的“未闭合 blocking/major 阻断收口”现在由 G2 Finding 台账门禁
  对所有风险等级执行；高风险仍受授权、数据/凭据/部署边界等独立安全约束。
- **历史验收记录**：v6.5/v6.6.1 切换清单随 G4 退役于 2026-11-17 删除；需要追溯时查仓库
  Git 历史。历史记录不作为现行操作指南。

## 4. 缺陷分级与处置

- `blocking`：会导致错误结果、数据损坏、安全问题或无法交付；必须修复后才能收口。
- `major`：实质缺陷或高风险回归；修复并用定向测试证明。
- `minor` / `info`：可维护性、可读性、建议；记录但不阻断收口，不为它们重开完整复核。
- 执行方必须基于证据回应每条 blocking/major：修复、或用具体反证说明不成立并保留分歧。
- 不采纳意见时给出反证；没有新证据不重复同一轮复核。
- 不得为了通过而降低断言、删除测试或放宽门禁。

## 5. 与状态机的关系

- 正式 REVIEW 与 SOL 复核都不改变阶段判定；阶段只由 `evaluate` 按合同、工作项、证据与
  门禁计算。
- 门禁是 G1 工作项终态、G2 合同/条件/证据/台账、G3 无停摆；G4 已退役，不再参与。
- `closure --close` 在任一门失败时拒绝并列出原因码；不存在“强制收口”开关。

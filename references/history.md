# Copilot Skill 版本与历史（历史记录）

> 本文件记录版本沿革与 SOL 通道变化过程，供追溯，**不是当前规则来源**。现行规则见
> `SKILL.md`、`references/workflow.md`、`runtime.md`、`review.md`、`agent-bridge.md`。
> **SOL 决策通道已于 v6.0.0（2026-11-12）退役；v7.0.0（2026-11-17）只恢复一次性 REVIEW
> （零工具、单轮、建议性）；v8.0.0（2026-11-17）正式退役 G4 原生只读复核；v8.1.0
> （2026-11-18）升级 `gpt-6.2-sol` 并恢复建议性 CONSULT / PLAN；v8.2.0（2026-11-18）
> 把三模式融合为 Copilot 内部统一按需认知能力；v8.3.0（2026-11-18）把“什么时候调用
> SOL”升级为确定性复杂度判断与主动介入；v8.4.0（2026-11-18）把 CONSULT / PLAN 并入
> 默认认知引擎 THINK，REVIEW 只在用户明确要求时执行**。

## v8.4.0（2026-11-18）SOL 默认认知引擎 THINK

- **模式收敛**：`SOL_MODES=("THINK","REVIEW")`；CONSULT / PLAN 只作为历史别名读取并归一
  到 THINK；`sol-advisory` 命令移除，新入口 `sol-think`。
- **触发**：简单任务 0 调用；中高复杂任务形成初步理解后建议一次 THINK；只有升级型信号
  （`risk-high` / `replan-history` / `reopened`）或合同修订才追加。
- **REVIEW**：只在 `review-admission --review-request`（用户明确要求）时建议；不再由风险
  或收口阶段自动派生。
- **输出与降级**：`copilot-sol-think/v1` 九段结构化判断；THINK 失败只登记并返回
  `status=unavailable`（退出码 0），REVIEW 失败仍非零。
- **不变量**：零工具、单轮、`gpt-6.2-sol` + `high`、CPA `127` 出口；G1–G3、授权边界与
  G4 退役状态不变。

## v8.3.0（2026-11-18）SOL 主动介入策略

- **问题**：v8.2 的准入只有“风险标签 + 门禁是否全绿”，门禁一旦有缺口就一律
  `not_needed`，导致多模块、跨层、高风险任务在整个开发期都不会调用 SOL；`evaluate` 只在
  PASS 且 `risk=high` 时给一句可选提示。没有复杂度判断，也没有“已咨询过”的覆盖判断。
- **复杂度**：新增 `review_policy.task_complexity`，只用状态事实：`risk-high`、
  `declared-trigger`、`multi-module`（证据跨 ≥2 目录）、`multi-action`（计划 ≥4）、
  `interdependent`（依赖边 ≥2）、`replan-history`、`reopened`。0 信号 low，1 个 medium，
  ≥2 个 high；只统计计划内工作项，治理派生的 `AUTO-*` 不计入。
- **主动介入**：`review_policy.sol_advice` 给出 `none` / `reuse` / `consult` / `plan` /
  `review`，绑定建议模式与阶段；`review-admission`、`evaluate`（`sol` 字段）与
  `drive`/`status` 展示，不派生工作项、不改变阶段。
- **覆盖与追加**：按合同指纹判定覆盖；升级型信号（`risk-high` / `replan-history` /
  `reopened`）新出现或合同修订才允许追加一次；结构变化不诱发重复调用。调用记录新增
  `risk` / `complexity` / `complexity_signals` 快照。
- **不变量**：G1–G3 与 fail-closed 不变；SOL advisory、零工具、单轮、不自动重试；G4 不存在；
  ZCode 唯一工程执行与技术决策主体。

## v8.2.0（2026-11-18）SOL 与 G1–G3 融合

- **单一记录流**：三模式成功/失败记录统一写入状态 `sol_calls`（`call_id` / `mode` /
  `prompt_version` / `evidence_digest`）；历史 `sol_reviews` / `sol_advisories` 只读兼容，
  不迁移、不改写、不重复计数。审计统一 `sol-audit.jsonl`，互斥统一 `.sol.lock`，记录 ID
  统一 `SC`。
- **管线收敛**：`build_prompt` 取代 `build_materials` / `build_advisory_prompt`；
  `record_sol_call` / `record_sol_call_failure` 取代四套按模式复制的登记函数（历史函数名
  保留为薄封装）；`_sol_channel_config` 只保留原始输出目录、文案与预检 schema。
- **共享上下文**：提示词自动注入目标、完整合同正文（`done_criteria_text` 与边界）、条件、
  工作项与证据登记摘要。旧实现误读不存在的 `contract["text"]`，合同正文实际从未提供给 SOL；
  本版修正并把该事实进入复用键。
- **确定性优先**：`review-admission` 增加门禁感知的 `recommended_action`；门禁有缺口时
  `not_needed`，全绿后按风险给出建议；`decision` 保留风险策略原义以兼容旧输出。
- **复用键**：`PROMPT_VERSION` + 模型/推理强度 + 目标 + 合同正文/边界 + 条件描述 +
  证据集合摘要 + 材料（辅助分析另加模式与问题）。条件状态变化不失效。
- **不变**：G1–G3 语义与 fail-closed；SOL advisory、零工具、单轮、不自动重试；ZCode 唯一
  工程执行与技术决策主体；G4 不恢复。

## v8.1.0（2026-11-18）GPT-6.2 SOL Edition

- **模型升级**：SOL 统一 `gpt-6.2-sol`（Responses API `/v1/responses`，本机 CPA `127`
  出口），不再使用 `gpt-6.1-sol`，不静默回退其他模型或别名。
- **推理强度**：CONSULT / PLAN / REVIEW 统一 `reasoning.effort="high"`、
  `reasoning.mode="standard"`；`validate_effort` 只接受 high，拒绝 low/medium/xhigh/max。
  上游回显 `model` / `reasoning.effort` / `reasoning.mode` 不一致即拒绝消费（退出码 9）；
  回显缺失时记录 `reasoning_verified=false`，不推测成功。
- **恢复建议性 CONSULT / PLAN**：新增 `copilot_dispatch.py sol-advisory --mode
  CONSULT|PLAN`；输出 `copilot-sol-advisory/v1`，写入状态 `sol_advisories` 与
  `sol-advisories/`，审计 `sol-advisory-audit.jsonl`。不进入 Finding 台账、不影响 G1–G3、
  不阻断任务；DECIDE 不恢复。
- **Responses API 兼容**：不发送 `temperature`/`top_p`/`top_logprobs`/`logprobs`，不请求
  logprobs include；保持单请求（`store=false`，无 `previous_response_id`）。响应中的
  `tools`/`tool_usage` 回显非空按安全事件拒绝。
- **可追溯性**：审计与任务记录新增 `request_id` / `trace_id`（CPA）/ `response_id`、
  `reasoning_resolved` 与 `reasoning_verified`。
- **不变**：零工具、单轮、不自动重试/换 Provider/二审；G1–G3、证据完整性、PWW 接受、
  授权边界与 ZCode 唯一工程执行/技术决策主体均不变。

## v8.0.0（2026-11-17）G4 Retirement Edition

- **G4 正式退役**：删除 `g4_attest.py`、`g4_shadow.py`、`test_execution.py`、
  `review_output.py` 与 `native_bridge` 复核校验、`native_agent.py review/review-context/
  capability`、`copilot_auto` 复核 ingest、`AUTO-G4*` 派生与宿主能力拒绝码。不再产生
  `g4_eligible`、G4 PASS、G4 BLOCKED 或 `host_capability_missing`。
- **门禁收敛**：`closure_check` 只返回 G1/G2/G3；Finding 台账门禁对所有风险等级生效
  （保留原 G4 的“未闭合 blocking/major 阻断收口”职责）；高风险任务不再要求 native 后端。
- **SOL 统一承接**：高风险建议执行一次 SOL 零工具单轮复核（advisory，不构成门禁）；
  `review_policy.admission` 与 `sol_review.admission` 的 high 分支返回
  `recommended` + `scope=sol`。
- **历史只读**：`native_reviews` / `review_attempts` / `g4_attestations` 与
  `authority=authoritative` 字段原样保留；历史 G4 BLOCKED 不改写为 PASS。
- **未放宽**：G1–G3、fail-closed、证据完整性、PWW 接受、授权与安全边界不变。

## v7.0.0（2026-11-17）SOL Review Edition（历史）

- **原生多轮 Reviewer 退役**：普通复核不再派发只读子代理、不再创建 review attempt、
  不做多轮重入；`native_agent.py review`、`review-context` 与原生复核 ingest 只接受
  `risk=high`（G4），其他风险返回 `native_review_retired`。
- **SOL 零工具单轮复核恢复**：新增 `scripts/sol_review.py` 与
  `copilot_dispatch.py sol-review`。复用既有本机 CPA `127` 出口与 `gpt-6.1-sol`
  （`reasoning.effort=high`），不新增 Provider/账户/密钥/付费方案；请求体不含工具定义，
  `tool_choice="none"`；一次调用只发一次请求，失败不重试、不换 Provider、不自动二审。
- **建议性 findings**：SOL findings 以 `advisory` 进入 Finding 台账；主代理修复并定向
  验证，误报可用 `finding --status dropped --note` 驳回；G4 的 `authoritative` findings
  仍不可被驳回。`sol_reviews` 与 `native_reviews` 严格分开，SOL 不会产生 `g4_eligible`。
- **`review-admission` 改为 SOL 建议**（not_needed/optional/recommended）；普通任务的
  `review_stops`/多轮重入不再参与。G4 链路与 fail-closed 条件不变。
- **G4 仍 BLOCKED**：当前 ZCode 3.18.2 没有内置精确只读载体，正式 G4 结论不变。

## v6.0.0（2026-11-12）ZCode Autonomous Development Edition

- **SOL 退役**：活动执行路径不再有 `gpt-6.1-sol` 调用、SOL 决策节点、等待 SOL 返回的状态
  门禁、仅为 SOL 服务的调度/重试/配额代码。`call_gpt.py`、`review_ledger.py`、
  `reply_protocol.py` 移入仓库 `legacy/`，不再分发；历史调用记录、REVIEW 裁决、任务台账、
  证据与 SHA-256 原样保留，经 `legacy-status` 只读查看。
- **ZCode 原生 Agent 成为唯一工程执行与技术决策主体**：计划、拆分、实施、调试、测试、
  验证与复核组织都由 ZCode 决定；Copilot 只做本地治理。
- **状态机收敛**为 RUNNING / PARTIAL / PASS / BLOCKED / CLOSED，移除 `pending_call`、
  `quota`、`round_seq`、`budget`、`pass_pending` 等远程判定字段；v1/v2 状态只读。
- **PARTIAL 持续推进**：本地派生 `AUTO-C<n>` / `AUTO-R-<id>` / `AUTO-G4` / `AUTO-REPLAN` /
  `AUTO-NEXT`，禁止因轮次、预算、费用、等待模型、重复评审、子 Agent PARTIAL 或普通失败停止。
- **CONSULT / PLAN / REVIEW 重新定位**：本地工作模式；PLAN 由 ZCode 决定并只被记录；正式
  REVIEW 仅在用户明确授权时进行，不是普通开发门禁。
- **G1–G4 保留**：G4 仍要求原生独立只读复核、宿主记录重核、目标摘要绑定与 fail-closed。
- **证据/审计保留**：先登记后引用、SHA-256、文件重读校验、脱敏采集、只增不改的 history、
  收口不可改、reopen 保留旧 closure 快照。

## 2026-11-11：SOL 指令拆分与 REVIEW 历史证据投影（已退役实现）

- `references/role.md` 拆为核心 + `role-consult.md` / `role-plan.md` / `role-review.md`，
  `call_gpt.py` 按模式组装，`role-map.md` 只作 v5.0 映射。指令 Token 14,102 → 9,146 / 9,359 /
  12,265（CONSULT / PLAN / REVIEW）。
- `review_ledger.py` 把历史背景证据投影为“编号 + 位置 + SHA-256 + 恢复条件”，全量包
  254,663 → 87,731 input tokens（−65.6%）；原始台账与证据包字节不变。
- 该批实现随 v6 一并归档到 `legacy/`；历史台账与证据文件保留。

## 2026-11-09：v5.3.3 对齐

- 本 Skill、`references/role.md`、项目 `AGENTS.md`、`docs/PROJECT-CHARTER.md` 统一到
  ChatGPT 自定义指令 v5.3.3：ZCode 原生 Agent 为工程执行主体，SOL 为 Copilot 按判据进入的
  决策通道，简单任务不强制经过 Copilot / SOL / 正式 REVIEW。
- 默认不设轮次、请求数或累计 Token 上限；只保留用户显式限额、服务商配额/限流与授权边界。
- 独立复核由“最多 2 轮、到顶即停”改为证据驱动的无固定次数上限。

## 2026-11-04～11-08：调用门槛与预算口径调整（历史）

- 2026-11-04：SOL 决策通道不再要求用户显式点名，由 Copilot 按“何时调用”判据自主进入；
  REVIEW 三态裁决仍须用户显式要求。
- 2026-11-08：全局门槛对齐，仅正式 REVIEW 保留显式点名要求；当时的“仍受次数、预算、配额
  约束”表述已被 2026-11-09 的无上限口径取代。
- 旧“默认 2/3 轮 + 弹性追加”预算方案只作历史追溯，不再是任何停止条件。

## 更早沿革

- **v5.1.1**（2026-11-04 逐字落盘）为上一版协作基线；v4.3 / v4.5 / v4.2 / v4.0 / v3.4 等
  历史指令文件的本地副本已于 2026-11-17 随 `~/.zcode/references/` 清理删除，原文只在
  ChatGPT 设置维护。
- **模型基线（历史）**：v5 时代使用 `gpt-6.1-sol` + `reasoning.effort=high`，2026-11-01 单次
  真实调用验证过模型名与响应一致、零工具、无回退；High 相对低档的实际增益**从未验证**。
  v6 起该通道退役，本段只说明历史事实。
- 本技能原名 `gpt-assist`（2026-10-19 更名 `copilot`）；`gpt-cpa`、`gpt-director` 已于
  2026-10-18 并入并从技能目录删除，保持退役，不重新引入；历史审计归档在
  `~/.reasonix/archive/removed-skills-20261018/`。
- ChatGPT 自定义指令是唯一权威治理基线，但**不是权限来源**，不高于宿主系统指令、用户授权或
  全局工作规范；本仓库不保存其副本与版本映射，按规则变化做增量同步。

## 历史材料位置

| 材料 | 位置 | 状态 |
|---|---|---|
| v5 SOL 实现（`call_gpt.py`、`review_ledger.py`、`reply_protocol.py`） | 仓库 `legacy/` | 已退役，不分发 |
| v5 历史测试（853 项） | 仓库 `legacy/tests/` | 仅回归历史行为 |
| 历史审计 JSONL / REVIEW 台账 / 旧任务状态 | 原路径保留 | 只读，`legacy-status` 查看 |
| 历史 REVIEW 台账说明（`review-ledger.md`） | `references/` | 带历史横幅，只读 |
| 旧角色与协议文档（`role.md`、`role-*.md`、`role-map.md`） | 仓库 Git 历史（2026-11-17 删除） | 已退役，不再分发或对照 |

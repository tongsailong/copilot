# Copilot Skill 版本与历史（历史记录）

> 本文件记录版本沿革与 SOL 通道变化过程，供追溯，**不是当前规则来源**。现行规则见
> `SKILL.md`、`references/workflow.md`、`runtime.md`、`review.md`、`agent-bridge.md`。
> **SOL 决策通道已于 v6.0.0（2026-11-12）退役；v7.0.0（2026-11-17）只恢复一次性 REVIEW
> （零工具、单轮、建议性）**，不恢复 CONSULT/PLAN 或状态门禁。下文涉及 SOL 的段落按时间
> 标注是否为历史事实描述。

## v7.0.0（2026-11-17）SOL Review Edition

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

- **v5.1.1**（2026-11-04 逐字落盘）为上一版协作基线，现仅作历史对照；v4.3 / v4.5 / v4.2 /
  v4.0 / v3.4 等历史指令文件只作保留。
- **模型基线（历史）**：v5 时代使用 `gpt-6.1-sol` + `reasoning.effort=high`，2026-11-01 单次
  真实调用验证过模型名与响应一致、零工具、无回退；High 相对低档的实际增益**从未验证**。
  v6 起该通道退役，本段只说明历史事实。
- 本技能原名 `gpt-assist`（2026-10-19 更名 `copilot`）；`gpt-cpa`、`gpt-director` 已于
  2026-10-18 并入并从技能目录删除，保持退役，不重新引入；历史审计归档在
  `~/.reasonix/archive/removed-skills-20261018/`。
- ChatGPT 自定义指令是行为理念的对照来源，**不是权限来源**，不高于宿主系统指令、用户授权或
  全局工作规范。

## 历史材料位置

| 材料 | 位置 | 状态 |
|---|---|---|
| v5 SOL 实现（`call_gpt.py`、`review_ledger.py`、`reply_protocol.py`） | 仓库 `legacy/` | 已退役，不分发 |
| v5 历史测试（853 项） | 仓库 `legacy/tests/` | 仅回归历史行为 |
| 历史审计 JSONL / REVIEW 台账 / 旧任务状态 | 原路径保留 | 只读，`legacy-status` 查看 |
| 旧角色与协议文档（`role.md`、`role-*.md`、`role-map.md`、`review-ledger.md`） | `references/` | 带历史横幅，仅作追溯 |

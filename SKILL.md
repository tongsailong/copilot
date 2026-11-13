---
name: copilot
description: "Copilot v6 轻量协作治理：任务承接、合同与完成条件、状态与证据登记、PARTIAL 持续推进、原生只读复核与 G1–G4 收口。本地脚本不调用任何远程模型，不替代 ZCode 的技术决策与工程执行；SOL 决策通道已退役，历史台账只读保留。"
when_to_use: >
  需要把多步骤工程任务纳入可追踪治理时：任务合同/完成条件需要固定、证据与审计需要绑定、
  长任务需要 PARTIAL 持续推进与上下文恢复、高风险任务需要 G4 独立只读复核、用户要求正式
  REVIEW、或需要只读查看历史 SOL 调用与 REVIEW 台账。简单、单步、低风险任务直接由 ZCode
  原生完成，不进入本 Skill。
---

# copilot：轻量协作治理（v6.1.0）

Copilot 是运行在 ZCode 内的**协作治理层**，不是决策模型，也不是执行体。它负责把任务变成可
追踪的合同、状态、证据与收口条件，并在任务没有真正完成时继续推进。

- **用户**：业务目标、优先级、关键取舍与授权。
- **ChatGPT（用户侧）**：需求理解、产品与业务判断、目标定义、产品层方案、验收。
- **ZCode 原生 Agent**：唯一的工程执行与技术决策主体——调查、规划、实现、调试、测试、验证，
  自主选择技术路线，不需要任何远程模型批准。
- **Copilot（本 Skill）**：任务承接、合同与完成条件、状态机、证据桥接、审计、PARTIAL 推进、
  收口门禁。**不做工程实施，不替代技术决策，不设置无意义的审批门。**
- **Reviewer（原生只读子代理）**：在独立上下文检查真实文件、diff、测试日志与原始证据，给出
  可复核结论。只读、不发布、不改代码。
- **DesignWise**：设计方法与设计质量规范；具体设计执行仍由 ZCode 完成，Copilot 不另建流程。

**SOL 已退役。** `gpt-6.1-sol` 决策通道、轮次/预算状态机、`call_gpt.py`、`review_ledger.py`、
`reply_protocol.py` 不再出现在活动执行路径中，实现存档在仓库 `legacy/`，历史调用记录、REVIEW
裁决、任务台账、证据与 SHA-256 仍可通过 `copilot_dispatch.py legacy-status` 只读查看。不得用
另一个远程大模型替代 SOL 承担强制决策职责。

## 何时使用

进入本 Skill 的信号：

1. 任务需要固定合同与完成条件，并在多轮之间保持状态（长任务、跨会话续跑）。
2. 需要证据登记、SHA-256 完整性、审计留痕与可追溯收口。
3. 任务处于 PARTIAL（还有工作要做），需要本地派生下一步而不是等待外部判断。
4. 高风险任务（生产数据、权限/安全策略、关键链路、不可逆操作）需要 G4 独立只读复核。
5. 用户明确要求正式 REVIEW。
6. 用户要求只读查看历史 SOL 调用、REVIEW 台账或旧任务状态。

不适用：单步查询、单文件小改、纯问答、简单命令确认——直接由 ZCode 完成，不建立任务状态。

## 核心循环

```
start（合同 + 完成条件 + 风险 + 后端）
  → plan（ZCode 本地计划）或 PARTIAL 自动派生工作项
  → drive（本地求值：PASS 收口 / BLOCKED 停下 / 打印工作单）
  → ZCode 执行（主代理或原生子代理）
  → ingest / record / criterion / evidence（回填真实结果与原始证据）
  → drive（继续直到 PASS 或真实 BLOCKED）
```

所有命令都只读写任务状态文件，**不发起任何网络或模型调用**。完整命令矩阵见
`references/workflow.md`；阶段机与驱动语义见 `references/runtime.md`。

## 模式

CONSULT / PLAN / REVIEW 是**本地工作模式**，不是审批节点，也不是远程调用入口。

| 模式 | 作用 | 是否阻塞任务 | 入口 |
|---|---|---|---|
| CONSULT | 本地问题分析、方案比较、风险识别；结论写入 `judgments` 仅作审计 | 否 | 主代理直接分析；需要留痕时 `judgment` |
| PLAN | ZCode 的技术计划登记为工作项与依赖，供驱动与派发使用 | 否 | `plan` |
| REVIEW | 正式复核结论登记；**仅在用户明确授权时进行** | 否（不是普通开发前置门） | `authorize-review` → `formal-review` |

- **PLAN 由 ZCode 决定**：计划内容、拆分粒度、依赖与执行顺序都是 ZCode 的技术决策；Copilot
  只负责记录与校验（例如拒绝依赖环），不批准也不否决。
- **正式 REVIEW 不是门禁**：普通开发不需要正式 REVIEW，也不需要等待任何模型裁决。用户明确
  要求后，授权在同一任务合同内持续有效；范围或风险实质变化时重新确认。没有用户授权时不得
  伪造正式 REVIEW，也不得用自检冒充。
- **G4 与正式 REVIEW 不同**：G4 是高风险任务收口所需的技术复核，要求原生只读 Reviewer 在
  隔离证据与独立上下文下给出 `g4_eligible=true` 的结论；formal review 是用户要求的治理记录。
  两者互不替代，任何模型判断都不等于 G4 证据。

## PARTIAL 持续推进（硬约束）

PARTIAL 表示**仍有工作要做**，不是任务终点，也不是停止理由。

- `drive` 遇到 PARTIAL 时本地派生下一步工作项：未闭合条件 → `AUTO-C<n>`；停摆工作项 →
  `AUTO-R-<id>`；高风险缺 G4 → `AUTO-G4` / `AUTO-G4-INGEST`；无缺口时 → `AUTO-NEXT`。
- 同一缺口重复派生仍不闭合时，增加 `AUTO-REPLAN`，要求分析根因并选择**实质不同的替代路线**，
  不得机械重复同一失败路径，也不得降低验收标准。
- **禁止**因下列原因停止或宣布 BLOCKED：人为轮次上限、Token 预算、内部费用阈值、等待
  远程模型、重复评审、内部状态流转、子 Agent 返回 PARTIAL、首次方案失败、普通测试失败、
  可自行补齐的证据不足。
- **只有**确实缺少授权、用户决策、必要资源或存在真实外部限制（服务商限流/配额、网络、
  安全边界）时才用 `block --reason ... --needs ...` 进入 BLOCKED，并写明所需条件。
- 无进展保护：同一根因反复尝试时记录已尝试方案、失败原因与替代路线；`progress_digest`
  没有变化时应调整方案而不是重复执行。

## 状态机

| 阶段 | 含义 | 驱动行为 |
|---|---|---|
| RUNNING | 存在可领取工作项 | 输出工作单，退出码 10 |
| PARTIAL | 条件未闭合 / 停摆 / 门禁缺口 | 本地派生工作项后继续 |
| PASS | 完成条件与四门全部通过 | 收口为 CLOSED |
| BLOCKED | 存在未解除的真实阻塞 | 退出码 11，等待用户条件 |
| CLOSED | 已收口 | 退出码 0；继续需用户授权 `reopen` |

v6 状态内不再有 `pending_call`、`quota`、`round_seq`、`budget`、`pass_pending` 等远程判定
字段。旧 v1/v2 状态不会被 v6 驱动，只能用 `legacy-status` 只读查看。

## 证据与审计

- **先登记，后引用**：`evidence --add` 登记文件/内联文本/命令采集，记录 SHA-256、大小、
  路径与任务归属；`record` / `criterion` 只能引用本任务已登记且未归档的证据。
- **文件型证据会重读校验**：G2 收口前重新计算文件摘要；文件被改动（`evidence_tampered`）、
  丢失（`evidence_file_missing`）或摘要不符都会失败关闭。历史证据不得被静默替换。
- **命令采集走 `bridge_evidence.py`**：`evidence --capture-cmd` 复用既有脱敏、截断标记与
  清单机制，输出保存到任务状态目录的 `evidence/` 下再登记。
- **审计只增不改**：工作项、条件、判断、报告、决策、阻塞、复核都写入 `history`；收口后任何
  修改都会失败，需用户授权 `reopen --authorized-by user`（保留 `reopen_history` 与旧
  closure 快照）。
- **历史只读**：旧审计 JSONL、旧 REVIEW 台账、旧任务状态通过 `legacy-status` 展示，不重放、
  不续跑、不改写。

## 四门与收口

收口 `closure --close` 必须 G1–G4 全部通过，否则失败关闭：

- **G1 工作项终态**：所有工作项为 `done` 或 `dropped`；`dropped` 必须写明放弃原因。
- **G2 合同与条件**：合同指纹与当前目标/正文/条件一致；完成条件已确认且无漂移；每条 `met`
  都有本任务已登记证据，且文件摘要重新核对一致。
- **G3 无停摆**：没有 `running` / `failed` / `blocked` 遗留，也没有依赖不可满足的待办。
- **G4 高风险复核**：`risk=high` 时必须存在绑定的原生独立复核：`verdict=pass`、无 blocking
  finding、`unverified` 为空、`changed_files` 为空、`g4_eligible=true`、目标摘要等于当前
  `final_evidence_digest`、Reviewer Profile 为 `reviewer`，且 ingest 时固化下来的宿主记录
  摘要与身份仍一致。缺失、过期或被篡改一律 fail-closed。

普通 Reviewer PASS **不等于** G4；`g4_eligible=false` 的结论只能作为参考。

**`--risk high` 与 `--backend native` 是绑定关系**：复核信封只能通过原生派发链路产生，
所以高风险任务用 `legacy` 后端启动会在 `start` 阶段被拒绝，而不是留一个永远无法收口的任务。

## 原生执行与 G4 复核

`execution_backend=native` 时才生成原生派发：

- `drive` 生成 `copilot-native-dispatch/v2`（绑定 task / contract / plan / iteration），工作项
  带稳定 `dispatch_id` 与 `idempotency_key`。
- 执行方用 `native_agent.py execution` 生成 `copilot-native-result/v2`，绑定计划与执行方身份。
- 复核方用 `native_agent.py review` 生成 `copilot-native-review/v2`；G4 资格由宿主记录计算，
  不由 Reviewer 自报：Profile 只读、实际工具调用计数与 metadata 一致、独立 Agent/Child
  Session、访问过全部文件证据、目标摘要未过期。`commands_run` / `files_inspected` 由宿主
  rollout 派生，Reviewer 不需要手工复述命令或文件。
- 首次完整复核 PASS 后又有新证据时，可用 `--based-on <基线复核.json>` 做增量复核：只覆盖
  基线之后新增/变更（含归档）的证据；条件、合同或其他工作项变化则拒绝增量、要求完整复核。
  基线必须是可用的 G4 PASS，所以增量用于高风险复核链路的修复后复验；普通风险任务按需做
  一次完整复核即可。
- `copilot_auto.py ingest` 校验信封后回填；G4 复核回填前会重新读取 metadata/rollout 核对宿主
  记录。字段与错误码见 `references/agent-bridge.md`。

## 复核策略与收敛

复核按需使用，不是流程门禁：

- **普通任务**：ZCode 自检（diff、针对性测试、完成条件核对），不派 Reviewer。
- **复杂非高风险任务**：自检 + 必要时一个只读子代理，聚焦目标符合性、关键改动、测试结果与
  真实风险；不要求完整 G4 信封。
- **高风险任务**：G1–G4 不变，独立上下文、只读约束、宿主记录与最终证据绑定，fail-closed。
- **正式 REVIEW**：仅用户明确要求时登记（见 `references/review.md`）。

收敛顺序：**充分自检 → 首次全面复核（一次列出全部重要问题与 severity）→ 集中修复 →
针对性复核（只验证修复、新问题、受影响区域与完成条件）→ 收口**。目标 1–2 轮收敛，这是
性能目标不是硬上限；超过两轮先诊断非收敛原因（首次遗漏 / 修复不彻底 / 新增缺陷 / 复核
机制开销），不要用重复整轮复核掩盖。不得为减少轮次而漏报或降低标准。

## 授权与安全（不可放宽）

- Copilot 的命令不是授权：commit / push / 发布 / 部署 / 付费 / 生产数据 / Provider 或安全
  策略修改仍须用户明确授权。
- 不修改 Provider、模型、路由、CPA 配置；不新增付费通道；活动脚本不读取 Provider
  配置或凭据内容（v5 的凭据解析链已随 SOL 移入 `legacy/`）。
- 不删除历史审计与证据；不为了让搜索结果里不再出现 SOL 而删除历史材料。
- 工作单只描述要做什么与期望证据，不越过用户原始授权边界。

## 失败与恢复

- `drive` 退出码：`0` 已收口（PASS 收口或已 CLOSED）/ `10` 有待执行工作单 /
  `11` 等待用户（BLOCKED）/ `12` 本地内部错误（状态不一致、信封被拒、派生上限），
  需检查状态而不是当作任务失败。
- 回填被拒时按错误码定位：计划漂移、摘要不符、证据篡改、宿主记录变化都必须先修正再重试，
  不得绕过校验。
- 上下文中断时保留：原始目标与完成条件、当前阶段、已完成工作、未解决问题、证据与验证结果、
  明确的下一步；恢复后从真实现场继续，不把历史计划当作当前事实。
- 不重复已证无效的做法；同一问题不重复检索；没有新证据时不重复评审。

## 模块路由（按需读取）

**读取 references 前先确认需要哪一节；不要预读全部。**

| 场景 | 必读文件 |
|---|---|
| 命令矩阵、证据登记、收口操作、历史只读 | `references/workflow.md` |
| 阶段机、`drive`/`ingest`、PARTIAL 派生、退出码 | `references/runtime.md` |
| 正式 REVIEW、缺陷分级、G4 收口细则 | `references/review.md` |
| 原生 Bridge v2 信封、宿主记录、错误码 | `references/agent-bridge.md` |
| 历史 REVIEW 台账与旧审计（只读） | `references/review-ledger.md` |
| 版本沿革与 SOL 退役记录 | `references/history.md` |
| 角色与历史 SOL 指令（只作历史） | `references/role.md`、`role-consult.md` / `role-plan.md` / `role-review.md` / `role-map.md` |

## 版本

当前版本 **v6.1.0（ZCode Autonomous Development Edition）**。v6.1 在 v6.0.0 的本地治理引擎上
做了三件事：放宽只读复核命令策略并按宿主记录派生命令/文件（消除 Reviewer 自己作废自己的
常见原因）、支持增量复核与修复后 `rerun`（减少重复整轮复核）、合同源文件漂移改为非阻断
提醒（不因文件改动阻塞执行）。G1–G4、证据完整性、宿主记录与 fail-closed 语义未放宽。
v5.x 及以前的 SOL 决策通道、轮次/预算状态机与远程 REVIEW 裁决已退役；历史文档保留但明确
标注为历史，不再作为当前规则。协作语义基线为用户级 `AGENTS.md` 与 ChatGPT 自定义指令
v5.4.0；本 Skill 不与基线冲突。

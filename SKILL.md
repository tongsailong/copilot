---
name: copilot
description: "Copilot v8.1 轻量协作治理：任务承接、合同与完成条件、状态与证据登记、PARTIAL 持续推进、SOL（gpt-6.2-sol，high，零工具）按需 CONSULT/PLAN/REVIEW 认知辅助与 G1–G3 收口。SOL 只提供建议，不替代 ZCode 的技术决策与工程执行；G4 原生只读复核已退役，历史台账只读保留。"
when_to_use: >
  需要把多步骤工程任务纳入可追踪治理时：任务合同/完成条件需要固定、证据与审计需要绑定、
  长任务需要 PARTIAL 持续推进与上下文恢复、复杂问题需要一次 SOL 零工具独立判断（CONSULT /
  PLAN）或一次 SOL 独立复核（REVIEW）、用户要求正式 REVIEW、或需要只读查看历史 SOL 调用、
  G4 记录与 REVIEW 台账。简单、单步、低风险任务直接由 ZCode 原生完成，不进入本 Skill。
---

# copilot：轻量协作治理（v8.1.0）

Copilot 是运行在 ZCode 内的**协作治理层**，不是决策模型，也不是执行体。它负责把任务变成可
追踪的合同、状态、证据与收口条件，并在任务没有真正完成时继续推进。

- **用户**：业务目标、优先级、关键取舍与授权。
- **ChatGPT（用户侧）**：唯一权威治理基线——需求理解、产品与业务判断、目标定义、产品层
  方案、角色分工、授权原则与最终验收；原文只在 ChatGPT 设置维护，本 Skill 不保存其副本、
  不维护版本映射或对齐表。
- **ZCode 原生 Agent（主代理）**：唯一的工程执行与技术决策主体——调查、规划、实现、调试、
  测试、验证，自主选择技术路线，不需要任何远程模型批准。**默认由主代理直接执行**，不为了
  体现协作而强制调用子代理。
- **内置子代理（按需）**：`Explore` 只做独立只读调查；`general-purpose` 只承担边界清晰、
  可独立交付且并行收益明显的工作。二者都由主代理拆分、控制范围并集成结果。
- **SOL（一次性认知辅助与独立复核）**：经既有本机 CPA `127` 通道调用 `gpt-6.2-sol`
  （`reasoning.effort=high`，`standard` 模式），**零工具、单轮**地阅读主代理一次性提交的
  材料，按模式返回结构化结论：CONSULT（问题分析/方案比较/风险识别）、PLAN（规划、依赖与
  替代路线建议）、REVIEW（独立复核 findings）。三者都是建议，不执行工程、不做技术决策、
  不读写台账、不调度子代理，也不自动进入第二轮。
- **Copilot（本 Skill）**：任务承接、合同与完成条件、状态机、证据桥接、审计、PARTIAL 推进、
  收口门禁。**不做工程实施，不替代技术决策，不设置无意义的审批门。**
- ~~**G4 只读复核**~~：**已正式退役**（v8.0.0）。G4 不再作为活动质量门禁，不再要求宿主
  只读 Agent、Profile 来源检查、权限快照准入或独立取证。历史 G4 记录只读保留，用于审计与
  追溯，不参与新任务的准入、派生或收口；不得把历史 G4 BLOCKED 改写成 PASS。
- **DesignWise**：设计方法与设计质量规范；具体设计执行仍由 ZCode 完成，Copilot 不另建流程。

**原生 Reviewer 与 G4 复核均已退役。** v8 起 SOL 是唯一远程通道：
`copilot_dispatch.py sol-review` 做独立复核（高风险任务建议执行一次，普通任务按风险决定），
`copilot_dispatch.py sol-advisory --mode CONSULT|PLAN` 做按需认知辅助。SOL 结论是
**advisory**，不构成收口门禁，也不证明宿主只读权限。
`native_agent.py review`、`review-context`、`capability`、review attempt 与
`copilot-native-review/*` 信封已删除。历史 `call_gpt.py`、`review_ledger.py`、
`reply_protocol.py` 实现存档在 `legacy/`，历史调用记录、G4 记录、REVIEW 裁决、任务台账、
证据与 SHA-256 仍可通过 `copilot_dispatch.py legacy-status` 只读查看。

## 何时使用

进入本 Skill 的信号：

1. 任务需要固定合同与完成条件，并在多轮之间保持状态（长任务、跨会话续跑）。
2. 需要证据登记、SHA-256 完整性、审计留痕与可追溯收口。
3. 任务处于 PARTIAL（还有工作要做），需要本地派生下一步而不是等待外部判断。
4. 多模块改造、数据链路、安全或生产高风险变更，需要一次 SOL 独立复核。
5. 高风险任务（生产数据、权限/安全策略、关键链路、不可逆操作）建议执行一次 SOL 单轮复核。
6. 复杂问题存在多条技术路线、跨模块调用链或疑难故障，需要一次独立认知视角（CONSULT / PLAN）。
7. 用户明确要求正式 REVIEW。
8. 用户要求只读查看历史 SOL 调用、REVIEW 台账或旧任务状态。

不适用：单步查询、单文件小改、纯问答、简单命令确认——直接由 ZCode 完成，不建立任务状态。

## 核心循环

```
start（合同 + 完成条件 + 风险 + 后端）
  → plan（ZCode 本地计划）或 PARTIAL 自动派生工作项
  → drive（本地求值：PASS 收口 / BLOCKED 停下 / 打印工作单）
  → ZCode 执行（主代理或原生子代理）
  → ingest / record / criterion / evidence（回填真实结果与原始证据）
  → [按需] sol-advisory（CONSULT/PLAN 认知辅助）或 sol-review（独立复核）
           → 主代理核实/采纳/修复/定向验证
  → drive（继续直到 PASS 或真实 BLOCKED）
```

除 `sol-review` 与 `sol-advisory` 外，所有命令只读写任务状态文件，不发起网络或模型调用。
两个 SOL 命令都是一次调用只发一次请求，不自动重试、不自动二审。完整命令矩阵见
`references/workflow.md`；阶段机与驱动语义见 `references/runtime.md`。

## 模式

CONSULT / PLAN / REVIEW 既可以是主代理的本地工作模式，也可以按需转成 **SOL 辅助调用**；
两者都不是审批节点，SOL 调用也不是决策入口。

| 模式 | 作用 | 是否阻塞任务 | 入口 |
|---|---|---|---|
| CONSULT | 本地问题分析、方案比较、风险识别；结论写入 `judgments` 仅作审计 | 否 | 主代理直接分析；需要留痕时 `judgment` |
| PLAN | ZCode 的技术计划登记为工作项与依赖，供驱动与派发使用 | 否 | `plan` |
| REVIEW | 正式复核结论登记；**仅在用户明确授权时进行** | 否（不是普通开发前置门） | `authorize-review` → `formal-review` |
| SOL CONSULT / PLAN | 一次性零工具辅助分析；结论写入 `sol_advisories`，只作建议 | 否 | `sol-advisory --mode CONSULT|PLAN` |
| SOL REVIEW | 一次性零工具独立审查；意见进入 Finding 台账供主代理核实 | 否 | `sol-review` |

- **PLAN 由 ZCode 决定**：计划内容、拆分粒度、依赖与执行顺序都是 ZCode 的技术决策；Copilot
  只负责记录与校验（例如拒绝依赖环），不批准也不否决。
- **SOL 只提供建议**：CONSULT / PLAN 是可选认知辅助，REVIEW 是可选独立审查。三者都不接管
  工程执行、技术选型、收口判断或写操作；材料由主代理一次性准备，结论由主代理判断是否采纳。
  不恢复 DECIDE，不设强制调用，也不因等待 SOL 停止已有工作。
- **正式 REVIEW 不是门禁**：普通开发不需要正式 REVIEW，也不需要等待任何模型裁决。用户明确
  要求后，授权在同一任务合同内持续有效；范围或风险实质变化时重新确认。没有用户授权时不得
  伪造正式 REVIEW，也不得用自检冒充。
- **正式 REVIEW 一次登记、后续修订**：结论只登记一次；证据编号/引用/文字口径的非实质
  修订用 `formal-review --amend <review_id> --reason ...` 追加 amendment，不生成新的 PASS。
  amendment 要求目标摘要未变；证据实质变化时拒绝 amendment，必须用
  `--supersedes <review_id>` 登记新结论并说明旧结论为何失效。原始记录、授权快照与结论不被
  覆盖。
- **SOL 与正式 REVIEW 不同**：SOL 是建议性独立审查（advisory），不构成门禁；formal review
  是用户明确要求时的治理记录。二者都不证明宿主权限，也不互相替代。G4 已退役，不再有
  原生只读复核门禁。

## Agent 调度（v6.9.0）

**主代理直接执行是默认策略。** 子代理只解决“独立上下文、真实并行、独立交付物”三类问题，
不用来体现协作，也不设调用比例、默认 Token 预算或强制审批。

### 先判断要不要派发

依次回答五个问题；任一答案不支持调用，就由主代理直接完成：

1. 是否需要独立上下文？（跨模块调用链、改动前风险调查、需要不受主代理当前假设影响）
2. 是否能够真正并行？（子代理工作与主代理当前工作可同时进行）
3. 是否有明确、独立的交付物？（不是“帮我看看”，而是可验收的结果/补丁/报告）
4. 是否能减少主代理总工作时间？（并行收益 > 上下文准备 + 集成成本）
5. 是否值得多一次模型调用与结果核对？

通常 **0–2 个**子代理是合理起点；只有存在真实独立工作才增加并行数量。可用
`copilot_dispatch.py agent-policy --json` 记录判断依据（只读建议，不是门禁）。判定顺序是
先否决、后选择：子代理不可用或写入范围冲突 → 主代理；只读调查 → `Explore`（**不交给全工具
代理**）；独立交付物需并发且有收益 → `general-purpose`；其余 → 主代理。以下任务
**默认不派发**：简单修改、单模块开发、连续调试、局部测试与修复、构建与最终集成、方案已
明确实施、以及不值得建立独立上下文的任务。

### Explore（只读调查）

适用：跨模块代码调查、复杂调用链追踪、多个独立方向的只读研究、改动前风险调查、需要独立
上下文的代码检查。

派发时必须提供：

- 调查目标与需要回答的问题；
- 文件范围或起始路径；
- **必要的项目背景与规则**——内置 `Explore` 的 `injectAgentsMd=false`，**不会自动注入
  AGENTS.md**，主代理必须把相关约束写进 prompt；
- 预期证据：真实文件路径、行号、命令输出或代码片段。

`Explore` 只调查、不修改工程文件；它的宿主工具集包含 `Bash`（可写/可执行），所以**不是
只读沙箱**；需要证明只读权限的结论不能依靠它。

### general-purpose（独立交付）

仅在同时满足以下条件时使用：工作范围明确、文件边界明确、完成条件明确、与主代理正在修改
的文件没有不可控冲突、并行收益明显高于上下文与集成成本。`general-purpose` 拥有完整工具
（`tools=["*"]`）并继承 MCP，**不能当作只读复核者**。主代理负责拆分工作、控制授权范围、
检查文件冲突、集成成果与最终测试验收；内置子代理不能再创建自己的子代理。

### 子代理失败与降级

- **主代理继续**：子代理不可用（`UNKNOWN_AGENT_TYPE`、Profile 未加载、权限不足、超时）时，
  主代理继续完成可独立完成的工作；普通任务不得因 Agent Profile 加载失败而 BLOCKED。
- **不机械重派**：同一失败派发不原样重复；先分析错误码（类型不存在、权限不满足、范围冲突），
  改用主代理、`Explore` 或调整任务边界。同一缺口已有 `pending`/`running` 工作项时不再创建
  相同子代理任务。
- **PARTIAL 继续**：子代理返回 PARTIAL 只表示该工作项未完成；主代理分析剩余工作并继续，
  必要时派生 `AUTO-REPLAN`，不得把局部失败当作任务终点。
- **结果绑定**：子代理结果必须带来源（`agentId`/`childSessionId`/`profileSnapshot`）、
  任务范围和原始证据；执行信封由 `native_agent.py` 从宿主记录生成，不接受自报身份。

## PARTIAL 持续推进（硬约束）

PARTIAL 表示**仍有工作要做**，不是任务终点，也不是停止理由。

- `drive` 遇到 PARTIAL 时本地派生下一步工作项：未闭合条件 → `AUTO-C<n>`；停摆工作项 →
  `AUTO-R-<id>`；未闭合 finding → `AUTO-FIX-<F>`；已修复未验证 → `AUTO-VERIFY-<F>`；
  无缺口时 → `AUTO-NEXT`。G4 退役后不再派生 `AUTO-G4*`；SOL 复核是否执行由主代理按风险
  决定，不变成阻塞性的派生工作项。同一缺口已有 `pending`/`running` 工作项时不再派生第二条，
  多个入口重复求值不会复制工作单。
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
| PASS | 完成条件与 G1–G3 全部通过 | 收口为 CLOSED |
| BLOCKED | 存在未解除的真实阻塞 | 退出码 11，等待用户条件 |
| CLOSED | 已收口 | 退出码 0；继续需用户授权 `reopen` |

v6 状态内不再有 `pending_call`、`quota`、`round_seq`、`budget`、`pass_pending` 等远程判定
字段。旧 v1/v2 状态不会被 v6 驱动，只能用 `legacy-status` 只读查看。

### BLOCKED 下的受控写入

BLOCKED 表示**不能正常收口或继续受阻动作**，不表示无法记录真实进展。允许在阻塞期间登记：

- 已完成的执行结果（`record`）、修复后重验（`rerun`）；
- 已存在的真实证据（`evidence --add`）、完成条件核销（`criterion`）；
- 报告、判断、额外阻塞（`report` / `judgment` / `block`）；
- 用户已明确授权的正式 REVIEW 结论及后续 amendment（`authorize-review` / `formal-review`）。

这些登记**不会解除阻塞**，也不改变合同、授权或验收条件；`closure --close` 仍拒绝未解除的
阻塞，G1–G3 仍按原规则重算。可能扩大范围或具破坏性的操作继续拒绝：`plan`、`decide`、
`evidence --drop`。允许多个真实阻塞并存；存在多条未解除阻塞时，`unblock` 必须用
`--block-id` 逐条解除，不能用一条理由把不同阻塞一起清除。合同修订走单独的
`contract --amend`：BLOCKED 下允许，但会更新合同指纹并使旧正式 REVIEW 授权失效，
不属于上述普通写入白名单。

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
- **证据卫生提醒**：`closure-check` 对重复登记同一文件、内容完全相同、临时目录证据、
  `evidence/` 下未登记文件，以及“执行信封引用的证据摘要已被更新或归档”给出**非阻断**提醒；
  不改变门禁，由 ZCode 判断补 note、归档、重新回填执行结果或保留。
- **收口交付清单（只读）**：`handoff --state ... [--json]` 汇总任务阶段、证据统计与工作区
  未提交/未跟踪文件的分类（治理记录、任务证据、验收产物、未分类）。它只读 git 状态，
  **不提交、不删除、不改写 `.gitignore`**；已提交内容与忽略项不在清单里，也不会被自动处理。
- **执行信封审计事件**：执行信封回填写入 `native_result_ingested` 历史事件；被拒执行信封
  追加到任务目录 `review-attempts.jsonl`（只记错误码与绑定值，不记正文），供复盘统计
  “无效信封”而不必依赖终端输出。
- **SOL 复核审计**：`sol-review` 成功时把结构化结果写入状态 `sol_reviews`、原始输出存入任务
  目录 `sol-reviews/<review_id>.raw.txt`，失败把真实错误码与材料摘要写入 `sol_reviews`
  （`status=failed`）与 `history`；`sol-review-audit.jsonl` 只记脱敏元数据（材料/请求/回复
  摘要、模型、推理强度回显、请求/追踪 ID、usage、耗时、错误码），不记正文与凭据。相同材料
  再次调用直接复用，不重复请求。
- **SOL 辅助分析审计**：`sol-advisory` 成功写入状态 `sol_advisories`、原始输出存入
  `sol-advisories/<advisory_id>.raw.txt`，审计写入 `sol-advisory-audit.jsonl`；失败写入
  `sol_advisories`（`status=failed`）与 `history`。辅助分析不进入 Finding 台账、不改变完成
  条件、不影响 G1–G3，也不阻断任务。

## 三道门与收口

收口 `closure --close` 必须 G1–G3 全部通过，否则失败关闭（G4 已退役，不再参与收口）：

- **G1 工作项终态**：所有工作项为 `done` 或 `dropped`；`dropped` 必须写明放弃原因。
- **G2 合同、条件与台账**：合同指纹与当前目标/正文/条件一致；完成条件已确认且无漂移；每条
  `met` 都有本任务已登记证据，且文件摘要重新核对一致；Finding 台账中 `open` / `regressed` /
  已修复未验证（`fixed`）的 blocking/major 阻断收口（所有风险等级），minor/info 不阻断。
- **G3 无停摆**：没有 `running` / `failed` / `blocked` 遗留，也没有依赖不可满足的待办。

历史 G4 记录（`native_reviews` / `review_attempts` / `g4_attestations`）只读保留：不产生
新门禁、不影响 PASS，也不得因为存在历史 BLOCKED 记录而阻止新任务收口。SOL 复核是建议，
即使未执行或失败也不阻断普通任务收口。

## PWW 质量结论与用户接受

verify 的结构化质量报告（`copilot-verify-result/v1`）走报告接入路径时自动治理：

- `report --verify-report <报告文件> [--workspace <repo>]` 解析并校验机器可读结论；PWW 自动创建
  用户接受完成条件，绑定报告内容摘要、`W1..Wn` / `R1..Rn`、目标快照、代码快照（如声明）与
  当前合同/条件。重复接入同一报告是幂等，不重复登记条件；FAIL/PARTIAL/BLOCKED 不创建接受
  条件，其未解决项仍沿替代链保留。
- **报告替代**：存在生效报告时，新报告必须用 `supersedes_report_id` 指向它，并显式处理旧报告
  全部未解决 W/R：继承到新报告（新 PWW 的 W/R）或用 `resolved_warning_ids` /
  `resolved_residual_risk_ids` 声明解决。解决声明仅在 `subject` 与警告来源报告完全一致时
  生效；PASS 不能继承未解决警告；PASS/PWW 未覆盖旧 W/R 时整次替代被拒绝，
  FAIL/PARTIAL/BLOCKED 则自动继承旧未解决项。旧接受记录保留但不会
  继承，新 PWW 重新进入 pending；旧报告已由用户接受的 W/R 视为已解决，不要求新报告继承。
- **代码范围**：声明了 `commit` / `files` / `diff_sha256` / `code_scope_sha256` 的报告必须在
  verify 时先运行 `code_scope_capture.py --workspace <repo> --state <state> --exclude <报告文件>`，把 `commit`、`code_scope_sha256` 与 `files_paths`
  （字符串路径数组，重命名含新旧路径）写入 `subject.files`；`dirty` 可选，给出时必须一致。
  `diff_sha256` 若给出必须与 `code_scope_sha256` 相同；报告文件放任务目录或工作区外。
  `subject.code_scope_sha256` 是报告生成时声明的摘要并进入 `report_sha256`；接入时用
  `--workspace` 重算，代码在生成后/接入前变化即拒绝。Copilot 保存的是接入时重算快照，
  只证明接入/收口时一致，不证明报告生成时刻。
- **工作区身份**：任务工作区优先由 `start --workspace` 绑定，否则由 native execution
  binding、合同来源、任务证据所属仓库、任务目录唯一派生；`report --workspace` 必须与
  任务绑定一致，无法确认或指向其他仓库时 `quality_code_scope_workspace_unbound` /
  `_mismatch` 失败关闭。G2 收口前重算快照，HEAD、tracked/untracked/新增/删除/重命名或
  声明范围不一致即拒绝；质量报告快照不排除已登记证据。纯文档
  报告不要求代码快照或工作区；提供 `--workspace` 时必须声明代码字段。
- **G2 强制**：未接受、只接受部分警告、接受绑定失效（目标/合同/条件/警告/代码变化）、
  报告替代关系不合法或最新质量结论不是 PASS/PWW 时，`closure --close` 一律拒绝。接受检查
  与条件列表相互独立，合同修订删除条件也不能绕过。
- **只有用户能接受**：`accept --by user --accept <W/R id> --quote <用户原话>`；执行方、子代理、
  verify 与 Copilot 不得代替用户接受，`criterion` 命令也不能直接核销接受条件。只接受部分
  警告时保持 pending。Copilot 记录的是 user-declared 接受；CLI 无法从进程层证明来源，
  伪造属于授权越界（见 `docs/DECISIONS.md` 第 49 条）。
- **重开与失效**：任务重开后旧 PWW 接受立即失效，必须重新验收并再次取得用户接受；
  目标/合同/条件/警告/代码变化、报告被替代或接受记录与证据/条件不一致时同样拒绝收口。
- **原始结论保留**：接受 PWW 后收口记录为 `closure.verdict=pass_with_warnings` 与
  `closure.quality`；原始 `PASS_WITH_WARNINGS` 不改写为 PASS。`quality --state ...` 查询
  当前报告、替代关系、未解决项与接受状态；下游不得只看 `closure.verdict`。
- **边界**：必需测试/证据缺失、完成条件未核销、blocking/major、存在未验证项或交付无法验证时
  拒绝 PWW；FAIL/PARTIAL/BLOCKED 报告不会被接受机制提升。高风险任务建议在最终验收前完成
  接受登记（接受会改变条件状态与最终证据摘要）。


**`--risk high` 不再绑定 `--backend native`**：G4 退役后高风险任务可用任一后端启动，
收口只需 G1–G3；原生后端是可选执行路径，不是高风险前提。

## 原生执行与 SOL 复核

原生信封链路只承担**执行**：`execution_backend=native` 时才生成原生派发：

- `drive` 生成 `copilot-native-dispatch/v2`（绑定 task / contract / plan / iteration），工作项
  带稳定 `dispatch_id` 与 `idempotency_key`。
- 执行方用 `native_agent.py execution` 生成 `copilot-native-result/v2`，绑定计划与执行方身份。
- 执行信封由 `native_agent.py execution` 从宿主 metadata/rollout/DB 生成，绑定计划、
  执行方身份与证据 SHA-256；`copilot_auto.py ingest` 校验后回填。
- **独立复核与认知辅助都走 SOL**：`copilot_dispatch.py sol-review`（REVIEW）与
  `sol-advisory --mode CONSULT|PLAN` 都是零工具、单轮、advisory；不再有 review attempt、
  宿主只读能力探测或 `copilot-native-review/*` 信封。需要独立意见时，主代理一次性准备材料
  （目标/条件、规则、完整 diff、关键源码、数据与权限逻辑、测试结果、已知问题与高风险区域）
  提交一次。
- **历史兼容**：v7 及更早的状态里可能保留 `native_reviews` / `review_attempts` /
  `g4_attestations`。它们只读保留用于审计，不再被任何门禁、派生或 ingest 逻辑消费；
  不得手工改写历史记录，也不得把历史 G4 BLOCKED 当成当前任务状态。
- **高风险任务必须显式声明 `test_policy`**：`start`/`contract --amend` 与
  `bind-execution` 都会拒绝没有 captured 策略的高风险任务；合同策略进入合同指纹，
  `required=false` 才允许测试层 N/A，调用方只能加严，不能删除或放宽合同要求。

## 复核策略与收敛（v7）

复核是风险控制手段，不是开发流程的必经节点。普通复核只有一条活动通道：
**SOL 零工具单轮复核**（`copilot_dispatch.py sol-review`）。

### 风险驱动，不强制

| 任务类型 | SOL 调用策略 |
|---|---|
| 简单文案、小 Bug、简单 UI | 不调用 |
| 普通功能开发 | 按需调用 |
| 多模块重大改造 | 建议调用一次 |
| 广告、归因、数据链路 | 建议调用一次 |
| 安全及生产高风险变更 | 按风险调用一次 |
| 用户明确要求 REVIEW | 调用一次 |

- `review-admission` 只返回建议（`not_needed` / `optional` / `recommended`），不写状态、
  不是门禁。主代理按风险、复杂度与潜在收益自主决定。
- **高风险任务**：建议在最终验证后执行一次 SOL 复核；SOL 是 advisory，不构成收口门禁，
  也不证明宿主只读权限。G4 原生只读门禁已退役，不再有 `host_capability_missing` 或
  G4 BLOCKED 状态。
- 正式 REVIEW 仍只在用户明确授权时登记，不因风险等级推定授权。

### CONSULT / PLAN 认知辅助（v8.1）

SOL 除了独立复核，还提供两种可选的建议性认知辅助，由主代理判断是否值得一次调用：

| 场景 | 模式 | 期望产出 |
|---|---|---|
| 复杂问题存在多条技术路线、需要方案比较 | CONSULT | `analysis` / `options` / `risks` / `recommendation` |
| 疑难故障、跨模块调用链、需要独立认知视角 | CONSULT | 关键判断与不确定项 `unverified` |
| 复杂任务拆分、依赖关系与风险分析 | PLAN | `steps`（含 `depends_on`）/ `risks` / `alternatives` |
| 方案可行性需要替代路线与顺序建议 | PLAN | `recommendation` 与适用条件 |

- **入口**：`copilot_dispatch.py sol-advisory --state S --mode CONSULT|PLAN --materials M.md
  [--question "..."]`；`--dry-run` 只校验请求，`--list` 只读查看。
- **不适合调用**：简单修改、常规文档调整、已有明确方案的重复工作、能直接从源码或日志
  得到答案的问题。不设固定轮次，不重复提交相同上下文和相同问题。
- **治理边界**：结论写入 `sol_advisories`，不进入 Finding 台账、不改变完成条件、不影响
  G1–G3；是否采纳、如何实施由 ZCode 主代理决定。
- **模型与推理强度**：所有 SOL 模式统一 `gpt-6.2-sol` + `reasoning.effort="high"`
  （`standard` 模式），不按角色降级，不允许静默回退到 `medium`/`low` 或其他模型；
  上游回显的 `model`、`reasoning.effort`、`reasoning.mode` 不一致时拒绝消费（退出码 9）。

### 单轮原则（硬约束）

- **一次调用只发起一次 SOL 请求**。同一材料摘要（辅助分析还绑定模式与问题）再次调用直接
  复用已有结论，不重复付费。REVIEW 与 CONSULT/PLAN 各自独立判定，不互相复用。
- **不自动二审**：修复后由主代理核实、修复并做受影响的定向测试；不因 Minor/Info 新建
  第二轮，不与 SOL 反复辩论，不把 PASS 当“再跑一轮”的理由。
- 只有用户明确要求、变更范围实质变化、出现新的关键风险、或首次材料存在重大遗漏时，才用
  `--force --trigger <user-request|scope-changed|new-risk|materials-omitted> --reason ...`
  再调用一次；必须换新材料并写明原因。
- **失败收敛**：429、超时、网络错误、截断、响应格式异常、模型身份不符都记录真实原因并退出，
  不自动重试、不切换 Provider、不派原生 Reviewer 顶替，也不阻断主代理继续开发。用户要求
  必须完成独立复核而 SOL 不可用时，如实报告 BLOCKED，不伪造 PASS。

### 审查材料（由主代理一次性准备）

必须包含：任务目标与完成条件、相关项目规则/关键决策、完整变更 Diff、必要的关键文件内容、
关键调用链与数据流、已执行测试及结果、尚未解决的问题、需要重点关注的风险。不得只给简短摘要
让 SOL 猜测。`sol-review` 自动注入合同/完成条件/目标，并把材料拼成一次完整上下文；超过
400,000 字符直接拒绝（提示主代理整理），不静默截断。

SOL 输出契约：总体结论 `verdict=pass|partial|blocked`；`findings` 按
`blocking/major/minor/info` 分级，每项带文件、位置、材料依据与建议；`unverified` 列出无法
从材料确认的事项。PASS 与 blocking/major 或未验证项冲突时自动降级为 PARTIAL 并记录
`verdict_adjusted`。

辅助分析输出契约（`copilot-sol-advisory/v1`）：`mode`、`summary`、`analysis`，以及按模式的
`options`（CONSULT）或 `steps`（PLAN），加 `risks`、`alternatives`（PLAN）、
`recommendation`、`unverified`。schema 或 mode 与请求不一致、字段类型不符时拒绝消费，
不做叙述猜测。

### 输出消费与 Finding 台账

- SOL 的结论是**建议**，不是任务 PASS。`sol_reviews` 与历史 `native_reviews` 分开；
  任何 SOL 记录都不会产生 `g4_eligible`，也不改变门禁集合。
- SOL findings 以 `advisory` 身份进入 Finding 台账：主代理逐条核实后，修复并登记
  `fixed`/`verified`；确认是误报的，可用 `finding --status dropped --note <反证>` 驳回
  （仅 advisory 的 blocking/major 允许）。v7 及更早的 authoritative 记录仍只能修复并
  定向验证。
- 未闭合的 blocking/major 阻断 G2（所有风险等级）；minor/info 不阻断、不派发新复核。
- 原始输出保存在任务目录 `sol-reviews/`，审计 `sol-review-audit.jsonl` 只记脱敏元数据
  （材料/请求/回复摘要、模型、usage、耗时、失败原因），不记正文与凭据。

### Finding 台账与定向复验

- 每条实质 finding 进入 `finding_ledger`，使用稳定 `F1..Fn`；同一根因换一种表述仍映射到
  同一 ID（显式 `id` / `root_cause` / 文件+摘要相似度），已关闭问题没有新证据只记为
  重复报告，不重复登记为新问题。
- 生命周期 `open → fixed → verified → closed`；`fixed` 只表示已改，`verified` 才表示修复
  被定向验证。`copilot_dispatch.py finding --state … --list` 查看；
  `finding --state … --id F1 --status fixed --evidence E1` 登记修复；
  `--status verified --evidence E2` 登记定向测试/检查结果。`fixed`/`verified` 必须有证据，
  `closed` 只能从 `verified` 进入。
- 未闭合 blocking/major 阻断 G2；minor/info 不阻断、不派生复核。已修复未验证的 finding
  派生 `AUTO-VERIFY-<F>`：只跑对应定向测试并登记证据，**不**重新派发完整复核；仍复现时
  标记 `regressed` 继续修复。
- **不重报、不重复复核**：已关闭问题无新证据不得重报。只有变更范围实质扩大、原结论依赖的
  代码变化、跨关键路径的新问题或风险判断失效时，才按重入触发再调用一次 SOL；同一材料摘要
  直接复用已有结论。

收敛顺序：**充分自检 → 稳定最终执行结果与证据 → 按风险调用一次 SOL（如有）→ 集中修复 →
定向验证 → 收口**。SOL 不是收口前提：主代理判断材料是否具备、意见是否有增量价值。
目标 1–2 轮收敛是性能目标，不是硬上限；不得为减少轮次而漏报或降低标准。

### 效率指标

`copilot_dispatch.py review-metrics --state …` 输出 SOL 复核计数与 verdict/finding 分布、
复核准入、停止标记、Finding 台账与无效信封计数；历史 G4 数据只作为计数并标记
`g4.retired`，不参与活动判断。

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
- 子代理派发失败时先读错误：`UNKNOWN_AGENT_TYPE`（类型未发现）→ 确认工作区、会话启动时机
  与可用类型，不要原样重试；执行信封被拒时按错误码定位（`executor_identity_missing`、
  `result_sha_mismatch`、`evidence_sha_mismatch`、`dispatch_mismatch`）→ 修正元数据或证据后
  重新生成信封，不复用旧输出。普通任务在这些失败后由主代理继续。
- 上下文中断时保留：原始目标与完成条件、当前阶段、已完成工作、未解决问题、证据与验证结果、
  明确的下一步；恢复后从真实现场继续，不把历史计划当作当前事实。
- 不重复已证无效的做法；同一问题不重复检索；没有新证据时不重复评审。

## 模块路由（按需读取）

**读取 references 前先确认需要哪一节；不要预读全部。**

| 场景 | 必读文件 |
|---|---|
| 命令矩阵、证据登记、收口操作、只读交付清单、历史只读 | `references/workflow.md` |
| 阶段机、`drive`/`ingest`、PARTIAL 派生、退出码 | `references/runtime.md` |
| SOL 三模式、单轮复核、正式 REVIEW、缺陷分级、台账与历史 G4 记录 | `references/review.md` |
| 原生 Bridge v2 执行信封、错误码 | `references/agent-bridge.md` |
| Agent 调度与内置 Explore/general-purpose 能力基线 | `copilot_dispatch.py agent-policy`；`references/agent-bridge.md` |
| 历史 REVIEW 台账与旧审计（只读） | `references/review-ledger.md` |
| 版本沿革、G4 退役与 SOL 升级记录 | `references/history.md` |

## 版本

当前版本 **v8.1.0（GPT-6.2 SOL + 按需认知辅助）**。本版升级模型并恢复可选的辅助认知能力，
不改变任务合同、G1–G3、授权边界与工程执行主体：

- **模型升级**：统一 `gpt-6.2-sol`（Responses API `/v1/responses`，本机 CPA `127` 出口）；
  不再使用 `gpt-6.1-sol`，不静默回退其他模型或别名。
- **推理强度固定 high**：CONSULT / PLAN / REVIEW 全部 `reasoning.effort="high"`、
  `reasoning.mode="standard"`；拒绝 `low`/`medium`/`xhigh`/`max`，也不允许 Provider 覆盖。
  上游回显 `model` / `reasoning.effort` / `reasoning.mode` 不一致即拒绝消费（退出码 9）。
- **兼容 GPT-6.2 Responses API**：不发送 `temperature`/`top_p`/`top_logprobs`/`logprobs`
  等推理模型不支持的采样参数；不使用 `include` 的 logprobs；保持单请求、`store=false`，
  不引入 `previous_response_id` 多轮会话。
- **恢复 CONSULT / PLAN（建议性）**：新增 `sol-advisory --mode CONSULT|PLAN`，零工具、
  单轮、advisory；结论写入 `sol_advisories`，不进入 Finding 台账、不影响 G1–G3、不阻断任务。
- **职责边界不变**：ZCode 主代理仍是唯一工程执行与技术决策主体；SOL 不恢复 DECIDE、不强制
  调用、不执行命令、不读写台账、不调度子代理；正式 REVIEW 仍只按用户授权登记。
- **可追溯性**：审计与任务记录新增请求 ID / CPA trace ID / 响应 ID、推理强度回显与
  `reasoning_verified` 标记；无法取得回显时如实标记未验证，不推测为成功。
- **错误收敛不变**：一次调用一次请求，不自动重试、不换 Provider；429/5xx/超时/网络/结构
  异常/身份或参数不符都如实记录，且不阻断主代理继续开发。

v8.0.1（历史）只收紧 SOL 安全与可靠性，不改变任务合同、G1–G3 与授权边界：

v8.0.0（历史）正式退役 G4 原生只读复核，把独立质量审查统一交给 SOL 零工具单轮复核：

- **G4 退役**：删除 `g4_attest.py`、`g4_shadow.py`、`test_execution.py`、原生复核信封与
  `native_agent.py review/review-context/capability`；不再产生 `g4_eligible`、G4 PASS、
  G4 BLOCKED、`AUTO-G4*` 或 `host_capability_missing`。
- **门禁收敛为 G1–G3**：`closure --close` 只检查工作项终态、合同/条件/证据与 Finding 台账、
  无停摆。台账检查对所有风险等级生效（原 G4 的安全职责保留），SOL 的 advisory findings
  同样进入台账。
- **高风险不再绑定 native 后端**：`risk=high` 可用 `legacy` 或 `native`；`test_policy`
  仍必须在合同中声明（通用工程约束，不是 G4 前提）。
- **SOL 承接独立审查场景**：安全/权限逻辑、数据迁移与完整性、归因/关键数据链路、复杂核心
  业务、重大架构调整、用户明确要求时，建议执行一次 SOL 复核；SOL 仍是 advisory、零工具、
  单轮、不自动二审，不证明宿主只读权限。
- **历史只读**：`native_reviews` / `review_attempts` / `g4_attestations` 与历史 G4 BLOCKED
  状态原样保留，只用于审计追溯；`legacy-status` 仍可只读查看，不改写、不重放。
- **未放宽**：G1–G3、fail-closed、证据完整性、PWW 接受、授权与安全边界不变。G4 退役不取消
  生产数据/权限/凭据/部署等独立授权约束。

v7.0.0（历史）用 SOL 零工具单轮复核取代原生 Reviewer 多轮复核：

- **唯一远程复核通道**：`scripts/sol_review.py` + `copilot_dispatch.py sol-review`。复用既有
  本机 CPA `127` 出口与 `gpt-6.1-sol`（`reasoning.effort=high`），不新增 Provider、账户、
  密钥或付费方案。
- **能力级零工具**：请求体不含 `tools`/`functions`/`parallel_tool_calls`，显式
  `tool_choice="none"`；上游出现 `message`/`reasoning` 之外的 output 条目即按安全事件拒绝。
- **单轮**：一次调用只发一次请求；同一材料摘要直接复用结论；再次复核必须
  `--force --trigger <原因> --reason <说明>`。失败不重试、不换 Provider、不派原生 Reviewer。
- **输出契约**：`copilot-sol-review/v1`，verdict + blocking/major/minor/info + unverified；
  PASS 与 blocking/major/未验证项冲突时降级为 PARTIAL。
- **Finding 建议化**：SOL findings 以 `advisory` 进入台账；主代理修复并定向验证，误报可用
  `finding --status dropped --note <反证>` 驳回。
- **当时的 G4 状态**：v7 仍保留 G4 原生只读复核链，并因宿主缺少精确只读载体而 BLOCKED；
  该状态已在 v8.0.0 正式退役，历史记录保留。

v6.12.0 是 G4 宿主能力校验与调度收敛专项：

- **宿主现场复核（ZCode 3.18.2）**：工作区只读 Profile 真实加载并生效——`profileSnapshot`
  记录 `tools=[Read,Grep,Glob]`、`disallowedTools` 含 `Write/Edit/Bash`，44 个真实
  `independent-readonly` 子会话只出现 `Read/Grep/Glob`；普通模式请求 Bash 返回
  `tool_not_found`。但该 Profile 是 **project 级自定义 Agent**（宿主 `rCi` 把
  `<cwd>/.zcode/agents` 标为 `source:"project"`），不是内置子代理。
- **G4 架构合规（BLOCKED）**：原定架构只允许 ZCode 内置子代理。宿主内置只有
  `Explore`（含 Bash）与 `general-purpose`（全工具），没有精确只读载体；`Agent` 工具
  没有派发期 `tools` 参数。项目自定义 Profile 即使工具集精确，也不作为正式 G4 方案；
  `profileSnapshot.source` 必须是 `built-in`，否则 `profile_source_not_builtin`。
  因此当前宿主的正式 G4 只能是 **BLOCKED**，不能用自定义 Agent 或放宽权限制造 PASS。
- **plan 模式 Bash 注入（新发现）**：父会话 `planEnabled=true` 时子会话继承 plan 模式，
  宿主会给子代理注入 Bash 并可真实执行（真实记录：`independent-readonly` 子会话执行了
  `git status --short`）。因此“精确只读”不能只看 `profileSnapshot`：`g4_attest`、
  `native_agent review` 与 `capability` 都读取子会话 `runtime/execution_state`，要求
  `planEnabled=false`；缺失/未知一律 fail-closed（`host_session_state_missing` /
  `host_session_state_unknown` / `host_session_plan_mode`）。
- **控制平面工具**：宿主对所有子代理无条件注入 `RespondToCoordinator`
  （`agent.message.respond`、`sideEffectScope=session`，只向父运行时队列发消息，不能读写
  文件或执行命令）；它不在声明白名单内，`x3n` 即使 Profile 禁用也会重新加入。按“工具集
  字面精确等于 Read/Grep/Glob”的口径，它使任何子代理都无法满足字面精确；按“无额外
  文件/命令能力”的口径不影响只读性。代码不把它加入白名单，G4 仍按声明策略与内置来源判定。
- **能力探测可复验**：`capability` 的 `host_version` 是本次真实探测值（读
  `app.asar package.json`，每次重新探测、不缓存；探测失败才回退常量并标
  `source=unavailable`），`host_baseline` 固定为代码基线版本；同时输出
  `session_execution`（mode/planEnabled）、`classification.carrier` /
  `architecture_compliant`，并保留 `host_evidence` 逐调用统计。
  `agent-policy --g4-required --plan-mode` 在 plan 模式下直接给出“不可派发”。
- **能力缺口不反复派发**：最近一次 G4 取证的拒绝码属于宿主/Profile 能力缺口时，
  `drive` 派生 `AUTO-G4-CAPABILITY`（诊断/修正/如实 block），而不是再派一个相同的
  只读会话；环境修复后新取证会正常回到 `AUTO-G4`。
- **未放宽**：G1–G4、fail-closed、attempt/宿主记录/证据绑定、PWW 接受与工具白名单不变；
  plan 模式与来源约束只会让更多场景 fail-closed，不会放行。

v6.11.0 是效率与性能专项，只减少重复开销，不改判定规则：

- **字节码预编译**：文档命令统一用 `python3 -B`，`-B` 只禁止写 `.pyc`、不禁止读取。
  `sync_runtime.py --apply` 在同步后编译运行时字节码，单次命令不再重复编译约
  17k 行源码。行为不变；解释器版本或源码变化时 Python 自行重编译。
- **一次命令一次快照**：`capture_code_snapshot` 新增可选 `cache`，由命令入口
  （`drive`/`status`/`closure-check`）在单次进程内复用同一工作区快照；`close_task`、
  `_evaluate_core`、`_gate_quality`、`derive_work` 共用一份，不再对同一工作区反复
  扫描 git。库调用与测试默认 `cache=None`，漂移检测仍逐次重算。
- **更少的 git 子进程**：工作区根与 HEAD 合并为一次 `rev-parse`（失败才分开重试）；
  `git_root()` 单进程按目录缓存（一个任务的多个证据路径不再各跑一次 git）。
- **未放宽**：G1–G4、fail-closed、代码范围/证据绑定、漂移检测与 PWW 接受规则不变；
  缓存只复用同一命令内的只读结果，不跨进程。畸形仓库（index 有 gitlink 但缺
  `.gitmodules`）仍按 `submodule_status_failed` 让快照不可用。

v6.10.0 收敛复核频率与工具执行判定：

- **三级复核策略**：`risk=low`（文档/文案/样式/简单修复）与 `risk=medium`（普通开发/
  多页面 UI/常规重构，历史 `normal` 等价）默认不派 Reviewer；只有声明实质触发因素且独立
  视角确有收益时才派一次定向复核。`risk=high` 恒走 G4，门禁不变。正式 REVIEW 仍要求用户
  明确授权。`start --risk` 接受 `low|medium|high`（`normal` 兼容映射到 `medium`），
  `review-admission` 输出 `tier` 与风险准入/重入结论。
- **工具请求不等于执行**：`host_records.tool_execution_state()` 把宿主记录分为
  `completed`/`denied_not_executed`/`executed_failed`/`execution_unknown`；只有策略拒绝
  错误码且零时长/零输出/无退出码才认定未执行。`g4_attest`、`native_agent review` 与
  `capability` 统一使用该判定。
- **Bash 拒绝不再使复核失效**：plan 模式拒绝 Bash 等“请求但未执行”的记录不计入 G4 工具
  白名单违规、不触发只读检查、不消耗 attempt；原会话继续用 `Read/Grep/Glob`。实际执行
  Bash/Write/Edit 仍不合格；状态未知 fail-closed；重复拒绝记录
  `repeated_denied_calls`。
- **未放宽**：G1–G4、fail-closed、attempt/宿主记录绑定、证据与 PWW 接受规则不变。

v6.9.0 优化 Agent 调度与原生子代理集成：

- **调度策略落地**：主代理直接执行为默认；`Explore` 只做独立只读调查；`general-purpose`
  只承担边界清晰、可独立交付且并行收益明显的工作。新增五问决策清单与
  `copilot_dispatch.py agent-policy`（只读建议，不是门禁）；不设调用比例、预算或强制协作。
- **内置能力基线（ZCode 3.18.2）**：`Explore` 工具集含 `Bash`（`read_only=0`）、
  `injectAgentsMd=false`；`general-purpose` 为 `tools=["*"]` 并继承 MCP。宿主没有按派发
  覆盖工具的参数，也没有 OS 级只读沙箱。
- **G4 不放宽**：仍要求宿主精确 `Read/Grep/Glob` + 禁用 `Write/Edit/Bash`；内置 Explore 与
  general-purpose 都不合格。新增
  `native_agent.py capability --metadata ...` 只读诊断实际权限与 G4 资格；无法满足时
  fail-closed 并如实报告能力缺口，不伪造 PASS。
- **Profile 根因收敛**：宿主在会话启动时从 user/project 目录加载 Profile 并在会话内缓存；
  旧 user `reviewer`（含 Bash）或内置 Explore/general-purpose 会产生
  `profile_tools_not_exact`；未知类型返回 `UNKNOWN_AGENT_TYPE`，不会静默回退。
- **失败与持续推进**：子代理不可用/返回 PARTIAL 时主代理继续；不原样重复失败派发；普通任务
  不因 Profile 加载失败而 BLOCKED；G4 能力缺口与业务源码缺陷分开报告。
- **未放宽**：G1–G4、fail-closed、attempt/宿主记录绑定、证据与 PWW 接受规则不变。

v6.8.0 收敛独立复核效率：

- **风险准入**：新增 `review-admission`；高风险恒需 G4，普通风险默认不派 Reviewer，声明
  `security`/`core-logic`/`data-integrity` 等触发因素才建议复核；纯文档/格式调整不复核。
- **一次完整复核 + 定向复验**：PASS 写入 `review_stops`；同一目标/代码范围拒绝重复
  attempt；文档级变化返回 `review_not_needed_docs_only`；代码/目标变化必须声明
  `blocking-fix`/`major-fix`/`code-changed` 等重入触发并记录 `review_admissions`。
- **Finding 台账**：稳定 `F1..Fn`、跨轮去重、`open→fixed→verified→closed` 生命周期与
  证据要求；未闭合 blocking/major 阻断 G4/G2，minor/info 不阻断；已修复未验证派生
  `AUTO-VERIFY-<F>` 做定向测试，不重跑完整复核。
- **调度幂等与失败恢复**：同一快照的未消费 attempt 直接复用；`derive_work` 不复制
  `pending`/`running` 的同一缺口；派发失败不无限重试；上下文恢复沿用现有 attempt。
- **度量**：`review-metrics` 新增 `full_reviews`、`incremental_reviews`、
  `review_admissions_total`、`review_stops_total`、`rereported_findings`、
  `incremental_new_blocking/major`、`no_information_reviews`、首次完整复核与总耗时。
- **未放宽**：G1–G4、fail-closed、attempt 绑定、宿主记录、证据与 PWW 接受规则不变。

v6.7.1 修复报告替代与代码范围绑定：

- **替代关系**：新增 `supersedes_report_id`、`resolved_warning_ids`、
  `resolved_residual_risk_ids`；新报告必须明确替代当前报告，旧未解决 W/R 必须继承或
  在 subject 一致时声明解决，否则拒绝；未解决项沿替代链保留，PASS 不能携带未解决警告。
- **代码范围**：`code_scope_capture.py` 在 verify 时先于报告生成 `commit`/`dirty`/
  `code_scope_sha256`；声明摘要进入 `report_sha256`，接入时重算并绑定，G2 收口前再次
  重算，代码漂移拒绝收口。Copilot 保存的 `code_scope` 标注为 ingest 重算，不冒充生成
  时刻；纯文档报告不要求快照。
- **工作区身份**：`start --workspace` 绑定任务工作区，未绑定时从证据/合同/派发上下文
  唯一派生；`report --workspace` 必须一致，未绑定或指向其他仓库失败关闭；工作区进入
  接受绑定。提供 `--workspace` 时报告必须声明代码字段，纯文档报告省略工作区。
- **历史兼容**：v6.7.0 报告按旧载荷重算摘要（缺少 `generated_at` 时不写空串），兼容旧/
  过渡绑定口径与缺少 `acceptance_history` 的历史接受记录；真实 v6.7.0 状态已验证。
- **G2**：替代链、覆盖完整性、代码快照与接受记录一致性全部纳入检查；PASS 绑定变化报
  `quality_binding_stale`，G4 仅排除未跟踪证据产物，原有 PWW 接受与原始结论保留规则不变。

v6.7.0 建立 PWW 自动治理：

- **报告接入即识别**：verify 结构化质量报告（`copilot-verify-result/v1`）经
  `report --verify-report` 接入；只认标记 + JSON 结构，缺字段、正文关键词、校验失败一律
  fail-closed。
- **自动接受条件**：PWW 自动创建用户接受完成条件并绑定报告摘要、W/R 集合、目标快照与合同；
  重复接入幂等，新报告替代旧报告时原接受不继承。
- **G2 强制与用户边界**：未接受/部分接受/绑定失效/最新结论非 PASS 时拒绝收口；只有
  `accept --by user` 能登记接受，部分接受保持 pending；执行方不得代替用户。
- **原始结论分离**：`closure.verdict` 保留 `pass_with_warnings`，`closure.quality` 与
  `quality` 命令记录原始 verdict、接受范围与时间，不改写为 PASS。

v6.6.1 把工作区里最后一个
Reviewer 专用文件改为通用只读权限载体：

- **Profile 通用化**：`.zcode/agents/reviewer.md` → `.zcode/agents/independent-readonly.md`，
  只声明宿主只读权限（`tools=[Read,Grep,Glob]`、禁用 `Write/Edit/Bash`、无 skills/MCP），
  不再包含 Reviewer 身份、复核流程、输出契约或生命周期；同一个子代理既可用于普通只读
  调查，也可用于独立复核。
- **输出契约随派发走**：`review-json/v2` 的格式要求写在主 Agent 的派发 prompt 里，不再由
  Profile 承担；G4 判定、Session/attempt/证据绑定与 fail-closed 不变。
- **调用策略归主 Agent**：是否派生子代理、派几个、是否需要复核由 ZCode 主 Agent 按风险
  自主决定；Copilot 只做状态治理与取证，不维护 Reviewer 生命周期。

v6.6 把复核方身份与权限证据解耦：

- **有效权限取代 Profile 身份**：复核资格只看宿主 `profileSnapshot` 派生的有效工具策略
  （`tools=[Read,Grep,Glob]`、禁用 `Write/Edit/Bash`、无 skills/MCP）；Profile 名称、来源
  与路径只作记录事实，不再参与 G4 判定。缺少策略证据时失败关闭。
- **范围**：`native_bridge`/`native_agent`/`governance`/`test_execution`/`g4_attest`/
  `g4_shadow` 同步改为策略判定；执行方只读 Profile 一律拒绝（`executor_profile_readonly`）。
- **保留**：宿主 `tools` 白名单仍是唯一可证明的只读约束；工作区通用只读 Profile
  （v6.6.1 起为 `independent-readonly.md`）在原生接口支持按派发指定工具前仍保留。

v6.5 完成正式 G4 切换与旧 Reviewer 体系退役：

- **正式 G4 切换**：`copilot_auto._ingest_review` 只在 `risk=high`、`g4_shadow` 状态
  `attested` 且新链 eligible 时写入正式 `g4_eligible=true`/`g4_source="candidate"`；
  `governance` 拒绝没有 candidate 来源的旧记录。信封自报 `g4_eligible` 不再生效。
- **旧机制退役**：删除旧全局 `agents/reviewer.md` 与运行时副本，复核只使用工作区级
  只读 Profile（Read/Grep/Glob，无 Bash）；删除 Bash 白名单、rollout 强依赖、旧会话/
  宿主记录重复校验入口与重复证据计算；`sync_runtime` 不再分发任何 Profile。
- **合同测试策略**：高风险任务必须显式声明 `test_policy`；合同命令/报告/产物进入合同
  指纹与 binding，调用方只能加严；测试证据按四层判定，只有当前合同显式不要求测试时
  才接受 N/A。
- **保留**：G1–G4 标准、fail-closed、attempt 绑定、原始输出持久化与一次性消费均不降低；
  历史 attestation 与旧台账只读保留。

v6.4 基于《测试体系与独立复核机制过度设计专项审计报告》的真实任务证据，只做三项 P1 收敛：

- **BLOCKED 受控写入**：允许在阻塞期间登记证据、工作项结果、条件核销、报告、判断、额外
  阻塞与已授权正式 REVIEW；不自动解除阻塞，不改变合同/授权，`close` 仍拒绝；`plan`、
  `decide`、`evidence --drop` 继续拒绝。
- **复核顺序与 PASS 收口**：最终 G4 复核前先稳定条件、证据与摘要，派发前读取
  `review-context` warnings；PASS 后 minor/info 记录为 findings/residual_risks，不修改已
  通过证据重开完整复核；后续实质变化必须重新判断原 PASS 是否仍覆盖新快照。
- **正式 REVIEW amendment**：原始结论只登记一次；非实质修订用 `--amend` 追加，目标摘要
  变化时拒绝并必须用 `--supersedes` 登记新结论；原始记录、授权快照与结论不被覆盖，
  `review-metrics` 分别统计有效/已 superseded/amendment。
- **G2 完整代码范围与真实宿主绑定**：`review-context --create-attempt` 同时冻结 git 工作区
  的代码范围（tracked/untracked/新增/删除/重命名）；G2、`review --attempt`、ingest 与 G4
  都重算代码快照，未登记源码变化按 fail-closed 拒绝。真实宿主 metadata 的
  `createdAt`/`prompt`/`workspaceRoot` 与 attempt 绑定，旧会话/旧工作区不能复用。

v6.3 基于 10 个真实任务的复核复盘（ad-strategy 7 轮复核、Honor 7 次 G4 尝试、review-perf
4 次正式 REVIEW、gromore / 腾讯 / journey 等）收敛“无效评审多、重复取证”的机制问题：补齐
真实只读命令白名单（`git rev-list` / `git cat-file` / `git ls-tree` / `paste` 等）与危险
长选项缩写拒绝，`&&` 与 `;`/`|` 同等串联只读命令；放宽围栏 JSON 解析的真实变体；finding
增加稳定 ID/根因/状态/去重；新增 `review-context`、`review-metrics` 与无效信封审计；
执行证据漂移进入收口提醒。**v6.5 起 Reviewer 无 Bash，上述命令白名单与 `preflight-commands`
已随旧机制一并退役；本节只作版本沿革。**
G1–G4、证据完整性、宿主记录与 fail-closed 语义未放宽；新增能力不扩大任何授权。
v5.x 及以前的 SOL 决策通道、轮次/预算状态机与远程 REVIEW 裁决已退役；历史文档保留但明确
标注为历史，不再作为当前规则。协作语义基线是 ChatGPT 自定义指令（唯一权威）与用户级
`AGENTS.md`；本 Skill 只维护轻量治理规则，不保存其副本、不维护版本映射，按规则变化做
增量同步。

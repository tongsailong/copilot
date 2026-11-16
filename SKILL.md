---
name: copilot
description: "Copilot v6 轻量协作治理：任务承接、合同与完成条件、状态与证据登记、PARTIAL 持续推进、原生只读复核与 G1–G4 收口。本地脚本不调用任何远程模型，不替代 ZCode 的技术决策与工程执行；SOL 决策通道已退役，历史台账只读保留。"
when_to_use: >
  需要把多步骤工程任务纳入可追踪治理时：任务合同/完成条件需要固定、证据与审计需要绑定、
  长任务需要 PARTIAL 持续推进与上下文恢复、高风险任务需要 G4 独立只读复核、用户要求正式
  REVIEW、或需要只读查看历史 SOL 调用与 REVIEW 台账。简单、单步、低风险任务直接由 ZCode
  原生完成，不进入本 Skill。
---

# copilot：轻量协作治理（v6.11.0）

Copilot 是运行在 ZCode 内的**协作治理层**，不是决策模型，也不是执行体。它负责把任务变成可
追踪的合同、状态、证据与收口条件，并在任务没有真正完成时继续推进。

- **用户**：业务目标、优先级、关键取舍与授权。
- **ChatGPT（用户侧）**：需求理解、产品与业务判断、目标定义、产品层方案、验收。
- **ZCode 原生 Agent（主代理）**：唯一的工程执行与技术决策主体——调查、规划、实现、调试、
  测试、验证，自主选择技术路线，不需要任何远程模型批准。**默认由主代理直接执行**，不为了
  体现协作而强制调用子代理。
- **内置子代理（按需）**：`Explore` 只做独立只读调查；`general-purpose` 只承担边界清晰、
  可独立交付且并行收益明显的工作。二者都由主代理拆分、控制范围并集成结果。
- **Copilot（本 Skill）**：任务承接、合同与完成条件、状态机、证据桥接、审计、PARTIAL 推进、
  收口门禁。**不做工程实施，不替代技术决策，不设置无意义的审批门。**
- **G4 只读复核**：高风险任务收口所需的独立复核，要求宿主强制 `Read/Grep/Glob`、禁用
  `Write/Edit/Bash` 的子代理在独立上下文完成。内置 `Explore` 含 Bash、`general-purpose`
  为全工具，**都不能充当 G4 执行者**；没有可证明的只读能力时保持 fail-closed，如实报告
  宿主能力缺口，不伪造 PASS。
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
- **正式 REVIEW 一次登记、后续修订**：结论只登记一次；证据编号/引用/文字口径的非实质
  修订用 `formal-review --amend <review_id> --reason ...` 追加 amendment，不生成新的 PASS。
  amendment 要求目标摘要未变；证据实质变化时拒绝 amendment，必须用
  `--supersedes <review_id>` 登记新结论并说明旧结论为何失效。原始记录、授权快照与结论不被
  覆盖。
- **G4 与正式 REVIEW 不同**：G4 是高风险任务收口所需的技术复核，要求原生只读 Reviewer 在
  隔离证据与独立上下文下给出 `g4_eligible=true` 的结论；formal review 是用户要求的治理记录。
  两者互不替代，任何模型判断都不等于 G4 证据。

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
上下文的代码检查、高风险任务的独立只读复核（仅作为调查，不是 G4）。

派发时必须提供：

- 调查目标与需要回答的问题；
- 文件范围或起始路径；
- **必要的项目背景与规则**——内置 `Explore` 的 `injectAgentsMd=false`，**不会自动注入
  AGENTS.md**，主代理必须把相关约束写进 prompt；
- 预期证据：真实文件路径、行号、命令输出或代码片段。

`Explore` 只调查、不修改工程文件；它的宿主工具集包含 `Bash`（可写/可执行），所以**不是
只读代理**，不得用于需要证明只读的 G4 复核。

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
  任务范围和原始证据；执行/复核信封由 `native_agent.py` 从宿主记录生成，不接受自报身份。
- **G4 例外**：高风险 G4 需要宿主证明的只读子代理；若当前环境没有可用的只读能力载体，
  G4 保持 fail-closed，任务按真实能力缺口报告，不把治理能力缺口误报成业务源码缺陷。

## PARTIAL 持续推进（硬约束）

PARTIAL 表示**仍有工作要做**，不是任务终点，也不是停止理由。

- `drive` 遇到 PARTIAL 时本地派生下一步工作项：未闭合条件 → `AUTO-C<n>`；停摆工作项 →
  `AUTO-R-<id>`；未闭合 finding → `AUTO-FIX-<F>`；已修复未验证 → `AUTO-VERIFY-<F>`；
  高风险缺 G4 → `AUTO-G4` / `AUTO-G4-INGEST`；无缺口时 → `AUTO-NEXT`。同一缺口已有
  `pending`/`running` 工作项时不再派生第二条，多个入口重复求值不会复制工作单。
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

### BLOCKED 下的受控写入

BLOCKED 表示**不能正常收口或继续受阻动作**，不表示无法记录真实进展。允许在阻塞期间登记：

- 已完成的执行结果（`record`）、修复后重验（`rerun`）；
- 已存在的真实证据（`evidence --add`）、完成条件核销（`criterion`）；
- 报告、判断、额外阻塞（`report` / `judgment` / `block`）；
- 用户已明确授权的正式 REVIEW 结论及后续 amendment（`authorize-review` / `formal-review`）。

这些登记**不会解除阻塞**，也不改变合同、授权或验收条件；`closure --close` 仍拒绝未解除的
阻塞，G1–G4 仍按原规则重算。可能扩大范围或具破坏性的操作继续拒绝：`plan`、`decide`、
`evidence --drop`。允许多个真实阻塞并存；存在多条未解除阻塞时，`unblock` 必须用
`--block-id` 逐条解除，不能用一条理由把不同阻塞一起清除。合同修订走单独的
`contract --amend`：BLOCKED 下允许，但会更新合同指纹并使旧正式 REVIEW 授权/G4 绑定失效，
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
  不改变四门，由 ZCode 判断补 note、归档、重新回填执行结果或保留。
- **收口交付清单（只读）**：`handoff --state ... [--json]` 汇总任务阶段、证据统计与工作区
  未提交/未跟踪文件的分类（治理记录、任务证据、验收产物、未分类）。它只读 git 状态，
  **不提交、不删除、不改写 `.gitignore`**；已提交内容与忽略项不在清单里，也不会被自动处理。
- **复核审计事件**：执行/复核信封回填写入 `native_result_ingested` / `native_review_ingested`
  历史事件；被拒信封（含生成阶段：输出漏绑定字段、实际命令未通过只读校验）追加到任务目录
  `review-attempts.jsonl`（只记错误码与绑定值，不记正文），供复盘统计“无效信封”而不必依赖
  终端输出。

## 四门与收口

收口 `closure --close` 必须 G1–G4 全部通过，否则失败关闭：

- **G1 工作项终态**：所有工作项为 `done` 或 `dropped`；`dropped` 必须写明放弃原因。
- **G2 合同与条件**：合同指纹与当前目标/正文/条件一致；完成条件已确认且无漂移；每条 `met`
  都有本任务已登记证据，且文件摘要重新核对一致。普通风险任务另有 Finding 台账检查：
  `open` / `regressed` / 已修复未验证（`fixed`）的 blocking/major 阻断收口；minor/info 不阻断。
- **G3 无停摆**：没有 `running` / `failed` / `blocked` 遗留，也没有依赖不可满足的待办。
- **G4 高风险复核**：`risk=high` 时必须存在绑定的原生独立复核：`verdict=pass`、无 blocking
  finding、`unverified` 为空、`changed_files` 为空、`g4_eligible=true` 且
  `g4_source="candidate"`（由新链 `g4_attest` + `test_execution` + `g4_shadow` 派生，
  不接受信封自述）、`candidate_g4.eligible=true`、绑定派发前创建的 `review_attempt_id`、
  目标摘要等于当前 `final_evidence_digest`、代码范围快照等于当前工作区。缺失、过期、
  代码/证据漂移、会话或工作区不匹配、宿主记录/原始输出被篡改、attempt 重放一律 fail-closed。
- **G2 的代码范围**：G2 除合同/条件/证据外，还检查最近一次已消费 attempt 的代码快照；未登记
  为 evidence 的源码、未跟踪文件、删除/重命名文件同样纳入范围，不能因“没登记证据”绕过。

普通 Reviewer PASS **不等于** G4；`g4_eligible=false` 的结论只能作为参考。

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
  声明范围不一致即拒绝；质量报告快照不排除已登记证据，G4 仅排除未跟踪证据产物。纯文档
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
  拒绝 PWW；FAIL/PARTIAL/BLOCKED 报告不会被接受机制提升。高风险任务在最终 G4 前完成接受
  登记（接受会改变条件状态与最终证据摘要）。


**`--risk high` 与 `--backend native` 是绑定关系**：复核信封只能通过原生派发链路产生，
所以高风险任务用 `legacy` 后端启动会在 `start` 阶段被拒绝，而不是留一个永远无法收口的任务。

## 原生执行与 G4 复核

`execution_backend=native` 时才生成原生派发：

- `drive` 生成 `copilot-native-dispatch/v2`（绑定 task / contract / plan / iteration），工作项
  带稳定 `dispatch_id` 与 `idempotency_key`。
- 执行方用 `native_agent.py execution` 生成 `copilot-native-result/v2`，绑定计划与执行方身份。
- 复核方用 ZCode 原生只读子代理派发：宿主 `profileSnapshot` 的有效工具策略必须是
  `tools=[Read,Grep,Glob]` 且禁用 `Write/Edit/Bash`、无 skills/MCP；Profile 名称、来源与
  路径不参与判定（`reviewer.profile`），无宿主策略时失败关闭。内置 `Explore` 含 Bash、
  `general-purpose` 为 `tools=["*"]`，均不满足；宿主也没有按派发覆盖工具的参数。
  派发前后可用 `native_agent.py capability --metadata <metadata.json>` 只读诊断实际权限与
  G4 资格（`g4.executor`/`precheck_only`/`codes`/`remedy`，并从宿主 `tool_usage` 汇总
  `host_evidence`：先确认会话存在，再按工具名/`read_only=0` 统计写能力与未分类调用），它
  不派发、不写状态；`g4.executor` 只是策略预检，不代表 G4 已通过。
  再用 `native_agent.py review` 生成 `copilot-native-review/v2`；`g4_eligible` 只是结构预检，
  正式资格由 `g4_attest` 读取宿主 DB（`tool_usage`/`part`/`session_entry`）独立核验
  有效权限策略、会话/prompt/工作区、工具调用与原始输出，再由 `test_execution` 与
  `g4_shadow`/候选闭环派生。`commands_run` 必须为空，`files_inspected` 由宿主工具记录合并。
- **G4 必须先创建 review attempt**：`review-context --create-attempt --workspace <任务仓库>`
  在派发前把 task / contract / plan / dispatch / work item / execution / 当前
  `final_evidence_digest`、条件摘要、文件证据内容摘要，以及 **git 工作区代码范围**
  （tracked/untracked/新增/删除/重命名，逐文件 SHA-256）固化为不可变 attempt；把返回的
  `review_attempt_id` 与 `code_snapshot.files` 写入 Reviewer prompt。`review --attempt <id>`
  要求宿主 metadata 的会话创建时间晚于 attempt、prompt 携带该 id、`workspaceRoot` 等于
  attempt 冻结的代码根，信封绑定同一目标；旧会话、旧工作区或旧输出不能换一个新 target
  重新封装。G4 代码快照只排除未跟踪证据产物；已跟踪源文件即使登记为证据也进入快照。
- 用 `native_agent.py review-context --state …
  --dispatch … --execution … --work-item … [--previous-review prev.json]
  [--create-attempt] [--workspace <任务仓库>] [--base <任务开始 commit>]` 生成绑定值、
  证据清单、`execution_evidence_unbound` 漂移、代码范围快照、上轮 findings 与建议 scope，
  一次交给 Reviewer。存在未核销条件、证据漂移或工作区不是 git 仓库时拒绝创建 attempt；
  默认快照覆盖工作区相对 HEAD 的变更，任务代码已提交时用 `--base` 纳入已提交范围。
- 首次完整复核 PASS 后又有新证据时，可用 `--based-on <基线复核.json>` 做增量复核：只覆盖
  基线之后新增/变更（含归档）的证据；条件、合同或其他工作项变化则拒绝增量、要求完整复核。
  基线必须是可用的 G4 PASS，所以增量用于高风险复核链路的修复后复验；普通风险任务按需做
  一次完整复核即可。
- `copilot_auto.py ingest` 校验信封后回填；正式 G4 由 shadow/candidate 决定，回填前
  `g4_attest` 独立核对宿主记录与 attempt、`test_execution` 核对测试证据，并在同一份状态
  写入中消费 attempt。被拒的 attempt 不消费，修正后可用同一 attempt 重试；已消费的
  attempt 不能重放另一份结果。字段与错误码见 `references/agent-bridge.md`。
- **高风险任务必须显式声明 `test_policy`**：`start`/`contract --amend` 与
  `bind-execution` 都会拒绝没有 captured 策略的高风险任务；合同策略进入合同指纹，
  `required=false` 才允许测试层 N/A，调用方只能加严，不能删除或放宽合同要求。

## 复核策略与收敛

复核是风险控制手段，不是开发流程的必经节点：

- **低风险（`risk=low`）**：README/文档/项目规则、文案与样式、普通 UI 布局、简单 bug 修复、
  普通测试代码、小范围既有模式改动。主代理自检（diff、定向测试、完成条件核对）闭环，
  默认不派 Reviewer。
- **中风险（`risk=medium`，历史 `normal` 等价）**：普通功能开发、多页面 UI、常规流程/
  性能优化、普通模块重构。主代理完成开发与验证；存在特定技术不确定性时可按需用内置子代理
  做针对性调查/咨询，但**不自动**创建 review attempt、G4 任务或复核台账。只有声明实质
  触发因素、且独立视角确实能显著降低风险时，才派发一次定向只读复核。
- **高风险（`risk=high`）**：权限/安全、计费/归因、迁移/一致性、共享基础设施/核心状态机、
  关键业务链路、生产发布。G1–G4 不变：主代理先自检与测试，再按剩余风险完成一次 G4，
  独立上下文、宿主精确只读约束、宿主记录与最终证据绑定，fail-closed。
- **正式 REVIEW**：仅用户明确要求时登记（见 `references/review.md`），不因风险等级推定授权。

### 三级复核策略（v6.10.0）

- **不做复杂评分**：只看任务风险标签与已声明的实质触发因素，不新建评分模型、决策 Agent
  或层层审批。`review-admission` 返回 `tier=low|medium|high` 与
  `required`/`not_required`/`not_needed`；低/中风险无触发因素时直接执行，不建立复核计划。
- **低风险不自动复核**；**中风险不强制完整复核**；**高风险按真实剩余风险做 G4**。风险标签
  与触发因素冲突时以显式触发因素为准（低风险声明 `security` 会升级为一次定向复核）。
- **避免过度复核**：不要求每个开发任务启动 Reviewer；不把主代理自检转成正式 REVIEW；
  不把普通子 Agent 调查转成 G4；没有新证据/源码变化不重复复核；不为凑复核轮次重复派发。
- **PASS 即停止**：首次有效复核覆盖约定风险、无未解决 blocking/major、证据满足要求、
  基线与目标一致时立即结束；局部修复只验证受影响部分，只有架构/关键契约/风险范围实质
  变化才重新完整复核。
- **针对性调查不是正式复核**：子代理咨询、`Explore` 调查不消耗复核 attempt，也不进入
  G4 台账。

### 工具请求不等于工具执行（v6.10.0）

- 宿主 `tool_usage` 记录区分 `completed`、`denied_not_executed`、`executed_failed` 与
  `execution_unknown`。只有错误类型明确为策略/权限/工具缺失
  （`permission_denied`/`policy_denied`/`tool_not_allowed`/`tool_not_found`）、诊断列
  （`duration_ms`/`output_bytes`）存在且为零、无退出码时，才认定为“拒绝且未执行”。
- **被拒绝且确认未执行的** `Bash`/`Write`/`Edit` 请求不使复核会话失效、不算 G4 工具违规、
  不消耗有效 attempt；Reviewer 继续用 `Read/Grep/Glob` 完成复核，一次拒绝不触发重新派发。
- **实际执行** `Bash`/`Write`/`Edit` 仍不符合当前 G4 工具策略；执行状态未知
  （`running`/`cancelled`/证据缺失）保持 fail-closed。同一越权工具被反复拒绝时记录
  `repeated_denied_calls`，Reviewer 应停止无效重试，必要时由主代理终止该会话并新建只读复核。
- **G4 权限不变**：仍是 `Read/Grep/Glob` + 禁用 `Write/Edit/Bash`；本次不建设新沙箱、
  不修改宿主安全策略、不扩大子代理写入权限。

### 风险准入与停止（v6.8.0）

- **先判断要不要复核**：`copilot_dispatch.py review-admission --state … [--trigger …]
  --reason …` 返回 `required` / `not_required` / `not_needed`，并给出
  `tier=low|medium|high`。`risk=high` 恒为 `required`（G4 原有门禁不变）；`low`/`medium`
  默认 `not_required`，只有声明实质触发因素（`security`、`permissions`、`core-logic`、
  `data-integrity`、`architecture`、`global-execution`、`production`、`release`）才建议
  派发。文案/格式/纯文档调整、仅注释改动、已被确定性测试覆盖的局部修改不派 Reviewer。
  高风险 G4 例外：G4 代码范围包含 tracked 文档，PASS 后文档变化同样使旧快照失效，需要
  重新 G4（这是代码范围绑定，不是文案复核）。
- **一次完整复核**：代码与证据稳定后才派发。`review-context` 输出 `review_admission`、
  `finding_ledger`、`execution_evidence_unbound` 与 `scope_reason`；存在未核销条件、
  证据漂移或台账未闭合 finding 时先解决，不要让 Reviewer 基于即将过期的快照工作。
- **停止标记**：PASS 回填时自动写入 `review_stops`（目标摘要、代码范围、findings 计数）。
  同一目标与代码范围的再次 attempt 直接拒绝（`review_not_needed`）；上轮 PASS 之后只有
  文档/格式文件变化时返回 `review_not_needed_docs_only`。不为 minor/info 建议改代码再重开
  完整复核；把 minor 记录为 findings、把已核实非缺陷风险记录为 `residual_risks`。
- **重入必须有实质原因**：代码/目标/证据变化后再次完整复核，需要 `--trigger`
  （`blocking-fix` / `major-fix` / `code-changed` / `scope-expanded` / `evidence-changed` /
  `risk-changed` / `security-fix` / `concurrency-fix`）与 `--reason`，写入
  `review_admissions`。增量复核（`--based-on`，代码范围未变）不需要重入触发，但基线与覆盖
  仍按原规则 fail-closed。
- **派发幂等**：同一工作项、执行结果、目标与代码范围的未消费 attempt 直接复用；重复调用
  `review-context --create-attempt` 不会生成第二个派发快照。派发失败/超时/上下文恢复时，
  attempt 未被消费即可继续使用；已消费的 attempt 不能重放另一份输出。

### Finding 台账与定向复验

- 每条实质 finding 进入 `finding_ledger`，使用稳定 `F1..Fn`；同一根因换一种表述仍映射到
  同一 ID（显式 `id` / `root_cause` / 文件+摘要相似度），已关闭问题没有新证据只记为
  重复报告，不重复登记为新问题。
- 生命周期 `open → fixed → verified → closed`；`fixed` 只表示已改，`verified` 才表示修复
  被定向验证。`copilot_dispatch.py finding --state … --list` 查看；
  `finding --state … --id F1 --status fixed --evidence E1` 登记修复；
  `--status verified --evidence E2` 登记定向测试/检查结果。`fixed`/`verified` 必须有证据，
  `closed` 只能从 `verified` 进入。
- 未闭合 blocking/major 阻断 G4（高风险）与 G2（普通风险）；minor/info 不阻断、不派生
  复核。已修复未验证的 finding 派生 `AUTO-VERIFY-<F>`：只跑对应定向测试并登记证据，**不**
  重新派发完整复核；仍复现时标记 `regressed` 继续修复。
- **不重报、不重复复核**：Reviewer 复用 finding ID 与状态；已关闭问题无新证据不得重报。
  只有变更范围实质扩大、原结论依赖的代码变化、跨关键路径的新问题、风险判断失效或高风险
  修复改变整体结论时，才按上面的重入触发重新完整复核。Reviewer PASS 后立即停止，不因还能
  提出建议而重开任务；没有新增信息的复核由 `review-metrics` 统计为
  `no_information_reviews`。

收敛顺序：**充分自检 → 稳定最终执行结果与证据 → 首次全面复核（一次列出全部重要问题与
severity）→ 集中修复 → 针对性复核（只验证修复、新问题、受影响区域与完成条件）→ 收口**。
最终 G4 复核必须在完成条件已核销、证据已登记且摘要稳定之后进行；派发前读取
`review-context` 的 `warnings` / `scope_reason`，存在条件未闭合或证据漂移时先解决原因。
目标 1–2 轮收敛，这是性能目标不是硬上限；超过两轮先诊断非收敛原因（首次遗漏 / 修复不彻底
/ 新增缺陷 / 复核机制开销），不要用重复整轮复核掩盖。不得为减少轮次而漏报或降低标准。

首轮与后续轮次的边界：

- **首轮一次看全**：枚举变更面、读取受影响调用方与测试、按根因归组、检查同类缺陷的其他位置，
  一次给出全部可独立确认的问题；finding 用稳定 ID（`F1..Fn`）与可选 `root_cause`/
  `category`/`related`，不把一个根因拆成多轮。
- **后续只验证变化**：复用 finding ID（`open`/`resolved`/`regressed`）、只复核修复与受影响
  区域、不重复已有效检查；已关闭问题无新证据不得重报。
- **PASS 即停止**：合同与条件满足、无 blocking、`unverified` 为空即停，不为 minor/info 建议
  重开；把 minor 记录为 findings、把已核实非缺陷风险记录为 `residual_risks`，不要为了让
  finding 归零而修改已通过的证据再重开完整复核。若后续发生实质代码/证据/合同变化，原 PASS
  不再覆盖新快照，必须按新范围重新完整或增量复核。
- **每轮新会话**：不要续跑同一 Reviewer 会话（宿主会重置 `totalToolUseCount`，导致
  `review_toolcalls_incomplete`）；每次复核都派发新的只读子代理。
- **效率指标**：`copilot_dispatch.py review-metrics --state …` 输出有效复核轮次、首轮 finding
  数、重复 finding 比例、无效信封计数、重复命令/文件、每轮证据包字符数与首轮到 PASS 耗时；
  正式 REVIEW、原生复核与 G4 分别计数。

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
  与可用类型，不要原样重试；`profile_tools_not_exact`（权限不满足）→ 该 Profile 含
  Bash/Write/MCP 或工具集不精确，不能作为 G4 执行者；`review_toolcalls_incomplete` 等宿主
  记录问题 → 新建复核会话/attempt，不复用旧输出。普通任务在这些失败后由主代理继续。
- 上下文中断时保留：原始目标与完成条件、当前阶段、已完成工作、未解决问题、证据与验证结果、
  明确的下一步；恢复后从真实现场继续，不把历史计划当作当前事实。
- 不重复已证无效的做法；同一问题不重复检索；没有新证据时不重复评审。

## 模块路由（按需读取）

**读取 references 前先确认需要哪一节；不要预读全部。**

| 场景 | 必读文件 |
|---|---|
| 命令矩阵、证据登记、收口操作、只读交付清单、历史只读 | `references/workflow.md` |
| 阶段机、`drive`/`ingest`、PARTIAL 派生、退出码 | `references/runtime.md` |
| 正式 REVIEW、缺陷分级、G4 收口细则 | `references/review.md` |
| 原生 Bridge v2 信封、宿主记录、错误码 | `references/agent-bridge.md` |
| Agent 调度、内置 Explore/general-purpose 能力与 Profile 发现、G4 能力诊断 | `references/agent-bridge.md` 第 1.1 节；`native_agent.py capability`；`copilot_dispatch.py agent-policy` |
| 历史 REVIEW 台账与旧审计（只读） | `references/review-ledger.md` |
| 版本沿革与 SOL 退役记录 | `references/history.md` |
| 角色与历史 SOL 指令（只作历史） | `references/role.md`、`role-consult.md` / `role-plan.md` / `role-review.md` / `role-map.md` |

## 版本

当前版本 **v6.11.0（ZCode Autonomous Development Edition）**。v6.11.0 是效率与性能专项，
只减少重复开销，不改判定规则：

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
标注为历史，不再作为当前规则。协作语义基线为用户级 `AGENTS.md` 与 ChatGPT 自定义指令
v5.4.0；本 Skill 不与基线冲突。

---
name: copilot
description: "Copilot v9.6 轻量协作治理：任务承接、合同与完成条件、状态与证据登记、PARTIAL 强制续跑与调度领取、SOL（gpt-6.2-sol，high，零工具）默认认知引擎 CONSULT / PLAN 与生命周期主动调度（义务化、有界自动执行、覆盖率统计）。PARTIAL 是连续执行状态而不是终点：只要仍有原始授权内的可执行工作，就必须派生、领取并继续；阻塞按工作项成立，单项缺授权不冻结整个任务。drive 把工作项领取落盘（短租约）避免并发重复，回填/阻塞自动释放；SessionStart 与 UserPromptSubmit 只读提示未闭合任务，用户补齐授权后立即恢复执行。SOL-first 生命周期：简单/机械任务 0 调用；非简单任务启动默认 CONSULT，调查后按需 PLAN（不依赖工作项数量），执行期按六类可归因事件追加 CONSULT，medium/high 任务可做阶段复盘，有实质进展的非简单任务在最终工程验证完成后、收口前做综合判断；生命周期命令把“应调用”物化为调度义务并可直接执行（每条命令最多 1 次，`--sol auto|advise|off`），`sol-stats` 以显式分母统计覆盖率。REVIEW 仅在用户明确要求时执行。SOL 只提供认知输入，ZCode 是唯一技术决策与工程执行主体；G4 原生只读复核已退役，历史台账只读保留。"
when_to_use: >
  需要把多步骤工程任务纳入可追踪治理时：任务合同/完成条件需要固定、证据与审计需要绑定、
  长任务需要 PARTIAL 持续推进与上下文恢复、非简单任务需要 SOL 默认认知（启动 CONSULT、
  调查后 PLAN、执行期动态再咨询、阶段复盘与收口判断）、用户明确要求 SOL 判断或独立复核
  （REVIEW）、或需要只读查看历史 SOL 调用、G4 记录与 REVIEW 台账。简单、单步、低风险任务
  直接由 ZCode 原生完成，不进入本 Skill。
---

# copilot：轻量协作治理（v9.6.2）

Copilot 是运行在 ZCode 内的**协作治理层**，不是决策模型，也不是执行体。它负责把任务变成可
追踪的合同、状态、证据与收口条件，并在任务没有真正完成时继续推进。
任务必须通过 `start` / `drive` 进入状态机；没有状态的自由文本"阶段报告"不构成本 Skill 的任务。

- **用户**：业务目标、优先级、关键取舍与授权。
- **ChatGPT（用户侧）**：唯一权威治理基线——需求理解、产品与业务判断、目标定义、产品层
  方案、角色分工、授权原则与最终验收；原文只在 ChatGPT 设置维护，本 Skill 不保存其副本、
  不维护版本映射或对齐表。
- **ZCode 原生 Agent（主代理）**：唯一的工程执行与技术决策主体——调查、规划、实现、调试、
  测试、验证，自主选择技术路线，不需要任何远程模型批准。**默认由主代理直接执行**，不为了
  体现协作而强制调用子代理。
- **内置子代理（按需）**：`Explore` 只做独立只读调查；`general-purpose` 只承担边界清晰、
  可独立交付且并行收益明显的工作。二者都由主代理拆分、控制范围并集成结果。
- **SOL（默认认知引擎与用户要求的独立复核）**：经既有本机 CPA `127` 通道调用
  `gpt-6.2-sol`（`reasoning.effort=high`，`standard` 模式），**零工具、单轮**地阅读主代理
  一次性提交的精炼材料。**CONSULT** 是默认认知入口：理解目标与隐含需求、判断问题本质与
  关键矛盾、比较路线与取舍、提示风险与遗漏、给出调查方向与再咨询条件；**PLAN** 在调查
  之后给出方案比较、优先级与阶段目标；**REVIEW** 只在用户明确要求时输出结构化 findings。
  三者都是 advisory，不执行工程、不做技术决策、不读写台账、不调度子代理，也不做机械式
  逐行审查。THINK 保留为只读别名（v8.4–v8.5 旧名称），读取时归一为 CONSULT。
- **Copilot（本 Skill）**：任务承接、合同与完成条件、状态机、证据桥接、审计、PARTIAL 推进、
  收口门禁。**不做工程实施，不替代技术决策，不设置无意义的审批门。**
- ~~**G4 只读复核**~~：**已正式退役**（v8.0.0）。G4 不再作为活动质量门禁，不再要求宿主
  只读 Agent、Profile 来源检查、权限快照准入或独立取证。历史 G4 记录只读保留，用于审计与
  追溯，不参与新任务的准入、派生或收口；不得把历史 G4 BLOCKED 改写成 PASS。
- **DesignWise**：设计方法与设计质量规范；具体设计执行仍由 ZCode 完成，Copilot 不另建流程。

**原生 Reviewer 与 G4 复核均已退役。** v8 起 SOL 是唯一远程通道：
`copilot_dispatch.py sol-consult` / `sol-plan` 是默认认知入口（按生命周期与任务性质建议），
`copilot_dispatch.py sol-review` 只在用户明确要求时做一次独立复核。SOL 结论是
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
4. 非简单任务（未声明类型或多步骤、多方案、跨模块、重要决策）需要默认 SOL 认知：
   启动阶段 CONSULT 一次，调查后按需 PLAN（`review-admission` 给出复杂度与调用建议）。
5. 复杂问题存在多条技术路线、跨模块调用链或疑难故障，需要一次独立认知视角。
6. 重大新证据（原方案失败、风险升级、任务重开）出现，需要一次定向再咨询。
7. 用户明确要求 SOL 判断，或明确要求正式独立复核（REVIEW）。
8. 用户要求只读查看历史 SOL 调用、REVIEW 台账或旧任务状态。

不适用：单步查询、单文件小改、纯问答、简单命令确认——直接由 ZCode 完成，不建立任务状态。

## 核心循环

```
start（合同 + 完成条件 + 风险 + 任务类型 + 后端）
  → [非简单任务，启动] sol-consult（understanding：目标理解/问题/风险/调查方向）
  → plan（ZCode 本地计划）或 PARTIAL 自动派生工作项
  → [调查后，按需] sol-plan（方案比较/优先级/阶段目标）
  → drive（本地求值：PASS 收口 / BLOCKED 停下 / 打印工作单）
  → ZCode 执行（主代理或原生子代理）
  → ingest / record / criterion / evidence（回填真实结果与原始证据）
  → [新证据/新风险/方案失败] sol-consult 动态追加；主代理自主采纳/调整/否决
  → [中高复杂，阶段边界] sol-consult --stage stage（成果复盘，recommended 自动执行）
  → [最终工程验证完成后、收口前] sol-consult --stage closure（综合判断）→ ZCode 最终验收 → 收口
  → [仅用户明确要求] sol-review 一次（REVIEW 独立复核）
```

`sol-consult` / `sol-plan` / `sol-review` 是显式模型调用入口；其余生命周期命令
（`start` / `plan` / `record` / `criterion` / `closure` …）和 `drive` 在默认 `--sol auto` 下
会顺带执行**至多 1 条** recommended 调度义务（1 次请求，失败只记录、不阻断）——所以
“不调用模型”只对 `--sol off|advise`、读命令和测试进程成立。所有命令其余工作只读写任务
状态与本地证据/派发文件；其中 `evidence --capture-cmd` 会执行调用方给出的本地命令，
`handoff` 会做只读 Git 查询。每次 SOL 调用只发一次请求，不自动重试、不自动二审；
CONSULT / PLAN 失败只记录并降级，不阻断工程推进。完整命令矩阵见 `references/workflow.md`；
阶段机与驱动语义见 `references/runtime.md`。

## 模式

CONSULT / PLAN / REVIEW 都是 **SOL 调用**；工作项计划由 ZCode 本地决定，不经过 SOL。
没有审批节点，SOL 调用也不是决策入口。

| 模式 | 作用 | 是否阻塞任务 | 入口 |
|---|---|---|---|
| 本地分析 | 主代理自行比较方案、识别风险；需要留痕时写 `judgment` | 否 | 主代理直接分析 |
| 工作项计划 | ZCode 的技术计划登记为工作项与依赖，供驱动与派发使用 | 否 | `plan` |
| 正式复核 | 正式复核结论登记；**仅在用户明确授权时进行** | 否（不是普通开发前置门） | `authorize-review` → `formal-review` |
| SOL CONSULT | 默认认知入口：目标理解、问题本质、路线取舍、风险、调查方向、再咨询条件 | 否 | `sol-consult` |
| SOL PLAN | 调查后的方案比较、优先级、阶段目标与验收关注点 | 否 | `sol-plan` |
| SOL REVIEW | 用户明确要求的一次独立审查；意见进入 Finding 台账供主代理核实 | 否 | `sol-review` |

- **计划由 ZCode 决定**：计划内容、拆分粒度、依赖与执行顺序都是 ZCode 的技术决策；Copilot
  只负责记录与校验（例如拒绝依赖环），不批准也不否决，也不交给 SOL 生成工程步骤。
- **SOL 只提供认知输入**：CONSULT / PLAN / REVIEW 都不接管工程执行、技术选型、收口判断或
  写操作；材料由主代理一次性整理，结论由主代理核实后决定采纳、调整或否决，并用
  `sol-accept --call-id ... --status accepted|modified|rejected` 登记事实（v9.1 起可附
  `--decision` / `--rationale` / `--delta` / `--evidence-ref E1,E3` / `--source-ref`）。
  不恢复 DECIDE，不设硬性调用次数上限，也不因等待 SOL 停止已有工作。
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
- **历史决策/约束**——用 `knowledge_handoff.py build --target subagent` 生成交接块并放入
  prompt；路径不能替代正文，子 Agent 不会自动读 Memory/Vault；
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

## PARTIAL 强制续跑（硬约束）

PARTIAL 表示**仍有未完成但可继续的工作**，是连续执行状态，不是任务终点，也不是停止理由。
PASS 才表示目标达成、证据充分、可以结束；BLOCKED 只在剩余工作**真的**缺少授权、用户决策、
必要资源或受外部限制时成立。

### 续跑判定

每次求值都会给出 `continuation`（`phase` / `required` / `action` / `executable` /
`derivable` / `unattributed_criteria` / `blocked_items` / `dependency_blocked` /
`next_command` / `reason`）：

| phase / 局面 | action | required | 含义 |
|---|---|---|---|
| 有任何可领取工作项 | `execute` | true | 输出工作单继续执行 |
| 无工作项但可派生 | `derive` | true | 先派生再执行，不得结束回合 |
| PASS（门禁已过） | `close` | true | 收口是下一步，不是等待用户 |
| CLOSED | `none` | false | 已收口 |
| 所有剩余工作真实阻塞 | `wait` | false | 退出码 11，等待外部条件 |

必须在下述事件后重新判定并继续：工作项完成/失败、发现新问题、测试失败、进入 PARTIAL、
产出阶段报告、上下文恢复、某项被标记 BLOCKED、子代理返回阶段结果。

- **报告是进度记录，不是终点**：`report` / `record` / `rerun` / `criterion` / `block` /
  `unblock` 结束时都会输出续跑判定；仍有可执行工作时返回 `EXIT_CONTINUE=10`，不得据此
  结束回合。`drive` 只在 `continuation.required=false` 时才以等待（11）退出。
- **工作项派生**：未闭合条件 → `AUTO-C<n>`；停摆/失败工作项 → `AUTO-R-<id>`；未闭合
  finding → `AUTO-FIX-<F>`；已修复未验证 → `AUTO-VERIFY-<F>`；无缺口时 → `AUTO-NEXT`。
  G4 退役后不再派生 `AUTO-G4*`。同一缺口已有 `pending`/`running` 工作项时不再派生第二条。
- **无进展保护**：同一缺口重复尝试仍不闭合时派生 `AUTO-REPLAN`，要求分析根因并选择**实质
  不同的替代路线**；替代路线仍在执行时不再派生同路径的下一次尝试，替代路线已结束而缺口
  仍未闭合时派生新的 `AUTO-REPLAN`（说明上一路线为何未闭合），不留给通用 `AUTO-NEXT`
  静默续跑。`progress_digest` 没有变化时应调整方案而不是机械重复。
- **授权按动作判断**：缺 commit 授权只阻塞 commit，缺 push 授权只阻塞 push，缺部署/生产
  数据/付费授权只阻塞对应动作；代码调查、已授权修改、测试与修复继续。不得把单项授权缺口
  扩大成整任务 BLOCKED。用户补充授权后用 `unblock` 精确解除对应阻塞并立即恢复该项。
- **禁止**因下列原因停止或宣布 BLOCKED：人为轮次上限、Token 预算、内部费用阈值、等待
  远程模型、重复评审、内部状态流转、子 Agent 返回 PARTIAL、首次方案失败、普通测试失败、
  可自行补齐的证据不足。
- 只有确实缺少授权、用户决策、必要资源或存在真实外部限制（服务商限流/配额、网络、安全
  边界）时才用 `block`，并写明原因、所需条件与责任方；一项阻塞不影响其它可执行工作。

### Stop 续跑守卫与恢复注入（机械衔接层）

`EXIT_CONTINUE=10` 只是命令退出码，模型仍可能写完报告就结束回合。`scripts/copilot_stop_guard.py`
把本地续跑判定接到 ZCode 的 `Stop` 钩子上：Stop 时读取工作区 `.copilot-tasks/*/state.json`，
只要还有 `continuation.required=true` 的任务，就以退出码 2 让宿主继续当前回合（宿主单回合最多
连续续跑 3 次，且原因文本必须非空）。它只读状态、不调用模型、不改任务状态，CLOSED / 真实
BLOCKED / 所有剩余工作均被项级阻塞时一律放行。

**状态文件位置是硬条件**：任务必须建在 `<workspace>/.copilot-tasks/<task>/state.json`。
三个钩子只扫描该目录；放在工作区根目录或别处时 CLI 仍能手工推进，但不会被自动续跑、
恢复或回合提醒发现（`start` 会提示）。这不是路径偏好，而是 Hook 能看见任务的唯一入口。

**归属绑定（取代时间过期）**：守卫不按“多久没更新”放行。Copilot CLI 在处理任务的命令里把当前
`ZCODE_SESSION_ID` 记到任务目录 `session.json`；守卫只拦截“本会话真正推进过”的任务，
其它会话、其它工作区、以及从未绑定过当前会话的旧任务都不参与判定。这样既不会因为旧任务
永久误拦工作区，也不会因为时间上限放弃仍有可执行工作的任务。要继续一个旧任务，先在本会话
运行一次会改写状态的命令（如 `drive`），绑定即建立。

安装（默认工作区级；若本机 `Stop` 钩子未获 `workspaceHookAdmission` 则不触发，此时装用户级）。
同一条安装命令同时写入 `Stop`、`SessionStart`（`startup|resume|compact`）与
`UserPromptSubmit` 三个钩子：

```bash
python3 -B ~/.zcode/skills/copilot/scripts/copilot_stop_guard.py \
  install --workspace <repo> --apply      # 写入 <repo>/.zcode/config.json，原文件有备份
python3 -B ~/.zcode/skills/copilot/scripts/copilot_stop_guard.py \
  install --user --apply                  # 写入 ~/.zcode/cli/config.json（全部工作区生效）
python3 -B ~/.zcode/skills/copilot/scripts/copilot_stop_guard.py status --workspace <repo>
python3 -B ~/.zcode/skills/copilot/scripts/copilot_stop_guard.py \
  uninstall (--workspace <repo> | --user) --apply
```

`--user` 会改写用户级 `config.json`（同文件还含 mcp/plugins），安装前自动备份，且只追加/移除
这两条守卫命令，不动其它配置；重复安装会把多条/过期记录收敛为一条，不产生重复
Hook。`ZCODE_USER_CONFIG` 可覆盖目标路径（测试用）。新会话生效（配置在会话启动时加载）。

**恢复注入（`SessionStart`，`startup|resume|compact`）**：`resume`/`compact` 且任务绑定本会话
时，钩子注入只读快照——任务、当前阶段与原因、未闭合条件、下一条命令与可粘贴的续接命令，
并提示先核实现场、复用已完成工作与已登记证据。`startup`（全新会话）或会话尚未绑定任务时，
只注入“存在未闭合任务”的只读指针（阶段、阻塞原因/所需条件、归属会话），不注入任务正文、
不自动绑定、不强制续跑；由当前会话 `status --json` 核实后自行决定是否 `drive` 接管。
`clear` 与普通用户消息不注入。所有路径都不写状态、不消费 Stop 次数；任何异常一律静默
退出 0，不阻断会话启动。`resume`/`compact` 还会重放本会话已召回的前 2 条历史结论
（`<copilot-recall-state>`），恢复后直接复用，不重新检索。

**自动召回（`UserPromptSubmit`，工程任务首轮）**：与任务指针同一次注入，零模型调用、
确定性分类。闲聊、短指令、纯问答、斜杠命令不触发；需要工程信号且在 Git 仓库内才检索当前
项目 Memory 与 Vault 正式区。注入上限 Memory 3 条 + Vault 2 条、总长 1,400 字符，带
`path:line`、核验时间、`source_commit` 与片段；截断/部分失败显式标注。同会话同项目按强词
哈希去重，换主题才增量召回（单会话最多 4 次、冷却 60 s）；失败/零命中只写事件、不阻断
回合。状态在 `~/.zcode/copilot-runs/context-recall/`，不保存原始 prompt 或疑似凭据。

**收口后 Capture 提示（`Stop`）**：本会话绑定任务进入 `CLOSED` 或真实 `BLOCKED` 且当前
终止版本无回执时，守卫以退出码 2 要求一次 Capture 评估——只要求判断与落盘，不继续开发、
不改任务状态、不伪造 PASS。评估后用
`copilot_stop_guard.py capture-ack --task-dir <任务目录> --status done|skipped|failed
--reason "<一句话>"` 写 `.capture.json`；同一 `closed_at`/`updated` 版本只提示一次。
绑定前已终止的旧任务不提示。候选写入沿用 knowledge-manager 授权，正式卡仍需用户授权。


**放行路径**：`COPILOT_STOP_GUARD=0`、工作区 `.copilot-tasks/.stop-guard-off`、任务目录
`.stop-guard-off`、任务 state 的 `stop_guard=false`，或 `disable --state <state> --reason "<原因>"`
（用户明确要求停止/紧急退出时由主代理执行，`enable` 恢复）。用户的 ESC 中断本身会中止钩子
进程，宿主不会续跑。

**异常一律 fail-closed**：state 损坏、绑定文件损坏、任务目录不可读、钩子输入非法时不静默放行，
也不伪造 PASS，而是以退出码 2 给出可执行的修复命令（修复该文件，或对已废弃任务执行
`disable --state ...`）。守卫是兜底，不替代主代理执行工作单。

### 单回合续跑上限与跨回合交接

ZCode 每回合最多接受 **3 次** Stop 自动续跑（发行版硬编码 `xmc=3`，无配置项，钩子也拿不到
已用/剩余次数）；第 4 次及以后的退出码 2 会被宿主忽略。守卫把这一事实写进每次注入的续跑
理由，并且**不推测剩余次数**、不断言"这是最后一次"。据此执行：

- **一次拦截 = 一个最小可验证工作单元**：优先完成一个工作项或一条完成条件（实施 + 定向
  验证 + 证据登记）。不用只读核对、重复计划或长篇说明代替实际进展。
- **结束续跑前必须落盘**：`record`/`report` 写结论，`evidence --add` 登记证据，未完成项
  保留 `pending`/`blocked` 并写明原因；守卫给出的续接命令原样可粘贴。
- **不得改状态规避上限**：禁止为结束回合把 PARTIAL 改写成 BLOCKED 或 PASS。BLOCKED 只在
  真实外部阻塞（每条有 `reason`/`needs`）时登记，PASS 只由 G1–G3 全部通过的收口产生；
  改状态规避等同于伪造结论。
- **续接不新建任务**：用满 3 次仍未完成时，同一 `--state` 就是续接点；用户在新回合粘贴
  守卫给出的 `python3 -B <runtime>/scripts/copilot_auto.py drive --state <绝对路径>` 继续。
- 守卫**不增加持久化计数器**，仍只读状态与绑定文件；3 次是宿主行为，不是 Copilot 的预算、
  轮次上限或停止理由。

### Goal 模式（可选，仅用户发起）

原生 `/goal`（headless `--target`）让宿主跨回合持续同一目标，可超出单回合 3 次续跑上限。
它**不是默认机制**：普通任务继续用 `drive` + Stop 守卫；只有用户明确要求时才使用。

**适用**：多阶段、预计远超 3 次续跑的复杂工作；目标清晰可验证（有客观完成标志）；
权限客户端已确认可用。**不适用**：单回合能完成的任务、目标本身待澄清、环境权限未就绪。

- **只能由用户发起**：Copilot 与主代理不得自行 `/goal` / `--target`，也不得把既有任务
  偷偷改造成 goal 目标。
- **Goal 与 Plan 互斥**：宿主不允许两者同时启用；不要为了启用 goal 强制切换会话模式。
- **objective 对齐合同目标**：objective 必须与 Copilot 合同目标一致，不得扩大、缩小或改写
  完成条件；目标变化走 `contract --amend` 并重新确认，而不是只改 objective。
- **验收仍以 Copilot 为准**：goal 完成校验通过后，主代理必须核对 Copilot 最终状态与 G1–G3；
  goal 自报完成、校验器结论或 `target` 状态都不能当作工程 PASS，也不能据此 `closure --close`。
- **不用 yolo 绕过权限**：缺少权限客户端时如实报告并停下，不得用 `--mode yolo` 或等价配置
  绕过审批；不调用未公开的内部预算/目标接口。
- **异常先暂停**：连续多轮无进展、token 消耗异常增长、权限错误反复出现时先 `/goal pause`
  并调查根因，不继续放大消耗。
- 不引入常驻后台服务，不递归启动 ZCode 进程，不修改宿主安装包与运行时。

### 条件归属（block 的第二个作用）

阻塞一个工作项时，`--criterion` 声明它承载哪些完成条件；派生工作项 `AUTO-C<n>` 天然承载
`C<n>`，计划期也可以在工作项上写 `target_criteria: ["C1"]`，`block` 会自动归属。若被阻塞项
既无显式 `--criterion` 也无自动归属，未闭合条件会一直悬空并每轮派生一条执行不了的
`AUTO-C<n>`，任务永远到不了真实 BLOCKED：此时求值会把条件列进 `unattributed_criteria`，
`continuation.reason` 与 `block` 输出都会给出补齐归属的具体命令。

## 状态机

| 阶段 | 含义 | 驱动行为 |
|---|---|---|
| RUNNING | 存在可领取工作项 | 输出工作单，退出码 10 |
| PARTIAL | 条件未闭合 / 停摆 / 门禁缺口，但仍有可执行或可派生工作 | 本地派生后继续 |
| PASS | 完成条件与 G1–G3 全部通过 | 收口为 CLOSED |
| BLOCKED | 所有剩余工作真实阻塞（任务级显式阻塞，或项级阻塞覆盖全部剩余工作） | 退出码 11，等待外部条件 |
| CLOSED | 已收口 | 退出码 0；继续需用户授权 `reopen` |

**阻塞作用范围**：`block --work-item A2 [--criterion C2]` 只冻结 A2（及其下游依赖），其余
工作项照常可领取；`block --scope task` 才冻结整个任务，仅在目标/资源整体不可推进时使用。
任务只有在"全部剩余工作项直接阻塞或因依赖无法开始，且没有被卡住的未闭合条件"时才进入
BLOCKED；还有可推进的条件时先派生 `AUTO-C<n>`。

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
`--block-id` 逐条解除，不能用一条理由把不同阻塞一起清除；被项级阻塞冻结的工作项在解除后
自动回到 `pending`，立即重新进入可执行（`continuation.action=execute`）。合同修订走单独的
`contract --amend`：BLOCKED 下允许，但会更新合同指纹并使旧正式 REVIEW 授权失效，
不属于上述普通写入白名单。

## 证据与审计

- **先登记，后引用**：`evidence --add` 登记文件/内联文本/命令采集，记录 SHA-256、大小、
  路径与任务归属；`record` / `criterion` 只能引用本任务已登记且未归档的证据。
- **Recall 证据**：`Recall: hit` 把同轮实际 Read 的卡路径登记为文件证据；`zero-hit` 把
  有界检索命令/输出登记为 `--capture-cmd`；`skipped` 在 report/note 写明原因。回执文字
  本身不是召回证据。
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
- **SOL 调用记录（单一权威流）**：`sol-consult`、`sol-plan` 与 `sol-review` 的成功与失败
  记录统一写入状态 `sol_calls`（字段含 `call_id` / `mode` / `stage` / `question` /
  `prompt_version` / `evidence_digest` / `materials_sha256` / `progress_digest` /
  `complexity_signals` / usage / 延迟 / `acceptance`）；原始输出存入
  `sol-consult/<call_id>.raw.txt`、`sol-plans/<call_id>.raw.txt` 或
  `sol-reviews/<call_id>.raw.txt`；审计统一写 `sol-audit.jsonl`（三模式共用，只记脱敏元数据：
  材料/请求/回复摘要、模型、推理强度回显、请求/追踪 ID、usage、耗时、错误码），不记正文与
  凭据。状态 `calls` 计数是台账记录，不等价 HTTP 请求数；`request_attempted` 表示进入 transport，
  `response_received` 表示收到 HTTP 响应，异常时 `send_observed=unknown`，不推断字节已出站；
  历史缺失字段明确单列。三模式共用同一把 `.sol.lock`。采纳结论由 `sol-accept` 回填，属于主代理的事实判断，
  不是用户授权，也不改变门禁；记录含 `decision`/`rationale`/`delta`/`evidence_resolution` /
  `advice_ref`，可选 `work_item_refs` 与解析状态仅供交叉追踪，不表示因果或完成证明，不作为门禁；
  缺项只给非阻断提示。
  历史任务状态里的 `sol_reviews` / `sol_advisories` 继续
  **只读兼容**：`--list`、`review-metrics`、`review-admission` 与单轮复用都会合并读取，
  历史 CONSULT / PLAN / THINK 记录按 CONSULT 计入覆盖，不迁移、不改写、不重复计数。
- **SOL 与门禁共用上下文**：提示词自动注入任务目标、完整合同正文、完成条件、工作项状态与
  证据登记摘要（ID/摘要/大小/支持的条件/归档标记），主代理只需提供本次增量的材料与问题。
  单轮复用键包含提示词版本、模型与推理强度、目标、合同正文与边界、条件描述、证据集合摘要
  与材料；合同、证据或提示词版本变化即失效，不会复用过期判断。

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
- **认知与独立复核都走 SOL**：`copilot_dispatch.py sol-consult` / `sol-plan`（默认认知）与
  `sol-review`（REVIEW，用户明确要求）都是零工具、单轮、advisory；不再有 review attempt、
  宿主只读能力探测或 `copilot-native-review/*` 信封。CONSULT 提交目标、现场事实、已尝试方案
  与本次问题；PLAN 提交调查形成的候选方案与约束；REVIEW 提交完整 diff、关键源码、数据与
  权限逻辑、测试结果、已知问题与高风险区域，一次提交。
- **历史兼容**：v7 及更早的状态里可能保留 `native_reviews` / `review_attempts` /
  `g4_attestations`。它们只读保留用于审计，不再被任何门禁、派生或 ingest 逻辑消费；
  不得手工改写历史记录，也不得把历史 G4 BLOCKED 当成当前任务状态。
- **高风险任务必须显式声明 `test_policy`**：`start`/`contract --amend` 与
  `bind-execution` 都会拒绝没有 captured 策略的高风险任务；合同策略进入合同指纹，
  `required=false` 才允许测试层 N/A，调用方只能加严，不能删除或放宽合同要求。

## SOL 认知策略（v9.0）

**SOL 是默认认知引擎，但不是按阶段机械运行的流程。** ZCode 主代理是唯一工程执行与技术决策
主体：SOL 给出目标理解、问题判断、路线比较、取舍、风险与优先级，主代理自主采纳、调整或
否决，并按现场事实实施与验证。SOL 不调查、不读仓库、不跑测试、不写代码、不维护台账。

### 任务类型与路由

`start --task-kind <kind>` 声明任务性质；未声明时按 `standard`（非简单）处理。类型只决定
“是否默认进入 SOL 认知流程”，不是工作量估算，也不是新的门禁：

- 简单/机械类型（`trivial` / `mechanical` / `docs` / `format`）：默认 0 调用，除非出现
  `risk-high`、`replan-history`、`reopened` 等升级型信号或用户明确要求；
- 其余类型（`standard` / `feature` / `bugfix` / `refactor` / `architecture` /
  `performance` / `research` / `product-design` / `data-infra` / `governance` /
  `release` / `multi-option`）：默认进入 SOL 生命周期。

### 生命周期（默认调用，不是门禁）

| 阶段 | 默认行为 | 入口 |
|---|---|---|
| A 任务启动 | 非简单任务默认一次 CONSULT：目标与隐含需求、问题本质、关键矛盾、初步策略、主要风险、调查方向 | `sol-consult --stage understanding` |
| B 调查之后 | 已形成现场事实且存在实质方案决策（方案类任务类型 / 声明触发 / 中高复杂 / ≥2 工作项任一）建议一次 PLAN：方案比较、取舍、优先级、阶段目标。单工作项任务只要命中任一条件同样适用 | `sol-plan --stage planning` |
| C 执行期动态 | 出现六类可归因事件之一时追加 CONSULT：实质证据变化（finding 复现、条件回退）、决策点（新登记决策）、风险升级、重规划、合同实质修订（scope-changed）、任务重开；普通证据追加、备注变化、常规步骤推进不触发 | `sol-consult --stage development --force --trigger ...` |
| D 阶段复盘 | medium/high 任务完成 ≥2 个工作项（或 ≥1 项且已核销条件）后做阶段成果复盘；同一合同与重开周期共用一个稳定窗口，普通工作项/证据/条件进展不反复制造新 D 义务（`recommended`） | `sol-consult --stage stage` |
| E 收口判断 | 有实质进展的非简单任务在最终工程验证完成后（工作项终态、条件核销、证据与合同必要测试齐备）、G1–G3 收口之前做一次综合判断：目标达成、重要遗漏与剩余风险；工程验收仍由 ZCode 完成 | `sol-consult --stage closure` |

- **没有硬性调用次数或 Token 上限**：重大新证据、实质策略变化、方案失败、新风险都可以
  追加；但每次必须有明确问题与预期决策价值（`--force --trigger --reason` 是决策价值声明，
  不是次数配额）。同一材料/同一合同没有新信息时复用已有结论，不重复付费。
- **CONSULT 与 PLAN 独立覆盖**：同一合同内分别判断，不互相替代；合同实质修订后旧结论
  不再覆盖当前合同，先重新 CONSULT 理解，再按需 PLAN。
- **REVIEW 只在用户明确要求时执行**：不由阶段、风险或调用次数自动派生；用户明确要求后，
  授权在同一任务合同内持续有效，范围或风险实质变化时重新确认。
- 任务处于任务级 BLOCKED 或已收口（CLOSED）时不建议新的 SOL 调用；项级阻塞只冻结对应
  工作项，其余工作与 SOL 认知照常推进。

### 复杂度与建议（确定性，只读）

`review-admission` / `evaluate` / `drive` / `status` 输出三个层次，全部只读、不写状态、
不是门禁：

- `complexity`：`low` / `medium` / `high` ＋ 命中的信号（见下表）与 `non_simple`；
- `advice.action`：`none` / `reuse` / `consult` / `plan` / `review`，附建议模式、阶段与命令；
- `recommended_action`：`not_needed` / `optional` / `recommended` 兼容视图。

| 信号 | 含义 |
|---|---|
| `risk-high` | 任务风险为 high（不可逆/生产/安全影响） |
| `declared-trigger` | 准入时声明了 security / permissions / core-logic / data-integrity / architecture / global-execution / production / release |
| `multi-module` | 已登记证据跨越 ≥2 个模块目录 |
| `multi-action` | 计划内工作项 ≥4 |
| `interdependent` | 计划内工作项依赖边 ≥2 |
| `replan-history` | 出现过 `AUTO-REPLAN`（方案失败后重规划） |
| `reopened` | 任务曾重开 |
| `user-request` | 用户明确要求 SOL 判断或独立复核（`--user-request`） |

- 0 个信号且为简单类型 → `none`：SOL 0 调用，即使进入 PASS/CLOSED 也不因阶段调用；
- 1 个 → `medium`，≥2 个 → `high`：非简单任务仍按生命周期建议 CONSULT / PLAN；
- 已覆盖且没有升级型新信号 → `reuse`：直接复用已有认知结论；
- 只有升级型信号（`risk-high` / `replan-history` / `reopened`）新出现，或合同被修订
  （合同指纹变化＝新范围），才允许追加；证据目录增加、工作项增减等结构变化不算新的认知对象。
- 收口前建议通过 `status` / `review-admission` / `evaluate` 获得；`drive` 命中 PASS 时按
  固定顺序推进：最终验证 → 收口 CONSULT（如建议）→ ZCode `sol-accept` 独立验收 →
  G1–G3 收口。验证有缺口时 `drive` 直接返回 `EXIT_EXECUTE`，不执行 SOL、不收口。

### 生命周期主动调度（v9.6）

**建议只有落成“义务”并真的被执行，才算 SOL 参与了任务。** 调度层把每次生命周期判断
物化为一条调度义务（`copilot-sol-obligation/v1`），并在真实命令路径上有界执行：

| 概念 | 含义 |
|---|---|
| 义务（obligation） | 一次“欠下的认知判断”：`family`（A 启动 / B 规划 / C 事件 / D 里程碑 / E 收口）+ `event` + `instance` + 合同指纹 + 级别（`recommended` / `optional`）；写入 `state.sol_schedule`，只增不改 |
| 义务键 vs 复用键 | 义务键标识“哪个窗口欠一次判断”（任务 + 合同 + family + 事件 + 实例）；复用键仍由 `sol_review` 的 subject 摘要决定“同一材料与问题能否不重复付费”。两把键分开，避免用复用去掩盖漏调 |
| 窗口 | `open` / `closed`；只有已持久化的事实能关闭窗口（任务收口、更晚窗口已满足、合同修订）。关闭时仍为 `due`/`claimed` 即为**漏调**（派生，不写死）；`in_flight` 单列为未决 |
| 有界执行 | 每条生命周期命令最多执行 **1 条义务、1 次请求**（`drive` 只在 RUNNING 阶段消费一次，PASS 分支的 E 窗口单独一次）；不循环、不自动重试 |
| 自动材料 | `--obligation SO<n>` 执行时由状态确定性生成材料（目标、合同、条件、工作项、证据身份与相对上次的实质变化），主代理不必先写材料文件；手动调用仍以 `--materials` 为准 |

**策略开关**：`--sol auto|advise|off`（或 `COPILOT_SOL_AUTO`），默认 `auto`：

- `auto`：`recommended` 级义务直接执行（D 里程碑与 E 收口都是 recommended）；历史 `optional` 义务只提示，收口时按 `not-required` 计合理跳过。
- `advise`：只打印可直接粘贴的命令（无占位符），不发起调用。
- `off`：只记录义务，不打印、不执行。
- 读命令（`status` / `sol-schedule` 不带 `--execute` / `sol-stats` / `handoff`）**永不执行**；
  测试进程（`unittest` 已加载）默认不自动调用；`COPILOT_SOL_AUTO=off` 可强制离线。

**执行与结算**：执行仍走既有通道（`_run_sol_channel` → `sol_review`，同一 `sol_calls` 台账、
同一 `.sol.lock`），不新建平行调用体系。成功写 `source=executed` 与 `call_id`；同一认知对象
已有有效结论时由复用满足（`source=reuse`，不重复付费）；失败记录真实原因（`status=failed`
+ `failure`），工程继续，不阻断；前置条件连续失败 3 次后终止该义务，避免每条命令重复报错。
合同修订后旧合同下未执行的义务显式记为 `skipped/contract-amended`（不进入新分母，但仍披露）。

**覆盖率（观测，不是门禁）**：`sol-stats --state S` 或 `sol-stats --workspace W` 输出
`copilot-sol-stats/v1`，calls 明确拆出台账 completed/failed/total、进入 transport 的尝试、收到响应、
发送状态未知、未尝试与历史缺观测；Token 与 latency_ms P50/P95 只基于实际记录，缺样本为 null/0 并披露记录数。
分母显式且只含适用窗口：启动（非简单任务）、PLAN（按
`plan_admission` 判定适用）、收口（有实质进展且中/高复杂或高风险的非简单任务）、事件（每出现一次
构成一个窗口）。统计区分建议 / 执行 / 复用 / 跳过 / 失败 / 漏调 / 未登记
（`not_registered`），以及采纳 `accepted|modified|rejected`、tokens 与延迟（缺失标
`recorded_calls`，不推断）。覆盖率低只说明参与不足，**不改变阶段、G1–G3 或收口结论**。

### CONSULT / PLAN 输入与输出

- **入口**：`copilot_dispatch.py sol-consult --state S --materials M.md --stage understanding
  --question "本次真正需要判断的事项"`；`sol-plan` 用法相同。`--dry-run` 只校验请求，
  `--list` 只读查看。CONSULT / PLAN 必须给出 `--question`，没有明确问题时不调用 SOL。
- **输入（主代理整理）**：任务目标、当前工程基线、已确认事实、关键问题与约束、已尝试方案
  及结果、必要证据摘要。**不要**提交完整仓库、冗长日志、重复报告、无关历史或敏感凭据；
  已确认的事实不要重复调查。材料上限 120,000 字符（提示阈值 60,000），超限拒绝而不是静默截断。
- **输出契约 `copilot-sol-think/v2`**（CONSULT 与 PLAN 共用同一超集结构，所有字段必须出现）：
  `judgment`、`goal_understanding`、`essence`、`strategy` 为字符串；
  `alternatives`、`tradeoffs`、`risks`、`priority`、`investigation`、`phase_goals`、
  `assumptions`、`omissions`、`decisions`、`verify`、`reconsult`、`unverified` 为字符串数组。
  字段类型不符或没有判断内容时拒绝消费。
- **重新咨询条件**：原方案连续失败需要改策略、出现新的关键风险、任务目标/范围/约束实质
  变化、材料有重大遗漏、出现超出当前判断力的重大不确定性。普通测试失败本身不构成再咨询。
- **治理边界**：结论写入 `sol_calls`（`mode=CONSULT|PLAN`），不进入 Finding 台账、不改变
  完成条件、不影响 G1–G3；是否采纳、如何实施由 ZCode 主代理决定，并用 `sol-accept` 登记。
- **模型与推理强度**：所有 SOL 模式统一 `gpt-6.2-sol` + `reasoning.effort="high"`
  （`standard` 模式），不按角色降级，不允许静默回退到 `medium`/`low` 或其他模型；
  上游回显的 `model`、`reasoning.effort`、`reasoning.mode` 不一致时拒绝消费（退出码 9）。

### 单轮原则（硬约束）

- **一次调用只发起一次 SOL 请求**。同一认知对象（合同＋材料＋问题＋阶段＋提示词版本）
  再次调用直接复用已有结论，不重复付费。CONSULT、PLAN、REVIEW 各自独立判定，不互相复用。
- **不自动二审**：修复后由主代理核实、修复并做受影响的定向测试；不因 Minor/Info 新建
  第二轮，不与 SOL 反复辩论，不把 PASS 当“再跑一轮”的理由。
- 再次调用必须用 `--force --trigger <原因类型> --reason ...`，并换新材料或新阶段。
- **失败收敛**：429、超时、网络错误、截断、响应格式异常、模型身份不符都记录真实原因，
  不自动重试、不切换 Provider、不派原生 Reviewer 顶替。CONSULT / PLAN 失败只记录并返回
  “可继续”，主代理按已有信息继续推进；REVIEW 失败如实报告，不伪造 PASS。

### 审查材料（REVIEW 专用）

用户明确要求独立复核时，材料必须包含：任务目标与完成条件、相关项目规则/关键决策、完整
变更 Diff、必要的关键文件内容、关键调用链与数据流、已执行测试及结果、尚未解决的问题、
需要重点关注的风险。不得只给简短摘要让 SOL 猜测。`sol-review` 自动注入合同/完成条件/
目标，并把材料拼成一次完整上下文；超过 400,000 字符直接拒绝（提示主代理整理），不静默截断。

### 知识交接（Memory / Vault → SOL / 子 Agent）

主 Agent 实际 Read 过的 Memory/Vault 卡片，用
`python3 -B scripts/knowledge_handoff.py build --input handoff.json --output handoff.md`
生成交接块；`read` 必须附卡片正文摘录，脚本会校验摘录确实出现在卡片中；
`index-only`/`unverified` 必须如实标注，不得写成已确认。`read` 表示「主代理声明已读 +
脚本核对摘录来源」，不证明宿主同轮工具调用；需要时用可选 `read_evidence` 记录会话/
工具证据，由 Copilot 另行登记。只交接与当前任务直接相关的 1–5 条，不把整库或完整
Memory 注入。

- `sol-consult` / `sol-plan` / `sol-review` 支持 `--knowledge-file handoff.json`，校验通过后
  把交接块追加到 materials；不要再手工粘贴同一块。
- 派发原生子 Agent 时，用 `--target subagent` 生成同一块并放入 prompt；子 Agent 不自动
  继承 Memory/AGENTS，路径不能替代正文。
- 涉及既往经验或已确认决策时，材料附卡片路径、核验时间与必要摘录；SOL 零工具，不能
  假设它会自行读卡。

REVIEW 输出契约：总体结论 `verdict=pass|partial|blocked`；`findings` 按
`blocking/major/minor/info` 分级，每项带文件、位置、材料依据与建议；`unverified` 列出无法
从材料确认的事项。PASS 与 blocking/major 或未验证项冲突时自动降级为 PARTIAL 并记录
`verdict_adjusted`。

### 项目上下文入口（Context Engine，v9.4.0）

四层信息（原生 Memory / Vault / Task Memory / 工程现场）的确定性衔接统一走
`scripts/context_engine.py`，只读、零模型调用：

- `resolve`：按 Git 根与 `sha256[:16]` 精确定位项目记忆目录（同名不同路径不串库），
  输出分支/HEAD/工作区、Memory 索引统计与规则文件是否存在；缺失为 `absent`，不可读为
  `unreadable`，两者都不是零命中。
- `recall`：只在 knowledge-manager 正式白名单与当前项目 Memory 内检索（关键词必填，
  `|`/`,` 分隔），跳过 `01-Inbox`/`03-Daily-Reviews`；默认 40 行 / 24 MB / 5 s 预算，
  `zero_hit`、`failed`、`budget_exhausted` 严格区分；结果带 `path:line:snippet`、核验时间、
  `source_commit`；按（项目、关键词、scope、预算、文件集摘要）短期缓存，文件变化即失效。
- `pack`：装配项目身份、工程基线、任务合同与完成条件、历史线索、可信度提示与验收要求；
  `self` 12,000 / `sol` 8,000 / `subagent` 5,000 字符，超限按固定顺序裁剪并标 `trimmed`。
  子 Agent 不继承 Memory/AGENTS 注入，派发前用 `--target subagent` 生成并放进 prompt；
  已有交接块用 `--handoff-file` 组合。
- `capture`：读任务状态与证据，生成带任务/证据溯源、去重前置、住所待判定的候选草稿到任务
  目录，并打印 `candidate_write.py` 命令；工具本身不写 Vault 与原生 Memory。

接线：UserPromptSubmit 对工程任务首轮自动召回（Memory 3 + Vault 2 条、≤1,400 字符、
会话内去重与 60 s 冷却，失败不阻断）；SessionStart(startup|resume|compact) 快照/指针追加
项目现场/Memory 可用性与上述命令，resume/compact 重放已召回摘要；Stop 在任务收口或真实
阻塞后要求一次 Capture 评估并写幂等回执（`capture-ack`），不改任务状态。历史线索与现场
冲突时以现场为准，并在报告中写出卡路径、差异与核验时间。详见 `docs/CONTEXT-ENGINE.md`。

CONSULT / PLAN 输出契约（`copilot-sol-think/v2`）：`judgment`、`goal_understanding`、
`essence`、`strategy` 为字符串；`alternatives`、`tradeoffs`、`risks`、`priority`、
`investigation`、`phase_goals`、`assumptions`、`omissions`、`decisions`、`verify`、
`reconsult`、`unverified` 为字符串数组。schema 不一致、字段类型不符或判断为空时拒绝消费，
不做叙述猜测。

### 输出消费与 Finding 台账

- SOL 的结论是**建议**，不是任务 PASS。SOL 记录统一在 `sol_calls`（历史
  `sol_reviews` / `sol_advisories` 只读兼容），与历史 `native_reviews` 分开；任何 SOL 记录
  都不会产生 `g4_eligible`，也不改变门禁集合。
- SOL findings 以 `advisory` 身份进入 Finding 台账：主代理逐条核实后，修复并登记
  `fixed`/`verified`；确认是误报的，可用 `finding --status dropped --note <反证>` 驳回
  （仅 advisory 的 blocking/major 允许）。v7 及更早的 authoritative 记录仍只能修复并
  定向验证。
- 未闭合的 blocking/major 阻断 G2（所有风险等级）；minor/info 不阻断、不派发新复核。
- 原始输出保存在任务目录 `sol-consult/`、`sol-plans/` 或 `sol-reviews/`，审计
  `sol-audit.jsonl` 只记脱敏元数据（材料/请求/回复摘要、模型、usage、耗时、失败原因），
  不记正文与凭据。

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

收敛顺序：**充分自检 → 稳定最终执行结果与证据 → 按生命周期做一次收口 CONSULT（如建议）→
集中修复 → 定向验证 → 收口**。SOL 不是收口前提：主代理判断材料是否具备、意见是否有增量价值。
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

### 跨回合续接（同一任务）

跨回合（用满自动续跑、用户新开会话、进程重启）继续同一任务时：

1. **复用同一 `--state`**：不新建任务、不改 `task_id`，续接点就是原 `state.json`。
2. **先核实再动手**：`status --json` 看阶段、`continuation`、ready/stalled、门禁缺口；
   核对 Git 工作区实际改动、证据文件与摘要、未闭合条件、未完成工作项及其阻塞原因。
3. **不重做已完成工作**：`done` 的项、已登记且摘要未变的证据直接复用；只继续真正未完成的
   部分。证据不覆盖、不改写，需要新证据就新增。
4. **保持原授权范围**：续接不继承新的授权，也不扩大范围；原任务内已给的授权继续有效。
5. **仍在 PARTIAL 就继续**：按派生规则执行并回填，不因"换了回合"另建重复任务，也不把
   上一回合的进展当成已完成而未登记。
6. **真实 BLOCKED 才停**：列出每条阻塞的 `reason`/`needs` 与解除条件；条件满足后
   `unblock` 并立即恢复。

## 模块路由（按需读取）

**读取 references 前先确认需要哪一节；不要预读全部。**

| 场景 | 必读文件 |
|---|---|
| 命令矩阵、证据登记、收口操作、只读交付清单、历史只读 | `references/workflow.md` |
| 阶段机、`drive`/`ingest`、PARTIAL 派生、退出码 | `references/runtime.md` |
| SOL CONSULT/PLAN/REVIEW、单轮调用、正式 REVIEW、缺陷分级、台账与历史 G4 记录 | `references/review.md` |
| 原生 Bridge v2 执行信封、错误码 | `references/agent-bridge.md` |
| Agent 调度与内置 Explore/general-purpose 能力基线 | `copilot_dispatch.py agent-policy`；`references/agent-bridge.md` |
| 项目上下文（resolve/recall/pack/capture）、四层信息与验收映射 | `docs/CONTEXT-ENGINE.md` |
| 历史 REVIEW 台账与旧审计（只读） | `references/review-ledger.md` |
| 版本沿革、G4 退役与 SOL 升级记录 | `references/history.md` |


## 版本

当前 **v9.6.2（统计口径修正、SOL 锁等待有界、文档与死代码收敛）**：

- **覆盖率口径修正（P1 缺陷）**：任务级窗口（A 启动 / B 规划 / E 收口）的分母是“本任务是否
  适用”（0/1），分子过去却取满足义务的条数——多轮 reopen 的多次收口判断会得出
  `rate=3.0`（3 次收口 ÷ 1 个任务）。现按任务计 1 次（满足＝真实执行或复用），义务条数与
  执行/复用/跳过/漏调的分项仍完整保留在 `by_family`；`totals` 新增 `closure_eligible_tasks`。
- **事件分母**：C 事件窗口按“每出现一次事件 → 一个适用窗口”计数，合理跳过（合同修订、
  被新窗口取代、任务已关闭）不再进入分母，与 `_expire_superseded` / `sync` 的口径一致。
- **自动调度锁等待有界**：此前文档承诺“有界执行”，但自动路径从不传 `--lock-timeout`，
  实际会无限等待另一进程的 SOL 请求。现在自动路径固定带上限
  （`AUTO_LOCK_WAIT_S` 默认 120 s，`COPILOT_SOL_LOCK_TIMEOUT` 覆盖）；拿不到锁时义务保持
  `due`、不付费、不阻断工程命令。`sol-review` 也接受同一参数。
- **文档/帮助与实现对齐**：生命周期命令在 `--sol auto` 下会顺带执行 1 条义务，不再宣称
  “所有命令都不调用模型”；收口顺序统一写成 最终验证 → E → `sol-accept` → G1–G3；D 里程碑
  级别为 `recommended`（不是 `optional`）；风险只影响认知准入、REVIEW 仅用户明确要求；
  钩子的 `0/2`（Stop，fail-closed）与 `exit 0`（SessionStart/UserPromptSubmit）分开描述。
- **死代码收敛**：删除零引用且无动态访问的常量/别名/助手（`DEFAULT_BUDGET_S`、
  `TERMINAL_OBLIGATION_STATUSES`、`EVENT_SCHEMA`、`open_obligations`、`describe`、
  `SOL_EVENT_ORDER`、`SOL_DYNAMIC_EVENTS`、`SOL_EVENT_FAMILY`/`event_family`、
  `COMPLEXITY_LEVELS`、`NON_SIMPLE_TASK_KINDS`、`LEGACY_ASSIGNEES`、
  `QUALITY_ACCEPTANCE_STATUSES`、`REVIEW_ATTEMPT_RE`、`TERMINAL_PHASES`、`_bool`、
  `add_admission_triggers`、`list_sol_think_calls`、`ACCEPTANCE_EVIDENCE_STATES`、
  `record_sol_advisory*`、`SCHEMAS`、`_optional_text`、`_action_fingerprint`、
  `LEGACY_THINK_SCHEMAS`、`_brief`）；`legacy/` 与历史审计/状态读取兼容不动。
- **性能**：SOL 网络栈（`urllib.request`/`ssl`/`http.client`/`email`）改为请求路径内惰性导入；
  同机交替 A/B 中位数（先 `compileall`，每条命令 `-B`）：`import copilot_dispatch`
  98.2 → 80.0 ms，`status` 155 → 127 ms、`closure-check` 133 → 110 ms、`sol-stats`
  129 → 112 ms、`handoff` 149 → 132 ms。
- **不变量**：G1–G3、收口顺序与 `closure_readiness` 准入、PARTIAL 续跑、SOL advisory、
  REVIEW 仅用户明确要求、G4 退役、Provider/模型/账户配置全部不变。

**v9.6.1（收口时序：最终验证完成才执行收口 CONSULT）**：

- **为什么改**：v9.6.0 的 E 准入只看“工作项是否耗尽”（`all_done`），`_sol_schedule_step`
  又固定传 `gates_passed=False`，于是最后一个工作项一关闭就可能立刻执行收口 CONSULT——
  而合同要求的测试、最终证据、条件核销甚至 G1–G3 还没完成，SOL 拿到的是不完整的收口材料。
- **统一准入**：`governance.closure_readiness()` 是 E 与收口的唯一准入，先查便宜事实
  （工作项终态、条件核销、证据、合同必要测试的执行痕迹、重开后无进展、开放 finding），
  再算确定性门禁（不含质量报告的用户接受，它属于 ZCode 验收之后的用户一步）。
- **验证摘要**：`closure_verification_digest()` 只取验证事实的 `sha256[:16]`，作为 E 义务的
  instance。义务与当前摘要不符时拒绝付费；同一摘要再到达判 `reuse`；重跑测试或新增/修订
  证据后摘要变化，旧 E 结论不再覆盖新结果。
- **入口一致**：`drive` 在缺口时返回 `EXIT_EXECUTE` 且不执行 E；`closure --close`、手动
  `sol-consult --stage closure`、`close_task` 都拒绝绕过；回填命令只更新义务。`evaluate`
  的 `continuation` 在缺口时给出 `verify` 动作与阻塞原因。
- **验收顺序强制**：E 执行后必须先有 ZCode 的 `sol-accept`（accepted/modified/rejected 任一）
  才能收口——`closure_acceptance_pending()` 让 `close_task` 失败关闭、`drive` / `closure
  --close` 停在 `EXIT_EXECUTE` / `EXIT_CONTINUE` 并打印命令。裁决不是 SOL 批准权，也不替代
  用户对质量报告的接受。
- **材料口径进摘要**：`MATERIALS_SCHEMA` 参与 `closure_verification_digest()`，材料生成口径
  变化（例如合同正文来源修正）即可让旧 E 结论停止覆盖新结果；材料内自报口径版本。
- **测试证据可核对**：`evidence --capture-cmd` 现在真的把命令写进证据记录（此前该字段是死的），
  合同声明命令按词元顺序匹配（允许 `-B` 等运行器参数），自述“测试已通过”不算执行痕迹。
- **不变量**：五阶段架构、G1–G3、PARTIAL 续跑、SOL advisory（E 不是门禁、不需 SOL PASS）、
  REVIEW 仅用户明确要求、G4 退役、模型/Provider 配置全部不变。

**v9.6.0（SOL 生命周期主动调度：义务化、有界执行、覆盖率统计）**：

- **为什么改**：v9.0–v9.5 的 SOL 建议是纯函数输出，只在 `start`/`status`/`drive`/
  `review-admission` 打印；建议命令带 3 处占位符，`_run_sol_channel` 又要求
  `--materials`/`--question`，于是“应当调用”从未进入任何状态，事后也无法区分“没建议”
  与“建议了没执行”。`drive` 在 PASS 分支直接收口，收口 CONSULT 结构上不可达。
- **调度义务**：新增 `scripts/sol_schedule.py`（纯函数、零网络、零状态写入）。每次生命周期
  判断物化为义务（family A–E / event / instance / 合同指纹 / level），写入
  `state.sol_schedule`；窗口只按已持久化事实关闭，漏调由关闭时的事实派生。
- **有界自动执行**：`start`/`plan`/`record`/`rerun`/`criterion`/`judgment`/`evidence`/
  `report`/`accept`/`decide`/`block`/`unblock`/`finding`/`contract`/`reopen`/`closure` 与
  `drive` 在回填后做一次有界调度步：最多 1 条义务、1 次请求，并由
  `AUTO_LOCK_WAIT_S`（默认 120 s，`COPILOT_SOL_LOCK_TIMEOUT` 覆盖）限制锁等待；拿不到
  锁时义务保持 `due`、不付费、不阻断。`--sol auto|advise|off` 控制策略，非 recommended
  义务只提示，读命令永不执行。
- **PLAN 准入唯一口径**：`plan_admission()` 不再由工作项数量单独决定（方案类类型、声明
  触发、中/高复杂、≥2 工作项任一），并默认读取任务自己声明的触发因素。
- **六类动态事件可归因**：`material-evidence-change`（finding 复现 / 条件回退）、
  `decision-point`（新登记的显式决策，`add_decision` 写入 `decision_id`）、
  `risk-escalation`、`replan-history`、`scope-changed`（合同指纹变化）、`reopened`。
  调用记录持久化 `sol_event_snapshot` 与 `closure_key`，事件比较与收口周期去重以真实
  记录为基线（合同修订后仍与旧基线比较，才能判为 scope-changed）。
- **里程碑与收口接线**：D 从“仅 high”扩到 medium/high（完成后 ≥2 项或 ≥1 项且条件已核销），
  级别 `recommended`（`auto` 下自动执行 1 次）；`drive` 的 PASS 分支与 `closure --close`
  先消费 E 窗口、再要求 `sol-accept`，然后收口
  （`closure.sol_entry` 区分 `drive` / `closure-command` / `zcode-native`）。
- **去重、并发与降级**：同一 (合同, family, event, instance) 只有一个义务；同一 subject
  复用不重复付费；`task_lock(timeout)` 拿不到锁即放弃本次执行（义务保持待执行）；失败记录
  真实原因并继续，前置条件连续失败 3 次后终止该义务。
- **统计**：`sol-stats` 输出显式分母的覆盖率、事件分布、采纳分布、tokens 与延迟；建议、
  执行、复用、跳过、失败、漏调、未登记分开统计。覆盖率不是门禁。
- **不变量**：SOL 零工具单轮、advisory、G1–G3、ZCode 唯一工程执行与技术决策主体、REVIEW
  仅用户明确要求、授权边界、模型与 Provider 配置、G4 退役状态全部不变。

**v9.5.1（宿主集成修正：startup 恢复、钩子可发现性、工作单条件归属）**：

- **SessionStart matcher 含 startup**：宿主只在全新会话发 `source=startup`；匹配器为
  `startup|resume|compact`。startup 只给只读指针（不注入任务正文、不自动绑定、不强制
  续跑），resume/compact 且绑定本会话时仍给完整快照。
- **状态文件位置是硬条件**：任务必须位于 `<workspace>/.copilot-tasks/<task>/state.json`，
  三个钩子只扫描该目录；`start` 对目录外路径给出提示。
- **工作单条件归属**：`work_order` 优先使用本工作项 `target_criteria`（`AUTO-C<n>` 次之），
  多条件工作项提示“还对应 Cx、Cy”，不再一律写 C1。
- **交办次数跨过期租约累计**：中断后被其它会话接管仍记第 2 次；主动释放/完成后重新领取
  从 1 计数。
- **不变量**：G1–G3、PARTIAL 强制续跑、SOL-first advisory、授权边界与 G4 退役状态不变。

**v9.5.0（持续推进：调度领取、恢复提示与阻塞边界）**：

- **调度领取（claims）**：`drive` 在 RUNNING 阶段把工作项领取落盘（`claims`：owner 会话、
  `claim_id`、`expires_at`、`handoff_count`；默认 TTL 1800s，`COPILOT_CLAIM_TTL` 可调）。
  同一工作项不会被两个会话同时领取；其它会话的有效租约不抢占，到期自动回收；`record` /
  `rerun` / 条件 `met` / `block` 自动释放。**领取是交办事实，不是执行证明**，工作项终态
  仍以 `status` / `result` / 证据为准。只剩通用 `AUTO-NEXT` 兜底且具体缺口已被其它会话
  领取时，`continuation.action=wait`（`required=false`）且 `drive` 返回 11，不重复规划。
- **计划目标条件**：`plan` 工作项可声明 `target_criteria`（必须真实存在于合同），用于领取
  去重、阻塞自动归属；该字段不进入计划摘要，native dispatch 的计划指纹保持不变。
- **UserPromptSubmit 逐回合提示**：`install` 写入 Stop / SessionStart / UserPromptSubmit
  三个事件；`user-prompt` 只读列出未闭合任务（含 BLOCKED 的 reason/needs）与判断规则。
  用户本回合补齐授权/决策时，先用其原话登记 `unblock --by user --quote "<原话>"`，再
  `drive` 继续；提示本身不是用户新指令，也不构成授权。没有未闭合任务时不注入。
- **恢复可发现性**：SessionStart(startup|resume|compact) 对本会话绑定任务注入完整快照，startup 只给指针；
  或重启后会话标识变化时只注入只读指针（任务阶段、可执行项、阻塞原因/需要、归属标注），
  不绑定、不接管、不强制续跑。快照与 status 均显示阻塞原因与所需条件。
- **阻塞边界与解除来源**：仍有可执行工作项时，`block --scope task` 被拒绝，必须显式
  `--force` 并记录 `ready_at_creation` / `forced_scope`；单项授权缺口走 `--work-item`。
  `unblock --by user` 必须同时给出 `--quote`，状态记录 `resolved_by`、
  `authorization_source=caller-declared` 与原话，供审计核对。
- **不变量**：G1–G3、PARTIAL 强制续跑、SOL-first advisory、证据完整性、授权边界、ZCode
  唯一工程执行主体与 G4 退役状态全部不变。

**v9.4.0（项目上下文与长期记忆衔接）**：

- **Context Engine**：新增 `scripts/context_engine.py`（`resolve`/`recall`/`pack`/`capture`），
  把原生项目 Memory、正式 Vault、Copilot Task Memory 与工程现场拼成一条确定性数据流：
  按 `sha256[:16]` 定位项目记忆、白名单有界召回（预算/零命中/失败/截断可区分、短期缓存）、
  按目标装配上下文包（self/sol/subagent 三档字符上限）、收尾生成带任务与证据溯源的候选
  草稿。全程只读、零模型调用、不建记忆库、不写正式知识。
- **恢复与交接**：SessionStart(startup|resume|compact) 快照追加项目现场（根/分支/HEAD/工作区）、
  原生 Memory 与 Vault 可用性以及召回/交接命令（失败即省略，不阻断启动）；`status` 人工
  输出附一行上下文提示，JSON 契约不变；子 Agent 用 `pack --target subagent`（≤5,000 字符）
  显式交接，不假设其继承 Memory/AGENTS。
- **可信度**：命中一律带核验时间与 `source_commit`；无核验标注「视为未核实」，来源提交
  与当前 HEAD 不一致标注「基线漂移」；冲突以现场为准并写出卡路径、差异与核验时间。
- **不变量**：G1–G3、PARTIAL 强制续跑、SOL-first advisory、证据完整性、授权边界、ZCode
  唯一工程执行主体与 G4 退役状态全部不变。

当前 **v9.3.0（治理一致性与续跑绑定加固）**：

- **合同修订授权门**：`contract --amend` 不再能悄悄减少未闭环条件或关掉 `test_policy`。
  净减少未核销条件（含把条件改写成更弱表述）或 `required` 由真变假时，必须显式
  `--authorized-by user` 并写明 `--authorization-note "用户要求"`；拒绝时状态不变。
  合同历史记录 `authorized_by`、`authorization_source=caller-declared`、授权说明、新增/
  删除的未核销条数、`removed_criteria` 与 `test_policy_weakened`。`authorized_by` 是调用方
  自报声明而非可验证凭证：它是“执行方声称用户同意”，审计须核对授权说明与用户原话。
  `reopen` 同理，`reopen_history` 标注 `authorization_source=caller-declared`。
- **条件与包装项同步**：完成条件转 `met` 时，自动撤销仍无结果的 `AUTO-C<n>` 包装项
  （`auto_closed_for`），不再出现“条件已完成但永远差一条工作项”；条件回退 `pending`
  时重开该项。计划中的业务工作项即使声明 `target_criteria` 也不被顺带撤销。
- **重开不得复用旧 PASS**：`reopen` 记录当时的进展基线（证据数、终态工作项、条件状态），
  没有新证据、新工作项结果或条件变化的收口请求返回 PARTIAL，`close` 直接拒绝；`evaluate`
  同步 fail-closed。
- **阻塞与恢复项**：同一工作项被多个 block 引用时，只有全部解除才恢复；恢复支路
  `AUTO-R-*` 重启后归一到同一家族（不再出现 `AUTO-R-AUTO-R-…`）。
- **Hooks 续跑绑定**：会话只在命令成功（0/10）且非只读命令后绑定；任务根查找在 `.git`
  处停止，不继承祖先目录；`resume` 摘要按整行截断以保证命令可粘贴；损坏/未绑定任务不注入，
  绑定任务损坏时给出修复提示；守卫自身异常 fail-closed（exit 2 + `internal-error`）。
- **SOL 单轮键**：同材料/同模板的单轮复用键先 `strip()` 再哈希，并纳入提示词模板哈希，
  `--force` 优先于复用；CPA 端点拒绝带 query/fragment 或额外路径。
- **CLI 一致性**：`plan` / `finding` / `accept` / `reopen` / `contract --amend` 在仍有续跑
  工作时同样返回 `EXIT_CONTINUE=10`，不再以 0 结束回合。
- **不变量**：G1–G3、PARTIAL 强制续跑、SOL-first advisory、证据完整性、授权边界、ZCode
  唯一工程执行主体与 G4 退役状态全部不变。

历史版本（v9.2.0 及更早，含 v9.2.0（ZCode 原生恢复衔接与上下文瘦身）、v9.1.0（SOL 采纳记录）、
v9.0.0（SOL-first 默认认知生命周期）、v8.5.0 PARTIAL 强制续跑、v8.0.0 G4 退役、
v7.0.0 SOL 零工具单轮复核、v6.x 治理收敛与 v5.x 退役说明）见
`references/history.md`；本文件不再重复历史正文。

# AGENTS.md — Copilot 源码仓库规则

本文件只约束 `<repo>` 仓库。用户级全局 `AGENTS.md` 的授权边界与
安全规则优先；本文件只补充项目定位、角色、模型路由、闭环与交付纪律。详细协议以
`SKILL.md`、`references/` 与 `docs/` 为准，本文件不重复展开。

## 1. 项目定位

Copilot 是运行于 ZCode 内的**轻量协作治理 Skill**：负责任务承接、合同与完成条件、状态机、
证据桥接、审计、PARTIAL 持续推进与 G1–G3 收口。ZCode 原生 Agent 是唯一的工程执行与技术
决策主体；用户侧 ChatGPT 负责需求与产品层目标、交接与验收；SOL 作为默认认知引擎
（CONSULT / PLAN）按生命周期提供整体判断，用户明确要求时执行一次独立复核（REVIEW），均由
`gpt-6.2-astra` 零工具单轮调用完成。`gpt-6.2-sol` 只保留为显式兼容模型（`--model`），
不做失败回退。

当前基线 **v9.9.0**（Astra 默认认知引擎迁移与调用效率优化：模型身份入账、按模型隔离退避、
纯登记命令不隐式付费、复用命中与模型级指标）。与版本号
无关、长期有效的关键约束：

- **执行与续跑**：任务必须经 `start` / `drive` 进入状态机；PARTIAL 不是终点，
  `continuation.required=true` 时必须继续派生/执行；阻塞按工作项成立，单项缺授权不冻结
  其余工作；`drive` 用 `claims`（短租约）避免并发重复，回填/阻塞自动释放。
- **SOL 是认知引擎，不是门禁**：生命周期为 A 启动 CONSULT → B 调查后 PLAN → C 升级型事件
  CONSULT → D 中高复杂阶段复盘 → E 最终验证完成后的收口 CONSULT。`sol_schedule.py` 把窗口
  物化为义务，每条命令最多 1 个义务 / 1 次请求（`--cognition auto|advise|off`，
  `--sol` 兼容）。生命周期命令默认 `auto`；纯登记/报告/合同命令默认 `advise`，只在命令行
  显式 `auto` 时才付费执行，`judgment` / `report` / `evidence` 等不得隐式调用模型。同模型、
  同 task_id、同归一化上下文与同问题时复用，不重复付费；复用命中登记到 `sol_reuses`。
  义务窗口关闭时写 `disposition`：`not-executed` 用**窗口打开时**的策略
  （`policy_at_open`；历史义务按周期策略快照的变更历史回推，无可证明快照记 `unknown`），
  不用当前配置倒推；`cycle` = 1 + 重开次数，窗口比较不跨周期追溯。`sol-accept` 只记录采纳
  事实，**不因代码已改、测试通过或任务 PASS 推断采纳**。CONSULT / PLAN 失败只登记并降级
  （退出码 0）；REVIEW 只在用户明确要求时执行。SOL 不进入 Finding 台账，不影响阶段与 G1–G3。
- **收口顺序**：最终工程验证（`closure_readiness` 是 E 窗口与收口的唯一准入）→ 收口 CONSULT
  → ZCode `sol-accept` 独立验收 → G1–G3 fail-closed 收口；工作项全部关闭或 G1–G3 通过都
  不等于验证完成。G4 原生只读复核正式退役，历史记录只读保留；原生复核信封出现时返回
  `native_review_retired`。
- **上下文与恢复**：只读 Context Engine（`resolve`/`recall`/`pack`/`capture`）衔接原生
  Memory、Vault 与工程现场，零模型调用、不写知识库；SessionStart 只读快照与 UserPromptSubmit
  只读提醒都不写任务状态，异常一律 exit 0。
- **唯一源码**：本仓库是唯一源码。`~/.zcode/skills/copilot` 是构建产物，只能由
  `scripts-sync/sync_runtime.py` 单向下发，不得直接编辑。旧 Reviewer 全局 Profile 已退役；
  工作区 `.zcode/agents/independent-readonly.md` 是通用只读能力载体（只声明宿主权限，不定义
  Reviewer 角色或流程），供主代理按需做独立只读调查，普通任务不强制使用，不参与收口门禁。
  该文件不属于同步分发范围。运行时临时修复必须先回迁本仓库，再同步下发；不使用符号链接替代
  副本。源码与运行时的分工见 `docs/DECISIONS.md`。
- **版本沿革**：v9.7.1 及更早的逐版本变更记录、G4 退役过程与 SOL 升级历史见
  `references/history.md`；当前现场状态见 `docs/STATUS.md`，长期决策见 `docs/DECISIONS.md`。
  本文件只保留当前架构与执行规则，不重复历史正文。

## 2. 角色职责

顶层协作角色按 `docs/PROJECT-CHARTER.md`：用户、ChatGPT（用户侧）、Copilot、ZCode 原生
Agent、SOL（CONSULT / PLAN / REVIEW）、DesignWise。Executor 是 ZCode 工程执行体系内的
子角色，不是与顶层角色并列的职责；G4 只读复核已退役。

| 角色 | 职责 | 关键限制 |
|---|---|---|
| ZCode 主代理 | 默认执行主体：现场调查、工程规划、技术选型、实现、调试、测试、集成与交付；按需派发子代理 | 不擅自改变产品目标、业务规则或授权范围；不为体现协作强制调用子代理 |
| 内置 `Explore` | 独立只读调查：跨模块/调用链/改动前风险/独立上下文检查 | 工具集含 `Bash`，不是只读沙箱；不注入 AGENTS.md，主代理必须提供范围与项目背景；不修改工程文件 |
| 内置 `general-purpose` | 边界清晰、可独立交付、并行收益明显的子任务 | `tools=["*"]` 并继承 MCP；主代理负责拆分、冲突检查、集成与验收；不能当作只读复核者 |
| Copilot 状态层 | 合同、条件、状态、证据登记、G1–G3、PARTIAL 派生、收口、审计 | 是本地脚本与状态，不是模型；除显式 `sol-consult` / `sol-plan` / `sol-review` 外不调用模型、不实施工程、不替代技术决策、门禁不得绕过 |
| SOL CONSULT | 默认认知入口：零工具单轮输出目标理解/问题本质/路线与取舍/风险/调查方向/再咨询条件；结论写入统一 `sol_calls`（advisory） | 只做认知判断；不执行工程、不做技术决策、不写状态、不进入 Finding 台账、不影响 G1–G3；失败只降级不阻断 |
| SOL PLAN | 调查后的一次零工具规划判断：方案比较、优先级、阶段目标与验收关注点；结论写入统一 `sol_calls`（advisory） | 不规定具体代码操作；最终技术方案与执行计划由 ZCode 决定；不进入 Finding 台账、不影响门禁 |
| SOL REVIEW | 用户明确要求时的一次零工具独立审查；结论写入统一 `sol_calls`（历史 `sol_reviews` 只读兼容）与 Finding 台账（advisory） | 只做 REVIEW；不执行工程、不做技术决策、不自动二审；结论只是建议，不构成收口门禁，也不由阶段或风险自动派生 |
| 正式 REVIEW | 用户明确要求时的治理记录 | 不是普通开发前置门禁；无授权不得伪造 |
| ~~G4 复核~~ | **v8.0.0 已正式退役**；历史记录只读保留 | 不再参与准入、派生或收口；不得把历史 BLOCKED 改写成 PASS |

**模型独立 ≠ Agent 独立。** 共享同一 Provider/模型不等于共享上下文、会话或判断。
子代理与执行方必须是不同的 Agent ID / Child Session / 上下文；独立性由独立上下文与独立
取证保证，不由模型名或 Profile 名称保证。工作区 `.zcode/agents/independent-readonly.md`
只是通用只读能力载体（不作为普通任务的前置条件，也不参与收口门禁）。

## 3. 模型路由规则

- **唯一远程调用是 SOL（CONSULT / PLAN / REVIEW）**：只有 `scripts/sol_review.py` 可以导入
  网络客户端、读取本机 CPA 凭据与包含模型名（默认 `gpt-6.2-astra`；`gpt-6.2-sol` 仅显式
  兼容）；`governance.py`、
  `copilot_dispatch.py`、`copilot_auto.py`、`native_bridge.py`、`native_agent.py` 不得导入
  网络客户端或已退役的 SOL 模块（`call_gpt` / `review_ledger` / `reply_protocol`）。SOL 只
  接收主代理材料、一次请求、零工具（请求体无 `tools`/`functions`，`tool_choice="none"`）、
  `reasoning.effort="high"`，不自动重试、不换 Provider、不自动二审、不静默降级模型或推理强度。
  上游回显 `model` / `reasoning.effort` / `reasoning.mode` 与请求不一致时拒绝消费。
  Astra 失败只如实记录并降级，不自动切换 `gpt-6.2-sol`、不换 Provider、不重试。
- **退避按模型隔离**：窗口键为 `provider + model + error_class`（`state.model_backoff`），
  SOL 过载不冻结 Astra，Astra 过载不阻塞显式 SOL；成功只清本模型；旧 `state.sol_backoff`
  单例只读兼容，不继承给新默认模型。
- **Executor / Reviewer 继承主代理**：Profile 中省略 `model` 或写 `model: inherit`，
  Provider/模型/推理级别跟随主代理当前会话。不得写死官方 DeepSeek 直连模型或任何
  固定 Provider；`thoughtLevel` 只在钉死具体模型时生效，`inherit` 下忽略。
- **统一出口**：子代理请求经主代理当前 CPA 路由（默认 `127.0.0.1:8317`），不得因
  局部问题回退到官方直连 API，也不得通过修改 CPA Provider、凭据池或全局网络配置绕过。
- **模型名 ≠ 上游身份**：以 `~/.zcode/cli/db/db.sqlite` 的 `model_usage`、
  rollout 的 `model.providerId` / `x-cpa-trace-id` 与 CPA `usage_events` 三方互证为准；
  Agent 自报模型名不算证据。
- **主代理换模型后必须复查**：确认 Executor/Reviewer 仍为 `inherit` 且实际路由一致。
  Agent Profile 在进程/会话启动时缓存，配置修改后须新会话或新进程验证。
- 不修改 Provider、模型路由与 CPA 配置；相关变更须用户单独授权。

## 4. 工程执行规则

1. 修改前核实项目身份、分支、`git status`、相关入口与既有改动；保留他人及任务外改动。
2. 只改与本任务相关的文件；不顺手重构、格式化或修复无关内容。
3. 运行测试：

   ```bash
   python3 -B -m unittest discover -s tests -p 'test_*.py'
   python3 -B -m unittest discover -s legacy/tests -p 'test_*.py'   # 历史回归
   ```

   新增行为应有对应测试；不得为通过而放宽断言或删除测试。`legacy/tests` 只验证 v5
   历史行为，历史文档契约类允许 skip，但实现回归失败必须查明。
4. 检查实际 diff，清除调试残留、秘密和任务外内容。
5. 源码改动后必须同步并复查：

   ```bash
   python3 scripts-sync/sync_runtime.py --apply
   python3 scripts-sync/sync_runtime.py --check   # 缺失 0、不同 0
   python3 scripts-sync/sync_runtime.py --baseline       # 分发源码变化后重建基线
   python3 scripts-sync/sync_runtime.py --verify-baseline  # 只读校验，必须通过
   ```

   `--baseline` 只记录 Git 跟踪文件；未跟踪的本地脚本不进入提交树中的 manifest。**新增分发文件
   （`SKILL.md` / `scripts/**` / `references/**`）必须先 `git add` 明确路径再 `--baseline`**，
   否则该文件不会被纳入基线保护，`--verify-baseline` 也会因 manifest 与跟踪集不符而失败；
   顺序固定为：暂存新增文件 → `--baseline` → `--verify-baseline` → `--check` → 分发相关测试。
   同步只覆盖 Skill（Profile 不参与分发）；`--prune` 仅在明确要求清理运行时多余文件时使用。
   `--apply` 结束时会预编译运行时字节码（`__pycache__` 不在分发集合内，`--check` 口径不变）。
   源码仓库本地同样可以执行 `python3 -m compileall -q scripts`：文档命令都用 `python3 -B`，
   `-B` 只禁止写 `.pyc` 不禁止读，预编译后每条命令少约 130ms 源码重编译；不要为提速去掉 `-B`。
6. 真实模型调用、push、发布、部署均需单独授权；本地 commit 仅在任务明确授权时执行。

## 5. Copilot 闭环规则

- 状态机收敛为 `RUNNING` / `PARTIAL` / `PASS` / `BLOCKED` / `CLOSED`；不得恢复
  `pending_call`、`quota`、`round_seq`、`budget`、`pass_pending` 等远程判定字段。
- **PARTIAL 不是终点**：必须本地派生工作项（`AUTO-C<n>` / `AUTO-R-<id>` / `AUTO-FIX-<F>` /
  `AUTO-VERIFY-<F>` / `AUTO-REPLAN` / `AUTO-NEXT`）并继续推进，直到 PASS 或真实 BLOCKED。
  v8.5 起 `continuation.required=true`（含报告后的 `derive`/`execute`/`close`）一律继续，
  不得以阶段报告或 `report` 收束回合；单项阻塞只冻结该项，不得扩大成整任务 BLOCKED。
  G4 退役后不再派生 `AUTO-G4*`；SOL 认知由主代理按生命周期决定，不是阻塞性工作项。v9 起
  SOL 以建议+命令形式呈现（`advice.action` 为 `consult`/`plan`/`review`），不派生
  `AUTO-SOL*` 工作项，也不因等待 SOL 停止。禁止因轮次、Token、内部费用、等待模型、
  重复评审、子 Agent PARTIAL、首次方案失败或普通测试失败停止。
- `execution_backend` 默认 `legacy`，native 为显式 opt-in；既有协议决定不得擅改。
- **Agent 调度按 `SKILL.md`**：主代理直接执行为默认；`Explore` 只做独立只读调查（不注入
  AGENTS.md，必须提供范围与背景）；`general-purpose` 只承担边界清晰的独立交付物。
  不设调用比例或预算；子代理不可用/PARTIAL 时主代理继续，不原样重复失败派发。
- **G4 已退役**：不再有宿主只读载体要求、权限探测或独立取证门禁；`Explore` 含 Bash、
  `general-purpose` 为 `tools=["*"]`，都只能按普通子代理使用。
- G1–G3 保持 fail-closed：证据不足、绑定不符、门禁未过时必须失败关闭，不得用
  “看起来合理”替代验证；SOL 判断或复核结论也不等于证据、测试或授权判断。
- 执行/复核必须绑定 task / contract / plan / dispatch / work_item /
  `execution_result_sha256`；证据须先登记且属于本任务，文件型证据会重读校验摘要。
- SOL 只提供认知输入（advisory），由主代理核实、修复并做定向验证；不得把 SOL 结论当作
  文件读取证明、宿主权限审计或收口依据。CONSULT / PLAN 不进入 Finding 台账、不影响
  G1–G3，也不得被当作工程决策；采纳结论用 `sol-accept` 登记。
- 正式 REVIEW 仅在用户明确授权时登记；授权在同一任务合同内持续有效，不得成为普通开发
  门禁，也不得用自检冒充。
- SOL 一次调用只发一次请求：失败（429/5xx/超时/网络/截断/格式/模型身份/推理强度或模式
  不符）如实记录，不自动重试、不换 Provider、不派原生 Reviewer 顶替。CONSULT / PLAN
  失败按 `status=unavailable` 降级，主代理继续；REVIEW 失败返回真实非零退出码，只有用户
  明确要求完成独立复核时才按 BLOCKED 报告。
- 不跳过 G1–G3、收口与证据校验；未过门不得标记 CLOSED 或 PASS。测试默认使用本地夹具，
  只有显式真实 E2E 才调用既有授权配置。
- 不重做状态机、门禁、自动驱动或证据协议；需要变更时先说明影响并取得授权。

## 6. 修改与交付纪律

- 只暂存明确路径，禁止 `git add .`；提交前检查暂存 diff，确认无秘密、凭据、真实审计
  日志、临时状态或任务外改动。commit 授权不含 push。
- 不提交 `.env*`、API Key、凭据、`.audit/`、运行时状态、数据库、备份或 `__pycache__`；
  这些由 `.gitignore` 与同步工具排除。
- 交付时区分已完成、未完成与未验证；复杂任务按 PASS / PARTIAL / BLOCKED 给出结论，
  并说明测试、同步、未验证项与剩余风险。
- 文档职责单一：长期决策进 `docs/DECISIONS.md`，当前现场状态进 `docs/STATUS.md`，
  执行规则进本文件，Skill 触发与用法进 `SKILL.md`，详细协议进 `references/`。
  同一规则只保留一处权威定义。
- 不修改其他 Skill、全局 `AGENTS.md`、CPA Provider/模型路由/凭据，也不删除历史审计、
  备份或归属不明的内容；发现敏感信息或归属不清时先保护并报告。

## 文档导航

- 项目定位与开发方式：`README.md`
- 角色职责：`docs/PROJECT-CHARTER.md`
- 阶段目标：`docs/ROADMAP.md`
- 已确认 / 建议 / 待决：`docs/DECISIONS.md`
- 当前证据与未验证项：`docs/STATUS.md`
- Skill 入口与运行契约：`SKILL.md`
- 操作命令：`references/workflow.md`
- 阶段机与持续推进：`references/runtime.md`
- 复核与历史 G4：`references/review.md`
- 原生信封：`references/agent-bridge.md`

# AGENTS.md — Copilot 源码仓库规则

本文件只约束 `<repo>` 仓库。用户级全局 `AGENTS.md` 的授权边界与
安全规则优先；本文件只补充项目定位、角色、模型路由、闭环与交付纪律。详细协议以
`SKILL.md`、`references/` 与 `docs/` 为准，本文件不重复展开。

## 1. 项目定位

Copilot 是运行于 ZCode 内的**轻量协作治理 Skill**：负责任务承接、合同与完成条件、状态机、
证据桥接、审计、PARTIAL 持续推进与 G1–G3 收口。ZCode 原生 Agent 是唯一的工程执行与技术
决策主体；用户侧 ChatGPT 负责需求与产品层目标、交接与验收；认知（CONSULT / PLAN）按生命周期
提供整体判断，用户明确要求时执行一次独立复核（REVIEW），默认由 `gpt-6.2-sol` 零工具单轮调用
完成（`gpt-6.2-astra` 可经 `COPILOT_COGNITION_MODEL` 显式选择，也是默认模型发生**可明确
归因**的模型级/传输级故障时唯一的单次兜底目标）。没有第二认知通道、没有 `--model` 切换；
兜底只有一次、同一认知对象、不写默认模型、不切 Provider、不产生第三次尝试。

当前基线 **v9.15.0**（SOL 主导认知 + Astra 单次兜底：可归因故障由 Astra 接管同一次调用并
如实记录 `requested_model` / `actual_model` / `fallback_used` / `primary_error` /
`attempt_count`；认证、凭据冷却、账户额度、请求构造 4xx、安全终止/用户取消、回答质量不回退
（账户/认证/终止类结构化错误码被包进 5xx 外壳时同样按禁止类归类）；显式 Astra 不
反向回退，退避与复用按模型隔离（兜底结论不复用；缺 `actual_model` 的旧记录按未命中）。
v9.14.0 的默认模型回切 SOL 与 v9.13.1 的认知策略作用域分离不变）。与版本号无关、长期有效的
关键约束：

- **执行与续跑**：任务必须经 `start` / `drive` 进入状态机；PARTIAL 不是终点，
  `continuation.required=true` 时必须继续派生/执行；阻塞按工作项成立，单项缺授权不冻结
  其余工作；`drive` 用 `claims`（短租约）避免并发重复，回填/阻塞自动释放。
- **用户回合必须落地**：用户批准/授权/解除阻塞先由钩子写入任务目录 `.user-inputs.jsonl`
  （`U<n>`，只记录不推断授权），再由 `input --consume U --block-id B --reason`（或
  `unblock --from-input U`）写成 decision 并只解除对应阻塞；`--dismiss` 记录不构成任务
  事实的原因。一条输入只解除它覆盖的阻塞，不猜授权、不改合同范围；`start` 对同 `task_id`
  拒绝重复建任务。Stop 在存在可处置输入时优先 exit 2 要求处置，`drive` 在 BLOCKED 且可
  处置时退出 10——控制流必须真的接通，不能只改提示词。
- **认知是认知引擎，不是门禁**：生命周期为 A 启动 CONSULT → B 调查后 PLAN → C 升级型事件
  CONSULT → E 最终验证完成后的收口 CONSULT（D 普通里程碑复盘已退役，不得恢复）。等级为
  **T0–T3**，第一依据是**用户原始意图**（`start --intent` / `intent --text|--file`，默认不可
  覆盖），再结合目标、合同文字与状态事实；文件类型、工作项数量与默认 `risk=medium` 不能单独
  判成 T0，`docs`/`mechanical` 涉及治理/规则/架构/协作时仍升级。窗口固定：T0 无、T1 仅 A、
  T2/T3 为 A/B/E；意图缺失或无法分类时至少 T1。`cognition_schedule.py` 把窗口物化为义务，
  每条命令最多 1 个义务 / 1 次请求（`--cognition auto|advise|off`，无其他别名）。
  **策略有两个作用域，不能互相写回**：命令行 `--cognition` 或环境变量 `COPILOT_COGNITION`
  只决定**本次调用**的有效策略（覆盖只记入 `policy_overrides` 审计环），**绝不**写任务级
  策略、快照或后续 A/B/C/E 调度；任务级策略只由 `start --cognition` 与
  `cognition-policy --policy … --reason …`（持久化：操作者/时间/原值/原因，写
  `state["cognition_policy"]` 与历史）写入。求值优先级：命令行显式 > 环境变量 > 任务级策略
  > （历史任务）周期快照 > 命令默认。**调度点**为生命周期命令（`start`/`plan`/`record`/
  `rerun`/`criterion`/`unblock`/`reopen`/`closure`/`drive`）：省略 `--cognition` 时按任务策略
  **自动发出真实请求**；纯记录命令（`judgment`/`evidence`/`report`/`accept`/`decide`/`block`/
  `finding`/`contract`）只物化窗口并打印命令，不隐式付费，显式 `--cognition auto` 才执行。
  调用记录带 `tier` / `dispatch`（`auto-obligation` / `manual-obligation` / `manual`）/
  `cognition_family`。同模型、
  同 task_id、同归一化上下文与同问题时复用，不重复付费；复用命中登记到 `cognition_reuses`
  （历史 `sol_reuses` 只读兼容）。义务记录 `opened_by` 与 `tier`，`cognition-stats` 输出
  `expected`/`triggered`/`invoked`/`succeeded`/`failed`/`reused`/`deferred`/`skipped`/`missed`
  与 auto/manual、显式分母的成功率/漏调率、等级/窗口/模式分布与任务类型覆盖、采纳与
  延迟口径。
  义务窗口关闭时写 `disposition`：`not-executed` 用**窗口打开时**的策略
  （`policy_at_open`；历史义务按周期策略快照的变更历史回推，无可证明快照记 `unknown`），
  不用当前配置倒推；`cycle` = 1 + 重开次数，窗口比较不跨周期追溯。义务终态须可审计：
  关闭仍 due 记 `not-executed` + `missed` + `finished_at` + `responsible` +
  `resolution_condition`，`cognition-audit` 只读列出、`cognition-skip` 只登记
  waiver/external/replaced 处置；PASS、G1–G3 与 `cognition-accept` 都不改写义务终态。
  `cognition-accept` 只记录采纳
  事实，**不因代码已改、测试通过或任务 PASS 推断采纳**。CONSULT / PLAN 失败只登记并降级
  （退出码 0）；REVIEW 只在用户明确要求时执行。认知不进入 Finding 台账，不影响阶段与 G1–G3。
- **收口顺序**：最终工程验证（`closure_readiness` 是 E 窗口与收口的唯一准入）→ 收口 CONSULT
  → ZCode `cognition-accept` 独立验收 → G1–G3 fail-closed 收口；工作项全部关闭或 G1–G3 通过都
  不等于验证完成。G4 原生只读复核正式退役，历史记录只读保留；原生复核信封出现时返回
  `native_review_retired`。
- **上下文与恢复**：只读 Context Engine（`resolve`/`recall`/`pack`/`capture`）衔接原生
  Memory、Vault 与工程现场，零模型调用、不写知识库；SessionStart 只读快照与 UserPromptSubmit
  只读提醒都不改写 `state.json`（用户输入只追加到 `.user-inputs.jsonl`），异常一律 exit 0；
  `state.next_step` 是跨回合与 xmc=3 之后的唯一下一步事实来源。
- **唯一源码**：本仓库是唯一源码。`~/.zcode/skills/copilot` 是构建产物，只能由
  `scripts-sync/sync_runtime.py` 单向下发，不得直接编辑。旧 Reviewer 全局 Profile 已退役；
  工作区 `.zcode/agents/independent-readonly.md` 是通用只读能力载体（只声明宿主权限，不定义
  Reviewer 角色或流程），供主代理按需做独立只读调查，普通任务不强制使用，不参与收口门禁。
  该文件不属于同步分发范围。运行时临时修复必须先回迁本仓库，再同步下发；不使用符号链接替代
  副本。源码与运行时的分工见 `docs/DECISIONS.md`。
- **版本沿革**：v9.7.1 及更早的逐版本变更记录、G4 退役过程与认知升级历史见
  `references/history.md`；当前现场状态见 `docs/STATUS.md`，长期决策见 `docs/DECISIONS.md`。
  本文件只保留当前架构与执行规则，不重复历史正文。

## 2. 角色职责

顶层协作角色按 `docs/PROJECT-CHARTER.md`：用户、ChatGPT（用户侧）、Copilot、ZCode 原生
Agent、认知（CONSULT / PLAN / REVIEW）、DesignWise。Executor 是 ZCode 工程执行体系内的
子角色，不是与顶层角色并列的职责；G4 只读复核已退役。

| 角色 | 职责 | 关键限制 |
|---|---|---|
| ZCode 主代理 | 默认执行主体：现场调查、工程规划、技术选型、实现、调试、测试、集成与交付；按需派发子代理 | 不擅自改变产品目标、业务规则或授权范围；不为体现协作强制调用子代理 |
| 内置 `Explore` | 独立只读调查：跨模块/调用链/改动前风险/独立上下文检查 | 工具集含 `Bash`，不是只读沙箱；不注入 AGENTS.md，主代理必须提供范围与项目背景；不修改工程文件 |
| 内置 `general-purpose` | 边界清晰、可独立交付、并行收益明显的子任务 | `tools=["*"]` 并继承 MCP；主代理负责拆分、冲突检查、集成与验收；不能当作只读复核者 |
| Copilot 状态层 | 合同、条件、状态、证据登记、G1–G3、PARTIAL 派生、收口、审计 | 是本地脚本与状态，不是模型；除显式 `cognition-consult` / `cognition-plan` / `cognition-review` 外不调用模型、不实施工程、不替代技术决策、门禁不得绕过 |
| 认知 CONSULT | 默认认知入口：零工具单轮输出目标理解/问题本质/路线与取舍/风险/调查方向/再咨询条件；结论写入统一 `cognition_calls`（advisory） | 只做认知判断；不执行工程、不做技术决策、不写状态、不进入 Finding 台账、不影响 G1–G3；失败只降级不阻断 |
| 认知 PLAN | 调查后的一次零工具规划判断：方案比较、优先级、阶段目标与验收关注点；结论写入统一 `cognition_calls`（advisory） | 不规定具体代码操作；最终技术方案与执行计划由 ZCode 决定；不进入 Finding 台账、不影响门禁 |
| 认知 REVIEW | 用户明确要求时的一次零工具独立审查；结论写入统一 `cognition_calls` 与 Finding 台账（advisory） | 只做 REVIEW；不执行工程、不做技术决策、不自动二审；结论只是建议，不构成收口门禁，也不由阶段或风险自动派生 |
| 正式 REVIEW | 用户明确要求时的治理记录 | 不是普通开发前置门禁；无授权不得伪造 |
| ~~G4 复核~~ | **v8.0.0 已正式退役**；历史记录只读保留 | 不再参与准入、派生或收口；不得把历史 BLOCKED 改写成 PASS |

**模型独立 ≠ Agent 独立。** 共享同一 Provider/模型不等于共享上下文、会话或判断。
子代理与执行方必须是不同的 Agent ID / Child Session / 上下文；独立性由独立上下文与独立
取证保证，不由模型名或 Profile 名称保证。工作区 `.zcode/agents/independent-readonly.md`
只是通用只读能力载体（不作为普通任务的前置条件，也不参与收口门禁）。

## 3. 模型路由规则

- **唯一远程调用是认知（CONSULT / PLAN / REVIEW）**：只有 `scripts/cognition_call.py` 可以导入
  网络客户端、读取本机 CPA 凭据与模型名（默认 `gpt-6.2-sol`，`gpt-6.2-astra` 显式可选且是
  唯一兜底目标，模型常量只在 `cognition_call.py` 定义）；`governance.py`、
  `copilot_dispatch.py`、`copilot_auto.py`、`native_bridge.py`、`native_agent.py` 不得导入
  网络客户端或已退役的认知模块（`call_gpt` / `review_ledger` / `reply_protocol`）。认知只
  接收主代理材料、一次请求（外加至多一次 Astra 兜底接管）、零工具（请求体无
  `tools`/`functions`，`tool_choice="none"`）、`reasoning.effort="high"`，不自动重试、
  不换 Provider、不自动二审、不静默降级模型或推理强度。
  上游回显 `model` / `reasoning.effort` / `reasoning.mode` 与请求不一致时拒绝消费。
  认知失败只如实记录并降级（可归因故障由 Astra 接管一次），不继续切换其他模型、不换 Provider。
- **退避按模型隔离**：窗口键为 `provider + model + error_class`（`state.model_backoff`），成功只清
  本模型；旧 `state.sol_backoff` 单例只读兼容，不写新值、不继承给新默认模型。
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
- **认知模型选择**：默认 `gpt-6.2-sol`；`gpt-6.2-astra` 可经 `COPILOT_COGNITION_MODEL`
  （或显式参数）选择，也是默认模型发生可明确归因的模型级/传输级故障时**唯一**的单次兜底
  目标（认证/额度/请求构造/质量问题不回退，显式 Astra 不反向回退，
  `COPILOT_COGNITION_FALLBACK=off` 可关闭）。注册表外的模型名一律拒绝发送。换默认模型或
  改变兜底策略须用户明确授权，并按 `docs/DECISIONS.md` 记录理由与证据。
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
  G4 退役后不再派生 `AUTO-G4*`；认知由主代理按生命周期决定，不是阻塞性工作项。v9 起认知以建议+命令形式呈现（`advice.action` 为 `consult`/`plan`/`review`），不派生
  `AUTO-认知*` 工作项，也不因等待认知停止。禁止因轮次、Token、内部费用、等待模型、
  重复评审、子 Agent PARTIAL、首次方案失败或普通测试失败停止。
- `execution_backend` 默认 `legacy`，native 为显式 opt-in；既有协议决定不得擅改。
- **Agent 调度按 `SKILL.md`**：主代理直接执行为默认；`Explore` 只做独立只读调查（不注入
  AGENTS.md，必须提供范围与背景）；`general-purpose` 只承担边界清晰的独立交付物。
  不设调用比例或预算；子代理不可用/PARTIAL 时主代理继续，不原样重复失败派发。
- **G4 已退役**：不再有宿主只读载体要求、权限探测或独立取证门禁；`Explore` 含 Bash、
  `general-purpose` 为 `tools=["*"]`，都只能按普通子代理使用。
- G1–G3 保持 fail-closed：证据不足、绑定不符、门禁未过时必须失败关闭，不得用
  “看起来合理”替代验证；认知判断或复核结论也不等于证据、测试或授权判断。
- 执行/复核必须绑定 task / contract / plan / dispatch / work_item /
  `execution_result_sha256`；证据须先登记且属于本任务，文件型证据会重读校验摘要。
- 认知结论只提供参考输入（advisory），由主代理核实、修复并做定向验证；不得把认知结论当作
  文件读取证明、宿主权限审计或收口依据。CONSULT / PLAN 不进入 Finding 台账、不影响
  G1–G3，也不得被当作工程决策；采纳结论用 `cognition-accept` 登记。
- 正式 REVIEW 仅在用户明确授权时登记；授权在同一任务合同内持续有效，不得成为普通开发
  门禁，也不得用自检冒充。
- 认知一次调用最多两次请求：默认模型一次，发生可明确归因的模型级/传输级故障时由
  Astra 接管一次（同一材料/问题/推理等级/零工具），否则失败（429/5xx/超时/网络/截断/
  格式/模型身份/推理强度或模式不符）如实记录，不自动重试、不换 Provider、不派原生
  Reviewer 顶替。CONSULT / PLAN
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

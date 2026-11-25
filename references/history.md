# Copilot Skill 版本与历史（历史记录）

> 本文件记录版本沿革与 SOL 通道变化过程，供追溯，**不是当前规则来源**。现行规则见
> `SKILL.md`、`references/workflow.md`、`runtime.md`、`review.md`、`agent-bridge.md`。
> **SOL 决策通道已于 v6.0.0（2026-11-12）退役；v7.0.0（2026-11-17）只恢复一次性 REVIEW
> （零工具、单轮、建议性）；v8.0.0（2026-11-17）正式退役 G4 原生只读复核；v8.1.0
> （2026-11-18）升级 `gpt-6.2-sol` 并恢复建议性 CONSULT / PLAN；v8.2.0（2026-11-18）
> 把三模式融合为 Copilot 内部统一按需认知能力；v8.3.0（2026-11-18）把“什么时候调用
> SOL”升级为确定性复杂度判断与主动介入；v8.4.0（2026-11-18）把 CONSULT / PLAN 并入
> THINK；v8.5.0（2026-11-19）把 PARTIAL 改为强制续跑的连续执行状态，并把阻塞收窄到
> 工作项范围；v9.0.0（2026-11-20）把 SOL 恢复为按生命周期的默认认知引擎
> CONSULT / PLAN，THINK 只作读取别名；v9.1.0（2026-11-20）完善 SOL 采纳记录
> （实际决策/理由/证据关联/非阻断提示）与质量指标；v9.2.0（2026-11-20）接入
> SessionStart 恢复注入并给 SKILL 上下文瘦身；v9.3.0（2026-11-21）加固合同修订、
> 续跑绑定与条件包装项同步；v9.4.0（2026-11-21）新增只读 Context Engine，衔接原生
> Memory / Vault / Task Memory / 工程现场；v9.5.0–v9.7.1（调度领取、收口时序、有界执行、
> 可选议程与义务终态处置）的完整记录于 2026-11-24 从 `SKILL.md` 版本段迁入本文件**。

<!-- v9.5.0–v9.7.1 条目于 2026-11-24 从 SKILL.md 版本段迁入，正文未改写；SKILL.md 只保留当前版本与跨版本不变量。 -->

## v9.9.0（Astra 默认认知引擎迁移与调用效率优化，2026-11-25）

- **默认模型迁移**：所有认知入口（CONSULT / PLAN / 事件再咨询 / 收口判断 / THINK 别名）默认
  `gpt-6.2-astra`（`codex`、high、standard、Responses API、零工具单轮）。`gpt-6.2-sol` 只作
  显式兼容（`--model gpt-6.2-sol`），不自动回退、不在失败后切换；历史 SOL 记录、审计与历史
  文档只读保留。模型 ID 仍只存在于 `sol_review.py`。
- **模型身份入账与复用门**：记录/失败/审计/请求摘要带 `provider`、`model_key`
  （`codex:<model>`）；复用只命中同模型记录，缺模型归属的历史记录不被当成新模型响应。
- **退避按模型隔离**：`state["model_backoff"]`（`copilot-model-backoff/v2`），键
  `provider + model + error_class`；服务端 `Retry-After` 逐模型独立遵守，成功只清本模型。
  旧 `state["sol_backoff"]` 单例只读兼容，不继承给 Astra：SOL 过载不冻结 Astra，Astra 过载
  也不阻塞显式 SOL 调用。
- **调用语义分离**：纯登记/报告/合同命令默认 `advise`，自动执行只认命令行显式
  `--cognition auto` / `--sol auto`；修掉 `judgment` 无 `--sol` 时默认真发 CONSULT 的路径。
  `start` / `closure` / `drive` 保持默认 `auto`。
- **复用与指标**：复用键含 `task_id` + 归一化上下文 + 模型 + 提示词版本；命中写
  `sol_reuses`；`sol-stats` 增加 `by_model`（Token、耗时、重复调用）与 `reuse`。
- **完整性**：`incomplete` / `max_output_tokens` / 空文本仍记为失败；24000 输出预算与 300 s
  超时不变。未改 CPA 配置、OAuth、Provider 或模型路由。

## v9.7.1（认知记录与义务处置的终态闭环）

- **历史采纳只补可证明的结论**：`sol-accept --historical` 只为**已收口**任务开放，`pending`
  必须写明原因（`--reason` + 可选 `--reason-code`：`no-attribution` /
  `superseded-within-cycle` / `later-cycle-superseded` / `insufficient-record`），
  `accepted/modified/rejected` 也必须有任务内依据。重复执行完全相同的登记判 `duplicate`
  幂等返回（不追加历史、不改登记次数）；既有已决结论不被覆盖、不被降级为 `pending`；
  正常入口对 CLOSED 任务的拒绝不变。**不因代码已改、测试通过或任务 PASS 推断采纳**——
  无法归因的一律保留 `pending` 并写明理由，不以 pending 数量下降为目标。
- **义务终态处置**：窗口关闭时写 `disposition`（`completed` / `covered` / `skipped` /
  `failed` / `not-executed` / `unsettled`）+ `disposition_reason` + `disposition_at`，
  已有处置不覆盖。`not-executed` 的理由引用该周期的策略快照
  （`schedule.policy_snapshots[<cycle>]`，来自 `--sol` / `COPILOT_SOL_AUTO`）；**没有快照
  的历史义务记 `unknown`**，不用当前配置倒推。`due` 不等于强制调用：策略 `off`/`advise`
  下义务保持 due，但快照让事后能分清“策略关闭”与“漏调”。
- **未能调度留痕**：条件成立但本次没执行（SOL 锁被占用等）只累加 `deferrals` 与
  `last_deferral_reason`，义务仍 `due`，窗口关闭时进入处置理由；不静默清除、不冒充失败。
- **收口周期隔离**：义务带 `cycle`（1 + `reopen_history` 条数），窗口比较只在同周期内；
  授权重开后的收口判断不追溯覆盖上一周期的窗口，旧周期义务保持原周期与原事实。
- **不阻断**：处置与周期只进 `summary`（`dispositions` / `by_cycle` / `policy` /
  `cycle`）与 `sol-stats`（`dispositions`、`by_cycle`、`reopened_tasks`）观测值，不改
  `pending` / `missed`、不改 coverage 分母、不影响 G1–G3 与收口。历史义务可只读推导处置，
  不回写历史状态。
- **不变量**：G1–G3、`closure_readiness` 收口准入、E 前 `sol-accept`、PARTIAL 续跑、
  SOL advisory、REVIEW 仅用户明确要求、G4 退役、零工具单轮、Provider/模型/凭据与授权
  边界全部不变；本轮未获推送、发布或系统配置修改授权。

## v9.7.0（SOL 主动认知与实战决策闭环）

- **复盘依据**：对 2026-11-22–11-23 的 40 个任务、68 次 SOL 调用（447k tokens、累计 4,618.6 s）
  做了逐条核对。问题不是“SOL 判断质量差”，而是四个确定性缺陷：材料不带历史判断（同一问题
  重复付费）、收口后仍追加 B/D 窗口（7 次非收口调用发生在收口判断之后）、
  `blockage-change`/`repeated-work-failure` 没有事件身份与专属问题、34 次已完成调用
  的采纳结果不可见。
- **可选议程**：CONSULT / PLAN 的输出契约新增可选 `agenda`（1–6 条，固定类型：核心问题、
  错误前提、遗漏风险、完成条件补充、取舍、最值得验证的事实、关键反例），SOL 只给一句话级
  认知提示；`governance.evaluate` / `status` 只读呈现 `sol_agenda`，不派生工作项、不进计划
  SHA、不影响 G1–G3。议程缺失时输出契约与旧调用方完全兼容。
- **认知连续性**：调度材料新增“上次 SOL 判断与采纳”（最近三次的判断、采纳状态、决策与
  实际差异）与“未处理议程”；`build_prompt` 也注入同样的事实段。差异行补充新增决策与
  未解除阻塞变化，避免同一事实被反复提问。材料口径升为
  `copilot-sol-schedule-materials/v3`（参与收口验证摘要，旧 E 结论按既有规则失效）。
- **收口顺序**：同一收口周期（合同 + 重开次数）已有收口判断后，不再推荐 B 规划或 D 里程碑；
  被收口覆盖而仍未执行的 B/D 义务按 `skipped/closure-covered` 结算，不再误记为漏调。升级型
  事件（重规划、风险升级、合同修订、重开等）仍可打开新的 C 窗口。
- **事件质量**：`blockage-change` 只由“新的或实质变化的未解除阻塞”触发，单纯解除阻塞不再
  制造事件；`repeated-work-failure` 保持每阶段一次（第 2、3+ 次失败），两者都有专属问题，
  普通测试失败与常规进展不触发。
- **采纳可见**：`evaluate` 输出 `sol_pending_acceptance`，`status` 提示未回填的已完成认知
  调用并给出 `sol-accept` 入口（只提示、不阻断）；E 收口的采纳门语义不变。
- **可观测值**：`sol-stats` 新增 `agenda`（有议程的调用数/条数/未处理条数）、
  `pending_acceptance`、`post_closure_calls`（收口后仍发生的非收口认知调用，应为 0）；
  schema 升为 `copilot-sol-stats/v2`，明确这些观察值不证明判断价值。
- **经验闭环**：`context_engine.py capture` 的候选草稿包含任务内 SOL 判断与采纳痕迹
  （含议程与证据溯源），并注明“任务内事实，不是可复用结论”；不写 Vault、不替代知识写入授权。
- **不变量**：G1–G3、`closure_readiness` 收口准入、PARTIAL 续跑、SOL advisory、REVIEW 仅用户
  明确要求、G4 退役、零工具单轮、Provider/模型/凭据配置与授权边界全部不变。

## v9.6.2（统计口径修正、SOL 锁等待有界、文档与死代码收敛）

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

## v9.6.1（收口时序：最终验证完成才执行收口 CONSULT）

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

## v9.6.0（SOL 生命周期主动调度：义务化、有界执行、覆盖率统计）

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

## v9.5.1（宿主集成修正：startup 恢复、钩子可发现性、工作单条件归属）

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

## v9.5.0（持续推进：调度领取、恢复提示与阻塞边界）

- **调度领取（claims）**：`drive` 在 RUNNING 阶段把工作项领取落盘（`claims`：owner 会话、
  `claim_id`、`expires_at`、`handoff_count`；默认 TTL 1800s，`COPILOT_CLAIM_TTL` 可调）。
  同一工作项不会被两个会话同时领取；其它会话的有效租约不抢占，到期自动回收；`record` /
  `rerun` / 条件 `met` / `block` 自动释放。**领取是交办事实，不是执行证明**，工作项终态
  仍以 `status` / `result` / 证据为准。只剩通用 `AUTO-NEXT` 兜底且具体缺口已被其它会话
  领取时，`continuation.action=wait`（`required=false`）且 `drive` 返回 11，不重复规划。
- **计划目标条件**：`plan` 工作项可声明 `target_criteria`（必须真实存在于合同），用于领取
  去重、阻塞自动归属；该字段不进入计划摘要，native dispatch 的计划指纹保持不变。
- **UserPromptSubmit 逐回合提示**：`install` 写入 Stop / SessionStart / UserPromptSubmit
  三个事件；`user-prompt` 只读列出**本会话负责**的未闭合任务（含 BLOCKED 的 reason/needs）
  与授权判断规则；其它会话/未绑定的旧任务不逐回合展开、也不重复披露（现场与阻塞原因由
  SessionStart 指针披露一次），本会话另有任务时它们只占一行计数。归属无法确认
  （`session.json` 损坏，Stop 会 fail-closed 拦截）的任务仍逐回合提示；宿主未给会话标识时
  只有**恰好一个**候选任务才当作本会话的展开，多个候选只报数量、让先 `status --json`
  核对归属（与 Stop 同一口径：不按“最近更新”替会话选任务）。
  用户本回合补齐授权/决策时，先用其原话登记 `unblock --by user --quote "<原话>"`，再
  `drive` 继续；提示本身不是用户新指令，也不构成授权。没有本会话任务时不注入。
- **恢复可发现性**：SessionStart(startup|resume|compact) 对本会话绑定任务注入完整快照，startup 只给指针；
  或重启后会话标识变化时只注入只读指针（任务阶段、可执行项、阻塞原因/需要、归属标注），
  不绑定、不接管、不强制续跑。快照与 status 均显示阻塞原因与所需条件。
- **阻塞边界与解除来源**：仍有可执行工作项时，`block --scope task` 被拒绝，必须显式
  `--force` 并记录 `ready_at_creation` / `forced_scope`；单项授权缺口走 `--work-item`。
  `unblock --by user` 必须同时给出 `--quote`，状态记录 `resolved_by`、
  `authorization_source=caller-declared` 与原话，供审计核对。
- **不变量**：G1–G3、PARTIAL 强制续跑、SOL-first advisory、证据完整性、授权边界、ZCode
  唯一工程执行主体与 G4 退役状态全部不变。

## v9.4.0（2026-11-21）项目上下文与长期记忆衔接

- **背景**：Copilot 已有合同/条件/证据/状态，但“这是哪个项目、以前确认过什么、压缩恢复
  后丢了什么、子 Agent 需要知道什么、这次经验该不该沉淀”仍靠模型自觉：原生 Memory 只在
  主会话每轮注入，子 Agent 实测不继承 Memory/AGENTS，`knowledge_handoff.py` 只校验渲染，
  SessionStart 快照不含项目身份与工程基线。
- **新增**：`scripts/context_engine.py` 的 `resolve` / `recall` / `pack` / `capture`——
  `sha256[:16]` 项目记忆定位、白名单有界召回（零命中/失败/截断可区分，文件集摘要 + TTL
  缓存）、三档字符上限上下文包（self 12k / sol 8k / subagent 5k）、带任务与证据溯源的
  候选草稿；零模型调用、无网络、不建记忆库。
- **接线**：SessionStart(resume|compact) 快照追加项目现场/Memory 可用性/召回与交接命令，
  只读且失败省略；`status` 人工输出加非阻断提示，JSON 契约不变；`docs/CONTEXT-ENGINE.md`
  记录四层职责、数据流与 C1–C10 验收映射。
- **边界**：不接管宿主记忆抽取、不改知识库写入规则、不自动在 `start` 强制召回、不新增
  Provider；G1–G3、PARTIAL 续跑、SOL-first advisory 与 G4 退役状态不变。

## v9.3.0（2026-11-21）治理一致性与续跑绑定加固

- **背景**：v9.2.0 之后对治理主链路做了一次自审：合同可被“改写成更弱表述”绕过完成条件，
  条件核销后包装项仍 pending 导致永不 PASS，重开后旧 PASS 可立即复用，多阻塞共用一个
  `blocked_by` 字段，Hooks 在命令失败/只读命令后也绑定会话，SOL 单轮复用键对材料空白与
  模板变化不敏感。
- **合同修订授权门**：`amend_contract` 改为净未核销条件计数（不含 PWW 接受条件），净减少
  或 `test_policy.required` 由真变假视为破坏性修订，必须 `authorized_by="user"`；历史记录
  新增 `authorized_by`、`previous_criteria_sha256`、`removed_criteria`、
  `test_policy_weakened`。改写措辞仍留审计痕迹但不要求授权。
- **条件-包装项同步**：`_sync_criterion_work_items` 在条件 met 时把无可执行结果的
  `AUTO-C<n>`（或 `targets` 含该条件的自动项）置为 `dropped` 并记 `auto_closed_for`；
  条件回退时重开。计划内业务工作项与已开工包装项不受影响。
- **重开进展基线**：`reopen` 记录 `_progress_snapshot`（证据数、终态工作项、条件状态）；
  `_reopened_without_progress` 在 `evaluate` 与 `close_task` 两处 fail-closed，无实质进展
  时返回 PARTIAL / 拒绝收口；旧记录无基线时退回严格时间戳比较。
- **多阻塞与恢复家族**：`resolve_block` 只在没有其它未解除 block 引用该工作项时恢复，
  否则更新 `blocked_by`；`derive_work` 对 `AUTO-R-*` 逐层剥前缀并归一到
  `AUTO-R-<base>-R<n>`，避免重启后嵌套膨胀。
- **Hooks 绑定加固**：`copilot_dispatch.py` / `copilot_auto.py` 只在命令成功（0/10）且非
  只读后 `touch_session`；只读集合新增 `quality`，`evidence` 无 `--add/--drop`、`contract`
  无 `--amend` 视为只读；`find_tasks_root` 遇 `.git` 停止，不再继承祖先工作区任务；
  `session-start` 只处理 `bound` 与 `corrupt` 绑定，未绑定异常不注入；摘要按行截断；
  `check` 捕获任何异常后按 fail-closed 退出 2 并记录 `internal-error`。
- **SOL 与 CLI**：`review_subject_sha256` 对材料 `strip()` 后哈希并把
  `prompt_template_sha256()` 纳入 subject；`--force` 优先于复用判定；`_is_local_cpa_url`
  拒绝 query/fragment 与 `/v1` 之外路径；`plan`、`finding`、`accept`、`reopen`、
  `contract --amend` 统一返回 `EXIT_CONTINUE`。
- **不变量**：G1–G3、PARTIAL 强制续跑语义、SOL-first 生命周期与 advisory 边界、零工具
  单轮、G4 退役状态与授权边界均未改变。

## v9.2.0（2026-11-20）ZCode 原生恢复衔接与上下文瘦身

- **SessionStart 恢复注入**：`copilot_stop_guard.py session-start` 只处理
  `source=resume|compact`，对绑定本会话且 `continuation.required=true` 的任务注入一段只读
  快照（阶段、未闭合条件、续接命令）；不写状态、不消费 Stop 次数，异常退出 0；`install` /
  `uninstall` 同时维护 `Stop` 与 `SessionStart`。
- **SKILL 瘦身**：版本沿革外移到本文件，SKILL.md 84,291 → 56,707 B（−32.7%），规则与命令面
  未删减。
- **宿主实测**：headless `--resume` 确认宿主拼接恢复快照并继续由 Stop 守卫续跑（共 44 次
  请求、3.33M input tokens，不宜无预算复跑）；`compact` 与 GUI 新会话未单独复现。

## v9.1.0（2026-11-20）SOL 采纳记录与证据关联

- **背景**：v9.0.0 的 `sol-accept` 只记录 status/note/reason，实际任务中出现“建议已记录、
  决策已发生、但采纳记录没有关联 E1/E2 等证据”的追溯缺口。
- **记录 v2**：`copilot-sol-acceptance/v2` 新增 `decision`、`rationale`、`delta`、
  `evidence_refs`、`source_refs`、`evidence_note` 与 `advice_ref`；原建议沿 `call_id`
  引用，不复制正文；旧字段与旧 CLI 参数保持可用。
- **证据解析**：登记时逐条判定 `linked` / `unresolved` / `archived`，未登记或归档引用
  只作待核引用，不自动补登记、不进入正式证据链；来源定位只记录，不冒充已核验证据。
- **非阻断提示**：缺实际决策、缺理由、modified 缺差异、暂无证据、引用未登记/已归档时
  输出 warnings 与中文提示，登记成功、退出码 0，不改变阶段与 G1–G3。
- **统计**：`sol_cognitive.acceptance_quality` 分开报告完整率与证据关联率，并披露
  source-only、无证据、待核、归档与排除的旧版记录数；原四态与 `adopted` 不变。
- **历史兼容**：旧采纳记录只读可读、不计入质量分母、不迁移、不重写、不重放已 CLOSED 任务。
- **不变量**：SOL-first 生命周期、零工具、单轮、advisory、G1–G3、授权边界均不变。

## v9.0.0（2026-11-20）SOL-first 默认认知引擎

- **模式**：`SOL_MODES=("CONSULT","PLAN","REVIEW")`；`THINK` 只作读取别名归一到 CONSULT；
  CONSULT / PLAN 与 REVIEW 共享单轮零工具管线、`sol_calls` 记录与审计。
- **生命周期**：起点 CONSULT（understanding）→ 调查后 PLAN（planning）→ 新证据/方案失败时
  动态 CONSULT（development）→ 高复杂阶段复盘（stage）→ 收口综合判断（closure）。
- **路由**：`--task-kind` 16 类；简单/机械类型（trivial/mechanical/docs/format）0 调用，
  未声明按 `standard` 非简单处理；没有硬性次数或 Token 上限，重复调用需说明决策价值。
- **采纳与统计**：`sol-accept` 记录 accepted/modified/rejected；`review-metrics` 的
  `sol_cognitive` 输出覆盖率、重复率、采纳、usage/latency（缺失标 `unavailable`）。
- **降级**：CONSULT / PLAN 失败登记后退出码 0，不阻断工程推进；REVIEW 失败仍非零。
- **不变量**：零工具、单轮、`gpt-6.2-sol` + `high` + `standard`、CPA `127` 出口；
  G1–G3、授权边界与 G4 退役状态不变。

## v8.5.0（2026-11-19）PARTIAL 强制续跑与按工作项阻塞

- **背景**：`ad-opt-sdk-2026-11-19` 未建立状态机，以自由文本阶段报告收束；机制上任何
  未解除 block 都会冻结全部工作，`report` 恒返回 0。
- **续跑判定**：`evaluate` 输出 `continuation`；`record`/`rerun`/`criterion`/`report`/
  `block`/`unblock` 以 `EXIT_CONTINUE=10` 提示仍有下一步；`drive` 只在真实等待时退出 11。
- **按工作项阻塞**：`block --work-item` / `--criterion` / `--scope task`；单项缺授权不再
  冻结整个任务；`unblock` 自动恢复冻结项；整体 BLOCKED 需全部剩余工作受阻。
- **派生收敛**：阻塞条件不派生 `AUTO-C<n>`；已有 `AUTO-REPLAN` 时不再重试同一路径。
- **不变量**：G1–G3、SOL 语义、授权边界、G4 退役状态不变。

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

## v8.0.1（2026-11-17）SOL 安全与可靠性加固（历史）

v8.0.1 只收紧 SOL 安全与可靠性（错误详情脱敏、base_url 绕过、advisory 提级、重复付费
防护、台账幂等、ID 唯一、dry-run 校验），不改变任务合同、G1–G3 与授权边界。
细节见 `legacy/` 与 Git 历史（f62f375 / c44c13a / 20db0ba）。

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

## v6.3–v6.12.0（2026-11-13～11-17）复核收敛与宿主能力沿革（历史）

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

v6.3 基于 10 个真实任务的复核复盘（某广告策略项目 7 轮复核、某广告项目 7 次 G4 尝试、某复核专项
4 次正式 REVIEW、gromore / 腾讯 / 某投放链路 等）收敛“无效评审多、重复取证”的机制问题：补齐
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

# runtime：v8 阶段机、驱动与持续推进

本文说明 Copilot v8 的**本地运行时语义**：阶段如何判定、PARTIAL 如何派生工作、
驱动如何收敛、状态如何保真恢复。命令与参数见 `workflow.md`；复核门禁见 `review.md`。

## 1. 设计原则

- **本地判定**：阶段判定、工作项派生、门禁检查只读状态文件与证据文件，不调用网络、模型或
  子进程（命令采集只在 CLI 显式请求时执行）。`sol-consult` / `sol-plan` / `sol-review`
  是仅有的例外：结果统一写入 `sol_calls`（历史 `sol_reviews` / `sol_advisories` 只读
  兼容），不自动改变阶段、不参与 PARTIAL 派生；CONSULT / PLAN 也不进入 Finding 台账或
  G1–G3。SOL 调用失败只登记真实原因，不产生阻塞或派生工作项。
- **SOL 由生命周期调度，不靠文案提醒**：v9.6 起 CONSULT / PLAN 按阶段物化为**调度义务**
  并持久化在 `state.sol_schedule`；生命周期命令在自身事务内**有界执行**（每条命令最多
  1 个义务、1 次请求），而不是只打印命令等主代理记得执行。义务执行与否都不改变阶段、
  门禁、BLOCKED 判定或 `continuation`，详见 §5。
- **PARTIAL 不是终点**：PARTIAL 表示还有工作可做，运行时必须派生下一步，而不是停止或
  等待外部判断。
- **无人工上限**：没有轮次上限、Token 预算、费用阈值或评审次数限制。唯一的循环保护是
  `MAX_DRIVE_STEPS=32`：连续 32 步仍未收敛时返回内部错误（退出码 12）并保留状态，要求
  检查工作项是否被反复派生——它不是任务终止、不是 BLOCKED、不是 PASS。
- **fail-closed**：字段缺失、摘要不符、绑定过期一律拒绝，不用“看起来合理”替代校验。
- **提醒不阻断**：`evaluate` 在阶段结果里附带 `warnings`（例如合同源文件被改动），只提示
  不同步，不改变阶段、不参与 G1–G3。修复后的重新验证用 `rerun` 重开工作项，旧结果留在
  `rerun_history`，不做静默覆盖。

## 2. 阶段判定顺序

`evaluate(state)` 按固定顺序判定，命中即返回，并在结果中附带 `continuation`：

| 顺序 | 条件 | 阶段 | 说明 |
|---|---|---|---|
| 1 | `closure.closed` 为真 | CLOSED | 已收口；继续需用户授权 `reopen` |
| 2 | 存在未解除的**任务级** block | BLOCKED | 输出原因与 `needs`，退出码 11 |
| 3 | 合同缺失或条件未确认 | PARTIAL | 派生 `AUTO-CONTRACT` |
| 4 | 存在可领取工作项 | RUNNING | 输出工作单，退出码 10 |
| 5 | 全部剩余工作受阻且无可推进条件 | BLOCKED | 项级阻塞覆盖所有剩余工作；退出码 11 |
| 6 | 仍有未闭合（未被阻塞）条件 | PARTIAL | 派生 `AUTO-C<n>` |
| 7 | 存在停摆工作项 | PARTIAL | 派生恢复项 |
| 8 | G1–G3 全部通过 | PASS | 收口为 CLOSED |
| 9 | 其他门禁未过 | PARTIAL | 按缺口派生 |

- **项级阻塞不短路**：`ready_actions` 只跳过被 `work_items` 冻结的工作项及其无法开始的
  下游依赖，其它工作照常可领取；只有 `--scope task` 的阻塞才冻结全部工作。
- **可领取（ready）**：`status=pending`、不在项级阻塞名单、且所有 `depends_on` 已完成。
- **停摆（stalled）**：`running`/`failed`/`blocked` 的遗留项；或 `pending` 但依赖不存在、
  依赖已 `dropped`/`failed`/`blocked`。被项级阻塞的项及其下游不算停摆（已登记的真实阻塞）。
- **整体 BLOCKED 的条件**：全部非终态工作项直接阻塞或因依赖无法开始，且没有未被 `criteria`
  声明的未闭合条件；否则先派生 `AUTO-C<n>`（宁可继续也不提前停）。
- **PARTIAL 永远有出口**：即使没有 ready 项、没有未闭合条件、没有停摆项，也会派生
  `AUTO-NEXT` 要求重新核对目标、证据与真实阻塞。

### continuation 判定

`continuation(state)` 只读状态，输出 `phase` / `required` / `action` / `executable` /
`derivable` / `blocked_items` / `dependency_blocked` / `next_command` / `reason`：

| 局面 | action | required |
|---|---|---|
| CLOSED | `none` | false |
| 所有剩余工作真实阻塞 | `wait` | false |
| PASS | `close` | true |
| 有 ready 项 | `execute` | true |
| 无 ready 但可派生（dry-run 预演） | `derive` | true |
| 具体缺口已被其它会话领取 | `wait`（`claimed_by_others`） | false |
| 无 ready 且无可派生 | `none` | false |

`evaluate`、`next`、`status`、`drive`、`record`、`rerun`、`criterion`、`report`、`block`、
`unblock`、`plan`、`finding`、`accept`、`reopen`、`contract --amend` 都使用同一判定。
`EXIT_CONTINUE=10` 表示"还有下一步"，不能据此结束回合；`EXIT_WAIT=11` 只在
`required=false` 的真实等待时使用。任何会推进状态并留下续跑要求的命令都以 10 结束：
把它们当 0 会让调用方把一次回填或计划登记误读成任务终点。

`reopen` 之后旧 PASS 不能直接复用：`evaluate` 在门禁全绿但**自最近一次重开后没有新证据、
没有新的终态工作项、也没有条件重核销**时返回 PARTIAL（`任务重开后尚未产生新的实质进展`），
`close_task` 用同一判定 fail-closed。进展基线在 reopen 时按计数采集（证据数、终态工作项、
条件状态），不依赖时间戳，同秒内重开与推进也不会误判。

## 3. PARTIAL 派生规则

`derive_work(state)` 一次最多派生 `MAX_DERIVED_PER_CALL=12` 项，全部 `assignee=zcode`、
`origin` 标明来源、`derived_reason` 记录原因，并写入 `history`：

| 触发 | 工作项 id | 目标 |
|---|---|---|
| 合同缺条件/未确认 | `AUTO-CONTRACT` | 补齐可核销条件与边界 |
| 条件未闭合 | `AUTO-C1`、`AUTO-C2`… | 执行工作、登记证据、`criterion met` |
| 最新报告为 PWW、绑定当前且接受条件未闭合 | `AUTO-ACCEPT` | 把原始结论与全部 W/R 如实呈现给用户，取得用户明确接受后 `accept --by user`；执行方不得代接受。非 PWW 或绑定失效时改派 `AUTO-QUALITY` |
| 质量报告替代/代码范围/工作区/接受门禁未过 | `AUTO-QUALITY` | 重新 verify、声明替代与解决、确认任务工作区并用当前代码快照重新接入 |
| 停摆工作项 `A1` | `AUTO-R-A1` | 分析失败根因并重试/调整/记录真实阻塞；恢复项按根因家族归一，`AUTO-R-W1-R2` 的下一次恢复仍是 `AUTO-R-W1-R3`，不会套出 `AUTO-R-AUTO-R-…` |
| Finding 台账有 `open`/`regressed` 的 blocking/major | `AUTO-FIX-<F>` | 修根因、登记修复证据并标记 fixed，不直接重跑完整复核 |
| Finding 已 `fixed` 但未验证 | `AUTO-VERIFY-<F>` | 只跑对应定向测试/检查，登记证据并标记 verified |
| `AUTO-C*` / `AUTO-R-*` 已派生 ≥3 次仍不闭合 | `AUTO-REPLAN` | 根因分析 + 实质不同的替代路线；替代路线执行中不派生同路径尝试，替代路线已结束而缺口仍在时派生新的 `AUTO-REPLAN` |
| 无其他缺口 | `AUTO-NEXT` | 明确下一步最小可执行动作 |
| 存在连续失败工作项 | `AUTO-REPLAN` | 同上 |

- 重名时用 `-R2`、`-R3` 递增（`_derive_id`），不覆盖已有工作项。
- 同一缺口已有 `pending`/`running` 工作项时不再派生第二条；重复调用 `drive` 或多个入口
  同时求值不会把 `AUTO-C1` 复制成 `-R2`/`-R3`。
- 已随 `block --criterion` 声明为阻塞的完成条件不再派生 `AUTO-C<n>`，避免制造无法执行的
  工作单；解除阻塞后自动恢复派生。
- **已被有效领取的条件不再派生**：领取中的工作项若通过 `target_criteria`（计划期声明）或
  `AUTO-C<n>` 隐含承载某条件，该条件在本轮派生中跳过；租约到期或被释放后自动恢复派生。
  只剩通用 `AUTO-NEXT` 兜底、且具体缺口都在其它会话手里时，`continuation` 返回
  `action=wait`（`required=false`），`drive` 退出 11，不制造第二个“重新规划”工作项。
- **计划工作项可声明 `target_criteria`**：值必须是合同内真实存在的条件；用于领取去重与
  阻塞自动归属。该字段不进入计划摘要，既有计划指纹与 native dispatch 绑定不变。
- 已有 `pending`/`running` 的 `AUTO-REPLAN` 时，同一缺口不再派生第 4 次同路径尝试，等替代
  路线产出结果；替代路线已结束（`done`/`failed`/`dropped`）而缺口仍未闭合时，派生新的
  `AUTO-REPLAN` 并要求说明上一路线为何未闭合，不由通用 `AUTO-NEXT` 兜底静默续跑。
- G4 退役后不再派生 `AUTO-G4*`；按生命周期主动建议的是 CONSULT / PLAN，REVIEW 只在用户明确要求
  时执行；两者都不是阻塞性工作项。
- 失败/停摆的 `AUTO-C*` 项不再额外派生恢复项，避免同一缺口两份工作单。
- **条件核销同步包装项**：`criterion --status met`（以及 reopen 把接受条件退回未闭合）会
  同步它自动派生的 `AUTO-<cid>`。仍为 `pending`、无结果无证据的包装项标记 `dropped` 并写明
  `随完成条件 C<n> 核销自动撤销`（不伪造 `done`）；条件重新未闭合时恢复为 `pending`。
  已产出结果/证据的包装项、以及计划里人写的工作项（即使声明 `target_criteria`）不受影响。
- 派生项带通用约束：不得越过原始授权范围、不得改目标/条件/业务规则、只做缺口需要的最小
  改动；若合同写有 `boundaries`，原样带入。
- 派生项 `depends_on` 为空：它们是对缺口的直接行动，不阻塞其他工作。

## 4. 驱动循环（`copilot_auto.py drive`）

1. 读取状态 → `evaluate`；
2. CLOSED → 打印状态，退出 0；
3. BLOCKED → 打印原因与 `needs`；若 `continuation.required` 为真（口径不一致）抛出内部
   错误，否则退出 11；
4. PASS → 先查最终验证准入（`closure_readiness`）：有缺口就打印缺口并退出 10，既不执行
   E 也不收口；准入通过才按 §5 消费 family E 的收口认知窗口（`--sol auto` 时真的执行一次
   CONSULT），再 `close_task`，退出 0；
5. RUNNING → 先按 §5 有界消费 1 个到期义务（每次 `drive` 最多一次，只展示或执行，
   不改变阶段），再取前 `--max-actions` 个 ready 项，写入调度领取并把工作单打印出来；
   `backend=native` 且工作项属于已登记计划时，经 `native_bridge.build_dispatch` 写出
   `dispatch.json`（先派发、后领取，避免计划漂移）；退出 10。
6. PARTIAL → `derive_work`；若派生成功则 `begin_iteration`（记录本轮工作项与证据快照，
   并把上一轮未结束的迭代标记为 `ENDED`）并保存，比较 `progress_digest`；若派生结果为空，
   退出 12；
7. 循环最多 32 步，超限退出 12 并保留状态。

回填类命令（`record` / `rerun` / `criterion` / `report` / `block` / `unblock`）在写入后
调用同一 `continuation` 判定：仍须执行时返回 10，并把下一步命令与工作项打印出来；只有
已收口或所有剩余工作真实阻塞时返回 0。

**进展摘要 `progress_digest`**：由工作项状态/结果摘要/证据引用、条件状态/证据引用、
证据 id 与摘要、收口状态计算。派生前后摘要不变且没有 `AUTO-NEXT` 时输出警告；同一缺口
反复派生（`AUTO-C*` / `AUTO-R-*` 各自累计 ≥3 次）会附带 `AUTO-REPLAN`，
要求换路线而不是重复执行。

**调度领取（`state.claims`）**：`drive` 在 RUNNING 阶段把本次交办写入短租约，记录
`work_item_id`、`owner`（会话标识；无会话时为 `local`）、`claim_id`、`acquired`、
`expires_at`、`handoff_count`。默认 TTL 1800s，可用 `COPILOT_CLAIM_TTL` 调整（0 表示立即
过期，便于测试与快速回收）。语义：

- **不是执行证明**：领取只表示“已交给某个会话”；工作项终态仍由 `status`/`result`/证据决定。
- **不重复交办**：其它会话的有效租约不会被抢占，`ready_actions` 对非领取方隐藏该项；
  重试/再次 `drive` 可由同一领取方续租（`handoff_count` 递增）。
- **不会永久锁死**：租约到期自动失效；`record` / `rerun` / 条件 `met` / `block` 会删除
  对应领取，进程崩溃最多等到 TTL 结束即可重新领取。
- **等待不是阻塞**：具体缺口都被其它会话领取时，本会话 `continuation.action=wait`
  （`required=false`），`drive` 退出 11；这不是整体 BLOCKED，也不需要用户授权。

**Stop 续跑守卫（`copilot_stop_guard.py`）**：退出码 10 只在调用方读取时才有效；模型可以直接
结束回合。守卫把同一判定接到 ZCode `Stop` 钩子：stdin 读事件 JSON（`session_id`、`cwd`、
`stop_hook_active`），取 `cwd` 向上 4 层找 `.copilot-tasks`，对每个 state 求 `continuation`；
任一 `required=true` 就向 stderr 写续跑理由并以退出码 2 阻止结束（ZCode 取 stderr 作为
`decision=block` 的 reason，截断 4000 字符）。守卫只读，不写入 state 与绑定文件。

会话绑定只在实际推进成功后写入（失败的命令、只读命令与 `--list`/`--show` 子动作都不写），
并且先判归属再读 state：不属于本会话的任务即使 state 损坏也不会误拦当前会话。向上查找
`.copilot-tasks` 到 Git 根即止，不继承父目录里别的项目的任务集。

SessionStart（`startup|resume|compact`）对绑定当前会话且 `continuation.required=true` 的任务
注入完整快照；startup 或未绑定会话只给只读指针；
本会话任务自己的 state/绑定损坏时仍给出修复提示（不静默丢掉待续跑任务），其它任务的异常
不进入本会话上下文。

注入理由固定包含：3 次上限说明、归属、下一步动作、下一条命令、**可原样粘贴的续接命令**
（`python3 -B <runtime>/scripts/copilot_auto.py drive --state <绝对路径>`）、一次拦截只做
一个最小可验证工作单元、结束前必须落盘（进展/证据/未完成项/原因）、不得把 PARTIAL 改写成
BLOCKED/PASS 规避上限、以及新回合先 `status --json` 核实再继续。守卫不持久化计数器。

**单回合上限（`xmc=3`）**：ZCode 每回合最多接受 3 次 Stop 自动续跑（发行版硬编码，无配置项，
钩子拿不到已用/剩余次数）；第 4 次及以后的退出码 2 被宿主忽略，`stop_hook_active` 只能说明
"本回合已被续跑过"，不能推出剩余次数——因此守卫与文档都不声明"还剩几次"或"这是最后一次"。
用满 3 次仍未完成时，同一 `--state` 就是续接点，由用户在新回合粘贴续接命令继续。

**归属**：Copilot CLI 会改写状态的命令把 `ZCODE_SESSION_ID` 追加到任务目录 `session.json`
（最多保留 20 个会话，只读命令不写）。守卫只拦截本会话绑定过的任务；其它会话、其它工作区、
未绑定的旧任务都不拦截——不用时间过期，因此不会因为人为时间上限放弃仍有可执行工作的任务。
没有会话标识（手工调用）时只有恰好一个候选任务才生效；有多个候选时不按“最近更新”替会话
选任务，只记 `ambiguous-no-session` 并放行，避免把别人的任务强制续跑。

**放行**：无任务、CLOSED、BLOCKED/`wait`、`stop_guard=false`、任务或工作区目录
`.stop-guard-off`、`COPILOT_STOP_GUARD=0`、`disable --state <state> --reason "<原因>"`（用户
明确要求停止/紧急退出；`enable` 恢复）。ESC 中断会中止钩子进程，宿主不续跑。

**异常 fail-closed**：state 损坏、`session.json` 损坏、`.copilot-tasks` 不可读、钩子输入非法
都以退出码 2 返回，并给出修复路径或 `disable` 命令；不静默放行，也不伪造 PASS。
排查时可设 `COPILOT_STOP_GUARD_LOG=<path>`，每次判定追加一行 JSON（退出码、工作区、
会话、任务与阶段），默认不写任何文件。

**恢复注入（`SessionStart`）**：`copilot_stop_guard.py session-start` 处理 `SessionStart`
事件：

- `resume|compact` 且任务绑定本会话：输出完整只读快照（任务 id、阶段与原因、未闭合条件、
  阻塞原因/`needs`、下一条命令、可粘贴续接命令、项目现场与 Memory 可用性），提示先核实
  现场、复用已完成工作与已登记证据。
- `startup` 或没有任何任务绑定本会话：只输出**只读指针**（任务阶段、可执行项、阻塞
  原因/需要、归属标注），不披露状态路径与续接命令，不绑定、不接管、不强制续跑。这样
  重启后会话标识变化仍能发现现场，但不会把无关会话引向旧任务。
- 它不写 state/session、不消费 Stop 次数；无未闭合任务、文件损坏或输入非法时一律静默
  退出 0（fail-open），不阻断会话启动。

**用户回合提示（`UserPromptSubmit`）**：`copilot_stop_guard.py user-prompt` 只读列出**本会话
负责**的未闭合任务（含 BLOCKED 的 `reason`/`needs`）与判断规则：用户本回合补齐授权/决策/
条件时，先用其原话 `unblock --by user --quote "<原话>"`（或补齐条件），再 `drive` 继续执行；
用户没有补齐时不要恢复执行；本提示不是用户新指令，也不构成授权；先 `status --json`
核对阶段与证据，工作项被其它会话领取时避免并发重复。

其它会话或未绑定会话的旧任务不逐回合展开，也不重复披露（现场与阻塞原因由 `SessionStart`
指针披露一次）；本会话另有任务时它们只占一行计数。否则一条无关消息也会带上别处旧任务的
全部细节。归属无法确认（`session.json` 损坏，Stop 会 fail-closed 拦截）的任务仍逐回合提示；
宿主未给会话标识时只有恰好一个候选任务才展开，多个候选只报数量并提示先 `status --json`
核对（与 Stop 同一口径：不按“最近更新”替会话选任务）。没有本会话任务时整个块不注入。

`install` 同时写入 `Stop`、`SessionStart`（matcher `startup|resume|compact`）与
`UserPromptSubmit` 三个事件，`uninstall` 一并移除。

`closure-check` 的退出码与结论一致：PASS=0、FAIL=1（`--json` 同样），避免“检查过”被误当
“通过”。

## 5. SOL 生命周期调度（v9.6）

SOL 仍是 advisory：调度只决定**什么时候该有一次认知判断**，不决定阶段、门禁或技术方案。
实现分两层——`scripts/sol_schedule.py` 是纯策略/投影层（无网络、无文件 I/O、无门禁），
`governance.py` 把义务落盘到 `state.sol_schedule`，`copilot_dispatch.py` 在生命周期命令内
有界执行。

**五个认知窗口（family A–E，与 `review_policy.sol_advice` 同一口径）**

| 窗口 | 时机 | 模式 |
|---|---|---|
| A | 启动 / 未覆盖当前合同的 CONSULT | CONSULT（understanding） |
| B | 调查后方案取舍（方案类任务、声明触发、中高复杂或 ≥2 工作项） | PLAN（planning） |
| C | 重大新证据（六类事件：`material-evidence-change` / `decision-point` / `risk-escalation` / `replan-history` / `scope-changed` / `reopened`） | CONSULT（development） |
| D | 里程碑复盘（中高复杂 + ≥2 完成项，或 ≥1 完成项 + 已核销条件；recommended） | CONSULT（stage） |
| E | 收口综合判断（最终工程验证完成后、G1–G3 收口之前的非简单有进展任务，medium 也是 recommended） | CONSULT（closure） |

**义务与窗口**：每条义务记录 `obligation_id`（`SO<n>`）、family / mode / stage / event /
instance、`contract_sha256`、`level`、`status`、`window`。`status` 取值 `due` / `claimed` /
`in_flight` / `satisfied` / `failed` / `skipped`；`window` 只有 `open` / `closed`。

- **义务 key ≠ 复用 key**：义务 key 含实例与合同指纹（同一窗口不重复创建）；复用 key
  只含认知对象，因此同一合同、同一材料、同一问题会被判为 `reuse` 并直接满足未闭合义务，
  不再付费。
- **窗口只在事实发生后关闭**：更晚窗口真的被满足时，更早仍未满足的窗口才记为
  `superseded`（漏调可见）；刚创建、只给提示、预算耗尽都不关闭窗口，避免把“还没来得及
  做”误判成漏调。`due` / `claimed` 落在已关闭窗口上即派生 `missed`。
- **同一 family + event 只保留一个未执行窗口**：连续进展会反复打开同一里程碑窗口，旧的
  未执行窗口在新建时即记 `skipped` + `skip_by=superseded-by-newer` 并关闭（历史状态里
  已堆积的多条打开窗口在下一次同步时同样收敛成一条）；`in_flight` 不被取代。否则一次
  连续进展就会堆出多条打开窗口，收口时被误算成多条漏调。
- **D / E 都是 recommended（含 medium）**：里程碑复盘与收口综合判断对中高复杂有进展任务
  是必需窗口；`due_obligations()` 只取 recommended，标成 optional 会让它们结构性无法自动
  执行（`drive` / `closure` 永远取不到）。历史 `optional` 义务在收口时按
  `skipped` + `skip_by=not-required` 结算为合理跳过，`missed` 只统计确实应执行而未执行的
  窗口；收口前先结算“更晚窗口已满足”，更早的里程碑记 `superseded` 而不是漏调。
- **收口覆盖最新里程碑**：E 已满足时仍未执行的 D 窗口记 `skipped` +
  `skip_by=closure-covered`（收口综合判断覆盖了里程碑复盘的认知对象）——最后一批进展刚
  打开 D 窗口就收口时不计漏调；其它更早窗口（A/B/C）仍按 `superseded` 记漏调。
- **合同修订**：旧合同下仍在 open 的义务显式记为 `skipped` + `skip_by=contract-amended`
  并关闭窗口——旧事实留在审计里，但不进入新合同的覆盖率分母。

**终态处置与收口周期（v9.7.1）**

- **每个义务都要有终态**：窗口关闭时写一次 `disposition`——`completed`（真实执行或复用）、
  `covered`（`closure-covered`：收口判断已覆盖该认知对象）、`skipped`（合同修订、被更新
  窗口取代、非必需等，必有 `skip_reason`）、`failed`（真实失败，带错误码）、
  `not-executed`（关闭时仍未执行）、`unsettled`（`in_flight`：请求已发出但未落盘）。
  附 `disposition_reason` 与 `disposition_at`；已有处置不覆盖（只增不改），也不改变
  `status`、`window` 与 `missed` 口径。
- **`due` 不等于强制调用**：`schedule.policy_snapshots[<cycle>]` 记录本周期实际生效的
  `off` / `advise` / `auto` 与是否允许自动执行（由生命周期命令的 `--sol` /
  `COPILOT_SOL_AUTO` 写入）。`not-executed` 的理由引用这份快照；**没有快照的历史义务一律
  记 `unknown`**，不拿当前配置倒推“当时为什么没调用”。
- **未能调度要留痕**：条件成立但本次没能执行（SOL 锁被占用等）只累加 `deferrals` 与
  `last_deferral_reason`，义务仍保持 `due`；窗口关闭时这条事实现身在 `not-executed` 的
  理由里，不被静默清除，也不冒充失败（真实请求失败仍由 `failed` 表示）。
- **收口周期隔离**：新义务带 `cycle`（1 + `closure.reopen_history` 条数）。窗口比较
  （`_advance_windows` 的“更晚窗口已满足”）只在同一周期内进行：重开后的收口判断覆盖的是
  重开后的认知对象，不能追溯宣布上一周期的窗口已被覆盖。旧周期的义务保持原周期与原事实
  （合同修订/被更新窗口取代的既有语义不变）；`summary.by_cycle` 与
  `sol-stats.totals.reopened_tasks` 只是观测值。
- **历史义务只读推导**：`obligation_disposition()` 对没有持久化处置的旧义务按窗口事实现算，
  不写回历史状态；历史任务无需重跑命令也能看到终态与 `unknown` 理由。
- **不阻断**：处置与周期分布只进 `summary.dispositions` / `by_cycle` 与 `sol-stats`，
  不改 `pending` / `missed`、不改 coverage 分母、不阻止收口；`sol_schedule_warnings` 仍是
  非阻断提醒。

**有界执行**

- 执行点：`start` / `plan` / `record` / `rerun` / `criterion` / `judgment` / `evidence` /
  `report` / `accept` / `decide` / `block` / `unblock` 与 `drive` 的调度步
  （`_sol_schedule_step`），以及 `closure --close` 与 `sol-schedule --execute`。
- **上限 1 个义务 / 1 次请求 per 命令**：不存在每任务固定次数上限，也不自动重试；剩余
  窗口留给下一条命令（`continuation` 仍要求继续，因此驱动链会自然走到）。
- 策略 `--sol auto|advise|off`（默认 `auto`）：`auto` 直接经既有 SOL 通道执行到期义务；
  `advise` 只打印可直接执行的命令与义务 id；`off` 静默。环境变量 `COPILOT_SOL_AUTO`
  可覆盖：`off` 强制离线，`on` 允许执行；测试进程（`unittest` 已加载）默认不执行，只有
  显式 `on` 的调度测试才走真实通道。
- **只读命令永不执行**：`next` / `status` / `sol-schedule` / `sol-stats` / `closure-check`
  只物化并展示义务，不写 state、不发请求。
- **可执行命令只有一个来源**：`sol-schedule` 与调度步输出的命令带真实状态路径与
  `--obligation`，可原样执行；`evaluate` / `review-admission` 里的 `sol.command` 仍是
  含 `<>` 占位的**模板**，只用于说明意图，不能当作已执行或可直接执行。
- **收口顺序固定**：最终工程验证完成（收口准入通过）→ SOL 收口综合判断（family E，
  `prefer_family="E"`）→ ZCode 最终验收（`sol-accept`，accepted / modified / rejected 任一）
  → `close_task` 的 G1–G3 收口。E 窗口不占用 `drive` RUNNING 分支的那个额度，
  `close_task` 记录的 `closure.sol_entry`（`drive` / `closure-command`）标明本次收口由哪个
  入口完成；收口时仍 open 的窗口记为 `pending_before_close`，未满足的记
  `missed_before_close`。验收这一步是强制的顺序事实：`governance.closure_acceptance_pending()`
  在当前摘要的收口 CONSULT 尚无裁决记录时让 `close_task` 失败关闭、`continuation` 给
  `action="accept"`、`drive` / `closure --close` 停在 `EXIT_EXECUTE` / `EXIT_CONTINUE`。
  裁决不是 SOL 批准权：三种结论都可继续，验收也不替代用户对质量报告的接受。
- **收口准入唯一口径（v9.6.1）**：`governance.closure_readiness(state)` 是 E 窗口与收口
  的唯一准入，返回 `ready` / `digest` / `all_actions_terminal` / `criteria_satisfied` /
  `final_evidence_recorded` / `required_tests_completed` / `no_pending_required_work` /
  `verification_passed` / `blockers`。先查便宜事实（工作项终态、完成条件、证据、必要
  测试、重开后无进展、开放 finding），全部通过才算确定性门禁（不含质量报告的**用户
  接受**——那是 ZCode 验收之后的用户一步，不能把 SOL 挡在它后面）。工作项全部关闭、
  G1–G3 通过都**不等于**最终验证完成：合同 `test_policy.required=true` 时必须存在带
  `capture_cmd` 的采集证据（词元按序匹配声明命令，允许插入 `-B` 等运行器参数）或结构化
  质量报告给出 `required_tests_executed=true`。
- **收口摘要（`final_verification_digest`）**：只含验证事实（合同指纹、重开迭代、条件状态
  与引用证据、终态工作项、采集证据与报告摘要）与材料口径（`MATERIALS_SCHEMA`）的
  `sha256[:16]`。E 义务的 instance 就是它：调度义务与当前摘要不符时拒绝付费调用；同一
  摘要再次到达判 `reuse` 不二次付费；重跑测试、新增/修订证据或材料生成口径变化使摘要
  变化，旧 E 结论不再覆盖新结果，需要新一轮验证后重新派生 E。材料内也自报口径版本，
  审计能看出这份结论是按哪套材料规则得出的。
- **入口一致**：`record` / `rerun` / `criterion` / `evidence` 等回填命令只更新义务，
  `drive` 在验证缺口时返回 `EXIT_EXECUTE` 并拒绝执行 E，`closure --close` 在验证缺口时
  直接失败（退出码 1）而不收口，`sol-consult --stage closure` 在缺口或摘要不符时拒绝调用，
  `close_task` 对缺测试证据的任务拒绝收口、对等待验收的收口 CONSULT 同样拒绝收口。
  任何入口都不能绕过验证、E 调度或 ZCode 验收。
- **E 是 advisory**：收口 CONSULT 返回的风险与遗漏由 ZCode 独立核实、修复并定向验证；
  它不是门禁，不需要 SOL PASS 才能收口，失败只降级。
- **失败降级**：请求失败（配额、网络、超时、截断、模型身份不符等）按 `record_sol_obligation_error`
  记录真实原因，`max_attempts=3` 后把义务置 `failed` 并继续工程推进；失败不冻结工作项、
  不改变阶段、退出码仍为 0，只是 `sol_schedule_warnings` 里可见。
- **失败分类与退避窗口（v9.8）**：`sol_review.py` 把上游失败归入十类 `error_class`
  （`upstream_overloaded` / `upstream_stream_timeout` / `upstream_credential_cooling` /
  `upstream_auth` / `upstream_rate_limit` / `upstream_server_error` /
  `upstream_client_error` / `client_timeout` / `network_error` / `response_incomplete`），
  并把原始 `http_status`、`upstream_code`、`retry_after`（秒数）与 `retry_after_raw`
  （原始文本，可能是 HTTP-date）、`request_id`、`trace_id` 原样写入 `sol-audit.jsonl` 与
  `sol_calls` 失败记录；上游过载与本地凭据冷却期 503 都**不**记作认证失败。重试语义不变：
  一次调用只发一次请求，不自动重试、不换 Provider。
  `state["model_backoff"]`（`copilot-model-backoff/v2`，键 `codex:<model>`）按
  `provider + model + error_class` 记录到期时间：**服务端 Retry-After 是
  最短等待时间**（秒、毫秒、HTTP-date 均支持，HTTP-date 按 UTC 换算，过期或不可解析则不用），
  原样遵守、不被本地上限或环境变量缩短；只有在没有有效服务端值时才按 `error_class` 取默认
  窗口（60 秒），此时才受 `COPILOT_SOL_BACKOFF_MAX`（只能下调）与 1800 秒上限约束。
  窗口期调度步与 `sol-schedule --execute` 只登记 deferral（不消耗 `max_attempts`、不发请求），
  手动 `sol-consult` / `sol-plan` / `sol-review` 直接拒绝，`--ignore-backoff` 仅在确认上游
  恢复后显式覆盖；成功调用只清除本模型的窗口。窗口写在任务状态里，跨阶段、跨进程、跨会话
  同样生效，到期后恢复既有调度频率。**按模型隔离**：SOL 过载不冻结 Astra，Astra 过载不阻塞
  显式 SOL 调用；旧 `state["sol_backoff"]` 单例只读兼容，不继承给新默认模型。它是失败后的
  节流记录，**不是门禁**：不改变阶段、条件、G1–G3，也不派生工作项。
- **并发**：SOL 通道由 `.sol-review.lock` 独占；拿不到锁抛 `LockBusyError`，命令回退为
  “本轮不执行”，不排队重试、不重复付费。`sol-consult` / `sol-plan` 支持
  `--lock-timeout <秒>`。

**覆盖率统计（观测，不是门禁）**：`sol-stats --state <path>` 或 `--workspace <dir>` 按
合同口径输出应调/实际执行/失败/复用/合理跳过/应调未执行与原因、CONSULT 与 PLAN 的阶段
分布、六类事件分布、采纳（accepted/modified/rejected）、tokens 与延迟。分母只统计当前
合同下的适用窗口：启动＝非简单任务、规划＝`plan_admission().required`、收口＝非简单、有实质进展且
复杂度为 medium/high（`closure_eligible_tasks` 在 totals 中单列）、事件＝已识别事件数减去
合理跳过数。任务级窗口（A 启动 / B 规划 / E 收口）的分母是“本任务是否适用”（0/1），
所以分子也按任务计 1 次：满足＝**真实执行或复用**（`by_family` 仍分别保留执行/复用/
跳过/漏调的条数，用于解释“覆盖了几次”）。事件窗口按“每出现一次事件 → 一个适用窗口”
计数，执行/复用计入分子，合理跳过不进入分母，漏调单列。打印命令、预检通过与模拟响应
都不算执行。分母为 0 时 `rate` 输出 `null`（不是 100%），任务级 `not_registered`
表示“应当登记却没有义务记录”。

## 6. 收口与重开

- `closure_check` 依次检查 G1–G3（工作项终态、合同/条件/证据/台账、无停摆），全部通过才
  `passed=true`；G4 已退役，不再参与。
- `closure --close` 需要 G1–G3 通过，写入 `closure`（时间、`by`、`iteration_id`、门禁快照、
  `sol_entry`）并把阶段固定为 CLOSED；未结束的迭代同时标记为 `CLOSED` 并补 `ended`，
  之后的修改一律拒绝。收口前先消费 family E 的收口认知窗口（§5）：成功则是收口前的
  最后一条认知记录，未执行则把仍 open 的窗口记入 `closure.sol_schedule.at_close`，
  两者都不影响 CLOSED 的成立。
- `reopen --reason ... --authorized-by user` 需要显式用户授权声明；它保存旧 closure 快照到
  `reopen_history`（含授权声明），清理收口状态，任务回到 RUNNING/PARTIAL，并开一条新迭代
  承接后续工作（已收口的旧迭代保持 `CLOSED`，不改写历史）。旧快照在写入前做深拷贝，避免
  自引用。
- `final_evidence_digest` 绑定合同指纹、条件与证据、工作项结果与证据；它仍用于证明目标摘要
  稳定与历史复核的过期判断。**排除历史 `origin=g4-review` 工作项**以保持旧状态摘要稳定；
  新任务不再产生该类工作项。

## 7. 状态保真与恢复

- **原子写入**：同目录临时文件 + `fsync` + `os.replace`，并 `fsync` 目录；中断不会留下半个
  JSON。
- **并发安全**：`.lock` 文件 + `fcntl.flock` 串行化写者；`save_state(..., expect=...)` 用
  `save_seq` 做 CAS，识别“读到旧状态再写”的竞争并拒绝本次写入（提示 `save_seq` 变化，
  需重新读取后重做）。
- **schema 守卫**：只接受 `copilot-governance/v1` + `version=1`；v1/v2 SOL 状态不被驱动，
  只能用 `legacy-status` 只读查看。
- **上下文恢复**：状态里保留目标、合同、条件、工作项与结果、证据与摘要、判断、报告、决策、
  阻塞、调度领取、复核记录、迭代与完整 `history`。中断后从状态继续，不重建任务、不覆盖
  已有状态；租约随状态保存，未过期时其它会话不会重复领取，过期后自动回收。
  **没有单独的 `--resume` 开关**：续跑就是继续使用同一 `--state` 文件——先 `status --json`
  核实现场，再 `drive`。
- **历史不可改写**：`history` 只追加；证据不可替换；收口后不可修改。

### 跨回合续接（用满 3 次续跑 / 新会话 / 进程重启）

1. **同一 `--state`**：不新建任务、不改 `task_id`；续接点就是原 `state.json`。
2. **先核实现场**：`status --json` 看阶段与 `continuation`；核对 Git 工作区改动、证据文件与
   摘要、未闭合条件、未完成工作项与阻塞原因。历史计划不是当前事实。
3. **不重做已完成工作**：`done` 工作项、已登记且摘要未变的证据直接复用；只继续真正未完成
   的部分，证据只新增不覆盖。
4. **保持原授权**：续接不扩大范围，原任务内已授予的授权继续有效。
5. **仍是 PARTIAL 就继续**：按派生规则执行并回填，不因换回合另建重复任务。
6. **真实 BLOCKED 才停**：逐条列出 `reason`/`needs` 与解除条件，解除后 `unblock` 恢复。

### Goal 模式（可选，仅用户发起）

`/goal`（headless `--target`）让宿主跨回合持续同一目标，可超出单回合 3 次续跑上限；普通任务
不使用。启用条件：多阶段复杂工作、目标清晰可验证、权限客户端已确认可用。约束：

- 只能由用户发起；Copilot 与主代理不得自行启用，也不得把既有任务改造成 goal 目标。
- Goal 与 Plan 互斥，不为启用 goal 强制切换会话模式。
- objective 与合同目标一致；目标变化走 `contract --amend`，不只改 objective。
- 验收仍以 Copilot 最终状态与 G1–G3 为准；goal 自报完成/校验器结论不是工程 PASS。
- 缺少权限客户端时如实报告并停下，**不得用 `--mode yolo` 绕过审批**；不调用未公开的预算/目标接口。
- 连续无进展、token 异常增长、权限错误反复出现时先 `/goal pause` 并排查。
- 不引入常驻服务、不递归启动 ZCode、不修改宿主安装包与运行时。

## 8. 何时可以 BLOCKED

只有以下情况才允许 `block --reason ... --needs ...`：

- 缺少用户授权（例如需要 commit/push/发布/付费/生产数据操作）；
- 需要用户决策且无法由现场证据推出；
- 缺少必要资源（设备、凭据、外部环境）；
- 真实外部限制（服务商限流/配额、网络、安全边界）。

**按动作与工作项登记**：缺 commit 授权只阻塞 commit 工作项，缺 push 授权只阻塞 push；
其它工作继续。用法：

```bash
copilot_dispatch.py block --state S --reason "未获 push 授权" --needs "用户授权 push" \
    --work-item A2 --criterion C2
```

`--criterion` 声明同时被卡住的完成条件（避免反复派生 `AUTO-C<n>`）；`--work-item` 可重复，
只冻结这些项及其下游依赖。`--scope task` 会冻结全部工作，仅当目标或资源整体不可推进时使用。
`unblock --block-id` 解除后，被冻结工作项恢复为 `pending`，立即重新进入可执行。

**自动归属**：派生工作项 `AUTO-C<n>` 自动承载 `C<n>`；计划项可写
`target_criteria: ["C1","C2"]`。阻塞这些项时未显式给 `--criterion` 也会自动继承，避免条件
悬空后被反复派生。若被阻塞项既无显式也无自动归属，求值返回 `unattributed_criteria` 并把
补齐归属的命令写进 `continuation.reason` / `next_commands`；此时任务停在 PARTIAL 是**归属
缺口**，不是可执行工作，补齐后才会进入真实 BLOCKED。

以下**不得**作为 BLOCKED 理由：轮次/预算/费用阈值、等待远程模型、重复评审、内部状态流转、
子 Agent 返回 PARTIAL、首次方案失败、普通测试失败、可自行补齐的证据不足。

### 子代理派发失败与降级（v6.9.0）

- **主代理继续**：子代理不可用、派发失败或返回 PARTIAL 只影响该工作项。主代理分析剩余工作，
  继续可直接完成的调查、修复、测试与集成；普通任务不因 Agent Profile 加载失败而 BLOCKED。
- **不原样重派**：`UNKNOWN_AGENT_TYPE` 先确认工作区/会话启动时机/可用类型；执行信封被拒时
  按错误码修正元数据或证据后重新生成。同一缺口已有 `pending`/`running` 工作项时不再创建
  相同子代理任务；连续失败派生 `AUTO-REPLAN`，要求换实质不同的路线。

### BLOCKED 下的受控写入

BLOCKED 阻止的是收口和受阻动作，不是事实记录。允许 `record`、`rerun`、`evidence --add`、
`criterion`、`report`、`judgment`、额外的 `block`、以及已授权的 `authorize-review` /
`formal-review`（含 amendment）。这些写入不会解除阻塞、不会改变合同/授权、也不会让
`close_task` 提前通过；G1–G3 仍按当前状态重算。

继续拒绝：`plan`（改变计划范围）、`decide`（可能改变优先级/范围）、`evidence --drop`
（破坏性归档）。多个真实阻塞可以并存；存在多条未解除阻塞时，`unblock` 必须用
`--block-id` 逐条解除，不能用一条理由把不同阻塞一起清除。合同修订使用单独的
`contract --amend`：BLOCKED 下允许，但会更新合同指纹并使旧正式 REVIEW 授权失效，
不属于上述受控写入白名单。

## 9. 恢复清单

中断或回到旧任务时按顺序核对：

1. `status --json`：阶段、reason、ready/stalled、门禁缺口；
2. 未闭合条件与对应证据是否仍存在、摘要是否一致；
3. 已尝试方案与失败原因（工作项 `result`、`fail_streak`、`AUTO-REPLAN` 记录）；
4. 是否存在真实 block（`needs` 是否已满足）；
5. 下一步：`drive` 输出工作单 → 执行 → `record`/`criterion` → 再次 `drive`。
6. **重新核实执行方身份**：恢复后不继承历史权限声明；执行信封必须用本次会话的真实
   `metadata.json` 生成，不接受自报身份。
7. **复用仍有效的证据**：已完成且摘要未变的证据直接引用；漂移或宿主记录变化的一律重新
   登记。“恢复”不复活旧结论，也不因恢复而重复已完成的工作。历史 review attempt 只读，
   不再复用或重建。
8. **续跑上限后的续接**：本回合已被续跑 3 次（宿主上限）时，不要新建任务、不要改状态规避；
   在同一 `--state` 上保存进展与证据，由用户在新回合执行守卫给出的续接命令，再从第 1 步核对。

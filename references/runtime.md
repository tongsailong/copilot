# runtime：v8 阶段机、驱动与持续推进

本文说明 Copilot v8 的**本地运行时语义**：阶段如何判定、PARTIAL 如何派生工作、
驱动如何收敛、状态如何保真恢复。命令与参数见 `workflow.md`；复核门禁见 `review.md`。

## 1. 设计原则

- **本地判定**：阶段判定、工作项派生、门禁检查只读状态文件与证据文件，不调用网络、模型或
  子进程（命令采集只在 CLI 显式请求时执行）。`sol-consult` / `sol-plan` / `sol-review`
  是仅有的例外：它们由主代理显式调用（CONSULT / PLAN 按生命周期建议、REVIEW 由用户
  明确要求），结果统一写入 `sol_calls`（历史 `sol_reviews` / `sol_advisories` 只读兼容），
  不自动改变阶段、不参与 PARTIAL 派生；CONSULT / PLAN 也不进入 Finding 台账或 G1–G3。
  SOL 调用失败只登记真实原因，不产生阻塞或派生工作项。
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
4. PASS → `close_task`，退出 0；
5. RUNNING → 取前 `--max-actions` 个 ready 项，写入调度领取并把工作单打印出来；
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
没有会话标识（手工调用）时只对最近更新的一个任务生效，避免一次性误拦全部历史任务。

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

**用户回合提示（`UserPromptSubmit`）**：`copilot_stop_guard.py user-prompt` 只读列出工作区
未闭合任务（含 BLOCKED 的 `reason`/`needs`）与判断规则：用户本回合补齐授权/决策/条件时，
先用其原话 `unblock --by user --quote "<原话>"`（或补齐条件），再 `drive` 继续执行；
用户没有补齐时不要恢复执行；本提示不是用户新指令，也不构成授权；先 `status --json`
核对阶段与证据，工作项被其它会话领取时避免并发重复。没有未闭合任务时不注入。

`install` 同时写入 `Stop`、`SessionStart`（matcher `startup|resume|compact`）与
`UserPromptSubmit` 三个事件，`uninstall` 一并移除。

`closure-check` 的退出码与结论一致：PASS=0、FAIL=1（`--json` 同样），避免“检查过”被误当
“通过”。

## 5. 收口与重开

- `closure_check` 依次检查 G1–G3（工作项终态、合同/条件/证据/台账、无停摆），全部通过才
  `passed=true`；G4 已退役，不再参与。
- `closure --close` 需要 G1–G3 通过，写入 `closure`（时间、`by`、`iteration_id`、门禁快照）
  并把阶段固定为 CLOSED；未结束的迭代同时标记为 `CLOSED` 并补 `ended`，之后的修改一律拒绝。
- `reopen --reason ... --authorized-by user` 需要显式用户授权声明；它保存旧 closure 快照到
  `reopen_history`（含授权声明），清理收口状态，任务回到 RUNNING/PARTIAL，并开一条新迭代
  承接后续工作（已收口的旧迭代保持 `CLOSED`，不改写历史）。旧快照在写入前做深拷贝，避免
  自引用。
- `final_evidence_digest` 绑定合同指纹、条件与证据、工作项结果与证据；它仍用于证明目标摘要
  稳定与历史复核的过期判断。**排除历史 `origin=g4-review` 工作项**以保持旧状态摘要稳定；
  新任务不再产生该类工作项。

## 6. 状态保真与恢复

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

## 7. 何时可以 BLOCKED

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

## 8. 恢复清单

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

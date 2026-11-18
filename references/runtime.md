# runtime：v8 阶段机、驱动与持续推进

本文说明 Copilot v8 的**本地运行时语义**：阶段如何判定、PARTIAL 如何派生工作、
驱动如何收敛、状态如何保真恢复。命令与参数见 `workflow.md`；复核门禁见 `review.md`。

## 1. 设计原则

- **本地判定**：阶段判定、工作项派生、门禁检查只读状态文件与证据文件，不调用网络、模型或
  子进程（命令采集只在 CLI 显式请求时执行）。`sol-think` 与 `sol-review` 是仅有的例外：
  它们由主代理显式调用（THINK 按复杂度建议、REVIEW 由用户明确要求），结果统一写入
  `sol_calls`（历史 `sol_reviews` / `sol_advisories` 只读兼容），不自动改变阶段、不参与
  PARTIAL 派生；THINK 也不进入 Finding 台账或 G1–G3。SOL 调用失败只登记真实原因，不产生
  阻塞或派生工作项。
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

`evaluate(state)` 按固定顺序判定，命中即返回：

| 顺序 | 条件 | 阶段 | 说明 |
|---|---|---|---|
| 1 | `closure.closed` 为真 | CLOSED | 已收口；继续需用户授权 `reopen` |
| 2 | 存在未解除的 block | BLOCKED | 输出原因与 `needs`，退出码 11 |
| 3 | 合同缺失或条件未确认 | PARTIAL | 派生 `AUTO-CONTRACT` |
| 4 | 存在可领取工作项 | RUNNING | 输出工作单，退出码 10 |
| 5 | 仍有未闭合条件 | PARTIAL | 派生 `AUTO-C<n>` |
| 6 | 存在停摆工作项 | PARTIAL | 派生恢复项 |
| 7 | G1–G3 全部通过 | PASS | 收口为 CLOSED |
| 8 | 其他门禁未过 | PARTIAL | 按缺口派生 |

- **可领取（ready）**：`status=pending` 且所有 `depends_on` 已完成。
- **停摆（stalled）**：`running`/`failed`/`blocked` 的遗留项；或 `pending` 但依赖不存在、
  依赖已 `dropped`/`failed`/`blocked`。
- **PARTIAL 永远有出口**：即使没有 ready 项、没有未闭合条件、没有停摆项，也会派生
  `AUTO-NEXT` 要求重新核对目标、证据与真实阻塞。

## 3. PARTIAL 派生规则

`derive_work(state)` 一次最多派生 `MAX_DERIVED_PER_CALL=12` 项，全部 `assignee=zcode`、
`origin` 标明来源、`derived_reason` 记录原因，并写入 `history`：

| 触发 | 工作项 id | 目标 |
|---|---|---|
| 合同缺条件/未确认 | `AUTO-CONTRACT` | 补齐可核销条件与边界 |
| 条件未闭合 | `AUTO-C1`、`AUTO-C2`… | 执行工作、登记证据、`criterion met` |
| 最新报告为 PWW、绑定当前且接受条件未闭合 | `AUTO-ACCEPT` | 把原始结论与全部 W/R 如实呈现给用户，取得用户明确接受后 `accept --by user`；执行方不得代接受。非 PWW 或绑定失效时改派 `AUTO-QUALITY` |
| 质量报告替代/代码范围/工作区/接受门禁未过 | `AUTO-QUALITY` | 重新 verify、声明替代与解决、确认任务工作区并用当前代码快照重新接入 |
| 停摆工作项 `A1` | `AUTO-R-A1` | 分析失败根因并重试/调整/记录真实阻塞 |
| Finding 台账有 `open`/`regressed` 的 blocking/major | `AUTO-FIX-<F>` | 修根因、登记修复证据并标记 fixed，不直接重跑完整复核 |
| Finding 已 `fixed` 但未验证 | `AUTO-VERIFY-<F>` | 只跑对应定向测试/检查，登记证据并标记 verified |
| `AUTO-C*` / `AUTO-R-*` 已派生 ≥3 次仍不闭合 | `AUTO-REPLAN` | 根因分析 + 实质不同的替代路线 |
| 无其他缺口 | `AUTO-NEXT` | 明确下一步最小可执行动作 |
| 存在连续失败工作项 | `AUTO-REPLAN` | 同上 |

- 重名时用 `-R2`、`-R3` 递增（`_derive_id`），不覆盖已有工作项。
- 同一缺口已有 `pending`/`running` 工作项时不再派生第二条；重复调用 `drive` 或多个入口
  同时求值不会把 `AUTO-C1` 复制成 `-R2`/`-R3`。
- G4 退役后不再派生 `AUTO-G4*`；SOL 复核由主代理按风险决定，不是阻塞性工作项。
- 失败/停摆的 `AUTO-C*` 项不再额外派生恢复项，避免同一缺口两份工作单。
- 派生项带通用约束：不得越过原始授权范围、不得改目标/条件/业务规则、只做缺口需要的最小
  改动；若合同写有 `boundaries`，原样带入。
- 派生项 `depends_on` 为空：它们是对缺口的直接行动，不阻塞其他工作。

## 4. 驱动循环（`copilot_auto.py drive`）

1. 读取状态 → `evaluate`；
2. CLOSED → 打印状态，退出 0；
3. BLOCKED → 打印原因与 `needs`，退出 11；
4. PASS → `close_task`，退出 0；
5. RUNNING → 取前 `--max-actions` 个 ready 项打印工作单；`backend=native` 且工作项属于
   已登记计划时，经 `native_bridge.build_dispatch` 写出 `dispatch.json`，退出 10；
6. PARTIAL → `derive_work`；若派生成功则 `begin_iteration`（记录本轮工作项与证据快照，
   并把上一轮未结束的迭代标记为 `ENDED`）并保存，比较 `progress_digest`；若派生结果为空，
   退出 12；
7. 循环最多 32 步，超限退出 12 并保留状态。

**进展摘要 `progress_digest`**：由工作项状态/结果摘要/证据引用、条件状态/证据引用、
证据 id 与摘要、收口状态计算。派生前后摘要不变且没有 `AUTO-NEXT` 时输出警告；同一缺口
反复派生（`AUTO-C*` / `AUTO-R-*` 各自累计 ≥3 次）会附带 `AUTO-REPLAN`，
要求换路线而不是重复执行。

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
  阻塞、复核记录、迭代与完整 `history`。中断后从状态继续，不重建任务、不覆盖已有状态。
  **没有单独的 `--resume` 开关**：续跑就是继续使用同一 `--state` 文件——先 `status --json`
  核实现场，再 `drive`。
- **历史不可改写**：`history` 只追加；证据不可替换；收口后不可修改。

## 7. 何时可以 BLOCKED

只有以下情况才允许 `block --reason ... --needs ...`：

- 缺少用户授权（例如需要 commit/push/发布/付费/生产数据操作）；
- 需要用户决策且无法由现场证据推出；
- 缺少必要资源（设备、凭据、外部环境）；
- 真实外部限制（服务商限流/配额、网络、安全边界）。

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

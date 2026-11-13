# runtime：v6 阶段机、驱动与持续推进

本文说明 Copilot v6 的**本地运行时语义**：阶段如何判定、PARTIAL 如何派生工作、
驱动如何收敛、状态如何保真恢复。命令与参数见 `workflow.md`；复核门禁见 `review.md`。

## 1. 设计原则

- **纯本地**：阶段判定、工作项派生、门禁检查只读状态文件与证据文件，不调用网络、模型或
  子进程（命令采集除外，它只在 CLI 显式请求时执行）。
- **PARTIAL 不是终点**：PARTIAL 表示还有工作可做，运行时必须派生下一步，而不是停止或
  等待外部判断。
- **无人工上限**：没有轮次上限、Token 预算、费用阈值或评审次数限制。唯一的循环保护是
  `MAX_DRIVE_STEPS=32`：连续 32 步仍未收敛时返回内部错误（退出码 12）并保留状态，要求
  检查工作项是否被反复派生——它不是任务终止、不是 BLOCKED、不是 PASS。
- **fail-closed**：字段缺失、摘要不符、绑定过期一律拒绝，不用“看起来合理”替代校验。
- **提醒不阻断**：`evaluate` 在阶段结果里附带 `warnings`（例如合同源文件被改动），只提示
  不同步，不改变阶段、不参与四门。修复后的重新验证用 `rerun` 重开工作项，旧结果留在
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
| 7 | 四门全部通过 | PASS | 收口为 CLOSED |
| 8 | 其他四门未过 | PARTIAL | 按缺口派生（含 `AUTO-G4`） |

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
| 停摆工作项 `A1` | `AUTO-R-A1` | 分析失败根因并重试/调整/记录真实阻塞 |
| 高风险缺 G4 | `AUTO-G4` | 组织一次独立只读复核并回填 |
| 已有 G4 标记完成但门禁仍缺 | `AUTO-G4-INGEST` | 检查回填/绑定/拒绝原因，必要时重做复核 |
| `AUTO-C*` / `AUTO-R-*` / `AUTO-G4*` 已派生 ≥3 次仍不闭合 | `AUTO-REPLAN` | 根因分析 + 实质不同的替代路线 |
| 无其他缺口 | `AUTO-NEXT` | 明确下一步最小可执行动作 |
| 存在连续失败工作项 | `AUTO-REPLAN` | 同上 |

- 重名时用 `-R2`、`-R3` 递增（`_derive_id`），不覆盖已有工作项。
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
6. PARTIAL → `derive_work`；若派生成功则 `begin_iteration`（记录本轮工作项与证据快照）
   并保存，比较 `progress_digest`；若派生结果为空，退出 12；
7. 循环最多 32 步，超限退出 12 并保留状态。

**进展摘要 `progress_digest`**：由工作项状态/结果摘要/证据引用、条件状态/证据引用、
证据 id 与摘要、收口状态计算。派生前后摘要不变且没有 `AUTO-NEXT` 时输出警告；同一缺口
反复派生（`AUTO-C*` / `AUTO-R-*` / `AUTO-G4*` 各自累计 ≥3 次）会附带 `AUTO-REPLAN`，
要求换路线而不是重复执行。

## 5. 收口与重开

- `closure_check` 依次检查 G1–G4，全部通过才 `passed=true`。
- `closure --close` 需要四门通过，写入 `closure`（时间、`by`、`iteration_id`、门禁快照）
  并把阶段固定为 CLOSED；之后的修改一律拒绝。
- `reopen --reason ... --authorized-by user` 需要显式用户授权声明；它保存旧 closure 快照到
  `reopen_history`（含授权声明），清理收口状态，任务回到 RUNNING/PARTIAL。旧快照在写入前
  做深拷贝，避免自引用。
- `final_evidence_digest` 是 G4 的复核目标：它绑定合同指纹、条件与证据、工作项结果与证据，
  **排除 `origin=g4-review` 的工作项**——否则复核完成后自身状态变化会让合法结论永久“过期”；
  其他任何变化仍会使旧复核失效（fail-closed）。

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
- **历史不可改写**：`history` 只追加；证据不可替换；收口后不可修改。

## 7. 何时可以 BLOCKED

只有以下情况才允许 `block --reason ... --needs ...`：

- 缺少用户授权（例如需要 commit/push/发布/付费/生产数据操作）；
- 需要用户决策且无法由现场证据推出；
- 缺少必要资源（设备、凭据、外部环境）；
- 真实外部限制（服务商限流/配额、网络、安全边界）。

以下**不得**作为 BLOCKED 理由：轮次/预算/费用阈值、等待远程模型、重复评审、内部状态流转、
子 Agent 返回 PARTIAL、首次方案失败、普通测试失败、可自行补齐的证据不足。

## 8. 恢复清单

中断或回到旧任务时按顺序核对：

1. `status --json`：阶段、reason、ready/stalled、门禁缺口；
2. 未闭合条件与对应证据是否仍存在、摘要是否一致；
3. 已尝试方案与失败原因（工作项 `result`、`fail_streak`、`AUTO-REPLAN` 记录）；
4. 是否存在真实 block（`needs` 是否已满足）；
5. 下一步：`drive` 输出工作单 → 执行 → `record`/`criterion` → 再次 `drive`。

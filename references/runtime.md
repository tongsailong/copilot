# Copilot 操作细则：自动协作运行时与驱动（阶段 2–4）

> 本文件是 `SKILL.md` 的**操作细则与命令矩阵**，按任务需要读取。硬约束、门禁、授权边界与
> 裁决口径以 `SKILL.md` 为准；本文件不新增、不放宽任何硬约束，冲突时以 `SKILL.md` 为准。
> 读取触发条件见 `SKILL.md`「专业模块路由」。

## 自动协作运行时（阶段 2 / T1–T4）

以下规则以 `copilot_dispatch.py` 的实际实现为准（状态版本 2；v1 只读兼容、写入升级为 v2；
v1 无风险分级，升级时显式补为 `normal`）。
主代理是执行方与状态维护者，GPT 是认知角色；本节是双方的**运行时契约**，不是 GPT 的权限扩张。

### 1. 任务启动与合同确认

自动闭环在首次调用前建立任务级状态，后续每轮都带同一个状态文件：

```bash
python3 ~/.zcode/skills/copilot/scripts/copilot_dispatch.py start \
  --state /tmp/copilot/dispatch.json --goal-file /tmp/copilot/goal.md \
  --contract-file /tmp/copilot/contract.md --criteria-file /tmp/copilot/criteria.json \
  --task-id <稳定任务标识> --mode PLAN [--risk high] [--rounds-limit N]
```

- `start` 不覆盖已有状态；`task_id` 缺省随机生成，`--mode` 建议显式声明（空模式由首次
  自动调用钉住，之后不同模式一律拒绝）。
- `contract_sha256` 覆盖**目标 + 合同正文 + 结构化完成条件（id/描述）**的规范化摘要；
  每轮调用、`ingest`、`recover` 都核对它，不符即拒绝。条件只改状态（pending→met）不会改变指纹；
  改条件集合会改变指纹（见下条）。
- 完成条件来源：`--criteria-file`（字符串数组或 `{criterion_id, description}` 数组），否则解析合同
  的 `## 完成条件` 列表；两者都没有时条件为空、`criteria_state=draft`，不阻塞启动，但 G2 不放行。
- **确认只能由 GPT 在 PLAN 回复中给出**：`criteria_confirmed: true` 把已起草条件置为 confirmed，
  记录 `criteria_sha256` 与 `confirmed_round_id`；确认时不得改写条件描述。未确认就声明
  `verdict=pass` 会被拒绝。主代理不得自行把条件标成 confirmed。
- `contract --json` 只读查看；`contract --amend --criteria-file ...` 调整条件集合。
  已确认后只能由用户决策修改（`--source user --reason ...`），主代理不得擅自改验收口径。
- **合同实质修改的后果**：重算 `contract_sha256`；条件回到 `draft`、清空 `criteria_sha256` 与
  确认轮次；所有旧 PASS 轮标记为失效（轮次记录保留，不删除）；旧 PASS 不得再驱动
  `pass_pending`、G2 或收口；必须由 GPT 在新合同下重新确认并重新判断。存在 `pending_call`
  时拒绝修改，避免「按旧合同发出、按新合同入账」。
- **旧状态迁移**：G2 会重算合同指纹并与 `contract_sha256` 比对，旧版本（指纹不含条件集合）
  或未经 amend 的改动会被判为 `contract_fingerprint_mismatch`，不得收口。修复方式是
  `contract --amend`（条件集合不变也可以执行）：重算指纹并作废旧 PASS，再重新确认与判断。
- 风险级别在 `start` 时固定；需要改变高风险/普通风险时由用户决策。状态文件不是防篡改
  载体，私改 `risk` 降级不具备正当性；v2 状态缺失或非法 `risk` 时 G4 按 fail-closed 拒绝。
  v1 状态没有风险分级，升级时按遗留语义补为 `normal`；若 v1 任务实际为高风险，须由用户
  在后续阶段决定重新声明方式，不能靠收口后补记。

### 2. 结构化完成条件

- id 形如 `C1`…（不可变）；状态 `pending` / `met` / `not_met`；`met` 必须带已登记证据引用。
- 条件描述不得原地改写（用 `contract --amend`）；`criteria_digest` 只覆盖 id + 描述，
  状态与证据变化不会造成「条件集漂移」误报。
- `verdict=pass` 必须**恰好覆盖全部条件各一次**，且每条都是 `met`；缺条、多条、未知条、
  非 met 都会整轮拒绝，不能用自然语言总结代替逐条核销。

### 3. `decide` / `evidence` / `report` / `quota`

```bash
# 已确认的业务决策（用户或 GPT）；是后续轮次的上下文事实，不是证据
copilot_dispatch.py decide --state S --text-file d.md --source user

# 证据登记：只存元数据与摘要，正文仍由证据包承载；同 id 同摘要幂等
copilot_dispatch.py evidence --state S --id E1 --file out.log --kind test-output --note ...
copilot_dispatch.py evidence --state S --id E2 --text '关键输出' --kind inline
copilot_dispatch.py evidence --state S --id E1 --drop --reason '被新证据取代'

# 执行报告：必须绑定已验证轮次；blocked 必须给原因
copilot_dispatch.py report --state S --summary-file r.md [--round-id <rid>]
copilot_dispatch.py report --state S --blocked --reason '<缺口或阻塞>'

# legacy 复核报告：原始报告文件必填；review_of 必须指向真实已存在的对象
copilot_dispatch.py report --state S --kind review --file reviewer-report.md \
  --review-of <execution-report/report_sha256 或 evidence/sha256> \
  --review-of-kind execution-report|evidence --verdict pass|partial|blocked
```

- `decide` 单条上限 2000 字；`report` 上限 8000 字；报告是执行方反馈，不替代现场证据。
- 复核报告必须提供原始报告文件，登记为 `legacy-review-report` 证据并保存 SHA-256、任务归属、
  轮次和 `review_of_kind`；`review_source=legacy`，**不冒充** native Reviewer。同一文件与
  绑定重复登记幂等；缺失文件、摘要不符、跨任务、错误绑定或同一对象换报告都被拒绝。
- 原始报告若声明 `copilot-native-review/v1`，其 `verdict` 必须与登记/报告 verdict 一致；
  声明了该 schema 但正文不是合法 JSON、或 verdict 缺失/非法时失败关闭。schema 比较忽略
  大小写与首尾空白，不能借写法差异绕过原始结论一致性检查。
- native 任务不得用 legacy 报告混入复核权威；native 复核必须走 `ingest --native-result`。
- `record --evidence-id` 只能引用**已登记、属于当前任务、未被归档**的证据；文件型证据会重读
  并校验摘要。任一引用无效时整次回填失败，工作项状态、产出与证据列表都不改变。
- 证据 id 已登记但摘要不同时拒绝静默替换；需要新 id 或先 `--drop`（保留归档记录）。
- `quota` 查看/清除冷却。`--source user` 表示用户确认；`--source call` 必须同时给
  `--call-id`，该调用必须是本状态中已入账、身份验证通过、且发生在配额检测之后的轮次；
  状态缺少检测时间或时间不可比对时拒绝，不能把「未知」当「无需比较」。
  只凭来源参数或未知 `call_id` 不能把 `waiting` 改回 `ok`。
- **配额/冷却的调用契约（T4）**：上游错误体明确给出 `usage_limit_reached` 或
  `model_cooldown`（`code` 或 `type` 任一）时，调用以退出码 8 结束，并把等待状态写入任务状态；
  普通 HTTP 429（没有上述显式配额码）仍按上游错误返回 3，不误判为配额耗尽。
  HTTP 200 的响应体同样可能携带该错误体（代理把业务错误包在成功状态里）：命中时走**同一**
  配额通道（写等待、退出 8、不自动重发），不能当普通结构错误立即重试。
  `waiting` 期间发送前直接拒绝：不创建 pending、不发 HTTP、不改轮次与工作项。
  `reset_at` 到期后可以继续原任务，但不得自动重放此前失败的请求；`reset_seconds` 缺失或
  不可解析时按未知冷却保守阻塞，需人工核实。`/v1/models` 能列出模型不证明额度已恢复，冷却未结束
  时禁止调用；成功调用（身份验证通过且已入账）后才清除 `waiting`。

### 4. 九段上下文与 20000 字符预算

`brief` 生成每轮注入块，固定九段：原始目标；完成条件与边界；已确认决策；工作项进度；
往轮判断；待解决；证据索引；预算/配额/检查点/无进展；声明。当前轮之外的历史轮次只回灌
有判断价值的摘要，不整段堆叠。

- 总预算 `MAX_RENDER_CHARS=20000`；超预算按固定阶梯裁剪：证据索引 20→10→5→0，
  往轮摘要 1200→400，工作项结果 600→200，决策 8→3；最低档把**已终态**工作项压成
  `id(status)` 汇总（未终态项仍完整保留，含执行反馈与阻断原因），历史只在状态文件里保留，
  不删除。任何阶梯仍超预算时失败关闭并提示精简合同，不静默丢弃保护段。
- 保护段（目标、完成条件、边界、收口状态、阻断原因）按
  `MAX_PROTECTED_SECTION_CHARS=6000` 完整注入；单条条件描述超 `MAX_CRITERION_CHARS=800`
  只截断该条并标注，但**条目一条都不省略**；条件段整体超 6000 字符时明确拒绝渲染
  （提示 `contract --amend` 精简或拆分任务），不在不完整合同上做判断。工作项标题与
  预期证据分别以 300/400 字符为限，超出同样显式标注。
- `brief --no-stamp` 只预览，不更新无进展指纹；正常 `brief` 记录本轮问题摘要与六维进展，
  但不递增 `round_seq`（轮次只由真实调用推进）。`loop-step --stamp` 也可更新该指纹。

### 5. 三态协议

- GPT 的 `verdict` 映射：`pass`→stop，`partial`→continue，`blocked`→blocked。
- **缺失 verdict 不等于 PASS**；旧回复、无安排块的咨询轮、被结构校验拒绝的轮次都不改变
  控制状态。PASS 只认 GPT 明示值，主代理不得把 PARTIAL 改成 PASS。
- `pass_pending`：最近一次**带判断**的已验证轮为显式 pass 时成立，直到收口。此期间
  `ready_actions` 为空，不领取也不执行普通工作项；后续若出现新的 non-pass 判断，旧 PASS
  被覆盖，不能再收口。

### 6. PASS 四门

`closure-check` 只读计算，`loop-step` 在 PASS 挂起时把结果写入 `closure.gate`；
`closure --close` 要求最近 PASS 轮且四门全过。

- **G1 工作项**：全部终态；`dropped` 必须有原因。
- **G2 逐条条件**：核销轮次取**最近一次带判断的已验证轮**且该轮 verdict=pass；若最新判断是
  partial/blocked，旧 PASS 已被覆盖，`closure-check` 追加 `latest_judgment_not_pass` 并失败。
  条件已 confirmed 且 `criteria_sha256` 未漂移；全部 `met`；每条证据已登记、
  属于当前任务、摘要一致、文件存在可重读、未归档；且 `updated_round_id` 等于本 PASS 轮
  （旧轮判断不算本轮核销）。合同指纹须与当前目标/合同正文/条件集合一致：旧版本状态或
  未走 amend 的改动会报 `contract_fingerprint_mismatch`。不做 evidence 时效检查，时效由条件本身判断。
- **G3 停摆**：没有 running/failed/blocked 或依赖不可满足的挂置项。
- **G4 高风险复核（当前 fail-closed）**：普通风险自动通过；高风险必须存在可验证的
  **宿主可信原生独立复核记录**。阶段 0–3 的 Agent Bridge 只提供普通原生执行/复核回填，
  `g4_eligible=false`，**不构成 G4 依据**；当前 G4 仍不消费 Bridge 记录，因此对高风险任务
  一律返回 `native_review_missing`，任务停在 BLOCKED，不进入 REJUDGE，也不要求 GPT 再判。
  `--review-ledger` 是 GPT REVIEW 的审计记录，不是 G4 依据；即使台账字段全部有效、
  GPT 返回 PASS、或 PLAN → REVIEW 辅助调用成功，也不能通过 G4。手写同构 JSON、
  任务状态文件、`source=native` 等调用者可填写标签同样不接受。
  不得手工补台账、伪造归属或声称 G4 已满足；原生可信复核记录接入前，高风险任务不能自动收口。
  **当前能力缺口**：阶段 0–3 的 Bridge 尚未绑定 `task_id` / `contract_sha256` / `round_id` 与证据，
  因此不实现 `--native-review` 专用入口，也不接受 `source=native` 等调用者可填写标签；遇到该缺口
  直接 BLOCKED 并报告，不进入 REJUDGE 循环（不是 GPT 再判一次能解决的问题）。独立复核由 ZCode 主代理
  按 `AGENTS.md`《委派与独立复核》用原生子智能体在独立上下文完成；合同修改后，旧合同下的复核记录
  自动失去 G4 资格，必须按新合同重新完成。低风险任务不受影响；用户主动终止用
  `closure --force-close --reason`，`closed_by=user`，不冒充 PASS。

### 7. `pass_pending` 执行锁

从显式 PASS 到收口之间，执行面锁定：不发起新工作项、不把「继续干活」当作补充证据来绕开四门、
不以主代理自检替代核销。此期间主代理只做三件事：本地四门核销、失败原因报告、收口或等待用户决策。
收口后锁仍生效（见第 12 条）。

### 8. `loop-step` 阶段优先级

`loop-step` 不调用 GPT，只按状态求值并落盘四门结果，阶段优先级固定：

`CLOSED` > `RECOVER` > `BLOCKED` > `REVIEW_PENDING` > `QUOTA_WAIT` > `CLOSURE_CHECK` > `REJUDGE` > `EXECUTE` > `GUIDANCE_NEEDED`

- `CLOSED`：已收口，只允许只读命令与用户授权 reopen。
- `RECOVER`：存在 `pending_call`，先按 `call_id` 恢复，不得发起新调用。
- `BLOCKED`：最近判断为 blocked，或高风险任务缺少原生独立复核记录（外部能力缺口），
  等待用户决策/授权或必要前提。
- `REVIEW_PENDING`：native 执行结果已回填但独立复核未完成。即使 GPT 已 PASS，也不
  进入最终判断/收口；先回填复核，再显式 `drive`。该阶段先于 `QUOTA_WAIT` 求值。
- `QUOTA_WAIT`：配额/冷却阻塞；若已有 PASS 挂起，仍先做本地四门核销，失败则停在
  `QUOTA_WAIT`（此时 `pass_pending` 可能为 true）。不调用 GPT、不执行普通工作项。
- `CLOSURE_CHECK`：PASS 挂起、native 复核完成且四门通过，下一步按四门收口（冷却期内也可本地收口）。
- `REJUDGE`：PASS 挂起但四门失败，或 native 复核有 blocking finding/verdict≠pass，
  需要补充证据或由 GPT 重新判断；G4 缺少原生复核属于外部能力缺口，不走本阶段，直接 BLOCKED。
- `EXECUTE`：有依赖已满足的待执行工作项。
- `GUIDANCE_NEEDED`：没有可执行项也没有 PASS，需要 GPT 给下一步指导。

`--json` 返回 phase、pass_pending、revision、checkpoint、no_progress、pending、quota、
ready_actions、stalled、criteria 计数、gate、notes 与 next_commands；按 next_commands 行动，
不要自行发明阶段。

### 9. REJUDGE 重新判断流程

1. 先读取 `closure.gate.reasons` 的失败码（G1–G4）与缺口；native 任务还要读取
   `native_review.reasons`（`native_review_missing` / `native_review_blocking`），不猜原因。
2. 用 `report --blocked --reason <失败码与缺口>` 留痕（绑定最近已验证轮次）。
3. 带同一 `--dispatch-state` 发起新调用，让 GPT 在补充证据/修正方案后重新给出
   `pass` / `partial` / `blocked`；不得把旧 PASS 原样复用，也不得由主代理改判。
   native 复核的 blocking finding 必须原文进入本轮提示，SOL 需明确决定修复还是继续。
4. 新判断为非 pass 时，旧 PASS 被覆盖；为 pass 时重新走四门与 native 复核完成度检查。
   四门仍失败则继续 REJUDGE，直到通过、blocked 或用户终止。**例外**：G4 返回
   `native_review_missing` 时属于外部能力缺口，停在 BLOCKED，不发起新的 GPT 重判。

### 10. 8 轮检查点（观察信号，不停止）

`budget.checkpoint_every=8`；`round_seq` 为 8 的倍数时 `loop-step` 返回 `checkpoint=true`
（第 8、16、24… 轮）。2026-11-08 起检查点**只提示、不停止**：`drive` 仍按「是否有新增
工作或证据」继续推进，只把已用轮次、进展与剩余风险写进输出，供汇报与回顾使用。
`--rounds-limit` 同样只保留为观察字段，不再是软停止条件；不得用重建状态、清空轮次或换
状态文件来伪造进度。

### 11. 无进展检测

六维进展指纹：工作项、报告、证据、决策、完成条件、收口。连续 2 轮不变或同一工作项失败 ≥3 次
即 `no_progress`，但**检测不终止任务**：`drive` 汇总失败、未解决事项与新增证据交 SOL 重新
分析（原因、旧方案失效证据、替代路径、下一步、完成条件/证据、授权边界），执行后再次 REVIEW。
诊断材料无新信息时不重复请求（`no_progress_no_new_information`）；REVIEW 恢复分析须显式
`--auto-review`。仅无安全替代/缺决策/授权/资源/外部硬限制才 BLOCKED 并须举证；不得重复失败
方案、伪造进展或降低验收标准。

### 12. CLOSED 后的停止规则

- `closure.closed=true` 后，`record` / `decide` / `evidence` / `report` / `contract --amend` /
  `quota` 写入 / `ingest` / `recover` 全部拒绝；只允许 `next`、`brief --no-stamp`、
  `closure-check`、`loop-step` 等只读/求值命令（普通 `brief` 会写进展指纹，收口后同样被拒绝）。
- `loop-step` 返回 `CLOSED`；不再发起 GPT 调用、不执行工作项、不追加新决策。
- 继续工作只有一条路：用户明确授权 `closure --reopen --reason ...`。重开会保留历史与
  审计记录，但**作废全部显式 PASS 轮**，回到 GUIDANCE_NEEDED，必须重新取得 PASS 并重新核销四门。
- `--force-close` 只记录用户终止（`closed_by=user`，无 verdict），不是 PASS，也不能用
  force 绕过四门后宣称完成。

## 自动协作驱动（阶段 4）

阶段 2 已能逐条启动、登记、判断与核销，但每轮仍由主代理人工串联：读 `loop-step` → 判断是否该调用 →
拼 Prompt → 调 `call_gpt.py` → 写 report / evidence → 再下一次。`scripts/copilot_auto.py drive`
把这条机械链路收敛为一个入口；它不改任何门禁，只消除逐轮中转和漏步。

### 1. 执行边界

- **主代理仍是唯一执行方**：`drive` 不执行工作项、不派子代理、不生成「已完成」结论，只返回执行包
  供主代理用原生工具完成。SOL 保持零工具，不做工程执行。
- **PARTIAL 不是终态**：SOL 给了 `copilot-plan` 就按 `actions` 执行；没给可执行安排时，驱动按未闭合
  条件与回复原文在本地派生一个 `origin=partial-without-plan` 工作项并进入 `EXECUTE`，不需要用户传话
  或逐轮授权。执行结果与证据回填后，下一次 `drive` 必须把新材料交回 SOL 重新判断——同一轮里已经有
  执行/复核/报告材料时不再派生，避免本地空转。
- 返回的执行包带 `work_order`：`goal / constraints / expected_evidence / completion_criteria /
  authorization / return_contract / executed_by / directive_complete`。它只补充执行视图，不改写 SOL
  字段；主代理按 `return_contract` 用 manifest 回传结果与原始证据。
- 循环持续到 PASS 收口或真实 BLOCKED（需要用户决策、授权、资源，或继续不安全）。不因轮次 2/3/5/8、
  检查点、累计 Token 或正常调用费用停止；也不在没有新增工作与证据时机械重复调用。
- 只复用现有实现：状态机 / 四门 / PASS 锁 / CLOSED 锁用 `copilot_dispatch.py`；调用 / 身份校验 /
  pending / 恢复 / 配额 / 审计用 `call_gpt.py`；证据包用 `bridge_evidence.py`，驱动层不复制这些逻辑；
  不递归启动 ZCode CLI、不读写宿主数据库、不调用未公开接口。
- **一次 `drive` 至多发起一次真实 GPT 调用**，单进程内不自动循环；轮次推进由下一次 `drive` 完成。
- 自动循环不绕过用户授权与高风险边界：高风险 G4 仍 fail-closed，`force-close` 仍不是 PASS。
  REVIEW 自动闭环必须显式 `--auto-review`（表示用户已明确要求连续正式 REVIEW）；未显式授权时 `drive` 拒绝 REVIEW，
  仍走复核台账人工路径。启用后每轮走 `call_gpt.py --review-ledger`（默认
  `<work-dir>/review-ledger.json`），第 2 轮起由驱动自动生成 `--extra-round-reason` 并绑定
  `new-evidence`，不要求用户逐轮传话。自动生成的文字只陈述事实（第几轮、上一轮裁决、本轮
  实际提交了哪些 manifest 证据、仍未闭合的条件），**不预先断言「新增/哈希变化」**——是否构成
  新增证据由台账按内容哈希判定，自动文字不能替调用方取得新增证据资格。成本与 Token 只记录，
  不作为停止门。
- `execution_backend=native` 时 `--result-file` 关闭，执行/复核分别用
  `copilot_auto.py ingest --native-result` 纯本地回填（0 请求、不改轮次），复核回填后再由显式 `drive`
  让 SOL 判断；`drive` 不自动派发 Reviewer、不自动生成复核信封，也不把普通原生复核升级为 G4。
- 默认不设请求数/Token 上限：用量只记录进审计供观察，不阻断 `drive`；只有显式运营上限
  （`start --max-requests/--max-total-tokens`）被触及时才返回 `budget_exhausted`，且不创建 pending、
  不发送请求。

### 2. 单轮闭环

1. 主代理按返回的 `actions` 用原生工具执行工作项，把实际结果与原始证据写成 manifest。
2. `drive --result-file manifest.json` 依次完成：
   - 校验并原子回填工作项与证据，自动登记执行报告（明确标注由驱动生成，不是 GPT 判断）；
   - 把 manifest 中 `include=true` 的证据交给 `bridge_evidence.py` 生成证据包；
   - 组装 Prompt（含最近一次执行报告、REJUDGE 的四门失败码与输出契约）；
   - 调用一次 GPT，回复由 `call_gpt.py` 直接写回状态，**不需要用户转发或重新组织**；
   - 重新求值：`EXECUTE` 返回执行包；PASS 且四门通过自动收口；四门失败返回
     `rejudge_required`；BLOCKED / QUOTA_WAIT / RECOVER 停下等待。
3. 主代理继续执行下一个执行包，直到 `closed` 或需要用户决策的停止状态。

### 3. manifest

```json
{
  "round_id": "copilot-p4-r1",
  "actions": [
    {"id": "T1", "status": "done", "result": "实际结果摘要",
     "result_file": "/tmp/copilot/t1.log", "evidence_ids": ["E1"]}
  ],
  "evidence": [
    {"id": "E1", "file": "/tmp/copilot/t1.log", "kind": "test-output",
     "note": "T1 原始命令输出", "include": true}
  ],
  "report_note": "可选：执行方补充说明（非证据）"
}
```

- `actions` 非空；id 必须已存在且未终态；`status` 只能是 `done/failed/blocked/dropped`，
  且必须给出 `result` 或 `result_file`。自动驱动要求同步执行完成，不接受 `running`。
- `evidence[].id` 形如 `E1`；`file` 与 `text` 二选一；`include` 只接受 JSON 布尔
  （字符串 `"false"`、`0`、`null` 一律拒绝，避免静默把要求排除的证据放进 SOL 上下文）；
  `include=false` 只登记、不进证据包。
- 任一校验失败整次不写盘（退出码 2），不会出现半更新。
- 只能回填最近已验证轮次；`pass_pending` 期间或最近 decision 为 `stop/blocked` 时拒绝
  普通回填，必须先完成四门核销或由 GPT 重新判断。
- **阶段门禁先于回填**：回填前只读预检当前阶段，处于 `CLOSED`/`RECOVER`/`BLOCKED`/
  `QUOTA_WAIT`/`REVIEW_PENDING` 时整轮拒绝（退出码 2，提示先处理该阶段），不写任何状态；
  被复核的执行快照不会因新证据或新报告被污染。

### 4. 返回状态与退出码

| status | exit | 含义 / 下一步 |
|---|---|---|
| `execute` | 10 | 用原生工具执行 `actions`，完成后带 manifest 再 `drive` |
| `closed` | 0 | 四门通过且已收口；停止，不再调用 |
| `wait_user` | 11 | 无进展恢复待授权/无新信息、`rejudge_required`、材料不可执行；按 `reason` 处理，PARTIAL 不停 |
| `segmentation_required` | 10 | 预算无法提高/被拒；按分段计划取证 |
| `recover` | 11 | 有未结算 `pending_call`；先 `recover` 或显式 `abandon`，禁止重发 |
| `blocked` | 11 | 最近判断 blocked，或高风险 G4 缺少原生复核 |
| `quota_wait` | 11 | 配额/冷却中，不调用、不执行 |
| `failed` | 2–9 | 沿用 `call_gpt.py` 退出码（用法/上游/超时/审计/配额/身份） |

`--dry-run` 只校验 manifest、求值与生成 Prompt，不写状态、不建目录、不调用 GPT。
带 `--result-file` 时它只做只读预演（返回 `phase=DRY_RUN` 与 `state_phase`），
**不会生成证据包、不重读复核原文、不校验 bridge 侧摘要**：预览通过只说明 manifest 与阶段
可接受，不等于真实回填与入包一定成功。

### 5. 停止条件

- CLOSED、RECOVER、BLOCKED、QUOTA_WAIT 不发起新调用。**无进展先恢复分析，不直接终止**：六维无进展
  （streak ≥2 或同一工作项 failed ≥3）按第 11 节交 SOL 重析并继续执行；仅诊断材料无新信息才停为
  `no_progress_no_new_information`（REVIEW 须 `--auto-review`）。BLOCKED 仅在无安全替代 / 缺决策 /
  授权 / 资源 / 外部硬限制时成立并须举证。轮次 / 检查点 / Token / 费用不停，不得伪造进展或降标准。
- PARTIAL 按 §1 本地派生并进入 `EXECUTE`；新材料回填后必须交回 SOL 再判断，不允许本地连续派生。
- REVIEW 的独立正式裁决行（`裁决：PASS|PARTIAL|BLOCKED`）即使没有 `copilot-plan` 也会落账，并按
  `pass→stop / partial→continue / blocked→blocked` 派生 decision：PASS 进入四门核销，PARTIAL 触发
  本地派生，BLOCKED 停机。解析不到唯一裁决行时只留痕，不改变控制状态。
- 每一轮 PARTIAL 都应包含：未完成事项、问题与证据依据、具体下一步、完成条件、业务/授权边界、
  需要回传的证据、验收要求；缺失时按 `guidance_without_actions` 继续追问，而不是静默等用户。
- REJUDGE 先自动 `report --blocked` 留痕失败码，再发起一次重判；不在同一进程连续重判。真实调用失败
  时保留 `pending_call`，下一次 `drive` 只报告 `recover`，绝不自动重发。回复已取得但 plan 被拒
  （`dispatch_plan_rejected`）时，`drive` 对本次拒绝先 dry-run 再尝试 `recover-reply`；成功时复用原
  `call_id` 继续求值，**不发起新的 GPT 请求**，失败时仍按原退出码失败关闭。

### 6. 测试边界

`tests/test_auto_loop.py` 使用 127.0.0.1 Fake Upstream 与脚本化执行器验证驱动衔接，不联网、不产生
费用；它不证明 GPT 的真实判断质量，也不等于主代理真实自主执行。真实调用仍按 `call_gpt.py` 的授权、
身份与配额规则执行。**测试与证据的边界**：本地单元测试只证明状态机、命令与门禁逻辑，不等于真实 GPT 调用验证；
交付报告必须区分「已通过的本地测试」与「真实调用实测」，没有真实调用时不得写成已实测。

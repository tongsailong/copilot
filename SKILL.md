---
name: copilot
description: "仅在用户显式点名 Copilot / GPT 通道，或明确要求使用本 Skill 时调用：经 CPA 零工具通道调用 GPT-6.1-SOL（reasoning.effort=high），承担完整 ChatGPT 认知角色——交流问答、分析推理、产品探索、需求与方案规划、技术评估、执行指导、进度判断与结果验收；CONSULT / PLAN / REVIEW 只是内部模式与留痕契约，不限制任务范围。未点名一律不调用——仅要求独立复核、代码审核、第二意见或复杂任务协作不构成授权；未点名时的独立复核按 `AGENTS.md`《委派与独立复核》使用 ZCode 原生子智能体的独立上下文；任务复杂、多文件、多代理协作、存在可选模式、其他复核通道不可用或调用失败，都不构成切换理由。同一长任务可在关键阶段多次调用，每轮须有明确目标与新增输入。唯一正式 GPT 助理入口。"
when_to_use: >
  仅在用户显式点名 Copilot / GPT 通道（含 `/copilot ...`），或明确要求使用 copilot Skill 时调用。
  用户点名后由主代理按任务性质选内部模式（CONSULT / PLAN / REVIEW 只决定留痕与输出契约），
  走 CPA 零工具通道：`gpt-6.1-sol` + `reasoning.effort=high`。
  未点名不调用：独立复核、代码审核、第二意见按 AGENTS.md《委派与独立复核》使用 ZCode 原生子智能体的独立上下文；
  任务复杂、涉及多文件、需要多代理协作、存在可选 REVIEW 模式、按风险分档需要独立复核、
  其他复核通道不可用或调用失败、主代理自认为判断无法自证，都不构成调用或自动切换的理由。
  用户已显式启用的长任务中，只有出现新的关键证据、实质性变化或明确未解决的问题时才再次调用：
  执行中发现会实质改变方案的新事实、关键决策无法可靠确定、出现重要阻塞、
  原计划已失效，或整改后需要定向复验。
  不适用：用户未点名的任何判断、规划、指令编写或评审；目标明确、低风险、可直接执行的
  常规编码 / 测试 / 文件操作 / 查询；仅因任务复杂、涉及多文件、需要多代理协作，
  或存在可选的 PLAN / REVIEW 模式；已有充分证据的重复确认，
  前次判断尚未执行且没有新证据的机械重问。
---

# copilot：GPT-6.1-SOL 完整认知角色通道

copilot 是本宿主（ZCode）内部的**完整 GPT 认知入口**：它承接用户日常交给 ChatGPT 的那部分工作，
不是执行体，也不是调查者，更不是独立复核通道。
它**只在本通道内**工作：常规任务的规划与调度由 ZCode 原生负责，
并且只有用户显式点名 Copilot / GPT 通道或明确要求使用本 Skill 时才由本通道承接（见「何时调用」）。

- **ZCode 主代理（继承当前会话模型）**：探索现场、读取与修改文件、运行测试、采集原始证据；承接 copilot 的安排，
  直接执行或经原生 Agent 子代理派发；回填真实结果；**负责执行自检与必要的独立复核**。
- **copilot（GPT-6.1-SOL High）**：承担完整 ChatGPT 认知职责——交流与知识问答、问题讨论与分析推理、
  产品探索与需求分析、产品层方案规划、技术分析与方案评估、调研结果分析、任务定义与执行指导、
  按真实回报判断进度、对照原始目标验收、给出下一步要求。
- **宿主子代理**（继承主代理模型，如 general-purpose）：承接具体调查、代码检查、资料收集、测试与整改；
  有工具、能取证，但不是判断方。

**调查权不下放给 GPT，执行细节不预先规定。** copilot 的安排必须由主代理承接；
不得把调查任务委托给 GPT。

**独立复核不归本通道。** GPT 不读文件、不跑命令、不比对实际 diff，不具备现场取证能力，
因此不承担独立复核：独立复核由主代理用原生子智能体在独立上下文完成（见下文
「独立复核与 copilot 是两件事」）。REVIEW 模式是**隔离证据上的裁决能力（历史兼容）**，
不是独立复核，两者不得互相冒充。

**入口**：用户 `/copilot ...`、明确要求 GPT / copilot 判断、评审、规划或复核，或明确要求使用本
Skill 时，**在用户点名之后**必须实际走一次通道
（`bridge_evidence.py` 取证 → `call_gpt.py` 调用），不能只按本 Skill 的原则自行分析、
然后把结论说成「已由 copilot 判断」。用户未点名时不调用，也不因加载本 Skill 而调用。
用户说「直接执行 / 按建议执行」时，
执行的是通道返回的 `copilot-plan` 工作项，不是跳过 GPT 判断。没有上文（新会话、上下文丢失）时，
先从已有交付物、`/tmp/copilot` 产物、GPT REVIEW 台账或用户处恢复目标与上一轮结论；恢复不了就先问，
不要冒充「按上一轮建议执行」。

默认交付是**给主代理执行的工作安排**，不要求用户做复制粘贴中介；只有用户明确要求独立指令文本时，
才输出一个标准 Markdown 代码块（目标 / 范围 / 验收标准 / 约束 / 要补的证据）。

**零工具是能力级约束**：GPT 只经 `call_gpt.py` 直连 CPA `127` provider 的 `/v1/responses`，
请求体永不包含 `tools`，`tool_choice="none"`，并显式携带 `reasoning.effort="high"`
（`--reasoning-effort` 可显式降档，绝不自动降档）。它不能读本机文件、执行命令、搜索、派发子代理，
也不得作为执行子代理或复核子代理被派发（宿主子代理由主代理显式指定模型与任务，
本 Skill 禁止把 GPT 派为子代理）。独立复核按 `AGENTS.md`《委派与独立复核》分流：用户明确要求，或风险分档
确需独立复核时，由主控使用 ZCode 原生子智能体的独立上下文（只读、与实际执行上下文隔离；
不经本通道，不静默降级为主控自检）；详细边界见下文「独立复核与 copilot 是两件事」。
**独立复核是主代理的职责，不是 GPT 的职责**；确需 GPT 的隔离证据裁决时，仍须用户显式点名本通道，
且那是裁决，不是独立复核。

## 模式与路由

CONSULT / PLAN / REVIEW 是**内部模式**，不是调用范围：它们决定留痕字段、证据纪律与输出契约，
不限制 GPT 能处理什么任务（交流问答、产品探索、方案规划、技术评估、执行指导、验收都在范围内）。

| 模式 | 解决什么问题 | 输出 | 执行方 |
|---|---|---|---|
| CONSULT | 交流问答、问题分析、事实判断、风险识别、产品与需求讨论、方案或文档评审 | 结论—依据—建议；必要时定向取证项；默认不出三态裁决 | 主代理，或按需派宿主子代理 |
| PLAN | 产品层方案规划、方案比较与选择、成本/风险权衡、行动安排与分工 | 目标 / 完成条件 / 边界 / 必要背景 / 方案选择与取舍 / 验收标准 / 工作安排 / 决策失效条件 | 主代理承接，可派宿主子代理 |
| REVIEW | 对交付物或改动做 PASS / PARTIAL / BLOCKED 证据裁决与收口判断（历史兼容） | 三态裁决 + 最小整改项 + 是否收口 | 主代理承接整改 |

REVIEW 的正式裁决必须是回复中**独立、唯一**的一行 `裁决：PASS|PARTIAL|BLOCKED`；
`最终裁决：` / `整体结论：` 只在明确且唯一时兼容。代码块、引用块、示例里的裁决不算，
裸 `结论：` 与 `copilot-plan` 的 decision 也不是三态裁决。`call_gpt.py` 默认注入这段输出契约；
解析不到或出现冲突时按「无裁决」记录（不得当 PASS），也不自动重发付费调用。

**三态两个来源（必须标注）**：`验收：PASS|PARTIAL|BLOCKED` 是主代理对照原始目标给出的验收结论；
`裁决：PASS|PARTIAL|BLOCKED` 是本通道在隔离证据上给出的裁决。两者并存时分别标注，
不得用一个冒充另一个（`copilot-plan` 的 decision 也不是三态）。

三种模式全部走 CPA（`127` provider）的 `gpt-6.1-sol`，Responses API，零工具，请求体显式
`reasoning.effort="high"`。`call_gpt.py` 只接受这三种模式；`DECIDE` 已合并（方案选择归 PLAN，
问题判断归 CONSULT，收口归 REVIEW），传入会被拒绝，不存在第四条调用分支。

`--model` 只能覆盖 CPA 通道模型：接受裸 ID 或 `127/` 前缀；其他 provider 前缀、空模型名与被取代/
退役 ID（`gpt-6.1-sol-high`、`gpt-6.1-sol-max`，已退役）在发送前拒绝。
`gpt-6-astra-high` 等旧入口仍可显式指定（回滚路径，保留不删），但不再是默认值、不会被自动选用；
`--reasoning-effort` 只按显式值发送，不做自动降档或换档重试。

宿主（ZCode）自身的规划、子代理、看图、常规复核与自动命名走宿主主模型与原生能力
（以 ZCode 实际模型配置为准）。SOL 只在显式调用本 Skill 时使用，不是日常执行或复核模型。
TypeSafe / `jev-latest` 是 Reasonix 侧的底层决策能力（其 `system_one` / `decision_model` 机制本轮
未迁移到 ZCode，`jev-triage` 仅存在于 Reasonix 侧）；copilot 不承载它，也不得把它的结构化问题
改发 CPA 冒充同一裁决。

## 何时调用

**显式启用是硬门槛**：只有用户明确点名 Copilot / GPT 评审通道（含 `/copilot ...`），或明确要求
使用本 Skill 时才调用。以下情况都**不构成**授权，也不构成自动切换理由：

- 用户仅要求独立复核、独立审核、代码审核、第二意见或复杂任务协作；
- 任务复杂、改动跨多文件；
- 需要多代理协作或派发子代理；
- 存在可选的 PLAN / REVIEW 模式（模式可用 ≠ 本次该用）；
- 按 `AGENTS.md`《委派与独立复核》的风险分档判定需要独立复核；
- 原生复核子智能体暂不可用、调用失败或输出不充分；
- 主代理自认为判断无法自证、认为 GPT 会有增量价值；
- 其他 Skill 规定的必经评审门（如 designwise 的 Gate 1 / Gate 2）：这类门不构成 Copilot 授权，
  未点名时按该 Skill 的降级路径执行；《委派与独立复核》意义上的代码/变更独立复核仍走原生子智能体。

用户未点名时：独立复核一律使用 ZCode 原生子智能体的独立上下文；其余任务由主代理或原生子代理
完成，不得自动改走本通道。

**用户点名后的路由**：按用户意图选模式，并在调用前确认本轮要解决的具体问题与证据范围。

- 实质方案取舍或权衡，主代理有多个都成立的候选、缺少外部裁决依据 → CONSULT / PLAN；
- 影响风险判断的证据需要隔离裁决（安全与隐私、权限边界、关键业务链路、全局执行机制等
  高影响改动，或证据相互冲突）→ CONSULT / REVIEW；
- 视觉方向 / 阶段门槛等必须基于真实像素的独立评审（`--image`）→ CONSULT / REVIEW；
- 用户明确要求 GPT / copilot 复核或收口裁决 → REVIEW。

用户点名不豁免证据纪律与授权边界；证据不足时先补证，补不到按缺证处理。

**独立复核与 copilot 是两件事**：独立复核按 `AGENTS.md`《委派与独立复核》由主控使用原生子智能体
的独立上下文（只读、新上下文），不经本通道；用户指定其它审核工具或明确要求只做主控自检时，
遵循用户指定。原生复核不可用、材料不足或调用失败时，按该节如实报告，不得声称独立复核已完成，
也不得静默改由本通道冒充同一复核。只有用户显式点名本通道时才额外调用 GPT；
「原生复核不足 + GPT 有增量价值」本身不构成调用授权。需要 copilot 判断也不代表可以省略独立复核。

**长任务的多阶段协作**（仅用于用户已显式启用本通道的任务；同一任务可以多次调用，但每次都有门槛）：

| 阶段 | 触发 | 模式 | 必须携带 |
|---|---|---|---|
| 开始 | 目标有歧义、方案有重要权衡、风险不明确，且主代理无法自证取舍 | PLAN 或 CONSULT | 原始需求、约束、现状与候选方案 |
| 执行中纠偏 | 新事实改变方案、关键决策无法确定、重要阻塞、原计划失效 | CONSULT 或 PLAN | 最新证据 + 已执行工作项与真实结果 |
| 阶段交付 | 需要判断继续 / 调整 / 返工，且该判断无法由现场证据直接推出 | PLAN 或 REVIEW | 该节点产物 + 执行证据 |
| 最终复核 | 高影响交付，且存在现场复核无法闭合的证据缺口 | REVIEW | 交付物 + 验收条件映射 + 已知缺口 |

同一任务复用同一交接状态与同一 GPT REVIEW 台账，不因「上一轮已收口」而新建。
**没有新增关键证据、没有实质性变化、也没有明确未解决的问题时，不重复 REVIEW**：
不要为「再确认一次」、为「看起来严谨」、为上一个未执行的建议重新开轮。
补证或整改后只复验受影响的条件与缺口，不重跑完整复核。

## 工作流程

### 1. 取证

先明确本轮要判断的条件，再只取覆盖这些条件的最小证据。

```bash
python3 ~/.zcode/skills/copilot/scripts/bridge_evidence.py \
  --goal-file /tmp/copilot/goal.md \
  --file 'src/main.rs::/fn handle_/' \
  --cmd 'test::cargo test --lib 2>&1' \
  --note '以下为执行方说明，不是证据：…' \
  --out /tmp/copilot/evidence.md --manifest /tmp/copilot/evidence.json
```

按需组合：`--file`（全文 / `::A/B` 行段 / `::/REGEX/` 命中行）、`--image`（真实像素）、
`--cmd`（真实执行，保留原始输出与退出码）、`--dir`（清单）、`--json`（子结构）、`--stdin`。
脚本负责脱敏、sha256 指纹、采集方式、预算截断与省略量标注；凭据类文件默认拒绝整读。
`--dir-glob` 过滤目录清单。`call_gpt.py` 会自动注入《证据自检表》（条目数 / ID / 未纳入证据及原因），
让缺证对 GPT 可见；不要指望 PASS 摘要掩盖缺口。

**复用已有有效证据**：同一轮已经有有效探针输出、截图或命令结果时，优先用 `--file` / `--stdin`
把同一份产物纳入证据包，不重复执行；如果两次采集之间现场可能变化，重新采集并在备注写明版本/时间。
相互独立的只读检查尽量合并到一条命令，避免为每个检查单独起一轮模型；但不得为了省步骤跳过
关键条件的取证或指纹校验。

**图片证据**：用 `--image` 声明，类型按文件头魔数判定；证据包只落元数据与机器可读引用，
`call_gpt.py` 读取同一文件、校验 sha256 一致后以内联 `input_image` 发送。图片不存在、损坏、
格式不受支持或指纹不符时明确失败，不得用路径、描述或 base64 文本代替像素。
单次/单张上限由脚本参数控制（采集侧 `bridge_evidence.py --image-max-bytes`、发送侧
`call_gpt.py --max-image-bytes`，默认均 8MB；`call_gpt.py --max-images` 默认 8 张），超出明确失败。
小尺寸视觉判断必须用原生像素；若放大，只能用整数倍最近邻，并标注原始尺寸与放大倍数。
2026-10-16 实测可用格式：png / jpeg / webp / gif；bmp 不在本地支持格式内（魔数校验即拒绝，
不会出站），通道变化后需重测。

**编号顺序**：证据包按 `--file` → `--image` → `--cmd` → `--dir` → `--json` 生成 E 编号。
先取证、确认实际 ID，再写任务文本；不要预设图片是 E1/E2/E3。

### 2. 调用

```bash
python3 ~/.zcode/skills/copilot/scripts/call_gpt.py \
  --mode REVIEW \
  --subject '<交付物稳定标识>' \
  --review-ledger /tmp/copilot/review-<交付物标识>.json \
  --prompt-file /tmp/copilot/task.md \
  --evidence /tmp/copilot/evidence.md \
  --audit-log ~/.zcode/skills/copilot/.audit/copilot.jsonl \
  --out /tmp/copilot/copilot-reply.md
```

任务文本必须写明：用户原始需求（原话、成功标准、约束与授权边界）、本轮模式与待决问题、
哪些是原始记录、哪些是解释。**不要把「PASS / 已完成」当材料**；只有自评、没有原始材料时先补证，
补不到就写明缺失。

**审计日志是正式调用的必需参数。** 缺 `--audit-log` 或路径写不进去时，脚本在出站前失败
（退出码 2，请求未发出）。每次调用先落盘一条 `call_requested`（含 `call_id`、`fsync`），
再发请求，最后写 `call_result`；两者靠 `call_id` 关联。结果记录写不进去时返回退出码 7，
**按「审计不完整」报告：请求可能已发出，不得当作成功，也不得重试**。
回复已取得但后置步骤（回复落盘 / 台账 / 交接状态）失败时，会追加一条 `call_post_failure`
并以退出码 2 结束；同一 `call_id` 至多只有一条 `call_result`（结果记录本身写失败时为 0 条）。
`--dry-run` 不出站、不写审计，但会读取或**创建/更新 GPT REVIEW 台账**，并可能写入 `subject` / `audit_log`
绑定；它不是完全无副作用的沙盒，正式复核前不要把它当成「只读预检」。

**模型身份是消费前提（硬约束）。** 只有退出码 0 **且**审计 `call_result` 里
`model_identity_ok=true` 的回复，才能作为有效 GPT 指导。请求模型与上游回显 `model` 必须一致
（只容忍回显两端空白）；
回显缺失、不可解析或与请求不一致时脚本返回**退出码 9**，并已丢弃该回复：stdout 无正文、
不写 `--out`、不入GPT REVIEW 台账、不写交接状态（也不覆盖状态里已有的成功调用凭证），
stderr 只留脱敏身份诊断。此时按**未取得有效回复**汇报，
不得转述其正文，不得据该 `call_id` 验收或补录；脚本不重试、不换模型、不降档，
是否重新发起由用户决定。手动 `--model` 指定**非默认**模型时仍是可用入口，但审计 / 台账 /
交接状态会标记 `model_source=override`，其回复按手动模式留痕，**不得冒充默认通道
（`gpt-6.1-sol`）的判断**；显式传入默认名按 `default` 留痕（身份仍按请求名校验）。
`--out` 只在成功且身份通过时写入；失败时既不写也不删除既有文件——那份文件是上一轮遗留，
不得当作本轮结果。**P0 之前写入的历史轮次没有身份凭证**：脚本不回灌其回复原文，也不让其
`decision` 控制工作项（轮次计数与已有工作项保留）；需要依据时重新发起调用，不得把旧轮次
当作已验证判断转述。审计记录缺少 `model_source` / `reply_sha256` / `model_requested`
等字段时同样按「凭证不完整」拒绝补录（相比旧版本删除 `model_source` 的默认回退，属于
有意的收紧：无法证明来源的响应不进入交接状态）。凭证里的 `mode` 也必须与挂起模式严格
一致；带旧审计（没有 `mode` 字段）的 pending 轮次无法按常规 `recover` 提交，只能用
`--abandon` 显式放弃——这是 fail-closed，不是静默放行。

**自动协作通道（带 `--dispatch-state`）的身份是固定档位，不接受覆盖。** 进入自动闭环
（`PLAN` 多阶段、续轮 REVIEW 等会写交接状态的调用）时，脚本在出站前硬性检查：
请求模型必须是 `gpt-6.1-sol`、`reasoning.effort` 必须是 `high`、来源必须是默认规则
（`model_source=default`，不是 `override`）；任何显式 `--model`（含显式写默认名）一律拒绝，
不做「手动覆盖后仍按标准通道留痕」的降级解释。审计路径必须是本 Skill 的标准日志
`~/.zcode/skills/copilot/.audit/copilot.jsonl`，任意其他审计文件都被拒绝——否则旧日志里的
成功记录可以被当成当前任务的凭证。自动化测试需要隔离路径时显式设置
`COPILOT_AUDIT_TEST_MODE=1`；**正式运行不得设置该变量**，它只用于声明「这是隔离测试」，
不改变生产信任判断。身份不一致仍按退出码 9 处理：不输出正文、不写 `--out`、不写台账、
不写任务状态，也不留下可被恢复消费的 pending。手动 `--model` 仍可在**不带**
`--dispatch-state` 的一次性调用里使用（Luna / Astra 等入口保留），但其结果按手动模式留痕，
不得冒充标准 SOL High 自动指导。

REVIEW 一律要求证据包；没有证据时不要调用，直接按 `PARTIAL（缺证）` 或 `BLOCKED` 汇报。
文本 REVIEW 还必须带 `--review-ledger`（GPT REVIEW 审计记录，**不构成 G4 依据**），
否则发送前拒绝（退出码 2）；唯一例外是显式 `--one-shot-visual-review` 且证据含图片的
一次性视觉评审，它不携带跨轮状态，不得用于续轮。CONSULT / PLAN 不需要台账。

CPA 响应可能超过前台 2 分钟：优先以后台任务运行（如 run_in_background），等待宿主的任务完成
通知再读取输出；不要用固定 `sleep` 反复轮询。需要主动检查时使用有界等待（例如先等 60–120 秒，
再查看进程、审计和输出各一次），不要在调用进行中不断起新回合。
超时与连接中断属于**请求结果未知**（可能已被上游接收并计费）：脚本不自动重发，只写审计并显式失败。
**已收到 HTTP 错误响应（含 429/5xx）也不自动重发**：状态码不足以证明上游没有处理请求或没有计费，
脚本保留原始状态码与输出留痕后直接失败。`--max-retries` 只对**可证明请求未建立**的传输层失败
（如拒绝连接、域名解析失败、网络不可达）生效；native 自动任务默认 `0`（只尝试一次），
legacy/手动通道沿用默认 `2`。任何情况下都不要在外面套一层重试——那会绕过这层付费保护。

native 任务另有两道预算门（`budget_status`，从审计重建，进程重启不归零）：
- **请求数硬上限**（默认每任务 6 次）：按 `call_requested` 在出站前精确计数；达到即拒绝，
  未创建 pending、未发 HTTP、未递增轮次（退出码 12 / drive `budget_exhausted`）。
- **累计 Token 阈值**（默认 500k）：按 `usage` 的输入/输出/总量核算。usage 只有响应返回后
  才知道，因此这是「达到即停」的阈值而非严格上界，单次调用最多使总量超出一个调用的量；
  另按上一次调用的总量做保守预估拦截。上游未返回 usage 时只计 `unknown_usage_calls`，
  此时仅请求数上限可强制执行。

### 3. 承接执行与反馈

**按需建立交接状态**：PLAN、多阶段任务或预期会产出工作项的 REVIEW，在首次调用前
`start` 并传 `--dispatch-state`。一次性 REVIEW 不强制创建 dispatch：台账与审计已经记录
本轮与回复，没有工作项时多一份状态文件只会增加漂移。若回复出现 `copilot-plan` 而当时
未传状态，用 `ingest --reply-file` 从已保存的回复补录，不为补录重新调用 GPT。
补录**必须**带 `--call-id`（可选 `--audit-log`，默认本 Skill 的标准审计日志），脚本据此从审计
重建来源凭证：该 `call_id` 必须对应一条 `ok=true` 且 `model_identity_ok=true` 的 `call_result`，
回复文件摘要必须与审计里的 `reply_sha256` 一致，调用模式必须与状态/本轮模式一致。
**任务级状态（本轮 `start` 建立）的归属只认 pending 凭证**：状态持久化
`task_id` / `round_id` / `contract_sha256`（目标、完成条件与边界的规范化摘要），`ingest` 逐项核对
任务、轮次、合同指纹、模式、模型身份与回复摘要，任一不符即拒绝；`call_id` 必须是本轮
`pending_call` 记下的那一个，同一凭证只能消费一次（重复 `ingest` 被拒绝）。调用成功后的提交由
`call_gpt.py` 一次原子完成（回复凭证 + 轮次 + `last_call` + 清理 pending），**不再用「审计日志里
最后一条成功调用」推断归属**——同模式的不同任务、不同轮次、不同合同因此不会互相补录。
**旧格式状态（P0 之前、没有 `task_id` 与合同指纹）只保留人工兼容入口**：仍按 `last_call` /
最后一条成功 `call_result` 的旧规则校验，用于已存在的手工流程；它不能进入新的自动协作任务，
需要自动闭环时用 `start --task-id ... --contract-file ...` 新建带任务凭证的状态，
而不是把旧状态改造成「看起来合规」。任何一项不满足都拒绝写入——不能只凭文件存在或正文格式
正确就接受；历史遗留的 `--out` 文件（含身份验证失败那轮留下的文件）不得当作本轮回复。

**请求在前、提交在后，mid-flight 中断可精确恢复。** 发送请求前，脚本先把本轮调用意图
（`task_id`、`round_id`、`contract_sha256`、`call_id`、`mode`、请求模型、effort、来源）
落盘为 `pending_call`；拿不到回复就不发请求。取得回复后先构造完整凭证（含 `reply_sha256` 与
`model_identity_ok`），验证通过再与轮次、`last_call`、pending 清理合并为**一次**原子写盘
（临时文件 + fsync + `os.replace`）。写盘失败时 pending 保留，状态不前进也不丢弃已成功取得的
回复；`recover` 只依据状态里的 pending 与本轮审计凭证续接，**不会重新调用 GPT**，因此不会
重复调用或重复消费凭证。`pending_call` 未结算时自动通道拒绝发起新调用（避免两轮意图交错）。
状态写入带比较交换（`round_seq` 与挂起 `call_id` 必须与磁盘一致）：两个进程共用同一状态时，
只有先落盘者拿到本轮，另一个在**发送请求前**失败——不会双计费，也不会互相覆盖已提交轮次。
**任务级自动协作必须声明模式**：`start --mode` 建议显式给出；状态创建时为空则由首次自动调用
钉住，之后不同模式一律被拒（空模式无法证明轮次属于同一契约）。一次性视觉评审
`--one-shot-visual-review` 不携带跨轮状态，不能与 `--dispatch-state` 组合。
`recover` 对「结果未知」（超时、网络中断、审计无结果）和「审计成功但回复已找不回」都不自动
清理：前者可能已计费，后者可能丢掉已取得的回复，都必须显式 `--abandon` 人工确认。

```bash
# 建立任务级交接状态：task_id 与合同指纹随状态持久化
python3 ~/.zcode/skills/copilot/scripts/copilot_dispatch.py start \
  --state /tmp/copilot/dispatch.json --goal-file /tmp/copilot/goal.md \
  --contract-file /tmp/copilot/contract.md --task-id copilot-p0-20261101 --mode PLAN
# 调用时加 --dispatch-state /tmp/copilot/dispatch.json
# （自动通道只接受 SOL High + 默认来源 + 标准审计日志，禁止 --model 覆盖）

python3 ~/.zcode/skills/copilot/scripts/copilot_dispatch.py next \
  --state /tmp/copilot/dispatch.json

python3 ~/.zcode/skills/copilot/scripts/copilot_dispatch.py record \
  --state /tmp/copilot/dispatch.json --id T1 --status done \
  --result-file /tmp/copilot/t1-result.md --evidence-id E1 --evidence-id E2
# 证据引用必须先登记、属于本任务；文件型证据会重读并校验摘要，任一不符整次回填失败

# 调整完成条件：已确认后必须由用户决策并给原因；实质修改会重算合同指纹并作废旧 PASS
python3 ~/.zcode/skills/copilot/scripts/copilot_dispatch.py contract \
  --state /tmp/copilot/dispatch.json --amend --criteria-file /tmp/copilot/criteria.json \
  --source user --reason '用户调整验收口径'

# 补录：--call-id 必填，脚本从审计校验身份与摘要后才写入
python3 ~/.zcode/skills/copilot/scripts/copilot_dispatch.py ingest \
  --state /tmp/copilot/dispatch.json --reply-file /tmp/copilot/copilot-reply.md \
  --call-id <该次成功调用的 call_id> --mode PLAN

# 恢复：按 pending 里的 call_id 续接提交，不重新调用 GPT
python3 ~/.zcode/skills/copilot/scripts/copilot_dispatch.py recover \
  --state /tmp/copilot/dispatch.json --call-id <pending 里的 call_id> \
  --reply-file /tmp/copilot/copilot-reply.md
# 只有人工作出放弃决定时才用 --abandon：调用前中断、结果未知（超时/网络中断，
# 可能已计费）、或审计成功但回复已找不回。放弃会留 abandoned_calls 记录，不产生轮次
python3 ~/.zcode/skills/copilot/scripts/copilot_dispatch.py recover \
  --state /tmp/copilot/dispatch.json --call-id <call_id> --abandon
```

copilot 需要推进时输出一个 `copilot-plan` 块，`call_gpt.py` 自动写回交接状态：

````markdown
```copilot-plan
{
  "decision": "continue",
  "summary": "一句话说明为什么这样安排",
  "actions": [
    {
      "id": "T1",
      "title": "短标题",
      "assignee": "deepseek",
      "goal": "要求的结果",
      "constraints": "不能做什么 / 授权边界",
      "expected_evidence": "需要返回哪些原始证据",
      "depends_on": []
    }
  ]
}
```
````

- `decision`：`continue` / `stop` / `blocked`；`stop` / `blocked` 不得再排工作项。
  `continue` 不带 `actions` 合法，表示继续已有工作项。
- `assignee` 只能是 `zcode`（主代理直接做）、`deepseek`（经宿主原生子代理派发）或
  `reasonix`（仅兼容迁移前历史安排）；不得派给 GPT。
  宿主当前没有可承接 `deepseek` 工作项的子代理类型时，如实按「无法派发」报告
  （BLOCKED，或由主代理直接执行并如实标注），不得冒充已派发。
- 同一 id 在续轮出现表示更新：只覆盖实际给出的字段，未重述字段保持原值；`done` / `dropped`
  的 id 不可复用。依赖缺失、未知、自依赖或成环会被拒绝。
- 多个 `copilot-plan` 块、无法解析的围栏、`stop/blocked` 带 actions 都会失败，
  不会静默当成没有安排。
- 无安排块是合法形态（CONSULT 常只给分析）；回复正文会作为历史前缀回灌下一轮，
  不作为当前事实。

**闭环**：提交目标、证据与上轮工作项状态 → copilot 判断下一步 → 主代理直接执行或派发 →
回填真实结果与证据编号 → copilot 决定继续、补证、改方案或收口。
多轮推进可用阶段 4 的 `scripts/copilot_auto.py drive` 自动串联以上机械步骤（见
「自动协作驱动（阶段 4）」）；主代理仍是唯一执行方，用户不需要逐轮转发。
简单任务直接做，不机械派发子代理；不为展示调度能力制造工作项。

## 原生 Agent Bridge（阶段 0–3.6，显式 opt-in）

当任务声明 `--execution-backend native` 时，`drive` 在 `EXECUTE` 阶段额外返回
`native_dispatch`（`copilot-native-dispatch/v1`）。主代理按其中的 `actions` 用原生工具或
`general-purpose` 子代理执行，**不得改写 SOL 的 title / assignee / goal / constraints /
expected_evidence / depends_on**；每个工作项已有稳定的 `dispatch_id` 与
`idempotency_key`，重复生成复用同一 ID。

执行与复核是**两份独立信封、两份独立报告**，并使用
`scripts/native_agent.py` 从宿主实际运行记录生成，而不是主代理手拼：

- 执行：`copilot-native-result/v1`，必须携带 `plan_call_id` / `plan_reply_sha256` /
  `dispatch_id` / `idempotency_key`、真实执行方 `agent_id` / `child_session_id` /
  `profile_id`、原始证据文件及 SHA-256、计时/调用统计。身份与模型来自 Agent
  `metadata.json` 和 rollout，证据摘要由脚本计算；用
  `copilot_auto.py ingest --state <state> --native-result <execution.json>` 回填。
- 复核：`copilot-native-review/v1`，用 `execution_result_sha256` 绑定被复核的执行结果；
  Reviewer 必须是独立子代理、`profile_id=reviewer`，只读检查真实文件/diff/日志/证据，
  `changed_files=[]`，`g4_eligible=false`；Reviewer 身份、实际模型和复核摘要同样由
  `native_agent.py review` 从运行记录填入。用 `ingest --native-result <review.json>` 回填。
- `ingest` 是**纯本地**回填：不调用 `call_gpt.py`、不产生 HTTP、不递增轮次、不创建
  `pending_call`；重复信封按结果 SHA 幂等；CLOSED 或存在 pending 时拒绝写入。
  SOL 的最终判断只能由随后**显式**运行的 `drive` 发起（`drive --native-result` 已停用）。
- `commands_run` 允许顶层 `;` / `|` 组合只读命令；Bridge 会按引号感知拆分后逐段校验。
  `&&`、`||`、重定向、反引号、`$()`、换行和未闭合引号仍失败关闭。
- `ingest --dry-run` 只校验不写盘；`--native-result` 只接受 native 状态，legacy 状态
  继续用 `ingest --result-file`，两者不能混用。
- **复核顺序**：执行结果回填后阶段为 `REVIEW_PENDING`；复核未回填时，即使 SOL 已给出
  PASS，也不进入 `CLOSURE_CHECK`/收口，且不发起新的 SOL 请求。复核有 blocking finding
  或 verdict≠pass 时进入 `REJUDGE`，SOL 必须看到具体问题并决定修复还是继续。
  `closure --close` 在收口前再次检查复核完成度；CLOSED 后 ingest 一律拒绝。
- 旧状态自动补 `execution_backend=legacy` 及空 native 字段，不改变原 legacy 行为。

**Reviewer Profile**：`~/.zcode/agents/reviewer.md`，静态配置为 `model: inherit`
（跟随 ZCode 主代理当前会话的 Provider/模型/推理级别，不固定模型名称）/
`Read,Grep,Glob,Bash` / 禁止 `Write,Edit`。Bash 仅允许只读检查，bridge 另有命令白名单；
这不是 OS 沙箱，也不是模型身份凭证。详细协议、字段、错误码与操作顺序见
`references/agent-bridge.md`。

**G4 不放宽**：Bridge 的普通原生复核记录 `g4_eligible=false`，不进入 G4；高风险任务仍
`native_review_missing` + BLOCKED。GPT REVIEW 台账、Agent 自报身份、手写同构 JSON 同样
不是 G4 依据。本阶段未实现 Reviewer 自动派发、SOL 自动闭环或收口（阶段 4 另行授权）。

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
```

- `decide` 单条上限 2000 字；`report` 上限 8000 字；报告是执行方反馈，不替代现场证据。
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
  `waiting` 期间发送前直接拒绝：不创建 pending、不发 HTTP、不改轮次与工作项。
  `reset_at` 到期后可以继续原任务，但不得自动重放此前失败的请求；`reset_seconds` 缺失或
  不可解析时按未知冷却保守阻塞，需人工核实。成功调用（身份验证通过且已入账）后才清除
  `waiting`。

### 4. 九段上下文与 20000 字符预算

`brief` 生成每轮注入块，固定九段：原始目标；完成条件与边界；已确认决策；工作项进度；
往轮判断；待解决；证据索引；预算/配额/检查点/无进展；声明。当前轮之外的历史轮次只回灌
有判断价值的摘要，不整段堆叠。

- 总预算 `MAX_RENDER_CHARS=20000`；超预算按固定阶梯裁剪：证据索引 20→10→5→0，
  往轮摘要 1200→400，工作项结果 600→200，决策 8→3。
- 目标、完成条件、边界、收口状态、阻断原因**永不裁剪**；裁剪量与省略原因会标注。
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
- **G2 逐条条件**：条件已 confirmed 且 `criteria_sha256` 未漂移；全部 `met`；每条证据已登记、
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
  低风险任务不受影响；用户主动终止用 `closure --force-close --reason`，
  `closed_by=user`，不冒充 PASS。

### 7. `pass_pending` 执行锁

从显式 PASS 到收口之间，执行面锁定：不发起新工作项、不把「继续干活」当作补充证据来绕开四门、
不以主代理自检替代核销。此期间主代理只做三件事：本地四门核销、失败原因报告、收口或等待用户决策。
收口后锁仍生效（见第 13 条）。

### 8. G4 高风险独立复核的边界

- **G4 只接受原生独立复核记录**，不接受 GPT 的任务 PASS，也不接受 GPT REVIEW 的裁决。
  GPT 返回 PASS 只表示它对当前证据的判断，不表示收口条件已满足，也不表示复核已完成。
- **独立复核由 ZCode 主代理负责**：按 `AGENTS.md`《委派与独立复核》，由主控用原生子智能体
  在独立上下文完成；GPT 不读文件、不跑命令、不比对 diff，不承担独立复核。
- **当前能力缺口**：阶段 0–3 已实现 Agent Bridge（普通原生执行/复核回填），但 ZCode 仍
  没有宿主可信绑定 `task_id` / `contract_sha256` / `round_id` / 证据的 G4 复核记录机制。
  Bridge 记录显式 `g4_eligible=false`，G4 不消费它；因此高风险任务暂时无法自动通过 G4，
  必须停在 BLOCKED 并报告缺口。这不是 GPT 再判一次能解决的问题，不应进入 REJUDGE 循环。
- 本阶段不实现 `--native-review` 的 G4 专用入口，也不接受 `source=native` 等调用者可填写
  标签；在用户授权并具备可信记录机制前，不得声称 G4 已满足。
- 合同修改后旧合同下的复核记录自动失去 G4 资格；高风险任务必须按新合同重新完成原生复核，
  在此之前仍保持 BLOCKED。

### 9. `loop-step` 八阶段

`loop-step` 不调用 GPT，只按状态求值并落盘四门结果，阶段优先级固定：

`CLOSED` > `RECOVER` > `BLOCKED` > `QUOTA_WAIT` > `REVIEW_PENDING` > `CLOSURE_CHECK` > `REJUDGE` > `EXECUTE` > `GUIDANCE_NEEDED`

- `CLOSED`：已收口，只允许只读命令与用户授权 reopen。
- `RECOVER`：存在 `pending_call`，先按 `call_id` 恢复，不得发起新调用。
- `BLOCKED`：最近判断为 blocked，或高风险任务缺少原生独立复核记录（外部能力缺口），
  等待用户决策/授权或必要前提。
- `QUOTA_WAIT`：配额/冷却阻塞；若已有 PASS 挂起，仍先做本地四门核销，失败则停在
  `QUOTA_WAIT`（此时 `pass_pending` 可能为 true）。不调用 GPT、不执行普通工作项。
- `REVIEW_PENDING`：native 执行结果已回填但独立复核未完成。即使 GPT 已 PASS，也不
  进入最终判断/收口；先回填复核，再显式 `drive`。
- `CLOSURE_CHECK`：PASS 挂起、native 复核完成且四门通过，下一步按四门收口（冷却期内也可本地收口）。
- `REJUDGE`：PASS 挂起但四门失败，或 native 复核有 blocking finding/verdict≠pass，
  需要补充证据或由 GPT 重新判断；G4 缺少原生复核属于外部能力缺口，不走本阶段，直接 BLOCKED。
- `EXECUTE`：有依赖已满足的待执行工作项。
- `GUIDANCE_NEEDED`：没有可执行项也没有 PASS，需要 GPT 给下一步指导。

`--json` 返回 phase、pass_pending、revision、checkpoint、no_progress、pending、quota、
ready_actions、stalled、criteria 计数、gate、notes 与 next_commands；按 next_commands 行动，
不要自行发明阶段。

### 10. REJUDGE 重新判断流程

1. 先读取 `closure.gate.reasons` 的失败码（G1–G4）与缺口；native 任务还要读取
   `native_review.reasons`（`native_review_missing` / `native_review_blocking`），不猜原因。
2. 用 `report --blocked --reason <失败码与缺口>` 留痕（绑定最近已验证轮次）。
3. 带同一 `--dispatch-state` 发起新调用，让 GPT 在补充证据/修正方案后重新给出
   `pass` / `partial` / `blocked`；不得把旧 PASS 原样复用，也不得由主代理改判。
   native 复核的 blocking finding 必须原文进入本轮提示，SOL 需明确决定修复还是继续。
4. 新判断为非 pass 时，旧 PASS 被覆盖；为 pass 时重新走四门与 native 复核完成度检查。
   四门仍失败则继续 REJUDGE，直到通过、blocked 或用户终止。**例外**：G4 返回
   `native_review_missing` 时属于外部能力缺口，停在 BLOCKED，不发起新的 GPT 重判。

### 11. 8 轮预算检查点

`budget.checkpoint_every=8`；`round_seq` 为 8 的倍数时 `loop-step` 返回 `checkpoint=true`
（第 8、16、24… 轮）。`--rounds-limit` 是软上限，不是硬截断；检查点不重置轮次计数、不清空历史。
到检查点时汇报已用轮次、进展、剩余风险与继续/收口的取舍，由用户决定；不得用重建状态、
清空轮次或换状态文件来绕过检查点。

### 12. 无进展检测

六维进展指纹：工作项、执行报告、证据、决策、完成条件、收口。连续 2 轮六维完全不变时
`no_progress=true`；同一工作项 `failed` 连续 ≥3 次会进入停摆提示。检测只提示，不自动
`dropped`、不自动改判；主代理应给出根因、补证或建议改判，不能靠重复同一请求刷轮次。

### 13. CLOSED 后的停止规则

- `closure.closed=true` 后，`record` / `decide` / `evidence` / `report` / `contract --amend` /
  `quota` 写入 / `ingest` / `recover` 全部拒绝；只允许 `next`、`brief --no-stamp`、
  `closure-check`、`loop-step` 等只读/求值命令（普通 `brief` 会写进展指纹，收口后同样被拒绝）。
- `loop-step` 返回 `CLOSED`；不再发起 GPT 调用、不执行工作项、不追加新决策。
- 继续工作只有一条路：用户明确授权 `closure --reopen --reason ...`。重开会保留历史与
  审计记录，但**作废全部显式 PASS 轮**，回到 GUIDANCE_NEEDED，必须重新取得 PASS 并重新核销四门。
- `--force-close` 只记录用户终止（`closed_by=user`，无 verdict），不是 PASS，也不能用
  force 绕过四门后宣称完成。

### 14. 中断恢复与配额等待的后续动作

- 调用前已落 `pending_call`；进程中断后 `loop-step` 返回 `RECOVER`。用 `recover --call-id`
  按审计凭证续接提交，**绝不重新调用 GPT**。结果未知（超时/网络中断/审计无结果）或审计成功
  但回复找不回时，不自动清理；只有人工确认后才可 `--abandon`（不产生轮次）。
- 配额/冷却期间停在 `QUOTA_WAIT`：不调用 GPT、不执行工作项；若已有 PASS 挂起，仍可做本地
  四门核销，通过即可收口。`reset_at` 未知时保守视为仍阻塞，必须人工核实。
- 解除冷却只有两种依据：用户确认，或一次**已入账且身份验证通过**的成功调用
  （`quota --clear --source call --call-id ...`）。`/v1/models` 能列出模型不证明额度已恢复；
  冷却未结束时禁止调用；`reset_at` 到期后可以继续原任务，但不得自动重放此前失败的请求。
  `waiting` 的解除仍只凭用户确认或一次已入账且身份验证通过的成功调用；`reset_seconds`
  未知时保守视为仍阻塞，先核实再发起。

## 自动协作驱动（阶段 4）

阶段 2 已能逐条启动、登记、判断与核销，但每轮仍由主代理人工串联：读 `loop-step` →
判断是否该调用 → 拼 Prompt → 调 `call_gpt.py` → 写 report/evidence → 再下一次。
`scripts/copilot_auto.py drive` 把这条机械链路收敛为一个入口；它不改任何门禁，
只消除逐轮中转和漏步。

### 1. 执行边界

- **主代理仍是唯一执行方**：`drive` 不执行工作项、不派子代理、不生成「已完成」结论，
  只返回执行包供主代理用原生工具完成。
- 只复用现有实现：状态机 / 四门 / PASS 锁 / CLOSED 锁用 `copilot_dispatch.py`；
  调用 / 身份校验 / pending / 恢复 / 配额 / 审计用 `call_gpt.py`；证据包用
  `bridge_evidence.py`。驱动层不复制这些逻辑。
- 不递归启动 ZCode CLI、不读写宿主数据库、不调用未公开接口。
- **一次 `drive` 至多发起一次真实 GPT 调用**，单进程内不自动循环；轮次推进由下一次
  `drive` 完成。
- 自动循环不绕过用户授权、成本预算与高风险边界：高风险 G4 仍 fail-closed，
  REVIEW 仍走复核台账路径（`drive` 只承接 PLAN / CONSULT），`force-close` 仍不是 PASS。
- `execution_backend=native` 时，`--result-file` 关闭，执行/复核分别用
  `copilot_auto.py ingest --native-result` 纯本地回填（0 请求、不改轮次）；复核回填后
  再由显式 `drive` 让 SOL 判断。`drive` 不自动派发 Reviewer、不自动生成复核信封，
  也不把普通原生复核升级为 G4。
- native 预算两道门：请求数硬上限（默认 6/任务，出站前按审计判定）与累计 Token 阈值
  （默认 500k，按 usage 核算，不是严格上界）。达到后 `drive` 返回 `budget_exhausted`，
  不创建 pending、不发送请求。

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
- `evidence[].id` 形如 `E1`；`file` 与 `text` 二选一；`include=false` 只登记、不进证据包。
- 任一校验失败整次不写盘（退出码 2），不会出现半更新。
- 只能回填最近已验证轮次；`pass_pending` 期间或最近 decision 为 `stop/blocked` 时拒绝
  普通回填，必须先完成四门核销或由 GPT 重新判断。

### 4. 返回状态与退出码

| status | exit | 含义 / 下一步 |
|---|---|---|
| `execute` | 10 | 用原生工具执行 `actions`，完成后带 manifest 再 `drive` |
| `closed` | 0 | 四门通过且已收口；停止，不再调用 |
| `wait_user` | 11 | 检查点 / 无进展 / 轮次上限 / `rejudge_required` / 指导未给动作；按 `reason` 处理 |
| `recover` | 11 | 有未结算 `pending_call`；先 `recover` 或显式 `abandon`，禁止重发 |
| `blocked` | 11 | 最近判断 blocked，或高风险 G4 缺少原生复核 |
| `quota_wait` | 11 | 配额/冷却中，不调用、不执行 |
| `failed` | 2–9 | 沿用 `call_gpt.py` 退出码（用法/上游/超时/审计/配额/身份） |

`--dry-run` 只校验 manifest、求值与生成 Prompt，不写状态、不建目录、不调用 GPT。

### 5. 停止条件

- CLOSED、RECOVER、BLOCKED、QUOTA_WAIT 不发起新调用。
- 8 轮检查点、`--rounds-limit`、六维无进展（streak ≥2 或同一工作项 failed ≥3）
  在**出站前**停止，不靠「先调用再检查」多刷一轮。
- REJUDGE 先自动 `report --blocked` 留痕失败码，再发起一次重判；不在同一进程连续重判。
- 真实调用失败时保留 `pending_call`；下一次 `drive` 只报告 `recover`，绝不自动重发。

### 6. 测试边界

`tests/test_auto_loop.py` 使用 127.0.0.1 Fake Upstream 与脚本化执行器验证驱动衔接，
不联网、不产生费用；它不证明 GPT 的真实判断质量，也不等于主代理真实自主执行。
真实调用仍按 `call_gpt.py` 的授权、身份与配额规则执行。

**测试与证据的边界**：本地单元测试只证明状态机、命令与门禁逻辑，不等于真实 GPT 调用验证。
交付报告必须区分「已通过的本地测试」与「真实调用实测」；没有真实调用时不得写成已实测。

## 证据纪律（硬约束）

1. 交给 GPT 的是原始产物，不是「PASS / 已完成」的结论；关键证据没进上下文就先补取。
2. 证据确实取不到（需授权、环境缺失、产物已覆盖）时，在任务文本写明「无法取得及原因」，
   预期 PARTIAL（缺证）或 BLOCKED；不得伪造、不得推断成已完成。
3. 不转发摘要冒充证据：测试结论要附原始命令输出与退出码。
4. 证据由执行方采集不等于自动可信；按可核验性判断，任何证据不得证明其覆盖范围之外的事项。
5. 视觉问题只用 `--image` 的真实像素判断；不得用文件名、路径、尺寸数字或文字转述代替像素。
6. 材料中要区分：已验证事实（含来源与 sha256）、推断（写依据）、尚未验证、证据矛盾、
   影响结论的缺口。

## REVIEW 收敛

REVIEW 的目标是用证据收敛到一个可交付裁决，不是无限补证。CPA 调用无状态，跨轮依据只能来自台账。

- 首轮前建立验收条件清单，逐条映射到证据 ID；一次覆盖所有可取得的关键条件，取不到的写明原因。
- `--review-ledger` 自动合并历史证据、按内容去重并统一重编号、注入往轮裁决、检查轮次上限与
  本轮是否真有新增/变化证据、记录本轮裁决与指纹。历史证据包必须保留到复核结束。
- **台账绑定交付物与审计日志**：新建台账必须用 `--subject` 声明稳定的交付物标识，
  并在台账中持久记录本次使用的审计日志（规范化绝对路径）。同一交付物只能有一份台账在推进：
  - 换台账路径、删台账重建、复制台账另起一份、或更换审计日志，都不会把轮次归零：
    前三种会被拒绝并指出已有台账，换日志会被拒绝并指出台账绑定的日志。新建台账会丢掉
    往轮裁决与证据指纹，不能用来重置轮次。
  - **新建台账只接受标准审计日志**（`~/.zcode/skills/copilot/.audit/copilot.jsonl`）。
    轮次核对只能覆盖「本次日志 + 标准日志」；若允许用其他日志建账，先用日志 A 复核、
    删掉台账、再用日志 B 建账就会查不到 A 里的历史，把轮次重置为第 1 轮。
    因此非标准日志在新建时直接拒绝，已有台账也只能继续用其绑定的日志。
  - 证据包丢失不构成新建台账的理由，先恢复证据包；恢复不了按 PARTIAL（缺证）/ BLOCKED 汇报。
  - 确认是新的交付物时，用新的 `--subject` 与新的台账路径正常新建。
  - 新建台账必须给 `--subject`；早期台账（无 `subject`）在正式复核中首次使用时必须补一次
    `--subject`，之后沿用同一个值（只读查看台账不需要）。
  - 早期审计记录（无 `event`/`call_id`）无法跨台账追溯：与之对应的早期台账必须原样保留，
    不要删除后重建；无法确认绑定时按下面的规则安全失败。
- **并发复核被拒绝**：正式复核会对台账加独占锁，覆盖「读轮次 → 请求 → 入账」整段。
  同一台账同时只允许一次正式复核；第二个进程在出站前失败（退出码 2，未发请求、未计轮次）。
  不要为了让两个任务并行而新建台账或复制台账——那会绕过轮次上限。
- **`--audit-log` 的四种状态**（决定能否新建或继续）：
  - **参数缺失**（没传 `--audit-log`）：正式调用在出站前失败（退出码 2）；新建台账被拒绝。
  - **路径无效**（父目录不存在、指向目录、不可写）：审计写入失败，正式调用在出站前失败；
    新建台账会被拒绝；已有台账即使绑定核对通过，也会因写不了开始记录而无法出站。
  - **文件不存在但父目录可写**：这是合法的空日志，首次调用会创建；核对按「无历史」处理。
  - **文件存在但没有该交付物的记录**：该日志本身按「无历史」处理，不是错误；
    但新建台账时只接受标准日志（见上一条）。
  标准路径是 `~/.zcode/skills/copilot/.audit/copilot.jsonl`（Reasonix 侧历史台账绑定的旧路径
  `~/.reasonix/skills/copilot/.audit/copilot.jsonl` 为历史绑定、继续在 Reasonix 侧使用）；
  新建台账只接受标准路径，不要为单次复核另开日志。
  **跨宿主迁移因此只能手工做**：把 Reasonix 台账搬到 ZCode 时脚本会拒绝新建，
  必须由用户确认原日志后手工补台账的 `audit_log` 字段；不要为此放宽新建规则。
- **历史证据包可能无法按当前解析规则复现**：2026-10-25 之前的采集器用固定三反引号包裹正文，
  正文自带围栏时会提前闭合，包本身存在歧义；现行解析器按围栏类型与长度严格配对，因此这类旧包
  可能少算条目。合并时会把解析结果与台账记录的条目指纹逐一核对，**缺失即失败关闭**
  （不会静默丢证据，也不会让「同内容不得重复提交」的闸门失效）。遇到这种报错：
  保留台账与旧证据包原状，按 PARTIAL（缺证）/ BLOCKED 如实汇报。确需继续时，用当前
  `bridge_evidence.py` 重新采集独立证据包；旧包无法并入原台账，而同一交付物的轮次
  不能重置——**不得自行新建台账、改 subject 或删除台账来绕开限制**。是否把整改后的
  产物作为新的交付物重新立项（新的 subject 与台账），由用户确认；未确认就停止自动复核。
- **证据包格式版本**：当前采集器在包内声明 `- 证据包格式：2`，正式复核（含台账与非台账路径）
  **只接受当前版本**，更旧的包在解析前直接拒绝并提示重新采集。原因：旧包用固定三反引号包裹，
  正文里的 `### E<n> —` 标题与图片注释可能泄漏到围栏外，从结构上无法可靠区分
  「合法的第二个条目」与「泄漏出来的伪造条目」——只能在格式层面显式拒绝。
  这不是兼容性疏忽，而是有意的安全边界：旧包本身不可信，重采成本远低于按伪造证据裁决的风险。
- **审计日志绑定与旧台账**：台账创建时记录的日志就是它的绑定，之后每次正式复核必须使用同一份。
  换日志会失去跨台账的轮次核对，等同于重置轮次，因此直接拒绝，不会静默改绑。
  早期台账（无 `audit_log` 字段）首次使用时，只有在日志中能确认属于本台账的历史
  （匹配 `call_id` 或台账路径）才采纳并写入绑定；没有证据时一律安全失败（包括轮次为空的台账），
  需要恢复原审计日志，或由用户确认后在台账中手工补 `audit_log` 字段（规范化绝对路径）。
  脚本不会因为「台账是空的」就接受一份新日志。
- **记账顺序**：GPT 返回后先写 `call_result`，再入账（`record_round`，按 `call_id` 幂等），
  最后写 `--out`。因此 `--out` 写失败**不会漏记轮次**：命令明确报输出失败，轮次已计入，
  回复已打到 stdout。台账落盘失败时本轮未入账，但该调用已在审计中，后续复核会按已用轮次计入，
  不能靠重试重置；同一 `call_id` 恢复不会重复计账。
  审计里的 `call_result.ok` 只表示 **GPT 调用成功**；是否入账看同一 `call_id` 是否出现在台账
  轮次里，出现 `call_post_failure`（如 `ledger_write_failed` / `reply_write_failed`）说明后置步骤失败。
- **裁决未形成（`UNPARSED`）不是有效裁决**：台账/审计记录 `UNPARSED` 时，说明回复没有可机读的
  三态行或存在冲突。按「无裁决」报告，不得当 PASS；主代理可以在报告中给出自己的判断，但必须
  标注它不是本轮正式裁决，也不得因此自动重发付费调用。
- 轮次上限：普通 2 次（首轮 + 1 次针对性复核）；高风险 3 次（首轮 + 最多 2 次）。
  风险按影响判断：安装升级、权限或安全边界、不可逆操作、生产数据、用户要求独立复核 → `high`；
  普通交付 → `normal`。风险在创建台账时用 `--risk high` 指定；台账一旦创建，风险级别不可更改
  （脚本拒绝覆盖，不会静默改写）。新建台账只用于新的交付物；同一交付物的持续复核必须复用原台账。
  该 `--risk` 是 **GPT 台账**的风险，与任务状态 `start --risk` 是两个独立字段，不会自动同步；
  G4 只看任务状态的 `risk`，台账标 `high` 不能弥补任务状态被标成 `normal`。
  不为多跑几轮把普通任务标成 `high`，也不靠新建台账改级别。
- 首轮之外的复核必须对应新的实质证据、未解决的关键缺陷或足以改变结论的矛盾，
  并用 `--extra-round-reason` 说明；同一组证据原样重提会被拒绝（`security` 依据除外）。
- 超限出口：`--extra-round-basis security`（新发现的重大安全问题，每次说明风险）与
  `--extra-round-basis remediation`（已按裁决完成整改，每份台账仅一次，必须带整改产生的
  新增/变化证据）。
- 达到轮次上限仍有关键问题未解决时，停止自动循环，按 PARTIAL/BLOCKED 汇报，
  写明缺什么、由谁决定下一步。

处理复核意见：阻塞项才进入下一轮；非阻塞观察记录为延后项，不驱动复核。PARTIAL 不自动等于
「再跑一轮完整复核」；先判断缺的是否关键、执行方能否补、是否只能由用户动作闭合。
同一问题再次出现时，先查合并包是否已含该证据；已含就引用编号，不再重复补交或调用。
任务文本只用包内 E 编号，不引用来源文件的内部标签。
分级、批量修复与独立复核的预算见下一节。

## 评审档位与缺陷处置

**档位不是新模式**：路由仍只有 CONSULT / PLAN / REVIEW，脚本参数不变；档位只决定同一次
REVIEW 的**评审深度与证据范围**。不因任务名称含 REVIEW 就套用最重流程。

### 1. 三档评审（按影响面选，不按名称选）

| 档位 | 适用 | 本轮必须覆盖 | 独立复核 |
|---|---|---|---|
| **快速** | 文档、文案、低风险规则表述 | 改动正文 + 引用一致性 | 通常不需要 |
| **标准** | 常规脚本、测试、功能修改 | 改动路径 + 相关测试 + 回归面 | 默认 1 次终审 |
| **安全** | 凭据、审计/台账、证据解析、外部请求、权限边界、失败关闭逻辑 | 全部受影响的安全不变量 + 攻击测试矩阵 + 失败路径 | 必须 1 次终审；仅在触发条件成立时追加 1 次定向复验 |

拿不准时按更高一档；但**档位高不等于可以扩大改动范围**。安全档不得因「想少跑一轮」省略攻击测试。
档位与台账的 `--risk` 是两个维度：档位决定**本轮评审深度与证据范围**，`--risk` 决定**台账轮次上限**，
按实际影响面分别判断。该判 `high` 的仍按影响判 `high`；本节只反对**无依据抬高**，
不反对因安全影响而抬高。

### 2. 首轮评审后：先集中分级，再动手

1. **合并去重**：同一根因的多条发现合并为一条，保留最强证据与最小复现。
2. **逐条定级**：P0（安全或正确性被直接击穿）/ P1（高影响，本轮必须修）/ P2（可延后，记录即可）。
   每级都要写：实际证据、最小复现、验收标准。
3. **三分**：真实缺陷 / 误报（写明推翻依据）/ 可延后观察（写明触发条件与责任方）。
4. **修复前先写两张清单**：受影响的安全不变量；对应攻击测试矩阵（输入形态 × 期望结果）。
   矩阵优先覆盖「边界形态组合」（缩进 / 围栏字符 / 元数据位置 / 并发 / 空值），
   而不是逐个发现被动补测——那样每轮都会冒出新形态。
5. 不把每条低优先级建议都变成代码改动；P2 默认只登记，不驱动修复。

### 3. 批量修复与范围控制

- 同一批相关文件集中修改；开发中跑定向测试，一批改完再跑必要全量测试。
  不要「修一条 → 全量测试 → 再修一条」地来回切换。
- 与本任务无直接依赖的相邻问题单独登记，不顺手扩大范围。
- 不省略关键攻击测试；也不为凑「测试数量」重复跑等价断言。

### 4. 独立复核：范围、预算、停止条件

- **默认 1 次只读终审**（`AGENTS.md`《委派与独立复核》意义上的独立复核；档位不构成调用理由）：
  只检查实际 diff、关键不变量与测试证据，不重跑未变更区域的完整调查，也不重新论证已关闭的项。
- 只有出现**新的 P0/P1、关键不变量失效、或证据不足以判断安全性**时，才追加 1 次**定向复验**：
  范围只覆盖该缺陷及其影响路径；先最小复现，再修复。
- **独立复核最多 2 轮；到顶即停**。达到上限仍有阻塞项时，如实报 PARTIAL / BLOCKED 并写明缺口与责任方；
  **不得把未解决的 P0/P1 标成 PASS**，也不得无限循环。
- P2 在有依据时可延后（写明触发条件与责任方），不驱动新一轮复核。
- 这里的「轮」指独立复核轮次；与台账的付费 REVIEW 轮次上限（普通 2 / 高风险 3）是两件事，
  互不替代，也不互相豁免。

## 判断质量与收口（对照 ChatGPT 自定义指令 v5.0）

按 v5.0 的原则判断，不新增模式或机制。完整原文见
`~/.zcode/references/chatgpt-custom-instructions-v5.0.md`（五部分 30 条，逐字保存）；
**逐条承载表在 `references/role.md`**（30 行，每条给出落点，无遗漏、无弱化）。本 Skill 只选择性融入，不是复制。

**调用条件以本 Skill「何时调用」的显式启用门槛为准**（用户点名才进入本通道）；
ChatGPT 自定义指令 v5.0（2026-11-01 逐字落盘）只是判断原则的对照基线，不构成调用授权。
下面是**依据 v5.0 原则制定的本地实施规则**，不是 v5.0 原文逐字规定：能自己做就直接做；
原生能力先行；复杂 / 多文件 / 多代理 / 存在可选模式均不构成理由，也不构成调用授权；
复核原则上单轮完成，按风险独立决定；无新增证据不重复 REVIEW。

通用原则（目标与价值优先、事实优先、简单可靠优先、最小充分执行、安全与授权优先）
的主干已在 `~/.zcode/AGENTS.md` §1 / §2 / §4 生效，本 Skill 不再整条复述。下面六条是
**本通道需要的表述**：要么是全局规范未细化到这一层的判断口径，要么是 GPT 侧必须
显式看到、否则会误判的边界。

1. **事实三分**：区分事实 / 推测 / 建议并显式标注；证据不足写「无法判断」，
   不把推测写成事实。
2. **证据优先级**：按真实运行 > 针对性测试 > 静态检查 > 配置表面状态的顺序取用；
   只要求达成目标所必需的动作，不做无关加固或扩展。
3. **三态带来源**：`裁决：PASS / PARTIAL / BLOCKED` 只由 REVIEW 给出；
   `验收：PASS / PARTIAL / BLOCKED` 是主代理对照原始目标给出的验收结论（v5.0 #25/#27/#30）。
   两者分行、分别标注，不得互相冒充；日常问答与讨论不强制产出三态。
4. **固定任务接口**：向执行方下达任务时使用「目标 / 完成条件 / 边界 / 必要背景」四段（v5.0 #14）；
   执行方按同一结构回报，按五项回流验收（完成条件、关键证据、范围合规、未解决问题、剩余风险，#25）。
5. **收口与独立性**：关键证据充分、目标完成、没有影响交付的阻塞项时立即收口，
   剩余不确定性不改变当前决策时不再追问；未验证项如实列出，不得写成已通过。
   执行方自查、运行验证与 GPT 判断是三件事；**独立复核归主代理**，是否复核取决于
   **风险、影响面与证据缺口**，不机械安排多轮；复核的范围、预算与停止条件见
   「评审档位与缺陷处置」§4。
6. **两个判断分开**：v5.0 原文要求「优先使用工具的原生能力」「按风险决定调查、测试和复核范围」。
   据此制定的**本地实施规则**是：是否需要独立复核按 `AGENTS.md`
   《委派与独立复核》判断，是否使用外部认知通道按「何时调用」的显式点名门槛判断。
   多文件、多代理、涉及代码或测试、存在 PLAN / REVIEW 模式都不构成调用理由——
   这一条**不是 v5.0 原文**。

## 边界与失败

- copilot 不做调查、不执行、不做独立复核；需要更多证据时它给出「要什么 / 从哪取 / 覆盖哪个条件」，
  由主代理完成。
- 工作项或裁决不是授权：发布、推送、付费、不可逆、生产数据、凭据等动作仍须用户授权；
  派发不得绕过主代理的权限边界。
- 不得把 GPT 提出的建议直接写成已执行或已完成；汇报区分
  「GPT 已提出（建议）／宿主主代理已执行（动作）／已用证据验证（结果）」。
- GPT 对执行方回报的判断不得表述为「我已核验现场」或「我已独立复核」。
- 已完成时明确写「无需继续操作」并给出最终结论或裁决，不为显得完整再造一轮任务。
- 脚本非 0 退出时不得报告整条流程成功；按实际阶段说明是**未取得回复**、**请求结果未知**，
  还是**已取得回复但后处理失败**（落盘 / 台账 / 交接状态）。不得把已收到的回复说成未取得。
  退出码 9 归入「**未取得有效回复**」（正文已丢弃、审计只留摘要），不是「请求结果未知」。
  不得伪造、不得静默换模型后声称同一裁决、不得自动降级到别的链路。
  退出码：0 成功 / 2 用法、凭据或审计前置条件不满足 / 3 上游错误 / 4 超时 /
  5 非良性 output 条目（安全事件）/ 6 响应非完整成功 /
  7 请求可能已发出但结果审计未能写入（审计不完整，不得视为成功、不得重试）/
  8 配额或冷却限制（`usage_limit_reached` / `model_cooldown`，stderr 带 `reset_seconds`；
  不自动重试、不换模型、不降档，额度恢复需人工确认再发起；普通 HTTP 429 没有上述显式
  配额码时按上游错误返回 3，不误判为配额耗尽）/
  9 模型身份验证失败（回显模型缺失、不可解析或与请求不一致；该回复已丢弃：不输出正文、
  不写 `--out`、不入台账、不写交接状态，不得转述其内容，不得凭该轮文件补录）。
- 退出码 9 与「未取得有效回复」同义：本轮没有可用的 GPT 判断（正文已丢弃，审计只留摘要）。
  只有退出码 0 且审计
  `model_identity_ok=true` 的回复才能进入验收结论、GPT REVIEW 台账与交接状态的工作项；
  身份缺失、身份不一致或来源凭证无效时一律停止消费，不得用换模型、降档、
  手工改写审计或直接写台账/状态来绕过。
- **本地信任边界（明确声明，不宣称绝对防伪）**：任务凭证里的 `contract_sha256` 与
  `reply_sha256` 只证明「同一份目标/合同」「同一份回复正文」，是关联与完整性凭证，
  **不是签名、不做身份认证**；本轮没有 HMAC、外部密钥或签名设施。能读写本机文件的用户
  仍在这条信任边界内，可以改写审计、状态或待补录的回复文件——本机制防的是
  「错误归属、跨任务/跨轮次串线、旧回复冒充本轮」这类来源混淆，不是防本机恶意篡改。
  因此正式运行只认标准审计日志路径，隔离的临时审计路径只在显式声明测试时使用；
  在报告里可以说「凭证与任务/轮次/合同绑定且不可跨任务复用」，不得说「不可伪造」。
- 需要补证时只复验受影响的条件，不重跑已确认项；达到轮次上限仍未解决时如实报
  PARTIAL/BLOCKED，并写明责任方。
- 本 Skill 不新增或改动宿主的 provider、模型路由、工具与权限配置
  （含 ZCode 的 `~/.zcode/v2/provider_config.json`；Reasonix 侧 `config.toml` 为历史遗留、同样不动）。

## 版本与历史

- **当前对照**：ChatGPT 自定义指令 **v5.0**（`~/.zcode/references/chatgpt-custom-instructions-v5.0.md`，
  2026-11-01 逐字落盘，五部分 30 条，是全局判断原则的对照基线）。**引用层级要分清**：
  v5.0 原文只给出原则（原生能力优先、按风险决定复核范围、复核者与执行上下文隔离、原则上单轮复核），
  本 Skill 的**具体调用判据是本地的实施规则**，依据这些原则制定，不是原文逐字规定；
  遇到两者表述不一致时，以「何时调用」的本地判据执行，并如实标注它是本地规则。
- **模型基线**：`gpt-6.1-sol` + 请求体显式 `reasoning.effort="high"`。
  2026-11-01 单次真实调用已验证：请求名与响应 `model` 字段完全一致、上游接受该参数并产出独立
  reasoning 条目、零工具、无回退。**未验证**：High 相对低档位的实际能力增益（缺对照实验），
  不得据 `reasoning_tokens > 0` 宣称 High 已生效。
- **历史**：v4.3（2026-10-23 的调用口径，未落盘为独立原文，其更严的调用门槛已并入
  v4.5 与「何时调用」）；v4.5（2026-10-25 的对照版本）；历史版本文件：
  `chatgpt-custom-instructions-v4.5.md`、`chatgpt-custom-instructions-v4.2.md`、
  `chatgpt-custom-instructions-v4.0.md`、`chatgpt-custom-instructions-v3.4.md` 只作历史版本
  保留，不再是当前依据。早期还曾按用户提供的 v3.9 原则做定向融合，v3.9 无本地原文，
  已由后续版本依次对照取代。
- ChatGPT 自定义指令只是行为理念的对照来源，**不是权限来源**，不高于宿主系统指令、
  用户授权或全局工作规范。
- `gpt-cpa` 与 `gpt-director` 已并入本技能并从技能目录删除（2026-10-18），保持退役，不重新引入。
  本技能（原名 `gpt-assist`，2026-10-19 更名为 `copilot`；2026-11-01 从「GPT 判断与指挥通道」
  升级为「GPT-6.1-SOL 完整认知角色通道」）是唯一正式 GPT 助理入口。
  历史审计归档在 `~/.reasonix/archive/removed-skills-20261018/`。

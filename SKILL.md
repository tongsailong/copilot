---
name: copilot
description: "Copilot 任务承接与自动驱动，并在需要高级工程决策时由 Copilot 判断进入 SOL 通道（gpt-6.1-sol、effort=high、零工具）：技术方案取舍、风险分析与工程结果判断。CONSULT/PLAN/REVIEW 是内部模式；简单明确任务不调用；独立复核走原生子智能体；REVIEW 三态裁决须用户显式要求。唯一正式 GPT/SOL 入口。"
when_to_use: >
  两类情形：1. Copilot 任务承接或自动驱动中出现需要高级工程决策的事项（目标/范围/完成条件存在
  实质歧义、多个方案都成立且取舍依赖成本或风险、关键假设无法由现场证据闭合、原计划失效或重要
  阻塞、整改后需要定向复验）；由 Copilot 判断并进入，不要求用户重复点名。简单、明确、低风险的
  常规编码/测试/文件操作/查询不调用。2. 用户显式点名 Copilot / GPT 通道（含 `/copilot ...`），
  或要求正式 REVIEW 三态裁决（须用户显式要求）。SOL 固定走 CPA 零工具通道；独立复核、代码审核、
  第二意见按 `AGENTS.md`《委派与独立复核》使用原生子智能体，不属本通道。同一任务可在关键阶段
  多次调用并复用同一交接状态；没有新增关键证据或实质变化时不重复调用。
---

# copilot：GPT-6.1-SOL 高级工程决策通道

copilot 是本宿主（ZCode）内的 **SOL 高级工程决策通道**：任务承接与自动驱动中遇到需要高级工程决策的
事项时进入本通道（见「何时调用」），不要求用户重复点名。它不是执行体、调查者，也不是独立复核通道；
需求理解、产品与业务判断、目标定义、产品层方案规划与结果验收归用户与用户侧 ChatGPT，不并入 SOL。
常规任务的规划与调度由 ZCode 原生负责；REVIEW 三态裁决仍须用户显式要求，独立复核走原生子智能体。

- **ZCode 主代理（继承当前会话模型）**：探索现场、读写文件、运行测试、采集原始证据；承接 copilot
  的安排，直接执行或经原生 Agent 子代理派发；回填真实结果；**负责执行自检与必要的独立复核**。
- **copilot（GPT-6.1-SOL High）**：授权范围内的高级工程决策权威——技术方案与架构取舍、工程风险
  分析、复杂技术问题判断、依据提交证据给出工程工作项安排与工程结果判断、按真实回报判断进度、
  给出下一步工程要求。产品与业务层判断归用户与用户侧 ChatGPT，SOL 不代替；高级工程决策不得以
  工程执行模型替代。
- **宿主子代理**（继承主代理模型，如 general-purpose）：承接调查、代码检查、资料收集、测试与整改；
  有工具、能取证，但不是判断方。

**调查权不下放给 GPT，执行细节不预先规定。** copilot 的安排必须由主代理承接；
不得把调查任务委托给 GPT。

**角色映射（对齐 ChatGPT 自定义指令 v5.3.3，当前协作基线）**：本 Skill 名下有两个可区分的角色——
**Copilot 协作层**（运行于 ZCode 内、由主代理按本 Skill 的 `copilot_dispatch.py` /
`copilot_auto.py` / `call_gpt.py` 实际执行的协作治理机制：任务承接、任务合同与状态、门禁、证据
桥接、结果回传）与 **SOL 决策角色**（`gpt-6.1-sol` + `reasoning.effort=high`，承担授权范围内的高级
工程判断与工程结果判断）。Copilot 的核心高级决策只走 SOL 通道，不以工程执行模型替代；GPT 不以
任何形式作为执行子代理或复核子代理参与。ZCode 原生 Agent 是工程执行主体，负责现场实施与验证；
用户决定业务目标与关键取舍。Skill 名称沿用 `copilot`，
不表示两个角色合并。执行与回传以执行方实际回报为准，不假定接口、后台任务或通知必然可用。

**六角色边界（v5.3.3）**：六角色（用户 / ChatGPT 用户侧 / Copilot / SOL / ZCode 原生 Agent /
DesignWise）的职责以 `~/.zcode/AGENTS.md` §1 为准，本 Skill 的「角色映射」只区分 Copilot 协作层
与 SOL 决策角色。这里只补本通道必须显式声明的边界：Copilot 与 DesignWise 都由 ZCode 主代理实际
执行，不是独立的工程执行 Agent——Copilot 不做代码实施、不代替 ZCode 执行工作项、不擅自调度原生
子 Agent（`copilot-plan` 是工作安排输入，具体实施与原生派发由主代理在授权范围内落实）；SOL 不是
ZCode 原生 Agent，无工具、不读本地工作区、不调度 Agent，其输出是高级决策输入，不构成授权、执行
结果或独立复核，可以回答产品问题但那只是认知分析，不构成产品/业务决策，重大取舍仍由用户决定。
任务承接与自动驱动不扩大授权（进入条件按「何时调用」）。付费调用受用户显式限额、服务商配额与
限流、审计与授权边界约束；一次 drive 至多发起一次 SOL 判断调用，这是单轮驱动节流，不是任务总轮次
或 HTTP 请求总数上限，也不构成人为预算。

**默认协作闭环（v5.3.3）**：ChatGPT 定义目标 → ZCode 原生 Agent 承接工程任务 → Copilot 治理与
推进（按需调用 SOL 决策）→ ZCode 原生 Agent 执行与验证 → 证据回传 → 验收。简单任务不强制经过
Copilot、SOL 或正式 REVIEW；普通技术问题由 ZCode 原生 Agent 自主解决。

**与 DesignWise 的分工**：设计方法与设计质量归 DesignWise，由 ZCode 原生 Agent 承载实际设计工作，
Copilot 不另建设计工作流；DesignWise 的评审门不构成 Copilot 授权，也不构成进入本通道的理由。

## 专业模块路由（按需读取，不预读全部）

**触发专业任务时，必须先用 ZCode 的文件读取工具（Read）读取对应文件再执行。** 本文件里出现路径
不会让 ZCode 自动加载它；references 不会随 Skill 正文自动进入上下文。**不得一次性读取全部
references**，只读当前任务需要的那一份。核心授权、安全、证据与门禁约束已写在本文件，不依赖任何
模块是否被读取。

| 任务 / 场景 | 必读文件 | 用途 |
|---|---|---|
| 任何模式：取证、调用、交接状态、恢复、失败处理 | `references/workflow.md` | 完整命令矩阵与恢复语义 |
| 自动协作运行时、`drive`、多轮推进 | `references/runtime.md` | 阶段 2–4 状态机、九段上下文、manifest |
| 正式 REVIEW、三档评审、缺陷分级、收口 | `references/review.md` | REVIEW 收敛与评审档位细则 |
| GPT REVIEW 台账、审计日志机制 | `references/review-ledger.md` | 台账绑定、并发与记账细则 |
| 原生 Agent Bridge（`--execution-backend native`） | `references/agent-bridge.md` | 信封字段、错误码、Reviewer Profile |
| 版本沿革与历史对照 | `references/history.md` | 只作历史，不改现行规则 |
| SOL 侧指令 | `references/role.md` | 由 `call_gpt.py --role-file` 注入，主代理不读取 |

按任务类型的读取范围：

- **普通 CONSULT**：只读本文件；需要取证或调用细则时读 `workflow.md`。
- **复杂 PLAN / 多阶段**：本文件 + `workflow.md`；需要自动运行时再加 `runtime.md`。
- **正式 REVIEW**：本文件 + `review.md`；需要台账细则时再读 `review-ledger.md`。
- **原生 Bridge**：本文件 + `agent-bridge.md`。
- **故障处理**：按故障类型读 `workflow.md`（调用/恢复）、`runtime.md`（阶段/驱动）或
  `review-ledger.md`（台账/审计）的对应小节，不整本预读。

读取 references 时只读与当前问题相关的小节；大文件用有界读取。已读且现场未变化的内容不重复读取；
引用时给文件路径与小节名，便于追溯。

## 模式与路由

CONSULT / PLAN / REVIEW 是**内部模式**，不是调用范围：它们只决定留痕字段、证据纪律与输出契约，
不限制 GPT 能处理什么任务（交流问答、产品探索、方案规划、技术评估、执行指导、验收都在范围内）。

| 模式 | 解决什么问题 | 输出 | 执行方 |
|---|---|---|---|
| CONSULT | 交流问答、问题分析、事实判断、风险识别、产品与需求讨论、方案或文档评审 | 结论—依据—建议；必要时定向取证项；默认不出三态裁决 | 主代理，或按需派宿主子代理 |
| PLAN | 产品层方案规划、方案比较与选择、成本/风险权衡、行动安排与分工 | 目标 / 完成条件 / 边界 / 必要背景 / 方案选择与取舍 / 验收标准 / 工作安排 / 决策失效条件 | 主代理承接，可派宿主子代理 |
| REVIEW | 对交付物或改动做 PASS / PARTIAL / BLOCKED 证据裁决与收口判断（历史兼容） | 三态裁决 + 最小整改项 + 是否收口 | 主代理承接整改 |

REVIEW 的正式裁决必须是回复中**独立、唯一**的一行 `裁决：PASS|PARTIAL|BLOCKED`；
`最终裁决：` / `整体结论：` 只在明确且唯一时兼容。裁决值必须是精确三态词，只允许首尾的
标点/强调符号修饰；同一行再附叙述（如 `裁决：PASS（证据充分）`）或全篇出现多行裁决行
（含重复同一裁决）时按「无裁决」处理，不靠否定词黑名单猜极性。代码块、引用块、缩进代码、示例里的裁决不算，
裸 `结论：` 与 `copilot-plan` 的 decision 也不是三态裁决。`call_gpt.py` 默认注入这段输出契约；
解析不到或出现冲突时按「无裁决」记录（不得当 PASS），也不自动重发付费调用。

**三态两个来源（必须标注）**：`验收：PASS|PARTIAL|BLOCKED` 是主代理对照原始目标给出的验收结论；
`裁决：PASS|PARTIAL|BLOCKED` 是本通道在隔离证据上给出的裁决。两者并存时分别标注，不得互相冒充
（`copilot-plan` 的 decision 也不是三态）。

三种模式全部走 CPA（`127` provider）的 `gpt-6.1-sol`，Responses API，零工具（请求体不含 `tools`
字段，`tool_choice="none"`），请求体显式 `reasoning.effort="high"`。`call_gpt.py` 只接受这三种模式；`DECIDE` 已合并（方案选择归 PLAN，
问题判断归 CONSULT，收口归 REVIEW），传入会被拒绝，不存在第四条调用分支。

`--model` 只能覆盖 CPA 通道模型：接受裸 ID 或 `127/` 前缀；其他 provider 前缀、空模型名与被取代/
退役 ID（`gpt-6.1-sol-high`、`gpt-6.1-sol-max`，已退役）在发送前拒绝。
`gpt-6-astra-high` 等旧入口仍可显式指定（回滚路径，保留不删），但不再是默认值、不会被自动选用；
`--reasoning-effort` 只按显式值发送，不做自动降档或换档重试。

宿主（ZCode）自身的规划、子代理、看图、常规复核与自动命名走宿主原生能力与主模型（以实际模型配置
为准）；SOL 只在 Copilot 进入决策通道或用户显式要求时使用，不是日常执行或复核模型。
TypeSafe / `jev-latest` 是 Reasonix 侧的底层决策能力（`system_one` / `decision_model` 机制本轮未迁移
到 ZCode，`jev-triage` 仅存在于 Reasonix 侧、属历史遗留）；copilot 不承载它，也不得把它的结构化
问题改发 CPA 冒充同一裁决。

## 何时调用

**两条进入路径，不能混用**：

1. **SOL 决策通道（Copilot 判断是否进入）**：任务承接或自动驱动中出现需要高级工程决策的事项时，
   由 Copilot 判断并直接进入，进入后由 SOL 给出决策；不要求用户重复点名。典型判据：目标/范围/
   完成条件存在实质歧义且现场证据无法单独闭合；多个技术方案都成立且取舍依赖成本、风险、可维护性
   或长期约束；关键假设无法由现有证据验证且错误假设会实质改变方案；执行中出现会改变方案的新事实、
   原计划失效或重要阻塞；整改后需要定向复验且判断不能由现场证据直接推出。简单、明确、低风险、
   可直接执行的常规编码 / 测试 / 文件操作 / 查询不调用，主代理能按既有规则自证的小型取舍也不调用；
   任务复杂、跨多文件、需要多代理协作本身不是调用理由。
2. **用户显式要求**：用户 `/copilot ...`、明确要求 GPT / copilot 判断、规划或 REVIEW 三态裁决时，
   按用户意图进入。**REVIEW 三态裁决仍须用户显式要求**，不得由流程、风险分档或主代理自行升级触发。

**高级工程决策不得由执行模型替代**：属于第 1 类的事项必须走 SOL 通道，不能由主代理或执行子代理
直接拍板后声称「已按 Copilot 判断」。SOL 不读文件、不跑命令，判断依据是证据包；证据不足时先补证，
补不到按缺证处理。

**独立复核不是 SOL 决策**：独立复核、代码审核、第二意见按 `AGENTS.md`《委派与独立复核》使用
ZCode 原生子智能体的独立上下文；以下情况都不构成进入本通道的理由：用户仅要求独立复核、独立审核、
代码审核、第二意见或复杂任务协作；任务复杂、改动跨多文件；需要多代理协作或派发子代理；存在可选的
PLAN / REVIEW 模式（模式可用 ≠ 本次该用）；按《委派与独立复核》的风险分档判定需要独立复核；
原生复核子智能体暂不可用、调用失败或输出不充分；主代理自认为判断无法自证、认为 GPT 会有增量价值；
其他 Skill 规定的必经评审门（如 designwise 的 Gate 1 / Gate 2——这类门不构成 Copilot 授权，按其
降级路径执行，代码/变更独立复核仍走原生子智能体）。

**路由**：按本轮要解决的问题选模式，调用前确认具体问题与证据范围。实质方案取舍或权衡、主代理有多个
都成立的候选且缺少外部裁决依据 → CONSULT / PLAN；影响风险判断的证据需要隔离判断（安全与隐私、
权限边界、关键业务链路、全局执行机制等高影响改动，或证据相互冲突）→ CONSULT，需要正式三态 REVIEW
时向用户说明并取得显式要求；视觉方向 / 阶段门槛等必须基于真实像素的独立评审（`--image`）→ CONSULT，
正式 REVIEW 仍需用户显式要求；用户明确要求 GPT / copilot 复核或收口裁决 → REVIEW。

**调用边界**：SOL 决策不豁免证据纪律与授权边界；证据不足时先补证，补不到按缺证处理。默认不设
轮次、请求数或累计 Token 上限；受用户显式限额、服务商配额与限流、审计与授权边界约束。没有新增
关键证据、没有实质性变化、也没有明确未解决的问题时，不重复调用；不为「再确认一次」、为「看起来
严谨」或为上一个未执行的建议重新开轮。

**长任务的多阶段协作**（SOL 决策通道可在关键阶段多次进入；每次都有门槛，不要求用户逐轮点名）：

| 阶段 | 触发 | 模式 | 必须携带 |
|---|---|---|---|
| 开始 | 目标有歧义、方案有重要权衡、风险不明确，且主代理无法自证取舍 | PLAN 或 CONSULT | 原始需求、约束、现状与候选方案 |
| 执行中纠偏 | 新事实改变方案、关键决策无法确定、重要阻塞、原计划失效 | CONSULT 或 PLAN | 最新证据 + 已执行工作项与真实结果 |
| 阶段交付 | 需要判断继续 / 调整 / 返工，且该判断无法由现场证据直接推出 | PLAN 或 CONSULT | 该节点产物 + 执行证据 |
| 最终验收 | 高影响交付，且现场证据不足以支持收口 | 主代理验收 + 原生独立复核；需要正式三态 REVIEW 且用户已明确要求时，另走 REVIEW | 交付物 + 验收条件映射 + 已知缺口 |

同一任务复用同一交接状态与同一 GPT REVIEW 台账，不因「上一轮已收口」而新建。补证或整改后只复验
受影响的条件与缺口，不重跑完整复核。

## 调用契约（所有模式）

本节是每次调用都必须遵守的硬约束；完整命令矩阵、扩展选项与恢复语义见 `references/workflow.md`。

**取证**：先明确本轮要判断的条件，再只取覆盖这些条件的最小证据。证据由
`scripts/bridge_evidence.py` 采集（`--file` / `--image` / `--cmd` / `--dir` / `--json` / `--attest` /
`--stdin`），脚本负责脱敏、sha256 指纹、采集方式、预算截断与省略量标注；`--cmd` 同时收 stdout 与
stderr。脱敏是**有损**的，被改写的行会在包内披露并写入 manifest 的 `redacted_lines`；包内代码与
磁盘不一致时必须按披露内容判断。复核需要确认「被审的是哪份代码」时用 `--attest REPO[::标签]`，
由 git 机器生成绑定证据；非 git 工作区直接失败，不降级为自述。图片证据用 `--image`，发送侧读取
同一文件并校验 sha256 后内联真实像素；不得用路径、描述或 base64 文本代替像素。证据包按
`--file` → `--image` → `--cmd` → `--dir` → `--json` → `--attest` → `--stdin` 生成 E 编号：
先取证、确认实际 ID，再写任务文本。`call_gpt.py` 会自动注入《证据自检表》（条目数 / ID / 未纳入
证据及原因 / 截断条目及省略字符数）。

**调用**：三个模式统一经 `call_gpt.py` 出站，标准调用形态：

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

**审计日志是正式调用的必需参数。** 缺 `--audit-log` 或路径写不进去时，脚本在出站前失败（退出码 2，
请求未发出）。每次调用先落盘一条 `call_requested`（含 `call_id`、`fsync`），再发请求，最后写
`call_result`；两者靠 `call_id` 关联。结果记录写不进去时返回退出码 7，**按「审计不完整」报告：
请求可能已发出，不得当作成功，也不得重试**。回复已取得但后置步骤（回复落盘 / 台账 / 交接状态）失败
时追加 `call_post_failure` 并以退出码 2 结束；同一 `call_id` 至多一条 `call_result`。`--dry-run`
不出站、不写审计，但会读取或创建/更新 GPT REVIEW 台账并写入绑定；它没有完全副作用隔离，正式复核前
不要当成「只读预检」。

**输出路径不得与输入或审计指向同一文件。** `--audit-log`、`--out`、`--dump-request`、
`--review-ledger`、`--dispatch-state`、`--segmentation-plan` 之间，以及它们与 `--role-file` /
`--prompt-file` / `--evidence` 之间若解析到同一 inode（含符号链接、硬链接），出站前直接拒绝
（退出码 2）：这些输出是截断写或原子替换写，会破坏审计、台账、交接状态或覆盖本轮输入证据。

**REVIEW 一律要求证据包**；没有证据时不要调用，直接按 `PARTIAL（缺证）` 或 `BLOCKED` 汇报。文本
REVIEW 还必须带 `--review-ledger`（GPT REVIEW 审计记录，**不构成 G4 依据**），否则发送前拒绝
（退出码 2）；唯一例外是显式 `--one-shot-visual-review` 且证据含图片的一次性视觉评审，它不携带
跨轮状态，不得用于续轮。CONSULT / PLAN 不需要台账。

**模型身份是消费前提（硬约束）。** 只有退出码 0 **且**审计 `call_result` 里 `model_identity_ok=true`
的回复才能作为有效 GPT 指导：请求模型与上游回显 `model` 必须一致（只容忍两端空白）；回显缺失、不可
解析或与请求不一致时返回**退出码 9**，并已丢弃该回复——stdout 无正文、不写 `--out`、不入 GPT REVIEW
台账、不写交接状态（也不覆盖已有成功凭证），stderr 只留脱敏诊断。此时按**未取得有效回复**汇报：
不得转述正文，不得据该 `call_id` 验收或补录；脚本不重试、不换模型、不降档，是否重发由用户决定。
手动 `--model` 指定**非默认**模型仍是可用入口，但审计 / 台账 / 交接状态标 `model_source=override`，
其回复按手动模式留痕，**不得冒充默认通道（`gpt-6.1-sol`）的判断**。**P0 之前的历史轮次没有身份
凭证**：脚本不回灌其正文，也不让其 `decision` 控制工作项；需要依据时重新调用。

**自动协作通道（带 `--dispatch-state`）的身份是固定档位，不接受覆盖。** 进入自动闭环时，脚本在出站
前硬性检查：请求模型必须是 `gpt-6.1-sol`、`reasoning.effort` 必须是 `high`、来源必须是默认规则
（`model_source=default`）；任何显式 `--model`（含显式写默认名）一律拒绝。审计路径必须是本 Skill 的
标准日志 `~/.zcode/skills/copilot/.audit/copilot.jsonl`。自动化测试需要隔离路径时显式设置
`COPILOT_AUDIT_TEST_MODE=1`；**正式运行不得设置该变量**。不带 `--dispatch-state` 的一次性调用仍可用
手动 `--model`，但按手动模式留痕，不得冒充标准 SOL High 自动指导。

**不自动重发、不外层重试。** CPA 响应可能超过前台 2 分钟：优先以后台任务运行，等宿主完成通知再读
输出，不用固定 `sleep` 反复轮询。超时与连接中断属于**请求结果未知**（可能已接收并计费）：脚本只写
审计并显式失败，不自动重发。**已收到 HTTP 错误响应（含 429/5xx）也不自动重发**；`--max-retries`
只对可证明请求未建立的传输层失败生效。**截断恢复**：默认不发送 `max_output_tokens`；截断
（`status=incomplete` + `reason=max_output_tokens`）为非裁决——不写 `--out`/状态、不推进轮次、保留
审计与证据指纹，按观测 `output_tokens` 翻倍对齐 4096，用同一任务/证据/轮次重发并记 `retry_of`；
连续两次在更高预算下仍未突破历史最高输出，视为静默截断，不再升档，转为分段计划并失败关闭。

**用量只记录、不阻断**：成功有 `usage` 计真实 Token；成功无 `usage` 或可能已送达的失败计
`unknown_usage_calls`，不当作 0。默认不设请求数、Token 上限；只有调用方在 `start` 时显式给出
`max_requests` / `max_total_tokens` 才生效（请求数上限出站前精确计数，Token 阈值是「达到即停」）。
服务商真实限额、配额耗尽与限流按上游错误码处理，不在这里放行。

**失败与恢复的完整语义**（pending、`recover`、`recover-reply`、`--abandon`、分段计划）见
`references/workflow.md`；核心原则：请求在前、提交在后，mid-flight 中断按 `pending_call` 精确恢复，
`recover` **不会重新调用 GPT**；结果未知或回复已找不回时必须由人工作出放弃决定。

## 承接执行与反馈（PLAN / 多阶段）

**长任务的多阶段协作**：同一任务可在关键阶段再次调用，复用同一 `--dispatch-state` 交接状态，不为
每个阶段各建一份新台账；没有固定轮次上限，按新增证据与未解决问题决定是否继续。

**按需建立交接状态**：PLAN、多阶段任务或预期会产出工作项的 REVIEW，在首次调用前用
`copilot_dispatch.py start` 建立任务级状态并传 `--dispatch-state`；一次性 REVIEW 不强制创建 dispatch。
状态持久化 `task_id` / `round_id` / `contract_sha256`（目标、完成条件与边界的规范化摘要）；
`ingest` 逐项核对任务、轮次、合同指纹、模式、模型身份与回复摘要，任一不符即拒绝；同一 `call_id`
只能消费一次。调用成功后的提交由 `call_gpt.py` 一次原子完成（回复凭证 + 轮次 + `last_call` +
清理 pending），不用「审计日志里最后一条成功调用」推断归属。**旧格式状态只保留人工兼容入口**，
不能进入新的自动协作任务。

**请求在前、提交在后。** 发送前先把调用意图（`task_id`、`round_id`、`contract_sha256`、`call_id`、
`mode`、请求模型、effort、来源）落盘为 `pending_call`；取得回复后先构造完整凭证（含
`reply_sha256` 与 `model_identity_ok`），验证通过再与轮次、`last_call`、pending 清理合并为**一次**
原子写盘。写盘失败时 pending 保留，状态不前进；`recover` 只依据 pending 与本轮审计凭证续接。
`pending_call` 未结算时自动通道拒绝发起新调用。状态写入带比较交换（`round_seq`、`call_id`、
`save_seq`），并发进程不会双计费或用旧快照覆盖他人写入。

**`copilot-plan` 块**：copilot 需要推进时输出一个机器可解析的安排块，`call_gpt.py` 自动写回交接状态：

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
  `reasonix`（仅兼容迁移前历史安排）；不得派给 GPT。宿主当前没有可承接 `deepseek` 工作项的子代理
  类型时，如实按「无法派发」报告（BLOCKED，或由主代理直接执行并如实标注），不得冒充已派发。
- 同一 id 在续轮出现表示更新：只覆盖实际给出的字段，未重述字段保持原值；`done` / `dropped`
  的 id 不可复用。依赖缺失、未知、自依赖或成环会被拒绝。
- 多个 `copilot-plan` 块、无法解析的围栏、`stop/blocked` 带 actions 都会失败，不会静默当成没有安排。
- 无安排块是合法形态（CONSULT 常只给分析）；回复正文会作为历史前缀回灌下一轮，不作为当前事实。

**闭环**：提交目标、证据与上轮工作项状态 → copilot 判断下一步 → 主代理直接执行或派发 →
回填真实结果与证据编号 → copilot 决定继续、补证、改方案或收口。多轮推进可用阶段 4 的
`scripts/copilot_auto.py drive` 自动串联机械步骤（见 `references/runtime.md`）；主代理仍是唯一
执行方，用户不需要逐轮转发。简单任务直接做，不机械派发子代理；不为展示调度能力制造工作项。

**交付形态**：默认交付是内部工作安排——主代理直接承接、执行与回填，不是复制粘贴中介；只有
用户明确要求独立指令文本，或交接接口不可用需要人工传递时，才额外给 Markdown 代码块。

完整命令（`start` / `next` / `record` / `contract` / `ingest` / `recover` / `recover-reply`）与
失败关闭条件见 `references/workflow.md`。

## 原生 Agent Bridge（opt-in）

任务声明 `--execution-backend native` 时，执行与复核是**两份独立信封、两份独立报告**，由
`scripts/native_agent.py` 从宿主实际运行记录生成：执行 `copilot-native-result/v1`，复核
`copilot-native-review/v1`（绑定 `execution_result_sha256`，Reviewer 独立只读、`profile_id=reviewer`、
`changed_files=[]`、`g4_eligible=false`）。主代理不得改写 SOL 的 title / assignee / goal /
constraints / expected_evidence / depends_on。复核未回填时即使 SOL 已 PASS 也不得收口；复核有
blocking finding 或 verdict≠pass 时进入 REJUDGE。`ingest` 是纯本地回填，不调用 GPT、不递增轮次；
SOL 的最终判断只能由随后显式运行的 `drive` 发起。

**G4 不放宽**：Bridge 的普通原生复核记录 `g4_eligible=false`，不进入 G4；高风险任务仍
`native_review_missing` + BLOCKED。GPT REVIEW 台账、Agent 自报身份、手写同构 JSON 同样不是 G4 依据。

字段、校验失败码、命令矩阵与 Reviewer Profile 见 `references/agent-bridge.md`。

## 自动协作运行时与驱动（阶段 2–4）

`copilot_dispatch.py` 维护任务合同、结构化完成条件、工作项、证据、报告、配额与四门；
`copilot_auto.py drive` 把「读阶段 → 拼 Prompt → 调用 → 回填 → 再求值」收敛为一个入口，不改任何
门禁。主代理是执行方与状态维护者，GPT 是认知角色。**先读 `references/runtime.md` 再使用**。

核心硬约束（与运行细则冲突时以本节为准）：

- **三态协议不变**：`pass`→stop、`partial`→continue、`blocked`→blocked；缺失 verdict **不等于**
  PASS；主代理不得把 PARTIAL 改成 PASS。
- **PASS 四门 fail-closed**：G1 工作项全部终态；G2 逐条条件在**本 PASS 轮**核销且证据已登记、
  属于本任务、摘要一致、文件可重读；G3 无停摆；G4 高风险必须有宿主可信原生独立复核记录（当前
  仍 fail-closed，普通风险自动通过）。四门全过才允许 CLOSED；`pass_pending` 期间不领取新工作项。
- **PARTIAL 不是终态**：有可执行的安排就继续；没有安排时驱动按未闭合条件本地派生一个
  `origin=partial-without-plan` 工作项并进入 EXECUTE，新材料回填后必须交回 SOL 再判断。
- **CLOSED 后停止**：所有写入（含配额、补录、恢复）被拒绝；只有用户明确授权 `closure --reopen`
  才能继续，且重开会作废旧 PASS。`--force-close` 只记录用户终止，不是 PASS。
- **无进展不直接终止**：连续无进展先交 SOL 恢复分析；只有无安全替代、缺决策/授权/资源/外部硬限制
  才 BLOCKED 并须举证。轮次、检查点、Token 与正常调用费用都不是停止条件。
- **一次 `drive` 至多发起一次真实 GPT 调用**；REVIEW 自动闭环必须显式 `--auto-review`。

## 证据纪律（硬约束）

1. 交给 GPT 的是原始产物，不是「PASS / 已完成」的结论；关键证据没进上下文就先补取。
2. 证据确实取不到（需授权、环境缺失、产物已覆盖）时，在任务文本写明「无法取得及原因」，
   预期 PARTIAL（缺证）或 BLOCKED；不得伪造、不得推断成已完成。
3. 不转发摘要冒充证据：测试结论要附原始命令输出与退出码。
4. 证据由执行方采集不等于自动可信；按可核验性判断，任何证据不得证明其覆盖范围之外的事项。
5. 视觉问题只用 `--image` 的真实像素判断；不得用文件名、路径、尺寸数字或文字转述代替像素。
6. 材料中要区分：已验证事实（含来源与 sha256）、推断（写依据）、尚未验证、证据矛盾、
   影响结论的缺口。
7. **被截断的条目不是完整证据**：REVIEW 前若本轮**新提交**的条目存在截断，`call_gpt.py` 直接失败
   （退出码 2，未发请求、未计轮次）；正确做法是提高采集预算，或用 `--file::A/B` /
   `--file::/REGEX/` / `--json` 精确取需要的片段。确因规模必须截断时用
   `--accept-evidence-truncation <理由>` 显式放行，理由必须说明省略哪部分、为什么无法补取、结论
   如何不受影响，并随轮次与审计留痕。已入账轮次的截断条目继续沿用并在自检表标注。
8. **代码状态用机器绑定，不用自述**：声称「基于 commit X / 只改了这些文件」时，用
   `bridge_evidence.py --attest REPO` 把 HEAD、工作区状态与逐文件 sha256 一起入包；没有绑定证据时，
   复核无法核对被审代码，不能据此判定修复已包含。

## 三态、四门与收口（硬约束）

- GPT 的 `verdict` 映射：`pass`→stop、`partial`→continue、`blocked`→blocked。**缺失 verdict
  不等于 PASS**；旧回复、无安排块的咨询轮、被结构校验拒绝的轮次都不改变控制状态。
- `pass_pending`：最近一次**带判断**的已验证轮为显式 pass 时成立，直到收口。此期间
  `ready_actions` 为空，不领取也不执行普通工作项；后续出现新的 non-pass 判断时旧 PASS 被覆盖。
- `closure-check` 只读计算，`loop-step` 在 PASS 挂起时把四门结果写入 `closure.gate`；
  `closure --close` 要求最近 PASS 轮且四门全过。阶段求值优先级：
  `CLOSED > RECOVER > BLOCKED > REVIEW_PENDING > QUOTA_WAIT > CLOSURE_CHECK > REJUDGE > EXECUTE >
  GUIDANCE_NEEDED`；按 `next_commands` 行动，不自行发明阶段。
- **G4 高风险复核（当前 fail-closed）**：`--review-ledger` 是 GPT REVIEW 审计记录，不是 G4 依据；
  即使台账字段全部有效、GPT 返回 PASS、或 PLAN → REVIEW 辅助调用成功，也不能通过 G4。手写同构
  JSON、任务状态文件、`source=native` 等调用者可填写标签同样不接受。不得手工补台账、伪造归属或
  声称 G4 已满足；原生可信复核记录接入前，高风险任务不能自动收口，停在 BLOCKED，不进入 REJUDGE。
- **本地信任边界**：`contract_sha256` 与 `reply_sha256` 只证明「同一份目标/合同」「同一份回复
  正文」，是关联与完整性凭证，**不是签名、不做身份认证**；报告里可以说「凭证与任务/轮次/合同绑定
  且不可跨任务复用」，不得说「不可伪造」。
- 完整阶段细则、退出码表与驱动行为见 `references/runtime.md`。

## 判断质量与收口

判断原则按 v5.0 的 30 条承载（逐条落点表在 `references/role.md`，无遗漏、无弱化），完整原文见
`~/.zcode/references/chatgpt-custom-instructions-v5.0.md`；本 Skill 只选择性融入，不是复制。
**协作语义基线是 v5.3.3**（`~/.zcode/references/chatgpt-v5.3.3-alignment.md` 是对齐摘要，不是原文
全文、不是权限来源）。六角色分工、「不因人为默认轮次、Token 预算或内部费用阈值提前停止」「PARTIAL
不是默认终点」与「五种机制不混同」的现行口径见 `~/.zcode/AGENTS.md` §1、§2、§5 与上文「角色映射」；
产品设计层要求由 `AGENTS.md` §1 产品设计与 `references/role.md`《产品设计》承载，产品与业务取舍
归用户及 ChatGPT 侧，**不改变授权、独立复核与 G4 边界**。

**调用条件以本 Skill「何时调用」为准**：SOL 决策由 Copilot 判断，不要求用户重复点名，REVIEW 三态
裁决仍须用户显式要求；ChatGPT 自定义指令 v5.0 只是判断原则的对照基线，不构成调用授权。下面是
**依据 v5.0 原则制定的本地实施规则**，不是 v5.0 原文逐字规定。**优先使用工具的原生能力，不因加载本 Skill 而调用**——通道入口由「何时调用」判定，与「是否该用原生工具」是两个独立判断。

通用原则（目标与价值优先、事实优先、简单可靠优先、最小充分执行、安全与授权优先）的主干已在
`~/.zcode/AGENTS.md` §1 / §2 / §4 生效，本 Skill 不再整条复述。下面六条是**本通道需要的表述**：

1. **事实三分**：区分事实 / 推测 / 建议并显式标注；证据不足写「无法判断」，不把推测写成事实。
2. **证据优先级**：按真实运行 > 针对性测试 > 静态检查 > 配置表面状态的顺序取用；只要求达成目标
   所必需的动作，不做无关加固或扩展。
3. **三态带来源**：`裁决：PASS / PARTIAL / BLOCKED` 只由**正式 REVIEW（隔离证据上的三态裁决）**
   给出；PLAN / CONSULT 自动协作返回的运行时 verdict 是流程状态判断，不是正式 REVIEW，也不等于
   原生独立复核结论。`验收：PASS / PARTIAL / BLOCKED` 是主代理对照原始目标给出的验收结论。
   三者分行、分别标注，不得互相冒充；日常问答与讨论不强制产出三态。
4. **固定任务接口**：向执行方下达任务时使用「目标 / 完成条件 / 边界 / 必要背景」四段；执行方按同一
   结构回报，按五项回流验收（完成条件、关键证据、范围合规、未解决问题、剩余风险）。
5. **收口与独立性**：关键证据充分、目标完成、没有影响交付的阻塞项时立即收口，剩余不确定性不改变
   当前决策时不再追问；未验证项如实列出，不得写成已通过。执行方自查、运行验证与 GPT 判断是三件事；
   **独立复核归主代理**，是否复核取决于风险、影响面与证据缺口，不机械安排多轮。
6. **两个判断分开**：是否需要独立复核按 `AGENTS.md`《委派与独立复核》判断；是否需要 SOL 高级工程
   决策按「何时调用」判断，REVIEW 三态裁决仍须用户显式要求。多文件、多代理、涉及代码或测试、
   存在 PLAN / REVIEW 模式本身都不构成调用理由——这一条**不是 v5.0 原文**。

## 边界与失败（硬约束）

- copilot 不做调查、不执行、不做独立复核；需要更多证据时只给「要什么 / 从哪取 / 覆盖哪个条件」，
  由主代理完成。工作项或裁决不是授权：发布、推送、付费、不可逆、生产数据、凭据等动作仍须用户授权，
  派发不得绕过主代理的权限边界。
- 不得把 GPT 提出的建议直接写成已执行或已完成；汇报区分「GPT 已提出（建议）／宿主主代理已执行
  （动作）／已用证据验证（结果）」。GPT 对执行方回报的判断不得表述为「我已核验现场」或
  「我已独立复核」。已完成时明确写「无需继续操作」并给出最终结论或裁决，不为显得完整再造一轮。
- 脚本非 0 退出时不得报告整条流程成功；按实际阶段说明是**未取得回复**、**请求结果未知**，还是
  **已取得回复但后处理失败**（落盘 / 台账 / 交接状态），不得把已收到的回复说成未取得。不得伪造、
  不得静默换模型后声称同一裁决、不得自动降级到别的链路。退出码：0 成功 / 2 用法、凭据或审计前置
  条件不满足 / 3 上游错误 / 4 超时 / 5 非良性 output 条目（安全事件）/ 6 响应非完整成功 /
  7 请求可能已发出但结果审计写入失败（审计不完整，不得视为成功、不得重试）/ 8 配额或冷却限制
  （`usage_limit_reached` / `model_cooldown`，stderr 带 `reset_seconds`；不自动重试、不换模型、
  不降档，额度恢复需人工确认；普通 HTTP 429 没有显式配额码时按上游错误返回 3）/
  9 模型身份验证失败（回显模型缺失、不可解析或与请求不一致，该回复已丢弃）。
- **退出码 9 与「未取得有效回复」同义**：本轮没有可用的 GPT 判断（正文已丢弃，审计只留摘要），
  不是「请求结果未知」。只有退出码 0 且审计 `model_identity_ok=true` 的回复才能进入验收结论、
  GPT REVIEW 台账与交接状态的工作项；身份缺失、不一致或来源凭证无效时一律停止消费，不得用换模型、
  降档、手改审计或直接写台账/状态来绕过；不得转述其内容，不得凭该轮文件补录。
- **本地信任边界（明确声明，不宣称绝对防伪）**：`contract_sha256` 与 `reply_sha256` 只证明「同一份
  目标/合同」「同一份回复正文」，是关联与完整性凭证，**不是签名、不做身份认证**；本轮没有 HMAC、
  外部密钥或签名设施。能读写本机文件的用户仍在这条信任边界内，可以改写审计、状态或待补录的回复文件
  ——本机制防的是「错误归属、跨任务/跨轮次串线、旧回复冒充本轮」这类来源混淆，不是防本机恶意篡改。
  因此正式运行只认标准审计日志路径，隔离的临时审计路径只在显式声明测试时使用。
- 需要补证时只复验受影响的条件，不重跑已确认项；没有新增证据可提交、问题仍未解决时如实报
  PARTIAL/BLOCKED 并写明责任方。本 Skill 不新增或改动宿主的 provider、模型路由、工具与权限配置
  （含 ZCode 的 `~/.zcode/v2/provider_config.json`；Reasonix 侧 `config.toml` 为历史遗留、同样不动）。

## 版本与历史

**当前对照：ChatGPT 自定义指令 v5.0**（判断原则来源，原文
`~/.zcode/references/chatgpt-custom-instructions-v5.0.md`）；协作语义基线为 v5.3.3。模型基线为
`gpt-6.1-sol` + 显式 `reasoning.effort="high"`（High 相对低档位的实际能力增益未验证）。

- **历史原文（只作沿革，不构成现行依据）**：`chatgpt-custom-instructions-v4.5.md`、
  `chatgpt-custom-instructions-v4.2.md`、`chatgpt-custom-instructions-v4.0.md`、
  `chatgpt-custom-instructions-v3.4.md`；v4.3 无落盘原文，只作历史提及。
- **版本沿革、历史对照与退役记录**统一见 `references/history.md`，那里只作历史、不改现行规则；
  现行判据以本文件为准。

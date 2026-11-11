# Copilot 操作细则：取证、调用与承接（CONSULT / PLAN / 多阶段）

> 本文件是 `SKILL.md` 的**操作细则与命令矩阵**，按任务需要读取。硬约束、门禁、授权边界与
> 裁决口径以 `SKILL.md` 为准；本文件不新增、不放宽任何硬约束，冲突时以 `SKILL.md` 为准。
> 读取触发条件见 `SKILL.md`「专业模块路由」。

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
`--cmd`（真实执行，保留原始输出与退出码）、`--dir`（清单）、`--json`（子结构）、
`--attest`（git 代码绑定）、`--stdin`。
脚本负责脱敏、sha256 指纹、采集方式、预算截断与省略量标注；凭据类文件默认拒绝整读；
`--cmd` 同时收 stdout 与 stderr（只收 stdout 会把写 stderr 的命令记成「0B」假空证据），正文首行是
实际执行的命令。脱敏是**有损**的：标准序列化风格（`indent` 2/4、紧凑分隔符、`json.dumps` 默认分隔符）
的整份 JSON 文档按结构处理（键与非敏感字段保留、结果仍可解析；凭据键下的标量与容器内叶子一并遮蔽，
仅凭据形态的键名会被替换）；非规范排版的 JSON/JSON5 退词法路径，凭据键下的容器由原地扫描逐叶子
遮蔽（字符串、数字、URL 与数据形态的裸标量换成记录原值长度的 `<redacted:N>` 标记；括号、键名、布尔/空值、注释与行数保留，
整体可解析性不保证；容器含代码关键字或字面量在括号内时按代码处理，不当作字面量内容）；
其余文本里被改写的行会显式披露 `- 脱敏改写：第 N 行（采集正文行号；条目被裁剪时按裁剪前计）`，
行号同时写入 `--manifest` 的 `redacted_lines`：包内代码与磁盘不一致时必须按披露内容判断。
`--dir-glob` 过滤目录清单。`call_gpt.py` 会自动注入《证据自检表》（条目数 / ID / 未纳入证据及原因 /
截断条目及省略字符数），让缺证与截断对 GPT 可见；不要指望 PASS 摘要掩盖缺口。
采集端在有截断时会同时在 stderr 与 `--manifest` 的 `items_truncated` / `omitted_chars` 中报出，
不要等复核被拒才发现正文已被省略。

**代码绑定证据**：复核需要确认「被审的是哪份代码」时用 `--attest REPO[::标签]`，由 git 机器生成
HEAD、分支、`status --porcelain=v1 -z`、逐文件 sha256 与 `diff --stat HEAD`，不靠执行方文字自述。
非 git 工作区、git 不可用或读取失败时直接失败（退出码 2），不降级为普通文件清单——
没有绑定证据的复核只能信自述，而自述不是证据。

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

**编号顺序**：证据包按 `--file` → `--image` → `--cmd` → `--dir` → `--json` → `--attest` → `--stdin`
生成 E 编号。先取证、确认实际 ID，再写任务文本；不要预设图片是 E1/E2/E3。

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

**角色指令按模式组装**：默认不传 `--role-file`，脚本把共享核心 `references/role.md` 与当前模式的
专业补充（`role-consult.md` / `role-plan.md` / `role-review.md`）以空行连接后作为 `instructions`
注入；同一组文件内容产生同一份指令。显式 `--role-file <路径>` 时只注入该文件，不追加模式补充
（用于回滚、离线对照或自定义角色）。审计记录实际注入的文件路径、各自 SHA-256 与总字符数，
不记录正文。

**审计日志是正式调用的必需参数。** 缺 `--audit-log` 或路径写不进去时，脚本在出站前失败（退出码 2，
请求未发出）。每次调用先落盘一条 `call_requested`（含 `call_id`、`fsync`），再发请求，最后写
`call_result`；两者靠 `call_id` 关联。结果记录写不进去时返回退出码 7，**按「审计不完整」报告：
请求可能已发出，不得当作成功，也不得重试**。回复已取得但后置步骤（回复落盘 / 台账 / 交接状态）失败
时追加 `call_post_failure` 并以退出码 2 结束；同一 `call_id` 至多一条 `call_result`（结果记录本身
写失败时为 0 条）。`--dry-run` 不出站、不写审计，但会读取或**创建/更新 GPT REVIEW 台账**并写入
`subject` / `audit_log` 绑定；它没有完全副作用隔离，正式复核前不要当成「只读预检」。

**输出路径不得与输入或审计指向同一文件。** `--audit-log`、`--out`、`--dump-request`、
`--review-ledger`、`--dispatch-state`、`--segmentation-plan` 之间，以及它们与 `--role-file` /
`--prompt-file` / `--evidence` 之间若解析到同一 inode（含符号链接、硬链接），出站前直接拒绝
（退出码 2）：这些输出是截断写或原子替换写，会破坏审计、台账、交接状态或覆盖本轮输入证据。

**复核用量只读预检**：正式准备证据包前，可先用 `review_ledger.py --budget` 查看
`subject / risk / used / pending / ordinary_used / security_used / extra_reasons / next_action`
等观察字段（`max_rounds / default_rounds / extra_allowance / security_allowance` 仅作历史参考，
不构成停止条件）：

```bash
python3 ~/.zcode/skills/copilot/scripts/review_ledger.py \
  --file /tmp/copilot/review-<交付物标识>.json --budget --json
```

`pending`（审计成功但尚未入账的 REVIEW 调用）计入 `used`，与 `merge_and_guard` 同口径；台账不存在
时只按 `--risk` / `--subject` 报告参考值，**不创建台账**；预检 `allowed` 恒为 true，只报告用量与
下一步，不改变任何门禁或关闭语义。

**轮次口径（2026-11-08 起）**：复核**没有固定轮次上限**。普通 2 轮 / 高风险 3 轮、普通追加 +2 / +3、
Security +1 / +2 这些历史数字只用于审计与观察，不再阻断复核。是否继续只由两件事决定：本轮必须有
**新增或变化**的有效证据（第 2 轮起还要用 `--extra-round-reason` 说明为什么还需要一轮、实际改了什么、
对应哪条未闭合项）；确有实质未闭合问题。没有新增证据时 `merge_and_guard` 一律拒绝重复复核：换文件名 /
路径、改理由措辞、把同一内容重新打包都不算新证据，新建 subject、换台账或换日志也绕不过去重闸门。
`--extra-round-basis new-evidence|remediation|security` 只描述追加类型：`remediation` 不再限制一次；
`security` 表示本轮针对安全风险，任何轮次都可声明、不再要求「普通额度先耗尽」，但仍必须提供此前未
用于任何一轮 REVIEW 的新增有效证据，并写明具体安全风险与对应未闭合项。

**模型身份是消费前提（硬约束）。** 只有退出码 0 **且**审计 `call_result` 里 `model_identity_ok=true`
的回复才能作为有效 GPT 指导：请求模型与上游回显 `model` 必须一致（只容忍两端空白）；回显缺失、不可
解析或与请求不一致时返回**退出码 9**，并已丢弃该回复——stdout 无正文、不写 `--out`、不入 GPT REVIEW
台账、不写交接状态（也不覆盖已有成功凭证），stderr 只留脱敏诊断。此时按**未取得有效回复**汇报：
不得转述正文，不得据该 `call_id` 验收或补录；脚本不重试、不换模型、不降档，是否重发由用户决定。
手动 `--model` 指定**非默认**模型仍是可用入口，但审计 / 台账 / 交接状态标 `model_source=override`，
其回复按手动模式留痕，**不得冒充默认通道（`gpt-6.1-sol`）的判断**；显式传入默认名按 `default` 留痕
（身份仍按请求名校验）。`--out` 只在成功且身份通过时写入；失败时既不写也不删既有文件——那是上一轮
遗留，不得当作本轮结果。**P0 之前的历史轮次没有身份凭证**：脚本不回灌其正文，也不让其 `decision`
控制工作项（轮次计数与已有工作项保留）；需要依据时重新调用。审计缺少 `model_source` /
`reply_sha256` / `model_requested` 等字段时按「凭证不完整」拒绝补录（有意的收紧：无法证明来源的响应
不进入交接状态）；凭证里的 `mode` 必须与挂起模式严格一致，带旧审计（无 `mode`）的 pending 轮次只能
用 `--abandon` 显式放弃——fail-closed，不是静默放行。

**自动协作通道（带 `--dispatch-state`）的身份是固定档位，不接受覆盖。** 进入自动闭环（`PLAN` 多阶段、
续轮 REVIEW 等会写交接状态的调用）时，脚本在出站前硬性检查：请求模型必须是 `gpt-6.1-sol`、
`reasoning.effort` 必须是 `high`、来源必须是默认规则（`model_source=default`）；任何显式 `--model`
（含显式写默认名）一律拒绝，不做「手动覆盖后仍按标准通道留痕」的降级解释。审计路径必须是本 Skill
的标准日志 `~/.zcode/skills/copilot/.audit/copilot.jsonl`，其他审计文件一律拒绝——否则旧日志里的成功
记录会被当成当前任务的凭证。自动化测试需要隔离路径时显式设置 `COPILOT_AUDIT_TEST_MODE=1`；
**正式运行不得设置该变量**。身份不一致同样按退出码 9 处理：不输出正文、不写 `--out`、台账与任务状态，
也不留下可被恢复消费的 pending。不带 `--dispatch-state` 的一次性调用仍可用手动 `--model`
（Luna / Astra 等入口保留），但按手动模式留痕，不得冒充标准 SOL High 自动指导。

REVIEW 一律要求证据包；没有证据时不要调用，直接按 `PARTIAL（缺证）` 或 `BLOCKED` 汇报。文本 REVIEW
还必须带 `--review-ledger`（GPT REVIEW 审计记录，**不构成 G4 依据**），否则发送前拒绝（退出码 2）；
唯一例外是显式 `--one-shot-visual-review` 且证据含图片的一次性视觉评审，它不携带跨轮状态，不得用于
续轮。CONSULT / PLAN 不需要台账。

续轮合并包默认对已验证 PASS 之前、未被引用的背景证据做**历史证据投影**（见
`references/review-ledger.md`）：投影条目只有编号与 SHA-256 等身份记录，不是证据正文；`--full-evidence`
按编号强制原文、`--no-history-projection` 退回全量。两者都只影响渲染，不改台账与历史包。

CPA 响应可能超过前台 2 分钟：优先以后台任务运行（如 run_in_background），等宿主完成通知再读输出，
不要用固定 `sleep` 反复轮询；需要主动检查时有界等待（先等 60–120 秒，再查进程、审计与输出各一次）。
超时与连接中断属于**请求结果未知**（可能已被上游接收并计费）：脚本不自动重发，只写审计并显式失败。
`--timeout` 必须是正有限、且本机 socket 可表示的秒数（NaN/负数/`1e300` 之类在出站前以退出码 2 拒绝，
不留 pending、不写审计）。
**已收到 HTTP 错误响应（含 429/5xx）也不自动重发**：状态码不足以证明上游未处理或未计费，脚本保留
原始状态码与输出留痕后直接失败。`--max-retries` 只对**可证明请求未建立**的传输层失败（拒绝连接、
域名解析失败、网络不可达）生效；native 自动任务默认 `0`，legacy / 手动通道默认 `2`。任何情况下都不要
在外面套一层重试——那会绕过这层付费保护。

**输出预算与截断恢复**：默认不发送 `max_output_tokens`（仅显式 `--max-output-tokens` 才发送，ceiling
为可选上界）。截断（`status=incomplete` + `reason=max_output_tokens`）为非裁决：不写 `--out`/状态、
不推进轮次、保留审计与证据指纹，按观测 `output_tokens` 翻倍对齐 4096，用同一任务/证据/轮次重发并记
`retry_of` / `retry_reason`。默认不限升档次数，仅显式 `--max-output-retries` 构成运营限制；但**连续
两次在更高预算下仍未突破此前最高输出**（按历史最高值比较、留 10% 抖动余量，避免把固定上限附近的
1000→999→1000 抖动当成进展）视为静默截断或固定上限，不再升档。预算无法提高、
Provider 拒绝该字段与上述静默截断同样处理：不重发相同请求，自动生成分段计划（`--segmentation-plan`，分段文件 +
SHA-256 + `same_failed_request_must_not_be_resent`），以 `transition=segmentation_required` 失败关闭，
`drive` 返回同名状态。截断仍须过身份校验（不符 exit 9）；Provider 限制 / 认证 / 网络错误分别留痕。

native / legacy 任务都只保留**真实**约束（`budget_status`，从审计重建，进程重启不归零）：
- **默认不设请求数上限、不设 Token 上限**：用量（`requests / input_tokens / output_tokens /
  total_tokens / unknown_usage_calls`）照实记录进审计与 `budget.usage`，用于观察与成本核算，
  **不构成停止条件**，也不会因为累计费用阻止必要的判断或执行。
- **未知用量单列，不当作 0**：成功有 `usage` 计真实 Token；成功无 `usage` 或已送达/可能送达的失败
  无 `usage` 计 `unknown_usage_calls`；确认未送达只计 `not_delivered_calls`；失败但部分 `usage` 记
  已知 Token 并保留失败状态。响应按 `call_id+升档序号+response_id` 去重；已知总量只累计真实 `usage`。
- 只有调用方在 `start` 时**显式**给出 `max_requests` / `max_total_tokens`（或历史状态里显式保存了
  数值）时才生效，报告标为「显式运营上限」，不是代码默认阈值：请求数上限在出站前精确计数（达到即
  拒绝，未创建 pending、未发 HTTP、未递增轮次，退出码 12 / drive `budget_exhausted`）；Token 阈值按
  `usage` 核算，只有响应返回后才知道，因此是「达到即停」的阈值而非严格上界，另可选保守预估
  （`projection`）。
- 服务商真实限额、配额耗尽与限流仍按上游错误码处理（quota / 429），不在这里放行，也不伪装成已通过。

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
状态写入带比较交换：`round_seq`、挂起 `call_id` 与单调递增的 `save_seq` 都必须与磁盘一致，且
`save_state` 默认以调用方快照的 `save_seq` 为基线——两个进程共用同一状态时，只有先落盘者拿到
本轮，另一个在**发送请求前**或写盘时失败，不会双计费，也不会用旧快照覆盖他人已写入的证据、
工作项或收口结果。内容完全相同的重复保存不推进 `save_seq`；`start` 以「状态文件必须不存在」
为初始化前提，并发创建时后到者失败而非互相覆盖。带挂起 `call_id` 的状态另有按轮次的配额 CAS。
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

**已消费但 plan 被拒的回复**（审计 `call_post_failure.error=dispatch_plan_rejected`，
轮次已记 rejected，回复可能已扣费）不要重新调用模型；用 `recover-reply` 复用原
`call_id` 和原回复重新解析：

```bash
# 先 dry-run 只看预览，不写状态、不写恢复审计
python3 ~/.zcode/skills/copilot/scripts/copilot_dispatch.py recover-reply \
  --state /tmp/copilot/dispatch.json --call-id <原 call_id> \
  --reply-file /tmp/copilot/copilot-reply.md --dry-run
# 预览通过后去掉 --dry-run 正式应用
```

恢复会重新校验任务凭证、原调用审计、模型身份与回复内容，然后以原子写盘应用安排；
成功后复用原 `call_id`，**不会新增 SOL 请求或 REVIEW 轮次**，原回复文件、审计记录与
回复摘要保持不变，重复执行按 `already_applied` 幂等处理。写盘前的并发基线取**磁盘原文**
摘要（不是 `load_state` 补过默认键的内存态），因此缺默认节的旧状态照常可恢复；恢复审计
同时记录 `before_state_sha256_basis=disk-raw` 与 `loaded_state_sha256`，两者不同正是
「加载时补默认键」的现场。真正的外部写入仍被 CAS 拒绝，恢复不会覆盖他人改动。
以下情况仍 fail-closed：
字段含义冲突（如 `evidence` 与 `evidence_ids` 同时出现且不等）、空 `id`、语义错误、
未知字段冲突、任务/轮次/合同/身份校验失败。`copilot_auto.py drive` 在 `call_gpt.py`
返回退出码 2 且最近失败确为本次 `dispatch_plan_rejected` 时，会自动先 dry-run 再应用；
恢复失败仍按原退出码失败关闭，不猜测、不重发。

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

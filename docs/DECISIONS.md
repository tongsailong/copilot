# DECISIONS

已确认 = 当前执行依据；建议 = 尚未采纳，需要用户授权；待决 = 需要业务或授权决定。
本文件不把建议或规划写成既成事实。

## 已确认

1. **唯一源码是 `/media/vdb/projects/copilot`。**
   运行时 `~/.zcode/skills/copilot` 与 `~/.zcode/agents/reviewer.md` 是构建产物，只允许
   由 `scripts-sync/sync_runtime.py` 单向下发；禁止双向维护，禁止把运行时当第二开发工作区。
   依据：迁移前运行时目录不是 Git 仓库，也没有独立源码；DesignWise 已验证同一模式。

2. **分发范围固定为** `SKILL.md`、`scripts/**/*.py`、`references/**/*.md`、
   `agents/reviewer.md`。`tests/`、`docs/`、`manifests/`、`scripts-sync/`、`README.md`、
   `AGENTS.md` 属于源码仓库的开发/项目管理内容，不进入运行时。

3. **每次同步同时覆盖 Skill 和 Reviewer。** 不提供“只同步 Skill”的开关，避免
   Skill 已更新而 Reviewer 仍是旧版本。两侧任一侧漂移，`--check` 都返回 1。

4. **默认只检查、不写入。** `--apply` 才写入；`--prune` 必须与 `--apply` 同时显式使用。
   首次同步不清理运行时文件。

5. **写入必须原子且可验证。** 同目录临时文件 + `os.replace`，复制源文件权限；
   写后重新计算 SHA-256，缺失或不同不为 0 视为失败。

6. **拒绝符号链接与越界路径。** 源码和目标中的符号链接、目标相对路径 `..`/绝对路径、
   源码与目标互为祖先/后代，全部失败关闭（退出码 2），不使用软链接替代副本。

7. **敏感内容不进入源码仓库或分发物。** `.env*`、`*.pem`/`*.key` 等凭据、
   `.audit/` 真实审计日志、临时状态、`__pycache__` 均由同步工具跳过；`.audit/`
   即使出现在运行时也不会被 `--prune` 删除。

8. **运行时 `tests/` 不参与分发，也不在首次同步中删除。** 源码 `tests/` 是权威测试集；
   运行时残留测试仅作历史副本，后续是否清理需单独授权。

9. **测试的宿主路径固定。** 宿主级 ChatGPT 参考文档在 `~/.zcode/references/`，
   Skill 安装目录在 `~/.zcode/skills/`；测试不再从自身文件位置推断宿主目录，
   因此在源码仓库和已安装副本中都能运行。

10. **既有协议决定不变。** `execution_backend` 默认 `legacy`，native 为显式 opt-in；
    高风险 G4 保持 fail-closed；普通原生 Reviewer PASS 不等于 G4；不引入 Pi。

11. **Reviewer 模型跟随主代理，不单独钉死 Provider。** Reviewer Profile 使用
    `model: inherit`，Provider/模型/推理级别继承自主代理当前会话；不得再写死官方
    DeepSeek 直连模型。依据：2026-11-03 新进程真实调用验证——Reviewer 经 CPA
    `new-provider/deepseek-flash-high` 走与主代理相同的上游；独立 Agent/会话、
    只读工具与复核契约不变。Executor 同样按主代理路由执行；模型继承不改变
    Agent 身份、上下文与职责边界。

12. **本地 Git 基线。** 仓库在 `main` 分支建立首个可回溯提交，仅纳入源码、测试、
    文档、同步工具与项目规则；不配置远端，不 push、不发布。凭据、`.audit/`、
    数据库、运行时状态、备份与临时文件由 `.gitignore` 排除；后续提交只暂存明确路径。
    依据：2026-11-03 仓库正式化任务授权（本地 init + 初始提交，禁止 push）。

13. **[历史预算方案，已被第 16 条取代] REVIEW 预算改为「默认预算 + 弹性追加额度」。**
     ⚠ **状态注（2026-11-09）**：本条记录的默认 2/3 轮、+2/+3 追加额度与「额度用尽即停止」
     已由第 16 条取代，只作历史决策与当时依据保留，不再是现行停止条件；`remediation` 作为
     追加类型标签、subject / 台账 / 审计日志绑定等非预算约束继续适用。下文原文不改写。

    默认仍为普通 2 轮 / 高风险 3 轮；
    默认用尽后，若存在新增实质证据/整改进展/新风险证据，可在普通 +2、高风险 +3 的
    追加额度内继续**定向复核**，必须携带新增/变化证据并写明追加理由；额度用尽即停止，
    按 PARTIAL/BLOCKED 汇报。`security` 是普通预算全部用尽后的独立安全例外，额度与准入
    见第 14 条。`remediation` 不再是每台账一次的名额，只是追加类型标签；所有角色仍绑定原
    subject / 台账 / 审计日志，不能靠新建 subject、换台账或换日志扩容。
    依据：2026-11-03 万年历广告对齐任务 `rili-ad-alignment-20261103`——2/2 轮后仍有
     未闭合项，旧 `remediation` 名额已用，最终 Diff 失去定向复核出口；本机制保留受控出口，
     同时用「必须新增证据 + 追加额度硬顶」防止无限续轮。
     不变：G1–G4 fail-closed、独立 Reviewer、高风险证据/身份/授权要求、SOL 模型与 CPA 路由。

14. **[历史预算方案，已被第 16 条取代] Security REVIEW 改为有上限的独立安全例外。**
     ⚠ **状态注（2026-11-09）**：本条记录的普通 1 / 高风险 2 独立额度、合计 5 / 8 轮上限与
     「额度用尽即停止」已由第 16 条取代；`security` 现为可随时使用的本轮依据标签，不再需要
     等普通预算用尽。内容哈希去重、新证据准入、subject / 台账 / 审计绑定等非预算约束继续
     适用。下文原文不改写。

     普通预算（默认 + 普通追加）全部用尽后，
    才允许 `--extra-round-basis security`；独立额度为普通 1 轮 / 高风险 2 轮，不与普通额度
    互相占用，也不能提前切换到该通道。自动 REVIEW 合计上限为普通 5 轮（2 + 2 + 1）、
    高风险 8 轮（3 + 3 + 2）；这些是上限而非必须用完的配额。准入同时要求：确有新的实质
    安全风险；至少一条此前未用于任何一轮 REVIEW（普通或 Security）的新增有效证据；用
    `--extra-round-reason` 写明具体风险、对应未闭合项与新增证据。Security 与普通追加共用
    内容哈希去重，换文件名 / 路径、改措辞、重新打包同一内容都不算新证据；理由文字不能
    替代新证据。额度用尽即停止自动调用，按 PARTIAL/BLOCKED 汇报并请求用户针对该任务、
    具体风险与追加范围**明确授权**；自由文本、自声明 CLI 参数、新建 subject 或换台账均
    不构成授权。历史 Security 轮次计入现有额度（不清零），历史裁决不追溯撤销。`--budget`
    分别报告 `ordinary_*` / `security_*` 与 `next_action`；审计记录
    `review_risk / review_security_* / review_new_evidence`（含 SHA-256）。依据：2026-11-04
    安全审计证明旧规则下 `security` 可无限重复调用、且绕过证据去重（只用非空理由即可）。
    不变：普通默认预算与普通追加额度、G1–G4 fail-closed、Reviewer 独立性与身份验证、
    SOL 模型（`gpt-6.1-sol` + `reasoning.effort=high`）与 CPA 路由。

15. **截断证据不得直接用于 REVIEW；代码状态必须机器绑定。** 采集器按预算保留头尾时，
    条目元数据会写「- 截断：省略 N 字符」；`call_gpt.py` 解析并在自检表列出，且本轮
    **新提交**的截断条目在 REVIEW 前失败关闭（未发请求、未计轮次），需重新采集或用
    `--accept-evidence-truncation <理由>` 显式放行并随轮次/审计留痕。已入账轮次的截断
    条目继续沿用并标注，不永久锁死后续复核（避免一次历史截断把任务卡死）。同时新增
    `bridge_evidence.py --attest REPO[::标签]`：由 git 机器生成 HEAD、工作区状态、
    逐文件 sha256 与 `diff --stat HEAD`，非 git 工作区失败关闭。依据：2026-11-04 荣耀买量
    任务 `honor-multi-ad-activation-20261104`——R2 E21 正文被截断 6214 字符后送审，SOL 据此
    给出无法闭合的缺证裁决；4/4 轮都在追问「被审代码是哪一份」，执行方只能用文字自述。
    ⚠ **部分替代注（2026-11-09）**：真正的现行规则是「截断证据失败关闭 + 代码状态机器绑定」，
    这部分继续有效；G1–G4、Reviewer 独立性、证据 SHA 与审计绑定不变，该闸门只拦「明知不完整
    仍送审」，不降低任何验收标准。本条原文中的「不变：默认/追加/Security 预算与停止条件」已被
    第 16 条取代，不再作为现行停止条件。

16. **取消固定 REVIEW 轮次上限；用量与费用只记录、不阻断（取代第 13、14 条）。**
    2026-11-08 起复核不再有「普通 2 / 高风险 3、追加 +2/+3、Security +1/+2」这类停止门：
    历史数字保留在 `--budget` 输出里只作观察，`allowed` 恒为 true、`limits_enforced=false`。
    是否继续只由「本轮有新增/变化证据」+「确有未闭合实质问题」决定；第 2 轮起仍必须用
    `--extra-round-reason` 说明。内容哈希去重、subject/台账/审计日志绑定、换日志拒绝、
    并发锁全部保留；`security` 只是本轮依据标签，可随时使用，但仍需具体安全风险说明与
    此前未用于 REVIEW 的新增证据。请求数与累计 Token 照实记录（`budget.usage` / 审计），
    不再作为 native/legacy 的默认阈值，也不再阻止 drive 或 call_gpt；只有用户在 `start`
    显式给出 `max_requests` / `max_total_tokens` 才按显式运营上限执行。8 轮检查点与
    `--rounds-limit` 同样降为观察信号。依据：2026-11-08 用户要求解除过保守预算——
    固定轮次与 Token 阈值会在正常决策过程中打断任务。不变：G1–G4 fail-closed、Reviewer
    独立性与只读边界、证据完整性闸门、模型身份校验、SOL 模型与 CPA 零工具路由。

17. **输出预算默认不设人为上限；截断在同一任务/证据/轮次内自适应恢复。**
    2026-11-08 起 `call_gpt.py` / `copilot_auto.py` 默认不发送 `max_output_tokens`
    （不再有 8192 之类的固定值，也不把任何新数字变成天花板），由服务商/模型使用真实上限；
    只有调用方显式 `--max-output-tokens` 才发送，`--max-output-tokens-ceiling` 是可选上界。
    响应 `status=incomplete` + `reason=max_output_tokens` 时按「非裁决」处理：不写 `--out`、
    不入台账、不写状态、不推进轮次，保留失败请求审计（`usage` / `usage_available` /
    `max_output_tokens_sent` / `response_id`）与证据包指纹；按观测 `output_tokens` 约翻倍并对齐
    4096 整数倍升档，用同一任务、同一证据、同一 REVIEW 轮次重发，重发前另记一条带
    `retry_of` / `retry_reason` 的 `call_requested`。**升档默认不限次**：只有显式
    `--max-output-retries` 才构成运营限制，显式 `--max-output-tokens-ceiling` 是另一上界。
    预算无法继续提高或上游显式拒绝 `max_output_tokens` 时，不重发相同失败请求，而是生成
    `copilot-segmentation-plan/v1`（`--segmentation-plan`）：按证据条目切分、逐段写文件并记录
    SHA-256 与条目清单，声明 `same_failed_request_must_not_be_resent`；
    `call_gpt.py` 以 `transition=segmentation_required` 失败关闭，`drive` 返回
    `status=segmentation_required`，由主代理分段处理证据后重新进入 REVIEW，仍不产生裁决、
    不消耗正式轮次。截断响应同样要通过模型身份校验。服务商限制、认证失败、网络错误分别处理。
    不变：零工具、身份硬门、审计先行、pending 与恢复、证据完整性。

18. **PARTIAL 自动推进：本地派生工作项 + 真实执行桥接，循环到 PASS 或真实 BLOCKED。**
    2026-11-08 起 `copilot_auto.py drive` 在最近一轮 `verdict=partial` 且没有可执行安排时，
    按未闭合条件与回复原文本地派生一个 `origin=partial-without-plan` 工作项并进入 `EXECUTE`
    （0 请求、不消耗轮次、不需要用户传话或逐轮授权）。派生动作绑定 `source_round_id`，
    携带 `goal / constraints / expected_evidence / completion_criteria / authorization /
    return_contract / executed_by / directive_complete` 作为 `work_order`，明确「超出原授权
    必须 BLOCKED」。同一轮一旦已有执行结果、复核、报告或已完成派工，就不再派生，必须把
    新材料交回 SOL 判断。循环持续到 PASS 收口或真实 BLOCKED（需用户决策/授权/资源/不安全）；
    轮次数字、检查点、累计费用都不停止。「六维无进展」也不再直接停机：streak ≥2 或同一工作项
    连续失败 ≥3 次时，驱动汇总失败、未解决事项与新增证据，要求 SOL 分析原方案失效原因、
    给出替代路径、下一步动作、完成条件/证据与授权边界，执行后再次 REVIEW；诊断材料无新信息时
    才返回 `wait_user / no_progress_no_new_information`，REVIEW 恢复分析仍须显式 `--auto-review`。
    只有不存在安全可执行替代方案、缺少用户决策/授权/资源或外部硬限制时才 BLOCKED，并须有
    具体原因与证据；不得机械重复同一失败方案、伪造进展或降低验收标准。
    SOL 保持零工具、不承担执行；ZCode 主代理是唯一工程执行主体。依据：2026-11-08 用户要求
    解除 PARTIAL 后的人工中转与无进展即停。不变：G1–G4、独立复核、授权边界、PASS 语义
    （SOL PASS 仍需本地四门与 native 复核完成度）。

19. **未知用量单列，不与已知用量混算。**
    `copilot_dispatch.task_spend_from_audit` 按审计逐条分类：成功且有 `usage` 计真实 Token；
    成功但无 `usage`、已送达/可能送达的失败（HTTP 响应、超时、连接中断）无 `usage` 时计入
    `unknown_usage_calls`；能确认未送达的传输失败只计 `not_delivered_calls`；失败但返回部分
    `usage` 时记录已知 Token 并保留失败状态。同一响应按 `call_id + 升档序号 + response_id`
    去重，重试不重复累计；`budget.usage` 从审计跨轮次重建，已知总量只累计真实 `usage`，
    未知不当作 0，也不伪造 Token/费用。依据：2026-11-08 真实 REVIEW 中 408 无 usage 却
    `unknown_usage_calls=0`。

20. **文档现行口径统一到 ChatGPT 自定义指令 v5.3.2（2026-11-09）。**
    `SKILL.md`、`references/role.md`、项目 `AGENTS.md`、`docs/PROJECT-CHARTER.md` 的现行角色与
    协作语义以 v5.3.2 为准：六角色（用户 / ChatGPT 用户侧 / Copilot / SOL / ZCode 原生 Agent /
    DesignWise）不混同；SOL 只承担高级工程判断、技术方案取舍、风险分析与工程结果判断，需求理解、
    产品与业务判断及产品层方案归用户与用户侧 ChatGPT；不因人为默认轮次、请求数、累计 Token 或
    内部费用阈值提前停止；PARTIAL 不是默认终点；正式 REVIEW（隔离证据三态裁决）、PLAN / CONSULT
    运行时 verdict、原生独立复核与 G4 是不同机制，互不替代。v5.2.0 及更早的对齐文件只作历史；
    `~/.zcode/references/chatgpt-v5.3.2-alignment.md` 是**对齐摘要，不是原文**，也不是权限来源。
    第 16–19 条的预算、输出、自动推进与用量规则是该基线的本地落点。本条记录当时范围：只改文档，
    不改脚本、状态机、预算实现、模型路由与 G1–G4；其中「Python 内的旧预算 help / 注释 / 生成提示
    文本仍未修改」已由同日第 21 条取代（其余边界以第 21 条为准）。依据：2026-11-09 用户任务授权与
    一次真实 SOL PLAN 调用（`6eae8e768f390bbb`）。**本条记录 v5.3.2 基线批次；现行基线已由第 22 条
    升级为 v5.3.3。**

21. **运行时提示文本与独立复核轮次改为「证据驱动、无固定上限」（2026-11-09）。**
    调查确认「独立复核最多 2 轮」只存在于 `SKILL.md` 说明文字，代码侧（`copilot_dispatch.py`、
    `call_gpt.py`、`copilot_auto.py`、原生子代理链路）没有任何按次数的强制、拒绝或停止分支，
    `budget_status.allowed` 恒为 true、`limits_enforced=false`；它实际是文档级验收链停止门，
    可能让已整改且有新证据的任务在第 2 轮后无法再组织复核。据此：`SKILL.md` 改为**无固定次数
    上限**，每次追加必须携带与该问题相关的新增/变化证据；同一问题、同一组证据、改措辞或换文件名
    不构成再次复核理由；没有新证据时停止重复复核，但不停止已授权的整改、调查与取证。
    `review_ledger.py`、`call_gpt.py`、`copilot_dispatch.py` 的 help / docstring / `render_ledger`
    注入文本统一为「轮次与历史额度数字只用于审计观察，不是继续或停止条件」，不再向 SOL 暗示
    额度用尽须等待授权；`RISK_MAX_ROUNDS` / `EXTRA_ROUND_ALLOWANCE` /
    `SECURITY_EXTRA_ALLOWANCE` 仅保留为历史参考值，`--budget` 输出注明「不构成停止条件」。
    不变：G1–G4 与 G4 高风险 fail-closed（`native_review_missing`）、原生独立复核的只读与
    独立性要求、正式 REVIEW 授权门、证据去重与「无新增证据拒绝重复复核」、模型路由。
    SOL 的 CONSULT/PLAN 判断仍不是原生独立复核，也不是授权。依据：2026-11-09 用户任务授权
    与一次真实 SOL CONSULT（本轮决策与工作项）。机制改动已本地提交 `a5c56f2`
    （`fix(copilot): make review continuation evidence-driven`），未 push。

22. **文档现行口径升级到 ChatGPT 自定义指令 v5.3.3（2026-11-09）。**
    `SKILL.md`、`references/role.md`、项目 `AGENTS.md`、`docs/PROJECT-CHARTER.md` 与
    `docs/STATUS.md` 的现行协作基线引用由 v5.3.2 更新为 v5.3.3，历史版本记录保留。v5.3.3 与
    v5.3.2 的协作语义一致，主要消除歧义：ZCode 原生 Agent 是工程执行主体；Copilot 是运行于
    ZCode 内的协作治理机制；SOL 是 Copilot 按判据调用的大模型决策通道，不是独立执行 Agent；
    DesignWise 是运行于 ZCode 内的设计方法与质量规范 Skill；ChatGPT 负责产品与业务判断、
    产品层方案规划、任务交接与结果验收。默认闭环为「ChatGPT 定义目标 → ZCode 原生 Agent 承接
    工程任务 → Copilot 治理与推进（按需调用 SOL 决策）→ ZCode 原生 Agent 执行与验证 → 证据回传
    → 验收」，简单任务不强制经过 Copilot、SOL 或正式 REVIEW。
    `~/.zcode/references/chatgpt-v5.3.3-alignment.md` 是对齐摘要，不是原文，也不是权限来源。
    不变：自动推进、PARTIAL 修复、SOL 调用判据、正式 REVIEW 授权门、原生独立复核、G1–G4、
    模型路由与费用口径。依据：2026-11-09 用户任务授权。

23. **实战复盘（2026-11-10）：保留 fail-fast 与现有复核保真设计，只补齐失败后的恢复指引。**
    近两周 144 次真实 SOL 调用（126 成功 / 18 失败）中，失败均可归为外部原因或已修复缺陷：
    10 × 429 配额/冷却（2026-10-25 20:58 十路并发，账号 5 小时窗口耗尽）、4 × 输出截断
    （2026-11-08 起改为同轮升档恢复）、1 × TLS 握手 EOF、1 × 上游 408（`stream closed`）、
    2 × 客户端 300s 读超时。16 份真实 REVIEW 台账未发现证据截断、跨任务串线或裁决错配；
    唯一 UNPARSED 是 2026-10-28 的旧解析器缺陷，已于 2026-11-08 修复。
    据此**不改**三处：① 保留「HTTP 错误响应与结果未知不自动重试」的 fail-fast 策略
    （429/500/503 各只发一次有测试锁定；重试会重复计费，且 429 不证明额度已恢复）；
    ② 不上调 `--timeout` 默认值——它是 `urlopen(timeout=)` 的 socket 读超时，不是总时长上限，
    实测同一 call_id 成功耗时可达 530,471 ms，两次 300s 失败都是 300s 内无数据（2026-11-06
    重发后 15s 成功），上调只会延长故障发现时间；③ 不改台账逐轮携带历史证据的设计（单轮注入
    增长是保真代价，调整需产品侧决定）。
    **只改一处**：`copilot_auto.py` 的 `NEXT_STEPS["failed"]` 由「按退出码处理，不自动重发」
    改为写明可执行动作——有 pending 先按 `pending_call` 恢复、退出码 8 等配额、3/4 且无 pending
    时可显式再次 drive、7 不得重试、9 须先修正 CPA 路由再由人工决定是否重新发起；同时把失败
    `reason` 的「未写入任务状态」改为「本轮未入账：轮次与工作项未变（调用前指纹已落盘）」，因为
    `stamp_progress` 在调用前确实写过状态。依据：2026-11-08 19:07 自动闭环第 3 轮因上游 408
    返回 `failed`，实际恢复动作是 19:09 重新 drive、19:10 收口，原文案未覆盖该动作，执行方
    容易把可恢复失败误判为终止。不变：状态机、四门、证据协议、重试语义与模型路由。

24. **自评审 R1–R4（2026-11-10）：只修「会让判断失真或让状态走错」的缺陷，不新增架构层。**
    依据：以稳定基线 Copilot 对自身源码组织正式 REVIEW（17 条证据包），SOL 首轮裁决
    PARTIAL，提出 R1–R4；随后逐条在源码复现、修复、加回归测试（实测基线 728 → 781：
    R1–R3 后 779，R4 增补 2 项复核文件校验用例后 781；均为 `unittest discover` 口径，
    此前记录的 747 重复计入了 `test_sync_runtime.py` 19 项）。采用的最小
    修复：
    ① **裁决语法**去否定词黑名单，改为「独立、唯一、精确三态词」文法（允许首尾标点/强调
    修饰，附叙述或多裁决行一律无裁决），避免「裁决：PASS（…）」「未达到 PASS 标准」被记成
    PASS；
    ② **四门轮次**取最近一次带判断的已验证轮，最新判断非 pass 时旧 PASS 已被覆盖（新增
    `latest_judgment_not_pass`），`pass_pending` 与 `closure_check` 同源，消除「最新 PARTIAL
    仍拿旧 PASS 收口」；
    ③ **恢复 CAS 基线**取磁盘原文摘要（`disk-raw`），与 `_check_expect` 同源，旧状态不再因
    `load_state` 补默认键被误判为「外部修改」；审计同时记录 `loaded_state_sha256` 区分两种
    指纹，真实外部写入仍被拒绝；
    ④ **原生复核来源**以宿主 `metadata.outputFile` 为唯一真值（按严格 JSON 解析；仅兼容整段
    输出被包在单个 ```json 围栏里的形式，围栏外有叙述或多围栏一律拒绝；必须与 metadata
    同目录），信封与输出必须绑定 `task_id/work_item_id/execution_result_sha256`，调用方副本
    不能再改写裁决；`files_inspected` 必须对应真实工具调用（文件名与所在目录名需出现在同一
    命令段，或出现完整路径）；
    ⑤ **回填门禁**先于 `_apply_manifest`：`REVIEW_PENDING/QUOTA_WAIT/BLOCKED/...` 整轮拒绝且
    不写盘；
    ⑥ **上下文预算**保护段 6000、单条条件 800、条目永不省略，条件段整体超限时失败关闭并给
    恢复动作；最低档把已终态工作项压成 `id(status)` 汇总而不抛错；工作项标题/预期证据也设上限；
    ⑦ 其余真实性修正：HTTP 200 携带配额体走配额通道（退出 8、不重发）、`--segmentation-plan`
    与输入/审计的路径别名保护、`include` 只接受 JSON 布尔、自动追加理由只陈述事实（是否新增由
    台账按内容哈希判定）、本地子进程超时转可读错误不重试；
    ⑧ **已收口回填**：`CLOSED` 任务提交 `--result-file`/`--native-result` 整轮拒绝（exit 2、
    「未写入任何状态」），不再静默忽略后返回成功；
    ⑨ **恢复窗口**：先取磁盘原文基线再重载状态，`load → 基线` 之间的外部写入被读入而非被
    覆盖；基线后的写入仍由 CAS 拒绝；
    ⑩ **复核文件冒领**：`files_inspected` 要求完整路径，或文件名与父目录名出现在**同一命令段**
    （Bash 按 `;`/`|`/换行拆段）；复合命令拼凑与跨目录同名均拒绝，相对路径合法读取不误拒。
    ⑪ **SKILL.md 体积**：贴近 100,000 字节上限本身是可用性风险（超限由加载器静默截断头部），
    因此只做「删重复复述」的最小精简（99,487 → 98,051 字节，余量 1,949；其中 +21 字节为后续
    裁决文案对齐），不删触发条件、任务规则
    与安全约束；只读命令矩阵等细节由 `references/agent-bridge.md` 承载，SKILL.md 保留规则类别与
    失败关闭语义。其中 99,487 是本轮工作区的中间态字节数（精简前快照），不是 Git 基线：
    `git show HEAD:SKILL.md` 为 95,002 字节、`HEAD~2` 为 98,426 字节，回看差异需以工作区 diff 为准。
    **不变**：状态机九段阶段、G1–G4 语义（G4 仍 fail-closed）、证据登记与绑定协议、模型路由、
    复盘结论（不上调 timeout、不改 fail-fast、不改台账携带历史证据）。**未做**：不做架构重写、
    不新增层、不改 Provider/模型/凭据、不提交不推送。

25. **Reviewer 加固（2026-11-10）：只读命令拒绝引号外 shell 元字符，JSON 形态复核报告解析失败
    一律拒绝。** 依据：以稳定基线 Copilot 对本轮 Reviewer 加固成果组织正式 REVIEW（台账
    `copilot-native-reviewer-upgrade-v1`、call `fe1b0abdac569fc0`），SOL 裁决 PARTIAL 并指出
    F1/F2/F3；F1/F2 已在源码复现、修复并加回归测试。最小修复：
    ① **F1 只读命令**（`native_bridge.py`）：`shlex` 解析后未引用通配符仍是字面参数，执行时由
    shell 展开，`uniq /tmp/sample-*` 会变成「输入 + 输出」并覆盖文件。现拒绝引号外的一切 shell
    元字符（含 `* ? [ { } & > < \` $`、换行与 `"`），保留引号内字面量与 `;`/`|` 组合的合规只读
    命令；另拒绝长选项唯一前缀缩写、短选项聚簇、`find` 的 `--` 后谓词与小写 `git -c` 注入。
    前缀缩写保护按命令家族登记（当前 `git grep`、`sort`），未登记家族由 argv 重解析兜底，
    不宣称已覆盖全部命令，此残余风险已记录。
    `/dev/null` 重定向**只在 token 边界**消费（`>` 前必须是段首或空白，`2>` 的 `2` 前亦然），
    紧贴参数的写法（`sort -k2>/dev/null -o out in`，shell 里 `2` 属于 `-k2`）直接拒绝：早期
    版本在引号解析前用正则整体替换 `2>/dev/null`，会把校验用文本改成 `sort -k -o out in`，
    使真实 argv 的 `-o` 被当成 `-k` 的参数值跳过（校验 argv ≠ 实际 argv）。现不再改写命令文本。
    `2>` 前的空白还必须是**未转义**的：`cat /tmp/input\ 2>/dev/null` 里空格被反斜杠转义，
    shell 词法上 `2` 属于普通参数 `/tmp/input 2`，删掉它同样会让校验 argv ≠ 实际 argv；连续
    反斜杠数为奇数即视为转义，此时只消费重定向本体、保留 `2`（`a\\ 2>` 是裸空格，仍按 fd 处理）。
    token 边界只认 **Shell 空白（空格与制表符）**：Python `str.isspace()` 还把 U+00A0、U+000B
    等算作空白，但 bash 把它们当词内字符（`input<NBSP>2>/dev/null` 的 `2` 属于参数）；边界
    判定、转义判定与分段裁剪统一用 `_SHELL_SPACE`，非 Shell 空白紧贴重定向按「紧贴参数」拒绝。
    ② **F2 结构化复核报告**（`copilot_dispatch.py`）：截断 JSON 若用任意合法转义写 schema
    （如 `copilot-native-rev\u0069ew/v1`）就匹配不到明文标识，会被降级成 legacy 文本并跳过原始
    verdict 一致性检查。现按**声明 + 格式边界**两层判断：
    声明层——`_declares_native_schema` 先把 `\\uXXXX` 全部解码再匹配 schema 字段与标识，任意转义
    写法（值或字段名）都能识别，schema 键名允许引号内首尾空白；解析成功后按**全部**同名（忽略
    大小写与首尾空白）schema 键判定，**逐值校验**：任一含标识的字符串必须精确等于该标识，任一
    非字符串值含标识即拒绝，判定与键序无关（早期版本用「最后一个 native 值」覆盖，`…/v1 extra`
    可被后面的合法声明掩盖）。嵌套 schema 与带后缀的标识同样拒绝。解析后没有保留任何 native
    声明时（如 schema 字段自报其它值、正文声明与解析结果不一致）再用声明层兜底拒绝，不靠结构
    位置降级为 legacy。
    **重复键一律拒绝**：登记前用 `object_pairs_hook` 保留原始键值对，任意层级出现重复键即失败
    关闭且状态不变。`json.loads` 会把同名键折成最后一个值，`{"schema": "…/v1 extra",
    "schema": "…/v1"}` 折叠后非法声明被静默隐藏，`{"verdict": "blocked", "verdict": "pass"}`
    更能直接改写原始结论；检查不限于 native 报告，legacy JSON 同样适用。
    格式层——整份报告是纯 JSON（`{`/`[` 开头），或**单个代码围栏**包裹的报告本体（围栏前只有
    空白、围栏信息串不限、收尾围栏同字符且不短于开头、之后只允许空白）；声明了 native schema
    的报告在截断、缺收尾、多围栏、围栏前后夹散文时解析失败一律拒绝登记、状态不变。完整围栏内的
    合法 native JSON 照常解析并按原始 verdict 登记，与宿主输出「整段是一个围栏」的方向一致
    （`native_agent` 只认 ```，这里对围栏字符与信息串更宽松；宿主输出解析本身未改）；解析前剥离
    BOM。围栏前后还有别的文字时不算整份报告：只有完全没有 schema 声明才按 legacy 文本（不带声明
    的多围栏普通评审文本仍可登记）。
    ③ **裁决契约文案**与实现对齐：`review_ledger.parse_verdict` 要求全篇**恰好一行**裁决行，
    重复同一裁决也返回 None；用 tab 缩进的行同样按缩进代码排除（与 SKILL 文案一致）。
    `call_gpt.REVIEW_OUTPUT_CONTRACT` 与 `SKILL.md` 原写「多个不同裁决值」才按无裁决，现统一为
    「只能有一行（重复也算违约）」，不放松解析器。
    **第二轮正式 REVIEW**（同台账 call `d5eceae51f98ae65`）裁决 **BLOCKED**：指出上述重定向预处理
    与 schema 顺序依赖两处代码缺陷（已按本条修复并加回归），以及 F3 真实 Reviewer 链路缺证——后者
    需用户授权最小必要条件或调整完成条件，不由执行方自行降级。
    **第三轮正式 REVIEW**（call 见台账第 3 轮）裁决 **PARTIAL**：F1 方向正确但要求补齐完整扫描实现
    与边界矩阵；F2 指出**完全同名重复键折叠**仍可掩盖非法声明（已按本条「重复键一律拒绝」修复）；
    F3 主体绑定成立，仍要求离线补取原始记录（PLAN 审计、Reviewer 完整调用、宿主原始输出、生成与
    接纳命令及退出码）；最终同步/基线在最新指纹上需重跑。真实链路按用户授权执行：PLAN 1 次、
    Executor 1 个、Reviewer 生效 1 份信封（前 3 次运行被 fail-closed 拒绝，未登记）。
    **第四轮正式 REVIEW**（同台账第 4 轮）裁决 **PARTIAL**：F1 指出转义空白边界仍会删除普通 argv
    字符（已按本条「空白必须未转义」修复并加 argv 保真回归）；PLAN 审计摘要与回复文件摘要不一致
    经离线查明为 `.strip()` 与 `--out` 追加 LF 的口径差异（4,722 字节文件去掉尾部 LF 后摘要
    等于审计值，未改历史记录）；rollout 第 16 次调用截断、信封重生成/`cmp`/`ingest` 缺确切命令，
    已按未截断原文与独立退出码补齐；最终 `--check` 原始输出与测试进程/包装命令退出码分开记录。
    **第五轮正式 REVIEW**（同台账第 5 轮）裁决 **PARTIAL**：仅剩一项——`str.isspace()` 把 U+00A0、
    U+000B 等非 Shell 空白当作 token 边界，仍会删除普通参数字符 `2`。已按本条「token 边界只认
    Shell 空白」修复（边界、转义与分段裁剪统一 `_SHELL_SPACE`），并补 U+00A0/U+000B 的允许与
    拒绝用例；其余来源、过程、测试与同步补证项经该轮裁决为闭合。
    **第六轮正式 REVIEW**（同台账第 6 轮）裁决 **PASS**：「R5-LEX 已闭合，无剩余阻塞发现」。
    非阻塞限制保持：命令校验是事后检测而非执行前沙箱、`changed_files` 仍是 Reviewer 自报、
    NUL 的传输与运行行为未经矩阵验证；该裁决不等于 G4 满足，也不等于任务 CLOSED。
    **不变**：G4 仍 fail-closed、证据登记与绑定协议、Reviewer 只读 Profile、状态机与模型路由。
    **F3**（真实 Reviewer 链路复核信封）需要真实 native dispatch 的绑定才算数，本轮授权不含
    该调用，因此保留为已声明缺口，G4 不宣称满足。

## 建议（未采纳）

- 每次源码修改通过测试后，按任务授权在本地 Git 提交可回溯版本；未获 commit 授权的
  任务不提交。
- 在同步前增加“工作区必须干净/已记录基线”的检查，减少把未审 diff 直接下发。
- 为仓库增加离线 CI（仅运行测试，不依赖真实模型与网络）。
- 观察运行时 `tests/` 一段时间后，在明确授权下清理或归档。
- 上调 `call_gpt.py` / `copilot_auto.py` 的 `--timeout` 默认值（300s → 600s）。实测该值是
  socket 读超时而非总时长上限（同 call_id 成功耗时可达 530,471 ms），两次 300s 失败均为
  连接无数据、重发即成功；上调只延长故障发现时间，不提高成功率（第 23 条）。
- 为 429 并发突发增加全局串行/排队闸门。2026-10-25 的 10 次失败源于账号 5 小时窗口额度
  耗尽（`resets_in_seconds≈15008`），不是并发本身；加闸门是用高频维护成本换低收益。
- 压缩复核台账逐轮携带的历史证据（如只带上一轮新增）。会削弱「SOL 能看到全部已提交证据」
  的保真度，成本与质量权衡归产品侧，不在工程侧自行决定。

## 待决

- **Phase 3.6 真实 SOL 端到端调用（Native Bridge C4）**：2026-11-08 已在用户授权下完成 **4 次真实
  SOL 调用**，局部验证了默认字段省略、截断识别与一次升档恢复；**完整 Native Bridge C4 端到端
  仍未验证**，需单独授权（模型、请求数、token 预算、`max-retries=0`、用途与成本不确定性）。
  四次局部调用不能替代 C4 验收。
- **Git 远程与后续提交策略**：已建立本地 `main` 基线（第 12 条）；是否配置远端、
  何时提交后续变更、是否要求分支保护，仍需用户决定。
- **运行时残留测试与历史备份的长期保留策略**。
- **Phase 4 剩余范围**：Reviewer 自动派发、可信复核记录升级仍需单独授权与实现；
  SOL 自动重判/收口与 PARTIAL 自动推进已按第 18 条落地（本机实现，未经真实长链验证）。

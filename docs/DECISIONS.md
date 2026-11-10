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

## 建议（未采纳）

- 每次源码修改通过测试后，按任务授权在本地 Git 提交可回溯版本；未获 commit 授权的
  任务不提交。
- 在同步前增加“工作区必须干净/已记录基线”的检查，减少把未审 diff 直接下发。
- 为仓库增加离线 CI（仅运行测试，不依赖真实模型与网络）。
- 观察运行时 `tests/` 一段时间后，在明确授权下清理或归档。

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

# review：认知 CONSULT / PLAN / REVIEW、正式 REVIEW 与已退役的 G4

本文说明当前几类“认知与复核”，以及各自的触发条件、证据要求与失败处理；G4 历史记录
只读保留。

**一句话区分：**

- **普通自检**：ZCode 自己做，不登记为复核，不构成任何门禁依据。
- **认知 CONSULT（默认认知入口）**：按 T1–T3 认知等级在启动、升级型事件与收口前一次性调用
  `gpt-6.2-sol`（零工具、单轮、high），输出目标理解、问题本质、路线与取舍、风险、调查方向
  与再咨询条件；结论是认知输入，不是门禁。T0 不产生调用。
- **认知 PLAN（调查后的规划判断）**：T2/T3 任务在形成现场事实后调用，输出方案比较、优先级、
  阶段目标与验收关注点；最终技术方案仍由 ZCode 决定。
- **认知 REVIEW（用户要求）**：用户明确要求时一次性独立复核，输出结构化 findings；结论是
  建议，不是门禁。
- **正式 REVIEW**：用户明确授权时才做，是一个治理记录，**不是普通开发的强制前置门**。
- ~~**G4 复核**~~：v8.0.0 已正式退役，不再是门禁；历史记录只读保留用于审计。

## 0. 认知零工具单轮调用（v9.0：CONSULT / PLAN / REVIEW）

### 统一入口与共享上下文

- 三种模式共用一条管线：`copilot_dispatch.py` 的 `_run_cognition_channel` → `cognition_call.run_cognition`
  → `governance.record_cognition_call`；模式差异只在提示词、阶段语义、结果结构与失败语义。
- 提示词由 `cognition_call.build_prompt` 统一构造，自动注入同一份任务目标、合同正文（
  `done_criteria_text` 与边界）、完成条件、工作项状态与证据登记摘要（ID、类型、摘要前缀、
  大小、支持的条件、归档标记，上限 80 条 / 6000 字符）。主代理只需提供本次增量的材料与
  具体问题，不必重复粘贴门禁已有事实。
- `review-admission` / `evaluate` / `drive` / `status` 是“要不要调用认知”的确定性入口：
  先算任务类型与本地复杂度（只用状态事实），再结合已有覆盖给出建议；不写状态、不派生
  工作项、不改变阶段。
- 复杂度的唯一口径是**认知等级 T0–T3**（见 `runtime.md` §5）：第一依据是用户原始意图
  （`start --intent` / `intent --text|--file`），再结合目标、合同文字与状态事实；文件类型、
  工作项数量与默认 `risk=medium` 不能单独判成 T0。没有硬性次数或 Token 上限。

### 任务类型与生命周期

`start --task-kind <kind>` 声明 16 类任务；未声明按 `standard` 处理。等级按合同内容与原始
意图判定，不只看类型：

- 显式简单类型（`trivial` / `mechanical` / `docs` / `format`）且无信号、无高影响范围 → T0；
  一旦合同或意图涉及治理/规则/架构/协作，或出现 `risk-high` / `replan-history` / `reopened`，
  仍会升级；
- 高影响任务类型（`governance` / `architecture` / `data-infra` / `product-design` /
  `release`）与高影响触发因素（安全/权限/数据完整性/生产等）→ T3；
- 方案类类型（`PLAN_KINDS`）本身构成 `plan-kind` 信号，进入 T2 或以上；
- 未声明类型的 `standard` / `feature` / `bugfix` 默认 T1（一次启动 CONSULT），不因“没写
  类型”或“默认风险值”被压成 T0；
- 任务类型不是调用开关，工作项数量本身也不是高风险或高认知价值的证据。

| 阶段 | 建议 | 入口 |
|---|---|---|
| A 启动 | T1/T2/T3 在启动时执行一次 CONSULT：目标与隐含需求、问题本质、关键矛盾、初步策略、风险、调查方向 | `cognition-consult --stage understanding` |
| B 调查后 | T2/T3 在形成现场事实后执行一次 PLAN：方案比较、取舍、优先级、阶段目标 | `cognition-plan --stage planning` |
| C 执行期 | 升级型新信号（风险升级/方案失败重规划/任务重开）或合同实质修订时追加 CONSULT | `cognition-consult --stage development --force --trigger ...` |
| ~~D 阶段复盘~~ | **已退役**：普通里程碑不再自动派生义务；需要阶段性判断时由主代理按价值手动发起 | `cognition-consult --stage stage` |
| E 收口前 | T2/T3 且最终工程验证完成后（工作项终态、条件核销、证据与必要测试齐备）一次综合判断：目标达成、遗漏与剩余风险 | `cognition-consult --stage closure` |

- **E 的时机不是“最后一项关闭”**：收口 CONSULT 以 `closure_readiness` 为唯一准入——工作项
  终态、条件核销、证据登记、合同必要测试的执行痕迹全部满足后才执行；G1–G3 通过本身不等于
  最终验证完成。义务的 instance 是 `final_verification_digest`：摘要变化后旧结论不再覆盖新
  结果，同一摘要不重复付费。机制见 `runtime.md` §5“收口准入”。
- **追加而不是配额**：`--force --trigger <new-risk|scope-changed|stage-result|
  user-request|...> --reason ...` 是“为什么这次判断可能改变方案/优先级/风险”的决策价值
  声明，不是调用次数上限。
- **覆盖**：同一合同（`contract_sha256` 相同）内 CONSULT 与 PLAN 分别判断覆盖；没有升级型
  新信号时返回 `reuse`，直接复用结论。合同实质修订后旧结论不再覆盖，先重新 CONSULT。
- **不触发的情形**：普通测试失败、普通步骤推进、证据目录增加、工作项增减等结构变化不算
  新的认知对象；任务级 BLOCKED 或已 CLOSED 时不建议新的认知调用（项级阻塞只冻结该项）。
- **REVIEW**：只由 `review-admission --review-request`（用户明确要求）给出，不由阶段、风险
  或调用次数自动派生。

### 模型与推理强度

- 默认 `gpt-6.2-sol`（Responses API `/v1/responses`，既有本机 CPA `127` 出口）；
  `gpt-6.2-astra` 只作为显式可选（`COPILOT_COGNITION_MODEL` 或显式参数），没有 `--model`
  开关、没有别名，也没有失败回退或自动切换：SOL 不可用不会改选 Astra。
- 请求、回显校验、审计与 `cognition_calls` 记录都带 `provider` 与 `model_key`（`codex:<model>`）；
  复用只命中同模型记录，缺模型归属的历史记录不被当成新模型响应。
- 所有模式统一 `reasoning.effort="high"`、`reasoning.mode="standard"`：不按角色降级，
  不接受 `low`/`medium`/`xhigh`/`max`，也不允许配置缺失时静默回退。
- 不发送 GPT-6.2 推理模型不支持的采样参数（`temperature`/`top_p`/`top_logprobs`/
  `logprobs`），不请求 `include` 的 logprobs；保持单请求（`store=false`，不使用
  `previous_response_id` 多轮会话）。
- 上游回显 `model`、`reasoning.effort`、`reasoning.mode` 必须与请求一致；不一致按身份/
  参数覆盖事件拒绝消费（退出码 9）。回显存在且一致时记录 `reasoning_verified=true`；
  上游未回显时如实记为未验证，不推测为成功。
- 审计与任务记录保留 `request_id` / `trace_id`（CPA trace）/ `response_id`、usage 与延迟。

### 调用（CONSULT / PLAN）

```bash
python3 -B scripts/copilot_dispatch.py cognition-consult --state <state.json> \
  --materials <materials.md> --stage understanding \
  --question "本次真正需要判断的事项"

python3 -B scripts/copilot_dispatch.py cognition-plan --state <state.json> \
  --materials <materials.md> --stage planning \
  --question "需要在哪些方案之间取舍"
```

- `--question` 必填：没有明确问题时不要调用认知。`--stage` 省略时 CONSULT 默认
  `development`、PLAN 默认 `planning`；显式阶段必须是
  `understanding|planning|development|stage|closure` 之一。
- 材料只包含目标、当前基线、已确认事实、关键问题与约束、已尝试方案及结果、证据摘要；
  上限 120,000 字符（提示阈值 60,000），超限拒绝而不是静默截断。
- 零工具、单轮、默认 `gpt-6.2-sol` + `high`（Astra 显式可选），与 REVIEW 使用同一通道与同一安全约束。
- `--dry-run` 校验请求（模式、阶段、模型、推理强度、零工具、材料预算）；
  `--list` 只读查看记录；`--knowledge-file handoff.json` 追加知识交接块。

### 输出（CONSULT / PLAN）

- `copilot-cognition-consult/v2` 超集：`judgment`、`goal_understanding`、`essence`、`strategy`
  为字符串；`alternatives`、`tradeoffs`、`risks`、`priority`、`investigation`、
  `phase_goals`、`assumptions`、`omissions`、`decisions`、`verify`、`reconsult`、
  `unverified` 为字符串数组。schema 不一致、字段类型不符或没有判断内容时拒绝消费。
- 结果写入状态 `cognition_calls`（`mode=CONSULT|PLAN`，含 `stage` / `question` /
  `progress_digest` / `complexity_signals` / `acceptance`），原始输出存 `cognition-consult/` 或
  `cognition-plans/`，审计写同一份 `cognition-audit.jsonl`（仅脱敏元数据）。
- **治理边界**：不进入 Finding 台账、不改变完成条件、不影响 G1–G3、不阻断任务；是否采纳、
  如何实施由 ZCode 主代理决定。采纳结论用
  `cognition-accept --state <state> --call-id <SC...> --status accepted|modified|rejected` 登记
  （事实记录，不是用户授权，不改变门禁）。

### 采纳记录（v9.1，`copilot-cognition-acceptance/v2`）

```bash
python3 -B scripts/copilot_dispatch.py cognition-accept --state <state> \
  --call-id SC... --status modified \
  --decision "先补定向测试再改实现" \
  --rationale "现场缺少回归覆盖，直接改风险更高" \
  --delta "不采纳原建议的一次性重写，改为分两步" \
  --evidence-ref E1,E3 --source-ref "报告 §3 差异分析"
```

- **字段**：`decision`（ZCode 实际做法，不是认知原建议）、`rationale`（省略时回落
  `--reason`）、`delta`（modified 与原建议的差异）、`evidence_refs`、`source_refs`、
  `evidence_note`；保留 `status`/`note`/`reason`/`decided_by`/`at`/`basis`。
  `advice_ref` 只存 `call_id`/`mode`/`stage`/判断摘要/`reply_path`/`reply_sha256`，
  不复制原建议正文。
- **证据解析（登记时）**：`evidence_refs` 逐条判定为 `linked`（本任务已登记且未归档）、
  `unresolved`（未登记）或 `archived`（已 `dropped`），保存在 `evidence_resolution`；
  汇总状态由逐条结果派生（`linked` / `partial` / `unresolved` / `archived_only` /
  `source_only` / `none`）。未登记与归档引用只作待核引用，不自动补登记，也不进入正式
  证据链（`record`/`criterion`/`finding` 仍只接受已登记证据）。
- **非阻断提示**：`actual_decision_missing`、`rationale_missing`、
  `modified_delta_missing`、`evidence_missing`、`evidence_unregistered`、
  `evidence_archived` 写入记录并在 CLI 打印中文提示；登记始终成功、退出码 0，不改变任务
  阶段、证据表或 G1–G3。E 编号格式错误是输入错误，直接拒绝。
- **重复登记**：以最近一次为准，旧字段不隐式继承；每次登记的完整快照追加到
  `history` 的 `cognition_call_acceptance` 事件（只增不改）。`first_registered_at` 保留最初
  时间；`registration_count` 只计 v2 登记次数，不冒充历史全量。
- **历史兼容**：无 `schema` 的旧采纳记录保持只读可读，`reason` 作为历史理由展示，
  `note` 不被当成实际决策；不迁移、不重写、不重放已 CLOSED 任务。
- **指标**：`review-metrics` 的 `cognition.acceptance_quality` 以 v2 已决记录
  （accepted/modified/rejected）为分母，输出 `complete_rate`（决策+理由+modified 差异）、
  `evidence_link_rate`（至少一条 linked E）、`source_only`、`no_evidence`、
  `unresolved_records`、`archived_records`、`modified_with_delta` 与 `legacy_records`；
  零分母返回 `null`。原 `acceptance` 四态与 `adopted` 口径不变。这些是观察指标，
  不是验证率、KPI 或门禁。
- **失败**：只登记真实原因并返回 `status=unavailable`（退出码 0），主代理按已有判断继续。

### 触发与调用（REVIEW）

**只在用户明确要求时调用一次。** 不因风险等级、收口阶段或“也许需要第二意见”自动派生；
`review-admission --review-request` 只把用户要求写成 suggestion（`not_needed` /
`optional` / `recommended`），不写状态、不阻塞。

```bash
python3 -B scripts/copilot_dispatch.py cognition-review --state <state.json> \
  --materials <materials.md>
```

- `--materials` 由主代理一次性准备：目标、完成条件、相关规则/决策、完整 Diff、关键文件、
  调用链、测试结果、未解决问题与风险点。命令会自动注入合同/目标/条件；超过 400,000 字符
  拒绝而不是静默截断。
- **零工具**：请求体不含 `tools`/`functions`/`parallel_tool_calls`，`tool_choice="none"`；
  上游出现 `message`/`reasoning` 之外的 output 条目、回显非空 `tools`/`tool_usage`，即判
  安全事件并拒绝。
- `--dry-run` 只构建并校验请求（零工具、模型、推理强度、材料大小），并输出
  `prompt_version`、`cache_key_components`、`evidence_digest` 与注入的证据/工作项数量；
  不发请求、不写状态。

### 单轮与失败

- 一次调用只发一次请求。相同认知对象（合同 + 材料 + 问题 + 阶段 + 提示词版本）再次调用
  直接复用已有结论，不重复付费。CONSULT / PLAN / REVIEW 各自独立判定，不互相复用。
- **复用键**（`review_subject_sha256`）：提示词版本 `PROMPT_VERSION`、**提示词模板摘要**
  （`MODE_INSTRUCTIONS` + `STAGE_FOCUS` 的 SHA-256，改了模板但忘记 bump 版本也会失效）、
  模型与推理强度、任务目标、合同正文与边界、完成条件描述、证据集合摘要、主代理材料
  （按发送时的 `strip()` 归一）、模式与阶段；辅助分析额外绑定问题。条件从 pending 变 met
  不失效；合同、证据集合、提示词版本/模板或材料变化即失效，不会复用过期判断。
- `--force` 覆盖“相同材料”的复用命中：进度或上下文变了但主体键不变时，明确要求重新咨询
  不会被复用分支挡回。
- 再次调用必须 `--force --trigger <user-request|scope-changed|new-risk|materials-omitted|
  strategy-failed|major-uncertainty|stage-result> --reason <说明>`，并换新材料或新阶段。
- 超时、429/5xx、网络、截断、响应格式、模型身份、推理强度/模式回显不符：记录真实原因与
  `cognition_calls` 失败记录（历史状态仍读 `sol_reviews` / `sol_advisories`），不自动重试、不换
  Provider、不派原生 Reviewer 顶替。CONSULT / PLAN 失败只登记并返回“可继续”（退出码 0），
  主代理按已有信息推进；REVIEW 失败返回真实非零退出码，如实报告，不伪造结论。
- 失败不产生阻塞、不派生认知工作项。

### 输出（REVIEW）

- `copilot-cognition-review/v1`：`verdict`（pass/partial/blocked）、`findings`
  （blocking/major/minor/info，含文件/行/依据/建议）、`unverified`、
  `materials_reviewed`。PASS 与 blocking/major 或未验证项冲突时降级 PARTIAL。
- findings 以 `advisory` 进入 Finding 台账：主代理修复后 `finding --status fixed`，
  运行定向测试后 `--status verified`；确认误报时 `--status dropped --note <反证>`。
- 原始输出存任务目录 `cognition-reviews/`，审计 `cognition-audit.jsonl` 仅脱敏元数据（三模式共用）。

### 与门禁的关系认知没有工具、不能核验宿主权限，也不证明文件读取；认知的 PASS 不会写入
`native_reviews`，不会产生 `g4_eligible`，不改变 G1–G3 的结果。认知未执行、失败或给出
partial 都不阻断主代理继续推进（用户明确要求必须完成独立复核而认知不可用时除外）。

## 1. 正式 REVIEW（opt-in）

### 触发

只有在**用户明确要求**正式 REVIEW（或明确要求独立审核/第二意见）时才进入。普通开发、
跨文件改动、任务复杂、存在审核模式都不构成触发条件。

### 记录授权

```bash
copilot_dispatch.py authorize-review --state <state> --authorized-by user \
  --reason "用户要求交付前正式复核" --scope "当前任务合同"
```

- `--authorized-by` 必须显式传入且只能是 `user`（没有默认值）；它是调用方对“用户在该
  会话中明确要求”的声明，不是可验证凭证，治理层只能拒绝未声明或非 user 的登记。
- 必须写明用户要求与范围；授权记录绑定 `task_id` 与合同指纹，合同修订后需重新授权。
- 授权在同一任务合同内持续有效，不逐轮重新确认；范围或风险实质变化时重新确认。
- 记录授权**不会**自动执行 REVIEW，也不产生任何状态门禁。

### 登记结论

```bash
copilot_dispatch.py formal-review --state <state> --verdict pass \
  --summary "复核结论" --review-ref <产物路径或 SHA> --reviewer native
```

- 没有用户授权记录、或授权绑定的是旧合同时直接拒绝：不得伪造正式 REVIEW，也不得用
  普通自检冒充。注意这是登记层校验，不构成对调用方诚实性的密码学保证。
- 记录包含 `review_id`、verdict、摘要、引用、复核者、授权快照与当时的
  `target_sha256`，写入 `history`，只作审计与追溯。
- 正式 REVIEW 结论**不参与** G1–G3 判定；它不能让任务跳过证据要求。

### 修订与 supersede

正式 REVIEW 的“结论”和“报告引用”分开管理：

```bash
# 非实质修订：证据编号、引用链接、文字口径；目标摘要必须未变
copilot_dispatch.py formal-review --state <state> --amend FR<id> \
  --review-ref <新引用> --reason "仅更正证据编号，结论未变"

# 实质变化：证据/目标摘要变化后重新验证；旧记录原样保留
copilot_dispatch.py formal-review --state <state> --verdict pass \
  --summary "新证据下复核通过" --supersedes FR<id> --reason "证据实质变化"
```

- `--amend` 不生成新的 PASS，不改变原 verdict；原记录、授权快照与 `target_sha256` 不变，
  只在原记录下追加 `amendments[]`。
- 目标摘要变化时 `--amend` 直接拒绝：amendment 不能继承旧 PASS；必须用 `--supersedes`
  登记新结论，旧记录由 `review-metrics` 标为 superseded。
- 即使修订看起来只是“证据编号”，只要它新增/更正/归档了证据条目并改变了
  `final_evidence_digest`，就按实质变化处理，走 supersede；amendment 只用于目标摘要
  未变的引用、链接与文字口径修订。这样同一结论不会再被重复登记为新的 PASS，但可能
  表现为“1 条原始 PASS + 若干 supersede”，而不是所有修订都塞进 amendment。
- `--supersedes` 仍走原有授权与合同绑定校验；不能用 amendment/supersede 修改原始授权快照。
- `review-metrics` 分别输出 `formal_reviews_total`、`formal_review_amendments_total`、
  `superseded_formal_reviews_total` 与 `active_formal_reviews_total`，不把 amendment 计为
  新的 PASS。

### 独立性

v8 起原生 Reviewer 已退役，正式 REVIEW 的独立意见只能来自 SOL 零工具单轮复核：材料由主
代理一次性准备，请求体不含工具定义（`tool_choice="none"`），一次调用只发一次请求，模型
回显必须与请求一致。无法真实调用认知时，如实说明是“主代理自检”或“同一上下文复核”，
不得标记为正式 REVIEW，也不得伪造认知结论。历史原生 Reviewer 结论只读保留，不参与新任务。

## 2. 独立复核流程（认知）

### 调用建议（认知）

| 场景 | 建议 |
|---|---|
| T0：显式简单类型、无信号、无高影响范围 | 0 调用；主代理自检闭环 |
| T1：启动 | 启动 CONSULT（understanding） |
| T2/T3：启动 | 启动 CONSULT（understanding） |
| T2/T3：调查后形成现场事实 | PLAN（planning） |
| 出现升级型新信号（风险升级/重规划/重开）或合同修订 | 追加一次 CONSULT（C） |
| ~~T2/T3 重要阶段完成~~ | 里程碑复盘已退役；需要阶段判断时按价值手动发起 |
| T2/T3：最终工程验证已完成 | 收口 CONSULT（closure）；验证缺口未补齐时不执行；采纳与否都不阻止收口 |
| 用户明确要求独立复核（`--review-request`） | 一次 REVIEW |
| 用户明确要求认知判断（`--user-request`） | 直接一次 CONSULT |

`review-admission` 只返回建议（`none` / `reuse` / `consult` / `plan` / `review`），不写
状态、不是门禁。`recommended_action` 由任务类型、确定性复杂度与合同覆盖判断给出，不再
要求 G1–G3 全绿；门禁缺口不改变认知建议。REVIEW 只在 `review_requested`（用户明确要求）
时建议，不由风险等级、阶段或调用次数自动派生。

### Finding 台账与定向复验

- 每条实质 finding 进入 `finding_ledger`，使用稳定 `F1..Fn`；同一根因换一种表述仍映射到
  同一 ID（显式 `id` / `root_cause` / 文件+摘要相似度），已关闭问题没有新证据只记为
  重复报告。
- 生命周期 `open → fixed → verified → closed`；`fixed` 只表示已改，`verified` 才表示修复
  被定向验证。`fixed`/`verified` 必须有证据，`closed` 只能从 `verified` 进入。
- 未闭合 `blocking` 阻断 G2（任何风险等级）；`major` 只在 `risk=high` 时阻断，普通任务
  按提醒呈现；minor/info 不阻断。已修复未验证派生
  `AUTO-VERIFY-<F>`：只跑定向测试并登记证据，不重新派发完整复核。
- 认知 findings 是 advisory：主代理修复并定向验证，误报可用
  `finding --status dropped --note <反证>` 驳回；v7 及更早的 authoritative 记录仍只能
  修复并定向验证。

### 复核收敛流程

充分自检 → 稳定最终执行结果与证据 → 按生命周期做一次收口 CONSULT（如建议）→ 集中修复 →
定向验证 → 收口。认知不是收口前提；主代理判断材料是否具备、意见是否有增量价值。
目标 1–2 轮收敛是性能目标，不是硬上限；不得为减少轮次而漏报或降低标准。

### 首轮完整性协议

- 一次看全：枚举变更面、读取受影响调用方与测试、按根因归组、检查同类缺陷的其他位置；
  finding 使用稳定 ID（`F1..Fn`）与可选 `root_cause`/`category`/`related`。
- 材料必须真实完整并对应最终代码；材料缺失时认知应标记 `unverified`，不能推测已检查
  未提供的代码。主代理不得为了让评测结果好看而删减材料。
- PASS 即停止：合同与条件满足、无 blocking、`unverified` 为空即停；minor/info 记录为
  findings，不驱动重开。

### 后续轮次与停止

- 同一材料摘要直接复用已有结论，不重复付费。
- 只有用户明确要求、变更范围实质变化、出现新的关键风险或首次材料存在重大遗漏时，才用
  `--force --trigger <user-request|scope-changed|new-risk|materials-omitted> --reason ...`
  再调用一次；必须换新材料。
- 执行方必须基于证据回应每条 blocking/major：修复，或用具体反证说明不成立并保留分歧。
- 不采纳意见时给出反证；没有新证据不重复同一轮复核。不得为了通过而降低断言、删除测试
  或放宽门禁。

### 记录与边界

- 成功结果统一写入 `cognition_calls`（历史任务状态中的 `sol_reviews` / `sol_advisories` 继续
  只读兼容，不迁移、不改写、不重复计数），原始输出存 `cognition-consult/`、`cognition-plans/` 或
  `cognition-reviews/`，审计统一写 `cognition-audit.jsonl`，只记脱敏元数据。
- 失败把真实错误码与材料摘要写入 `cognition_calls`（`status=failed`）与 `history`；不自动
  重试、不换 Provider、不派原生 Reviewer 顶替。
- 认知结论不改变阶段判定；阶段只由 `evaluate` 按合同、工作项、证据与门禁计算。

## 3. G4：已退役的历史机制（v8.0.0）

**G4 原生只读复核已正式退役。** 以下内容只用于解释历史状态与只读数据，不是当前操作指南：

- **已删除**：`g4_attest.py`、`g4_shadow.py`、`test_execution.py`、
  `native_agent.py review/review-context/capability`、`copilot-native-review/*` 信封、
  review attempt 创建与 ingest、`AUTO-G4*` 派生、`host_capability_missing` /
  `profile_source_not_builtin` 等宿主能力拒绝码。
- **不再产生**：`g4_eligible`、`g4_source`、`candidate_g4`、G4 PASS、G4 BLOCKED。
  `native_review_retired` 仍保留，用于拒绝历史复核信封。
- **历史只读**：旧状态里的 `native_reviews` / `review_attempts` / `g4_attestations` 与
  `authority=authoritative` 字段原样保留，只能通过 `legacy-status` 或状态文件只读查看；
  不参与新任务准入、派生或收口，也不得批量改写为 PASS。
- **安全职责已转移**：原 G4 的“未闭合 blocking 阻断收口”现在由 G2 Finding 台账门禁在
  任何风险等级执行；`major` 只在 `risk=high` 时阻断，普通任务按提醒处理。高风险仍受授权、
  数据/凭据/部署边界等独立安全约束。
- **历史验收记录**：v6.5/v6.6.1 切换清单随 G4 退役于 2026-11-17 删除；需要追溯时查仓库
  Git 历史。历史记录不作为现行操作指南。

## 4. 缺陷分级与处置

- `blocking`：会导致错误结果、数据损坏、安全问题或无法交付；必须修复后才能收口（任何风险等级）。
- `major`：实质缺陷或高风险回归；修复并用定向测试证明。门禁上只在 `risk=high` 时阻断收口，
  普通任务按提醒处理。
- `minor` / `info`：可维护性、可读性、建议；记录但不阻断收口，不为它们重开完整复核。
- 执行方必须基于证据回应每条 blocking/major：修复、或用具体反证说明不成立并保留分歧。
- 不采纳意见时给出反证；没有新证据不重复同一轮复核。
- 不得为了通过而降低断言、删除测试或放宽门禁。

## 5. 与状态机的关系

- 正式 REVIEW 与认知复核都不改变阶段判定；阶段只由 `evaluate` 按合同、工作项、证据与
  门禁计算。
- 门禁是 G1 工作项终态、G2 合同/条件/证据/台账、G3 无停摆；G4 已退役，不再参与。
- `closure --close` 在任一门失败时拒绝并列出原因码；不存在“强制收口”开关。

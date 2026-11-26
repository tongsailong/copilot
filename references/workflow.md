# workflow：命令矩阵与操作流程

本文是 Copilot 的**操作层**权威说明：命令、参数、退出码与标准回合流程。
语义（阶段判定、派生规则、门禁细节）见 `runtime.md`、`review.md`、`agent-bridge.md`。
`cognition-consult` / `cognition-plan` / `cognition-review` 是显式模型调用入口；生命周期命令
（`start` / `plan` / `record` / `rerun` / `criterion` / `unblock` / `reopen` / `closure` /
`drive`）在有效策略为 `auto` 时会顺带执行至多 1 条 recommended 调度义务（1 次请求），
策略默认随任务生效（`start` 写入 `auto`，`--cognition off` 可让整条任务离线）。纯记录/报告/
合同命令默认 `advise`：只物化义务并打印命令，只有命令行显式 `auto` 才付费执行——所以
“不调用模型”对读命令、纯记录命令默认行为与测试进程成立。其余工作只读写本地任务状态文件。
每次认知调用只发一次请求，不自动重试、不自动二审；CONSULT / PLAN 失败只记录并降级，
不阻断工程推进。

## 1. 状态文件

- 默认路径由调用方指定（`--state`）；**任务状态应放在
  `<workspace>/.copilot-tasks/<task>/state.json`**：Stop / SessionStart / UserPromptSubmit
  三个钩子只扫描该目录。放在别处时任务仍可手工推进，但不会被自动续跑、恢复或回合提醒
  发现（`start` 对目录外的状态文件给出提示）。
- 同名辅助文件：`<state-basename>.dispatch.json`（原生派发）、`evidence/`（命令采集输出）、
  `.state.json.lock`（并发锁，短生命周期，文件名随状态文件名）。
- 单文件 JSON，`schema=copilot-governance/v1`、`version=1`；写入为
  “临时文件 + fsync + `os.replace`”，并用单调 `save_seq` 做 CAS 并发校验。
- 状态已存在时 `start` 会拒绝，避免覆盖进行中的任务；续跑直接使用同一文件。

### 启动开销

- 命令一律用 `python3 -B`：`-B` 只禁止**写入** `.pyc`，不禁止读取。运行时由
  `sync_runtime.py --apply` 预编译字节码；源码仓库可执行 `python3 -m compileall -q scripts`。
  没有有效字节码时每条命令要重编译约 17k 行源码（约 +130ms）。
- 同一命令内只扫描工作区一次：`status`/`drive`/`closure-check` 复用同一份代码快照与
  `git_root` 结果；这不是跨命令缓存，下一次命令仍重新计算（漂移检测不受影响）。

## 2. `copilot_dispatch.py`：任务与证据

```bash
python3 -B scripts/copilot_dispatch.py <子命令> --state <state.json> [参数]
```

生命周期命令（`start` / `plan` / `record` / `rerun` / `criterion` / `unblock` / `reopen` /
`closure`）与 `copilot_auto.py drive` 接受 `--cognition auto|advise|off`：做一次有界调度步，
`auto` 下**自动发出真实请求**，最多执行 1 条义务、1 次请求；`optional` 级只提示；读命令永不
执行。**省略 `--cognition` 时策略随任务生效**：命令行显式 > `COPILOT_COGNITION` > 任务快照
（`start` 写入，默认 `auto`）> 命令默认，因此 `start` 之后无需逐条重复 `--cognition auto`，
`start --cognition off` 会让整条任务（含 `drive`）保持离线。纯记录/报告/合同命令（`judgment` /
`evidence` / `report` / `accept` / `decide` / `block` / `finding` / `contract`）默认 `advise`：
回填后只物化义务、给出可粘贴命令，不隐式调用模型；确需顺带执行时必须在该条命令上显式写
`--cognition auto`。`COPILOT_COGNITION_AUTO=off` 可整体关闭自动执行，`on` 只放开测试进程的
执行闸门，不改变策略级别。

| 子命令 | 用途 | 关键参数 |
|---|---|---|
| `start` | 创建任务合同 | `--goal`、`--intent` / `--intent-file`（用户原始任务意图，认知分级的第一输入；省略时按目标与合同分类，无法可靠分类至少 T1）、`--contract-file`、`--criteria-file`、`--task-kind <kind>`（默认 `standard`，简单类型见 `trivial`/`mechanical`/`docs`/`format`）、`--trigger <name>`（可重复，声明 security/permissions/core-logic/...）、`--test-policy`/`--test-policy-file`（JSON：`required`/`commands`/`report`/`artifacts`/`require_*`）、`--risk low\|medium\|high`（历史 `normal` 等价 `medium`）、`--backend legacy\|native`、`--task-id`、`--mode`、`--note`、`--workspace`（省略取状态文件所在目录的 git 根）；输出认知等级 T0–T3 与窗口；`risk=high` 不再强制 native |
| `intent` | 补录/更正用户原始任务意图（默认拒绝覆盖，`--replace` 才重写并留 history）；输出按新意图计算的等级与窗口 | `--text` 或 `--file`、`--replace` |
| `plan` | 登记 ZCode 本地计划 | `--plan <plan.json>`（或 stdin）、`--source` |
| `status` / `next` | 查看阶段、工作项、门禁、下一步与认知建议 | `--json` |
| `record` | 回填工作项结果 | `--id`、`--status`、`--result`、`--evidence E1`（可重复） |
| `rerun` | 把已终结工作项重开为 pending（修复后重新验证） | `--id`、`--reason`；旧结果保留在 `rerun_history` |
| `criterion` | 核销完成条件 | `--id C1`、`--status pending\|met\|not_met`、`--evidence`（`met` 必填） |
| `judgment` | 记录自我判断（仅审计） | `--status pass\|partial\|blocked`、`--summary`、`--evidence` |
| `evidence` | 登记 / 归档 / 列出证据 | `--add`、`--file`、`--text`、`--capture-cmd`、`--drop --id`、`--list`、`--next-id` |
| `report` | 登记执行报告，或接入 verify 结构化质量报告 | `--summary`（可与下一项同用）、`--verify-report <报告文件>`、`--blocked`、`--reason`；PWW 自动登记用户接受条件 |
| `accept` | 登记用户对 PWW 的明确接受 | `--report <report_id>`（仅一份可省）、`--accept W1,R1`、`--by user`（必填）、`--quote "<用户原话>"`（必填）、`--note`；部分接受保持 pending |
| `quality` | 查看质量结论与接受状态 | `--json` |
| `decide` | 记录用户/任务决策 | `--text`、`--source`；**不能替代 PWW 接受** |
| `block` / `unblock` | 记录/解除真实阻塞 | `block --reason ... --needs ... (--work-item A2 [--work-item A3] [--criterion C2] | --scope task [--force])`；`unblock --reason ... [--block-id B...] [--by user --quote "<原话>"]`（多条未解除时必须指定）；解除后被冻结工作项自动恢复 `pending`。仍有可执行项时任务级 `--scope task` 被拒绝，必须显式 `--force`，状态记录 `ready_at_creation`/`forced_scope`；`--by user` 必须带原话，记录 `resolved_by` 与 `authorization_source=caller-declared` |
| `closure-check` | 只读检查 G1–G3 | `--json` |
| `review-admission` | 认知生命周期建议的单一确定性入口（只读、不写状态、不是门禁）：先算原始意图、任务类型、T0–T3 等级、窗口与已有认知覆盖，再给出 `advice.action`（`none`/`reuse`/`consult`/`plan`/`review`）、建议模式、阶段与命令 | `--trigger security`（可重复）、`--reason`、`--user-request`、`--review-request`、`--json`；返回 `decision`、`advice`、`task_kind`、`complexity`（含 `tier`/`tier_reason`/`windows`/`intent`）、`recommended_action`/`recommended_mode`/`recommended_stage`、`cognition_calls_total` |
| `cognition-consult` | 认知判断入口：材料 + 问题 → 一次请求 → 目标理解/问题本质/路线与取舍/风险/调查方向/再咨询条件；建议写入统一 `cognition_calls`（`mode=CONSULT`），不入门禁；失败只登记并降级 | `--materials <file>`（调度执行时可省略）、`--question`（必填，`--obligation` 时回落到义务问题）、`--stage <understanding\|planning\|development\|stage\|closure>`、`--obligation SO<n>`（按调度义务执行，材料由状态确定性生成）、`--lock-timeout <秒>`、`--knowledge-file`、`--dry-run`、`--list`、`--json`、`--timeout`、`--max-output-tokens`、`--audit-log`（默认 `cognition-audit.jsonl`）、`--output-dir`、`--ignore-backoff`（默认在上游退避窗口内不发请求；仅在上游已确认恢复时显式使用，仍然只发一次、不重试）；模型固定 `gpt-6.2-astra`，没有 `--model` 开关；再次调用：`--force --trigger <user-request\|scope-changed\|new-risk\|materials-omitted\|strategy-failed\|major-uncertainty\|stage-result> --reason` |
| `cognition-plan` | 认知调查后规划判断：方案比较、取舍、优先级、阶段目标与验收关注点；记录写入统一 `cognition_calls`（`mode=PLAN`） | 参数同 `cognition-consult`；省略 `--stage` 时默认 `planning` |
| ~~`sol-consult`~~ | v9.10.0 已移除（不保留跳转表，解析阶段即拒绝）；改用 `cognition-consult`，`--sol` 也改用 `--cognition` | — |
| `cognition-accept` | 回填主代理对认知建议的采纳结论（事实记录，不改变阶段、G1–G3 或授权）。v2 记录含实际决策、理由、modified 差异、证据引用与 `advice_ref`；缺项只给非阻断提示。`--historical` 只用于**已收口**任务的历史复核：`--status pending` 必须配 `--reason`（可加 `--reason-code`），重复执行幂等、不覆盖既有已决结论、不把已决结论降级为 `pending`；不因代码已改、测试通过或任务 PASS 推断采纳 | `--call-id SC...`、`--status accepted\|modified\|rejected\|pending`、`--decision`、`--rationale`（省略回落 `--reason`）、`--delta`、`--evidence-ref E1,E3`（可重复/逗号分隔，指向本任务已登记证据）、`--source-ref`（报告章节/文件路径，只定位不核验）、`--evidence-note`、`--note`、`--reason`、`--reason-code`、`--historical`、`--decided-by`（默认 `zcode`）、`--json` |
| `cognition-schedule` | 认知生命周期调度视图（只读）：义务清单、漏调、终态处置与所属周期、下一条可执行项与可直接粘贴的命令；`--execute` 时有界执行 1 条 `recommended` 义务（1 次请求，失败只记录）；同时显示当前上游退避窗口（窗口内不发起新请求） | `--state`、`--execute`、`--json`、`--cognition auto\|advise\|off`；不带 `--execute` 不写状态 |
| `cognition-stats` | 认知调度观测（显式分母）：`expected`/`triggered`/`invoked`/`succeeded`/`failed`/`reused`/`deferred`/`skipped`/`missed` 分开统计并区分 auto/manual，成功率/漏调率用显式分母（`success_rate`、`missed_rate`，分母 0 记 null），含 T0–T3 等级、A/B/C/E 窗口（`by_window`）与 CONSULT/PLAN/REVIEW 模式（`by_mode`）分布、任务类型覆盖（`totals.task_kind_tasks`）、事件分布、采纳分布、义务终态处置与周期分布、tokens（输入/输出/reasoning/总）与延迟、`by_model`（请求数/完成/失败/Token/p50/p95/重复调用）与 `reuse`（命中数、按模型）；`invoked` 只认带 `call_id` 的真实请求 | `--state` 或 `--workspace <根>`（聚合 `.copilot-tasks/*/state.json`）、`--json` |
| `agent-policy` | Agent 调度建议：主代理默认，Explore/general-purpose 按需（只读、不派发）。判定顺序：子代理不可用 → 主代理；写入冲突 → 主代理；`--research-only` → Explore（绝不交给全工具代理）；独立交付物需并发且有收益才建议 general-purpose；其余 → 主代理 | `--needs-independent-context`、`--parallel`、`--independent-deliverable`、`--write-conflict`、`--saves-time`、`--research-only`、`--subagent-unavailable`、`--json` |
| `finding` | 查看 / 更新 Finding 台账（不触发复核） | `--list`；`--id F1 --status open\|fixed\|verified\|closed\|regressed\|dropped --evidence E1[,E2] --note`；`fixed`/`verified` 必须有证据，多个证据可用逗号分隔；回填成功后按续跑判定返回 10 |
| `review-metrics` | 统计统一认知调用流（`cognition.consult_total` / `cognition.plan_total` / `cognition.failed` / `cognition.modes`）、准入门槛、停止标记与 Finding 计数 | `--state`；历史 G4 数据标记 `g4.retired` |
| `closure` | 收口：G1–G3 通过与**最终工程验证完成**（`closure_readiness`：工作项终态、条件核销、证据、合同必要测试的执行痕迹、finding 台账）都必须满足；验证有缺口时失败关闭（退出码 1）；补完后同一条命令先做收口 CONSULT（如建议）、再走 G1–G3 收口；`cognition-accept` 只是可选的采纳审计记录，不登记不影响收口 | `--close`、`--by`、`--cognition auto\|advise\|off` |
| `reopen` | 用户授权后重开 | `--reason`（必填，写用户原话/要求）、`--authorized-by user`（必填；调用方自报声明，不是凭证）；`reopen_history` 标注 `authorization_source=caller-declared` |
| `handoff` | 只读列出生效交付清单：分支/HEAD/上游、已暂存、未暂存、未跟踪分类 | `--state`（可选）、`--cwd`、`--limit`、`--json`；不提交、不删除、不改 `.gitignore` |
| `contract` | 查看/修订合同与条件 | `--show`、`--amend --contract-file/--criteria-file`、`--test-policy`/`--test-policy-file`（省略则保留原策略）、`--authorized-by user` + `--authorization-note "用户要求"`（破坏性修订必需）；BLOCKED 时也可修订；登记源文件摘要，文件被改动时 `status` 给出非阻断提醒。**破坏性修订**指待核销条件净减少（删掉未满足条件）或放松 `test_policy`，没有用户授权或未写明用户要求时 fail-closed；改写措辞/拆分/合并但只要不减少待核销条数不需要授权。合同历史记录 `authorized_by`、`authorization_source=caller-declared`、授权说明、新增/删除的未核销条数与被删除条件及旧指纹。`--authorized-by` 是调用方自报声明，不是可验证凭证：审计时须以用户原话/授权说明为准 |
| `authorize-review` | 记录用户对正式 REVIEW 的授权 | `--authorized-by user`（必填，调用方声明）、`--reason`、`--scope` |
| `formal-review` | 登记正式 REVIEW 结论或修订 | 新结论：`--verdict`、`--summary`；非实质修订：`--amend <review_id> --reason`；实质变化：`--supersedes <review_id> --reason`；均支持 `--review-ref`/`--reviewer` |
| `legacy-status` | 只读查看 v1/v2 SOL 历史状态与台账 | `--state`、`--audit-log`、`--ledger`、`--json` |

### 计划 JSON

```json
{
  "summary": "一句话计划",
  "actions": [
    {"id": "A1", "title": "标题", "goal": "要达成的结果",
     "expected_evidence": "期望证据", "depends_on": [], "constraints": "可选"}
  ]
}
```

- `id` 只允许字母数字与 `.` `_` `-`；单次最多 64 项；必须含 `title`/`goal`/`expected_evidence`。
- `depends_on` 只引用同计划内工作项；环依赖被拒绝；`assignee` 只能是 `zcode`。
- 计划的来源与摘要被记录；后续原生派发按 `plan_id` / `plan_sha256` 绑定，计划被改动会导致
  `plan_drift`。

### 证据

- **先登记再引用**：`record` / `criterion` / `judgment` 的 `--evidence` 必须引用本任务
  已登记、未归档的证据 id（`E1`、`E2`…），否则拒绝。
- `--file`：登记普通文件，记录绝对路径、大小与 SHA-256。
- `--text`：登记内联文本（小片段说明），同样计算摘要。
- `--capture-cmd "<shell 命令>"`：经 `bridge_evidence.py` 采集，输出写入
  `<state 目录>/evidence/<Eid>.md`，复用脱敏、截断标记与清单；`kind=capture`。
  与 `--file`/`--text` 互斥。该 id 已登记或已归档时拒绝：采集会先覆盖 `<Eid>.md`
  再校验摘要，重采会改写已登记/归档证据；归档不删除文件，所以只能用新的 id 采集。
- 证据不可替换：同 id 且摘要相同是幂等（返回已有记录，CLI 明确提示“已存在且摘要一致、
  未新增”）；同 id 摘要不同直接拒绝；撤回用 `--drop --id --reason`，历史记录保留。
- 文件型证据在 G2 收口前会被重新读取并与登记摘要比对：
  `evidence_tampered`（内容变化）、`evidence_file_missing`（文件丢失）都失败关闭。
- **证据卫生提醒（非阻断）**：`closure-check` 会提示重复登记同一文件、不同 id 内容完全相同、
  证据文件位于 `/tmp`/`/var/tmp`/`/dev/shm`、以及 `<任务>/evidence/` 下有未登记文件。
  这些提醒不进入门禁原因，由 ZCode 决定补 note、归档或保留。

### 只读交付清单（`handoff`）

`handoff` 汇总“当前任务 + 当前工作区”能核实的状态，供收口交接使用：

```bash
python3 -B scripts/copilot_dispatch.py handoff --state .copilot-tasks/<task>/state.json
python3 -B scripts/copilot_dispatch.py handoff --state ... --json
```

- 仓库信息：分支、HEAD、上游、落后/领先提交数。
- 变更分类：已暂存（下次提交会包含）、已修改未暂存、未跟踪文件按类别分组——
  治理记录（`.copilot-tasks/...`）、任务证据（已登记路径）、验收产物
  （`.apk`/`.png`/`.log` 等）、未分类（需 ZCode/用户判断）。
- 任务信息：阶段、风险、收口人、证据统计、缺失的证据文件与证据提醒。
- **只读边界**：命令只运行 `git --no-optional-locks` 查询，不 `add`、不 `commit`、
  不删除、不改写 `.gitignore`，也不处理忽略项。清单不构成提交授权。

### PWW 质量报告接入与用户接受

verify 报告末尾的 `<!-- copilot-verify-result/v1 -->` JSON 块是唯一机器可读入口；正文里的
`PASS_WITH_WARNINGS` 文本不触发治理。

```bash
python3 -B scripts/copilot_dispatch.py report --state S --verify-report report.md \
  [--workspace <repo>]
python3 -B scripts/copilot_dispatch.py quality --state S
python3 -B scripts/copilot_dispatch.py accept --state S --report VR-... \
  --accept W1,W2,R1 --by user --quote "用户接受的原话"
```

- 接入时校验 schema、verdict、W/R id、`checks`、`unverified`、`subject`、替代字段与
  `acceptance_status`；缺字段、未知字段、PWW 带未验证项/blocking、报告自报已接受、合同
  条件未核销都失败关闭。
- 存在生效报告时，新报告必须用 `supersedes_report_id` 指向它，且被替代目标必须是当前
  生效报告；`pww_supersede_missing` / `pww_supersede_invalid_target` 拒绝。
- 旧报告的每个未解决 W/R 必须：仍出现在新报告（继承，PWW 重新接受），或在
  `resolved_warning_ids` / `resolved_residual_risk_ids` 声明解决。解决声明只在
  `subject_digest` 与警告来源报告一致时生效；PASS 不能继承未解决项；部分覆盖拒绝
  （`pww_supersede_coverage_incomplete` / `pww_supersede_subject_mismatch`）。旧接受记录
  保留但不继承，新 PWW 重新 pending；旧报告已由用户接受的 W/R 视为已解决，不要求新报告
  继承（新报告出现的新 W/R 仍需重新接受）。
- PWW 自动创建**一个**用户接受完成条件（`C<n>`，`acceptance` 元数据），绑定
  `report_sha256`、W/R 集合、目标摘要、代码快照、合同/条件摘要与报告内容摘要。同一报告
  重复接入幂等；重复接入已替代的旧报告会被拒绝。重新验收必须使用新的 `report_id` 与
  `generated_at`。FAIL/PARTIAL/BLOCKED 报告不创建接受条件；其未解决项仍沿替代链保留。
- **代码类报告**：声明 `commit`/`files`/`diff_sha256`/`code_scope_sha256` 时，verify 必须先运行
  `code_scope_capture.py --workspace <repo> --state S --exclude <报告文件>`，把 `commit`、
  `code_scope_sha256` 与 `files_paths`（字符串路径数组，重命名含新旧路径）写入
  `subject.files`；`dirty` 可选，给出时必须一致。工作区有变更时 `files` 必须覆盖全部
  变更路径，干净时可省略。若给出 `diff_sha256`，必须与 `code_scope_sha256` 相同（同一
  快照别名）。报告文件必须放在任务目录或工作区外；放在工作区其他位置会被拒绝。`subject.code_scope_sha256` 是报告生成时声明的摘要并进入
  `report_sha256`；接入时用 `--workspace` 重算，生成后/接入前代码变化即摘要不一致拒绝。
  Copilot 保存的 `code_scope` 标注 `capture_source=copilot-ingest-recompute`，只是接入时
  重算快照，不冒充报告生成时刻。G2 收口前再重算，漂移报 `quality_code_scope_drift`。
- **工作区身份**：`start --workspace` 绑定任务工作区；未显式绑定时由 native execution
  binding、合同来源、任务证据所属仓库、任务目录唯一派生。`report --workspace` 必须与
  绑定一致（realpath/git root 归一），
  无法确认或指向其他仓库时 `quality_code_scope_workspace_unbound` / `_mismatch` 失败关闭。
  已登记证据文件不从快照排除——证据位于工作区内且发生变更时同样必须声明。纯文档报告
  不要求工作区或快照。
- G2 独立检查最新生效报告：未接受 → `pww_acceptance_pending`；部分接受 →
  `pww_acceptance_partial`；目标/合同/条件/工作区变化 → `pww_acceptance_stale`；
  PASS 的目标/合同/条件/工作区绑定变化 → `quality_binding_stale`；代码漂移 →
  `quality_code_scope_drift`；替代链不合法/未解决项残留 → `pww_supersede_*` /
  `pww_superseded_unresolved`；PARTIAL/BLOCKED/FAIL → `quality_verdict_not_pass`。
  合同修订删掉条件也不能绕过。
- `accept` 只接受 `--by user`，必须带用户原话；执行方、子代理、verify 与 Copilot 不得代为
  接受，`criterion --status met` 不能核销接受条件。接受事件与每次部分接受的原话记录在
  `acceptance_history`，完整接受后以 inline 证据绑定到条件（含 code_scope_sha256）；G2 会
  核对接受记录、历史、证据与条件状态一致，缺少任一环即拒绝。`accept` 记录的是
  user-declared 接受，CLI 不能证明用户来源（见 DECISIONS 49）。
- 任务 `reopen` 后旧 PWW 接受立即失效（`pww_reverify_required`），必须重新验收并再次取得
  用户明确接受；不能拿旧接受直接再收口。
- 收口保留原始结论：`closure.verdict=pass_with_warnings`、`closure.quality` 记录
  `raw_verdict`/`report_id`/接受状态/已接受 id；未接入质量报告的历史任务显示
  `quality.verdict=unrecorded`，**不伪造**已接受。
- 高风险任务：接受会改变条件状态与最终证据摘要，建议在最终验收前完成接受登记。

## 3. `copilot_auto.py`：驱动与回填

```bash
python3 -B scripts/copilot_auto.py drive  --state <state.json> [--dispatch-out <path>] [--max-actions N]
python3 -B scripts/copilot_auto.py ingest --state <state.json> [--result-file f]
```

### `drive`

一次调用在本地循环直到出现以下结果之一：

- `PASS` → 自动 `close_task`，退出码 0；
- `RUNNING` → 打印工作单（goal / constraints / expected_evidence / 状态回填命令），
  原生后端且工作项属于计划时写出 `dispatch.json`，退出码 10；
- `BLOCKED` → 打印状态；所有剩余工作真实阻塞时退出码 11，`CLOSED` → 退出码 0；
- `PARTIAL` → 本地派生工作项并继续循环，不等待任何外部判断；
- 内部错误（派生失败、信封被拒、连续 32 步未收敛）→ 退出码 12，状态保留。

`--max-actions N` 只控制一次工作单输出几项，不是任务上限。

### 续跑判定与回填命令

`record` / `rerun` / `criterion` / `report` / `block` / `unblock` 写入后都会重新求值并打印
续跑判定：

- 仍有可执行/可派生工作（或已 PASS 待收口）→ 退出码 10，打印下一步命令；
- 已收口或所有剩余工作真实阻塞 → 退出码 0；
- **报告是进度记录，不是任务终点**：`report` 不再以 0 表示"任务结束"；主代理必须按
  `continuation` 继续，不能据一次报告结束回合。

### `ingest`

- `--result-file`：`copilot-native-result/v2` 执行信封；校验绑定与证据后回填工作项状态，
  重复回填同一 `result_sha256` 是幂等成功。
- 复核不再经 `ingest`：原生复核信封已随 G4 退役，`--review-file` 参数已移除；独立复核
  使用 `cognition-review`（SOL 零工具单轮复核，advisory）。
- 被拒绝时退出码 12，错误码写在 stderr，状态不被部分写入。

## 4. 标准回合

```bash
# 1. 建立合同（--intent 记录用户原始要求，是认知分级的第一输入）
python3 -B scripts/copilot_dispatch.py start --state T/state.json \
  --goal "目标" --intent "用户原始要求的原文或等价摘要" \
  --contract-file contract.md --risk medium \
  --workspace /path/to/task-repo

# 2. 登记 ZCode 计划
python3 -B scripts/copilot_dispatch.py plan --state T/state.json --plan plan.json

# 3. 驱动 / 执行
python3 -B scripts/copilot_auto.py drive --state T/state.json    # 退出码 10 + 工作单

# 4. 执行并登记证据
python3 -B scripts/copilot_dispatch.py evidence --state T/state.json --add \
  --capture-cmd "python3 -B -m unittest discover -s tests -p 'test_*.py'"

# 5. 回填工作项与条件
python3 -B scripts/copilot_dispatch.py record --state T/state.json --id A1 \
  --status done --result "实际结果" --evidence E1
python3 -B scripts/copilot_dispatch.py criterion --state T/state.json --id C1 \
  --status met --evidence E1

# 6. 收口
python3 -B scripts/copilot_auto.py drive --state T/state.json    # PASS → 0
```

PARTIAL 时不需要额外命令：直接再次 `drive`，由本地派生工作项。

收口顺序是**最终工程验证 → 收口 CONSULT（如建议）→ G1–G3 收口**：`drive` 在 PASS 但验证
有缺口（例如合同 `test_policy.required=true` 却没有 `--capture-cmd` 证据）时打印缺口并
返回 10，不执行认知、不收口；补齐测试与证据后再 `drive`（或 `closure --close`）即自动执行
一次收口 CONSULT（任务为 T2/T3 且有实质进展时），随后直接收口。`cognition-accept` 只是可选的
采纳审计记录：不登记、甚至明确 rejected，都不阻止收口。手动
`cognition-consult --stage closure` 同样被缺口与“摘要不符”拒绝。

### 认知失败分类与上游退避窗口

一次认知调用失败时，Copilot 保留原始 HTTP 状态、上游错误码与 `Retry-After`（数值化后的
秒数 + 原始文本 `retry_after_raw`），并归入十类 `error_class`（写入 `cognition-audit.jsonl` 与
`cognition_calls`）：

| `error_class` | 典型来源 | 处理 |
|---|---|---|
| `upstream_overloaded` | 502/503 + `server_is_overloaded` / overloaded；529 | 登记失败、进入退避窗口，**不自动重试、不换 Provider** |
| `upstream_stream_timeout` | 408 / `request_timeout` / 上游流在完成前断开 | 同上；408 不是客户端超时 |
| `upstream_credential_cooling` | 本地 CPA 在凭据冷却期直接回 503 `auth_unavailable` | 同上；**不记作凭据失效或认证失败** |
| `upstream_auth` | 401/403 | 记录真实认证错误，不进入退避窗口 |
| `upstream_rate_limit` | 429 配额/限流 | 走既有配额错误处理 |
| `upstream_server_error` | 5xx | 进入退避窗口 |
| `upstream_client_error` | 400/404/405/409/422 等请求侧错误 | 记录，不进入退避窗口 |
| `client_timeout` | 本地 socket 读超时 | 记录；`send_observed=unknown`（不假设上游是否已处理） |
| `network_error` / `response_incomplete` | 传输异常 / 响应体不可解析、流被截断、`status=incomplete`、空文本 | 记录为失败，**绝不登记为成功认知** |

退避窗口（`state["model_backoff"]`，schema `copilot-model-backoff/v2`；键为
`provider + model + error_class` 的 `codex:<model>` 槽位，旧 `state["sol_backoff"]`
单例只读兼容且不冻结 Astra）：

- **服务端 `Retry-After` 是最短等待时间**：有效值（秒、毫秒、HTTP-date）原样遵守，
  **不被** 1800 秒上限、也不被 `COPILOT_COGNITION_BACKOFF_MAX` 缩短。HTTP-date 按 UTC（缺失时区
  按 GMT）换算为剩余秒数；已过期或无法解析时不用它，退回本地策略。
- **本地策略才有上限**：没有有效服务端值时按 `error_class` 取默认窗口（60 秒），
  该值受 `COPILOT_COGNITION_BACKOFF_MAX`（只能下调，不能调高或关闭）与 1800 秒上限约束。
- 窗口记录 `until`（绝对恢复时间）、`seconds`、`reason`（`retry-after` / `error-class`）、
  原始 `retry_after_raw` 与 `call_id`，失败记录同时保留同样字段供审计追溯。
- **不是门禁**：不改变阶段、条件、G1–G3，也不派生工作项；窗口期内 `cognition-schedule --execute`
  与自动调度只登记义务 deferral，不发请求。
- **按模型隔离**：认知过载不冻结 Astra，Astra 过载不阻塞显式认知调用；`Retry-After` 逐模型
  独立遵守；成功调用只清除本模型的窗口。
- 成功调用自动清除；手动 `cognition-consult` / `cognition-plan` / `cognition-review` 在窗口内直接拒绝并说明剩余时间，
  确需立即重试必须在确认上游已恢复后显式加 `--ignore-backoff`（仍然只发一次、不重试）。
- **跨阶段、跨进程、跨会话一致**：窗口写在任务 `state.json`，不依赖内存、会话绑定或锁；
  startup 失败后 plan 与 closure 在窗口内只登记 deferral，新会话恢复、`--resume` 或人工执行认知命令同样遵守，窗口到期后自动恢复既有调度频率（不降低 startup / plan / closure 频率）。
- 失败只登记降级：CONSULT / PLAN 失败退出 0，主代理按既有授权继续工程推进；REVIEW 失败返回
  真实非零退出码。

## 5. 正式 REVIEW 与已退役的 G4

```bash
# 用户明确要求时才记录授权；同一合同内持续有效
python3 -B scripts/copilot_dispatch.py authorize-review --state T/state.json \
  --authorized-by user --reason "用户要求在交付前正式复核" --scope "当前任务合同"

# 复核完成后登记结论（治理记录，不是门禁）
python3 -B scripts/copilot_dispatch.py formal-review --state T/state.json \
  --verdict pass --summary "结论摘要" --review-ref <复核产物> --reviewer native

# 非实质修订：证据编号/引用/文字口径，目标摘要必须未变
python3 -B scripts/copilot_dispatch.py formal-review --state T/state.json \
  --amend FR<review_id> --review-ref <新引用> --reason "仅更正引用，结论未变"

# 实质变化：证据/目标摘要变化后重新验证，旧记录保留并标为 superseded
python3 -B scripts/copilot_dispatch.py formal-review --state T/state.json \
  --verdict pass --summary "新证据下复核通过" \
  --supersedes FR<review_id> --reason "证据实质变化"
```

- 未记录授权时 `formal-review` 会被拒绝；合同修订后需要重新授权。`--authorized-by` 是
  调用方对用户要求的声明，不是可验证凭证。这不是普通开发的步骤。
- `--amend` 不生成新 PASS；目标摘要变化时拒绝，必须用 `--supersedes` 登记新结论。
  原始记录、授权快照和 verdict 不被覆盖；`review-metrics` 分开统计 amendment 与
  superseded。
- BLOCKED 下允许登记已授权的正式 REVIEW 及其 amendment；不会解除阻塞，也不能替代
  `unblock` 或收口。`plan`、`decide`、`evidence --drop` 在 BLOCKED 下仍被拒绝。
- G4 已退役：`risk=high` 不再需要原生复核信封，也不要求 `--backend native`；收口只检查
  G1–G3。历史 G4 记录只读保留（见 `review.md` 第 3 节）。

### 历史 G4 流程（仅供追溯，不再执行）

v7 及更早的高风险任务通过 `review-context --create-attempt`、`native_agent.py review`、
`capability` 与 `copilot_auto.py ingest --review-file` 完成原生只读复核；这些命令与
`g4_attest`/`g4_shadow`/`test_execution` 已在 v8.0.0 删除。历史状态中的
`native_reviews` / `review_attempts` / `g4_attestations` 只读保留，不参与当前门禁。
现在需要独立意见时，按第 2 节用 `cognition-review` 执行一次；它的 findings 进入 Finding 台账，
由主代理修复并用定向测试验证。

## 6. 历史只读

```bash
python3 -B scripts/copilot_dispatch.py legacy-status --state old-state.json --json
python3 -B scripts/copilot_dispatch.py legacy-status --audit-log .audit/copilot.jsonl
python3 -B scripts/copilot_dispatch.py legacy-status --ledger review-ledger.json
```

- v1/v2 状态不会被 v6 驱动；`legacy-status` 不写文件、不续跑、不改写历史。
- 历史调用记录、REVIEW 裁决、证据与 SHA-256 原样保留，仅标注为历史。

## 7. 只读效能观测（`efficiency_report.py`）

```bash
python3 -B scripts/efficiency_report.py --workspace /path/to/ws [--workspace /path/to/ws2] \
  --repo-root /media/vdb/projects/copilot \
  --recall-events ~/.zcode/copilot-runs/context-recall/events.jsonl \
  --host-db ~/.zcode/cli/db/db.sqlite --days 7 [--json]
```

- 数据来源全部是既有事实：`.copilot-tasks/*/state.json` 台账、`SKILL.md`/`AGENTS.md` 字节数、
  召回事件 JSONL、可选宿主 `db.sqlite`（只读打开）。
- 每个指标带 `basis`：`measured`（直接计数）、`proxy`（近似口径）、`unavailable`（事实源
  没有该数据）。不补零、不推断；`unavailable` 不是 0。
- 不写文件、不调用模型、不联网、不改任务状态；结果用于前后对照与排查，**不作为门禁**。
- 退出码：0 正常；2 参数错误。缺工作区或缺库只降级为 `unavailable`，不失败。

## 8. 退出码与常见错误

| 场景 | 退出码 |
|---|---|
| 命令成功 / `drive` 已收口 / 已收口或全部剩余工作真实阻塞时的回填命令 | 0 |
| `drive` 有待执行工作单；回填/报告后仍有下一步（`EXIT_CONTINUE`） | 10 |
| `drive` 等待用户（所有剩余工作真实阻塞） | 11 |
| 校验失败、信封被拒、内部错误 | 12 |
| 参数错误 | 2 |

常见拒绝码（`governance` / `native_bridge`）：

| 错误码 | 含义与处理 |
|---|---|
| `plan_drift` / `plan_mismatch` | 计划登记后被改动；恢复原计划或重新 `plan` 后再派发 |
| `result_sha_mismatch` | 信封内容与 `result_sha256` 不符；重新生成信封 |
| `evidence_tampered` / `evidence_sha_changed` | 证据文件变化或引用摘要不符；用新证据登记，不覆盖旧记录 |
| `evidence_foreign` | 证据属于其他任务；使用本任务证据 |
| `native_review_retired` | 使用了已退役的原生复核信封；改用 `cognition-review` |
| `schema_retired` | 使用了 v1 信封；改成 v2 |
| CAS 并发冲突（无单独错误码） | `save_seq` 比较交换发现其他进程已写入；重新读取状态后重做本步 |

## 8. 操作纪律

- 不手工编辑 `state.json`；所有变更走 CLI，保留 `history` 与审计链。
- 不删除证据或历史记录来“让检查通过”；用新证据和新工作项推进。
- `drive` 返回 12 时先检查状态与错误码，不要把它当作任务失败或 BLOCKED。
- `block` 只用于真实缺少授权/决策/资源/外部条件；其余情况继续派生工作。
- 授权按动作生效：缺 commit 只阻塞 commit，缺 push 只阻塞 push；用 `--work-item` 精确范围，
  不要用 `--scope task` 冻结仍有可执行工作的任务。

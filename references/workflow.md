# workflow：v6 命令矩阵与操作流程

本文是 Copilot v6 的**操作层**权威说明：命令、参数、退出码与标准回合流程。
语义（阶段判定、派生规则、门禁细节）见 `runtime.md`、`review.md`、`agent-bridge.md`。
所有命令都只读写本地任务状态文件，不发起网络或模型调用。

## 1. 状态文件

- 默认路径由调用方指定（`--state`）；建议放在任务目录，例如 `<task>/state.json`。
- 同名辅助文件：`<state-basename>.dispatch.json`（原生派发）、`evidence/`（命令采集输出）、
  `state.json.lock`（并发锁，短生命周期）。
- 单文件 JSON，`schema=copilot-governance/v1`、`version=1`；写入为
  “临时文件 + fsync + `os.replace`”，并用单调 `save_seq` 做 CAS 并发校验。
- 状态已存在时 `start` 会拒绝，避免覆盖进行中的任务；续跑直接使用同一文件。

## 2. `copilot_dispatch.py`：任务与证据

```bash
python3 -B scripts/copilot_dispatch.py <子命令> --state <state.json> [参数]
```

| 子命令 | 用途 | 关键参数 |
|---|---|---|
| `start` | 创建任务合同 | `--goal`、`--contract-file`、`--criteria-file`、`--risk normal\|high`、`--backend legacy\|native`、`--task-id`、`--mode`、`--note`；`--risk high` 必须配 `--backend native` |
| `plan` | 登记 ZCode 本地计划 | `--plan <plan.json>`（或 stdin）、`--source` |
| `status` / `next` | 查看阶段、工作项、四门、下一步 | `--json` |
| `record` | 回填工作项结果 | `--id`、`--status`、`--result`、`--evidence E1`（可重复） |
| `rerun` | 把已终结工作项重开为 pending（修复后重新验证） | `--id`、`--reason`；旧结果保留在 `rerun_history` |
| `criterion` | 核销完成条件 | `--id C1`、`--status pending\|met\|not_met`、`--evidence`（`met` 必填） |
| `judgment` | 记录自我判断（仅审计） | `--status pass\|partial\|blocked`、`--summary`、`--evidence` |
| `evidence` | 登记 / 归档 / 列出证据 | `--add`、`--file`、`--text`、`--capture-cmd`、`--drop --id`、`--list`、`--next-id` |
| `report` | 登记执行报告 | `--summary`、`--blocked`、`--reason` |
| `decide` | 记录用户/任务决策 | `--text`、`--source` |
| `block` / `unblock` | 记录/解除真实阻塞 | `block --reason ... --needs ...`；`unblock --reason ...` |
| `closure-check` | 只读检查 G1–G4 | `--json` |
| `closure` | 收口（必须四门通过） | `--close`、`--by` |
| `reopen` | 用户授权后重开 | `--reason`、`--authorized-by user`（必填） |
| `handoff` | 只读列出生效交付清单：分支/HEAD/上游、已暂存、未暂存、未跟踪分类 | `--state`（可选）、`--cwd`、`--limit`、`--json`；不提交、不删除、不改 `.gitignore` |
| `contract` | 查看/修订合同与条件 | `--show`、`--amend --contract-file/--criteria-file`；BLOCKED 时也可修订；登记源文件摘要，文件被改动时 `status` 给出非阻断提醒 |
| `authorize-review` | 记录用户对正式 REVIEW 的授权 | `--authorized-by user`（必填，调用方声明）、`--reason`、`--scope` |
| `formal-review` | 登记已授权的正式 REVIEW 结论 | `--verdict pass\|partial\|blocked`、`--summary`、`--review-ref`、`--reviewer` |
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
  这些提醒不进入四门原因，由 ZCode 决定补 note、归档或保留。

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

## 3. `copilot_auto.py`：驱动与回填

```bash
python3 -B scripts/copilot_auto.py drive  --state <state.json> [--dispatch-out <path>] [--max-actions N]
python3 -B scripts/copilot_auto.py ingest --state <state.json> [--result-file f] [--review-file f]
```

### `drive`

一次调用在本地循环直到出现以下结果之一：

- `PASS` → 自动 `close_task`，退出码 0；
- `RUNNING` → 打印工作单（goal / constraints / expected_evidence / 状态回填命令），
  原生后端且工作项属于计划时写出 `dispatch.json`，退出码 10；
- `BLOCKED` → 打印状态，退出码 11；`CLOSED` → 打印状态，退出码 0；
- `PARTIAL` → 本地派生工作项并继续循环，不等待任何外部判断；
- 内部错误（派生失败、信封被拒、连续 32 步未收敛）→ 退出码 12，状态保留。

`--max-actions N` 只控制一次工作单输出几项，不是任务上限。

### `ingest`

- `--result-file`：`copilot-native-result/v2` 执行信封；校验绑定与证据后回填工作项状态，
  重复回填同一 `result_sha256` 是幂等成功。
- `--review-file`：`copilot-native-review/v2` 复核信封；G4 复核会重新读取宿主记录
  （metadata / rollout）核对后才登记。
- 被拒绝时退出码 12，错误码写在 stderr，状态不被部分写入。

## 4. 标准回合

```bash
# 1. 建立合同
python3 -B scripts/copilot_dispatch.py start --state T/state.json \
  --goal "目标" --contract-file contract.md --risk normal

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

## 5. 正式 REVIEW 与 G4

```bash
# 用户明确要求时才记录授权；同一合同内持续有效
python3 -B scripts/copilot_dispatch.py authorize-review --state T/state.json \
  --authorized-by user --reason "用户要求在交付前正式复核" --scope "当前任务合同"

# 复核完成后登记结论（治理记录，不是门禁）
python3 -B scripts/copilot_dispatch.py formal-review --state T/state.json \
  --verdict pass --summary "结论摘要" --review-ref <复核产物> --reviewer native
```

- 未记录授权时 `formal-review` 会被拒绝；合同修订后需要重新授权。`--authorized-by` 是
  调用方对用户要求的声明，不是可验证凭证。这不是普通开发的步骤。
- 高风险 G4 不依赖 `formal-review`：它需要 `native` 后端下的原生复核信封（见
  `agent-bridge.md`）与四门检查，缺失即 fail-closed。因此 `--risk high` 只能用
  `--backend native` 启动；`legacy` 后端没有派发链路，无法产生可回填的复核。

## 6. 历史只读

```bash
python3 -B scripts/copilot_dispatch.py legacy-status --state old-state.json --json
python3 -B scripts/copilot_dispatch.py legacy-status --audit-log .audit/copilot.jsonl
python3 -B scripts/copilot_dispatch.py legacy-status --ledger review-ledger.json
```

- v1/v2 状态不会被 v6 驱动；`legacy-status` 不写文件、不续跑、不改写历史。
- 历史调用记录、REVIEW 裁决、证据与 SHA-256 原样保留，仅标注为历史。

## 7. 退出码与常见错误

| 场景 | 退出码 |
|---|---|
| 命令成功 / `drive` 已收口 | 0 |
| `drive` 有待执行工作单 | 10 |
| `drive` 等待用户（BLOCKED） | 11 |
| 校验失败、信封被拒、内部错误 | 12 |
| 参数错误 | 2 |

常见拒绝码（`governance` / `native_bridge`）：

| 错误码 | 含义与处理 |
|---|---|
| `plan_drift` / `plan_mismatch` | 计划登记后被改动；恢复原计划或重新 `plan` 后再派发 |
| `result_sha_mismatch` | 信封内容与 `result_sha256` 不符；重新生成信封 |
| `evidence_tampered` / `evidence_sha_changed` | 证据文件变化或引用摘要不符；用新证据登记，不覆盖旧记录 |
| `evidence_foreign` | 证据属于其他任务；使用本任务证据 |
| `g4_*` | G4 复核记录缺失/无效/未通过；重新组织独立复核 |
| `schema_retired` | 使用了 v1 信封；改成 v2 |
| CAS 并发冲突（无单独错误码） | `save_seq` 比较交换发现其他进程已写入；重新读取状态后重做本步 |

## 8. 操作纪律

- 不手工编辑 `state.json`；所有变更走 CLI，保留 `history` 与审计链。
- 不删除证据或历史记录来“让检查通过”；用新证据和新工作项推进。
- `drive` 返回 12 时先检查状态与错误码，不要把它当作任务失败或 BLOCKED。
- `block` 只用于真实缺少授权/决策/资源/外部条件；其余情况继续派生工作。

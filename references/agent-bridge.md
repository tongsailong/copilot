# agent-bridge：原生执行与复核信封（v2）

本文件说明 v6 的 Native Bridge：**ZCode 原生 Agent 执行 / Reviewer 复核 → Copilot 校验回填**。
v1 信封（SOL 通道时代）已退役，出现即 `schema_retired`。

## 1. 角色与边界

| 角色 | 职责 | 约束 |
|---|---|---|
| Executor | 执行单个工作项，产出结果与原始证据 | 可以是主代理（`kind=main`）或原生子代理（`kind=subagent`）；不得使用 `reviewer` Profile |
| Reviewer | 独立只读复核，给出 verdict/findings | 必须是 `kind=subagent`、`profile_id=reviewer`、工具仅 `Read/Grep/Glob/Bash`；不得修改文件、不得与执行方共用 Agent/Child Session |

Copilot 不派发 Agent、不调用模型、不写别人的会话。它只校验宿主已产生的记录并回填状态。
`native_agent.py` 从真实 `metadata.json`、rollout 与证据文件生成信封，避免主代理手抄身份。

## 2. 信封 v2

### 2.1 dispatch（`copilot-native-dispatch/v2`）

由 `copilot_auto.py drive` 自动生成（`--backend native` 且工作项属于已登记计划）：

```json
{
  "schema": "copilot-native-dispatch/v2",
  "dispatch_id": "b-<24 hex>",
  "task_id": "task-...",
  "contract_sha256": "<64 hex>",
  "plan_id": "p-<16 hex>",
  "plan_sha256": "<64 hex>",
  "iteration_id": "I3",
  "created_at": "ISO8601",
  "actions": [{
    "work_item_id": "A1",
    "dispatch_id": "d-<24 hex>",
    "idempotency_key": "ik-<24 hex>",
    "title": "...", "assignee": "zcode", "goal": "...",
    "constraints": "...", "expected_evidence": "...", "depends_on": []
  }]
}
```

- `dispatch_id` 与 `idempotency_key` 由 `task_id + plan_id + plan_sha256 + work_item_id`
  确定性派生：同一计划同一工作项重复派发得到相同 id（幂等），计划变化则 id 变化。
- 只有 `execution_backend=native` 才能生成；`legacy` 后端直接 `backend_not_native`。
- 不做空派发（`dispatch_empty`）；同批不得重复工作项/id。
- 既有 dispatch 的 id 与当前绑定不一致时拒绝覆盖（`dispatch_id_drift`）。

### 2.2 execution（`copilot-native-result/v2`）

```json
{
  "schema": "copilot-native-result/v2",
  "task_id": "...", "contract_sha256": "...",
  "plan_id": "...", "plan_sha256": "...", "iteration_id": "I3",
  "dispatch_id": "d-...", "idempotency_key": "ik-...",
  "work_item_id": "A1",
  "status": "done|failed|blocked|dropped",
  "summary": "一句话结果",
  "result": "实际结果（文件/命令/diff 摘要）",
  "evidence": [{"id": "E1", "file": "/abs/path", "sha256": "<64 hex>", "kind": "file"}],
  "executor": {"kind": "subagent", "agent_id": "...", "child_session_id": "...",
               "profile_id": "executor", "model_reported": "..."},
  "timing": {"duration_ms": 1200},
  "usage": {"total_tokens": 321},
  "result_sha256": "<信封自身摘要>"
}
```

校验要点：

- `task_id` / `contract_sha256` / `plan_id` / `plan_sha256` / `dispatch_id` /
  `idempotency_key` / `iteration_id` 必须与当前 dispatch 一致。
- `result_sha256` 必须等于 `envelope_sha256(信封去掉该字段)`；被改动即 `result_sha_mismatch`。
- 证据至少一条；文件必须是绝对路径、存在、可读；SHA-256 必须与文件实际内容一致
  （`evidence_sha_mismatch`），且证据属于本任务（`evidence_foreign`）。
- 已终态（done/dropped）的工作项不能被不同结果覆盖（`work_item_terminal`）；相同
  `result_sha256` 重复回填是幂等成功。修复后需要重新验证同一工作项时，先用
  `copilot_dispatch.py rerun --state ... --id <工作项> --reason <原因>` 把它重开为
  `pending`（旧结果保留在 `rerun_history`），再回填新的执行结果；不要为了让新结果
  通过而伪造新工作项或改写历史。
- 执行方不得使用 `reviewer` Profile（`reviewer_as_executor`）。

### 2.3 review（`copilot-native-review/v2`）

```json
{
  "schema": "copilot-native-review/v2",
  "task_id": "...", "contract_sha256": "...",
  "plan_id": "...", "plan_sha256": "...",
  "dispatch_id": "d-...", "work_item_id": "A1",
  "execution_result_sha256": "<被执行结果的摘要>",
  "target_sha256": "<attempt 冻结的 final_evidence_digest>",
  "review_attempt_id": "RA<24 hex>",
  "review_attempt_created_at": "<attempt 创建时间>",
  "review_scope": "full|incremental",
  "based_on_review_sha256": "<增量复核的基线；full 时为空>",
  "evidence_manifest": ["E7", "E8"],
  "reviewer": {"kind": "subagent", "agent_id": "agent-review",
               "child_session_id": "child-review", "profile_id": "reviewer",
               "model_reported": "..."},
  "host": {"metadata_path": "/abs/metadata.json",
           "output_file": "/abs/review-output.json",
           "rollout_path": "/abs/model-io-<child>.jsonl",
           "tool_calls": [{"name": "Read", "count": 2}, {"name": "Bash", "count": 1}],
           "agent_id": "agent-review", "child_session_id": "child-review",
           "session_created_at": "<metadata.createdAt>",
           "session_completed_at": "<metadata.completedAt>",
           "prompt_sha256": "<metadata.prompt 的摘要>",
           "workspace_root": "<metadata.workspaceRoot>"},
  "verdict": "pass|partial|blocked",
  "findings": [{"id": "F1", "severity": "blocking|major|minor|info", "file": "...",
                "line": 1, "summary": "...", "evidence": "...",
                "root_cause": "...", "category": "...", "status": "open",
                "related": ["F2"], "reproduce": "..."}],
  "files_inspected": ["/abs/path"],
  "commands_run": ["git status --short"],
  "changed_files": [],
  "unverified": [],
  "residual_risks": [],
  "g4_eligible": true,
  "result_sha256": "<信封自身摘要>"
}
```

校验要点：

- 必须绑定一条**已回填**的执行结果（`execution_result_sha256`）；先 ingest 执行信封。
- G4（`g4_eligible=true`）必须带 `review_attempt_id` / `review_attempt_created_at`，且
  attempt 必须是 `review-context --create-attempt` 在本任务派发前创建的
  （`review_attempt_missing`、`review_attempt_invalid`）。attempt 由宿主冻结目标摘要、
  文件证据内容与 git 工作区代码范围；`review --attempt` 拒绝旧 attempt、已消费 attempt、
  目标过期或代码范围已漂移的 attempt。
- `work_item_id` / `dispatch_id` / 执行摘要必须与执行结果一致（`review_binding_mismatch`）。
- Reviewer 必须是独立子代理（`reviewer_invalid`），Profile 必须是 `reviewer`
  （`reviewer_profile_invalid`），且与执行方不同 Agent / Child Session
  （`executor_reviewer_same`、`review_contaminated`）。
- `files_inspected` / `commands_run` 由 `native_agent.py` 从宿主 rollout **派生**：
  `commands_run` 取实际 Bash 调用，`files_inspected` 合并自报与实际的读取路径；
  两者可以为空，不再要求 Reviewer 手工复述，也不因自述与记录不完全一致而作废
  整轮复核。`declared_files_unverified` 非空时会写入复核记录，供审计。
- 相对路径解析：`cd <dir>; cat report.md` 与 `git -C <dir> …` 之后的相对 token 会按
  该目录解析成绝对路径再判定“是否访问过证据”，避免已读取却判 `review_file_not_inspected`
  的假阴性；仍建议 Reviewer 使用绝对路径。
- finding 元数据：`id`（`F1`–`F9999`，本轮唯一）与 `status`（`open|resolved|regressed`，
  缺省 `open`）可选；`root_cause`/`category`/`related`/`reproduce` 可选。同一轮重复 ID
  或完全相同的 `(severity,file,line,summary)` 会被拒绝（`review_finding_duplicate`），
  用于强制按根因合并而不是重复计数。
- 宿主输出解析：接受纯 JSON 或**唯一**一个 fenced 代码块（语言标记大小写/带空格、围栏
  后无换行、CRLF、前后有说明文字均可）；出现两个可解析对象时 `review_output_ambiguous`。
- 实际执行过的 Bash 命令必须全部只读；任何写操作、输出重定向、`-delete`、`-exec`、
  `--output`、`sort -o`、`git grep -O`、`sed -i`、awk 写文件/`system(` 等一律
  `review_command_not_readonly`。只读检查工具（`sed -n`、`awk` 只读程序、`echo`、
  `jq`、`unzip -l`/`-p`/`-c`、`aapt dump`、`apksigner verify`、单输入 `xxd`、不带
  `-C` 的 `file` 等）与单层字面量 `for` 循环允许。
- 不在 `PATH` 的 Android/JDK 检查工具可用绝对路径调用
  （`/path/build-tools/36.0.0/aapt2 dump ...`）：只有 `aapt`/`aapt2`/`apksigner`/
  `dexdump`/`javap` 做路径归一化，且 realpath 必须**不在被复核工作区内**（工作区根取
  当前目录最近的 `.git` 祖先；在仓库子目录取证不改变边界）。相对路径可执行文件
  （`./tools/aapt2`）与通用命令的绝对路径（`/usr/bin/git`、`/tmp/x/cat`）一律拒绝：
  同名二进制可能由被复核内容写入，通用命令必须走 `PATH`。
- 未加引号的通配符（`*`/`?`/`[`）只对**没有输出位置参数、也没有写/执行选项**的只读族
  放行（`ls`、`cat`、`sha256sum`、`grep`、`diff`、`jq`、`zipinfo`、`aapt2` 等）；
  `sed`/`awk`/`find` 在通配符**含 `/`** 时也放行（展开结果仍带 `/`，不会变成 `-` 开头的
  选项，写操作另有 `sed -i`、`awk print >`、`find -delete` 校验）：真实复核用
  `awk '…' app/build/test-results/*/*.xml` 统计用例数。shell 展开会把文件名变成额外参数，
  所以 `file -C`、`rg --pre`、`git grep --textconv`、`unzip`、`sort -o`、`uniq`、`xxd`
  等族仍要求加引号（`file "*.apk"`）。
- 顶层 `;`、`|`、`&&` 可以串联多条只读命令（`cd app/build/test-results && ls -la`）；
  两侧各自仍须通过只读校验（`ls && rm -rf x` 的第二段会被拒）。单独的 `&`（后台执行）、
  `||`、重定向（`/dev/null` 抑制除外）、命令替换（`$(...)`/反引号）、花括号展开一律拒绝；
  未加引号的 `$` 也拒绝（`$?`、`$1`、`$VAR`，例如 `echo "exit:"$?` 会让整轮复核作废），
  只有文档允许的单层 `for f in a b; do … "$f" …; done` 字面量循环例外。解释器单行命令
  （`python3`/`python`/`perl`/`node`/`ruby`/`sh -c`）不在白名单，取证请用 `sha256sum`、
  `grep`、`sed -n`、`head`、`wc`、`diff`、`stat`。
- `files_inspected` 若填写，路径必须存在且是普通文件（`review_inspection_invalid`）。
- `g4_eligible=true` 时必须：`verdict=pass`、无 blocking、`unverified` 为空、
  `changed_files` 为空，并附完整宿主记录（`g4_requires_pass`、`g4_with_unverified`、
  `g4_with_blocking`、`g4_host_record_missing`）。
- 非 G4 复核若 `verdict=pass` 但有 blocking/unverified，同样拒绝
  （`pass_with_blocking_finding` / `pass_with_unverified`）。
- 增量复核（`review_scope=incremental`）必须带 `based_on_review_sha256`
  （本任务已回填的 PASS、`g4_eligible=true`、含覆盖快照的复核）与
  `evidence_manifest`；基线之后新增/变更的证据必须全部列入清单，条件状态与
  其他工作项不得变化，否则拒绝并要求完整复核（`incremental_*` 原因码）。

## 3. 宿主记录与 G4 资格

`g4_eligible` 由**脚本按宿主记录计算**，Reviewer 不能自述。`native_agent.py review`
读取：

- `metadata.json`：`agentId`、`childSessionId`、`profileId`、`profileSnapshot`
  （`tools` 必须是 `Read/Grep/Glob/Bash`，`disallowedTools` 必须含 `Write`/`Edit`）、
  `status=completed`、`totalToolUseCount`、`outputFile`、`createdAt`、`completedAt` 与
  `prompt`（G4 attempt 的会话时间/prompt 绑定）。
- rollout（`~/.zcode/cli/rollout/model-io-<childSessionId>.jsonl`）：实际工具调用序列、
  模型 provider/model。
- `outputFile`：Reviewer 原始结论（verdict/findings/…），只能提供判断内容，不能提供身份。

回填 G4 复核时，`copilot_auto.py ingest` 会再次调用 `verify_review_host_record` 重新读取
metadata/rollout，确认：

| 检查 | 失败码 |
|---|---|
| Profile 仍是 `reviewer` | `host_profile_changed` |
| Agent / Child Session 与信封一致 | `host_agent_mismatch` / `host_session_mismatch` |
| `output_file` 与 metadata 的 `outputFile` 一致 | `host_output_mismatch` |
| `session_created_at` / `prompt_sha256` 与 metadata 一致 | `host_session_time_changed` / `host_prompt_changed` |
| 会话创建时间不早于 attempt 创建 | `review_attempt_session_precedes_attempt` |
| 宿主 prompt 包含 attempt id | `review_attempt_prompt_unbound` |
| rollout 工具调用计数与 `host.tool_calls` 完全一致 | `host_tool_calls_changed` |
| rollout 中没有非只读工具或写命令 | `host_*` / `review_command_not_readonly` |
| metadata 声称的调用数与 rollout 实际一致 | `host_tool_calls_changed` |
| 信封 verdict / findings / unverified / changed_files 与宿主 outputFile 原文一致 | `host_verdict_changed` / `host_findings_changed` / `host_unverified_changed` / `host_changed_files_changed` |
| attempt 存在、绑定本任务、未消费给其他结果、目标仍是当前摘要、会话一致 | `native_review_attempt_*` / `native_review_stale` |
| attempt 冻结的文件证据内容未变化 | `native_review_attempt_workspace_drift` |
| attempt 冻结的 git 代码范围未变化 | `native_review_code_scope_drift` |
| 宿主 `workspaceRoot` 等于 attempt 代码根 | `native_review_attempt_workspace_missing` / `native_review_attempt_workspace_mismatch` |

任何问题都拒绝回填，结论不得作为 G4 依据。

**局限**：这些检查保证“不靠主代理记忆或手抄身份”，不是宿主签名；能写本机文件的人仍可伪造。
G4 是本地可得的最强原生复核，不等于密码学证明。

## 4. `native_agent.py` 用法

```bash
# 执行信封：身份/证据/摘要全部由脚本从真实记录提取
python3 -B scripts/native_agent.py execution --state T/state.json \
  --dispatch T/state.dispatch.json --work-item A1 \
  --metadata <agent-exec>/metadata.json --status done --summary "完成" \
  --result-file result.md --evidence E1=/abs/raw.log --output A1.execution.json

# 派发前创建 attempt：冻结任务/计划/执行/目标摘要/证据/代码范围
python3 -B scripts/native_agent.py review-context --state T/state.json \
  --dispatch T/state.dispatch.json --execution A1.execution.json \
  --work-item A1 --create-attempt --workspace "$(git rev-parse --show-toplevel)" \
  [--base <任务开始 commit>] --output A1.context.json

# 复核信封：必须绑定同一 attempt；高风险缺 attempt 直接拒绝
python3 -B scripts/native_agent.py review --state T/state.json \
  --dispatch T/state.dispatch.json --execution A1.execution.json \
  --work-item A1 --attempt RA... --metadata <agent-review>/metadata.json \
  --input reviewer.raw.json --output A1.review.json

# 派发前预检 Reviewer 只读命令清单（不执行；失败 exit=2）
python3 -B scripts/native_agent.py preflight-commands --file cmds.txt
```

- 子命令在写文件前调用 `native_bridge` 的正式校验；失败不产生输出文件。
- `review-context --create-attempt` 只允许高风险 G4；存在未核销完成条件、执行证据漂移或
  工作区不是 git 仓库时拒绝创建，避免把即将过期/不可证明的快照交给 Reviewer。它把
  attempt id 与代码范围写入输出；派发时必须把该 id 放进 Reviewer prompt。
- `review --attempt` 会核对 attempt 属于本任务、未消费、绑定与当前执行一致、目标摘要与
  代码范围仍等于当前状态，并核对宿主 `createdAt`、`prompt` 与 `workspaceRoot`；旧会话/
  旧工作区无法冒充新 attempt。
- `preflight-commands` 只做静态只读校验（`--file` 每行一条或 JSON 字符串数组）；它不
  执行命令，也不能证明 Reviewer 的输出格式合规。
- `review-context` 输出 `execution_evidence_unbound`（执行证据摘要与 live 证据
  不一致/已归档/未登记）、`evidence_manifest` 与建议 scope；ingest 会重新校验。
- 证据 SHA-256 由脚本读取文件计算，不接受调用方传入。
- `--evidence` 形如 `E1=/abs/path`，可重复。

## 5. 回填与 idempotency

```bash
python3 -B scripts/copilot_auto.py ingest --state T/state.json --result-file A1.execution.json
python3 -B scripts/copilot_auto.py ingest --state T/state.json --review-file A1.review.json
```

- 执行回填：校验通过后把工作项置为信封 `status`、写 `result` 与 `evidence_ids`，并把规范化
  信封加入 `native_results`。
- 复核回填：加入 `native_reviews`，G4 资格与 attempt 重新核对通过后写入；同一份写入把
  `review_attempts` 中对应 attempt 标记为已消费（记录 `consumed_by_result_sha256` 与
  Reviewer 会话），被拒时不消费。
- 同一 `result_sha256` 重复回填幂等跳过；不同结果覆盖已终态工作项被拒绝。
- 被拒绝时退出码 12，stderr 含错误码，状态不被部分写入。

## 6. 错误码速查

| 类别 | 错误码 |
|---|---|
| schema | `schema_invalid`、`schema_retired`、`hash_invalid` |
| 任务/合同 | `task_mismatch`、`contract_mismatch`、`credential_missing` |
| 计划/派发 | `plan_missing`、`plan_unknown`、`plan_mismatch`、`plan_drift`、`dispatch_missing`、`dispatch_empty`、`dispatch_duplicate`、`dispatch_mismatch`、`dispatch_id_drift`、`idempotency_mismatch`、`iteration_mismatch`、`work_item_unknown`、`work_item_terminal` |
| 执行 | `backend_not_native`、`assignee_unsupported`、`invalid_status`、`executor_identity_missing`、`reviewer_as_executor` |
| 证据 | `evidence_missing`、`evidence_invalid`、`evidence_duplicate`、`evidence_unreadable`、`evidence_sha_mismatch`、`evidence_foreign`、`evidence_conflict` |
| 复核 | `reviewer_invalid`、`reviewer_profile_invalid`、`review_binding_mismatch`、`executor_reviewer_same`、`review_contaminated`、`invalid_verdict`、`review_attempt_missing`、`review_attempt_invalid`、`review_finding_invalid`、`review_finding_duplicate`、`review_inspection_invalid`、`review_command_not_readonly`、`review_output_ambiguous`、`review_output_unparsable`、`review_scope_invalid`、`incremental_base_missing`、`incremental_base_ineligible`、`incremental_manifest_missing`、`incremental_evidence_uncovered`、`incremental_criteria_changed`、`incremental_scope_too_narrow` |
| G4 | `g4_host_record_missing`、`g4_host_record_invalid`、`g4_tool_calls_missing`、`g4_tool_not_readonly`、`g4_not_eligible`、`g4_requires_pass`、`g4_with_unverified`、`g4_with_blocking`、`pass_with_blocking_finding`、`pass_with_unverified`、`host_profile_changed`、`host_session_time_changed`、`host_prompt_changed`、`review_attempt_prompt_unbound`、`review_attempt_session_precedes_attempt`、`native_review_attempt_*`、`native_review_attempt_workspace_drift`、`native_review_code_scope_*`、`incremental_code_changed` 等 |

错误码只用于定位与修复，不提供绕过开关；修正记录后重新生成信封。

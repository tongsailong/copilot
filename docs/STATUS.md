# STATUS

更新：2026-11-09 CST。本文件只记录现场可核实的事实；规划与未完成事项单独标注。

## 当前阶段

- **Phase 3.6**：离线协议、校验、信封生成、异常/幂等测试与兼容性已完成；**完整 Native Bridge
  端到端（C4）仍未执行、未验证**。2026-11-08 在用户授权下完成过 **4 次真实 SOL HTTP 调用**，
  只做链路自检（字段省略、截断识别、一次升档恢复），不构成 C4 验收，也不构成完整业务闭环。
- **Phase 4（部分落地，2026-11-08）**：SOL 自动重判/收口与 PARTIAL 自动推进已实现并通过
  离线全链路测试；Reviewer 自动派发、可信复核记录升级仍未开始，需单独授权。
- **文档与代码口径（2026-11-09）**：`SKILL.md`、`references/role.md`、项目 `AGENTS.md`、
  `docs/PROJECT-CHARTER.md` 的现行文本已统一到 ChatGPT 自定义指令 v5.3.2；历史预算与旧角色
  口径保留并加替代注，不改写当时事实。同日清理了 Python 侧会进入运行时提示的旧预算文字
  （`review_ledger.py` / `call_gpt.py` / `copilot_dispatch.py`），并把独立复核的「最多 2 轮；
  到顶即停」改为证据驱动的无固定次数上限；见下文「本轮收口」。

## 源码与运行时

| 项目 | 实际值 |
|---|---|
| 唯一源码 | `/media/vdb/projects/copilot` |
| 运行时 Skill | `/home/ctyun/.zcode/skills/copilot` |
| Reviewer 运行时 | `/home/ctyun/.zcode/agents/reviewer.md` |
| 同步工具 | `scripts-sync/sync_runtime.py` |
| 分发的 Skill 文件 | 12 个（`SKILL.md` + 9 个 `scripts/*.py` + 2 个 `references/*.md`） |
| 分发的 Reviewer 文件 | 1 个（`agents/reviewer.md`） |
| 首次同步 | 2026-11-02 20:25–20:26 CST |
| 同步前备份 | `/home/ctyun/.zcode/backups/copilot-presync-20261102-202527` |
| 最近同步 | 2026-11-09（SKILL.md、call_gpt.py、copilot_dispatch.py、review_ledger.py 共 4 个文件） |

首次同步结果：

- `--check`：缺失 0、不同 0、Reviewer `same`；运行时多出 5 个 `tests/*.py`（未受管理，未删除）。
- `--apply`：写入 0 个文件（源码由运行时当前版本迁移而来，迁移后已一致）。
- 写后复查：Skill 缺失 0、不同 0；Reviewer 一致。
- 独立复核：11 个分发文件 SHA-256 全部一致；Reviewer SHA-256
  `3923c2db43301d2b05096bf6456376d157a92a4097a94eb215431d4f3c58ebeb` 与源码一致。
- `.audit/copilot.jsonl` 哈希未变化（`dbbc8de7…`），未进入源码仓库。
- 仓库内无符号链接。

## 测试基线

- 源码仓库：**728 tests OK**（`python3 -B -m unittest discover -s tests -p 'test_*.py'`，
  2026-11-09；本轮新增 1 项「旧额度上限之外仍可凭新证据复核」）。此前基线 727（2026-11-08）。
- 迁移前运行时基线：561 tests OK；新增 `tests/test_sync_runtime.py` 19 项。
- 迁移适配：7 个测试原先从自身文件位置推断宿主目录，源码移出 `~/.zcode` 后失败；
  已改为显式 `~/.zcode/references`、`~/.zcode/skills`，并断言导入模块来自当前 checkout。
  未放宽断言内容。运行时旧测试副本未修改（不参与分发），在安装布局下仍可运行。

## 已完成

- Phase 0–3：CPA 调用、状态机、合同/轮次、证据包、四门、legacy 回填、审计与配额。
- Phase 3.5：ZCode 原生双 Agent 能力与元数据采集验证。
- Phase 3.6（离线）：Reviewer 复合只读命令归一化；执行/复核信封的计划与幂等绑定；
  `native_agent.py` 从运行记录生成正式信封；异常、防篡改与幂等测试；旧状态兼容；G4 未放宽。
- 本项目：唯一源码仓库、Git 初始化并建立本地 `main` 初始提交（无远端）、SHA-256 基线
  `manifests/source-baseline.sha256`（13 个文件）、单向同步工具与 19 项测试。

## 仓库正式化（2026-11-03）

- Git：`main` 分支已建立本地初始提交；无远端、无 push。Git 用户
  `阿稳 <tongsailong@foxmail.com>`（继承全局配置）。
- 纳入版本管理：`AGENTS.md`、`SKILL.md`、`README.md`、`.gitignore`、`agents/`、
  `scripts/`、`scripts-sync/`、`references/`、`tests/`（含 fixtures）、`manifests/`、`docs/`。
- `.gitignore` 排除：`__pycache__`、`.env*`、密钥/证书、`.audit/`、`*.sqlite*`/`*.db`、
  `*.log`/`*.jsonl`、`state.json`/`dispatch.json`/`dry-run.json`、备份、临时文件。
- 文档职责：执行规则 → `AGENTS.md`；长期决策 → `docs/DECISIONS.md`（新增第 12 条）；
  当前状态 → 本文件；Skill 用法与触发 → `SKILL.md`；协议细节 → `references/`。
- 同步复查：`--check` 缺失 0、不同 0、Reviewer `same`；基线 13 个文件 SHA-256 全部匹配。
  `AGENTS.md`、`docs/`、`tests/` 不参与分发，根 `AGENTS.md` 未复制进运行时 Skill。
- 2026-11-03 12:10 再次同步 3 个文件（`SKILL.md`、`references/role.md`、
  `scripts/copilot_auto.py`）：角色标签由厂商名改为“继承当前会话模型”的模型中立表述。
  同步前备份 `~/.zcode/backups/copilot-presync-20261103-121056`；写后 `--check` 0/0，
  三对文件 SHA-256 与源码一致。
- 新会话验证（2026-11-03 12:13，headless 新进程）：项目 `AGENTS.md` 已加载，模型能原样
  引用第 2、3 节内容；Skill 列表发现 `copilot`，入口
  `/home/ctyun/.zcode/skills/copilot/SKILL.md`；调用 Skill 工具加载正文后引用到
  “ZCode 主代理（继承当前会话模型）”等更新后原文。Skill 正文为按需加载，启动时只发现
  description 属正常行为。

## Reviewer 模型路由（2026-11-03）

- **旧配置**：`agents/reviewer.md` 固定 `model: deepseek/deepseek-v4-pro` +
  `thoughtLevel: max`。ZCode 将其解析为内置 `deepseek` Provider
  （`https://api.deepseek.com/anthropic`），请求绕过 CPA。历史 `model_usage` 中
  `zcode-reviewer / deepseek / deepseek-v4-pro` 共 103 条；CPA `usage_events`
  近 24h 无 `v4-pro` 记录，两侧一致。
- **现配置**：`model: inherit`，Reviewer 的 Provider/模型/推理级别继承自主代理当前
  会话；`tools`/`disallowedTools`/`maxTurns`/`permissionMode` 与 Profile 正文契约未改。
- **新进程真实调用验证**（headless 新会话，2026-11-03 11:52:36 CST）：
  - ZCode `model_usage`：子会话 `sess_subagent_agent_7fd9a45c-…`，
    `agent=zcode-reviewer`，`provider_id=new-provider`，
    `model_id=deepseek-flash-high`，`variant=max`，`query_source=subagent`，
    tokens 12696/485/13181。
  - Reviewer rollout：`model.providerId=new-provider`、
    `modelId=deepseek-flash-high`，响应头
    `x-cpa-trace-id=20261103115236-…-01a19f89-95b8-7d4f-a124-7e5ac93482e6`。
  - CPA `usage_events` id=157061：`request_id=01a19f89-95b8-7d4f-a124-7e5ac93482e6`
    （与 rollout trace 精确一致），`alias=deepseek-flash-high`，
    `model=deepseek/deepseek-v4.1-flash-fast`，
    `provider=openai-compatible-command`，`client_ip=127.0.0.1`，
    tokens 12696/485/13181（与 ZCode 记录同值）。
  - 同一 headless 会话的主代理请求走同一 alias/上游，证明 Reviewer 与主代理路由一致。
- **缓存限制**：ZCode 在进程/会话启动时加载 Agent Profile。当前桌面进程启动于
  11:39，早于 11:47 的配置写入，因此该窗口内 Reviewer 仍使用旧 Profile；新会话或
  重启后才加载 `model: inherit`。新进程验证已证明新配置解析与路由正确。
- **同步**：`--apply` 写入 2 个 Skill 文件 + Reviewer；`--check` 缺失 0、不同 0；
  源基线重建为 13 个文件（含既有 `scripts/reply_protocol.py`）；配置前 Reviewer
  运行时备份在 `~/.zcode/backups/copilot-reviewer-model-route-20261103-114752/`。

## REVIEW 弹性预算（2026-11-04；历史实施记录，非现行预算规则）

> **状态（2026-11-09）**：本节记录当时已落地并验证的机制，**已由 2026-11-08 的预算解除取代**
> （`docs/DECISIONS.md` 第 16 条）。默认 2/3 轮与 +2/+3 追加额度现在只是观察输出，不是停止
> 条件；下文保留当时事实，不作为当前规则引用。

- **实际阻塞证据**：万年历广告对齐任务 `rili-ad-alignment-20261103`
  （台账 `/tmp/copilot/rili-align-20261103/review.json`）第 1 轮 PARTIAL、第 2 轮
  `remediation` 后仍 PARTIAL，回复明确列出 C1 重建路径反例、F4 未立即释放、序列验证不足；
  旧机制 `remediation` 每台账仅一次已用尽，2/2 轮后无法再发起定向复核。
- **新机制**：默认预算仍为普通 2 轮 / 高风险 3 轮；默认用尽后，若有新增实质证据/整改进展，
  可在普通 +2、高风险 +3 的**弹性追加额度**内继续定向复核；必须带新增/变化证据并写明
  追加理由。追加额度用尽即停止，按 PARTIAL/BLOCKED 汇报。`security` 当时不设次数
  （该豁免已由 2026-11-04 安全收口废止，见下节）。
  `remediation` 改为追加类型标签，不再是一次性名额；仍绑定原 subject / 台账 / 审计日志。
- **透明性**：`review_ledger.py --budget` 输出 `default_rounds / used / remaining /
  extra_used / extra_allowance / extra_remaining / extra_reasons / pending`，并明确
  「仅 REVIEW 计入预算，CONSULT/PLAN 不计入」。审计新增 `review_kind`、
  `review_extra_index`、`review_extra_allowance`、`review_extra_reason` 等字段；
  台账轮次记录 `review_kind / extra_index / extra_allowance / extra_remaining_after`。
- **对实际台账的只读核验**：用新脚本读取上述万年历台账，显示 `default_rounds=2`、
  `used=2`、`extra_allowance=2`、`extra_remaining=2`，下一步提示「有新增实质证据/
  整改进展可弹性追加」；未修改该历史任务、未新发请求、未重开任务。
- **代码与文档**：`scripts/review_ledger.py`、`scripts/call_gpt.py`、`SKILL.md`、
  `references/role.md`、`docs/DECISIONS.md`（第 13 条）、相关测试。
- **测试**：全量 **667 tests OK**；针对性覆盖默认预算、追加准入、无新证据拒绝、
  pending 计入、CONSULT 不计入、追加裁决/证据留痕、额度用尽报 PARTIAL/BLOCKED。
- **运行时同步**：`--apply` 写入 4 个受管理文件（`SKILL.md`、`references/role.md`、
  `scripts/call_gpt.py`、`scripts/review_ledger.py`）；13:27 二次 `--apply` 只补写后两者
  的措辞。两次后 `--check` 均缺失 0、不同 0、Reviewer `same`；源与运行时四个文件
  SHA-256 一致。同步前备份
  `/home/ctyun/.zcode/backups/copilot-review-elastic-20261104-132205`（含 `SHA256SUMS`）。
- **未改变**：G1–G4 fail-closed、Reviewer 独立性、高风险证据/身份/授权要求、
  SOL 模型（`gpt-6.1-sol` + `reasoning.effort=high`）与 CPA 路由；未发起真实 SOL 调用。
- **已本地提交（四阶段优化合并提交），未推送、未部署**；仓库无远端。

## Security REVIEW 预算收口（2026-11-04；历史实施记录，非现行预算规则）

> **状态（2026-11-09）**：本节的 Security 独立额度与「普通预算用尽后才可用」已由
> `docs/DECISIONS.md` 第 16 条取代（`security` 现为可随时使用的本轮依据标签，仍要求具体安全
> 风险与新增证据）。证据去重、call_id 去重、identity 校验等非预算约束继续有效。

- **审计结论**：旧规则（`security` 不设次数、只要求非空理由）可对同一 subject、同一组证据
  连续发起任意多轮 security REVIEW，且绕过证据内容去重；普通预算、台账/审计绑定、模型身份
  与 pending 计数本身仍有效。审计当时的真实日志中没有 security 调用记录。
- **新机制**：`security` 改为普通预算（默认 + 普通追加）全部用尽后才可用的独立例外，额度
  固定普通 1 轮 / 高风险 2 轮（`SECURITY_EXTRA_ALLOWANCE`；合计自动上限普通 5 轮、
  高风险 8 轮，均为上限而非必须用完）。准入同时要求存在新的实质安全
  风险、提供此前未用于任何一轮 REVIEW（普通或 Security）的新增有效证据、并用
  `--extra-round-reason` 写明风险与新增证据。Security 与普通追加共用内容哈希去重：换文件名 /
  路径、改措辞、重新打包同一内容均被拒绝，理由文字不能替代新证据。
- **额度用尽**：停止自动调用，按 PARTIAL/BLOCKED 汇报并请求用户针对该任务、具体风险与追加
  范围明确授权；自由文本、自声明 CLI 参数、新建 subject 或换台账均不构成授权。
- **计数与留痕**：`_split_usage` 分别统计普通 / Security 的已用与 pending；历史 Security
  轮次计入现有额度（不清零）；`review_kind` 与 basis 冲突、同一 call_id 在台账重复出现时
  失败关闭。审计新增 `review_risk / review_ordinary_used / review_security_* /
  review_new_evidence`（含 SHA-256）；台账轮次记录 `security_index / security_allowance /
  security_remaining_after`。
- **只读预检**：`--budget` 保留旧字段语义（`remaining` 只表示默认普通预算），新增
  `ordinary_*` / `security_*` / `security_reasons` / `next_action`；`next_action` 与
  `merge_and_guard` 的准入分支逐条对应（普通默认剩余、普通追加剩余、Security 可用、
  额度用尽需用户授权、pending 占用、绑定异常拒绝）。
- **代码与文档**：`scripts/review_ledger.py`、`scripts/call_gpt.py`、`SKILL.md`、
  `references/role.md`、`docs/DECISIONS.md`（第 13 条修订、新增第 14 条）、相关测试。
- **测试**：全量 **689 tests OK**；针对性覆盖普通 2/4 轮与高风险 3/6 轮、Security 仅普通
  用尽后可用、普通 1 / 高风险 2 硬顶、同证据/改措辞/换文件名拒绝、pending 计入、call_id
  去重、两通道互不占用、双通道用尽、旧台账兼容、CONSULT/PLAN 不计入、审计字段与预检一致性。
- **未改变**：普通默认预算与普通追加额度、G1–G4 fail-closed、Reviewer 独立性与身份验证、
  SOL 模型（`gpt-6.1-sol` + `reasoning.effort=high`）与 CPA 路由；未发起真实 SOL 调用。
- **已本地提交（四阶段优化合并提交），未推送、未部署**；仓库无远端。

## 证据完整性收口（2026-11-04）

- **触发（只看历史日志的只读复盘）**：荣耀买量 ACTIVATION 任务
  `honor-multi-ad-activation-20261104`（`/tmp/copilot/honor-v1`）。R2 E21 正文被截断
  6214 字符仍送审，SOL 据此给出无法闭合的缺证裁决；4/4 轮都在追问「被审的是哪份代码」，
  执行方只能用文字自述。证据包 66.8k → 252.3k 字符、SOL 输入 36.9k → 97.7k tokens。
- **截断闸门**：`review_ledger.py` 只从条目元数据区（围栏之前）解析「- 截断：省略 N 字符」，
  围栏内同名行按正文处理；`call_gpt.py` 在自检表列出截断条目、dry-run 汇总显示，
  本轮**新提交**的截断条目在 REVIEW 前失败关闭（退出码 2，未发请求、未计轮次）。
  确需放行时用 `--accept-evidence-truncation <理由>`：理由写入台账
  `evidence_truncation_accept_reason` 与审计 `review_truncation_accept_reason`。
  已入账轮次沿用的截断条目只提示并标注，不永久锁死后续复核。
- **代码绑定证据**：`bridge_evidence.py --attest REPO[::标签]` 由 git 机器生成 HEAD、分支、
  `status --porcelain=v1 -z`、逐文件 sha256 与 `diff --stat HEAD`；非 git 工作区或读取失败
  直接退出 2，不降级为文件清单。采集端同一改动把截断计数写进 stderr 与 manifest
  （`items_truncated` / `omitted_chars`）。
- **有意未做**：不改 SOL 预算、模型与 CPA 路由，不动 G1–G4 与证据 SHA/审计绑定，
  不新增 Agent 或状态机。「每改一处就重采整包、重跑全量」的重复劳动属于工程习惯，
  写进 `SKILL.md` 的验证效率建议（批量整改 + 定向证据 + 最终全量），不在代码里强制。
- **测试**：全量 **700 tests OK**（新增 `tests/test_evidence_integrity.py` 11 项：截断解析、
  合并元数据、REVIEW 拦截与放行留痕、采集端汇总、attest 干净/脏工作区/子目录/非 git）。
- **未验证**：未发起真实 SOL 端到端复测（无付费授权）；节省以历史日志推算，不是实测；
  对「历史截断条目在后续轮次仍被沿用」只验证了元数据与提示，未做真实多轮调用。

## 预算解除与 PARTIAL 自动闭环（2026-11-08）

- **动因**：SOL 调用预算（固定 REVIEW 轮次、弹性追加额度、请求数/Token 阈值）与输出预算
  （固定 8192）会打断正常决策；PARTIAL 后被当作终态，需要用户逐轮传话与授权。
- **复核轮次（DECISIONS 第 16 条）**：`review_ledger.budget_status` 恒 `allowed: true`、
  `limits_enforced: false`、`unbounded: true`；普通/追加/Security 数字只作观察输出。
  继续条件只剩「本轮有新增或变化证据」+「确有未闭合实质问题」；第 2 轮起仍要求
  `--extra-round-reason`。内容哈希去重、subject/台账/审计日志绑定、并发锁、换日志拒绝保留。
- **请求与 Token（同上）**：`NATIVE_DEFAULT_MAX_REQUESTS` / `NATIVE_DEFAULT_MAX_TOTAL_TOKENS`
  为 `None`，`budget_limits` 默认不设上限、`usage` 照实记录；只有 `start` 显式给出
  `--max-requests` / `--max-total-tokens` 才按显式运营上限拒绝出站（`exit 11/12`）。
  8 轮检查点与 `--rounds-limit` 降为观察信号，不拦截 drive。
- **输出预算与截断恢复（DECISIONS 第 17 条）**：`call_gpt.py` / `copilot_auto.py` 默认不发送
  `max_output_tokens`（删除固定 8192，也不新增固定天花板）。`status=incomplete` +
  `reason=max_output_tokens` 时不写回复、不入台账、不写状态、不计正式轮次；按观测
  `output_tokens` 约翻倍并对齐 4096 升档，用同一任务/证据/REVIEW 轮次重发，重发前记
  `retry_of` / `retry_reason` 的 `call_requested`；升档默认不限次，只有显式
  `--max-output-retries` 才构成运营限制，显式 `--max-output-tokens-ceiling` 是另一上界。
  预算无法继续提高或上游显式拒绝时，不重发相同失败请求，而是生成分段计划
  （`copilot-segmentation-plan/v1`，逐段文件 + SHA-256 + 条目清单，声明
  `same_failed_request_must_not_be_resent`），以 `transition=segmentation_required` 失败关闭；
  `drive` 返回 `status=segmentation_required`，由主代理分段处理证据后重新 REVIEW。
  截断响应同样要通过模型身份校验（退出码 9 优先）。
- **PARTIAL 自动推进（DECISIONS 第 18 条）**：`drive` 在最近一轮 `verdict=partial` 且无
  可执行安排时，本地派生 `origin=partial-without-plan` 工作项（0 请求、不消耗轮次），
  带 `goal / constraints / expected_evidence / completion_criteria / authorization /
  return_contract / executed_by / directive_complete` 的 `work_order` 进入 `EXECUTE`，
  由 ZCode 主代理执行；该轮已有执行/复核/报告材料时不再派生，必须把新材料交回 SOL。
  新增 `_round_material()` 防止「执行完却不交回 SOL」的漏判。
- **循环语义**：`REVIEW → PARTIAL → NEXT_WORK → EXECUTION → EVIDENCE → REVIEW` 直到 PASS
  收口或真实 BLOCKED；轮次数字、检查点、累计费用都不停止。六维无进展（streak ≥2 或同一
  工作项失败 ≥3 次）不再直接停机：驱动把失败、未解决事项与新增证据交 SOL 重新分析，
  要求给出失效原因、替代路径、下一步、完成条件/证据与授权边界；相同诊断材料不重复发送，
  REVIEW 恢复分析仍须显式 `--auto-review`。授权边界不变：超出原授权（生产发布、真实数据写删、
  远push、对外消息、权限/安全策略、业务规则变更）→ BLOCKED 并说明所需授权。
- **代码**：`scripts/review_ledger.py`、`scripts/call_gpt.py`、`scripts/copilot_dispatch.py`、
  `scripts/copilot_auto.py`；文档 `SKILL.md`、`references/role.md`、
  `references/agent-bridge.md`、`docs/DECISIONS.md`（第 16–18 条）。
- **测试**：全量 **711 tests OK**。新增/改写覆盖：输出预算省略与升档重发（同输入/同轮次、
  审计 `truncated`/`usage`）、显式 ceiling 与重试上限失败关闭、PARTIAL 无 plan 派生 +
  执行 + 复核 + PASS 收口全链路（native 与 legacy 各一条）、BLOCKED 显式停止、
  多轮不被固定上限拦截、显式请求/Token 上限仍在出站前拒绝、旧台账兼容。
- **未验证**：未发起真实 SOL 调用（无付费授权）；真实上游 `max_output_tokens` 默认值、
  截断升档后的实际恢复率、超长证据分段策略均只有单元测试与 Fake Upstream 证据。
- **提交状态（截至该阶段记录时）**：当时仍在工作区，未 commit、未 push。后续事实：该批改动
  已随本地提交 `d2ba759`（2026-11-08 20:26）入库；仍未 push，仓库无远端。

## 真实 SOL 验证（2026-11-08，4 次 HTTP 请求）

- **授权依据**：任务书 §6「必要时可发起真实 SOL 调用验证，内部成本预算不得阻断必要验证」。
  只做链路自检，不产生业务裁决；审计与输出在 `/tmp/copilot/verify-20261108/`（临时目录，未入库）。
- **验证 1（默认不发送 max_output_tokens）**：CONSULT 调用 HTTP 200（退出码 0），`model_resolved=gpt-6.1-sol`，
  `model_identity_ok=true`，审计 `max_output_tokens=null`，usage 13544/36。证明省略该字段时
  真实 CPA/SOL 通道正常接受并使用服务商默认上限。
- **验证 2（截断识别）**：`--max-output-tokens 16 --max-output-retries 0`，真实响应
  `incomplete_details.reason=max_output_tokens`、`usage.output_tokens=16`；审计写入
  `error=max_output_tokens / truncated=true / max_output_tokens_sent=16 / output_escalation_index=0`，
  随后 `call_post_failure=output_truncated_exhausted`，无 `--out`、无台账、无轮次推进。
- **验证 3（升档恢复）**：初始预算 32 被截断（32 tokens）→ 自动升档到 4096 重发 → 完整回复
  （846 tokens），退出码 0；审计两条 `call_requested`（第二条 `retry_of=<call_id>` /
  `retry_reason=max_output_tokens` / `output_escalation_index=1`）与一条成功 `call_result`
  （`max_output_tokens_sent=4096`）。
- **合计**：4 次请求，输入 54125 / 输出 930 tokens。未用真实 SOL 跑完整 PARTIAL→EXECUTE→
  REVIEW→PASS 多轮链路（该链路仍只有 Fake Upstream 全链路测试）。

## 自动闭环最后收尾（2026-11-08）

- **输出截断恢复**：`--max-output-retries` 默认改为 `None`（不限次），仅显式传入才构成运营
  限制；按实际 `output_tokens` 约翻倍并对齐 4096 持续升档。无法继续提高（显式上限/ceiling）
  或 Provider 拒绝预算时，不重发相同请求，自动写出 `copilot-segmentation-plan/v1` 与
  `segment-*.md`（含条目、字符数、SHA-256），以 `transition=segmentation_required` 失败关闭；
  `drive` 返回 `status=segmentation_required`。截断仍不产生裁决、不写台账、不推进轮次、
  保留任务/合同/证据指纹；Provider 限制、认证失败、网络错误分别留痕。
- **未知用量**：`task_spend_from_audit` 区分已知/未知用量。成功有 usage 计真实 Token；
  成功无 usage、已送达/可能送达的失败无 usage 计入 `unknown_usage_calls`；确认未送达的
  传输失败只计 `not_delivered_calls`；失败但有部分 usage 记录已知 Token 并保留失败状态。
  同一响应按 `call_id + 升档序号 + response_id` 去重，重试不重复累计，跨轮次从审计重建。
- **无进展恢复**：`drive` 不再因 `progress_streak >= 2` 直接停止；汇总失败、未解决事项与
  新增证据，生成诊断包交 SOL 分析原方案失效原因、替代路径、下一步、完成条件/证据与授权边界，
  DeepSeek 执行后再次 REVIEW。相同诊断材料不重复请求（`no_progress_no_new_information`）；
  REVIEW 恢复分析仍须显式 `--auto-review`；只有无安全替代/缺决策/缺授权/缺资源/外部硬限制
  才可 BLOCKED，并须给出具体原因与证据。
- **代码**：`scripts/call_gpt.py`、`scripts/copilot_dispatch.py`、`scripts/copilot_auto.py`；
  文档 `SKILL.md`、`references/role.md`、`docs/DECISIONS.md`（第 17–19 条）、本文件。
- **测试**：全量 **727 tests OK**。新增覆盖：默认不限次升档直至成功、连续截断升档序列、
  Provider 拒绝预算转分段、显式重试上限、HTTP 408 无 usage 计未知、已送达失败计未知、
  确认未送达不计未知、成功 usage 准确、重复响应去重、跨轮次累计、连续无进展触发 SOL
  重新分析、无新信息不重复请求、未授权 REVIEW 不绕过、SOL 无安全替代时带证据 BLOCKED。
- **未验证**：未为本轮三项修复重新发起真实 SOL 调用（按任务书允许复用自动化与 Fake
  Upstream）；真实 Provider 对极大 `max_output_tokens` 的接受边界、真实超长证据分段后的
  裁决质量、无进展诊断在真实多轮中的效果仍只有单元测试与 Fake Upstream 证据。

## 本轮收口：代码文本与独立复核口径（2026-11-09）

> **此前记录的偏差已处理完毕**：2026-11-09 早先版本的本节列出 5 项「Python 仍保留旧预算文字 /
> 独立复核仍有 2 轮上限」，现已全部改写，不再存在「披露但未修」的现行偏差。

**独立复核「最多 2 轮」的实际语义（调查结论）**：该上限只写在 `SKILL.md` 的说明文字里，
`copilot_dispatch.py`、`call_gpt.py`、`copilot_auto.py` 与本机原生子代理链路均无任何按次数的
强制、拒绝或停止分支；`review_ledger.py --budget` 的 `allowed` 恒为 true、`limits_enforced=false`。
因此它不是代码级闸门，而是**文档级的验收链停止门**：执行方按文档在 2 轮后即不再组织复核，
即使已有整改与新证据也一样。按 v5.3.2「默认安排不应成为硬停止门」，本轮改为证据驱动、无固定次数上限。

- **改动范围**：`scripts/review_ledger.py`（模块说明、常量注释、`merge_and_guard` 文档、
  `render_ledger` 注入文本与 `--budget` 输出）、`scripts/call_gpt.py`（`--extra-round-reason` /
  `--extra-round-basis` 帮助、`_review_round_meta`、`EXIT_BUDGET` 注释）、
  `scripts/copilot_dispatch.py`（`--rounds-limit` / `--max-requests` / `--max-total-tokens` /
  `--no-budget-projection` 帮助、参数校验与 `render_state` 段 8）、`SKILL.md`
  「评审档位与缺陷处置」§4 与版本沿革、`tests/test_copilot.py`。
- **没有改动**：状态机、`budget_status` 计算、证据去重、G1–G4、原生独立复核工具集与独立性约束、
  正式 REVIEW 授权门、模型路由、CPA 配置。
- **旧数字的去向**：`RISK_MAX_ROUNDS` / `EXTRA_ROUND_ALLOWANCE` / `SECURITY_EXTRA_ALLOWANCE`
  常量保留，注释标为「旧策略参数，只用于报告与审计追溯」；`--budget` 以「历史参考」展示并注明
  「不构成停止条件」。继续复核的实际条件仍是「本轮有新增/变化证据 + 写明理由」。
- **G4 未受影响**：G4 仍要求宿主可信原生复核记录，缺少即 `native_review_missing` fail-closed；
  `g4_eligible=false` 不变。SOL 的 CONSULT/PLAN 意见依旧不是原生独立复核，也不是授权。
- **提交**：机制改动已本地提交 `a5c56f2`（`fix(copilot): make review continuation evidence-driven`），
  未 push；仓库无远端。

### 本轮验证（2026-11-09）

- 全量回归：`python3 -B -m unittest discover -s tests -p 'test_*.py'` → **728 tests OK**。
- 定向：`tests.test_review_budget`（19 OK）、`tests.test_copilot`（511 OK）、
  `tests.test_auto_loop` + `tests.test_sync_runtime`（40 OK）。
- 本地提示捕获（未联网、未付费）：用 `/tmp/copilot/budget-semantics/prompt-check/capture.py`
  在进程内替换 `write_request_dump`，以 `--mode REVIEW --dry-run` 构建出**真实最终请求体**。
  第 3 轮（越过旧 2+2+1 上限）的输入中确认包含「复核没有固定轮次上限」「轮次与历史额度数字只用于
  审计观察，不是继续或停止条件」「本轮复核理由（整改后定向复核）」；旧停止语句
  （「最多 2 轮」「到顶即停」「额度用尽后必须停下」「等待用户授权」「剩余额度」）均不存在。
- 运行时同步：`sync_runtime.py --apply` 写入 4 个 Skill 文件，复查 `--check`
  **缺失 0 / 不同 0 / Reviewer same**；5 个运行时 `tests/*.py` 未受管理并保留。
  源码与运行时 SHA-256 一致：
  `SKILL.md` `5f3b6d94…`、`scripts/call_gpt.py` `936da404…`、
  `scripts/copilot_dispatch.py` `f951c774…`、`scripts/review_ledger.py` `6e722b9e…`；
  Reviewer `9ef6dccf…` 两侧一致。审计日志 mtime 09:54（本轮 SOL CONSULT），dry-run 未写审计。
- **未验证**：本轮没有发起任何真实 SOL 调用；未做运行时端的真实 REVIEW 闭环；上述捕获证明的是
  请求体构建与文本注入，不等于 GPT 端实际解读或裁决质量。

## 文档一致性对齐（2026-11-09）

> **批次说明**：本节记录当日的 Markdown 对齐批次；同日后续的代码文本与独立复核收口见上节
> 「本轮收口」，两节的范围内表述互不冲突（本节「只改 Markdown」只适用于本批次）。

- **范围**：`SKILL.md`、`references/role.md`、项目 `AGENTS.md`、`docs/PROJECT-CHARTER.md`、
  `docs/DECISIONS.md`、`docs/ROADMAP.md`、`README.md`、本文件。只改 Markdown，未改 Python、
  测试、状态机、预算实现、模型路由与 G1–G4。
- **现行口径**：协作基线统一为 ChatGPT 自定义指令 **v5.3.2**（`chatgpt-v5.3.2-alignment.md`
  标注为摘要而非原文）；补入六角色边界；删除「每轮一次 / 预算 / 配额」这类把默认观察值写成
  停止门的表述；明确「一次 drive 至多一次 SOL 调用」只是单轮驱动节流，不是任务总轮次或 HTTP
  请求总数上限；区分正式 REVIEW、运行时 verdict、原生独立复核与 G4。
- **历史处理**：SKILL.md / role.md 的版本沿革保留并加替代注；DECISIONS 第 13、14 条标为历史
  预算方案（由第 16 条取代），第 15 条只标预算/停止条件部分被取代，证据完整性与机器绑定继续
  有效；STATUS 的 2026-11-04 预算小节标为历史实施记录，不改写当时事实。
- **真实调用范围**：明确 2026-11-08 的 4 次真实调用只做链路自检（字段省略、截断识别、一次升档
  恢复），完整 C4 与真实多轮闭环仍未验证。
- **验证**（本次执行）：
  - 全量测试 `python3 -B -m unittest discover -s tests -p 'test_*.py'`：**Ran 727 tests, OK**
    （exit 0）。
  - 同步：首次 `--apply` 写入 2 个 Skill 文件（`SKILL.md`、`references/role.md`）；修正交叉引用
    后再次 `--apply` 只写入 `SKILL.md`，Reviewer 无需更新。最终 `--check`：**缺失 0、不同 0、
    Reviewer same**（exit 0）。运行时仍保留 5 个未受管理 `tests/*.py`（设计上不清理）。
  - 哈希（该批次结束时）：源与运行时 `SKILL.md` 均为 `d4b3f572…`，`references/role.md` 均为
    `7c1be8b8…`。
  - 实际加载：在**独立子代理新上下文**中经 Skill 工具加载 `~/.zcode/skills/copilot/SKILL.md`，
    确认 description 含「不存在人为默认的轮次、请求数或累计 Token 上限」、正文标题为
    「copilot：GPT-6.1-SOL 高级工程决策通道」、存在「六角色边界（v5.3.2）」、当前对照为 v5.3.2。
    项目 `AGENTS.md` 在该子会话上下文中已注入。**注意**：这是子代理新上下文加载，**不是新的
    桌面/headless 会话**；本机没有可用的 ZCode headless CLI（`/usr/bin/zcode` 指向 Electron
    桌面应用），因此「新桌面会话加载验证」**未执行**。
- **未验证**：新桌面会话 / headless 进程级加载验证；真实 SOL 对这些文档的复核（本轮只有一次
  真实 PLAN 调用，`6eae8e768f390bbb`）。

## 未验证 / 未完成

- **真实 SOL 端到端（C4 / Native Bridge）**：未执行、未验证。2026-11-08 的 4 次真实调用只覆盖
  默认字段省略、截断识别与一次升档恢复，**不等于**完整闭环，也不替代 C4。
- **真实多轮业务闭环**：PARTIAL → EXECUTE → REVIEW → PASS 的完整链路目前只有 Fake Upstream
  全链路测试，**没有**真实 SOL 多轮闭环证据；真实上游的默认输出上限绝对值未知。
- 2026-11-08 三项收尾修复（默认不限次升档、未知用量、无进展重析）未重新发起真实调用，
  仍以自动化与 Fake Upstream 为证据。
- `native_agent.py` 的真实子代理 `metadata.json` + rollout 提取：目前只有单元测试与
  合成记录验证，未用真实 ZCode 子代理运行记录跑通。
- native 任务在真实执行/复核后的 G1–G4 与正常风险收口：未验证。
- 当前桌面会话内新 Reviewer Profile 的生效验证：该进程在配置写入前启动，Profile 为
  启动期缓存；需新会话或重启后复查 `model_usage` 的 `zcode-reviewer` 行（独立新进程
  已验证通过）。
- 运行时残留 `tests/` 与源码测试已不同步（设计上不分发）；未删除。
- Git 远端、后续提交节奏与分支保护策略未定；当前无远端、无 push、无发布。

## 下一任务

1. 获得一次性授权后执行 Phase 3.6 C4（真实 SOL，预算与请求上限见授权包）；2026-11-08 的 4 次
   局部调用不计入 C4 验收。
2. 在需要时用真实 SOL 复测本轮三项收尾修复：默认不限次升档、未知用量统计、无进展重析
   （需新的一次性授权）。
3. 达成后按固定流程开发：源码修改 → 测试 → 检查 diff → 同步 → `--check` → 新会话加载。
4. Phase 4 剩余范围（Reviewer 自动派发、可信复核记录升级）需单独授权与实现。

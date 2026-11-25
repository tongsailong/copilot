# Context Engine（项目上下文与长期记忆，v9.4.0）

`scripts/context_engine.py` 是 Copilot 的**项目上下文协调入口**：把「这个项目是什么、以前
确认过什么、任务做到哪、子 Agent 需要知道什么、这次经验该不该沉淀」变成确定性的本地检索
与结构化装配。它只读、零模型调用、不新建数据库，也不替代宿主记忆抽取或知识库写入。

## 1. 四层信息与职责

| 层 | 住所 | 谁维护 | Context Engine 的作用 |
|---|---|---|---|
| 原生项目 Memory | `<workspace basename[:48]>-<sha256(realpath)[:16]>/memory/` | ZCode 宿主自动抽取 + 用户 Memory 规则 | 只读定位、统计索引、召回卡片；不写、不复制、不重建抽取 |
| 长期知识 Vault | `<knowledge-vault>`（白名单目录） | `knowledge-manager` Skill | 白名单有界召回；保留卡路径/核验时间/`source_commit`；不写正式区 |
| Copilot Task Memory | `.copilot-tasks/<task>/state.json` | `governance.py` 状态机 | 读合同/条件/证据，装配恢复快照与上下文包 |
| 工程现场 | Git、`AGENTS.md`、源码、测试 | 用户与 ZCode | 采集分支/HEAD/工作区状态；历史与现场冲突显式标注、现场优先 |

数据流只有一条：**resolve（定位）→ recall（有界检索）→ pack（装配）→ 交接/恢复**；
任务结束时 **capture（沉淀评估）** 生成草稿，实际写入仍走
`knowledge-manager/scripts/candidate_write.py`（候选）或经授权后的 `vault_write.py`（正式）。

## 2. 命令

```bash
# 项目身份 + 原生 Memory 定位 + Git 基线 + 规则文件
python3 -B scripts/context_engine.py resolve --workspace <workspace> [--json]

# 白名单有界召回（关键词必填；| 或 , 分隔）
python3 -B scripts/context_engine.py recall --workspace <workspace> \
  --keywords '词1|词2' [--scope vault,memory] [--max-lines 40] \
  [--state <state.json>] [--no-cache] [--no-ledger] [--json]

# 上下文包：self 12,000 / 认知 8,000 / subagent 5,000 字符
python3 -B scripts/context_engine.py pack --state <state.json> --target subagent \
  [--keywords '词1|词2'] [--requirement '验收要求'] [--handoff-file handoff.json] [--out FILE]

# 沉淀评估 + 可直接补全的候选草稿（只写任务目录）
python3 -B scripts/context_engine.py capture --state <state.json> \
  [--keywords '词1|词2'] [--title '<候选标题>']
```

退出码：`0` 正常（含 `zero_hit`）；`1` 检索失败（`status=failed`）；`2` 参数/路径错误。

## 3. 关键语义

- **项目识别**：按 Git 根的真实路径计算 `sha256[:16]`，只匹配 `<basename>-<hash16>` 目录；
  同名的不同路径项目不会串库，缺失如实返回 `absent`，根不可读返回 `unreadable`。召回与
  上下文包只使用 Git 根对应的记忆目录（`role=root`）；`--cwd` 探测到的子目录可能是历史
  独立项目，不并入当前项目来源。
- **有界召回**：只扫描 knowledge-manager 公布的正式白名单（`10-Products`、`20-Platforms`、
  `30-Cross-Project-Decisions`、`15-Engineering-Memory`、`40-Glossary.md`）与当前项目
  Memory；排除 `01-Inbox`、`03-Daily-Reviews`、隐藏与工具目录。只有文件第 1 行的 `---`
  才进入 frontmatter 跳过（正文里的水平分隔线不会被误判）；标题行加权；每文件最多 4 条，
  先多收再按得分裁剪。
- **检索边界**：只产出 `realpath` 仍在白名单根内的文件；指向外部的符号链接跳过并在 `notes`
  记录，不可读子树记为来源错误（`failed`/`partial`），不静默当成零命中。
- **缺省不等于零命中**：`zero_hit` 与 `failed`、`partial`、`budget_exhausted`、`absent`
  严格区分；截断或来源失败优先于“有命中”，结果带 `truncated`/`notes`，不得当作完整结论。
- **预算**：默认 40 行 / 4,000 文件 / 24 MB / 5 s；超过任一预算即标注不完整；非正数预算
  直接以 `BUDGET_INVALID` 拒绝。
- **缓存**：键含项目根、关键词、scope、全部扫描预算（行数/文件数/字节数/时间）与白名单规则
  版本，值绑定文件集合的 `path+size+mtime_ns` 摘要；文件变化或 TTL（默认 600 s）到期即失效。
  只有完整结果（`ok`/`zero_hit`）入缓存，截断与失败结果不复用。缓存与台账写到任务目录
  `context/`，不写工作区外。
- **脱敏**：召回片段与 `capture` 去重片段统一走 `lib_common.redact`；命中路径按
  `is_sensitive_path` 整体替换为占位符。
- **可信度**：上下文包对每条命中输出 `核验时间 · source_commit · 状态`；缺失标注「视为
  未核实」，`source_commit` 与当前 HEAD 不一致标注「基线漂移」；冲突处理统一为
  「以现场为准 + 写出卡路径/差异/核验时间」，不静默采用旧结论。
- **沉淀**：`capture` 只读任务状态与证据，生成符合 `candidate_write.py` 校验契约的草稿
  （`created` 日期、`source_commit: null`、必填标题与顺序、去重前置），住所判定留作
  `candidate_target: 待判定`，由主 Agent 核验后补全；工具本身不写 Vault、不写原生 Memory。
  完成条件的证据引用同时兼容治理写入的 `{id, sha256}` 对象与字符串 id。

## 4. 接线

- **UserPromptSubmit（工程任务首轮自动召回）**：`copilot_stop_guard.py user-prompt` 在任务提示之外
  调用 `prompt_recall.maybe_inject`。分类是确定性的：闲聊、短指令、斜杠命令、纯问答不触发；
  需要至少一个工程信号（领域词/代码路径/命令）且当前目录在 Git 仓库内。命中后按
  Memory 3 条 + Vault 2 条、总长 ≤1,400 字符注入 `path:line`、核验时间、`source_commit`
  与片段；`budget_exhausted`/`partial` 显式标注「结果不完整」，不伪装成完整结论。
  每次调用最多 40 命中 / 1,200 文件 / 8 MB / 1.2 s，缓存 600 s；同会话同项目按强词哈希
  去重，换主题才允许增量召回，单会话最多 4 次。失败、零命中、冷却都返回 `exit 0` 且不阻断
  用户回合；事件写入 `~/.zcode/copilot-runs/context-recall/events.jsonl`。
- **SessionStart(startup|resume|compact)**：`copilot_stop_guard.py` 在任务状态快照后追加项目现场
  （根/分支/HEAD/工作区）、原生 Memory 与 Vault 可用性，以及 `recall` / `pack` 命令；
  `resume`/`compact` 还把本会话已召回的前 2 条结论放进 `<copilot-recall-state>`，恢复后直接复用、
  不重新检索。只读、失败即省略、总长仍受 `SESSION_START_LIMIT` 约束，任何异常都不阻断会话启动。
  该路径的单次 git 调用收紧到 1 s（钩子总超时 10 s），`git status` 失败时显示「未知」
  而不是 clean。
- **Stop（收口/阻塞后的 Capture 提示）**：本会话绑定任务进入 `CLOSED` 或真实 `BLOCKED` 且
  当前终止版本没有回执时，守卫以退出码 2 要求一次 Capture 评估（不是继续开发，也不改任务
  状态）。完成评估后用 `copilot_stop_guard.py capture-ack --task-dir <dir> --status
  done|skipped|failed` 写 `.capture.json`，幂等键为 `closed:<closed_at>` 或
  `blocked:<updated>`；同一版本重复 Stop 不再提示。绑定前就已终止的旧任务不提示，避免把
  别人的成果算到本会话。回执只记录评估结果，不放行未评估任务，也不产生 PASS。
- **status**：`copilot_dispatch.py status` 在人工输出末尾附一行上下文提示；JSON 契约不变。
- **子 Agent 交接**：`pack --target subagent` 输出 ≤5,000 字符的上下文包（项目身份、工程
  基线、任务条件、历史线索、可信度提示、验收要求），可与 `knowledge_handoff.py` 的
  `--target subagent` 交接块组合（`--handoff-file`）后放入 prompt。子 Agent 不继承
  Memory/AGENTS 注入（本仓库 2026-11-21 探针实测），所以交接必须显式给正文。

### 自动召回的状态与隔离

- 状态目录默认 `~/.zcode/copilot-runs/context-recall/`，可用 `COPILOT_RECALL_STATE_DIR` 覆盖；
  测试可用 `COPILOT_RECALL_MEMORY_ROOT` / `COPILOT_RECALL_VAULT_ROOT` 指向临时知识根。
- 只持久化强词哈希、已交付路径/行号/片段哈希与前 2 条短摘要；不保存原始 prompt、完整卡片或
  疑似凭据。长十六进制串不进入检索词。
- 项目键是 Git 根的 `sha256[:16]`，与会话哈希共同组成状态文件；不同项目不共享去重与召回内容。
- 同会话冷却 60 s；明显换主题（≥2 个新强词且与上次重合 <50%）可立即增量召回。


## 5. 边界

- 不改 ZCode 二进制、原生记忆抽取、宿主记忆目录内容与知识库写入规则。
- 不新增 Provider、不改模型路由；召回与装配零模型调用。
- 不做语义检索、不建向量库、不新增记忆数据库；检索结果只按确定性规则排序。
- 不越界读取：白名单根之外的符号链接目标与不可读子树都不会被静默当成“已扫描”。
- 不自动把召回结论写成事实：历史是线索，冲突以现场为准。
- 正式知识写入仍需用户授权，候选写入只走 `candidate_write.py`。

## 6. 验收要点（对应任务 C1–C10）

| 条件 | 证据 |
|---|---|
| C1 统一架构 | 本文件 §1 数据流；模块只依赖 `lib_common`/`governance` 只读接口 |
| C2 项目记忆 | `resolve` 的 `hash16` 匹配；同名不同路径单测 |
| C3 智能召回 | 白名单扫描、零命中/失败/截断区分、预算与缓存单测 |
| C4 历史复用 | 命中带 `path/line/snippet/meta`；`pack` 输出核验时间与 `source_commit` |
| C5 上下文连续性 | SessionStart 快照追加项目现场与命令；异常降级单测 |
| C6 Agent 交接 | `pack --target subagent` ≤5,000 字符；可与 handoff 组合 |
| C7 知识沉淀 | `capture` 草稿通过 `candidate_write.validate_content`；去重前置 |
| C8 可信度 | 未核实/基线漂移章节；冲突处理语句 |
| C9 性能与兼容 | 零模型调用（静态检查）；缓存命中/失效；活动套件不回归 |
| C10 测试与运行时 | `tests/test_context_engine.py`；`sync_runtime --check` 0/0；baseline 通过 |

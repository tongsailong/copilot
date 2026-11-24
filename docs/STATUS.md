# STATUS

更新：2026-11-25 CST。本文件只记录现场可核实的事实；规划与未完成事项单独标注。
当前版本：v9.8.0（多模态设计认知：真实图片输入、无图/有图双模式、设计协作
与知识复用）；v9.7.1 及更早的治理记录见下。基线以本次交付的最终提交与运行时核验为准。
任务 `sol-cognition-governance-p1p3-2026-11-23` 经用户授权一次性 `reopen` 后，已于
`2026-11-24T07:40:07` 在**最终代码**上重新收口 **CLOSED / PASS**（周期 2，G1–G3 全 PASS）。

**正式交付（2026-11-25）**：开发效能专项已入库——提交 `24a2e31`（12 文件，+1411/−370）
经 PR #9 合并为 `b83cf33`；CI Python 3.10 / 3.12 各 956 项 OK、`--verify-baseline` 通过；
main 三方（本地 / `origin/main` / `ls-remote`）同为 `b83cf33`（0/0），工作区干净；
运行时 `--check` 缺失 0 / 不同 0，27 个受管理文件源码与运行时 SHA-256 全部一致。

## 开发效能与自主交付闭环专项（2026-11-24 实施，2026-11-25 正式交付）

任务 `dev-efficiency-2026-11-24`（`task-fd963b4e34918738`）实施时授权不含 commit/push，
交付由后续任务 `delivery-2026-11-25`（`task-9066a221fd2fdeee`）在用户明确授权下完成；
现场基线、已修缺陷与保留边界如下。

- **调查基线（实测）**：26 个受管理分发文件、HEAD `b3ba3e7`（v9.8.0）；跨 5 个工作区
  （copilot / pedometer / rili / lele-pedometer-xin / designwise）共 104 个任务台账：91 收口
  且全 PASS、13 未收口（12 个超 24h 未更新）、条件 829/865、工作项 422（失败 0、重跑 24）、
  证据 1,524（丢弃 197、引用 2,577、复用率 0.575 proxy）、SOL 137 次 / 913,698 tokens
  （延迟 p50 55.2 s、p95 133.2 s；采纳已登记 83、待登记 52）、未闭合义务 4、未解除阻塞 18。
  召回事件 75 次（注入 69、零命中 6，其中 5 次预算耗尽；p50 659 字符 / 78 ms）。宿主库
  7 天：402 回合、40,028 次模型请求、44,181 次工具调用、工具错误 0、上下文超限 0。
- **已修缺陷（审计保真）**：SOL 义务终态处置此前用“周期最新策略”标注“当时调度策略”，
  与窗口打开时的真实策略不符（真实案例 `adfree-retire-2026-11-24` 在 `advise` 下打开却被
  标成 auto）。现新增 `policy_at_open` 与每周期 `policy_snapshots[*].history`（上限 50 条），
  历史义务按快照历史只读回推，无证据记 `unknown`；已存处置从不覆盖。回归见
  `tests/test_sol_obligation_disposition.py`。
- **指令面瘦身（实测字节，不等于注入量或 Token）**：`SKILL.md` 91,045 → 73,097（−19.7%，
  宿主单文件上限 100,000，余量 8,955 → 26,903）；`AGENTS.md` 23,973 → 15,221（−36.5%）；
  v9.7.1 及更早逐版本变更迁入 `references/history.md`（39,592 → 57,769，按需读取，不再进入
  Skill 正文）。口径：`SKILL.md` 正文只在调用 Skill 时载入（常驻的只有 description），
  `AGENTS.md` 按会话注入，两者不构成同一条“常驻注入”曲线；字节减少换算成的实际注入字符、
  Token 消耗与开发效率收益均**未测量**（`unavailable`），不得由字节数推导。
- **新增只读观测**：`scripts/efficiency_report.py`（`copilot-efficiency-report/v1`），只读
  台账 / 召回事件 / 宿主 `db.sqlite`，指标强制标注 `measured` / `proxy` / `unavailable`，
  不写文件、不调模型、不联网、不参与门禁。7 项单测覆盖口径与降级。
- **核查后无需改动的既有能力**：证据 SHA 重读、丢弃/篡改/跨任务拒绝、质量报告代码范围
  失效、`final_verification_digest`、PARTIAL 强制续跑与按项阻塞、证据哈希缓存、Hook 会话
  隔离与启动开销、SOL 有界义务执行——均有现有测试与真实宿主记录支撑。
- **保留边界（未新增门禁）**：普通 capture 证据不绑定代码快照，代码变更后不会自动失效；
  只有质量/verify 报告绑定 `code_scope`。报告内 `unavailable` 项（催促次数、提前停止、
  重复测试、压缩次数）当前事实源没有数据，不估算。
- **测试**：活动 956 项（含新增 7 项效能报告与 2 项处置保真回归）、legacy 853 项与 HEAD
  `b3ba3e7` 失败集合逐条一致（146 = 99 failures + 47 errors，历史 v5 行为测试，非本轮引入）。
- **同步**：`--apply` 后 `--check` 缺失 0 / 不同 0；`--baseline` 为 27 个 Git 跟踪文件
  （含 `scripts/efficiency_report.py`），`--verify-baseline` 通过。
- **未验证**：`efficiency_report.py` 的宿主库工具耗时 P95 是近似口径；未做同构任务的
  开发周期、返工率、Token 节省与用户接受度对照（见下节“最终验收补强”）。

### 最终验收补强（2026-11-24 验收，2026-11-25 随正式交付入库）

- **真实宿主验证（已做，最小充分）**：一次无头会话（`sess_7b8d02a1…`，`--mode yolo`，
  `copilot` Skill 调用 1 次），`SKILL.md` 注入 70,811 字节（不含 frontmatter）、
  `truncated=false`，注入内容同时包含开头/中段/结尾标志串（无静默截断头部）；会话未读取
  `references/history.md`；在该工作区完成 start → evidence → criterion → drive →
  closure-check → closure（`host-verify-2026-11-24`，CLOSED/pass/G1–G3 passed），中途 Stop
  退出码 2 触发续跑、capture 后放行。细节与取证见任务目录 `a2-host-verify.md`。
  本轮不覆盖业务任务与任务队列。
- **宿主验证中发现的缺陷（已修）**：`efficiency_report.py` 在**无 `SKILL.md` 的目录**下运行
  时 `skill_headroom = 100000 - None` 抛 `TypeError`（退出 1）。现缺文件即记 `unavailable`
  （`skill_bytes` / `skill_headroom` / `instruction_total_bytes` 一并），补 1 项回归；同一
  cwd 复核退出 0。另清理 `SKILL.md` 末尾重复的“## 版本”块（迁移残留，约 1.5 KB）。
- **基线保护（2026-11-25 已核销）**：交付前 `--baseline` 只记录 Git 跟踪文件，
  `efficiency_report.py` 曾不在 manifest（26 项）；交付阶段按固定顺序
  **暂存新增文件 → `--baseline` → `--verify-baseline` → `--check` → 分发相关测试** 执行，
  manifest 现为 **27 项**且含 `scripts/efficiency_report.py`，`--verify-baseline` 通过。
  交付前的 worktree 演练记录见任务目录 `a1-baseline-rehearsal.log`（该 worktree 已删除）。
- **验收口径**：可声明的是文件字节、注入字节（单次宿主实测）、台账/宿主库计数；
  **不可声明**的是“常驻注入下降 21.9%”“Token 节省”“开发周期/返工率改善”——无同构任务
  对照，保持未测量。宿主验证会话本身消耗 3.39M Token（含缓存读 3.28M），只作成本事实记录，
  不作效率结论。

## Hooks 必要性审计与性能优化（2026-11-24，工作区改动，未提交）

- **生效清单（核实到配置源头）**：用户级 `~/.zcode/cli/config.json` 只有三条钩子，均指向
  `copilot_stop_guard.py`：`Stop`、`SessionStart`（matcher `startup|resume|compact`）、
  `UserPromptSubmit`；无工作区级钩子，启用的 7 个插件都不贡献钩子。`PreToolUse: []` 是空
  死配置，已从用户级配置删除（只删该行，其它配置与权限位未变）。
- **必要性结论**：三项均保留——Stop 是宿主唯一“退出码 2 强制续跑”的机械衔接层；
  SessionStart 是 compact/resume 后唯一的状态恢复入口；UserPromptSubmit 是 BLOCKED 任务
  在“用户补齐授权当回合恢复执行”的唯一触发点。未注册的 `~/.zcode/hooks/design-gate` 与
  `~/.zcode/hooks/knowledge-loop` 为历史残留（无任何配置/规则引用），未删除，交用户处置。
- **性能修正（真实子进程 A/B，交错采样，中位数减解释器基线）**：`governance` 改为惰性导入
  （约 33 ms）+ 钩子高频路径跳过 `argparse`（约 11 ms）：无任务工作区 Stop 75.5→30.6 ms
  （-59.5%）、SessionStart 71.3→31.1 ms（-56.4%）、UserPromptSubmit 76.1→47.6 ms（-37.5%）；
  需要读状态的路径 75.8→70.5 ms、81.3→69.3 ms。判定口径未变。
- **上下文成本**：`user-prompt` 只展开本会话负责的任务，其它会话/未绑定任务收敛为一行计数
  （无关消息 1,445→0 字节）；本会话任务提示 1,421→1,229 字节（原四步判断规则并为两条）；
  Stop 续跑原因压缩但保留“领取工作项 / AUTO-REPLAN / disable / 强制续跑 / 3 次 / G1–G3 /
  不得改写 PARTIAL”等硬约束。
- **真实宿主验证**（`node /opt/ZCode/resources/glm/zcode.cjs -p|--resume` 配
  `COPILOT_STOP_GUARD_LOG`，共 **6 个无头会话**：plain / foreign / bound / cap / final /
  recall）：无任务工作区三事件均触发并放行；含其它会话任务的工作区
  `session-start-pointer:startup:1` 注入指针、`user-prompt` 0 注入、Stop `out-of-scope`
  放行；绑定任务的 `--resume` 会话 `session-start-inject:resume` →
  `user-prompt-inject:tasks=1` → Stop 退出码 2（`capture-required:CLOSED`）→ 宿主续跑并
  完成任务 → 第二次 Stop 放行，回合正常结束。
- **会话识别边界（本轮收口修复）**：宿主 `zcode.cjs` 在 SessionStart / UserPromptSubmit / Stop
  三处都由 `hookRunner.run` 注入 `session_id`（真实宿主 6 个无头会话共 31 次钩子调用，
  31/31 带 `sess_*`，无空值），无标识只出现在手工调用。原实现“无标识时按最近更新取一个任务”会把别人的任务强制续跑到当前上下文，已改为：
  有标识严格按绑定；无标识时**仅恰好一个候选任务**才回退；多个候选不选人（Stop 记
  `ambiguous-no-session` 并放行，UserPromptSubmit 只报数量、要求先 `status --json` 核对），
  但状态/绑定异常仍 fail-closed。新增 4 项回归（多任务不选、单任务仍续跑、异常不放行、
  逐回合只报数量）。
- **验证**：活动套件 `python3 -B -m unittest discover -s tests -p 'test_*.py'` **947 项 OK**
  （最终代码上重跑）；运行时副本（与源码 sha256 相同）用真实 stdin 载荷复跑 10 个会话场景
  矩阵（无任务 / 单任务·本会话 / 多会话并存 / 仅其它会话有任务 / 未绑定 / 绑定损坏 /
  BLOCKED+可执行并存 / 无标识多任务 / 无标识单任务 / 无标识多任务含损坏）全部符合预期；
  `sync_runtime.py --apply` 后 `--check` 缺失 0 / 不同 0，`--verify-baseline` 通过
  （基线已按最终源码刷新）。

## v9.8.0 多模态设计认知（2026-11-24，正式交付）

- **真实图片输入**：`sol-consult` / `sol-plan` / `sol-review` 的 `--image` 支持最多 6 张、
  单张 ≤8 MB、合计 ≤24 MB 的 PNG/JPEG/WebP；图片以 Responses API `input_image` 内容块
  与材料同轮提交，仍为零工具单轮。
- **审计边界**：只记录名称/类型/字节/尺寸/sha256/说明，不写 base64 与绝对路径；图片指纹
  进入请求摘要，换图即视为新材料；无图咨询不得冒充视觉评审。
- **协作协议**：设计判据归调用方（如 DesignWise），Copilot 只做认知调用、审计与采纳台账；
  意见不进 G1–G3、不改阶段、不是审批；可同时问缺陷、值得保留与创造性候选。
- **知识边界**：沿用现有 knowledge-manager / Memory 通道，不新建审美记忆系统；单个项目的
  色板/风格/材质/布局不自动成为其他项目的默认审美。
- **验证**：活动套件 `Ran 941 tests ... OK`；分发 26 个文件，运行时受管理文件缺失 0、
  差异 0；`references/design-review.md` 纳入 Git 跟踪与基线，`hm.txt` 为已知未受管理文件，
  不删除。

## v9.7.1 认知记录闭环：历史采纳回填、义务终态处置与议程观察口径（2026-11-24，已本地提交，未推送）

- **P1 基线（单一窗口）**：以 2026-11-23T23:33:59 的全量扫描为基线，不与其它时段混合。
  基线口径 104 次已完成 CONSULT/PLAN 调用、`pending` 44 条（`accepted` 40 / `modified` 20）；
  回填后同口径 109 次、`accepted` 43 / `modified` 20 / `rejected` 0 / `pending` 46。
- **P1 增量对账**：+5 次调用 = 本次补记历史复核的 2 条收口 CONSULT + 2 条新收口判断 +
  本任务 1 条启动 CONSULT；`accepted` 40 → 43 = 2 条新调用被采纳 + 1 条基线内已完成的调用
  （`SC010f0b43…`，23:31:53 完成）在 23:42:05 才登记采纳；`pending` 44 → 46 = −1（该次登记）
  +3（2 条历史复核 + 本任务 1 条）。三次差额全部对上，无“凭空增加/消失”的记录。
- **P1 回填结果**：已收口的 38 条历史 `pending` 全部登记为 `pending`/`historical-review` +
  原因码（`no-attribution` 34、`superseded-within-cycle` 3、`later-cycle-superseded` 1）；
  未收口的 8 条保持原状（rili 5、copilot 1、step-content-growth 1 及同项目 1 条），不写历史
  通道。连跑两次均为 38 条 `duplicate`、0 新增判决、0 覆盖，幂等成立。
- **P1 未解决**：46 条 `pending` 中 38 条带原因码但结论仍为待定（其中 34 条是“调用之后没有可
  归属的工程事实”，按证据门槛不回填）；8 条属未收口任务，依赖各自任务后续收口时按常规通道
  登记。任务计数口径存在 2 条差异（目录名去重 46 / `task_id` 去重 48），**如实保留、不调整
  分母**。
- **P3 时间线（只读还原，未改历史状态）**：`interstitial-stability-2026-11-23`、
  `commit-push-2026-11-23`、`zcode-memory-loop-2026-11-22` 各有 2 条 due 义务（A 启动、
  E 收口），`attempts=0`、无 `failure`、无 `deferrals`、任务内 0 次 CONSULT/PLAN 调用。
  可证明的机制事实：E 窗口在 3 s / 5 s / 45 s 内被同一命令物化并关闭
  （`same-command-close`）；A 窗口跨多条命令保持 `due` 2385 s / 29 s / 1395 s
  （`kept-due-across-commands`）。**原因不可确定**：无当时策略快照、无失败或延迟痕迹，无法
  区分策略关闭、未按建议执行或调度未发生；任务内无认知调用，也不构成“已被覆盖/被替代”。
  三条处置记 `not-executed` + 理由“窗口关闭时仍未执行；当时调度策略 unknown”，**不按当前
  `--sol auto` 倒推历史**。
- **P3 机制（最小实现）**：`cycle`（1 + 重开次数）、每周期首次同步写 `policy_snapshots`、
  窗口关闭时一次性写 `disposition` + 理由（只写不覆盖）、锁等待失败写 `deferrals` 并保持
  `due`、`_advance_windows` 按周期隔离前沿。历史义务的处置为**只读派生**、不回写：217 条
  义务中 212 条有终态（89 `completed` / 95 `skipped` / 28 `not-executed`），5 条仍开放（扫描
  时点 2026-11-24T00:13 的全库快照，不含本任务其后自身产生的义务）；全库
  `policy_snapshots` 为空，故所有历史 `not-executed` 理由均为 `unknown`。
- **P3 边界**：处置不是完成证明、不是门禁；`due` 不等于必须调用；`not-executed` 不阻断收口
  （`closure_readiness` 口径未变）；重开后的义务进入新周期，前后不互相覆盖；
  `closure-covered` 与收口后调用计数语义不变；`total` / `pending` / `missed` 与覆盖率分母
  口径不变。
- **P2 口径**：`agenda` 保持只读（不派生工作项、不进计划 SHA、不改 G1–G3、不增强制调用）；
  下一阶段只用真实任务已有事实做五类观察（改变决策 / 补充验证 / 已被覆盖 / 合理忽略 /
  被证伪），样本不足只记录不下结论，不预先实现议程状态机，不以议程或采纳条数为成功标准。
- **C6/C7 的精确口径**：已收口任务中**打开的 due 窗口为 0 条**；28 条历史 `due` 行
  （`window=closed`、`closed_reason=task-closed`）全部带显式终态处置
  （`not-executed` + `unknown`），无一条缺终态。6 条仍打开的 due 窗口全部位于**未收口**任务。
- **实跑发现的缺陷与修复**：本任务收口命令里实际多跑了一次 PLAN 调用
  （`SC41468606ea50f44053ffc70c`，其建议是“维持交付范围、不因规划窗口返工”）。根因是
  `_advance_windows` 用 `min()` 判断“最早已满足窗口”：同周期启动 CONSULT（A）几乎总先满足，
  于是同周期收口判断（E）对 B/D 的 `closure-covered` 覆盖**永不触发**。修复不改通用前沿
  语义，只加“同周期 E 已满足即覆盖 B/D”的判定，并新增回归
  `test_closure_coverage_does_not_depend_on_earlier_satisfied_window`。该缺陷属既有实现，
  v9.7.0 文档中“被覆盖窗口按 `closure-covered` 结算”在 A 已满足时不成立。
- **测试**：活动套件 `Ran 929 tests ... OK`（v9.7.0 的 894 + 35 项：历史回填 15、
  义务处置 20）。定向复跑 `test_docs_v6` 17、`test_sol_obligation_disposition` 20、
  `test_sol_acceptance_backfill` 15、`test_sol_scheduling` 84、`test_sol_think` 22、
  `test_context_engine` 44 全通过。按 SOL 收口判断的优先验证项新增两项反例：
  ① 授权重开后结算旧周期义务，不抵扣新周期 due、不追溯写处置，统计按周期归属；
  ② 新旧周期使用**同一义务键**时，迟到的旧周期事件只结算旧周期那一行。
  `legacy/tests` `Ran 853 tests`（99 F + 47 E + 32 skip），
  失败/错误集合与 `dab91e8` 上的 `git worktree` 实跑逐条一致（146 条，diff 为空）。
- **运行时与基线**：`sync_runtime.py --apply` 后 `--check` 受管理文件缺失 0 / 不同 0
  （运行时另有 1 个未受管理文件 `hm.txt`，按默认保留、未触碰）；`--baseline` 重建 25 个
  Git 跟踪文件的 `manifests/source-baseline.sha256`，`--verify-baseline` 通过。
  `python3 -m compileall -q scripts scripts-sync tests` 通过。
- **记录对齐（用户授权 reopen，已完成）**：周期 1 的收口（verdict=pass，
  `2026-11-24T00:42:25`）发生在上述修复之前。用户随后明确授权一次限定范围
  `reopen --authorized-by user`（`2026-11-24T07:29:06`，`authorization_source=caller-declared`，
  原收口记录与 `reopen_history` 保留、不删不改），仅用于修复后证据对齐与重新验收。按既有
  调度规则，`reopened` 事件触发一条动态 CONSULT（`SCc53a02c…`，义务 SO7 → `satisfied`），
  属策略义务且材料不含重开原因，不是重复调用。周期 1 的 E8/E16/E17/E18 采集于修复前、文件已
  被重写，按规则归档（文件保留不删），由最终代码证据取代：E20（活动套件 `Ran 929 tests` /
  `OK`，命令采集）、E21（legacy 853，命令采集）、E22（定向 205 全通过）、E23（legacy 与
  `dab91e8` worktree 双跑，146 条失败名逐条一致）、E24（套件结果行 + compileall + 同步 0/0 +
  baseline 25 校验）、E25（交付报告，收口前冻结）。
- **重新收口结果**：最终代码上的收口 CONSULT `SC6fcd6343f8aa308c46156df1`
  （2026-11-24T07:38:59，digest `f69147c387482847`，9,067 tokens）结论为“可按原合同收口、
  未见反例”；其保留项（重开授权依据、周期归属）已逐条核实并登记采纳。随后 `drive` 收口
  **CLOSED / PASS**（`2026-11-24T07:40:07`，`by=zcode-native`，G1–G3 全 PASS）。采纳记录：
  收口 E 与重开 C 为 `accepted`；两条周期 1 旧 E（`SC88e7d9d7…`、`SC914ca9f1…`）与缺陷触发的
  PLAN（`SC41468606…`）记 `modified`（方向一致、证据基础被替换或调用源于缺陷），**不计作
  accepted**。
- **覆盖行为对照（真实状态最小形状副本）**：旧代码（`dab91e8`）在 A 已满足时对同周期打开的
  B 一律保持 `due`（缺陷复现）；新代码只在**同周期 E 已满足**时写 `skipped`/`closure-covered`
  + `covered`，无 E 或 E 在另一周期时保持 `due`（无误覆盖、无跨周期覆盖）。周期 2 实跑中没有
  产生 B/D 窗口（无新的计划事件），故该分支的实证来自判定对照与回归测试，不冒充生产观测。
- **未验证**：本版未做真实宿主端到端（无新增宿主绑定或信封行为）；P2 观察需要后续真实任务
  样本，本轮无样本；8 条开放任务的 `pending` 只能由各自任务收口时处理；历史义务的处置是
  派生值，历史状态文件里的 `disposition` 字段仍不存在（按设计不回写）。

## v9.7.0 SOL 主动认知与实战决策闭环（2026-11-23，随同一次本地提交落库，未推送）

- **复盘范围与可复算事实（2026-11-23 只读统计）**：`/media/vdb/projects` 下 46 个任务状态
  文件、104 次已完成 SOL 调用（CONSULT/PLAN）；采纳登记 `accepted` 40、`modified` 20、
  `pending` 44（42% 未回填）。7 个任务在首次收口判断之后仍追加了非收口 CONSULT（各 1 次），
  即收口后补 B/D 的时序缺陷；`blockage-change` 在旧口径下由“阻塞解除”触发，属噪声事件；
  同一任务重复问同一问题的根因是材料里没有上次判断、采纳与未处理议程。
- **主动议程（CONSULT/PLAN 新输出）**：输出契约新增可选 `agenda`（1–6 条，固定类型
  `core-question` / `wrong-premise` / `missing-risk` / `completion-gap` / `decision` /
  `verify` / `counterexample`），`review_policy.normalize_agenda()` 做确定性归一（去空、
  截断 240 字符、限 6 条、去重）。`evaluate` / `status` 只读呈现 `sol_agenda`，不派生工作项、
  不进计划 SHA、不影响 G1–G3。
- **认知连续性**：调度材料升为 `copilot-sol-schedule-materials/v3`，新增“上次 SOL 判断与
  采纳”（最近 3 次判断的核心结论＋采纳状态）与“未处理议程”；提示词（`sol-prompt-v5`）
  注入同一事实。SOL 因此做增量判断而不是从零重问。
- **调度顺序修正**：同一收口周期（合同＋重开次数）已有收口判断后不再建议 B 规划或 D 阶段
  复盘；最后一批进展打开 B/D 又立即收口时记 `skipped/closure-covered`（合理跳过，不算漏调）；
  仍可由升级型事件（C）或新的最终验证摘要（E）打开新窗口。
- **事件质量**：新增 `blockage-change`（只在“仍未解除的阻塞”新增或实质变化时触发，解除不
  触发）与 `repeated-work-failure`（同一工作项连续失败），两者进入 `SOL_EVENT_TRIGGERS`
  与 `question_for`；阻塞解除不再制造无认知价值的事件。
- **采纳可见**：`evaluate` 输出 `sol_pending_acceptance` 并给出 `sol-accept` 回填命令，
  `status` 打印待回填条数；`sol-stats` 新增 `agenda`（calls/items/open）、
  `pending_acceptance`、`post_closure_calls` 观察值（不构成价值证明）；`post_closure_calls`
  只统计“收口判断之后、下一次授权 reopen 之前”的区间，重开后的合法调用不计为缺陷。
- **经验衔接**：`context_engine` 的 capture 草稿新增“SOL 认知与采纳”章节（判断、采纳、
  决策/差异、议程，`sol_cognition_calls` 计数），标注“任务内事实，不是可复用结论”，
  Capture 口径仍为 v1，不写 Vault 与原生 Memory。
- **测试**：活动套件 `Ran 894 tests ... OK`（68.8 s，v9.6.2 的 855 + 39 项新增：议程归一、
  材料历史/议程、状态只读呈现、收口后不开 B/D、`closure-covered` 合理跳过、事件质量、
  stats 新字段、capture 溯源）。`legacy/tests` 失败/错误集合与 `HEAD` 基线一致（详见交付
  报告；不含新增回归）。运行时 `--check` 缺失 0 / 不同 0（分发 25 文件），
  `--baseline` 25 个 Git 跟踪文件、`--verify-baseline` 通过。
- **真实宿主 E2E（两个临时工作区，真实 `gpt-6.2-sol`）**：
  - `feature`/medium、实际复杂度 low：A 启动 CONSULT `SC395c19d1a87ba6ac8eee7a1c`
    （33.7 s / 3,227 tokens，议程 3 条）；B 未准入（`plan_admission.required=false`），
    0 次强制规划，符合“简单任务不强制 CONSULT/PLAN”。
  - `multi-option`/medium：A `SCc131ae8195c34e003304c036`（37.4 s / 3,336 tokens，
    议程 3 条）→ `sol-accept accepted` → 同一命令内自动执行 B
    `SCf9978bad88b8bbd47915b121`（44.8 s / 3,976 tokens，议程 2 条），材料 2,242 字符，
    实测含“上次 SOL 判断与采纳（accepted）”与“未处理议程”两节；`sol-stats` 输出
    `agenda={calls:2, items:5, open:2}`、`pending_acceptance=1`、`post_closure_calls=0`；
    capture 草稿带 SOL 认知章节（`sol_cognition_calls=2`）。全程每条命令最多 1 次请求。
  - 本任务自身同样走真实生命周期：A `SC010f0b4379d4f18cca55c753`（48.7 s / 6,843 tokens）、
    B `SCac1ecdb143195b177cb0ebdc`（67.5 s / 7,898 tokens）、E
    `SC6380eac3a630f2652803f6fb`（38.9 s / 7,939 tokens，最终验证摘要 `56b2fc32e1895140`）。
    E 明确区分“机制可验证”与“真实收益未证明”，并指出统计口径不可直接比较（已按 E9 取证）。
- **边界**：本轮未 commit、未 push、未发布、未改 Provider/模型/安全配置、未新建 Agent 或
  工具权限、未恢复 G4、未改 G1–G3；任务级 `.copilot-tasks/` 数据不入库。

## v9.6.2 全面评审与针对性优化（2026-11-22，已交付 `main`）

- **P1 缺陷（已修）**：`sol-stats` 任务级窗口分子/分母单位不一致——真实任务
  `closure-timing-2026-11-21` 曾输出 `coverage.closure.rate=3.0（3/1）`（多轮 reopen 的 3 次
  收口判断 ÷ 1 个任务）。现按任务计 1 次（满足＝执行或复用），义务条数与执行/复用/跳过/
  漏调分项保留在 `by_family`；工作区聚合复查 `closure 2/9=0.2222`、所有 rate ≤ 1，
  `totals.closure_eligible_tasks` 新增且可复算。
- **P1 缺陷（已修）**：事件（C）分母包含合理跳过窗口，与 `_expire_superseded` / `sync`
  的跳过口径不一致；现分母＝识别事件数 − 合理跳过数，漏调仍单列。
- **P1 缺陷（已修）**：文档承诺自动调度“有界执行”，但 `_obligation_argv` 从不传
  `--lock-timeout`，`task_lock(timeout=None)` 会无限等待另一进程的 SOL 请求。现自动路径固定
  `AUTO_LOCK_WAIT_S`（120 s，`COPILOT_SOL_LOCK_TIMEOUT` 覆盖），拿不到锁时义务保持 `due`、
  不付费、退出码 0；`sol-review` 同步接受该参数。
- **P1 性能（已改）**：`sol_review` 的网络栈改为请求路径内惰性导入；同机交替 A/B 中位数
  （先 `compileall`，每条命令 `-B`）：`import copilot_dispatch` 98.2→80.0 ms，`status`
  155→127 ms、`closure-check` 133→110 ms、`sol-stats` 129→112 ms、`handoff` 149→132 ms
  （明细 `evidence/notes/perf-ab.txt`，E3）。
- **P2 已处理**：CLI 文档/帮助、SKILL、`references/*`、README 与实现对齐（生命周期命令在
  `--sol auto` 下会执行 1 条义务；收口顺序＝最终验证 → E → `sol-accept` → G1–G3；D 为
  `recommended`；风险不再派生 REVIEW；钩子 0/2 与 exit 0 分开描述；`--reviewer` 默认
  `zcode`）；清理 25 个零引用且无动态访问的符号（明细见 `SKILL.md` v9.6.2）。
- **测试**：新增 5 项回归（任务级窗口不超过 100%、事件分母排除跳过、全跳过为 N/A、
  自动路径锁等待有界 + 三个 SOL 子命令都接受该参数、新验证摘要不被旧收口判断/旧验收覆盖）
  与 1 项架构约束（SOL 网络栈惰性导入）；最终口径 `tests/test_sol_scheduling.py` 72 项，
  双版本全量 `Ran 855 tests ... OK`（3.12 / 3.10）。
- **SOL 调度闭环（真实调用）**：A 启动 CONSULT `SC6a8b166b0f9bd72f71c5843e`（SO1）、
  B 调查后 PLAN `SC61f1831bf3725c04609e0031`（SO2）、E 收口 CONSULT
  `SC5a509ce03aace685da7a07c4`（SO3，摘要 `7357716c5786ce3a`）；ZCode 验收
  `accepted`，随后 G1–G3 收口 `verdict=pass iteration=I0 by=zcode-native`。
  调度义务 3 个、未闭合 0、漏调 0；`sol-stats` 的 startup/plan/closure 覆盖率为 1.0/1.0/1.0，
  事件窗口分母为 0 时 `rate=null`（不再伪造 100%）。REVIEW 未发起（用户授权为可选）。
- **交付（2026-11-22，已完成）**：分支 `fix/copilot-v9.6.2-review-optimization` 提交
  `326b81c`（17 文件，+460/−229）→ push → PR #2 → PR CI run `44969511767` 的
  `Python 3.10` / `Python 3.12` 各 `Ran 855 tests ... OK`（`skipped=1`：runner 未安装
  knowledge-manager 的环境跳过）且 `Verify manifest baseline` 通过 → `mergeStateStatus=CLEAN`
  后 Squash 合并为 `8f51037`（`mergedAt=2026-11-22T01:03:55Z`，未绕过 Ruleset：其
  `updated_at` 早于本次交付、`bypass_actors=[]`）→ 合并后 `main` push CI run `44969658309`
  再次两版本 855 OK + baseline 通过 → 删除 feature 分支。`git diff 326b81c 8f51037` 为空，
  即 Squash 结果与已验证的分支头内容一致；当时本地 / `origin/main` / GitHub
  `refs/heads/main` 三方同为 `8f51037`、ahead/behind 0/0、工作区 clean。
  其后仅有记录性文档提交（PR #3 `3e222a3` 只改本文件，代码树不变）；最新 tip 以
  `git log -1 main` 为准，不在本文内固化。
- **运行时（合并后核实）**：以最终 `main` 执行 `--check` 为缺失 0 / 不同 0（分发 24 文件，
  未受管理 `hm.txt` 保留），24 个受管文件逐个 SHA-256 与源码一致，`--verify-baseline` 通过；
  合并只改变提交身份、不改变受管内容，故未重复 `--apply`；源码与运行时 `--version` 均为
  `9.6.2`。
- **边界**：未新建 Release、未移动 `v9.6.1` Tag/Release（annotated tag 对象仍
  `bf3eaa90…` → commit `c5a3e1c6…`，`publishedAt=2026-11-21T15:00:56Z`），未改 Ruleset、
  Provider、模型或账户配置。评审轮的真实 SOL 调用见上（A/B/E 三次）；交付轮另有 A
  `SC37688f023c0e656b45e48cc1`、B `SCbb3fc84bccca0e38126f0ccc`、E
  `SCca0f42037e01ccca9667c364`（摘要 `e9f0c2a92f1ea4e7`）与一次 D 阶段复盘；任务
  `v962-delivery` 收口为 `verdict=pass iteration=I0 by=zcode-native`（C1–C14 14/14 met，
  证据 E1–E8）。

## 收口、main 分支保护与 v9.6.1 Release（2026-11-21）

- **旧任务收口**：`open-source-ci-20261121` 阶段 **CLOSED**，完成条件 14/14 met，工作项 20/20
  终态，G1–G3 通过（`verdict=pass iteration=I1 by=zcode-native`）。邮箱隐私按用户决定
  修订条件并以用户授权解除阻塞（详见下节），未把 noreply 配置冒充为已完成，也未删除条件
  制造 PASS；原阻塞、失败尝试与证据（E1–E19）完整保留。
- **main 分支保护（Ruleset `27797379`，`main-protection`，active）**：目标
  `~DEFAULT_BRANCH`；规则 `deletion`（禁止删除）、`non_fast_forward`（禁止强推）、
  `pull_request`（`required_approving_review_count=0`、无 Code Owners、无最后推送审批、
  无强制讨论解决、允许 merge/squash/rebase）、`required_status_checks`（
  `Python 3.10` / `Python 3.12`，`strict_required_status_checks_policy=false`）。
  `bypass_actors=[]`，`current_user_can_bypass=never`——维护者同样不能绕过。
- **单人开发流程**：`feature branch → PR → CI（Python 3.10 / 3.12）→ 合并`；0 人工审批，
  无需自审或第二个 Reviewer，不会因缺少审批而死锁。main 不再接受直接推送：本地
  `git push origin main` 会被拒（`GH006 protected branch`），需改走 PR。紧急维护路径是
  临时修改/停用该 Ruleset 再推送，属可审计的仓库设置变更，不是日常绕过手段。
- **v9.6.1 正式发布**：Annotated Tag `v9.6.1`（对象 `bf3eaa90…`）指向提交
  `c5a3e1c6ac3cd9fae9bc6e1a48af753a536a2e88`（该提交的 CI 运行 `44915400161` success）；
  GitHub Release **Published**（非 draft、非 prerelease、无附件）：
  <https://github.com/tongsailong/copilot/releases/tag/v9.6.1>，发布时间
  2026-11-21T15:00:56Z。未登录访问：Release 页面 200、源码归档
  `archive/refs/tags/v9.6.1.tar.gz` 可下载（1,129,874 字节）。此前的 `v9.6.1` Tag/Release
  不存在，非重复创建；未移动任何既有 Tag。
- **运行时**：本轮只改 GitHub 配置与文档，未改受管源码；`--check` 缺失 0 / 不同 0、
  `--verify-baseline` 通过、`copilot_dispatch.py --version` = 9.6.1。

## 开源维护专项：公开记录修正与 GitHub Actions CI（2026-11-21）

- **blob 统计复算（公开口径）**：以 `refs/heads/main`（= 当前全部公开 refs，单分支、无标签）
  为范围，按 Git 对象类型验证去重，可达唯一 **blob = 823**；同一范围总对象 1,204
  （823 blob + 314 tree + 67 commit），本地对象库另有 5 个不可达 blob。复算命令：
  `git rev-list --objects refs/heads/main | awk '{print $1}' | git cat-file --batch-check='%(objecttype) %(objectname)' | awk '$1=="blob"{print $2}' | sort -u | wc -l`。
  观测时间 2026-11-21T14:17Z。逐提交检查 67 个提交，可达对象数为 1,156–1,204、blob 数为
  776–823，**没有任何提交等于 1,118**；此前记录的「1,118 个可达 blob」不可复现，已由 823
  取代。另一处 `+1133/−101` 是 v8.5.0 提交 `00bc903` 的 diffstat，不是 blob 统计。
  匿名完整克隆（见下）得到相同的 823，交叉验证成立。详见 `docs/DECISIONS.md` 第 78 条。
- **匿名 Clone（已取证）**：公共 HTTPS、隔离 `HOME`、`GIT_CONFIG_NOSYSTEM=1`、
  `GIT_CONFIG_GLOBAL=/dev/null`、`credential.helper` 置空、`GIT_TERMINAL_PROMPT=0`、
  `GIT_ASKPASS=/bin/true`、未传入任何 token 环境变量；本机到 `github.com` 的 pack 传输不稳定，
  仅用本机代理 `127.0.0.1:7897` 做无认证传输。浅克隆与完整克隆均成功：
  `HEAD = 9f3d561ac67ae9cb0c9ae2a7b9420ff66b4d0d23`、默认分支 `main`、67 个 commit、
  96 个跟踪文件、823 个可达 blob，`README.md` / `LICENSE` / `scripts/governance.py` 可读，
  `git ls-remote` 的 `refs/heads/main` 与 HEAD 一致。观测时间 2026-11-21T14:24Z。
  CI 建立后按同样条件再次匿名完整克隆（观测时间 2026-11-21T14:42Z）：
  `HEAD = 829ac4261e79ca2f1302cb6e4e0c3e1b1585cf42`、`main`、69 个 commit、97 个跟踪文件、
  830 个可达 blob，`.github/workflows/ci.yml` 在克隆中存在。823 是提交 `9f3d561` 的
  公开口径，随提交增加而增长，不是固定不变的总数。
- **记录口径（自引用限制）**：提交无法包含自身 SHA。本文件记录的是**取证时已核验的公开 tip**；
  写入该记录的文档提交本身必然晚于它。发布流程沿用既有惯例：代码提交 → 文档提交 →
  发布记录提交，不通过 amend/rebase/force push 制造同一提交内的自洽。
- **GitHub Actions CI（已建立）**：`.github/workflows/ci.yml`，`push` / `pull_request` 指向
  `main`；Python **3.10 / 3.12** 矩阵；执行编译检查、活动套件
  （`python3 -B -m unittest discover -s tests -p 'test_*.py'`，本地基线 849 项）与
  `sync_runtime.py --verify-baseline`。边界：`permissions: contents: read`、
  `persist-credentials: false`、不使用 `pull_request_target`、不引用 Secrets、不调用 SOL、
  不访问本机运行时或知识库、不做运行时同步/提交/发布。Action 固定到完整 commit SHA
  （`actions/checkout@3d3c42e5…` v7.0.1、`actions/setup-python@6d4fd22b…` v7.1.0）。
- **真实 CI 运行（通过）**：首次运行（`44914580721`，提交 `8a87a2e`）在两个 Python 版本上
  各失败 3 个用例——子模块 worktree 内 `git commit` 退出 128，因为子模块 config 不继承源仓库
  身份、托管 runner 无全局 `user.name/user.email`，而本机有全局配置故此前被掩盖。修复：
  `tests/test_code_scope_unified.py` 的 `run()` 注入 `GIT_AUTHOR_*` / `GIT_COMMITTER_*`
  环境变量（断言与用例语义未改），并在清空身份变量、`HOME` 为空、
  `GIT_CONFIG_GLOBAL=/dev/null`、`GIT_CONFIG_NOSYSTEM=1` 的环境下本地复现验证。
  第二次运行 **`44915220688`**（提交 `829ac42`）**success**：Python 3.10.22 与 3.12.16
  各 `Ran 849 tests` / `OK (skipped=1)`，两个 Job 的编译检查与 `--verify-baseline` 均通过。
  页面：<https://github.com/tongsailong/copilot/actions/runs/44915220688>。
- **邮箱隐私（已由用户决定关闭）**：2026-11-21 用户明确决定接受现有 Git 作者邮箱公开，
  不再要求 noreply、不清洗历史，该项由外部阻塞转为已决事项（历史记录保留：设置尝试、
  `gh` token 无 `user` scope、API 无该开关、Chrome 无登录态、未拼接地址）。
  合同条件 C4 经 `contract --amend --authorized-by user` 修订为「按用户决定处理」，
  阻塞以 `unblock --by user` 解除，任务 `open-source-ci-20261121` 收口为 **CLOSED / PASS**
  （14/14 条件、20/20 工作项终态、G1–G3 通过）。
- **运行时**：未改 v9.6.1 任何受管文件；`--check` 缺失 0 / 不同 0、
  `--verify-baseline` 通过、`copilot_dispatch.py --version` = 9.6.1。

## 正式交付与公开发布准备（2026-11-21）

- **交付**：提交 `2981a3f`（20 个文件，+4511 / −140），普通快进推送 `75a6fa1 → 2981a3f`；
  本地 HEAD、`origin/main`、`git ls-remote` 三方一致，ahead/behind 0/0。
- **测试（最终代码）**：`discover -s tests` **849 OK**（含 `test_sol_scheduling` 67 项）；
  `legacy/tests` 853 项 = 99 F + 47 E + 32 S，与 HEAD 干净工作树的失败集合逐条 diff 为空
  （同基线、无回归）。历史套件非全绿属 v5 契约保留，不作为当前判定依据。
- **运行时**：`--check` 缺失 0 / 不同 0；manifest 24 个文件逐个 SHA-256 与运行时一致；
  `copilot_dispatch.py --version` = 9.6.1；`--verify-baseline` 通过。
- **manifest 23 → 24**：`scripts/sol_schedule.py` 此前未跟踪，`--baseline` 只记录 Git 跟踪文件
  因而漏记；本次随提交进入基线后重建为 24 项。此后新增分发文件必须先 `git add` 再建基线。
- **公开前审计**：该轮以「1,118 个可达 blob」为口径 + 当前工作树全量扫描，无真实凭据/私钥/
  Token/连接串；**该数字不可复现，已由本轮按对象类型验证的 823 取代**（见上节与
  `docs/DECISIONS.md` 第 78 条）。`assign-secret-long` 命中均为显式假夹具；单分支、无标签、
  无 Actions、无 Release、单协作者；依赖仅标准库（可选 `Pillow`；`PyYAML` 仅 legacy 测试），
  无第三方代码与版权声明。详见 `docs/DECISIONS.md` 第 77 条。
- **文档脱敏**：62 处替换（个人署名/邮箱、本机绝对路径、内部宿主入口、跨项目业务名 →
  通用描述），二次运行 0 处；原始内容可由 `2981a3f` 恢复，未改写历史。
  代码内本机默认路径（`context_engine` / `knowledge_handoff`）按第 77 条保留不动。
- **公开文档**：新增 `LICENSE`（MIT，版权主体 `tongsailong`）、`SECURITY.md`、
  `CONTRIBUTING.md`；`README.md` 重写为公开说明（角色边界、依赖、安装、最小闭环、SOL 配置、
  安全边界、已知限制、版本演进）；`.gitignore` 增补 `reports/`。
- **可见性变更（已执行）**：`gh repo edit --visibility public` →
  `visibility=PUBLIC`、`isPrivate=false`、`licenseInfo=MIT`；同时设置仓库描述与 7 个 topics。
  公开后启用 **secret scanning + push protection**（只增强未降低）；`dependabot security
  updates` 保持 disabled（无第三方运行时依赖）；`allow_forking=true`、issues 开、projects 开、
  wiki/discussions 关、单协作者 admin。
- **未登录可读验证**：`raw.githubusercontent.com` 的 README / LICENSE / SECURITY.md /
  CONTRIBUTING.md / SKILL.md / `scripts/governance.py` / `docs/DECISIONS.md` 均 HTTP 200；
  仓库页匿名可读（title、描述、MIT 许可、文件树与 README 正文渲染）；匿名 `git clone`
  （显式禁用 credential helper 与交互提示）成功且当时 `HEAD = f001e0b`；`info/refs` 200。
  该记录随后被发布记录提交 `9f3d561` 取代；最新匿名取证为 `HEAD = 9f3d561`（见上节）。
  匿名 REST API 因共享出口 IP 触发限流（403），改用 HTML 与 git 协议取证，不视为失败。
- **不可撤回**：公开后他人可能已克隆或 fork，改回 Private 无法收回；用户已明确知悉并接受。
- **运行时不受影响**：`--version` 9.6.1、`--check` 缺失 0 / 不同 0、`--verify-baseline` 通过。

## 收口时序精准修复（2026-11-21，v9.6.1）

- **根因（现场证据）**：`review_policy.sol_advice` 的 E 分支只看 `all_done`（工作项是否
  耗尽），`copilot_dispatch._sol_schedule_step` 固定传 `gates_passed=False`，没有任何入口
  读“合同要求测试是否跑过、最终证据是否登记、条件是否核销”；`close_task` 只校验 G1–G3，
  不含合同 `test_policy`。因此最后一个工作项一 `record done`，E 就可能在下一条生命周期
  命令里立刻执行：SOL 拿到的是缺测试、缺证据、甚至条件未核销的收口材料。`register_evidence`
  还从不写 `capture_cmd`（死字段），所以“测试跑过”本身也无法核对。
- **实现（统一准入 + 验证摘要）**：`governance.closure_readiness(state)` 是 E 与收口的唯一
  准入（便宜的缺口事实优先，其后才是 `verification_gates`：G1 工作项、G2 条件/证据/台账、
  G3 无停摆；**不含**质量报告的用户接受）；`closure_verification_digest()` 只取验证事实的
  `sha256[:16]`（合同指纹、条件状态与引用证据摘要、终态工作项、带 `capture_cmd` 的采集证据、
  报告摘要），作为 E 义务 instance 与调用记录的 `final_verification_digest`。`test_verification_evidence`
  要求采集命令按词元顺序匹配合同声明（允许 `-B` 等运行器参数），自述报告不算执行痕迹。
- **入口一致**：`drive` 在缺口时打印缺口、返回 `EXIT_EXECUTE`（10）、零调用；`closure --close`
  在缺口时退出码 1；手动 `sol-consult --stage closure` 锁外预检 + 锁内复核两处拒绝，义务
  instance 与当前摘要不符时拒绝付费；`close_task` 对无测试证据的任务拒绝收口；`evaluate`
  的 `continuation` 给 `verify` 动作，`status` 显示“最终验证：未完成（摘要 …）”与缺口。
- **真实宿主端到端（真实 SOL 调用，不是替身）**：夹具任务 `task-72bcc554e6f6c3a7`
  （`/tmp/e2e-v961/state.json`，`test_policy.required=true`，
  commands=`["python3 -m unittest"]`）按 `start → plan → record ×2 → criterion ×2`
  （`--sol advise`）推进：① 工作项与条件齐备但无测试证据时，`closure --close --sol auto`
  **退出码 1**、`drive` **退出码 10**、`sol_calls=0`（E 未提前执行）；② 真实执行
  `python3 -B -m unittest discover -s tests` 并用 `evidence --capture-cmd` 采集登记后，
  同一条 `closure --close --sol auto` 自动执行 E：`SCf8876646268d06f1fba60bfd`，
  mode=CONSULT、stage=closure、model=gpt-6.2-sol、effort=high、zero_tool=true、
  `final_verification_digest=40424cfef8a47317`（与义务 SO2 instance 一致）、
  `materials_sha256=4e4e57f980a487304fca320ef7d1a926f58fabdadc65bc138701bac4c5392657`、
  status=completed、27,043 ms、3,045 tokens；③ 历史顺序 `sol_call_completed` →
  `sol_schedule_sync` → `task_closed`，义务 SO2 `satisfied/executed/window=closed`，
  `closure.sol_entry=closure-command`，`sol-stats` 的 `coverage.closure` 1/1。
- **测试（对应最终代码）**：完整活动套件 `python3 -B -m unittest discover -s tests` **843 项
  OK**（v9.6.0 的 824 + 本轮新增 19；证据 E6。更早的 E5 是同一条命令在 manifest 过期时的
  红跑，修基线后由 E6 取代）；`tests/test_sol_scheduling.py` 61 项 OK（含
  `ClosureAdmissionTests` 19 项：末项关闭无测试不收口、仅自述/无 `capture_cmd` 不解锁、条件未核销等待、完成后执行、
  同摘要复用不二次付费、重跑后旧结论失效、重新验证后允许新 E、`drive` 缺口不执行、
  `criterion` 重估准入、`closure --close` 拒绝绕过、`close_task` 拒绝、E 在收口之前的顺序、
  手动收口 CONSULT 拒绝、摘要变化拒绝、失败降级、风险只作 advisory、`-B` 词元匹配、异运行器
  不匹配）；受影响套件定向回归 448 + 181 项 OK（`test_sol_first`/`test_sol_trigger`/
  `test_continuation`/`test_governance`/`test_v93_hardening`/`test_v671_quality`/
  `test_pww_acceptance`/`test_v64_convergence`/`test_v62_convergence`/`test_stop_guard`/
  `test_xmc_adaptation`/`test_sol_fusion`/`test_review_efficiency`/`test_efficiency_v611`/
  `test_sol_think`/`test_v95_scheduling`/`test_sol_review_v7`）。未机械重跑全部 824 项。
- **同步与基线（最终代码）**：`--apply` 写入 9 个文件（`SKILL.md`、`references/review.md`、
  `references/runtime.md`、`references/workflow.md`、`scripts/copilot_auto.py`、
  `copilot_dispatch.py`、`governance.py`、`review_policy.py`、`sol_schedule.py`），
  `--check` 缺失 0 / 不同 0（`hm.txt` 未受管，默认保留）；`--baseline` 23 个 Git 跟踪文件、
  `--verify-baseline` 通过；运行时 `__version__=9.6.1`、运行时 `SKILL.md` 标题
  “copilot：轻量协作治理（v9.6.1）”，`zcode skills list` 读到的是同一份运行时文件。
- **观察（未改动的既有口径）**：收口命令内 E 完成后，`close_task` 的收口结算会用“同合同
  已有 CONSULT 覆盖”的既有复用规则把未执行的 A 窗口记为 `satisfied/source=reuse`，因此
  `sol-stats` 的 `coverage.startup` 在该夹具里是 1/1。这是 v9.6.0 的复用口径，本轮未修改，
  也不影响 E 的时机与收口顺序。
- **未验证 / 边界**：未做并发压测（锁语义由既有 `LockBusyError` 用例覆盖）；未重演
  “用户接受 PWW 之后再收口”的完整真机链路（该阶段刻意不进入 E 准入，由既有 PWW 用例覆盖）；
  未提交、未推送、未发布。
- **收口后的补充修复（材料合同正文，2026-11-21）**：本任务自己的收口 CONSULT 在 risks 里
  指出“合同正文显示未登记”。现场核查为真实缺陷：`sol_schedule.build_materials` 读
  `contract["body"]`，而合同正文实际存放在 `contract["done_criteria_text"]`（与
  `sol_review.build_prompt` 同源），所以**每份义务材料**都印出 `合同正文摘要：None`。
  已改为读 `done_criteria_text`（保留 `body` 作为旧状态回退），正文缺失时显式写
  “（未登记）”，并新增 2 项定向测试（`test_materials_carry_contract_body_instead_of_none`、
  `test_materials_mark_absent_contract_body_as_unregistered`）。修复后在同一代码上重跑：
  完整活动套件 **845 项 OK**（74.3 s）、`test_sol_scheduling` **63 项 OK** + 受影响套件
  **150 项 OK**（`test_sol_first`/`test_sol_trigger`/`test_stop_guard`/`test_xmc_adaptation`）、
  legacy **853 项 99F/47E/32S（146，与非修复基线相同）**；`--apply` 写入 1 个文件、
  `--check` 0/0、`--baseline` 23 个 Git 跟踪文件、`--verify-baseline` 通过。该修复发生在任务
  `task-5cc94126d942b517` 收口之后：工程改动、定向测试、完整套件与运行时同步已重做，
  但**该任务的 CLOSED 记录与 `final_verification_digest=df4d97f58ca0b794` 仍指向修复前的
  代码状态**；要把记录也对齐到修复后代码，需用户授权 `reopen` 后重新验证并重跑收口
  CONSULT（`reopen` 要求 `--authorized-by user`，不自报）。
- **收口记录对齐与验收（2026-11-21，同一 v9.6.1，用户授权 reopen 两次）**：把“材料修复、
  验证证据、SOL 收口判断、ZCode 验收、G1–G3 收口”对齐到同一份最终代码，并按真实事件核对
  顺序，而不是按报告文字。
  - **材料口径进入验证摘要**：`MATERIALS_SCHEMA` 升到 `copilot-sol-schedule-materials/v2`
    并纳入 `closure_verification_digest()`（材料内也自报口径）。修复只改材料生成代码时验证
    事实不变、旧摘要不变，旧 E 会被 `reuse` 覆盖到新材料上——这是本轮实测确认的口径缺口；
    现在材料口径变化会自动打开新的收口判断窗口。
  - **ZCode 验收的顺序缺口（真实路径，已修）**：第一次重开（I1）里 E 执行后 `drive` 直接
    `close_task`，收口后再 `sol-accept` 被 `_assert_open` 拒绝（“任务已收口，不能登记”），
    即自动路径把“ZCode 独立验收”这一步结构性跳过了。新增
    `governance.closure_acceptance_pending()`：当前最终验证摘要的收口 CONSULT 没有
    `accepted/modified/rejected` 记录时，`close_task` 拒绝收口、`continuation` 给
    `action="accept"`、`drive` 与 `closure --close` 停在 `EXIT_EXECUTE`/`EXIT_CONTINUE` 并打印
    `sol-accept` 命令。验收不是批准权：三种结论都能继续，SOL 仍不是门禁。
  - **E 与 G1–G3 的真实顺序（审计事实）**：I0 为 `evidence E7 21:02:30` → `A3 done 21:02:32`
    → `sol_call_completed SC23ca16ca… 21:03:15` → `task_closed 21:03:15`；I1 为 E
    `SCa76afbf…` 先于 `task_closed 21:23:24`。两个迭代都是“验证 → E → 收口”，E 从未等待
    G1–G3 全绿；缺的是 E 与收口之间的验收记录，本轮由上面的准入补齐。
  - **重开**：两次 `reopen --authorized-by user`（`authorization_source=caller-declared`）保留
    历史调用/证据/条件/收口记录，分别开启 I1、I2；`iteration` 参与摘要，重开后旧摘要失效，
    `reopened_without_progress` 要求重开后有新进展。
  - **测试（对应最终代码）**：完整活动套件 **849 项 OK**；`test_sol_scheduling` **67 项 OK**
    （新增“验收前不得收口/三种结论都可继续/收口后不能再补登记/材料口径变更使旧结论失效”
    等用例）；受影响套件 **292 + 263 项 OK**；`test_v64_convergence` 等其余模块随完整套件
    通过；legacy `legacy/tests` 146 项与非修复基线相同（99F/47E/32S）。
  - **最终真实链路（I2，全部由调度器产生，无手工记录）**：`reopen --authorized-by user`
    （21:29:00，I2，摘要从 `3ec6a26e2d1fc61a` 变为新值）→ 登记最终代码验证证据
    E10（`discover -s tests`，**849 项 OK**，21:32:47）与 E11（`discover -s legacy/tests`，
    853 项 99F/47E/32S，21:33:53）→ `drive --sol auto` 执行真实收口 CONSULT
    **`SC2fd44c691e57a5804eb4532e`**（21:34:31，mode=CONSULT、stage=closure、completed、
    model=`gpt-6.2-sol`、reasoning_effort=high/standard、**zero_tool=true**、
    `materials_sha256=0fead3a9f3c8d7d3…`（5891 字符，材料口径 v2）、
    `final_verification_digest=9a2989de52710787` 与义务 SO9 instance 一致、33,780 ms、
    5,218 tokens）→ `drive` 停在等待验收（`EXIT_EXECUTE`，未收口）→ ZCode 独立验收
    `sol-accept … --status accepted`（决策/依据/证据 E2、E9、E10、E11 全部登记，
    含对“摘要绑定”“legacy 非全绿”“合同正文未登记”三条的逐条核实）→ `drive` 执行
    G1–G3 收口（21:35:08，iteration=I2，verdict=pass，`closed_at` 同摘要）。
    顺序即「最终验证 → E → ZCode 验收 → G1–G3」，可直接从 `state.json` 的 `history`
    （`sol_call_completed` → `sol_call_acceptance` → `task_closed`）复算。
  - **同步与基线**：`--apply` 下发本轮改动（代码 3 个文件、文档 2 个文件；再执行写入 0 个），
    `--check` 缺失 0/不同 0（`hm.txt` 未受管，默认保留）、`--baseline` 23 个 Git 跟踪文件、
    `--verify-baseline` 通过；运行时 `__version__=9.6.1`。E10/E11 采集后仅追加本文档段落
    （不改代码与测试），随后重建基线并复验 manifest 用例；未提交、未推送、未发布。
  - **观察（记录，未改动，非门禁）**：① 本任务 `SO2`（family B / PLAN，`recommended`）自
    I0 打开后一直未执行，收口时按窗口事实记 `missed=1`，`closure-check` 给出“1 个窗口未执行”
    提醒——这是真实漏调（本任务确实没跑 PLAN），advisory 非门禁；② `sol-stats` 的
    `coverage.closure.rate=3.0`（分子 3 = 跨迭代已满足的 E 义务，分母 1 = 当前任务收口窗口
    是否适用），是 v9.6.0 起的统计口径（分子跨迭代计数、分母按适用性），非本轮引入，也未
    影响任何判定；如需改成封顶展示应单独任务处理。

## SOL 生命周期主动调度（2026-11-21，v9.6.0）

- **根因（现场证据）**：旧实现 `sol_advice` 只打印命令，建议里带 `<state>`/`<materials>`
  占位符，而 `sol-consult` 实际要求 `--materials` / `--question`；“应调未调”没有持久事实；
  `plan_required` 只看工作项数量；动态 CONSULT 只比较三个粗粒度信号；里程碑复盘只在
  `risk=high`；`drive` 的 PASS 分支直接 `close_task`，收口 CONSULT 结构上不可达。真实任务
  统计中 6 个有 4 个 `sol_calls=0`。
- **实现**：`scripts/sol_schedule.py`（纯策略层，零网络/零 I/O）把 A–E 窗口物化为义务并落在
  `state.sol_schedule`；`review_policy` 的 `plan_admission()` 是 PLAN 准入唯一口径；六类事件
  与 `sol_event_snapshot` 比较；`copilot_dispatch._sol_schedule_step` 在 15 个生命周期写命令
  （`start`/`plan`/`record`/`rerun`/`criterion`/`judgment`/`evidence`/`report`/`accept`/
  `decide`/`block`/`unblock`/`contract`/`reopen`/`finding`）与 `drive`、`closure`、
  `sol-schedule --execute` 内有界执行（每条命令 1 个义务 / 1 次请求），
  `--sol auto|advise|off`；`sol-stats` 给显式分母覆盖率。设计依据见
  `docs/DECISIONS.md` 第 74 条与 `references/runtime.md` §5。
- **按真实运行纠正的五类缺陷（文档/测试都不算证据）**：① `start` 是唯一带 `--sol` 却从不
  进入调度步的生命周期命令（`--sol auto` 只打印、不执行也不落义务），已补调度步并用 CLI
  级用例锁定；② 已执行义务只改 `status` 不关窗口，收口时会被算成“关闭前仍未执行”，新增
  `sol_schedule.settle()` 在成功/失败/跳过时关闭窗口；③ 连续进展会为同一 family+event 反复
  打开里程碑窗口（实测一次连续进展堆出 8 条 open D 义务、收口会误算 8 条漏调），改为新建
  时即取代更早的未执行窗口（`skip_by=superseded-by-newer`），历史堆积在下一次同步收敛；
  ④ D/E 曾被标成 `optional`，而 `due_obligations()` 只取 `recommended`，导致 medium 任务的
  里程碑复盘与收口 CONSULT 结构性不可自动执行（`sol-schedule --execute` 明确回报“没有
  recommended 义务”）；改为 D/E 一律 `recommended`，历史 `optional` 义务收口时记
  `skipped` + `skip_by=not-required`（合理跳过，不算漏调），收口前先结算更晚窗口已满足；
  ⑤ 真实收口时发现：最后一批进展刚打开 D 窗口、E 随即执行，D 被记成漏调（系统性假漏调）；
  改为 E 已满足时的 D 窗口记 `skipped` + `skip_by=closure-covered`。
- **真实 SOL 调用（运行时副本，非模拟）**：临时夹具任务 `task-d0f86a14e4af8675`
  （`/tmp/sol-sched-final/state.json`）走默认调度路径：`start` 自动执行
  `SCd240da149f519a3dcc4ffd8d`（CONSULT，understanding，gpt-6.2-sol，effort=high，
  zero_tool=true，31,137 ms，2,977 tokens），`plan` 自动执行
  `SCb2af8c3448f028d173629816`（PLAN，planning，同模型/强度，35,492 ms，3,101 tokens）；
  义务 SO1/SO2 均 `satisfied` + `window=closed` + `source=executed`，`sol-stats` 的
  `coverage.startup` 与 `coverage.plan` 都是 1/1（rate 1.0）。
- **真实宿主验证**：`zcode skills list` 直接读运行时 SKILL.md，显示
  “Copilot v9.6 … 生命周期主动调度（义务化、有界自动执行、覆盖率统计）”。
  另用 ZCode 0.18.0 headless `--prompt`（`COPILOT_STOP_GUARD=0`，避免守卫把探针任务改成
  续跑回合）让宿主 Agent 按技能文档自建任务（`task-7f28358b5a52202d`）：它没有手动调用
  `sol-consult`/`sol-plan`，而由调度步执行 `SC1750a2abbad7961a7caa4648`（CONSULT，
  startup，2,525 tokens），报告的 `coverage.startup` = 1/1。
- **测试（最终代码）**：活动套件 **824 项 OK**（`discover -s tests`，含新增 42 项
  `test_sol_scheduling`）；`legacy/tests` 853 项（skip 32）= 99 failures + 47 errors =
  **146**，失败/错误用例名集合与 `HEAD=75a6fa1` 基线 `diff` 为空（历史环境失败，非本轮引入）。
- **同步与基线（最终代码）**：末次 `--apply` 写入 5 个文件（`SKILL.md`、
  `references/review.md`、`references/runtime.md`、`scripts/review_policy.py`、
  `scripts/sol_schedule.py`），`--check` 缺失 0 / 不同 0（`hm.txt` 未受管，默认保留）；
  `--baseline` 23 个 Git 跟踪文件（`scripts/sol_schedule.py` 未跟踪，按规则不进入 manifest），
  `--verify-baseline` 通过；运行时 `__version__=9.6.0`，运行时副本 `start --sol advise` 冒烟
  真实创建 A 窗口义务并给出无占位符的可执行命令（`evidence/runtime-smoke.log`）。
- **未验证 / 边界**：15 类场景由定向测试覆盖（替身通道），未在真实宿主逐条重演；`drive`
  收口路径与 `closure` 入口在测试中用替身验证；未做多会话并发真实调度压测（锁语义由
  `LockBusyError` 用例覆盖）；`evaluate`/`review-admission` 的 `sol.command` 仍是含占位符的
  模板，可直接执行的命令以 `sol-schedule` 与调度步输出为准；**手动** `sol-consult` /
  `sol-plan`（不经调度义务）不记入覆盖率分子——本任务自身 2 次早期真实调用（`SC794602…`
  understanding、`SC5b3884…` planning）因此在 `sol-stats` 里显示 `startup`/`plan` 0/1，
  但调用本身完整记录在 `sol_calls`；本任务收口时按收口前代码把 1 条刚打开的 D 窗口记为漏调
  （随后按 ⑤ 修正，任务记录保留该历史事实）；本次未提交、未推送、未发布。

## 宿主集成与真实宿主验收（2026-11-21，v9.5.1）

- **Hook 安装（用户级，已授权）**：备份后写入 `~/.zcode/cli/config.json`：`Stop`（1 组，
  timeout 10）、`SessionStart`（matcher `startup|resume|compact`）、`UserPromptSubmit`
  （`user-prompt`）各 1 组，`PreToolUse` 保持原有 0 组；非钩子配置（模型、MCP、插件等）
  在安装前后一致。备份：`config.json.bak-20261121143827`（首次安装前）、
  `config.json.bak-20261121144420`（matcher 修正前）。重复执行 `install --user --apply`
  不产生重复组，字节内容不变。
- **宿主**：ZCode 0.18.0 headless（`node <zcode-cli-entry>`，默认
  `--mode yolo`），`--resume <sess_id>` 复用同一会话；钩子判定用
  `COPILOT_STOP_GUARD_LOG` 逐次落盘 JSONL（`note` 字段可逐条核对）。
- **场景 A（PARTIAL 自动续跑）**：`user-prompt-inject:1` → `drive` 领取 A1 并执行回填 →
  代理尝试结束回合 → Stop 返回 2（`RUNNING/execute/bound`）→ 代理执行 A2 → `CLOSED` 2/2；
  之后 Stop `out-of-scope:0` 放行。全程无用户提示。
- **场景 B（新用户回合续跑）**：首回合绑定任务；第二回合 `--resume` 另一会话 ID →
  `session-start-pointer:resume:1` + `user-prompt-inject:1`；代理在本回合补齐授权后执行
  `unblock --by user --quote "允许创建 allowed.txt 测试文件"`（`resolved_by=user`）→
  `CLOSED` 1/1，未重复派发。
- **场景 C（全新会话恢复）**：新会话启动 → `session-start-pointer:startup:1` +
  `user-prompt-inject:1`；代理 `status --json` 核实现场、`drive` 绑定并接管，执行 c1/c2 →
  `CLOSED` 2/2。注入的是可执行入口（任务、阶段、条件、续接命令），不是仅摘要。
- **场景 D（单项阻塞隔离）**：A1 `block --work-item`（reason + needs：用户提供凭据），
  A2/A3 继续执行完成；任务停在 `BLOCKED`（含 reason/needs），未伪造 PASS。
- **场景 E（重复/并发/中断）**：
  - 并发领取：`owner1` 领 A1、`owner2` 领 A2、`owner3` 得到 `wait` + 退出码 11，
    状态中无重复领取。
  - TTL 接管：`COPILOT_CLAIM_TTL=0` 后另一 owner 接管成功；`handoff_count` 连续累计
    （1→2→3），中断/接管不重置为 1。
  - 重复触发：同一 payload 连续两次 `user-prompt` 输出完全相同、state SHA-256 不变
    （钩子只读）；`install` 重复执行不产生重复 Hook。
  - 中断恢复：阶段一以 `COPILOT_STOP_GUARD=0` 只执行 `drive` 后结束（A1 已领取、未回填、
    无产物）；阶段二 `--resume` 同一会话 → `session-start-inject:resume`，工作项未丢失、
    重新领取并执行 A1/A2，每个动作只登记一次 `action_recorded` 与一份证据，产物只在恢复
    回合生成，最终 `CLOSED` 2/2。
- **修复（真实宿主暴露）**：① `SESSION_START_MATCHER` 补 `startup`（宿主新会话只发
  `startup`，原 `resume|compact` 匹配不到）；② `start` 对 `.copilot-tasks/` 之外的状态
  文件给出告警（钩子只扫描该目录，此前测试夹具放在工作区根目录导致钩子全部空转）；
  ③ `work_order` 的条件提示改用本工作项 `target_criteria`（原先固定提示 `C1`，A2 会把
  证据记到 C1）；④ `handoff_count` 跨过期租约累计。对应回归：
  `test_v95_scheduling`（工作单提示、目录告警、TTL 接管计数）、`test_stop_guard`
  （matcher 断言）。
- **测试**：活动套件 **782 项 OK**（重编译字节码后 54.9 s，首轮 75.9 s；baseline 重建后
  全绿，含 `test_real_manifest_matches_git_tracked_source`）；`legacy/tests` 853 项
  （skip 32）= 99 failures + 47 errors = **146**，失败/错误用例名集合与 `12ed456` 临时
  worktree 逐行相同（`diff` 0 行）。
- **运行时同步**：`--apply` 补齐 1 个文件；`--check` 缺失 0 / 不同 0（`hm.txt` 非受管
  保留）；SKILL.md、references 与 4 个脚本源/运行时 SHA-256 逐一相同；`--baseline`
  23 个 Git 跟踪项，`--verify-baseline` 通过。
- **未验证 / 边界**：未实现后台守护或自派生 resume 子进程（宿主 `xmc=3` 上限不变）；
  “执行中断”验证的是“领取后正常结束回合 + `--resume` 恢复”，未覆盖进程被强杀的时序；
  跨会话真并发以 3 个独立 owner 的 CLI 驱动验证，未在三个真实 ZCode 会话同时运行。

## 持续推进：调度领取、恢复提示与阻塞边界（2026-11-21，v9.5.0）

- **问题（现场复现）**：派生项只出现在 `derive` 输出里，没有“谁在执行、何时领取”的
  状态事实，主代理可以合法停在 PARTIAL；`block --scope task` 不检查是否仍有可执行工作，
  一个工作项或一次缺授权就冻结整任务；`unblock` 不记录是谁解除的，新授权无法与旧阻塞
  区分；SessionStart 只对绑定本会话的任务给快照，重启/新会话/其它会话的任务只剩
  `evaluate` 里一行摘要，恢复等于重新调查。
- **调度领取（`claims`）**：`governance.py` 新增 `claim_actions`/`active_claims`/
  `claim_owner` 与 `K`+`sha256[:12]` 领取 ID，写 `state["claims"]`：owner（会话 ID 或
  `local`）、claimed_at、TTL（`COPILOT_CLAIM_TTL`，默认 1800 s）、`handoff_count`；
  过期与 owner 不一致的领取自动失效。`ready_actions` 默认隐藏其它 owner 的领取项；
  `derive_work` 跳过已被领取的条件；`record`/`rerun`/条件核销/`block`/`drop` 成功后
  释放对应领取。`continuation` 在“仅剩其它会话领取项”时返回 `action=wait` +
  `claimed_by_others`，不再回退成 `AUTO-NEXT` 空转；`drive` 在 RUNNING 分支领取选中的
  工作项后保存（CAS 失败报错不吞），PARTIAL 且 `wait` 时以 `EXIT_WAIT` 结束。
- **条件归属**：计划动作可声明 `target_criteria`（必须是合同已有条件），派生项通过
  `_action_criteria` 与真实条件绑定，领取与续跑判断按条件而非仅按动作 ID；
  `target_criteria` 在计算计划 SHA 前剥离、动作创建后回填，native dispatch 指纹不变。
- **阻塞边界**：`block` 必须给 `--work-item` 或 `--scope task`（二者不可同时）；
  任务级阻塞无 `--force` 时若仍有 `ready_actions` 直接拒绝，并提示改用 `--work-item`；
  强制任务级阻塞记录 `ready_at_creation`/`forced_scope`。`unblock --by user --quote`
  记录 `resolved_by`/`authorization_source=caller-declared`/`authorization_quote` 与历史，
  `by=user` 无引用文本拒绝执行（自报不是凭证）。
- **恢复提示**：`copilot_stop_guard.py` 的 SessionStart 区分“resume/compact + 绑定本会话”
  （完整快照，保留 v9.4 上下文追加）与 startup/未绑定（只读指针：任务、阶段、未闭合
  条件/阻塞原因与 needs、续接命令，标注本会话/归属其它会话/未绑定）；新增
  `UserPromptSubmit` 钩子，每轮注入最多 3 个打开任务的只读提醒与四条判断规则
  （用户本轮给出授权 → `unblock --by user --quote` 后 `drive`；否则不要自行续跑；
  不是授权；不要重复派发），失败一律 exit 0。`install`/`uninstall` 同时维护 Stop、
  SessionStart（`startup|resume|compact`）与 UserPromptSubmit 三处。
  （2026-11-24 起口径收敛：只展开本会话负责的任务、判断规则并为两条，其余任务不再逐轮
  披露——见文首「Hooks 必要性审计与性能优化」。）
- **测试**：新增 `tests/test_v95_scheduling.py`（20 项，覆盖 C1 自动续跑、C2 派生→调度→
  执行→核销闭环、C3 单项阻塞隔离、C4 授权变更恢复、C5 条件变更恢复、C6 重复调度防护与
  领取不永久丢失、C7 上下文重载续跑、C8 真实 BLOCKED 的原因与 needs、C9 PASS 只在全部
  条件满足时成立）；`tests/test_stop_guard.py` 扩到 61 项（指针契约、UserPromptSubmit
  注入/关闭/畸形输入/只读放行）。活动套件 **779 项 OK / 77.9 s**（baseline 重建后全绿，
  含 `test_real_manifest_matches_git_tracked_source`）；`legacy/tests` 853 项（skip 32）
  99 failures + 47 errors = **146**，失败/错误用例名集合与 `261563d` 临时 worktree 逐行
  相同（`diff` 0 行）。
- **运行时同步**：`--apply` 写入 7 个文件；`--check` 缺失 0 / 不同 0（`hm.txt` 非受管
  保留）；源/运行时 4 个改动脚本 SHA-256 相同（`governance.py` `49907511…`、
  `copilot_auto.py` `471fb9eb…`、`copilot_dispatch.py` `409c568f…`、
  `copilot_stop_guard.py` `69e6a92e…`），`copilot_dispatch.__version__=9.5.0`；
  `--baseline` 在暂存后重建为 **23 个 Git 跟踪项**（分发集合未变，新测试不参与分发），
  `--verify-baseline` 通过。
- **未完成 / 未验证**：`UserPromptSubmit` 与新版 SessionStart 只做了单元测试与直接 CLI
  调用，未在真实 ZCode 回合中观察注入；安装到用户级 `~/.zcode/cli/config.json` 属全局
  配置变更，未授权未执行（当前宿主仍只装 Stop/SessionStart）。跨会话真实并发（两个会话
  同时 `drive`）未做宿主级 E2E，领取互斥由进程内测试覆盖。
  **上述限制已由 v9.5.1 章节的真实宿主验收覆盖**（用户授权安装用户级钩子并执行场景 A–E），
  该段只保留为 v9.5.0 交付时点的现场记录。

## 项目上下文与长期记忆衔接（2026-11-21，v9.4.0）

- **新增**：`scripts/context_engine.py`（`resolve`/`recall`/`pack`/`capture`）、
  `tests/test_context_engine.py`（43 项）、`docs/CONTEXT-ENGINE.md`。四层信息（原生
  Memory / 正式 Vault / Task Memory / 工程现场）走一条只读数据流；零模型调用、无网络、
  不建记忆库、不写 Vault 与原生 Memory。
- **接线**：`copilot_stop_guard.py` 的 SessionStart(resume|compact) 快照在任务状态后追加
  项目现场（根/分支/HEAD/工作区）、原生 Memory 与 Vault 可用性、`recall`/`pack` 命令；
  `copilot_dispatch.py status` 人工输出附一行上下文提示，JSON 契约不变。两处都是可选
  能力：模块缺失、路径不存在或任何异常时省略且不影响原有输出。
- **可信度**：记忆与 Vault 命中带 `path:line:snippet` 与核验时间/`source_commit`；无核验
  标注「视为未核实」，来源提交与当前 HEAD 不一致标注「基线漂移」；冲突统一「以现场为准
  + 写明卡路径、差异与核验时间」。
- **独立只读复核（2026-11-21）**：`independent-readonly` 子代理通读实现与接线，报 12 条
  发现 + 2 条待验证风险；逐条核实后全部确认并修复——`capture` 在证据为治理写入的
  `{id,sha256}` 对象时崩溃（TypeError）、有来源失败/截断被标成 `ok`/`zero_hit`、缓存键
  漏掉扫描预算、正文水平分隔线被误判为 frontmatter、记忆根不可读退化成 absent、`--cwd`
  子目录记忆并入当前项目、detail_level=3 丢弃命中却仍显示 ok、召回片段未脱敏、无参
  `pack` 打印 traceback（退出码 1）、证据路径字段取错、提示命令未做 shell 引用、
  SessionStart 把 git 状态未知显示成 clean；边界另加白名单 realpath 校验与不可读子树报错，
  SessionStart 单次 git 超时收紧到 1 s。每条修复都有对应回归测试（新增 14 项），
  没有修复时这些用例失败。
- **实测（本机，2026-11-21，进程内 5 次中位）**：`resolve` 13.4 ms（11.9–14.2）；
  `recall` 冷启动 35.8 ms（139 文件 / 687,386 字节 / 7,714 行，含 realpath 边界校验与片段
  脱敏），缓存命中 6.3 ms（键含全部扫描预算与文件集 `path+size+mtime_ns` 摘要，只有完整
  结果入缓存）；`pack --target subagent` 0.3 ms（不含召回）；模型调用与 token 消耗均为
  **0**。CLI 子进程另含约 70–90 ms Python 启动与 git 调用开销（resolve ~104 ms）。
- **测试**：活动套件 **751 项 OK / 54.7 s**（v9.3.0 基线 708 + 新增 43）；`legacy/tests`
  853 项（skip 32）99 failures + 47 errors = **146**，失败/错误用例名集合与 `19ee58e`
  临时 worktree 逐行相同（差异 0 行）——均为 v5 历史用例依赖已退役 `references/role.md`
  等文件的既有失败，与本次改动无关。
- **运行时同步**：`--apply` 写入 3 个受管理文件（`context_engine.py`、`copilot_dispatch.py`、
  `copilot_stop_guard.py`）；`--check` 缺失 0 / 不同 0（`hm.txt` 非受管文件保留）；
  `--baseline` 重建 22 个 Git 跟踪项并提示未跟踪的 `scripts/context_engine.py` 不进
  manifest；`--verify-baseline` 通过；源/运行时三个脚本 SHA-256 分别相同
  （`context_engine.py` `3f80f4a0…`、`copilot_dispatch.py` `ccdac863…`、
  `copilot_stop_guard.py` `8557fdb5…`）。
- **真实命令复验**：用真实任务状态跑 `capture`（此前会因证据对象崩溃）生成 2,750 字符
  草稿并通过 `candidate_write.validate_content`；真实 Vault 召回 40 行预算截断时返回
  `budget_exhausted` 且 `truncated=true`；`pack --target subagent` 3,863/5,000 字符；
  `status` 提示正常追加且 JSON 契约不变。
- **未完成 / 未验证**：未提交、未推送、未发布（任务未授权）；因此 manifest 暂不含未跟踪
  的新模块与测试，提交后需再次 `--baseline`/`--verify-baseline`。真实 Vault 召回只做了
  本机只读冒烟，未做跨项目知识正确性人工抽检；SessionStart 10 s 钩子预算未做慢盘压测。

## 仓库版本管理收口（2026-11-21）

- **盘点**：`git status --short --untracked-files=all` 共 **40 个未跟踪文件、154,498 字节**；
  A 正式资产 2、C 治理任务数据 38、D 临时/生成 0、E 真实敏感信息 0（扫描命中的 `sk-…` 是
  测试日志里的 17–19 字符夹具）。逐文件清单在
  `.copilot-tasks/repo-hygiene-2026-11-21/evidence/inventory.md`（本地，不纳入 Git）。
- **纳入 Git**：`scripts/knowledge_handoff.py`、`tests/test_knowledge_handoff.py`（提交
  `c799a5c`）。README 目录树、SKILL.md 与 `references/workflow.md` 早已描述该扩展与
  `--knowledge-file`，未跟踪属版本管理缺口；基线由 21 增至 **22 个跟踪项**。
- **本地化**：`.copilot-tasks/`（任务状态、合同、证据、SOL 原始输出、会话绑定）加入
  `.gitignore`，**文件全部保留在磁盘**。此前该目录内只有 `state.json` 被既有规则忽略、
  `session.json`/`evidence/` 等未被忽略，口径已统一。`.git/info/exclude` 的两条本机规则
  （`task-v612-g4-final4/`、`reports/junit.xml`）未改。
- **提示修正**：扩展现已随仓库分发，`KNOWLEDGE_EXTENSION_MISSING` 文案由“不随仓库分发”
  改为“缺少 `scripts/knowledge_handoff.py`”。
- **验证**：活动 **708 OK / 50.7 s**；`git archive` 干净 checkout 下 CLI 启动、知识交接
  `check`/`build` 与 `sol-consult --knowledge-file --dry-run`（materials 1,239 字符）正常；
  删除模块的副本中 CLI 仍启动，使用 `--knowledge-file` 返回 exit 2
  `KNOWLEDGE_EXTENSION_MISSING`；运行时 `--apply` 写入 1 个文件（`copilot_dispatch.py`），
  `--check` 0/0、baseline 22 项校验通过、源/运行时 `knowledge_handoff.py` 同为 `c85335f3…`。
- **推送（2026-11-21 完成）**：三个提交 `c799a5c`、`06d4f3d`、`3b3e2d9` 以普通快进推送
  `origin/main`（`dd1e601..3b3e2d9`），未 force / rebase / reset / clean；推送后本地 HEAD、
  `origin/main` 与远端 `refs/heads/main` 三方同为 `3b3e2d9`，ahead/behind 0/0。本段其余
  内容中“尚未推送”的表述是撰写当时的现场记录，已由该推送闭合；本节随后由文档跟进提交
  `docs(copilot): record repository hygiene delivery` 再次普通快进推送，最终本地与远端一致、
  工作区干净。knowledge_handoff 源码与测试已随 `c799a5c` 正式纳入版本管理，
  `.copilot-tasks/` 仍只是本地治理运行数据（Git 忽略、磁盘文件全部保留）。

## 收尾核查：测试数量、耗时口径、授权真实性与评审记录（2026-11-21，v9.3.0 收尾）

针对上一段交付中“684→706”“51.6 s→71.4 s”“五路评审”等说法做独立复核，结论如下
（详细数据见 `.copilot-tasks/copilot-review-2026-11-21/evidence/` 的
`test-count-diff.txt`、`timing-comparison.md`、`authorization-review.md`、`review-audit.md`）：

- **测试数量**：同一发现口径逐条比对测试 id，+18 `test_v93_hardening.*`、+5
  `test_stop_guard.*`、−1 被替换的旧用例 = **+22**；没有参数化/发现规则变化，历史基线未改。
  被替换的 `test_corrupt_state_fails_open_with_hint` 与新 fail-closed 契约相反，由两条更精确
  的用例取代。本轮补 2 条 CLI 级用例（破坏性修订授权说明、reopen 来源标注端到端），
  活动套件当前 **708 项**。
- **耗时**：51.6 s 与 71.4 s **不可直接比较**（前者无原始日志，字节码缓存与机器负载不同）。
  受控对比用同构导出树（都无 `__pycache__`、同机顺序执行）：684 项 71.6/73.7 s，
  706 项 74.8/76.7 s，**+22 项 ≈ +3.1 s（≈145 ms/项）**；预编译字节码的真实仓库 708 项
  52.0 s（两轮 51.8/52.2 s）。没有单项或公共初始化变慢的证据，也没有为提速删测试。
- **延迟**：差异来自字节码缓存状态，不是代码。源码刚改过时 `status` P50 237 ms（每次运行
  重编译改动模块）；`compileall` 后源码树 133 ms、运行时目录 130–142 ms，与 v9.2.0 的
  137.3 ms 一致。20 样本（运行时）：Stop 77–81 ms、SessionStart 75–80 ms、`drive` 74–76 ms。
- **授权真实性**：`authorized_by` 在 `contract --amend` / `reopen` / `authorize-review`
  三处都是**调用方自报声明**，架构内没有可验证的用户凭证——这是如实记录的限制。最小加固：
  破坏性修订必须另写 `--authorization-note "用户原话/要求"`，历史记录
  `authorization_source=caller-declared`、说明文本、新增/删除未核销条数；`reopen_history`
  标注同一来源。拒绝仍是 state 零写入（失败尝试不落历史，仅在 stderr 与“无对应
  contract_amended 记录”上可追溯）。
- **评审记录**：实际是主 Agent 八维度合并评审（`review-findings.md`）+ 1 个 `Explore`
  只读子代理做证据链专项，**没有五份独立子代理评审报告**；按五个范围（治理 / CLI 续跑 /
  Hooks / SOL / 证据绑定）逐条把结论对回最终源码与测试，P0/P1 均有代码与测试落点，
  P2 与未验证项记录准确。E1–E13 逐文件重算 SHA-256 全部一致。
- **仍未验证**：宿主真实 `compact` 事件、GUI 新会话；严格性能 A/B 不可得（改为受控同构对比）。

## 治理一致性与续跑绑定加固（2026-11-21，v9.3.0）

交付前 `HEAD=0e5f5b1`（= `origin/main`）。本变更集以提交 `cf29439`（父 `0e5f5b1`，21 个文件
+1169/−110）普通快进推送至 `origin/main`；本地 HEAD / `origin/main` / 远端 `refs/heads/main`
三方一致、ahead/behind 0/0，未使用 force push / rebase / reset / clean。未跟踪的
`scripts/knowledge_handoff.py`、`tests/test_knowledge_handoff.py` 与 `.copilot-tasks/`
保持原样，未纳入提交。

- **修复清单（全部有定向测试）**：
  1. 合同修订绕过完成条件 → 净未核销条件减少或 `test_policy.required` 由真变假要求
     `authorized_by="user"` + `authorization_note`（用户要求），历史留 `removed_criteria` /
     `test_policy_weakened` / `authorization_source` / 新增删除条数。
  2. 条件 met 后 `AUTO-C<n>` 仍 pending → 自动 `dropped`（`auto_closed_for`），条件回退时重开；
     计划内工作项与已开工包装项不动。
  3. 重开后可立即复用旧 PASS → `reopen` 记录进展基线，`evaluate`/`close` 双处 fail-closed。
  4. 多阻塞共用一个 `blocked_by` → 全部解除才恢复，否则改指向剩余 block。
  5. `AUTO-R-*` 重启后嵌套膨胀 → 家族归一（`AUTO-R-<base>-R<n>`）。
  6. Hooks 会话绑定过早/过泛 → 只在成功（0/10）且非只读后绑定；只读集合补 `quality`；
     `evidence --list`、`contract --show` 不绑定；任务根查找在 `.git` 停止。
  7. 恢复快照对未绑定损坏任务也提示、命令可能被截断 → 只处理 `bound`/`corrupt`，摘要按整行
     截断；`check` 任何异常 fail-closed（exit 2 + `internal-error`）。
  8. SOL 单轮复用键不敏感 → materials `strip()` + 提示词模板哈希 + `--force` 优先；
     CPA 端点拒绝 query/fragment/额外路径。
  9. CLI 退出码不一致 → `plan`/`finding`/`accept`/`reopen`/`contract --amend` 返回 10。
  10. **干净 checkout 不可用**（P1）：`copilot_dispatch.py` 硬导入未跟踪的
      `knowledge_handoff`；改为惰性导入，仅 `--knowledge-file` 缺失时报
      `KNOWLEDGE_EXTENSION_MISSING`。
- **测试**：`tests/` 活动全量 **708 项 OK**（交付提交前复跑 52.0 s，含新增 `tests/test_v93_hardening.py`
  20 项与 CLI 级授权/重开用例；收尾核查后由 706 增至 708，见上一节口径说明）；`legacy/tests` 853 项，99 failures +
  47 errors + 32 skips = 146，失败/错误用例名与既有基线逐条一致（`diff` 为空）。
- **同步**：`--apply` 写入 10 个受管理文件（惰性导入修复后补 1 个）；`--check` 缺失 0/不同 0
  （运行时 `hm.txt` 属用户未受管理文件，保留）；`--baseline` 重建 21 个 Git 跟踪项（未跟踪
  `knowledge_handoff.py` 不写入）；`--verify-baseline` 通过；运行时 `__version__=9.3.0`。
- **干净 checkout 复核**：`git ls-files` 导出的仅跟踪文件树中 `copilot_dispatch.py --version`
  返回 9.3.0、`status` 退出 0（`HEAD` 归档旧代码仍复现 `ModuleNotFoundError`，证明修复有效）。
- **Hooks 隔离 E2E**（运行时脚本，`/tmp/v93-hookcheck*`）：绑定会话 Stop exit 2 且提示含
  宿主 3 次上限与续接命令；其它会话 exit 0；畸形 JSON exit 2；`COPILOT_STOP_GUARD=0` exit 0；
  `resume`/`compact` 注入含任务 id、阶段、未闭合条件与完整命令；`startup`/`clear` 0 字节；
  失败命令（`criterion` 缺证据）与只读命令（`status`/`quality`/`contract --show`/
  `evidence --list`）均不产生绑定；绑定任务 state 损坏时 `session-start` 注入异常提示且
  Stop exit 2，未绑定损坏任务静默；含 `.git` 的子目录不继承祖先 `.copilot-tasks`。
- **延迟**（运行时脚本、7 次中位数、隔离工作区）：Stop 守卫 81.7 ms、`session-start`
  81.3 ms、`status` 143.8 ms、`review-admission` 145.2 ms；v9.2.0 交付记录为
  81.4 / 80.2 / 137.3 ms。夹具不同，只能说明同量级，不作严格 A/B；另一次轻量夹具测得
  73.4 / 73.7 / 126.3 ms，属同一量级波动。收尾核查的 20 样本复测（运行时目录）为
  Stop 77.2 / SessionStart 79.6 / `status` 130.0 / `drive` 73.6 ms（P50），详见上一节。
- **SKILL.md**：58,303 B（收尾核查后；v9.2.0 交付时 56,707 B，增量来自 v9.3 版本段与授权
  说明，未新增规则章节）。
- **SOL / 子代理**：本任务未发起真实 SOL 调用（真实模型调用不在本次授权内），按
  `unavailable` 降级，不阻断；使用 1 个只读子代理做证据链专项调查，其结论由主代理逐项
  核实后分级（详见 `docs/DECISIONS.md` 第 68 条）。
- **未验证 / 限制**：宿主真实 `compact` 事件与 GUI 新会话仍未单独复现（与 `resume` 共用注入
  路径，单测覆盖 `source=compact`）；严格性能 A/B 不可得（旧数据无原始日志），已改为同构
  导出树受控对比（见上一节）；`wait` 退出码、task 级阻塞在仍有可执行工作时的判定、
  `_tracked_actions` 的 AUTO-G4 过滤、legacy 后端 G2 未强制 `test_policy`、若干去重重构与
  SOL 脱敏广度按 P2 记录，未在本次实施。

## ZCode 原生恢复衔接与上下文瘦身（2026-11-20，v9.2.0）

现场基线：交付前 `HEAD=0b64f62`；交付提交 `37f5cd8a5ebb7eb67429deae50bac42c022f9090`
（`feat(copilot): optimize skill context and session recovery`，8 个跟踪文件 +583/−385），
已普通快进推送 `origin/main`（`0b64f62..37f5cd8`）。推送后本地 HEAD、`@{u}` 与
`git ls-remote origin refs/heads/main` 三方同为 `37f5cd8a…`，ahead/behind `0/0`；
未 force push、未 rebase。未跟踪的 `scripts/knowledge_handoff.py`、
`tests/test_knowledge_handoff.py` 与 `.copilot-tasks/` 保持原样。

- **SessionStart 恢复注入**：`copilot_stop_guard.py` 新增 `session-start` 子命令，只处理
  `source=resume|compact`；只对本会话已绑定且 `continuation.required=true` 的任务输出一段
  只读快照（任务/阶段/原因、未闭合条件、下一条命令、可粘贴续接命令）。不绑定任务、不写
  state/session、不消费 Stop 次数；`startup`/`clear`、其它会话、已收口、无任务一律静默；
  任何异常退出 0（fail-open）。`install`/`uninstall` 同时维护 `Stop` 与 `SessionStart`
  （matcher `resume|compact`），幂等且只动这两个条目。
- **SKILL.md 瘦身**：版本沿革外移到 `references/history.md`，并把原先内联但 history 缺失的
  v6.3–v6.12.0、v8.0.1 补回该文件；SKILL.md 84,291 → 56,707 B（−32.7%），现行规则、
  G1–G3、授权边界与命令面未删减。
- **测试**：`python3 -B -m unittest discover -s tests -p 'test_*.py'` **684 项 OK**（新增 12 项
  SessionStart/安装测试）；`legacy/tests` 853 项（skip 32）99 failures + 47 errors = 146，
  与 `git archive HEAD` 归档运行的失败/错误用例名逐条 diff 为空。
- **同步**：开发期 `--apply` 写入 5 个受管理文件，提交后写入 0 个；`--check` 缺失 0/不同 0；
  `--baseline` 重建 21 个 Git 跟踪项（未跟踪的 `knowledge_handoff.py` 未写入），
  `--verify-baseline` 通过；运行时 `copilot_dispatch.py __version__=9.2.0`，与源码逐文件
  SHA-256 一致。
- **隔离 E2E**（`/tmp/copilot-e2e2`，运行时脚本）：未完成时 Stop 守卫 exit 2、
  resume/compact 注入含任务 id 与 C1/C2；`startup`/`clear`/其它会话/收口后均输出 0 字节；
  收口 `closure-check` G1–G3 PASS，state 损坏时 `session-start` 静默 exit 0。
- **度量**：Stop 守卫 81.4 ms、session-start 80.2 ms、`status` 137.3 ms（7 次中位数），
  与升级前同一量级；正文本地估算 19.4k → 13.0k tokens/会话（0.23 token/byte 估算）。
- **SOL**：本任务 CONSULT `SC16677c9af5891518dd968f5e`（59.4s，`gpt-6.2-sol`、`high`、
  零工具）已按 `modified` 登记采纳结论与差异；未调用 PLAN/REVIEW。
- **本机安装**：用户级 `~/.zcode/cli/config.json` 已含 `Stop` 与新增
  `SessionStart(resume|compact)` 条目，安装前备份 `config.json.bak-20261120223622`，
  `mcp`/`plugins` 等其它配置未变；新会话启动时加载。
- **真实宿主 resume**：headless `--resume sess_a5b42fc8…`（ZCode CLI 0.18.0）的会话 rollout 中
  确认宿主拼接 `SessionStart hook additional context: #1 <copilot-session-recovery> …`
  （任务 id、RUNNING、未闭合 C1、续接命令），同一 rollout 出现 37 处 Stop 守卫续跑理由。
  该次 resume 共 44 次模型请求、3,334,400 input tokens（cache read 3,290,880）、约 3 分钟；
  带未完成绑定任务的 resume 会真实驱动执行，不宜无预算预期地复跑。
- **未验证 / 限制**：宿主真实 `compact` 事件尚未单独复现（与 resume 共用同一注入路径，
  单测覆盖 `source=compact`）；未在 GUI 新会话复跑。

## SOL 采纳记录与证据关联（2026-11-20，v9.1.0）

现场基线：`HEAD=ac9cd61`（v9.1.0 发布提交）。本轮 10 个跟踪文件（+657/−22）已提交并推送。
未跟踪的 `scripts/knowledge_handoff.py`、`tests/test_knowledge_handoff.py` 保持原样，未纳入 Git。

- **发布**：`ac9cd61b54c531c23842036fc3d88552c2086092`
  （`feat(copilot): improve SOL acceptance evidence tracking`），10 个文件 +657/−22；
  已普通快进推送 `origin/main`（`971e330..ac9cd61`）。当次发布核验时本地 `HEAD`、
  `origin/main`、`git ls-remote origin refs/heads/main` 三方同为 `ac9cd61b…`，ahead/behind
  `0/0`；未 force push、未 rebase。显式路径暂存，未使用 `git add .`/`-A`。
- **代码**：`governance.py` 新增 `copilot-sol-acceptance/v2` 记录（`decision`/`rationale`/
  `delta`/`evidence_refs`/`source_refs`/`evidence_note`/`advice_ref`）、登记时证据解析
  （`linked`/`unresolved`/`archived`）、非阻断 warnings 与 `acceptance_quality` 指标；
  `copilot_dispatch.py::sol-accept` 新增对应 CLI 参数并打印提示；版本号 `9.1.0`。
- **重复登记与历史**：当前记录以最近一次为准，旧字段不隐式继承；每次登记完整快照写入
  `history.sol_call_acceptance`，`first_registered_at` 保留最初时间，`registration_count`
  只计 v2 次数；旧记录只读可读、不计入质量分母。
- **测试**：`python3 -B -m unittest discover -s tests -p 'test_*.py'` **614 项 OK**，提交后
  在已提交树上重新运行确认（非仅复用提交前结果；v9.0.0 基线 604 + 新增 10 项：v2 多证据
  关联、未登记引用待核、无证据如实登记、仅来源定位、modified 缺差异、已归档引用、非法
  E 编号、重复登记快照、旧记录兼容、CLI 非阻断提示）。`compileall` 通过。
- **legacy 回归**：`legacy/tests` 853 项（skip 32）当前树 99 failures + 47 errors = 146，
  失败/错误集合与基线完全一致；在 `971e330` 临时 worktree 复跑同为 146，失败/错误用例名
  排序后 `comm` 差异 0 行。
- **运行时同步（发布核验）**：`--apply` 写入 0 个文件（开发阶段已完成同步，此前写入 6 个受
  管理文件）；`--check` 缺失 0/不同 0（`hm.txt` 等非受管文件保持原样，未 prune）；
  `--verify-baseline` 通过（`--baseline` 在开发阶段已重建 20 个 Git 跟踪项，纯文档更新不
  重写基线）；运行时 `__version__=9.1.0`，源码与运行时 `governance.py` 的 SHA-256 相同。
- **真实调用**：本任务 CONSULT `SC4782e907ccfdc83245835eb3`（48.5s）与 PLAN
  `SC31764715d2849a39b7533023` 均 `gpt-6.2-sol`、`effort=high`、零工具；两条采纳记录按
  新结构登记（decision/rationale/evidence_refs/advice_ref），证据编号指向本任务已登记证据。
- **未验证 / 限制**：未在独立 GUI 宿主会话复跑；未做正式 REVIEW（用户未要求）。

## v9.0.0 发布与运行时验证（2026-11-20）

- **提交**：`a5b2df9`（`feat(copilot): make SOL-first the default cognitive lifecycle`），
  26 个文件（25 修改 + `tests/test_sol_first.py` 新增），+2239/−787。显式路径暂存，未使用
  `git add .`/`-A`；未跟踪的 `scripts/knowledge_handoff.py` 与 `tests/test_knowledge_handoff.py`
  保持原样，未纳入、未删除。发布记录由 `bb3689d`（`docs(copilot): record v9.0.0 release
  verification`，1 文件 +26）补齐。
- **推送**：`origin/main` 两次普通快进（`d3990c5..a5b2df9`、`a5b2df9..bb3689d`）；
  记录写入时本地 `HEAD`、`@{u}`、`git ls-remote origin refs/heads/main` 三方同为
  `bb3689dd6163729021b7881152dfb9692dfa4ac1`，ahead/behind `0/0`；未 force push、未 rebase。
  本记录自身的后续修订只改文档，不改变代码、测试或运行时状态。
- **测试证据**：提交前 `python3 -B -m unittest discover -s tests -p 'test_*.py'` 604 项 OK；
  其后仅改动本记录，提交前用工作区指纹（`git diff | sha256sum`
  `58fcf006…`）确认树未变化，并定向复跑 `test_sol_first`（32）、`test_docs_v6`、
  `test_sync_runtime` 均 OK。`legacy/tests` 853 项失败/错误集合与 `HEAD=d3990c5` 完全一致
  （146 项，排序后 `comm` 差异 0）；`compileall` 通过。
- **运行时同步**：`--apply` 写入 0 个文件（此前已同步），`--check` 缺失 0/不同 0，
  `--verify-baseline` 通过。运行时 `copilot_dispatch.py __version__=9.0.0`，其
  `copilot_dispatch.py`、`sol_review.py`、`SKILL.md` 的 sha256 与源码逐一相同。
- **运行时能力核对（实际加载目录 `~/.zcode/skills/copilot`）**：
  `SOL_MODES=('CONSULT','PLAN','REVIEW')`、`COGNITIVE_MODES=('CONSULT','PLAN')`、
  `THINK→CONSULT` 归一、5 个生命周期 stage、16 类任务与 4 类简单任务；
  `start --task-kind feature` 输出 CONSULT（understanding）默认建议；
  `sol-consult`/`sol-plan --dry-run` 均输出 `gpt-6.2-sol`、`effort=high`、零工具；
  已有 CONSULT+PLAN 覆盖且出现 `risk-high` 新信号时，运行时的 `sol_advice` 返回
  `consult/development/new-risk`（动态咨询）；简单机械任务返回 `none`。
- **未验证 / 限制**：未在独立 GUI 宿主会话中复跑该链路；未做正式 REVIEW（用户未要求）。

## SOL-first 默认认知生命周期（2026-11-20，v9.0.0）

现场基线：`HEAD=d3990c5`；源码工作区含本轮修改的 `sol_review.py`、`review_policy.py`、
`governance.py`、`copilot_dispatch.py`、`copilot_auto.py` 与对应测试/文档；两个未跟踪知识
交接文件（`scripts/knowledge_handoff.py`、`tests/test_knowledge_handoff.py`）保持原样，
未删除、未纳入。

- **模式与生命周期**：`SOL_MODES=("CONSULT","PLAN","REVIEW")`，`THINK` 只作读取别名归一到
  CONSULT；`start/contract --task-kind` 声明 16 类任务，简单/机械类型 0 调用，未声明按
  `standard` 处理。`review_policy.sol_advice` 按 A–E 生命周期给出 `consult`/`plan`/`review`
  建议（起点理解、调查后规划、升级型新信号动态咨询、高复杂阶段复盘、收口判断），
  无硬性调用次数或 Token 上限；同一认知对象无新信息时复用，重复调用要求
  `--force --trigger --reason`。
- **记录与采纳**：`sol_calls` 记录 mode/stage/question/progress_digest/complexity/task_kind/
  usage/latency/zero_tool，采纳结论由 `sol-accept` 单独登记；`review-metrics` 新增
  `sol_cognitive`（覆盖、计数、失败、重复率、采纳、usage/latency，缺失标 `unavailable`）。
  修复了延迟统计只读 `duration_ms` 的缺陷，现优先读 `latency_ms` 并兼容旧字段。
- **失败语义**：CONSULT / PLAN 失败登记真实原因后以退出码 0 降级返回
  `copilot-sol-cognitive-degraded/v1`，不阻断工程；REVIEW 失败仍返回真实非零。
- **测试**：`python3 -B -m unittest discover -s tests -p 'test_*.py'` **604 项通过**（含新增
  `tests/test_sol_first.py` 32 项：简单任务 0 调用、默认起点 CONSULT、调查后 PLAN、产品
  设计/架构咨询、新证据动态咨询、普通步骤不触发、无新信息不重复、合同修订重新覆盖、
  ACCEPT/MODIFY/REJECT、零工具、失败继续、provider 不可用不伪造、PARTIAL 恢复复用、
  项级阻塞隔离、阶段复盘去重、收口 CONSULT 先于 REVIEW、正式 REVIEW 需用户要求、
  G1–G3 不受影响）。`python3 -m compileall -q scripts` 通过。
- **legacy 回归**：`legacy/tests` 853 项（skip 32）失败/错误集合与 `HEAD=d3990c5` 完全一致
  （146 项）；`comm` 对比两侧排序后的失败列表，差异 0 行，无新增回归。
- **真实 SOL 调用（隔离任务 `task-1db809b74a473082`，临时仓库 `/tmp/copilot-sol-first-e2e-20261120/ws`）**：
  1. CONSULT `SCcc56ea6d0d1a8fce103b5f78`：`gpt-6.2-sol`、effort=high、mode=standard、
     `zero_tool=true`、`tools_sent=false`、`tool_choice=none`，usage 2,943 tokens，
     latency 35,418 ms；返回目标理解、策略、2 个备选、3 项取舍、3 项风险、4 项验证信号。
  2. PLAN `SCe6cec088108623bf73380a8f`：同模型与零工具，2,528 tokens，22,641 ms；返回
     路线比较与 A1/A2 优先级。
  两条采纳结论均登记为 `accepted`（`sol-accept`），记录文件分别在 `sol-consult/` 与
  `sol-plans/`。
- **隔离端到端**：`start` → 起点 CONSULT 建议 → 真实 CONSULT → 登记计划 → 真实 PLAN →
  实现 `sort_items.py` 与 4 项 unittest → 证据 E1/E2 → 工作项与条件闭合 → `drive`
  `PASS`/`CLOSED`（`verdict=pass`，G1–G3 通过）。`review-metrics` 的 `sol_cognitive`：
  non_simple=true、default_covered=true、CONSULT 1/PLAN 1/REVIEW 0、failed=0、
  repeat_rate=0.0、acceptance accepted=2、latency_avg=29,029.5 ms、usage 5,471 tokens。
- **运行时同步**：`--apply` 写入 11 个文件，`--check` 缺失 0/不同 0，`--baseline` 重建为
  20 个 Git 跟踪文件，`--verify-baseline` 通过；`scripts/knowledge_handoff.py` 仍为未跟踪、
  未写入基线。
- **未验证 / 限制**：未在独立 GUI 宿主会话中复跑该 E2E（本轮通过宿主 CLI 与本会话执行）；
  未 commit / push（不在授权范围）；未做真实 REVIEW（用户未要求）。

## PARTIAL 续跑修复（2026-11-19，v8.5.0）

现场基线：`HEAD=f8a97b9`；源码工作区含本轮修改的 `governance.py`、`copilot_dispatch.py`、
`copilot_auto.py`、`review_policy.py`，以及未跟踪的知识交接与新测试文件。

- **根因证据**：`ad-opt-sdk-2026-11-19` 任务目录只有 `report.md` 与截图，无
  `state.json`/`contract.md`/`plan.json`；会话记录中无 `start`/`drive` 调用；报告以
  “需要用户决策”结束。完整证据见
  `~/.zcode/copilot-runs/copilot-continuation-2026-11-19/A1-root-cause.md`。
- **机制修改**：`evaluate` 输出 `continuation`；`record`/`rerun`/`criterion`/`report`/
  `block`/`unblock` 返回 `EXIT_CONTINUE=10`；`block` 支持 `--work-item`/`--criterion`/
  `--scope task`，`unblock` 恢复冻结项；`derive_work` 跳过被卡住的条件，替代路线执行中
  停止同路径重试，替代路线结束而缺口仍在时派生新的 `AUTO-REPLAN`；依赖被阻塞的下游工作
  计入“剩余工作受阻”。
- **测试**：新增 `tests/test_continuation.py`（27 项，覆盖用户要求的 15 个场景，含
  “REPLAN 已结束 + 条件未闭合仍给出具体下一步”与“部分解除阻塞后不得继续声称 BLOCKED”两项
  状态迁移回归）；`python3 -B -m unittest discover -s tests -p 'test_*.py'` 570 项通过
  （manifest 基线在同步步骤重建）；`legacy/tests` 失败集合与 `HEAD=f8a97b9` 完全一致
  （146 项，无新增回归）。
- **SOL REVIEW（用户明确要求，一次）**：`SCb338ed575c71466495d5088d` →
  `verdict=partial`；F1（major）“已完成的 REPLAN 抑制派生”已核实并按上条修正（新增
  `test_11b`）；F2（minor）“drive 的 BLOCKED 检查只保证输出自洽”不改门禁，改为用
  `test_12d` 在求值层验证部分解除阻塞后的状态迁移。
- **未验证**：真实宿主会话中的提前停止复发验证尚未执行；本轮未 commit / push。

## v8.5.0 发布与宿主端到端验收（2026-11-20）

- **提交**：`00bc903`（`feat(copilot): harden partial continuation and scoped blocking`），
  16 个文件（15 修改 + `tests/test_continuation.py` 新增），+1133/−101；未跟踪的
  `scripts/knowledge_handoff.py` 与 `tests/test_knowledge_handoff.py` 按既有指令保持不纳入。
- **推送**：`origin/main` `f8a97b9..00bc903` 普通快进；本地 `HEAD`、`@{u}`、`origin/main`
  三方同为 `00bc903`，ahead/behind `0/0`。
- **运行时同步**：`--apply` 0 个文件（已一致）、`--check` 缺失 0/不同 0、
  `--verify-baseline` 通过；运行时 `scripts/governance.py`、`SKILL.md` 与源码 sha256 相同，
  `copilot_dispatch.py __version__=8.5.0`。
- **宿主 E2E（隔离临时任务，两个独立宿主会话代理，未改业务仓库）**：
  1. 分项阻塞链路：`block --work-item A2 --criterion C2` 仅冻结 A2 且 `drive` 继续下发 A1；
     `record`/`criterion`/`unblock` 返回 10；外部凭据出现后 `unblock` 恢复 A2；
     最终 `PASS`/`CLOSED`，G1–G3 全 PASS。
  2. PARTIAL 报告链路：A1 done、C1 未核销时 `report` 返回 **10** 并给出 `AUTO-C1`；
     同一回合继续派生执行、`criterion` 10、`drive` 0 收口；最终 `CLOSED`/`verdict=pass`。
- **宿主限制（未完成项）**：GUI 新会话（`sess_424eee40…`）在首次工具调用处停在宿主
  `needs_attention: approval`，未获人工点击前无法继续；E2E 由宿主子会话代理完成，
  不是同一 GUI 会话。未绕过宿主审批。
- **未验证**：GUI 根会话内的同链路观察（受上述审批门限制）。

## 全面评审与加固（2026-11-18，v8.4.0 基线）

现场基线：`HEAD=2035755`、`origin/main=2035755`、运行时与源码受管理文件一致；工作区保留
两个未跟踪知识交接文件（`scripts/knowledge_handoff.py`、`tests/test_knowledge_handoff.py`），
未删除、未纳入提交。

本轮已修复的确认缺陷：

- **SOL pending 恢复**：pending 文件名使用 subject 摘要，但旧代码拿提示词文本哈希
  （`materials_sha256`）比较，真实失败窗口下永远无法恢复，可能重复付费；现在单独保存
  `subject_sha256` 并兼容旧文件。
- **SOL 建议命令可执行性**：合同修订后建议命令未带 `--force`，会被
  `single_round_decision` 直接拒绝；建议的 `second_round`/`trigger` 现在按同模式历史记录
  计算，与 CLI 准入一致。
- **跨计划工作项 id**：同一 id 跨计划登记会让 `_by_id` 永远命中第一条、第二条永远
  pending；现在显式拒绝跨计划重复。
- **历史 G4 工作项**：`origin=g4-review` 的旧动作不再参与可领取、停摆、G1 或恢复派生。
- **execution binding**：`execution` 核验 `metadata.prompt` 含 nonce 且
  `metadata.createdAt` 晚于 `binding.created_at`；缺失或不符失败关闭。
- **合同测试策略**：`ingest` 实际执行 `require_test_result` / `require_artifact_integrity`，
  合同 `report`/`artifacts` 必须是工作区内、存在且已登记的文件证据。
- **reopen 报告失效**：重开使所有活动质量报告 `reopened_at` 生效，普通 PASS 报告也必须
  重新验收，不能直接再次收口。
- **manifest**：`--baseline` 只记录 Git 跟踪文件；新增 `--verify-baseline`。当前基线 20 个
  跟踪文件，未跟踪的 `knowledge_handoff.py` 不进入基线。
- **P2/P3**：多计划原生派发与工作单同集合；`_write_dispatch` 拒绝写 CLOSED 任务；
  证据/派发/基线临时文件改为不可预测文件名 + 原子替换；敏感路径检查覆盖符号链接
  realpath；删除 `review_stops`/重入准入/旧 admission/`--plan-mode` 等死代码。
- **文档**：`agent-bridge.md` 移除 `sol-advisory` 现行能力；`review.md` 改为复杂度/覆盖
  路由；`history.md` 补 v8.4.0；`runtime.md`/`AGENTS.md` 修正“按风险自动 REVIEW”旧口径；
  `DECISIONS.md` 给 G4/Reviewer 历史条目加“历史/已被取代”标记。

验证：活动测试 **543 项 OK**（含 17 项未跟踪知识交接测试；Git 跟踪部分 526 项）；
`legacy/tests` 853 项与历史基线同集合（failures 99 / errors 47 / skipped 32）；
`sync_runtime.py --check` 与 `--verify-baseline` 均通过。

### 独立 REVIEW（2026-11-18，用户授权一次）

`sol-review`（`gpt-6.2-sol`，零工具、单轮）在 `repair` 前快照上返回 `verdict=partial`：
blocking 0 / major 2 / minor 0 / info 0。两条 major 均已处理：

1. **旧格式 pending 回退仍无法恢复**：原修复只覆盖新 `subject_sha256`；现在旧格式按
   `sha256(prompt)` 重算校验，能恢复且不会接受其他材料的 pending。
2. **测试策略校验偏字段存在性**：现在 `require_test_result` 在声明测试命令时要求
   `test_artifacts`；测试产物与合同 report/artifacts 的 mtime 必须不早于 binding，
   并必须已登记为文件证据（SHA-256 由登记时计算）。

REVIEW 的 `unverified` 项（并发同 subject、真实宿主 metadata 来源、干净 checkout 重建
manifest、advisory 消费路径）保持未独立验证；本轮只按现场代码与测试证据处理，不声称
已由 REVIEW 证明。REVIEW 结论是 advisory，不改变 G1–G3，也不构成收口门禁。

## SOL 默认认知引擎（2026-11-18，v8.4.0）

- **THINK 取代 CONSULT / PLAN**：`sol_review.SOL_MODES=("THINK","REVIEW")`；
  `normalize_mode` 把历史 CONSULT / PLAN 归一到 THINK；`sol-advisory` 命令移除，
  新入口为 `sol-think`。历史 `sol_advisories` / `sol_reviews` 只读兼容，历史认知记录
  按 THINK 计入覆盖。
- **路由**：`sol_advice.action` 收敛为 `none` / `reuse` / `think` / `review`；
  高风险或收口阶段不再自动建议 REVIEW，`review-admission --review-request` 是用户要求
  独立复核的唯一自动建议入口。
- **输入预算**：THINK 材料上限 120,000 字符（提示 60,000），REVIEW 保持 400,000；
  `build_prompt` 的 THINK 分支要求目标/基线/已确认事实/已尝试方案/证据摘要/本次问题。
- **输出结构**：`copilot-sol-think/v1`（judgment / essence / strategy / tradeoffs /
  risks / priority / verify / reconsult / unverified），严格类型校验。
- **降级**：THINK 超时、429、网络、格式或身份失败只写入 `sol_calls` 失败记录并返回
  `copilot-sol-think-degraded/v1`（退出码 0）；REVIEW 失败仍显式失败。
- **验证**：HEAD `2035755` 可复现活动测试 **518 项 OK**；当前工作区另有 17 项未跟踪的
  `tests/test_knowledge_handoff.py`，合计 535 项（该文件不进入本轮提交）。
  `legacy/tests` 853 项与 v8.3.0 基线 `1626801` 一致（failures 99 / errors 47 /
  skipped 32）；`sync_runtime.py --check` 缺失 0 / 不同 0；运行时 `sol-think --dry-run`
  实测 `mode=THINK`、`zero_tool=true`、`tool_keys_present=[]`、`prompt_version=sol-prompt-v3`。
- **真实调用（2026-11-18，用户授权）**：`copilot_dispatch.py sol-think` 实际调用
  `gpt-6.2-sol`，`reasoning.effort=high`、`reasoning.mode=standard` 回显一致
  （`reasoning_verified=true`）；`tools_sent=false`、`tool_choice="none"`、
  `zero_tool=true`、`attempts=1`、HTTP 200，延迟 55.8s，tokens input 1756 / output 1711 /
  reasoning 386；返回符合 `copilot-sol-think/v1`（判断/本质/策略/取舍/风险/优先级/
  验证信号/再咨询条件/未验证项齐全），记录 `SC31f97abfe6c297a283ce5674`。只发一次请求，
  未重试、未触发额外 REVIEW。本轮变更提交并普通快进推送 origin/main。

## 知识交接扩展（2026-11-18）

- 新增 `scripts/knowledge_handoff.py`：只接受 Vault/Memory 卡片；`read` 摘录必须出现在
  卡片正文中，`index-only`/`unverified` 不得冒充已读；最多 5 条，SOL 8,000 字符、
  子 Agent 5,000 字符。
- `sol-think`/`sol-review` 新增 `--knowledge-file`，校验后追加到 materials；dry-run
  与 `sol_calls` 记录 `knowledge_file`、`knowledge_block_sha256`、`knowledge_entries`。
- 验证：HEAD 可复现活动测试 **518 项 OK**（37.3s）；`sync_runtime.py --check` 缺失 0 / 不同 0；
  场景 A（新会话 Read 卡片 + Copilot 交接 + SOL CONSULT 收到摘录）、B（无关任务 0 工具）、
  C（跨会话复用 `code-quality-perf-cleanup-2026-11-18`）、D（Explore 显式交接、0 工具）
  均完成。
- 未改业务项目、Memory/Vault 正式知识；未提交/推送。

## SOL 主动介入策略（2026-11-18，v8.3.0）

基线：HEAD `ad27c2f`（`origin/main` 一致、工作区干净），运行时 v8.2.0 受管理 20 文件、
`--check` 缺失 0 / 不同 0。

### 基线审计（现场实测，不是按文档推断）

用同一构造脚本在本仓库 v8.2.0 上实测七种任务形态：

- 简单低风险（1 工作项）、普通中风险（1–3 工作项）、多模块复杂（5 工作项 + 依赖）、
  高风险（2–5 工作项）在**开发期**全部得到 `recommended_action=not_needed`；
  原因：`review_admission` 只在 G1–G3 全绿时采用风险建议，门禁一旦有缺口（开发期必然）
  就一律 `not_needed`。
- `evaluate()` 在 RUNNING/PARTIAL 不含任何 SOL 字段；`drive` 不提示；只有 PASS +
  `risk=high` 时在 `next_commands` 追加一句可选提示（`drive` 命中 PASS 会直接收口，
  该提示实际不可执行）。
- 没有复杂度判断；没有“已咨询过”的覆盖判断（记录一次 CONSULT 后准入仍返回 `not_needed`）。
- 单轮去重只依赖 `materials_sha256`（提示词版本/模型/强度/目标/合同/条件/证据/材料）。

### 实施

- **复杂度**：`review_policy.task_complexity` 只用状态事实给出 `low/medium/high` 与信号：
  `risk-high`、`declared-trigger`（8 个准入触发因素）、`multi-module`（证据跨 ≥2 个
  相对工作区目录）、`multi-action`（计划内工作项 ≥4）、`interdependent`（依赖边 ≥2）、
  `replan-history`（`AUTO-REPLAN`）、`reopened`（`closure.reopen_history`）。只统计计划内
  工作项，治理派生的 `AUTO-*` 与历史 `g4-review` 项不计入；工作区外的证据不计入模块数。
- **建议**：`review_policy.sol_advice` 返回 `none` / `reuse` / `consult` / `plan` /
  `review` ＋ `recommended_action` / `mode` / `phase` / `new_signals` / `command`。
  中高复杂任务在“有计划或证据”后建议一次 PLAN（无计划）或 CONSULT（有计划）；没有现场
  上下文时返回 `none`（先调查）。
- **覆盖**：按 `contract_sha256` 判定覆盖；只有升级型信号（`risk-high` /
  `replan-history` / `reopened`）新出现或合同被修订才允许追加一次，命令带
  `--force --trigger new-risk|scope-changed --reason`。结构变化（证据目录、工作项增减）
  不诱发重复调用；旧记录缺 `complexity_signals` 时按已覆盖处理。
- **展示**：`review-admission`（文本 + JSON `advice` / `recommended_mode` /
  `recommended_phase` / `complexity`）、`evaluate()` 的 `sol` 字段、`drive` 工作单前的
  提示与 `status` 的下一步。不派生工作项、不改变 phase、不影响 G1–G3。
- **记录**：SOL 调用记录新增 `risk` / `complexity` / `complexity_signals` 快照，追加判断
  只比较快照。
- **收口顺序**：`drive` 命中 PASS 即收口，因此收口前的 REVIEW 建议在 PASS 之前的
  `status` / `review-admission` 给出；`drive` 在 PASS 步不再打印无法执行的复核提示。
- **未采纳**：代码快照或计划 sha 进入复用键（代码每轮变化、计划常增量登记，会诱发
  “每次改动再问一遍”）；用复杂度决定收口或派生工作项（会把 advisory 变成门禁）。

### 测试

- 活动测试：基线 `469 OK / 34.5s` → 本轮 `501 OK`（多轮 36.9–39.6s）（新增
  `tests/test_sol_trigger.py` 32 项：复杂度分类、简单任务 0 调用、中高复杂主动介入、
  重复抑制、实质变化重调、G1–G3 不自动调用、失败继续、advisory 语义、用户显式请求、
  BLOCKED/CLOSED 不建议、无 G4、`sol_calls` 历史兼容）。
- 历史回归：`legacy/tests` 853 项，失败/错误 146 条；与基线 worktree（`083fe2e`）逐条
  比对**集合完全相同**（`diff` 为空），无新增回归。
- 运行时副本手动验证：简单任务 `none`、跨模块任务 `consult`、同材料 `sol-advisory`
  复用（exit 0、零请求）、SOL 结论不改变 `closure-check`。

### 性能（同机 A/B，baseline = `git worktree` 于 `083fe2e`）

| 指标 | 基线 | v8.3.0 | 差异 |
|---|---|---|---|
| `review_admission`（进程内，中位数） | 0.24ms | 0.26ms | +0.02ms |
| `evaluate`（进程内） | ≈0.0ms | ≈0.1ms | +0.1ms |
| CLI 墙钟（`status` / `closure-check` / `review-admission` / `review-metrics` / `sol-review --list` / `drive`） | 72–132ms | 73–132ms | −1.4 ~ +6.9ms（批次间噪声 ≤9ms） |

新增开销只发生在内存中的字典遍历：不加 `git` 调用、不读文件、不发起网络。SOL 单次调用
的提示词与请求体不变——本轮只改“是否建议调用”。

### 运行时同步

`sync_runtime.py --apply` 写入 8 个文件（SKILL.md + 3 references + 4 scripts），
`--check` 缺失 0 / 不同 0 / 多出 0；`review_policy.py`、`governance.py`、
`copilot_dispatch.py`、`copilot_auto.py`、`SKILL.md` 的 SHA-256 与源码一致。

### 未验证

- 本轮未发起真实 GPT-6.2 调用（触发策略是本地确定性判断，调用链、请求构造与模型参数
  未改）；“建议→实际调用”的真实延迟沿用 2026-11-18 的 v8.2 实测数据。
- 建议是否**减少**真实项目的重复调用，需要后续真实任务统计才有证据。

## SOL 与 G1–G3 融合（2026-11-18，v8.2.0）

基线：HEAD `802a875`（`origin/main` 一致，工作区干净），运行时 `~/.zcode/skills/copilot`
受管理 20 文件、--check 缺失 0 / 不同 0。本工作已提交并普通快进推送到 `origin/main`
（`083fe2e`），推送后完成运行时同步与一致性复查。

### 架构审计：真实调用链与重复点

现场源码确认（不是按文档推断）：

- **已有**：REVIEW / CONSULT / PLAN 共用 `_run_sol_channel` → `run_sol` → 一次 HTTP；
  G1–G3 是本地确定性门禁；SOL 是 advisory，不参与阶段判定。
- **重复点与根因**：
  1. 记录分 `sol_reviews` / `sol_advisories` 两套：两套审计文件、两把锁、两个 ID 前缀
     （SR/SA）、四个近乎复制的登记/失败函数；指标与准入也各按历史键读取。
  2. `build_materials` 与 `build_advisory_prompt` 除尾部要求外几乎逐行重复。
  3. `review-admission` 只有风险策略，不读取门禁事实：门禁未绿时仍可能建议调用 SOL。
  4. `build_materials` / `build_advisory_prompt` 读取不存在的 `contract["text"]`，合同正文
     （实际在 `done_criteria_text`）从未进入 SOL 上下文；证据登记也未注入，SOL 与 G2
     看到的不是同一份证据来源。
- **保留的不变量**：G1–G3 语义与 fail-closed、合同指纹/证据摘要重读、Finding 台账门禁、
  PWW 接受、授权边界、SOL advisory/零工具/单轮、ZCode 唯一执行与技术决策主体。

### 实施

- **单一记录流**：`sol_calls`（`call_id` / `mode` / `prompt_version` / `evidence_digest` /
  用量与耗时）；`iter_sol_calls` 合并统一流 + 历史 `sol_reviews` / `sol_advisories`（按 ID
  去重、按时间排序，不迁移、不改写）。`list_sol_reviews` / `list_sol_advisories` /
  `review_metrics` / `review_admission` / `evaluate` 全部改读同一流。
- **单一管线**：`build_prompt` 取代两个提示词构造器；`record_sol_call` /
  `record_sol_call_failure` 取代四个按模式复制的函数（旧函数名保留为薄封装）；
  `_sol_channel_config` 只保留原始输出目录、文案与预检 schema。审计统一
  `sol-audit.jsonl`，互斥统一 `.sol.lock`，ID 统一 `SC`。
- **共享上下文**：提示词自动注入目标、完整合同正文（`done_criteria_text`）与边界、
  完成条件、工作项状态、证据登记摘要（ID/类型/摘要前缀/大小/支持条件/归档标记，上限
  80 条 / 6000 字符）。修正了合同正文从未注入的事实缺陷。
- **确定性优先**：`review-admission` 增加 `gates`（G1–G3 原因码）与
  `recommended_action`：门禁有缺口时 `not_needed`（先修机械缺口），全绿后才按风险返回
  `optional` / `recommended`；`decision` 保留风险策略原义以兼容旧输出。
- **不并入报告/证据**：SOL 结论是建议，写进 `reports`/`evidence` 会让模型文本被当成完成
  证据引用，破坏 G2 语义；因此只统一调用记录与 `history` 事件（`sol_call_completed` /
  `sol_call_failed`），REVIEW findings 照旧经 Finding 台账由主代理核实推进。
- **缓存失效**：复用键 = `PROMPT_VERSION` + 模型/推理强度 + 目标 + 合同正文/边界 +
  条件描述 + 证据集合摘要 + 材料（辅助分析另加模式与问题）。条件状态变化不失效；
  合同、证据、提示词版本或材料变化即失效。
- **测试**：更新写路径断言到 `sol_calls`；新增 `tests/test_sol_fusion.py`（17 项）覆盖
  单一记录流、历史兼容、共享上下文与上限、复用键失效、门禁优先准入、SOL 不改变阶段/
  门禁、失败不停摆、审计与锁收敛。

### 性能（同机 A/B，baseline = `git worktree` 于 `802a875`）

同一基准脚本在同一台机器上构造同构任务（12 条证据、2 个工作项、200 文件工作区），
每条命令 5 次取中位数、两组各重复 4 轮再取中位数；`python3 -B` + 预算字节码。

| 指标 | 基线 802a875 | v8.2.0 | 差异 |
|---|---|---|---|
| `status` | 127.0ms | 123.4ms | −3.6ms |
| `closure-check` | 139.8ms | 133.1ms | −6.7ms |
| `review-metrics` | 133.0ms | 122.0ms | −11.0ms |
| `review-admission` | 131.1ms | 123.8ms | −7.3ms |
| `sol-review --dry-run` | 130.6ms | 122.2ms | −8.4ms |
| `sol-review --list` | 129.8ms | 123.8ms | −6.0ms |
| REVIEW 提示词 | 3,982 字符 | 5,230 字符 | +31% |
| CONSULT 提示词 | 3,998 字符 | 5,276 字符 | +32% |
| 状态文件 | 14,354 B | 14,328 B | −26 B |
| 活动测试 | 452 / 34.8–36.7s | 469 / 37.0–37.6s | +17 项，+约 1.9s |

- 确定性命令全部在 120–140ms 区间，差异在解释器启动与 I/O 噪声内：**没有实质开销增加**。
- 提示词增大来自本轮要求的“共享合同/条件/证据”注入（约 +300 tokens/次）；主代理相应地
  不必重复粘贴这些事实，调用次数不变。
- **避免重复调用的实测**：同一主体摘要第二次调用时 transport 调用数为 0（mock 断言）；
  门禁未绿的新任务上 `recommended_action=not_needed`，按 v8.1 的风险策略会建议一次
  REVIEW（对应 13.1s / 1,166 tokens 的真实基线，见下节）。
- **基线缺失（如实说明）**：仓库没有历史任务的 SOL 调用次数与 Token 遥测，无法给出
  “端到端任务耗时下降 X%”的真实百分比；上表只覆盖可重复的确定性路径与提示词体积。

### 融合后真实 API 验证（单次调用，无重试）

`sol-advisory --mode CONSULT` 走融合后的 `build_prompt` → `run_sol` → `record_sol_call`，
材料只写一句说明，合同/条件/工作项/证据全部由任务状态自动注入：

| 项 | 实测值 |
|---|---|
| 记录 ID | `SC481eb53c3c687460fb69585e`（统一 `sol_calls`，`mode=CONSULT`） |
| `model_resolved` | `gpt-6.2-sol`（与请求一致） |
| reasoning 回显 | `effort=high`、`mode=standard`，`reasoning_verified=true` |
| 提示词版本 | `sol-prompt-v2`（注入证据 1 条、工作项 2 个、合同正文与条件） |
| usage | input 838 / output 591 / reasoning 0 / total 1,429 |
| 延迟 | 16.6s（`latency_ms=16610`） |
| 追踪 | `response_id=resp_01a8f9ed…`、`trace_id=20261118111726-…`；CPA 未返回 `X-Request-Id`，`request_id` 为空（如实记录） |
| 零工具 | `tool_keys_present=[]`、`unsupported_params_present=[]` |
| 上下文理解 | 模型正确指出 C1 已闭合、C2 未闭合、E1 支持 C1，并把“未提供 E1 正文/复核结论”列入 `unverified` |
| 登记 | 状态 `sol_calls` 1 条 completed；审计 `sol-audit.jsonl` 写入成功（`audit_written=true`） |

验证任务状态在仓库外 `/tmp/copilot-v82-api-verify-20261118/`，未纳入仓库。

**验证后修复**：该次调用暴露原始输出文件名使用登记前的随机 ID，与记录 `call_id` 不一致
（`SCfebfc0…raw.txt` vs `SC481eb5…`）。已改为记录直接采用同一 `record_id`，并新增
“`reply_path` 基名 == `call_id`”断言；`tests/test_sol_fusion.py`（17）与
`tests/test_sol_review_v7.py`（66）重跑通过。请求/响应管线未改动，未重复发起真实调用。

### 验证与未验证

- 活动测试 `python3 -B -m unittest discover -s tests -p 'test_*.py'`：469 项通过
  （修复前 3 轮：37.1s / 37.0s / 37.6s）；修复后定向重跑 17 + 66 项通过。
- 历史回归 `legacy/tests` 与同步后运行时一致性见本轮交付记录；运行时同步后重新执行
  `--check` 与关键文件 SHA-256 抽查。
- **未验证**：融合后一次真实 REVIEW 的端到端耗时未复测（本轮真实验证使用 CONSULT）；
  真实调用只覆盖单次链路，未做并发或压力验证。
- **历史状态实测缺口**：本机 `.copilot-tasks/` 只有 v6.12 任务、无 SOL 记录，历史
  `sol_reviews` / `sol_advisories` 兼容性由构造状态测试覆盖（含无 `mode` 字段的历史记录）。

## GPT-6.2 SOL 升级与按需认知辅助（2026-11-18，v8.1.0）

基线：HEAD `20db0ba`（v8.0.1，`origin/main` 一致，工作区干净）。**本轮只做源码/测试/文档与
运行时同步，未提交、未推送。**

### 真实 API 调用（本机 CPA `127.0.0.1:8317`，`/v1/responses`）

| 模式 | 记录 ID | model_resolved | reasoning | usage（in/out/reasoning/total） | 延迟 | 结果 |
|---|---|---|---|---|---|---|
| 探针（最小请求） | — | `gpt-6.2-sol` | `effort=high`, `mode=standard` | 24 / 5 / 0 / 29 | 2.5s | HTTP 200 |
| REVIEW | `SR83dd3ef8e096691dd4954fbb` | `gpt-6.2-sol` | `high` / `standard` | 656 / 510 / 220 / 1166 | 13.1s | verdict `blocked`（材料不足以判断 C1，如实写入 unverified） |
| CONSULT | `SA733acd0064b6fd76494b4768` | `gpt-6.2-sol` | `high` / `standard` | 644 / 655 / 0 / 1299 | 23.5s | `options` 2 条 + `risks` + `recommendation` |
| PLAN | `SAde96f3d5cc5d04dc950eab7d` | `gpt-6.2-sol` | `high` / `standard` | 640 / 1216 / 90 / 1856 | 33.2s | `steps` 4 条（含 `depends_on`）+ `risks` |

- 推理强度证据：响应 `reasoning` 字段回显 `{"effort":"high","mode":"standard"}`，三条都记录
  `reasoning_verified=true`；请求体审计记录 `reasoning_effort=high`、`reasoning_mode=standard`。
- 追踪：`response_id`（`resp_…`）与 CPA `trace_id`（`X-Cpa-Trace-Id`）写入状态与审计；CPA 未
  返回 `X-Request-Id`，`request_id` 为空（如实记录，不伪造）。
- 零工具：请求体 `payload_keys` 只有 `model/input/instructions/reasoning/max_output_tokens/
  store/stream/tool_choice`，`tool_keys_present=[]`、`unsupported_params_present=[]`。
- 单轮复用：同材料再次调用 REVIEW / CONSULT 直接返回已有记录，未新增请求。
- 首次 REVIEW 曾因把 GPT-6.2 固定回显的全零 `tool_usage` 误判为工具使用而失败
  （`SRf3f693b9e211fb28310e3c6c`，审计 `ok=false`，HTTP 200）；改为只对非零用量/工具定义
  拒绝后重试成功。该失败记录与 usage 保留在状态与审计中。
- 验证任务状态在仓库外临时目录 `/tmp/copilot-sol-verify-20261118/`，未纳入仓库、未提交。
- CPA 日志旁证（`~/.local/opt/EasyCLIProxyAPI/oauth/logs/core-start-output.log`）：
  `POST /v1/responses` 在 10:02:02（20.02s）、10:02:59（13.07s）、10:03:28（23.46s）、
  10:04:06（33.14s）均为 HTTP 200，与审计 `elapsed_ms` 对应；日志不含模型名，模型身份仍以
  响应回显与 trace ID 为准。

### 范围与设计取舍

- **仅非流式**：请求固定 `stream=false`，未实现流式解析；SOL 是一次性建议，不需要增量输出。
- **不自动重试/退避**：保持仓库既有的“一次调用一次请求”硬约束与“不重复付费”原则。429/5xx/
  超时/网络失败如实登记并退出，由主代理在材料或范围实质变化时用
  `--force --trigger <...> --reason` 显式重新发起（本地有限、可审计的人工重试路径），
  不引入自动重试、Provider 切换或 `AUTO-*` 派生。
- **治理记录**：本轮另有仓库外治理任务 `task-v81-sol-upgrade-20261118`（G1–G3 PASS、
  `CLOSED`，verdict=pass），证据为活动/定向测试输出、真实 API 摘要、运行时同步复查、
  基线清单校验与 legacy 基线对比；状态与证据在 `/tmp/copilot-v81-governance-20261118/`，
  未提交。

### 已实施

- `scripts/sol_review.py`：`MODEL="gpt-6.2-sol"`；`validate_effort` 只接受 `high`；
  CONSULT / PLAN / REVIEW 三种模式提示词与输出契约；`run_sol` / `run_review` /
  `run_advisory`；不支持采样参数守卫；`reasoning` 回显校验（effort/mode 不一致拒绝，
  缺失记 `reasoning_verified=false`）；非零工具用量与工具定义拒绝；请求/追踪 ID 采集。
- `scripts/copilot_dispatch.py`：新增 `sol-advisory --mode CONSULT|PLAN`（零工具、单轮、
  `--question`、`--dry-run`、`--list`）；REVIEW 与 advisory 共用一次调用路径与失败/pending
  恢复；版本 `8.1.0`。
- `scripts/governance.py`：新增 `sol_advisories` 状态与 `record_sol_advisory` /
  `record_sol_advisory_failure` / `list_sol_advisories`；`review_metrics` 增加 advisory 计数。
  辅助分析不进入 Finding 台账、不影响 G1–G3。
- 测试：`tests/test_sol_review_v7.py`（模型/推理强度/三模式/回显/工具回显/CLI/advisory
  状态）、`tests/test_architecture.py`（唯一远程模块、三模式、无 DECIDE、无执行入口）、
  `tests/test_docs_v6.py`（文档契约）。

### 验证

- 定向：`tests.test_sol_review_v7` + `tests.test_architecture` + `tests.test_docs_v6`
  96 tests OK。
- 活动测试：452 tests OK。
- **历史回归（重要事实）**：`legacy/tests` 在本机基线 `20db0ba` 上就是
  `853 tests, failures=99, errors=47, skipped=32`，原因包括 v5 时期 `references/role.md`
  等文档已退役、`legacy/call_gpt.py` 仍按旧路径读取。本轮用 `git archive HEAD` 导出干净
  基线并逐条对比：失败/错误用例集合与本轮完全相同（146 条，`diff` 为空），因此本轮**没有
  新增 legacy 回归**；这些是基线既有的历史环境失败，未在本轮修复（超出本任务范围）。
- 运行时：`--apply` 写入 9 个文件并预编译 13 个；`--check` 缺失 0、不同 0、多出 0；
  `--baseline` 更新为 20 个受管理文件。用运行时副本执行 `sol-advisory --dry-run` 成功返回
  `gpt-6.2-sol` / `high` / 零工具。
- **未验证**：CPA 内部上游路由与是否内部重试（只能校验响应回显 model/reasoning 与 trace
  ID）；官方 GPT-6.2 文档 URL 在本机不可达（WebFetch 403、DeepSeek Fetch 404），兼容性以
  真实接口行为核实；多轮 `previous_response_id` 未实现（按任务要求保持单请求）；大材料
  （接近 400,000 字符）与并发压测未做。

## SOL 安全与效率加固（2026-11-17，v8.0.1）

基线：HEAD `55058ce`（v8.0.0）。**本轮只做源码/测试/文档与运行时同步，未提交、未推送。**

### 真实 SOL 复核（一次，零工具）
- 模型 `gpt-6.1-sol`（本机 CPA `127.0.0.1:8317`，`reasoning.effort=high`），回显一致；
  请求体无 `tools`/`functions`，`tool_choice="none"`，一次请求、无重试。
- 材料 154,821 字符；usage input 51,827 / output 7,481（reasoning 3,805）/ total 59,308；
  耗时 234,984 ms；原始输出 `SR7680218d6f76e7c458b37374.raw.txt`。
- verdict `partial`：0 blocking / 4 major / 6 minor / 1 info / 11 unverified。逐项结论见
  `docs/STATUS.md` 下方的“SOL findings 处置”。

### 已实施
- **F1 错误详情脱敏**：`_safe_detail`/`_safe_url` 统一脱敏限长（拒绝端点、上游错误正文、
  模型回显、传输异常）；审计/状态/stderr 不再回显完整 URL 或未净化正文。
- **F2 路由校验**：`run_review` 对最终 `base_url` 无条件执行本机 CPA 校验，且在解析凭据前
  失败；本机请求显式 `ProxyHandler({})`，不经过环境代理。
- **F3 advisory 提级**：SOL 不能把已解决的 authoritative finding 单方面提级为不可驳回阻断；
  单独登记 `escalation_of` 的 advisory 条目，原记录状态与严重度不变，可按误报驳回。
- **F4 防重复付费**：远程成功后先写可恢复结果（`sol-reviews/pending-<digest>.json`）再登记；
  审计/原始输出失败只告警，不再把成功结果登记为 failed；同材料再次调用直接恢复；
  `sol-review` 用任务级 `task_lock` 互斥。
- **F5/F6 台账与 ID**：同一 `review_sha` 重复合并完全幂等；重复 finding ID 的后备编号保证唯一。
- **F7/F11 效率**：dry-run 与真实调用共用材料校验；非 dry-run 不再重复构建 payload。
- **F8/F9/F10 文档**：正式 REVIEW 独立性改为 SOL 路径；活动文案统一 G1–G3；STATUS 更正
  high 准入 scope 为 `high-risk`。

### 验证
- 定向：`tests.test_sol_review_v7` + `tests.test_review_efficiency` 93 tests OK（含新增 F1/F2/F4/F5/F6/F7 回归）。
- 活动测试：430 tests OK（T2 33.9s）。
- 历史回归：853 tests OK（skipped=32）。
- T4 隔离验收：`tests/t4_acceptance.py` 全部 layers（T0 29 / T1 78 / T2 430 / T3 853）与
  18 个场景 OK。
- 运行时：`--baseline` 25 文件；`--apply` 写入 8 个文件并预编译 13 个；`--check` 缺失 0、
  不同 0、多出 0。
- 治理任务：本轮 `task-v8-sol-review-20261117` 使用仓库外临时状态，G1–G3 PASS、CLOSED，
  11 条 finding 全部 `verified`；临时状态、证据与原始输出未提交。
  收口后补了一处 advisory 提级重复去重（同一提级只保留一条），由 T4 final2 验证；按规则
  未在无用户授权时 reopen 重新登记。
- **未验证**：CPA 内部实际上游身份与是否内部重试（只能校验回显 model）；并发压力下锁的
  实际排队时长（互斥逻辑有单元级恢复测试，未做多进程压测）；pending 恢复只做故障注入级
  验证，未在生产磁盘故障下演练。

### SOL findings 处置

| ID | 级别 | 结论 | 处置 |
|---|---|---|---|
| F1 | major | 成立 | 新增 `_safe_detail`/`_safe_url`，错误详情脱敏限长；不再回显完整 URL/正文 |
| F2 | major | 成立 | `run_review` 对最终 `base_url` 无条件校验，且在解析凭据前失败；禁用环境代理 |
| F3 | major | 成立 | advisory 提级 authoritative 记录时改为单独登记可驳回条目，原记录不变 |
| F4 | major | 成立 | 先落 pending 可恢复结果再登记；审计/原始输出失败只告警；任务级互斥锁 |
| F5 | minor | 成立 | 同一 `review_sha` 重复合并完全幂等 |
| F6 | minor | 成立 | 重复 finding ID 后备编号保证唯一 |
| F7 | minor | 成立 | dry-run 与真实调用共用材料校验 |
| F8 | minor | 成立 | 正式 REVIEW 独立性说明改为 SOL 路径 |
| F9 | minor | 成立 | 活动文案统一 G1–G3 |
| F10 | minor | 成立 | STATUS 更正 high 准入 scope；说明历史 `review_policy.admission` 非活动入口 |
| F11 | info | 成立 | 非 dry-run 不再重复构建 payload |

## G4 退役与 SOL 统一（2026-11-17，v8.0.0）

基线：HEAD `873112d`（v7.0.0）。**本轮只做源码修改，未提交、未推送。**

### 已实施
- **G4 活动机制退役**：删除 `g4_attest.py`、`g4_shadow.py`、`test_execution.py`、
  `review_output.py`；`native_agent.py` 只保留 `bind-execution` / `execution`；
  `native_bridge` 只校验执行信封；`copilot_auto ingest` 只接受执行信封
  （复核信封返回 `native_review_retired`）。
- **状态机与门禁**：`closure_check` 返回 G1/G2/G3；`_gate_findings` 对所有风险等级生效；
  `derive_work` 不再派生 `AUTO-G4*`；`review_metrics` 只统计 SOL/准入/停止/台账，历史 G4
  计数标记 `g4.retired`；`start_state` 的 `risk=high` 不再要求 `--backend native`。
- **SOL 承接**：活动准入 `governance.review_admission`（经 `sol_review.admission`）的 high
  分支为 `recommended` + `scope=high-risk`（不再有 `g4_separate`）；历史
  `review_policy.admission` 只保留给兼容测试，不是活动入口。SOL 仍零工具、单轮、advisory。
- **历史兼容**：历史 `native_reviews` / `review_attempts` / `g4_attestations` 原样保留，
  只读审计；`legacy-status`、历史 G4 台账与 authoritative finding 保护级别不变。
- **版本**：`copilot_dispatch.__version__ = 8.0.0`；`SKILL.md` v8.0.0 块新增，v7.0.0 块
  降为历史。

### 验证
- 活动测试：`python3 -B -m unittest discover -s tests -p 'test_*.py'`（G4 专项测试文件
  已随模块删除；其余测试按 v8 契约重写）。
- 历史回归：`legacy/tests` 只验证 v5 行为，另行运行。
- 运行时同步：`scripts-sync/sync_runtime.py --apply` 后 `--check` 缺失 0、不同 0。
- **未验证**：未做真实 SOL 调用（无新增付费授权）；未做 G4 真实宿主 E2E（机制已移除）。

## SOL 单轮复核与 Reviewer 退役（2026-11-17，v7.0.0）


基线：HEAD `f68fafa`（v6.11.0）+ 工作区 v6.12.0 未提交改动。**本轮继续未提交、未推送。**

### 已实施

- 新增 `scripts/sol_review.py`：复用既有本机 CPA `127` 出口与 `gpt-6.1-sol`
  （`reasoning.effort=high`）；请求体无 `tools`/`functions`/`parallel_tool_calls`，
  显式 `tool_choice="none"`；非 `message`/`reasoning` output 即安全事件；模型身份回显
  必须一致；一次调用只发一次请求，不自动重试、不换 Provider。
- `copilot_dispatch.py sol-review`：材料 + 合同/目标/条件拼成一次上下文；单轮判定
  （同主体复用、再次复核需 `--force --trigger --reason`）；结果写入 `sol_reviews`、
  原始输出写入任务目录 `sol-reviews/`、审计写 `sol-review-audit.jsonl`（仅脱敏元数据）；
  失败写入 `sol_reviews`（`status=failed`）与 history，不阻断主代理。
- `review-admission` 改为 SOL 建议：`low→not_needed`、`medium→optional`、
  `high→recommended`（`g4_separate=true`），不写状态、不阻塞。
- Reviewer 退役：`create_review_attempt`、`native_agent.py review`、`review-context`
  与 `copilot_auto._ingest_review` 对 `risk!=high` 返回 `native_review_retired`；
  G4 链路（attempt/attest/shadow/test_execution）保留。
- Finding 台账：SOL findings 以 `authority=advisory` 进入台账；主代理修复并
  `fixed→verified`，误报可用 `dropped --note` 驳回；G4 的 `authoritative` 仍不可驳回。
- 安全加固（由本次真实 SOL 复核发现并修复）：
  - 参考性合并不再把无 `authority` 的历史正式 finding 降级为 advisory；
  - 禁用 HTTP 重定向，3xx 一律失败，避免 `Authorization` 被转发；
  - `unverified`/`materials_reviewed` 与 finding 字段严格类型校验，非法元素拒绝消费；
  - 响应读取中断/连接异常统一映射为 `SolUpstreamError`，进入失败审计与状态登记；
  - Provider 凭据只接受严格 `http(s)://127.0.0.1:8317`（无 userinfo），拒绝远端
    8317 或描述含 "cpa" 的规则；
  - 单轮主体摘要不含条件状态（pending→met 不会导致重复付费），材料/目标/合同变化
    才改变摘要。
- 版本 `7.0.0`；`SKILL.md`、`README.md`、`docs/DECISIONS.md` 第 57 条、
  `docs/PROJECT-CHARTER.md`、`docs/ROADMAP.md`、`references/*`、`AGENTS.md` 同步。

### 真实验证

- **路由**：CPA `/v1/models` 返回 `gpt-6.1-sol`；使用既有 `CUSTOM_127_API_KEY`
  （`~/.reasonix/.env` / ZCode 127 Provider）调用成功，未新增 Provider/账户/密钥。
- **真实 SOL 调用（一次）**：`model=gpt-6.1-sol`、`model_resolved=gpt-6.1-sol`、
  `zero_tool=true`、`attempts=1`、`latency_ms=155612`、usage `input=39318 /
  output=6319 / total=45637`（reasoning 4660）、`verdict=partial`、findings
  `4 major + 1 minor`、`unverified=6`。原始输出与审计见任务目录。
- **结果消费**：5 条 finding 全部逐条核实；4 major + 1 minor 均确认为真实问题并修复，
  以修复 diff 与全量测试证据标记 `fixed→verified`；E2E 任务四门 PASS 并收口。
- **E2E 任务目录**：`/tmp/copilot-v7-e2e-auBj`（临时目录；系统清理后不可用，仅证明
  本次运行）。真实调用使用主代理整理的 139k 字符材料（v7 diff + 新模块 + 测试结果）。
- **零工具/单轮失败路径**：mock 传输覆盖 429、超时、身份不符、非良性 output、
  截断、响应读取中断、重定向、格式异常；均只调用一次且进入失败审计。

### 已验证 / 未验证

- 活动测试（最终代码）：**863 条 OK，82.2s**；legacy 历史回归 **853 条 OK（skip 32），
  94.9s**。
- 运行时同步（最终代码）：`sync_runtime.py --apply` 后 `--check` 缺失 0 / 不同 0；
  运行时 `copilot_dispatch.py --version` = `7.0.0`。
- **未验证**：真实 G4（宿主 3.18.2 没有内置精确只读载体，正式 G4 仍 BLOCKED）；
  真实网络故障注入（429/断连）未做，只用异常/mock 注入；SOL 长期输出稳定性未做
  多任务统计。

## G4 宿主能力校验与调度收敛（2026-11-17，v6.12.0）

基线：HEAD `f68fafa`（v6.11.0，工作区 clean）。**本轮未提交、未推送。**

### 现场证据（真实宿主）

- 宿主版本：`dpkg` 记录 `zcode 3.18.2-10443`；`capability` 从
  `<zcode-resources>/app.asar` 的 `package.json` 读到 `version=3.18.2`
  (`name=@zcode/desktop`)。`host_version()` 每次重新探测，不缓存。
- 工作区只读 Profile 在当前会话真实加载：系统提供的 Agent 列表包含
  `independent-readonly (Tools: Read, Grep, Glob)`；一次实测派发的
  `metadata.profileSnapshot` 为 `tools=[Read,Grep,Glob]`、
  `disallowedTools=[Write,Edit,Bash,WebFetch,WebSearch,Agent,Task,TodoWrite,NotebookEdit]`、
  `source=project`、`workspaceRoot=<repo>`。
- 实测子会话（`sess_subagent_agent_3de333ee-…`）：宿主工具列表实际只有
  `Read/Grep/Glob/RespondToCoordinator`；`totalToolUseCount=2`，`tool_usage` 只有
  `Read`、`Glob`（`read_only=1`），无 Bash/Write/Edit；子会话自述与宿主记录一致。
- 历史 44 个 `independent-readonly` 子会话的 `tool_usage` 聚合只有
  `Read/Grep/Glob`（另有 2 次 `Bash` 为 `tool_not_found`，即工具未注册）。
- **plan 模式缺口**：8 个 `planEnabled=true` 的子会话中，1 个
  `independent-readonly` 会话真实执行了 `git status --short`
  （`status=completed`、`read_only=0`、`duration_ms=102`、输出 ` M src/calc.py`），
  说明 plan 模式会给子代理注入 Bash；同一 Profile 在非 plan 模式下 Bash 不可用。
- 宿主 `RespondToCoordinator` 会注入到所有子代理（`sideEffectScope=session`、
  `permission=agent.message.respond`）；历史复核会话未使用它。

### 已实施

- `host_records.session_execution_state(db, session_id)` 读取
  `session_entry(runtime/execution_state)`；`execution_state_problems()` 归类为
  `host_session_plan_mode` / `host_session_state_missing` /
  `host_session_state_unknown`。
- `g4_attest.attest` 增加子会话模式准入：plan 模式或状态未知 fail-closed；取证记录写入
  `session_mode` / `session_plan_enabled` 与对应 check 码。
- `native_agent.build_review` 在生成信封前做同一准入（`[host_session_*]`），
  `host` 字段记录 `session_mode` / `session_plan_enabled`。
- `native_agent.build_capability` 输出 `host_version`（本次真实探测值，失败回退常量）、
  `host_baseline`（代码基线）、`session_execution` 与
  `host_evidence.session_execution`；`agent_policy.capability_report` 接受
  `execution_state`/`host_info`，plan 模式、状态未知或调用方未提供状态时
  `g4.executor=false`。
- `agent_policy.dispatch_advice(g4_required, plan_mode)`：plan 模式下 G4 返回
  `executor=unavailable`；CLI 增加 `--g4-required` / `--plan-mode`。
- `review_policy.CAPABILITY_REJECTION_CODES` +
  `governance._latest_capability_rejections`：最近一次 G4 取证是能力缺口时派生
  `AUTO-G4-CAPABILITY`，不再机械重派。
- **G4 载体来源约束**：`native_bridge.G4_CARRIER_SOURCE="built-in"` +
  `g4_carrier_problems()`；`agent_policy.classify_profile` 增加
  `profile_source_not_builtin`、`carrier`、`architecture_compliant`；
  `native_agent.build_review`（attempt 或 `risk=high`）与 `g4_attest` 拒绝
  `source!=built-in` 的 Profile。项目级 `independent-readonly` 不再是正式 G4 载体。
- 版本 `6.12.0`；`SKILL.md`、`README.md`、`DECISIONS.md` 第 55/56 条、
  `references/agent-bridge.md` 同步记录。

### 已验证 / 未验证

- 活动测试（最终代码）：**820 条 OK，64.9s**；legacy 历史回归 **853 条 OK（skip 32），
  91.0s**。新增 plan 模式、能力缺口、execution_state、asar offset=0/畸形 header 等用例。
- 运行时同步（最终代码）：`sync_runtime.py --apply` 后 `--check` 缺失 0 / 不同 0；
  运行时 `copilot_dispatch.py --version` = `6.12.0`；8 个未受管理历史文件保留。
- 真实 `capability`（最终代码）：项目级 `independent-readonly` 子会话
  `g4.executor=false (profile_source_not_builtin)`、`carrier=project-profile`、
  `architecture_compliant=false`；历史 plan 模式会话仍是
  `host_session_plan_mode`。此前“`g4.executor=true`”的记录来自来源校验加入之前，
  不再作为当前结论。
- **真实 G4 技术 E2E（PASS/CLOSED，仅技术验证）**：在
  `.copilot-tasks/task-v612-g4-final4` 以 `risk=high, backend=native` 跑通
  Executor → 820 条测试 → attempt `RAdf146916cfc2dd2c470b285b` →
  `independent-readonly` Reviewer（81 次调用，仅 Read/Grep/Glob）→
  `g4_attest` 74 项检查 0 拒绝 → `g4_source=candidate` → `drive` PASS/CLOSED。
  **该 E2E 使用项目级自定义 Profile，按原定架构不属于合规 G4 载体**；它只证明
  技术只读链可用，不作为正式 G4 PASS 证据。证据保留在
  `<repo>/.copilot-tasks/task-v612-g4-final4/`。
- **架构合规结论（2026-11-17 复核）**：
  - `independent-readonly` 是 project 级自定义 Agent（`source:"project"`），不是内置
    Agent；宿主内置只有 `Explore`（含 Bash）与 `general-purpose`（全工具）。
  - 宿主没有派发期工具白名单；会话级 `--disallowedTools`/`permission.disallowedTools`
    会同时限制主代理，不能作为按派发的只读隔离。
  - `RespondToCoordinator` 是宿主无条件注入的控制平面消息工具
    （`agent.message.respond`、session 范围），不能读写文件/执行命令，但使任何子代理
    的“字面精确 {Read,Grep,Glob}”都不可能成立；代码不把它加入白名单。
  - **正式 G4 = BLOCKED（`host_capability_missing`）**：当前宿主没有内置精确只读
    载体；不得用项目自定义 Agent 或放宽权限制造 PASS。
- 代码约束：`source!=built-in` 的 Profile 在 `capability` 为
  `g4.executor=false`，`native_agent review`（attempt 或 `risk=high`）与
  `g4_attest` 直接拒绝（`profile_source_not_builtin`）；`.zcode/agents/
  independent-readonly.md` 仅保留为通用只读排查辅助。
- **未验证**：宿主后续版本是否提供内置精确只读 Agent 或派发期工具白名单；在此之前
  G4 保持 BLOCKED。


## 效率与性能专项（2026-11-16，v6.11.0）

基线：HEAD `0ee2209`（v6.10.0，工作区 clean）。v6.11.0 已提交并普通快进推送
`55df94a`（父提交 `0ee2209`，14 文件 +688/−78），本地 / `origin/main` /
`git ls-remote` 三方一致、0 领先 0 落后；运行时 `--apply` 后 `--check` 缺失 0 / 不同 0，
8 个未受管理历史文件保留，字节码已预编译。

### 现场证据（升级前）

- 单条命令约 200ms，其中约 130ms 为源码重编译：`python3 -X importtime` 显示
  `governance` 自身 92.7ms、`copilot_dispatch` 20.5ms、`native_bridge` 18.3ms；
  而同一份代码在存在有效 `__pycache__` 时 `import copilot_dispatch` 仅 65ms。
  `-B` 只禁止**写入** `.pyc`，因此没有预编译时每条命令都重新编译约 17k 行源码。
- `closure-check` 对同一工作区做 2 次 `capture_code_snapshot`（各约 60ms），
  `drive` 到 PASS 的路径最多 4 次（`evaluate` → `_gate_quality` → `close_task` →
  `quality_summary`）；`derive_work` 对每个 PWW 接受条件各 1 次。
- 每次快照 4 个 git 子进程（各约 13ms）；`_task_workspace_info` 对每个证据路径各跑
  一次 `git_root`。

### 已实施

- `code_scope.capture_code_snapshot(..., cache=None)` + `code_scope_problems(cache=)`：
  单次命令内按 `(workspace, 排除集, base)` 复用快照，命中返回深拷贝；库调用与测试
  默认不缓存，漂移检测仍逐次重算。
- `governance` 侧 `closure_check` / `_evaluate_core` / `evaluate` / `render_status` /
  `derive_work` / `close_task` / `quality_summary` / `_gate_quality` /
  `_gate_code_scope` / `_gate_criteria` / `_gate_review` / `review_attempt_problems` /
  `_review_trust_problems` / `_eligible_review` / `_incremental_review_problems`
  增加可选 `cache`；`drive`、`status`（含 `_brief`）、`closure-check` 入口创建并复用。
- `_gate_criteria` 证据文件重读按 `(path, mtime_ns, size)` 去重。
- `capture_code_snapshot`：`rev-parse --show-toplevel HEAD` 合并为一次调用（失败才分开
  重试）。**否决**“无 `.gitmodules` 跳过 `git submodule status`”：实测该调用在 index
  含 gitlink 而缺 `.gitmodules` 时以 128 失败，旧行为是快照不可用，跳过会静默缩小代码
  范围；保留无条件调用并加回归测试。
- `code_scope.git_root()` 单进程按起始目录缓存**成功**结果（非仓库不缓存）。
- `sync_runtime.py --apply` 同步后 `compileall` 运行时 `scripts/*.py`（只编译受管理
  文件，运行时里未受管理的遗留脚本不会让同步失败）；`--check` 口径不变
  （`__pycache__`、`.pyc` 不在分发集合内）。
- `references/runtime.md` 补充恢复语义：没有单独 `--resume` 开关（续跑即用同一
  `--state`）；恢复后重新核实工具权限、不继承历史权限声明；仍有效的证据与未消费
  attempt 复用，已消费/漂移/宿主记录变化的重建。
- 版本 `6.11.0`；`SKILL.md`、`README.md`、`DECISIONS.md` 第 54 条同步记录。

### 实测（同一基准：400 文件 / 60 变更 / 10 未跟踪，重复 3 次取最小值）

| 命令 | 升级前 | 升级后 | 变化 |
|---|---|---|---|
| `status --json` | 204ms | 96ms | −53% |
| `closure-check --json` | 211ms | 98ms | −54% |
| `review-metrics --json` | 215ms | 94ms | −56% |
| `handoff --json` | 227ms | 103ms | −55% |
| `review-admission --workspace` | 275ms | 159ms | −42% |
| `status`（已接入质量报告） | 396ms | 158ms | −60% |
| `closure-check`（同上） | 323ms | 158ms | −51% |
| `quality`（同上） | 301ms | 166ms | −45% |
| `drive`（同上，到 PASS/CLOSED） | 311ms | 166ms | −47% |
| `report --verify-report`（含采集子进程） | 540ms | 282ms | −48% |

（口径：同一基准目录、每条命令重复 3 次取最小值，升级前/后同一机器、无并发负载。
上表为**最终代码**的复测值；保留 `git submodule status` 无条件调用与机器波动使
部分项比中途版本高约 10–40ms。）

- 测试（最终代码）：活动 **799 条 OK，63.0s**（升级前 786 条 78.5s）；legacy
  **853 条 OK（skip 32），98.3s**（升级前 93.7s）；新增
  `tests/test_efficiency_v611.py` 13 条，覆盖缓存复用、缓存键、`git_root` 缓存、
  单次 `closure-check` 只扫描一次、跨命令篡改检测、gitlink 畸形仓库 fail-closed
  与同步预编译。
- 运行时实测（`~/.zcode/skills/copilot/scripts/copilot_dispatch.py status`，
  同一任务、同一机器、多次取最小值）：有预编译字节码 **115–144ms**；移走
  `scripts/__pycache__` 后 **250–262ms**；恢复后与 warm 一致（波动来自机器负载）。
- T4 隔离验收（最终代码）：T0 28 条 OK / T1 356 条 OK / T2 799 条 OK / T3 853 条
  OK（skip 32），34/34 场景通过，`all_layers_ok` 与 `all_scenarios_ok` 均为 true
  （`/tmp/copilot-bench/t4-freeze.json`，临时文件不进仓库）。
- 新会话加载（已实测）：全新 headless ZCode 会话
  （`node <zcode-cli-entry> -p …`，空目录、只读提问）加载运行时 Skill，
  正确回答版本 `v6.11.0`、`capture_code_snapshot` 单次进程快照复用规则，以及
  `risk=low/medium` 默认不派 Reviewer、`risk=high` 恒走 G4。
- 运行时一致：`--check` 缺失 0 / 不同 0（28 个受管理文件），8 个未受管理历史文件保留；
  `--baseline` 刷新为 28 个文件。关键文件源码与运行时 SHA-256 相同
  （`SKILL.md` `6bf54b32…`、`governance.py` `56ce0127…`、`code_scope.py` `0700e33d…`、
  `copilot_auto.py` `f2b5eaf2…`、`copilot_dispatch.py` `32244f39…`、
  `references/runtime.md` `09a763a7…`、`references/workflow.md` `817f7df2…`）。
- **未验证**：
  - 真实 GUI 会话内加载：未启动新 GUI 会话（当前会话启动期缓存的是旧版本）；
    headless 新会话已实测。
  - 真实高风险 G4 全链路耗时（需要宿主只读子代理会话）。
  - 数万文件大仓库上的快照耗时：只按 400 文件基准外推，未实测。

### 未改变

G1–G4 判定、fail-closed、证据/代码范围绑定、漂移与篡改检测、PWW 接受规则、状态字段、
运行时分发集合均未变化；没有新增依赖、MCP、Provider 或远程模型调用。

## 复核频率与工具执行判定（2026-11-16，v6.10.0）

基线：HEAD `92a1105`（v6.9.0，工作区 clean）。**本轮未提交、未推送。**

### 现场证据

- **宿主拒绝 Bash 的真实形态**：`~/.zcode/cli/db/db.sqlite` 中 `task_type=subagent_child`
  的 `Bash status=error` 有 4 条 `error_type=permission_denied`：
  `duration_ms=0`、`output_bytes=0`、`exit_code=NULL`，错误消息
  `Plan mode only allows read-only, non-destructive tools`；对应 `part` 的
  `state.status=error`、`metadata={}`、时间起止相同、无 output。这是可证明“拒绝且未执行”
  的记录，不是 Bash 实际运行失败。
- **旧判定把请求当执行**：v6.9.0 的 `g4_attest` 用全部 `tool_usage.tool_name` 做
  `host_tool_not_allowed`，并对 `status=error, read_only=0` 触发
  `host_tool_not_readonly`；`native_agent._db_review_tool_calls` 也在生成信封时直接以
  `review_tool_not_allowed` 拒绝。结果是：Reviewer 一次被宿主拒绝的 Bash 请求，会让整份
  复核在新链取证/信封阶段失效，尽管该会话随后仍用 Read/Grep/Glob 完成了复核。

### 已实施

- `review_policy.REVIEW_TIERS` / `tier_for()`：`low|medium|high` 三级；历史 `normal`
  归一化为 `medium`。`admission()` 返回 `tier`；低/中风险无触发因素时
  `not_required`，显式触发因素优先，`high` 恒 `required/g4`。
- `governance.RISKS` 接受 `low|medium|high|normal`，`start_state` 把 `normal` 存为
  `medium`；`_gate_review` 把 `low/medium/normal` 视为非高风险，G4 只对 `high` 生效。
  `copilot_dispatch start --risk` 默认 `medium`；`review-admission` 输出 `tier`；
  admission 记录与 metrics 增加 `tier` / `review_admission_tiers`。
- `host_records.tool_execution_state()`：统一分类 `completed` / `denied_not_executed` /
  `executed_failed` / `execution_unknown`；只有策略/权限/工具缺失错误码、诊断列
  （`duration_ms`/`output_bytes`）存在且显式为 0、无退出码才认定未执行；诊断列缺失或
  为 NULL 按 `execution_unknown` fail-closed。`table_columns()` 兼容缺少可选诊断列的
  旧宿主夹具。
- `g4_attest`：白名单 / 只读 / 副作用检查只作用于实际执行或状态未知的调用；被拒绝调用
  写入 `denied_tool_calls` / `denied_tool_counts` / `repeated_denied_tools` 并进入
  `residual`；同一工具拒绝 ≥3 次记录重复拒绝告警。实际执行 Bash/Write/Edit 仍失败；
  `running`/`cancelled`/证据缺失仍 fail-closed。
- `native_agent._db_review_tool_calls`：拒绝记录计入 `totalToolUseCount` 但不进入
  `calls`、不触发 `review_tool_not_allowed`；实际执行或状态未知的非只读工具仍拒绝。
  `_host_tool_summary` / `capability`：`denied_calls` / `denied_tools` /
  `repeated_denied_tools` 单独统计，被拒绝调用不计入 `write_capable_calls`。
- 文档：`SKILL.md`、`references/review.md`、`README.md` 增加三级策略与“工具请求 ≠ 执行”
  契约；版本 `6.10.0`。

### 验证

- 定向：`tests.test_g4_attest` 180（含新增 9 个拒绝/执行状态用例）、
  `tests.test_agent_policy` 45、`tests.test_native_agent_v6` 19、
  `tests.test_review_efficiency` 49、`tests.test_host_records` 12（含 7 个分类矩阵），
  合计 305 OK。
- 回归：活动全量 `discover -s tests` 786 OK；`legacy/tests` 853 OK（skip 32）；
  `tests/t4_acceptance.py` 全部场景通过。
- **未验证**：尚未用真实 ZCode 会话重跑“Reviewer 被 plan 模式拒绝 Bash 后完成复核”的
  端到端链路；当前证据来自真实宿主 DB 的历史行与合成夹具。

### 独立只读复核（2026-11-16）

- 执行者：原生子代理 `agent_73135720-487e-42cc-8f53-f7199ffe84e4`，类型
  `independent-readonly`（`Read/Grep/Glob`，无 Bash/写入）。复核对象是本轮未提交的工作区
  文件与关键函数；结论 **pass**，无 blocking/major。
- 已处理 findings：
  - minor（fail-closed 缺口）：`tool_execution_state` 原先在缺少 `duration_ms`/
    `output_bytes` 列时仍可能判 denied；已改为诊断列缺失即 `execution_unknown`。
  - minor（文档不一致）：`references/workflow.md` 的 `--risk normal|high` 改为
    `low|medium|high`；`references/agent-bridge.md` 改为“实际执行调用只读，denied 请求
    允许并计入请求数”；新增 `tools/tool_counts` 为请求计数的审计口径说明。
  - minor（语义不一致）：`tier_for` 对空/缺失风险改为失败关闭；`review_admission` 的
    `not_needed` 改为只要非高风险且停止标记覆盖当前快照就返回。
  - minor（测试空洞/覆盖）：新增 `tests/test_host_records.py` 分类矩阵（含诊断列缺失）、
    denied 行与 part completed 不一致被拒、`not_needed` 顶层决策断言；保留
    `attest()` 不消费 attempt 的说明（消费只发生在 ingest）。
  - info（阈值重复）：统一使用 `host_records.REPEATED_DENIAL_THRESHOLD`。
- **未验证**：复核方未运行测试（只读约束），测试结果由主代理执行；真实 ZCode E2E 仍未跑。

### 运行时同步

- `scripts-sync/sync_runtime.py --apply` 分发 28 个文件，写入 8 个变更（SKILL.md、
  references/review.md、copilot_dispatch.py、g4_attest.py、governance.py、
  host_records.py、native_agent.py、review_policy.py）；`--check` 缺失 0、不同 0。
- 运行时保留 8 个未受管理历史文件（legacy 脚本与旧测试），默认不清理。
- `manifests/source-baseline.sha256` 已按当前源码刷新（28 个文件，8 个哈希变化）。

## Agent 调度与原生子代理机制（2026-11-16，v6.9.0）

针对“Copilot 与 ZCode 原生 Agent 协作效率”的专项优化。基线核实：HEAD `4f0fb33`
（v6.8.0，已推送，工作区 clean），不是任务书所写的 `203fe97`/v6.6.1；ZCode 3.18.2，
运行时 Skill 同步前 27 个文件。**本轮未提交、未推送**。

### 根因（现场证据）

- **`profile_tools_not_exact` 不是名称匹配 bug。** 真实 `~/.zcode/cli/agents/*/metadata.json`
  统计 486 个原生子代理会话：旧 user `reviewer` 含 `Read/Grep/Glob/Bash` 153 次、内置
  `Explore` 153 次（`Bash,Glob,Grep,Read,WebFetch,WebSearch,TodoWrite`）、
  `general-purpose` 109 次（`tools=["*"]`）、project `reviewer` 精确只读 43 次、
  `independent-readonly` 21 次、`readonly-probe` 7 次。当前 G4 契约要求精确
  `Read/Grep/Glob` 且禁用 `Write/Edit/Bash`，前两类被拒是正确行为。
- **宿主 Profile 发现**：`rCi` 在会话 bootstrap 扫描 `~/.zcode/agents`（user）与
  `<会话工作目录>/.zcode/agents`（project，additionalDirectories 带命名空间），后加载
  覆盖前者；`agents-state.json.disabledAgentIds` 可禁用 user Profile。**加载结果在会话内
  缓存，文件新建/改名/删除不热更新**。旧 user `reviewer`（含 Bash）与 project 精确
  `reviewer` 在 2026-11-15 同日并存，证明存在“旧会话沿用启动期缓存”的窗口。
- **未知类型不静默回退**：宿主源码 `Njn`/`JKa` 对不存在的 `subagent_type` 抛可恢复的
  `UNKNOWN_AGENT_TYPE` 并列出可用类型；只有省略 `subagent_type` 才等于
  `general-purpose`。Desktop 的 `subagentsReadRuntimeConfig` 返回 `built-in-fallback`
  时只有内置 Agent，不扫描工作区目录。
- **内置能力**：`Explore` 的 `injectAgentsMd=false`，不会自动获得 AGENTS.md；`Bash` 在
  子代理 `tool_usage` 中，**completed** 调用为 `read_only=0,
  side_effect_scope=system`（失败调用可能为 `NULL`，`g4_attest` 对 `status=error`
  允许无副作用分类）。`general-purpose` 为 `tools=["*"]` 并继承 MCP（真实子会话可见
  `mcp__huashu-chrome__*`）。`Agent` 参数只有
  `description/prompt/subagent_type/run_in_background`，没有按派发覆盖工具的入口；
  也没有 OS 级只读沙箱。

### 已实施

- `scripts/agent_policy.py`（新增，纯函数）：内置能力基线、`classify_profile`、
  `capability_report`、`dispatch_advice`。不派发、不调用模型、不写状态。
- `native_agent.py capability --metadata ...`：只读诊断子代理实际权限与 G4 资格
  （`g4.executor`/`codes`/`remedy`）；`build_review` 的只读策略拒绝信息补充内置 Explore /
  general-purpose 的具体原因，错误码不变（仍为 `profile_tools_not_exact` 等）。
- `copilot_dispatch.py agent-policy`：五问调度建议（主代理默认 / Explore 调查 /
  general-purpose 独立交付），只读建议，不是门禁或调度器；版本 `6.9.0`。
- `SKILL.md`/`references/agent-bridge.md`/`review.md`/`runtime.md`/`workflow.md`/
  `AGENTS.md`/`README.md`：落地主代理默认、Explore/general-purpose 调用条件、Profile
  发现与根因、G4 fail-closed 与能力缺口报告、子代理失败降级；新增
  `docs/DECISIONS.md` 第 52 条。
- 未删除工作区 `independent-readonly.md`：它是通用只读能力载体（非 Reviewer 角色），
  不作为普通任务前置；内置 Explore/general-purpose 仍不能替代 G4。未新增任何自定义子代理、
  未修改 Provider/路由/账户、未新增 MCP。

### 效果基线（历史真实记录，优化前）

从 486 个子代理 metadata 与宿主 DB 汇总（时长为各会话 `totalDurationMs` 之和；工具数为
`tool_usage` 行数；Bash 为子代理实际发起的 Bash 调用；MCP 为 `mcp__*` 调用）：

| Profile / 策略 | 会话 | 总耗时 | 工具调用 | Bash | MCP |
|---|---:|---:|---:|---:|---:|
| `reviewer`（旧 user，含 Bash） | 153 | 51,372 s | 11,497 | 7,943 | 0 |
| `reviewer`（project，精确只读） | 43 | 5,709 s | 1,398 | 1（error） | 0 |
| 内置 `Explore` | 153 | 53,188 s | 8,414 | 6,780 | 0 |
| 内置 `general-purpose` | 109 | 24,901 s | 3,234 | 2,692 | 58 |
| `independent-readonly` | 21 | 6,516 s | 1,289 | 1（error） | 0 |
| `readonly-probe` | 7 | 161 s | 55 | 0 | 0 |

这些数字说明优化前的主要成本来自“用带 Bash 的 Profile/内置 Explore 做复核（被 G4 拒绝后
重做）”与 general-purpose 的 Bash/MCP 调用；优化后真实任务的对比尚未测量（见未验证项），
不能把上述历史值当作优化后结果。

### 工程验证

- 活动测试（**修复前**基线，对应本轮初始实现）：`743` 通过（含
  `tests/test_agent_policy.py` 29 项）；修复后该文件扩为 42 项，全量复跑不在本轮最小验证
  范围内，最终代码的定向证据见下节。
- 历史回归：`853` 通过（跳过 32）。
- T4 隔离验收：T0 0.8s / T1 20.3s / T2 68.3s / T3 89.1s，`all_layers_ok=true`，
  34/34 场景通过（`all_scenarios_ok=true`）；报告 `/tmp/copilot-t4-v690-final.json`
  （临时文件，不进入仓库）。
- 运行时分发：28 个文件，`--check` 缺失 0、不同 0；运行时 `copilot_dispatch.py --version`
  为 `6.9.0`，`agent-policy` 可执行。
- **未验证/限制**：本轮未发起新的真实子代理派发（项目规则要求真实模型调用单独授权），
  因此“优化后”真实任务的子代理次数/耗时对比尚未测量；效果基线来自历史 metadata/DB。
  G4 在只有内置 Agent 的环境下仍会 fail-closed；实际高风险的完整 G4 端到端需在具备宿主
  只读能力载体的环境单独验证。Desktop `subagentsReadRuntimeConfig` 的 built-in-fallback
  分支来自宿主源码，本会话未触发。

### 独立只读复核与修复（2026-11-16，v6.9.0 收尾）

- **复核执行者**：原生子代理 `agent_172d02a9-c1ca-4f71-9ba5-3cccdbb9d16e`，profile
  `independent-readonly`（`source=project`）；宿主 `profileSnapshot.tools=[Read,Grep,Glob]`、
  `disallowedTools` 含 `Write/Edit/Bash`，`injectAgentsMd=false`。宿主 `tool_usage` 记录
  165 次调用（Read 62 / Grep 93 / Glob 10），全部 `read_only=1`、0 次写能力调用；
  `native_agent.py capability` 输出 `g4.executor=true`、`codes=[g4_ready]`、无告警。
  复核结论：3 Major + 7 Minor + 2 Info。
- **Major 修复**：
  1. 写能力判定由“硬编码写工具清单”改为“已知只读工具集反向判定，未知工具 fail-closed”，
     并补入宿主真实写工具 `ApplyPatch` 与执行类 `js*`；逐调用证据同时使用 `read_only`。
  2. `agent-policy` 改为先否决后选择：`research_only` 只建议 `Explore`（不再被
     `independent-deliverable` 抢占为全工具代理），`write_conflict` 一律建议主代理。
  3. `host_evidence` 先查宿主 `session` 表：会话不存在返回 `session_not_found`，
     只有“会话存在但 0 次调用”才报 `total_calls=0`。
- **Minor/Info 处理**：`capability` 的 `g4.executor` 明确为策略预检（`precheck_only`）并
  区分 `profile_tools_missing`/`profile_snapshot_missing`；新增 `permission_mode_whitespace`
  告警；测试补宿主 SQLite 夹具（写调用、未分类调用、会话缺失、`ApplyPatch`/`js`/未知工具）；
  `agent_policy.py` 纳入架构约束与分发断言；`source-baseline.sha256` 重建；
  `PROJECT-CHARTER` 基线改 v5.5.1 并澄清 Reviewer 只指 G4 执行方；只读 Profile 措辞改为
  “普通任务不强制、G4 不得删除”；`Bash read_only=0` 限定为 completed 调用；ROADMAP 阶段
  序号归位。
- **定向验证（最终代码）**：`tests/test_agent_policy.py` 42 项 OK、`test_native_agent_v6`
  16 项 OK、`test_profile_policy` + `test_g4_attest` 177 项 OK、`test_architecture` +
  `test_sync_runtime` 29 项 OK、`test_docs_v6` 16 项 OK；`git diff --check` 通过；
  运行时重新同步后 `--check` 缺失 0、不同 0（28 个受管理文件）。

## 独立复核效率专项（2026-11-16，v6.8.0）

针对独立复核耗时过长、重复启动、修复后无条件完整复核、多入口重复触发与缺少停止条件
的专项优化。本轮在 v6.7.1 工作区基线上继续，**不降低 G1–G4 与 G4 fail-closed 标准**。

- **真实根因**：`derive_work` 对同一缺口没有 active 去重；完整复核没有“PASS 停止标记”，
  同一目标可重复创建 attempt；复核 findings 只有单轮结构校验，没有跨轮稳定台账，
  已关闭问题可换表述重报；修复后默认重跑完整复核，缺少定向验证入口；`review-metrics`
  只有轮次/重复文本，没有准入、停止、定向复验与耗时口径。
- **风险准入**：新增 `copilot_dispatch.py review-admission`（只读）。`risk=high` 恒需 G4；
  普通风险默认 `not_required`，声明 `security`/`permissions`/`core-logic`/
  `data-integrity`/`architecture`/`global-execution`/`production`/`release` 才建议派发；
  纯文档/格式/注释调整不派 Reviewer。
- **停止与重入**：PASS 回填写入 `review_stops`；同一目标/代码范围再次 attempt 返回
  `review_not_needed`，只有文档/格式变化返回 `review_not_needed_docs_only`；代码/目标/
  证据变化必须用 `review-admission --trigger ... --reason ...`（或
  `review-context --trigger/--reason`）声明实质原因，记录到 `review_admissions` 并绑定
  attempt。增量复核仍要求合法基线、代码范围未变、变化证据全覆盖。
- **Finding 台账**：复核回填时合并到 `finding_ledger`，稳定 ID `F1..Fn`；按显式 id、
  归一化 `root_cause`、同文件摘要相似度识别同一问题；生命周期
  `open → fixed → verified → closed`，`fixed`/`verified` 必须有证据，`closed` 只能从
  `verified` 进入；已验证/已关闭问题无新证据只记 `rereported`，有新证据才 `regressed`。
  新增 `copilot_dispatch.py finding --list/--id/--status/--evidence/--note`。
- **定向复验**：未闭合 blocking/major 派生 `AUTO-FIX-<F>`；`fixed` 未验证派生
  `AUTO-VERIFY-<F>`，只跑对应定向测试/检查并登记证据，不重新派发完整 Reviewer。
  未闭合 finding 清零前不派生 `AUTO-G4`。普通风险 G2 增加台账门禁，高风险由 G4 统一检查。
- **幂等与恢复**：同一工作项/执行结果/目标/代码范围/scope/证据清单的未消费 attempt 直接
  复用；`derive_work` 对同一缺口已有 `pending`/`running` 工作项时不再派生第二条；派发
  失败或上下文中断时未消费 attempt 可继续使用，已消费 attempt 不能重放。
- **度量**：`review-metrics` 新增 `full_reviews`、`incremental_reviews`、
  `review_admissions_total`/`review_admission_triggers`、`review_stops_total`、
  `finding_ledger`、`rereported_findings`、`incremental_new_blocking/major`、
  `no_information_reviews`、`avg_review_seconds`、`avg_first_full_review_seconds`、
  `total_review_seconds`；正式 REVIEW、原生复核与 G4 仍分别计数。
- **测试**：新增 `tests/test_review_efficiency.py` 41 项，覆盖准入、停止、重入、稳定
  ID、生命周期、severity 提级、目标变化保留 stop、定向验证、attempt 复用、ingest 台账
  幂等、CLI `--workspace/--record`、文件证据重读与指标；`test_governance` 增加“干净 PASS
  不能掩盖台账未闭合 major”用例。
- **独立复核（首轮）**：发现 2 阻断 + 4 实质问题，已全部修复并补测：`dropped` 不能由
  执行方清除 blocking/major；非正式 G4 复核只能新增/重复报告，不能把已有 finding 改成
  `verified`；准入与重入 trigger 双向兼容；停止标记只在对应 PASS 仍有效时生效，
  `--trigger` 可在同快照强制重入；高风险 G4 的文档变化仍需重新 G4，`.csv` 不再算文档；
  `review-admission` 增加 `--workspace/--base/--record`；finding 证据重读文件并校验摘要；
  ID 冲突、相似度比较、AUTO-FIX 重复派生与 candidate 异常半成品均已收敛。
- **未验证/限制**：文档级变化按扩展名识别，不能证明改动语义；定向测试证据由 ZCode
  负责真实性；真实高风险的完整 G4 + 重入链路仍需在最终 E2E 中复核。
- **工程验证**：活动测试 712 通过；历史回归 853（跳过 32）；T4 四层 + 34/34 场景通过；
  运行时分发 27 个文件，`--check` 缺失 0、不同 0；全新 ZCode headless 会话加载
  `copilot v6.8.0` 并实际执行 `start` / `review-admission --workspace --record` /
  `review-metrics`。工作区中已完成的 v6.7.1 基线与本轮改同一批模块，随 v6.8.0 一并提交。

## 质量报告替代与代码范围绑定（2026-11-16，v6.7.1）

专项评估确认 v6.7.0 存在两处实质缺陷：新 PASS 可无条件替代旧 PWW；质量报告未绑定实际
代码范围。本轮在现有结构上修复，**未提交、未推送、未发布**（后续随 v6.8.0 一并提交，
见上文工程验证）。

- **显式替代**：报告新增 `supersedes_report_id`、`resolved_warning_ids`、
  `resolved_residual_risk_ids`；存在生效报告时缺少/指向错误目标分别报
  `pww_supersede_missing` / `pww_supersede_invalid_target`；未知字段直接拒绝。
- **覆盖与继承**：旧未解决 W/R 必须在新报告继续列出（PWW 重新接受），或在
  `subject_digest` 与警告来源一致时声明解决；部分覆盖、subject 不一致、PASS 携带未解决
  项分别报 `pww_supersede_coverage_incomplete` / `pww_supersede_subject_mismatch` /
  `pww_superseded_unresolved`。未解决项沿替代链持久化，FAIL/PARTIAL 中转不能洗掉；
  旧接受记录保留但不继承；已由用户接受的 W/R 视为已解决，不要求新报告继承。
- **代码范围**：新增 `scripts/code_scope_capture.py`（只读）；代码类报告必须把
  `commit`、`code_scope_sha256` 与 `files_paths`（字符串数组，重命名含新旧路径）写入
  `subject.files`；`dirty` 可选，给出时必须一致。接入时用 `--workspace` 重算并绑定，
  G2 收口前再重算。HEAD、tracked/untracked/新增/删除/重命名、dirty 标记、声明范围不一致
  均拒绝（`quality_code_scope_*`）。已登记证据文件不从快照排除：证据位于工作区内且发生
  变更时同样必须声明，堵住 `evidence --add --file` 隐藏已修改源文件的路径。纯文档报告
  不要求快照。
- **P1 生成时机**：`subject.code_scope_sha256` 是 verify 报告生成时声明的摘要，已进入
  `report_sha256`；Copilot 在 ingest 时重算当前工作区，生成后/接入前代码变化会因摘要
  不一致被拒绝，G2 再重算检测后续漂移。`code_scope` 明确标注
  `capture_source=copilot-ingest-recompute` 与 `captured_at`，并绑定 `report_id`、
  `report_sha256`、`task_id`；它只证明接入/收口一致，不冒充生成时快照。生成与接入之间
  “改动后又完全还原”无法在无可信宿主事件时区分，属已披露限制。
- **P2 工作区身份**：`start --workspace` 绑定任务工作区；未显式绑定时从 native execution
  binding、合同来源、证据所属 git 仓库、任务目录唯一派生。`report --workspace` 经
  expanduser/abspath/realpath 并归一到 git root，必须与绑定一致；未绑定或指向其他仓库时
  `quality_code_scope_workspace_unbound` / `_mismatch` 失败关闭。v6.7.1 的
  `binding_sha256` 额外绑定 `report_id` 与 `workspace`；纯文档报告不需要工作区。
- **接受条件范围**：只有 PWW 创建用户接受条件；FAIL/PARTIAL/BLOCKED 不创建，未解决项仍
  沿替代链保留。`derive_work` 只在最新报告为 PWW 且绑定当前时派生 `AUTO-ACCEPT`，否则
  派生 `AUTO-QUALITY`。空操作 `contract --amend` 保留原接受记录，不重置条件。
- **G2**：新增替代链/覆盖完整性/代码范围/接受记录一致性检查；重算被替代报告摘要
  （`pww_supersede_target_tampered`）并校验报告结构（`quality_report_state_invalid`）。
  PASS 报告的目标/合同/条件/工作区绑定变化报 `quality_binding_stale`。G4 代码快照只排除
  未跟踪证据产物，已跟踪源文件即使登记为证据也进入快照。G1–G4 标准与高风险 G4
  attempt/binding/code_scope 不变。
- **P3 历史兼容**：v6.7.0 报告没有替代/代码快照字段时按旧载荷重算摘要；缺少
  `generated_at` 时旧载荷不写入空串（当时没有该字段）；绑定摘要同时接受旧口径与
  “含 subject_digest”的过渡口径；旧接受记录缺少 `acceptance_history` 时只在历史报告上
  回退到“接受记录 + 条件 + 证据”一致性校验。2026-11-16 对 `/tmp` 中 **9 个真实
  v6.7.0 状态**做隔离复制后验证：7 个已接受 PWW/PASS 任务 `closure_check` 通过；FAIL
  任务保持 `quality_verdict_not_pass`；失效 PWW 任务保持 stale/reverify；旧绑定匹配；
  旧状态不会被伪造成已接受。另在副本上完成“v6.7.0 已接受 PWW → reopen → v6.7.1 报告
  替代 → 用户接受 → CLOSED/pass_with_warnings”升级链路。原始历史文件未修改，复制件未
  进入仓库。
- **测试**：活动套件 `669 tests OK`（`tests/test_v671_quality.py` 66 项）；历史套件
  `853 tests OK (skipped=32)`。
- **运行时 E2E（隔离临时目录，已清理）**：旧 PWW → 新 PASS 无声明/部分覆盖均 exit 2；
  完整解决 W1/W2/R1 后收口 `pass`；代码报告绑定快照 → 用户接受 → 修改代码 →
  `quality_code_scope_drift` 拒绝收口与再次接受 → 新报告绑定新快照、替代旧报告 → 接受 →
  `CLOSED/pass_with_warnings`；纯文档 PWW 正常收口。
- **修复后 E2E（隔离临时目录，已清理）**：把已修改的 tracked 源文件登记为 evidence 后，
  `code_scope_capture.py` 仍把 `src/app.py` 列入 `files_paths`；声明范围漏掉它时接入 exit 2；
  完整声明后绑定、接受、空操作 `contract --amend`、幂等再接受、收口
  `CLOSED/pass_with_warnings` 全部通过。
- **P3 升级 E2E（隔离副本，已清理）**：复制真实 v6.7.0 已接受 PWW 状态，`reopen` 后接入
  v6.7.1 报告（替代旧报告、继承 W1/W2/R1），用户接受后 `CLOSED/pass_with_warnings`；
  旧接受记录保留在旧报告，未被伪造为已接受。
- **T4 验收**：`tests/t4_acceptance.py` 全层 `all_layers_ok=true`、`all_scenarios_ok=true`，
  34/34 场景（隔离目录）。
- **独立只读复核**：`independent-readonly` 子代理结论 PARTIAL，1 个 MAJOR + 7 个 MINOR/
  INFO：`evidence --add --file` 可把已修改源文件从代码快照排除（MAJOR）；G2 重算沿用了
  冻结的 evidence 排除、空操作 `contract --amend` 重置接受条件、非 PWW 报告创建
  `AUTO-ACCEPT`、被替代报告摘要未重算、`subject.files` 对象/rename 契约、`--workspace`
  未交叉校验、接受绑定措辞等。已修复：质量报告代码快照只排除治理文件与报告文件，任何
  工作区变更都必须声明；G2 重算被替代报告摘要并校验报告结构；空操作 amend 保留接受；
  仅 PWW 创建接受条件；`files_paths` 含新旧路径且拒绝非字符串项；绑定兼容旧/过渡口径。
  复核方只读，未执行测试；本轮测试由执行方重跑。
- **最终独立只读复核**：第二轮 `independent-readonly` 子代理结论 PARTIAL，指出 4 个
  MAJOR：PASS 报告 `binding_sha256` 未在 G2 校验、`derive_work` 在 reopen/代码漂移后仍
  派生不可执行的 `AUTO-ACCEPT`、G4/native review 仍把已登记证据从代码快照排除、
  给 `--workspace` 但 subject 未声明代码字段时只警告并落盘。已修复：PASS 绑定变化报
  `quality_binding_stale`；`AUTO-ACCEPT` 增加 stale/code drift 检查；G4 仅排除未跟踪
  证据产物、已跟踪源文件进入快照；`--workspace` + 文档 subject 直接失败关闭。另修
  `files: []`、短接受语、severity 枚举、subject 类型、reopen 子状态、legacy 过渡绑定
  幂等、报告文件位于工作区内拒绝、`GIT_DIR` 等环境变量清理。复核方只读、未执行测试；
  最终测试由执行方重跑。
- **第三轮独立只读复核**：结论 PARTIAL，1 个 MAJOR：执行信封仍排除全部 evidence，而
  review attempt 已改为只排除未跟踪证据，导致“已跟踪文件作证据”时 G4 两侧代码摘要不一致。
  已修复：执行信封与 attempt 共用 `safe_path_excludes`（只排除未跟踪产物）；新增
  tracked-evidence 执行绑定回归测试。另修 AUTO-QUALITY 对正常 pending/partial 接受状态的
  过度派生、CLI 重复接入仍显示“待接受”、任务目录内报告文件的误拒、falsy warnings 类型
  缺口、锁文件路径、`quality` 查询不重算报告摘要、partial accept 重复追加历史。
  复核方只读、未执行测试；最终测试由执行方重跑。
- **第四轮独立只读复核**：结论 PASS_WITH_WARNINGS，无 blocking/major；2 个 WARNING：
  执行信封与 attempt 的 evidence 集合仍可能不同（执行后才登记为证据的未跟踪产物）、
  `--output` 位于工作区内会造成两侧摘要不一致。已修复：执行信封固化
  `code_scope_excluded_paths`，attempt 直接复用；旧信封回退到“只排除未跟踪证据/不排除
  证据”双候选；未被快照排除的工作区内 `--output` 直接拒绝。另同步旧证据/锁文件文档与
  helper 帮助文本。复核方只读、未执行测试。
- **第五轮独立只读复核**：结论 PARTIAL，4 个 WARNING：`code_scope_problems` 仍动态并集
  全部未跟踪证据（late-registered 产物端到端仍失败）、信封排除集可放入任意未跟踪/
  不存在路径、`--output` 守卫在 task_dir==workspace 时退化为空操作、sanitization 对
  符号链接做 realpath 而执行侧做 abspath。已修复：重算只动态排除“attempt 时不在快照中
  的未跟踪证据”；信封排除集只接受治理路径或已登记且存在的证据；`--output` 仅允许
  工作区外或 `.copilot-tasks/`/`.audit/` 任务目录；两侧统一 abspath；嵌套仓库同时检查
  外层与最近 git root。复核方只读、未执行测试；最终测试由执行方重跑。
- **运行时同步**：分发 26 个文件（新增 `scripts/code_scope_capture.py`），`--check`
  缺失 0、不同 0。
- **headless 加载**：全新 ZCode headless 会话读取运行时 `~/.zcode/skills/copilot/SKILL.md`，
  确认加载 v6.7.1、`files_paths`、质量报告证据不从快照排除、`AUTO-ACCEPT` 仅对 PWW
  当前绑定派生、已接受 W/R 视为已解决；运行时与源码逐字节一致。
- **headless 宿主 E2E**：全新 ZCode headless 会话在隔离目录执行运行时 CLI：另一仓库
  `--workspace` 接入 exit 2；代码漂移后 `closure --close` exit 2 且
  `quality_code_scope_drift`；还原代码、空操作 amend、再次接受后
  `CLOSED/pass_with_warnings`。
- **限制**：用户接受仍是 user-declared，不是身份认证；代码快照证明接入/收口与报告一致，
  不证明生成时刻（无可信时钟），生成与接入间“改动后完全还原”不可区分；旧任务未显式
  绑定工作区时只能在证据/合同/派发上下文唯一时派生，否则代码报告失败关闭；报告来源
  真实性仍由 verify/执行方负责。

## PWW 自动治理与验收闭环（2026-11-15，v6.7.0）

用户要求解决 PWW 依赖执行方手动登记、接受后缺少端到端验证、收口丢失原始质量结论三个问题。
本轮只扩展 Copilot 的报告接入、G2、收口与事件记录，不改变 G1–G4 标准、正式 REVIEW 授权、
状态机或执行后端默认值；**未提交、未推送、未发布**。

- **报告接入**：verify 报告固定输出 `<!-- copilot-verify-result/v1 -->` + JSON
  （`report_id`、`generated_at`、原始 verdict、`warnings`、`residual_risks`、`unverified`、
  `acceptance_required`、`acceptance_status`、四项 `checks`、`subject`）。
  `report --verify-report` 只认该标记与结构（纯 JSON 无标记也拒绝）；缺字段、PWW/PASS 的
  `checks` 不全 true、PWW 带未验证项或 blocking/major、报告自报 accepted、合同完成条件
  未核销、内容摘要不符都失败关闭，不从正文关键词推断。
- **自动登记**：PWW 自动创建**一个**用户接受完成条件（`acceptance` 元数据），绑定
  `report_sha256`、W/R 集合、`subject` 摘要、目标/合同/条件摘要与报告文件；重复接入按内容
  摘要幂等，已替代的旧报告拒绝再次接入；新报告替代旧报告时旧接受不继承，条件重置 pending。
- **G2 强制**：`_gate_quality` 独立于条件列表检查最新生效报告；未接受
  `pww_acceptance_pending`、部分接受 `pww_acceptance_partial`、目标/合同/条件/警告变化
  `pww_acceptance_stale`、重开后 `pww_reverify_required`、最新结论非 PASS
  `quality_verdict_not_pass`、报告内容被改写 `quality_report_tampered`、报告集合/接受
  历史/接受证据与条件不一致 `quality_report_state_inconsistent` 时拒绝收口。合同修订删除
  接受条件也不能绕过。任务 `reopen` 会使旧接受失效，必须重新验收并再次接受。
- **用户边界**：只有 `accept --by user --accept <W/R id> --quote <用户原话>` 能登记接受；
  执行方/子代理/verify/Copilot 与 `decide`、`criterion` 命令都不能替代。只接受部分警告时
  保持 pending；接受事件（范围、原话、时间、绑定）写入 `acceptance_history`。
- **原始结论分离**：收口写 `closure.verdict=pass_with_warnings` 与 `closure.quality`
  （原始 verdict、report_id、接受状态、accepted_ids、时间）；`quality --state` 与
  `closure-check` 均展示。历史任务没有质量报告时显示 `unrecorded`，不伪造接受。
- **测试**：活动套件 `599 tests OK`（新增 `tests/test_pww_acceptance.py` 43 项，覆盖 PASS、
  自动登记、G2 拦截、伪造接受、部分接受、警告/目标变化、幂等、替代关系、PWW/PASS 前置
  条件、FAIL 保留、历史兼容、重开失效、状态篡改、保存失败与 CLI 闭环）；历史套件
  `853 tests OK (skipped=32)`。
- **真实 E2E（运行时脚本，非源码直调）**：`/tmp/copilot-e2e-pww-v2-5DGnlR` 完成
  PWW 接入 → 自动登记 `C2` → `closure --close` exit 2（`G2:criterion_pending;
  G2:pww_acceptance_pending`）→ `accept --by zcode` exit 2 → 用户接受 W1/R1 →
  四门 PASS → `CLOSED/verdict=pass_with_warnings`；`reopen` 后旧接受失效
  （`pww_reverify_required`，再接受 exit 2）→ 新报告 `VR-E2E-2` + 用户再次接受 →
  再次 `CLOSED/pass_with_warnings`。另验证普通 PASS 直接收口 `verdict=pass`
  （`/tmp/copilot-e2e-pass-wXe1Hh`）、新报告替代旧接受与合同变化 stale
  （`/tmp/copilot-e2e-invalid-cyAQnm`）、PASS 但 `checks` 含 false 被拒（exit 2）。
- **T4 验收**：`tests/t4_acceptance.py` 全层 `all_layers_ok=true`、`all_scenarios_ok=true`
  （T0/T1/T2/T3），34/34 场景。
- **独立只读复核**：`independent-readonly` 子代理首轮 PARTIAL（2 major + 6 minor），指出
  PASS 报告未校验 `checks`、纯 JSON 无标记可接入、`accept` stale 分支先落盘、缺失字段会
  抛 `KeyError`、状态篡改与重开接受等缺口。已修复并补测：PASS/PWW 均要求四项 checks 全
  true；只认 marker + JSON；`generated_at` 必填；stale 不落盘；防御性解析；接受记录/
  历史/证据/条件一致性检查；报告集合删除与多 active 报告 fail-closed；`reopen` 使旧接受
  失效。复核提出的“状态文件直改可伪造任一记录”属本地可信边界，已在 DECISIONS 49 披露。
- **规则同步**：`~/.zcode/AGENTS.md` 第 85 行 PWW 段落原写“用现有 `decide`/`report` 记录
  接受、必要时 `contract --amend` 登记条件”，与自动接入机制冲突；已最小改为
  `report --verify-report` 自动登记 + `accept --by user --quote`，并注明
  `decide`/`criterion` 不能替代、CLI 无法证明用户来源。备份
  `AGENTS.md.bak-20261115-pre-pww-automation`（`45ec4a34…`），新文件 88 行 /
  21,289 字节 / `f99a83c9…`。
- **运行时同步**：`sync_runtime.py --apply` 写入 6 个文件，`--check` 缺失 0、不同 0
  （25 个受管理文件，8 个未受管理文件保留）。
- **新会话加载**：headless 新进程读取运行时 Skill，确认 `copilot` v6.7.0、verify 第 10 节
  marker/`checks`/`generated_at` 规则、`accept --by user --quote` 与 reopen 失效规则均
  实际加载。
- **限制（未验证/残余）**：CLI 无法从进程层证明接受来自用户，`accept` 记录为
  `origin=user-declared`（要求用户原话与完整 W/R，执行方仍可能伪造；属于授权越界，
  不是可机械阻止的能力）；高风险任务接受会改变条件状态与最终证据摘要，必须在最终 G4
  之前完成接受；`subject` 只参与报告摘要与绑定，不核对报告声明的 commit/files 是否
  等于当前工作区；新 PASS 报告按“最新结论替代旧结论”处理，可替代未接受的 PWW，报告
  来源真实性由 verify/执行方负责；真实生产高风险任务未跑本链路，E2E 仅使用 `/tmp`
  临时目录与本地任务状态。

## 通用只读 Profile 与旧 Reviewer 文件退役（2026-11-15，v6.6.1）

用户确认接受最小化的通用只读 Profile。本轮只做文件通用化与引用清理，不改 G4 设计：

- **迁移**：删除 `.zcode/agents/reviewer.md`，新建 `.zcode/agents/independent-readonly.md`；
  frontmatter 权限不变（`tools: [Read, Grep, Glob]`、`disallowedTools` 含
  `Write/Edit/Bash/WebFetch/WebSearch/Agent/Task/TodoWrite/NotebookEdit`、
  `permissionMode: default`、`injectAgentsMd: false`）。正文改为通用只读约束，删除
  Reviewer 身份、复核流程、`review-json/v2` 输出契约与生命周期；输出格式由主 Agent
  在派发 prompt 中给出。
- **代码**：G4 已按宿主有效策略判定（v6.6），本轮无脚本逻辑改动，只更新版本号与文档/
  测试引用；`tests/test_profile_policy.py` 改为锁定“旧文件不存在 + 新文件唯一 + 通用正文”。
- **场景 A（普通只读调查）**：headless 新进程在仓库内派发 `independent-readonly` 调查
  `native_bridge`/`g4_attest` 的 Profile 策略实现。子会话 `agent_b59e25ab…`
  （parent `sess_bc74a8e8…`）profile `independent-readonly`、`workspaceRoot` 为仓库、
  tools `[Read,Grep,Glob]`、disallowed 含 Bash/Write/Edit；实际 2×Read、
  `read_only=1`、`side_effect_scope=none`、0 次 Bash；回报的函数名/行号与实际源码一致。
- **场景 B（正式 G4）**：`/tmp/copilot-e2e61-yzRiSL/task`，task `task-e2e-v661`；
  Executor `agent_ab904028…`（general-purpose，Read+Edit+`bash run-tests.sh` exit 0）、
  binding `EB24184a…`、execution `5e8e12d0…`；attempt `RA77095998…`；Reviewer
  `agent_ec4fd642…`（profile `independent-readonly`，9×Read+1×Glob，0 Bash）；
  raw 384 字节 SHA `15048f80…`；attestation `57980f16…`（69/69 checks，0 rejections，
  `profile_snapshot.name=independent-readonly`、tools 精确只读）；TE 四层全 true；
  候选 `eligible=true`；正式 `g4_eligible=True`/`g4_source=candidate`；attempt 一次性
  消费；`drive` 直接 `CLOSED/pass`。信封与 context 写在任务工作区之外，代码快照只有
  `src/calc.py`。
- **独立只读复核**：全新 headless 进程用 `independent-readonly` 复核 31 个文件的 diff，
  verdict `partial`，5 条 finding 全为 minor/info；已修复 F1（`review_output` 注释仍写
  Profile 要求纯 JSON）、F2（执行方策略缺失/空值的判定口径注释与文档）、F3（
  `manifests/source-baseline.sha256` 未更新）、F4（增加 `.zcode/agents` 仅允许单个通用
  Profile 的测试并纳入 T4 场景）、F5（`agent-bridge` 补充派发 prompt 的原始 JSON 模板）。
- **测试**：活动 `556 tests OK`；历史 `853 OK (skipped=31)`；T4 34/34 场景，
  T0 0.6s / T1 19.7s / T2 41.2s / T3 92.4s。`manifests` 基线 25 文件已重建。
- **运行时**：`--apply` 写入 6 个受管理文件，`--check` 缺失 0 / 不同 0；8 个未受管理
  文件保留；全新 headless 会话加载 Skill `v6.6.1`，并在仓库工作区识别
  `independent-readonly`（见场景 A）。
- **未完成/限制**：宿主仍不支持派发期工具白名单，因此必须保留一个只读 Profile 文件；
  该文件现在不承担任何 Reviewer 角色。metadata/SQLite 无签名；未做主动 Bash 渗透测试。

## 复核方去专用化：有效权限取代 Profile 身份（2026-11-15，v6.6.0）

目标是把“独立复核”与“Copilot 专用 Reviewer Profile”解耦。调查结论：ZCode
**3.18.2 不支持按派发指定工具白名单**，宿主只能在 Agent Profile frontmatter 上强制
`tools`/`disallowedTools`；内置 `general-purpose` 为 `tools:["*"]`、`Explore` 含
`Bash`，官方插件 Agent 也含 `Bash`。因此“没有 Profile 文件也能证明只读”在当前版本
不成立，专用只读 Profile 仍必须保留（见“未完成/限制”）。本轮完成的是 G4 判定口径与
调用链的去标识化：

- **P0 调查（静态证据）**：`<zcode-cli-entry>` 中 Agent 工具入参只有
  `description`/`prompt`/`subagent_type`/`run_in_background`；profile frontmatter 才支持
  `tools`/`disallowedTools`；`tools: []` 等于继承父工具。
- **P0 调查（真实宿主探针）**：`/tmp/copilot-probe-UPrxnK/ws` 用**非 `reviewer` 名称**的
  工作区 Profile（`independent-readonly`）派发真实子代理：宿主快照 `tools=[Read,Grep,Glob]`、
  `disallowedTools` 含 Bash/Write/Edit、`source=project`；子代理 `Bash` 不可用，实际
  只有 1 次 `Read`（`read_only=1`、`side_effect_scope=none`）；child `workspaceRoot`
  等于 `--cwd`，DB `session.task_type=subagent_child` 与 `tool_usage`/`part` 齐全。
- **判定口径**：`native_bridge`/`native_agent`/`governance`/`test_execution`/`g4_attest`
  改为校验**宿主 `profileSnapshot` 派生的有效权限策略**（`tools` 精确等于
  `Read/Grep/Glob`、禁用 `Write/Edit/Bash`、无 skills/MCP、`permissionMode` 缺省/空或
  default/plan）；Profile 名称、来源、路径只作记录事实。缺失/类型错误的策略失败关闭
  （含信封层 `reviewer.profile`/`executor.profile` 必填）；执行方等价只读策略 →
  `executor_profile_readonly`。
- **移除**：`g4_attest` 的 `allowed_profile_sources`（含 `project` 强制）与
  `require_workspace_profile`、`profile_path_outside_workspace`/`_user_dir`、
  `profile_permission_mode_missing` 及 CLI `--allowed-profile-source`/
  `--allow-user-profile`；`g4_shadow` 不再传递来源白名单；复核信封新增
  `reviewer.profile`（native/agent/session 真实性不变）。
- **保留**：Profile 文件存在且正文与 `profileSnapshot.systemPrompt` 一致、Session 独立性、
  会话晚于 attempt、prompt 携带 attempt id、工作区绑定、工具调用白名单、原始输出 SHA、
  attempt 一次性消费、TE 四层、fail-closed 全部不变。
- **真实 E2E（最终运行时）**：`/tmp/copilot-e2e-DMIFkP/task`，task `task-e2e-v66`；
  Executor `agent_97a16c06…`（`general-purpose`，Read+Edit+`bash run-tests.sh` exit 0）、
  binding `EBb7d938…`、execution `23b76673…`；attempt `RA67c08d…`；
  Reviewer `agent_9b32f76d…`（profile **`independent-readonly`**，9×Read+1×Glob，
  无 Bash）；raw SHA `f6ca91a6…`；attestation `74b4c230…`（69/69 checks，0 rejections）；
  TE `verified`（四层全 true）、候选 `eligible=true`、正式 `PASS`、
  `drive` 自动 `CLOSED/pass`（`2026-11-15T19:27:09+08:00`）。`verify_stored` 正例 ok；
  改写 raw verdict 后 `ok=false`。
- **E2E 暴露的流程约束**：当任务状态目录就是代码工作区时，把执行信封写在
  `--output A1.execution.json` 会让 attempt 快照多出该文件（执行信封自身被排除），
  TE 报 `execution_code_scope_mismatch`；信封应写到工作区之外。本轮首次 attempt
  `RAc63a3b…` 因此未获正式 G4（未消费），修正后重做 attempt 得到上述 PASS。
- **独立只读复核（原生子代理 `agent_c122fd25…`）**：对 24 文件未提交改动做只读复核，
  verdict `partial`，5 条 finding 全为 minor/info（F1 信封层把非列表 skills/MCP 归一化
  成空、F2 文档 permissionMode 口径、F3 envelope 示例缺 `executor.profile`、F4 项目
  `AGENTS.md` 仍写 Reviewer 含 Bash/固定工作区 Profile、F5 执行方只读判定依赖可选字段）。
  已修复 F1（非空 skills/MCP 一律拒绝，含类型错误）、F5（`executor.profile` 必填，
  `executor_profile_invalid`）、F2–F4（文档与 `AGENTS.md` 口径）。复核方自述的测试与
  宿主记录未由它重跑，由本文件的真实命令输出与本机证据承担。
- **测试**：活动 `552 tests OK`（41.9s）；历史 `853 OK (skipped=31)`；T4 34/34 场景、
  T0 0.6s / T1 20.8s / T2 41.7s / T3 93.3s 全过；新增名称无关、Bash/Write/MCP 拒绝、
  策略缺失/类型错误 fail-closed、只读 Profile 不得作为 Executor 等用例。
- **最终代码复核 E2E 证据**：用最终源码重跑 `g4_attest` 核对同一宿主记录，65 项检查
  中仅 `attempt_consumed` 失败（一次性消费的预期行为），Profile 策略、会话、输出 SHA、
  工具调用均再次通过；最终 `native_bridge.validate_review_result` 接受同一复核信封。
- **运行时**：`sync_runtime.py --apply` 写入 10 个受管理文件；`--check` 缺失 0 /
  不同 0；8 个未受管理文件保留。Skill 版本 `v6.6.0`。
- **当时的限制（v6.6.1 已解决）**：`.zcode/agents/reviewer.md` 未删除——原生接口无法在不
  使用 Profile 的情况下施加可验证只读约束。用户随后确认接受通用只读 Profile，
  v6.6.1 已把它改名为 `independent-readonly.md` 并删除旧文件。
- 另：metadata/SQLite 无签名；未做主动 Bash 渗透测试；真实 GUI 会话未人工观察。

## 正式 G4 切换与旧 Reviewer 退役（2026-11-15，阶段九）

在阶段八切换准入 PASS 的最终源码上执行正式切换：正式 G4 改由新链
`g4_attest` + `test_execution` + `g4_shadow`/候选派生，旧 Reviewer 全局 Profile、
Bash 白名单、rollout 强依赖与重复校验入口退役。

- **正式切换**：`copilot_auto._ingest_review` 仅在 `risk=high`、shadow `attested` 且
  `new_g4_eligible=true` 时写 `g4_eligible=true`/`g4_source="candidate"`；信封自报
  `g4_eligible` 不再生效。`governance._review_trust_problems` 只接受 candidate 来源与
  `candidate_g4.eligible=true`，旧记录报 `native_review_legacy_g4_source`。
- **退役**：删除源码 `agents/reviewer.md` 与运行时 `~/.zcode/agents/reviewer.md`；
  Reviewer 只使用工作区 `.zcode/agents/reviewer.md`（`tools: [Read,Grep,Glob]`，
  disallowed 含 Bash/Write/Edit，`injectAgentsMd=false`）。`native_bridge` Reviewer Bash
  白名单、`native_agent` 的 rollout/旧会话/重复证据校验、`preflight-commands` 删除；
  `sync_runtime.py` 不再分发 Reviewer。
- **合同测试要求**：高风险任务显式 `test_policy` 进入合同指纹；binding 从合同派生并
  记录 `requirement_source`/`contract_*`，调用方只能加严；TE 四层 A/B/C/D 与 N/A
  当前合同校验保留。
- **真实 E2E（最终源码/运行时）**：`/tmp/copilot-e2e-stage9e`，task `task-stage9e-e2e`、
  contract `630a0bd2…`、binding `EBace3fd…`、executor `agent_b9618acf…`
  （`bash run-tests.sh` exit 0）、execution `07c38ce5…`、attempt `RAe3297a…`、
  reviewer `agent_8eb506b1…`（profile `reviewer`，4×Read）、review `0174ba43…`、
  raw 2857 字节 SHA `0d67f731…`、attestation `813a58b5…`、TE layers 全 true、
  候选 `eligible=true`（0 reasons）、正式/影子 `pass`、`consistent`、attempt+binding
  消费、`drive` 直接 `CLOSED/pass`（无手工改状态）。
- **原始输出复核**：`verify_stored` 对上述 attestation `ok=true`；把 raw verdict 改为
  `partial` 后 `ok=false`。
- **Headless 新会话**：`sess_b4e6c9c2…` 加载 Skill `v6.5.0`（标题
  `# copilot：轻量协作治理（v6.5.0）`），实际 `Read(.zcode/agents/reviewer.md)`，
  tools `[Read,Grep,Glob]`；`global_profile_exists=false`。宿主 DB 的 tool_usage 与
  part 记录一致。
- **测试**：活动 `541 tests OK`（38.8s）；历史 `853 OK (skipped=31)`；T4 隔离矩阵
  34/34 场景、T0–T3 全过（T0 0.6s / T1 18.6s / T2 39.4s / T3 91.5s）。
- **运行时**：`sync_runtime.py --apply` 写入 15 个受管理文件；`--check` 缺失 0 /
  不同 0；8 个未受管理运行时文件保留；基线 25 个文件。
- **未完成/未验证**：尚无正式 REVIEW；未在真实 GUI 会话人工观察；大写 `PASS`
  归一化仍只有单元测试；metadata/SQLite 无签名；未做主动 Bash 渗透测试。

## 正式切换准入验证与最终演练（2026-11-15，阶段八，未正式切换）

在阶段七最终源码上重做真实 Executor → Reviewer → 候选 G4 → ingest → closure 链路，
把测试要求冻结进合同，修复 E2E 暴露的合同指纹缺口，并完成 T4 与历史证据复核。
正式 G4、旧 Reviewer、Bash 白名单、全局配置/Profile、运行时均未改动；未提交/推送/同步。

- **合同测试策略**：`start_state`/`amend_contract` 接受 `test_policy`（`required` /
  `commands` / `report` / `artifacts` / 两个 require 标志），并纳入
  `contract_sha256` 指纹；旧任务不传策略时指纹口径不变。`create_execution_binding`
  从合同派生命令与层要求，调用方只能加严：合同命令不可换、合同要求的层不能被关闭；
  binding 记录 `requirement_source` / `contract_test_policy` / `contract_report` /
  `contract_artifacts`。`native_bridge` 回填时再次核对，被篡改降低要求会
  `execution_binding_policy_weakened`。合同显式不要求测试时，`test_execution`
  返回 `not_applicable`（A–D 全部 NOT APPLICABLE），候选只在该合同策略下接受；
  无合同策略的旧 binding 不会变成 N/A。
- **E2E 暴露并修复的真实缺陷**：`_gate_criteria` 重算合同指纹时没有带
  `test_policy`，导致带测试策略的任务永远 `contract_fingerprint_mismatch`，
  drive 误派生 `AUTO-NEXT` 并使复核目标过期。已修复并加回归用例；E2E 状态中由该缺陷
  派生的空工作项已移除（测试状态清理），四门重算后通过。
- **最终真实 E2E（干净状态、中性 Reviewer prompt）**：`/tmp/copilot-e2e-stage8e`，
  task `task-1c17b4dff6e43809`、binding `EB833128…`（CLI `--test-policy-file` 创建，
  合同命令 `bash run-tests.sh`、两个 require 层 true）、executor child
  `sess_subagent_agent_d2d5d251…`（真实 `bash run-tests.sh` exit 0）、execution
  `025c12ad…`、attempt `RA89bf8d…`、reviewer child
  `sess_subagent_agent_1fa2dd5f…`（Profile `reviewer`，工具仅 Read/Grep/Glob，Read×4）、
  原始输出 2,705 字节 SHA `8c048b50…`、attestation `5a672314…`、TE 四层 A/B/C/D 全 true、
  候选 `eligible=true`（0 reasons）、正式与影子 `pass`、`consistent/equivalent`、attempt
  正式+候选消费、binding 消费给 execution、`drive` 直接收口 `CLOSED/pass`（无手工改状态）。
  Reviewer prompt 只给文件清单与绑定值，未预置 verdict/findings；模型自行给出 pass、
  3 条 findings（1 minor + 2 info），原始 JSON 未被改写。
- **复核后加固**：合同策略增加 CLI `start/contract --test-policy[-file]`；影子层接受
  合同显式不要求测试的 `not_applicable`（与候选同一 `G.contract_no_test_binding`）；
  bridge 回填逐项比对 `contract_test_policy`/`contract_report`/`contract_artifacts`，
  删除/改写即 `execution_binding_policy_weakened`；合同策略存在时 normal 风险也要求
  binding；TE 要求合同路径位于 workspace 内且 binding 的 `contract_sha256` 等于
  当前合同（amend 后旧 binding 的 N/A/旧命令失效）；N/A 还要求当前合同确有 captured
  不要求测试策略（伪造 contract-source binding 不放行）；`verify_stored` 对新记录
  比对 findings id/severity，旧记录无摘要时只核对 raw 并显式记 limitation。
- **篡改/边界拒绝（真实数据）**：改 verdict 为 partial → `g4_requires_pass`；改
  finding summary → `host_findings_changed`；改 unverified → `g4_with_unverified`；
  `expected_output_sha256` 不符 → attest `host_output_sha_mismatch`（eligible=False）；
  重复 ingest 幂等（review/attestation 数量与 `save_seq` 不变）。
- **历史证据独立复核**：阶段七、阶段八 attestation 均 `verify_stored.ok=true`；
  阶段八新记录含 findings 摘要并可逐条比对，阶段七旧记录无摘要时明确 limitation
  （只核对 raw）。raw 字节 SHA、verdict、findings（新记录）由 `verify_stored` 复核；
  attempt `code_scope`、execution binding、trace/turn/tool_call_ids 由独立重算/
  状态追溯核对。SHA 一致只证明文件完整性与同源记录，不是
  密码学身份认证。
- **新旧 G4 对照**：6 个样本中 4 个 `consistent/equivalent`（阶段七、阶段八 c/d/e），
  2 个 `new_stricter`：一个因 execution/attempt 代码快照漂移
  （`execution_code_scope_mismatch`），一个因 task_dir 在 workspace 外且代码快照为空
  （`task_dir_outside_workspace`、`code_scope_empty`）。未出现 `new_looser`；
  正式 G4 放行逻辑未改。
- **测试**：活动套件 `587 tests OK`（47.2s）；历史 `853 OK (skipped=31)`；T4 隔离
  矩阵 30/30 场景、T0–T3 全过（T0 0.6s / T1 19.4s / T2 48.6s / T3 89.5s）。
  产物增加 mtime 检查：早于本轮测试命令启动的文件即使 SHA 未变也判
  `test_artifact_stale`；该检查已在真实 E2E 数据上重放，TE 仍 verified。

## 候选 G4 闭环与 T4 验收（2026-11-15，阶段七，未正式切换）

在阶段六基础上把 binding/test_execution/attestation 接入候选闭环、持久化原始输出、统一
输出解析、纳入未声明嵌套 Git 仓库，并完成隔离 T4。正式 G4、旧 Reviewer、Bash 白名单、
运行时与全局 Profile 均未改动；未提交/推送/同步。

- **候选闭环**：`copilot_auto._ingest_review` 在正式判定后、单次 `save_state` 前调用
  `_finalize_candidate`：要求 shadow `attested`、新 G4 eligible、TE `verified`、
  execution binding 已消费给该 execution、review attempt 未被其他结果消费、原始输出
  存在且 SHA 与信封/attestation 一致；全部满足才在 attempt 上写
  `candidate_consumed_by_result_sha256` 并持久化原始输出。候选字段
  （`native_reviews[].candidate_g4`、`g4_attestations[].candidate`）与正式
  `g4_eligible`/正式 attempt 消费互不影响。重复 ingest 按结果 SHA 幂等跳过；不同结果
  复用同一 attempt/binding 被候选拒绝。
- **原始输出持久化**：`governance.store_review_output` 将 Reviewer 原始字节原子写入
  `<task_dir>/review-outputs/<sha>.txt`（0600、fsync、replace、回读校验；目录已加入
  code_scope 治理排除）。attestation 记录 `raw_output{path,sha256,bytes,source,parser}`；
  `g4_attest.verify_stored` 可在不读宿主 DB/metadata 的情况下复核：文件存在、SHA 与
  摘要一致、`review_sha256` 一致、可解析为合法 JSON、verdict 与记录一致。真实阶段七
  样本 raw_output 1346 字节、SHA 一致、`verify_stored.ok=true`。
- **输出契约统一**：新增 `scripts/review_output.py`（`review-json/v2`）：整段 JSON 或
  恰好一个围栏且围栏外仅空白；多围栏/围栏外叙述/重复键/非对象一律拒绝；不做文案修正。
  `native_agent` 与 `g4_attest` 的语法解析都调用该模块（语义/绑定检查仍各自执行）。
  旧版本“围栏外可带叙述”的兼容已移除，属明确的版本边界；工作区 Profile 已要求纯 JSON。
- **未声明嵌套 Git 仓库**：`code_scope` 新增 `nested_repos`：从变更路径向上发现含
  `.git`（目录或文件形式）的最近仓库根，递归记录 HEAD/tracked/untracked/删除/重命名/
  更深嵌套并纳入摘要；`_submodule_dirty_state` 内部也递归未声明嵌套。无法读取时快照
  不可用（fail-closed）。真实仓库矩阵覆盖脏 tracked、untracked、嵌套内外层；摘要公式
  再次扩展，阶段六及更早快照需重新取证。
- **原子性与故障注入**：候选标记、attestation、复核记录与正式消费在同一次
  `save_state`（mkstemp+fsync+replace）落盘；模拟保存失败后重读状态：
  `native_reviews`、attempt 消费、`save_seq` 均未变化，证明不会留下部分消费。
  binding 在执行 ingest 时一次性消费到 result_sha；候选再校验其归属。状态损坏
  （`g4_attestations` 非数组）修复为只含本条记录并写 history，不伪造 PASS。
- **真实端到端（阶段七）**：`/tmp/copilot-e2e-stage7`：binding `EB162037…`（nonce 写入
  prompt）→ 真实 executor child `bash run-tests.sh`（exit 0）→ 真实 reviewer 纯 JSON →
  正式 G4 与影子 `eligible=true`、`consistent/equivalent`、TE `verified`（A/B/D）→
  ingest 后 candidate `eligible=true`、raw output 持久化、attempt 正式+候选消费、
  binding 消费到 execution；`verify_stored` 在仅用持久化文件时 ok=true。
- **T4 隔离验收**：`tests/t4_acceptance.py` 仅用临时目录/夹具，运行 T0–T3 与 20 个
  场景矩阵（正常、旧会话、命令不符、非零退出、报告失败、产物过期、越界读、非法输出、
  原始输出丢失、DB 缺失、清理后复核、attempt/binding 重放、submodule/嵌套仓库、
  ingest 中断、增量基线失效、合同变化），报告 `/tmp/copilot-t4-report.json`：
  全部 layer 与 scenario 通过。
- **独立复核与修复**：阶段七两轮独立只读复核。首轮 partial（F2 major + F3/F4/F5/F8/F9）
  已修：`native_agent` 补 `sqlite3` 导入，DB 查询失败抛 `AgentRecordError`；候选把 raw
  解析结果与信封交叉核对；`nested_repos` 增加被忽略的未声明仓库扫描并递归内层；
  `_default_state` 补字段、`store_review_output` 先校验 64 位 hex；CLI `OSError` 映射
  错误退出码。第二轮 partial（F1 major：候选把 raw 与“规范化后”的复核记录比对，带
  findings/大写 verdict 的合法复核会被误判；F2/F3/F4/F5/F6 minor/info）修复如下：
  - 候选改为与**原始信封字段**比对（verdict 仅小写比较，findings/unverified/
    changed_files 用 canonical JSON），不再与 `validate_review_result` 重建后的结构比较；
    新增合 findings+`verdict="PASS"` 的正向用例与“候选不改正式字段”断言。
  - 执行 ingest 改为一次 `save_state`：证据、工作项回填、`native_results`、binding
    消费同批落盘；新增保存失败注入用例（状态无 done 工作项、无证据/结果、`save_seq`
    不变）。
  - 嵌套仓库扫描深度超限改为 fail-closed（`nested_repo_scan_depth_exceeded`），
    submodule/嵌套仓库内部也扫描被 `.gitignore` 忽略的仓库；新增两项真实 Git 用例。
  - `host.host_db` 随信封固化，候选回退读宿主库时使用同一库（旧信封仍回退默认库）。
  - `references/agent-bridge.md` 更新为 `review-json/v2` 严格契约。
  F7（require 标志默认 False）与 F13（raw_output 记在 attestation 内）作为已知设计/限制，
  见切换清单残余风险。
- **测试**：活动套件 `558 tests OK`（46.9s）；历史 `853 OK (skipped=31)`；T4
  `all_layers_ok`、`all_scenarios_ok` 均为 true（T0–T3 + 20/20 场景）。修复后未重跑真实
  ZCode e2e（首轮 e2e 早于 F1 修复，候选 findings 路径由真实归一化单测覆盖）。
- **未解决/限制**：候选 `eligible` 不改变正式 G4 放行，也不改变正式链的消费语义；
  `verify_stored` 只证明持久化输出自洽，不重读宿主记录；DB/metadata/outputFile 仍
  同用户可写、无签名；旧信封无 `output_sha256` 时交叉核对只能跳过；T4 的“真实
  reviewer 旧会话复用”等场景由合成夹具 + 真实 e2e 组合覆盖，未对每个场景重跑真实
  ZCode 子代理。

## 宿主持久化取证与执行证据加固（2026-11-15，阶段六，未正式切换）

在阶段五基础上消除 Reviewer rollout 依赖、建立 Executor 新鲜绑定、拆分测试证据层级、
纳入 submodule 脏工作区。正式 G4、旧 Reviewer、Bash 白名单、运行时与全局 Profile 均未改动；
未提交/推送/同步。

- **Reviewer 输出持久化**：新增 `scripts/host_records.py`（只读 SQLite）。Reviewer 最终输出
  取 `message.role=assistant` 的最后一条非空消息，按 `part(sequence)` 拼接 `type=text`
  文本；真实会话验证其与 metadata `outputFile` **字节一致**（DB 重建 SHA = 文件 SHA）。
  `g4_attest` 优先读 outputFile，两者都在时必须一致（`host_output_db_mismatch`）；
  outputFile 被清理时用 DB 重建（`output_source=db.message.part`），记录 message/part id、
  文本 SHA 与字节数；两者都缺失 → `host_output_missing` 失败关闭。影子把复核信封的
  `host.output_sha256` 传给取证器交叉核对，DB 文本被事后替换会 `host_output_sha_mismatch`
  （旧信封无该字段时只能记录限制，不能回填）。**注意**：G4 合格信封仍要求 outputFile
  存在；DB 回退适用于已有信封后的取证/影子，不是 build_review 的 G4 生成路径。真实验证：移走
  outputFile 与 rollout 后，新 G4 仍 `eligible=true`（DB 输出 SHA 与复核记录一致）；
  DB 同步删除 message/text part 后 → `host_output_missing` 拒绝。
- **rollout 降为辅助**：`native_agent review` 在 rollout 缺失时从 `session_entry/
  runtime/model_selection` 取模型、从 `tool_usage+part` 取调用，生成可取证信封；
  `host.tool_call_source=db.tool_usage+part`、`model_source=session_entry/...`，
  同时追加 `review_rollout_missing` 使旧 G4 保持 `false`（不修改正式放行逻辑）。
  `execution` 同样在 rollout 缺失时从 DB 取模型。`native_bridge` 允许 G4 不合格信封
  缺少 rollout/outputFile 路径，但要求 output SHA 格式合法；G4 合格信封仍强制三件套。
- **Executor 新鲜绑定**：新增 `native_agent bind-execution` 与状态字段
  `execution_bindings`。主代理在启动 Executor **之前**创建 binding（task/work_item/
  dispatch/workspace/预期测试命令/nonce/created_at）；execution 信封携带 binding，
  bridge 校验 binding 存在、未消费、任务/工作项/命令/工作区一致；ingest 一次性消费并
  绑定 result_sha；`test_execution` 要求 `metadata.prompt` 逐字含 nonce、session
  `createdAt ≥ binding.created_at`、binding 被本执行结果消费。高风险任务生成执行
  信封时强制要求 binding（`execution_binding_required`）。旧 completed 子会话的 prompt
  不含本轮 nonce、创建时间早于 binding，因此不能冒充本轮执行方。**正式 ingest/G4 收口
  尚未接入 binding/test_execution**，当前正式放行逻辑不因缺少 binding 改变。
- **测试证据四级**：`test_execution.layers` 分别记录
  `command_execution_verified`（真实命令/退出码/会话）、`expected_test_verified`
  （命令等于 binding 预先声明的预期命令）、`test_result_verified`（JUnit XML 解析
  tests/failures/errors/skipped；要求每个报告 testcase 数与 tests 属性一致、至少一个
  执行用例，`<failure/>`/`<error/>`/`<skipped/>` 子元素即使无属性也计入）、
  `artifact_integrity_verified`（产物在 execution/attempt/当前文件三方 SHA 一致）。
  `layers_required` 逐层记录是否要求，由 binding 的 `require_test_result` /
  `require_artifact_integrity` 或是否声明产物决定；未要求层即使为 true 也不参与
  `verified` 判定。`verified` 仍只是 A 层事实，不等同
  测试质量或产物由该命令生成。
- **submodule 脏工作区**：`code_scope.capture_code_snapshot` 新增
  `submodule_worktrees`，递归记录 HEAD、tracked/untracked/删除/重命名/嵌套 submodule
  的文件状态与 SHA；纳入快照摘要。嵌套 submodule 按声明的 nested 路径递归；status
  非零 → `submodule_status_failed`，嵌套读取失败/未初始化/真实路径越界 →
  `submodule_state_unreadable`，均 `available=false`，fail-closed。真实 Git 矩阵
  覆盖 tracked 修改、untracked、嵌套 submodule 与未初始化拒绝。**注意：摘要公式已扩展，
  阶段五及更早的 execution/attempt 快照会因 digest 变化判为漂移，需重新取证。**
- **真实端到端（阶段六）**：`/tmp/copilot-e2e-stage6` 新任务；binding
  `EBba3e2a…`（nonce 写入 Executor prompt）→ 真实 executor child 执行
  `cd … && bash run-tests.sh`（exit 0）→ 正式 G4 与影子均 `eligible=true`、
  `agreement=consistent`/`equivalent`；`test_execution=verified`，layers
  A/B/D=true、C=false（任务未要求结构化报告）；binding 被本执行结果一次性消费。
  真实负向：Reviewer 读取工作区外 contract → 影子 `read_outside_workspace`
  （new_stricter）；rollout 在 build 与 ingest 之间被清理 → 正式 ingest
  `g4_host_record_invalid`；移走 outputFile/rollout 后新 G4 仍可取证；DB 无最终输出 →
  `host_output_missing`。
- **测试**：活动套件 `528 tests OK`；历史 `853 OK (skipped=31)`。新增
  `test_host_records.py` 5 项、`test_execution_binding.py` 6 项；`test_g4_attest` 166、
  `test_g4_shadow` 41、`test_test_execution` 45（含 JUnit 负向）、
  `test_code_scope_unified` 10（含嵌套/未初始化 submodule）。
- **未解决/限制**：`native_agent` 与 `g4_attest` 的 JSON 围栏解析口径不同（前者较宽、
  后者拒绝围栏外叙述），可能产生假 `new_stricter`；旧信封无 `host.output_sha256` 时
  SHA 交叉核对只能跳过；DB 与 metadata 同用户可写、无密码学签名；DB 输出重建依赖 message/
  part 未被清理（session 删除会级联清理）；metadata 被清理时仍无法取证；binding 依赖
  Executor prompt 原样携带 nonce 与命令（模型改写会 unverified）；`test_result_verified`
  目前只解析 JUnit XML；产物仍不证明由该命令生成；阶段五快照需因摘要公式变化重新生成。

## G4 安全证据收敛（2026-11-15，阶段五，未正式切换）

本阶段完成真实测试执行证据、incremental 基线重放、代码快照统一、影子差异收敛与
正式切换验收清单。正式 G4、旧 Reviewer、Bash 白名单、运行时与全局 Profile 均未改动；
未提交/推送/同步。阶段三、四成果保留。

- **真实测试退出码**：宿主 SQLite `tool_usage` 有真实 `exit_code`/`status`/`started_at`/
  `completed_at`/`output_bytes`/`trace_id`/`turn_id`；`part` 的同一 `callID` 提供
  `state.input.command` 与 `state.output`。`scripts/test_execution.py` 按 executor
  `child_session_id` 合并两表，要求命令逐字匹配、`status=completed`、`exit_code` 存在且
  为 0，才判 `verified`；非 0 判 `failed`，缺字段判 `unverified`。真实 executor 会话
  `agent_9fedf983…` 的 `node --test` 记录 `exit_code=0`；失败探针
  `node -e "process.exit(3)"` 记录 `exit_code=3`，绑定判 `failed`。
- **执行方身份绑定（一致性校验，非防伪）**：execution 信封持久化
  `executor_metadata_path`；`test_execution` 读取该 metadata 并核对
  `agentId`/`childSessionId`/`parentSessionId`/`status=completed`、`metadataFile` 与传入
  路径一致、`profileId` 与 `profileSnapshot.name` 一致且不是只读 Reviewer Profile、
  metadata 的 `workspaceRoot`（存在时）必须等于 attempt 工作区，没有 `workspaceRoot` 时
  `cwd` 必须等于工作区或位于其下（允许在子目录启动）；再核对宿主 `session` 行的
  `parent_id`/`task_type=subagent_child`/`trace_id` 与工具调用 `trace_id` 一致，并要求
  每条测试调用时间落在 metadata `createdAt..completedAt` 内、早于 attempt `created_at`。
  替换会话/换 agent/非子会话/父会话不符/trace 不符/工作区不符/时间越界不能 `verified`；其中
  metadataFile 指向他处、profile 不符、工作区不符为 `failed`，metadataFile 或工作区
  字段缺失为 `unverified`（证据不足而非证明不符）。`build_execution` 对缺失
  metadataFile 保持兼容接受，但影子不会据此判 `verified`。
  **限制**：这些字段同源于 execution 信封与本机 metadata，不能证明会话确属本任务；
  “同一工作区里挑一个真实旧 completed 子会话并声明其命令”仍可能满足一致性校验，正式
  接入前需要把 Executor prompt 与任务/attempt 绑定（见切换清单残余风险）。
- **产物与代码绑定**：execution 信封可选携带 `test_commands`、`test_artifacts`（证据 id）
  与 `code_scope_sha256`；`build_execution` 自动采集工作区快照，`validate_execution_result`
  只校验字段格式（artifact 属于本次证据、SHA 与 code_scope 格式合法）；artifact 在
  attempt `evidence_snapshot` 中冻结、当前 SHA 未漂移、execution 与 attempt 的
  `code_scope_sha256` 相等，均由 `test_execution` 在影子取证时判定，正式 ingest 目前
  不据此拒绝。`native_results`
  持久化这些字段，影子 `g4_attestations[].test_execution` 保存 binding_id、命令/
  tool_call_id/trace/turn/退出码/时间/产物 SHA/metadata 路径与 SHA/session 父会话与 trace/
  limitations；同一输入 binding_id 确定性。
- **Incremental 基线重放**：影子读取基线 review、attempt、持久化 attestation 与 coverage，
  核对 schema/attestation_id/attempt_id/target/code_scope/evidence SHA/verdict/eligible/
  session/父会话/消费方/重复 attestation/跨任务/合同变化；覆盖集按当前
  `review_coverage` 重算，变化证据必须逐条列入 manifest（允许超集、禁止未知 id），
  代码、**其他**工作项、完成条件变化一律拒绝；被复核工作项自身的 action 变化按 `native_bridge` 同一规则放行。
  **基线 attempt 必须已被正式消费**，未消费时影子 attestation 不能补发信任，直接
  `baseline_attempt_not_consumed` 并要求完整复核。基线 attestation 必须带
  `target_source=state`。真实端到端基线（已消费、attestation 可复核、带一处新增证据）
  重放 `problems=[]`；覆盖无变化按 `incremental_without_change` 拒绝（应复用基线结论）；
  tamper/跨任务/未消费/合同变化/覆盖不足均拒绝。
- **代码快照统一**：`code_scope.snapshot_digest` 成为唯一摘要公式，
  `capture_code_snapshot` 与 `g4_attest` 都调用它；SHA 文本/文件 helper 统一到
  `lib_common`。真实 Git 矩阵覆盖初始/修改/新增/删除/重命名/`--base`/symlink/
  治理与构建目录排除/submodule gitlink；所有快照的 digest 与两处重算一致。
- **影子差异收敛**：记录新增 `old_verdict`/`old_reason`/`new_verdict`/`new_reason`/
  `difference_category`/`evidence_summary`，并标注 `old_reason_source` 与
  `old_host_reproducible`。旧 G4 仅因 rollout 缺失而拒绝、或旧 Reviewer 宿主 metadata
  已不可复查时归为 `incomparable`，不再误记为 new_looser；同一证据口径下的真实收紧仍记
  `new_stricter`。`test_execution` 未验证时新 G4 直接不合格，避免“测试未证明却 PASS”。
- **复杂度与职责**：`g4_attest` 1,305 行、`g4_shadow` 583 行、`test_execution` 444 行、
  `code_scope` 252 行。已消除快照摘要与确定性 JSON 序列化的重复实现
  （`code_scope.canonical_json` 与 `snapshot_digest` 为唯一实现）；测试证据、增量重放与差异比较各自独立；
  保留 `native_agent` 的 Profile/attempt/evidence 交叉检查（独立门禁，不与取证器合并）。
  旧 Reviewer Bash 白名单仍被正式路径使用，退役条件见 `docs/DECISIONS.md` 第 41/42 条。
- **真实隔离端到端（阶段五最终轮）**：`/tmp/copilot-e2e-stage5b` 新任务：真实 executor
  child 运行 `cd /tmp/copilot-e2e-stage5b && bash run-tests.sh`（内部 `node --test`），
  `tool_usage.exit_code=0`；真实 reviewer child（工作区 Profile）只读代码/证据并返回 JSON；
  正式 G4 与影子均为 eligible=true，`agreement=consistent`、
  `difference_category=equivalent`、`test_execution=verified`（命令逐字匹配、metadata SHA、
  session 父会话/trace、产物 SHA 三方一致）、attempt 由正式 ingest 一次性消费。该轮同时
  验证了三条真实负向：execution 与 attempt 代码快照不等 → `execution_code_scope_mismatch`
  （改 Profile 后旧执行结果自动失效）；真实 child 自行加了 `cd … &&` 前缀 →
  `test_command_not_found`；reviewer rollout 在 ingest 前被宿主清理 →
  `g4_host_record_invalid` 拒绝入账。
- **测试**：活动套件 `482 tests OK`（`test_g4_attest` 160、`test_g4_shadow` 40、
  `test_test_execution` 30、`test_code_scope_unified` 5、`test_profile_policy` 4）；
  历史套件 `853 tests OK (skipped=31)`。T1 定向 239 项 OK。正式切换验收清单见
  `docs/G4-SWITCH-CHECKLIST.md`。
- **未解决**：测试执行到 execution 信封生成之间的代码变化没有宿主时间戳可证（只以
  execution/attempt 快照相等限制后续窗口）；工作区快照包含未跟踪的 `.zcode` Profile，
  任何 Profile 编辑都会使既有 execution/attempt 失效（安全上正确，操作上要求先稳定
  Profile 再取证）；旧正式 G4 仍依赖 rollout，而 ZCode 只保留最近少量 rollout，子会话
  rollout 可能在 ingest 前被清理，因此正式 G4 必须在 Reviewer 会话结束后立即
  build+ingest，否则失败关闭；执行信封 `test_commands` 需与 Executor 实际命令逐字一致
  （模型自行改写命令会使绑定 unverified）；任何 exit 0 命令都可被声明为测试、任意稳定
  文件都可声明为产物，当前只证明“某条匹配命令在该会话成功、产物三方 SHA 一致”，不证明
  测试充分性，也不证明产物由该命令生成；executor 会话绑定是同源自洽校验，不能阻止同
  工作区旧会话复用；submodule 脏工作区内容不进入
  快照摘要；executor metadata/SQLite 无密码学签名，不是 OS 沙箱。

## G4 影子集成与新只读 Profile（2026-11-15，阶段四，未正式切换）

在用户第四阶段任务授权下完成影子接入、持久化、工作区只读 Profile 与真实隔离验证。
**正式 G4 放行逻辑、旧 `agents/reviewer.md`、全局 `~/.zcode/agents` 和运行时 Skill 均未修改**；
未提交/推送/同步。阶段三成果完整保留。

- **影子接入点**：`copilot_auto._ingest_review` 在 Reviewer 信封校验通过、正式 G4 宿主复核
  与 attempt 消费**之前**调用 `g4_shadow.build`；它只读 metadata/SQLite/attempt/证据/输出，
  不写 consume marker，失败只写 `g4_attestations`，不改变 `native_reviews`、`g4_eligible`、
  attempt 消费或退出码。影子记录与复核记录在同一次 `G.save_state` 落盘，避免“复核已入账
  但取证缺失”的中间态。
- **持久化**：新增 state 字段 `g4_attestations`；记录包含 shadow_id（确定性）、
  review_result_sha256、old/new G4 资格、agreement（consistent / new_stricter /
  new_looser / unavailable / not_applicable）、差异、attestation 摘要（attestation_id、
  schema_version、attempt/target、agent/child/parent session、trace/turn、profile_snapshot、
  model_selection、工具计数与 call ids、证据 SHA、code_scope_sha256、verdict、checks、
  rejections、residuals、limitations、created_at）、incremental 基线与 test_binding。
  `append_attestation` 按 shadow_id 幂等去重；正式 attempt 仍只由现有 ingest 消费。
- **工作区 Profile**：新增 `.zcode/agents/reviewer.md`（工作区级，不发布到全局）。
  ZCode 新进程实际加载后 `profileSnapshot.source=project`、`tools=[Read,Grep,Glob]`、
  `disallowedTools` 含 Write/Edit/Bash/Agent/Task/WebFetch/WebSearch/TodoWrite/NotebookEdit、
  `injectAgentsMd=false`、无 MCP、无 bypass；真实会话工具记录只有 Read/Grep。
  旧 `agents/reviewer.md` 保留不动（仍含 Bash，正式 G4 继续接受；新取证器会拒绝）。
- **快照缺口修复**：`g4_attest` 按 `code_scope.capture_code_snapshot` 的同一规范化公式
  重算 code_snapshot.digest；`kind=other` 必须是 `git submodule status` 中列出的已检出
  submodule 目录（含 `.git`），否则拒绝；真实 submodule gitlink 漂移已用本地仓库复现
  （capture 输出 `kind=other`、`+<sha> sub`，digest 与 `g4_attest` 重算一致）。
- **增量与测试绑定**：影子记录核对增量基线存在、verdict=PASS、g4_eligible、attempt 已消费、
  coverage 非空；缺失/不可用时新 G4 判不合格。test_binding 记录 commands_run、
  files_inspected 与执行证据 SHA；当前执行信封没有 exit_code 字段，退出码绑定明确标记
  `exit_code_bound=false`（未解决，不在本轮伪造）。
- **真实隔离端到端**：`/tmp/copilot-e2e-stage4`（独立 git 工作区 + 工作区 Profile）完成
  attempt → 真实 ZCode child session → Read 代码/证据 → 纯 JSON → metadata/SQLite →
  `g4_attest`（eligible=true、0 rejections、8 次工具调用）→ 影子 ingest → 持久化。旧 G4
  因宿主 rollout 已被清理（仅保留父会话文件）判 `review_rollout_missing`，影子用 DB 判 true，
  当时记录为 `new_looser` 并解释 `old_reason:review_rollout_missing`；阶段五已把这类
  旧证据不可用的差异重分类为 `incomparable`；attempt 未被影子消费。
  由于正式 `build_review` 依赖已清理的 rollout，本次 review 信封是按真实宿主记录手工
  组装为 `g4_eligible=false`（`host` 省略、由影子按 agentId 定位 metadata），只用于影子
  取证，不作为正式 G4 信封；重复 ingest 的幂等由 `tests/test_g4_shadow.py` 的集成用例
  验证，不由该临时 state 的 history 证明。
- **真实负向矩阵**：同一真实会话上验证 candidate 自身/执行方同 agent 或同 session →
  `reviewer_not_independent`；改代码 → `code_scope_drift`；改证据 → `evidence_drift`；
  删证据 → `evidence_missing`；工作区符号链接指向 `/etc/hostname` 的真实 Read →
  `read_symlink_escape`；错误 expected_target → `target_stale`；删 DB/旧会话/已有 consume
  marker → 分别 `host_db_unavailable` / `attempt_session_precedes` / `attempt_replayed`；
  恢复文件后重新 eligible。
- **测试**：活动套件 `431 tests OK`（`tests/test_g4_shadow.py` 24 项、
  `tests/test_profile_policy.py` 4 项、`tests/test_g4_attest.py` 160 项，覆盖 submodule/
  摘要复算/中文边界/异常兜底/幂等）；历史套件 `853 tests OK (skipped=31)`。
- **独立只读复核**：工作区 Profile 由真实宿主加载后，独立 Reviewer 只读取证首轮判
  PARTIAL，发现并已修复 3 个问题：影子未穷尽捕获异常（现由 `build` 顶层兜底，任何异常
  记 `shadow_internal_error` 而不阻断 ingest）；非 G4 复核的 expected_target 回退成
  attempt 自身导致 target 自证（现由 ingest 传当前 `final_evidence_digest(state)`）；
  `g4_attestations` 类型损坏会让 append 抛错阻断复核（现修复为新列表并写 history
  `g4_shadow_field_repaired`，不阻断正式放行）。其余 minor：历史已入账复核不回填影子，
  已在限制中记录。第二轮只读复核 PASS（仅剩 info/minor），随后又收紧了影子异常兜底、
  移除 attempt 自证回退、校验 host metadata 身份并补齐对应测试。
- **复杂度审计**：`g4_attest` 1,312 行、`g4_shadow` 357 行；与 `native_agent` 的 Profile/
  attempt/evidence 检查有职责重叠，但这是独立门禁的交叉取证而非单纯重复。`native_bridge`
  的 Reviewer Bash 白名单仍被正式 G4 使用，不能在新 Profile 切换前删除；退役条件见
  `docs/DECISIONS.md` 第 41 条。证据/代码 SHA 的重复计算保留为独立复算，不合并。
- **未解决/限制**：正式 G4 仍以 rollout 为工具调用来源，而真实 ZCode 只保留最近少量父会话
  rollout，子会话 rollout 常被清理，因此当前无法用真实会话得到旧 G4 `eligible=true`；
  影子 DB 取证已能在同一会话上给出 eligible=true，但本阶段不切换正式门禁。旧 Profile 与
  新 Profile 的输出契约差异（短前言）仍有待正式切换时对齐；历史低风险 review 不回填影子；
  退出码未绑定；metadata/SQLite 无密码学签名，不是 OS 沙箱。Profile 在会话启动时缓存：
  当前长会话仍可能使用旧 Profile，新建进程/会话才会加载工作区 `.zcode/agents/reviewer.md`；
  本次验证使用新 headless 进程。

## G4 宿主取证器（阶段三成果，现以影子模式接入）

在用户第三阶段任务授权下新增 `scripts/g4_attest.py` 与 L2 测试。**未切换现有 G4 入口**，
未删除 `agents/reviewer.md`，未同步运行时，未提交/推送。

- **只读取证**：metadata.json + SQLite（`session` / `part` / `tool_usage` /
  `session_entry`，`mode=ro`）+ attempt JSON + 证据文件 + Reviewer `outputFile`；不调用
  模型、不联网、不执行 Bash、不写宿主数据库。
- **校验**：Profile 工具精确等于 `{Read,Grep,Glob}`、空集拒绝、`disallowedTools ⊇
  {Write,Edit,Bash}`、`permissionMode` 不放宽、无 skills/MCP、来源与路径属于工作区级；
  会话完成、attempt 时间/prompt/workspace/target 绑定、`trace_id`/`turn_id`/Session 一致；
  `tool_usage` 与 `part` 的数量、名称、状态一致；`read_only=1` 且 `side_effect_scope=none`；
  每条文件证据必须有成功的精确 `Read.file_path`；代码快照与证据 SHA 不得漂移；执行方与
  复核方相互独立；attempt 只能消费一次。
- **路径边界**：原生只读 Profile 没有 OS 级路径沙箱。取证按 workspace、任务目录与已登记
  证据文件构成允许集合做 `realpath` 检查；越界（含符号链接、`..`、敏感文件名）默认拒绝
  G4；`--outside-reads record` 仅用于诊断，并写入 residual。
- **持久化**：输出 `copilot-g4-attestation/v1`，含 agent/child/parent session、
  `trace_id`/`turn_id`、`tool_call_id`、工具计数、证据 SHA、代码快照（digest/head/files/
  submodules）、attempt/metadata/review SHA、Reviewer verdict 与 findings 计数、检查项、
  拒绝原因与 `limitations`，以及本次使用的策略开关（`policy`）、attempt 绑定字段与
  `rollout_checked`（rollout 只保留最近少量文件，存在时按 `tool_usage` 子集核对），
  模型身份取 `session_entry` 的 `runtime/model_selection`。
- **独立复核后的加固**：Reviewer 结论强制类型与绑定校验（`schema` 必须为
  `copilot-native-review/v2`；findings/unverified 必须为 list；severity 大小写归一后限
  blocking/major/minor/info；task/plan/dispatch/work_item/execution 在 attempt
  有值时必须在结论中逐字回写，contract 若主动回写则必须一致）；空对象 `{}`、重复 JSON
  键、多个可解析对象、围栏解析失败或
  围栏外叙述均不视为有效结论；`tool_usage` 与 `part` 按 `tool_call_id` 逐调用比对名称与
  状态，`trace`/`turn` 必须唯一且非空；证据必须位于 workspace/任务目录，符号链接越界、
  敏感文件名、offset/limit 部分读取均拒绝，snapshot 条目必须带 path/id/sha256；
  `unverified` 提及证据路径/文件名/证据 ID/代码路径/工作项 ID（含句末句点）视为阻断，
  环境类未验证只进 residual；代码快照要求非空、`kind` 必须显式且 file/symlink 与实际类型一致、
  `git_root` 为 workspace 或含 `.git` 的祖先目录（拒绝 `/`）、`code_scope_sha256` 与
  digest 都非空且一致、`kind=other`（子模块 gitlink）只记 residual；`--task-dir` 必须位于
  workspace 内或恰为 attempt/metadata 目录，否则拒绝；`outputFile` 必须与 metadata 同目录；
  `--output` / `--consume-marker` 不得指向 metadata、attempt、DB、证据、`outputFile`、
  Profile 或代码快照文件；`--consume-marker` 必须与 `--output` 同时使用；执行方身份与
  attempt 消费标记显式校验；`--outside-reads record` 与跳检开关会把 `policy.diagnostic`
  置为 true，消费方必须同时检查该字段。
- **记录内容**：除核心绑定/摘要/检查项外，还保存 `profile`（source/path/sha256/tools/
  disallowedTools/permissionMode）、`review_findings_detail`（id/severity）、`policy`
  开关与 `replay_protection`；`review_sha256` 对 outputFile 原始字节取 SHA，可与
  `sha256sum` 直接核对。
- **测试**：阶段三完成时活动套件 `400 tests OK`（`tests/test_g4_attest.py` 157 项）；
  阶段四当前计数见上方影子集成小节。历史套件 `853 tests OK (skipped=31)` 未变化。
- **独立只读复核**：同一原生 Reviewer 独立上下文连续三轮（每轮只读、未改文件），最终结论 PASS、无 blocking；期间修复了解析器围栏叙述、绑定缺失、`task_dir` 扩大允许根、`kind=other` 假拒绝、声明摘要缺失、证据条目弱校验、trace/turn 空值、`unverified` 中文紧邻漏判等问题。
- **真实宿主验证**：在隔离 `/tmp/copilot-g4-probe` 用真实 ZCode 子代理会话完成一次端到端
  取证：`attest-v4-real.json` 为 `eligible=true`、0 failed checks（13 次工具调用、
  trace/turn/session/evidence/代码快照全部绑定，review SHA 与 `sha256sum` 一致）；
  `attest-v4-replay.json` 被 `attempt_replayed` 拒绝。旧 v3 探针是旧版代码的产物，
  其宿主记录（metadata/SQLite/part/tool_usage）在旧实现下通过；当前代码还会因缺少
  `kind`、旧输出缺少绑定字段而拒绝它，因此不再用它证明当前版本的兼容性。
- **已确认限制**：metadata 与本地 SQLite 没有密码学签名；未重跑 git，attempt 之后新增的
  未跟踪文件与 submodule 提交仍由 G2 `code_scope` 负责；`kind=other`（子模块 gitlink）只记
  residual，不校验内容；incremental 只检查 `evidence_manifest` 与
  `based_on_review_sha256` 存在，未重放基线结论；路径检查只能拒绝 G4，不能阻止
  越界内容已经进入模型上下文。当前正式 `agents/reviewer.md` 仍含 Bash，会被
  `profile_tools_not_exact`/`profile_disallowed_missing` 拒绝；其输出契约允许一句短前言
  加单个围栏，而本模块要求 JSON 外无任何文本，两者对齐（改 Profile 或放宽解析）属于
  下一阶段授权范围。
- **运行时状态**：按本阶段要求未同步；当前 `sync_runtime.py --check` 为 `缺失 2`
  （`g4_attest.py`/`g4_shadow.py`）、`不同 3`（`copilot_auto.py`/`governance.py`/
  `native_bridge.py`）、Reviewer `same`。正式切换与同步前不应把该检查当作回归。
- **下一步**：阶段四已完成影子接入、持久化与真实隔离验证；正式切换 G4、删除旧 Bash
  白名单与旧 Reviewer Profile 仍需另阶段授权。

## v6.4.0（2026-11-14，当前）：质量保障精简

本批次在用户任务授权下完成，**未提交、未推送、未发布**（用户明确本任务不授权 Git 提交/推送/发布）。

- **动机（专项审计）**：基于《测试体系与独立复核机制过度设计专项审计报告》的真实任务证据，
  只处理三项 P1：BLOCKED 受控写入、复核顺序与 PASS 收口、正式 REVIEW amendment。
- **BLOCKED 受控写入**：`record`、`rerun`、`evidence --add`、`criterion`、`report`、
  `judgment`、额外 `block`、已授权的 `authorize-review` / `formal-review`（含 amendment）
  在 BLOCKED 下允许登记；不解除阻塞、不改变合同/授权、`close` 仍拒绝。`plan`、`decide`、
  `evidence --drop` 继续拒绝。依据：某性能优化项目 4 次、某广告项目 4 次 `unblock → 登记 → block`。
- **复核顺序**：`AUTO-G4` 工作单要求先核销条件、稳定证据与摘要，再读取 `review-context`
  的 warnings/scope_reason 做最终复核；PASS 后 minor/info 记录为 findings/residual_risks，
  不修改已通过证据重开完整复核；后续实质变化仍必须重新判断原 PASS 是否覆盖新快照。
- **正式 REVIEW amendment**：新增 `formal-review --amend <review_id> --reason` 与
  `--supersedes <review_id> --reason`。非实质修订不生成新 PASS，目标摘要变化时拒绝；
  supersede 保留旧记录并由 `review-metrics` 统计。原始授权快照、verdict 与 `target_sha256`
  不被覆盖；BLOCKED 下仍要求有效用户授权。
- **指标**：`review-metrics` 新增 `formal_review_amendments_total`、
  `superseded_formal_reviews_total`、`active_formal_reviews_total`；不复用旧口径把
  amendment 计为新的正式 REVIEW。
- **测试**：活动套件 `243 tests OK`（新增 `tests/test_v64_convergence.py`、
  `tests/test_snapshot_binding.py` 及 governance/CLI/docs 回归）；相关 legacy native
  回归 `118 tests OK`；历史套件未全量运行（本次未修改 `lib_common` / `bridge_evidence`，
  按任务要求不做形式全量）。
- **运行时同步**：`sync_runtime.py --apply` 写入受管理文件；最终 `--check` 缺失 0、不同 0、
  Reviewer same；关键文件源码/运行时 SHA-256 逐项一致；`--baseline` 已刷新为 21 个文件
  （新增 `scripts/code_scope.py`）。
- **独立只读复核（非 G4、非正式 REVIEW）**：一次针对性原生 Reviewer 复核（只读、独立上下文），
  报 7 条 minor/info、无 blocking/major；已修 review-metrics 空 review_id 误计 superseded、
  AUTO-G4 “warnings 为空”与增量复核冲突、多阻塞一次全量解除、SKILL 标题版本、
  minor/residual_risks 分类口径与文档精度；并补对应测试。
- **G4 快照绑定完整性修复（v6.4 补强）**：原实现只在信封生成时重算 `target_sha256`，
  宿主记录未绑定 Reviewer 会话开始时间与内容快照，理论上旧 Reviewer 原文可被重新封装成
  新 target 的结论。现改为派发前 `review-context --create-attempt` 冻结 task / contract /
  plan / dispatch / work item / execution / `final_evidence_digest` / 条件摘要 / 文件证据
  内容摘要；`review --attempt` 要求 attempt 未消费、目标未过期、宿主 `createdAt` 晚于
  attempt 且 prompt 携带 attempt id；ingest 重新核对宿主记录后在同一次状态写入中消费
  attempt；G4 收口核对 attempt 存在、已消费、会话一致、目标 = attempt 快照 = 当前摘要，
  并重新核对文件证据内容。被拒 attempt 不消费，可修正后重试；已消费 attempt 不能重放。
  `tests/test_snapshot_binding.py` 覆盖 S0 PASS、代码/证据/合同变化过期、会话不匹配、
  attempt 重放、旧输出重封装、复核期间文件漂移、新快照重新 attempt、失败重试与幂等回填。
- **G2 完整代码范围（最终验收补强）**：新增 `scripts/code_scope.py`，由宿主从
  `git status --porcelain=v1 -z` 生成任务工作区代码快照，覆盖 tracked 修改、untracked、
  新增、删除、重命名，逐文件记录状态与 SHA-256；`.gitignore` 排除构建输出，治理临时
  目录/缓存额外排除，已登记证据由证据快照单独绑定。`review-context --create-attempt
  --workspace <repo>` 把代码快照写入不可变 attempt；`review --attempt`、ingest 与 G4 都
  重算代码快照，任何未登记源码/未跟踪文件/删除/重命名变化按
  `native_review_code_scope_drift` 失败关闭；G2 使用与 G4 相同的合格复核/attempt，并检查
  其代码快照。高风险任务在非 git 工作区拒绝创建 attempt；attempt 的 workspace 还必须与
  任务证据所属仓库一致。默认快照覆盖工作区相对 HEAD 的变更，任务代码已提交时用
  `--base <任务开始 commit>` 纳入已提交范围。增量复核只在代码范围未变化时可用。
- **真实 ZCode 宿主 Session 探针**：在隔离临时任务（`/tmp/copilot-g4-probe2-sP3th0`）用
  真实 Reviewer 子代理（`agent_42c6012a-...`，child `sess_subagent_agent_42c...`）走通：
  attempt `RAee6358e...` → Reviewer 实际读取证据并运行只读 `git status`/`git diff --stat`
  → 生成信封（g4_eligible=true）→ ingest 消费 attempt（`consumed_by_result_sha256` +
  Reviewer session）→ `closure_check` 全部通过。宿主 metadata 实测提供
  `createdAt`/`completedAt`/`prompt`/`workspaceRoot`/`cwd`/`outputFile`/rollout；prompt
  包含 attempt id、`workspaceRoot` 等于 attempt 代码根。用同一旧 session 绑定新 attempt
  被拒绝（`review_attempt_session_precedes_attempt` + `review_attempt_prompt_unbound`）。
  探针完成后本仓库继续修改；用最终源码复查该临时状态会按预期报
  `native_review_code_scope_drift`，这是 fail-closed 的正确结果。探针只证明当时真实
  宿主的字段/会话绑定，不作为当前仓库的 G4 结论。
- **Headless 新会话加载**：用独立 `node zcode.cjs -p ... --json --surface terminal
  --cwd <repo> --mode plan` 启动新会话
  `sess_86c0a3dc-4976-4eac-afc8-6b1a5aa69632`；rollout 记录实际调用
  `Skill(skill="copilot")`、`Read(.../skills/copilot/SKILL.md)`、
  `Read(.../agents/reviewer.md)`；返回 `skill_loaded=true`、标题
  `# copilot：轻量协作治理（v6.4.0）`，review_attempt/create_attempt/code_scope 与
  Reviewer attempt/code_snapshot 均识别为 true。
- **未验证**：未启动正式 REVIEW；未执行真实生产 G4（隔离探针不计生产 G4）；未在真实
  GUI 新会话观察加载（headless 新会话已验证）。
- **残余风险（诚实边界）**：attempt/宿主 metadata/rollout 与状态文件不是密码学签名；
  能改写本机文件的人仍可伪造宿主记录。代码快照证明“派发范围 = ingest 接受范围 = G4
  范围”，但不等于证明 Reviewer 逐字节读取了每个文件；实际读取仍由 rollout 的
  Read/Bash 记录与 `files_inspected` 体现。`code_scope` 排除已登记证据与 `.gitignore`
  构建输出，未登记的业务配置/产物是否属于源码由任务范围决定。

## v6.3.0（2026-11-14，历史）：Reviewer 多轮评审根因收敛

本批次在用户任务授权下完成，**未提交、未推送、未发布**（用户明确本任务不授权 Git 提交/推送/发布）。

- **动机（10 个真实任务复盘）**：某广告策略项目（7 轮复核、0 份信封入库）、某广告变现项目（ECPM）
  （7 次 G4 尝试、3 次机械有效、最终 1 份 partial）、某复核专项（1 份有效复核 + 4 次
  正式 REVIEW、证据编号频繁漂移）、gromore（A10/A13 各 8 条 finding）、某插屏广告项目、
  某广告平台 / 某投放链路 / usb-debug / 某性能优化项目 / splash。真正造成返工的是机制摩擦而非
  Reviewer 判断质量：安全命令缺失导致整轮作废（`git rev-list`、`paste -sd+`）、危险
  长选项缩写未覆盖（`git log --out=`、`git show --ext-d`、`git cat-file --filt`）、
  围栏 JSON 变体被判不可解析、`cd` 后相对路径被判“未访问证据”、finding 无可复用 ID/
  根因/状态导致跨轮重复、无效信封只打印到终端、执行证据漂移需人工发现、无预检与上下文
  助手、Reviewer Profile 缺少“首轮看全/后续只验变化/PASS 即停/每轮新会话”的协议。
- **只读策略**：白名单新增只读 git 子命令（`rev-list`/`cat-file`/`ls-tree`/`show-ref`/
  `for-each-ref`/`describe`/`merge-base`/`name-rev`/`check-ignore`/`check-attr`/
  `diff-tree`/`diff-index`/`diff-files`/`shortlog`/`blame`/`count-objects`）与文本流
  工具（`paste`/`comm`/`join`/`tac`/`rev`/`fold`/`expand`/`unexpand`/`column`/`cksum`/
  `seq`/`base64`）；`--filters` 加入 argv 拒绝；`git diff/log/show/diff-tree/diff-index/
  diff-files/cat-file` 的 `--output`/`--output-file`/`--ext-diff`/`--textconv`/
  `--open-files-in-pager`/`--filters` 唯一缩写一律拒绝（`--text` 精确放行）。
  顶层 `&&` 与 `;`/`|` 同等处理（两侧各自过只读校验，单独的 `&`、`||` 仍拒绝）；
  通配符含 `/` 时对 `sed`/`awk`/`find` 放行（展开结果仍带 `/`，写操作另有参数校验），
  `uniq`/`sort -o`/`xxd` 仍要求引号。
- **解析与覆盖**：围栏解析容忍语言标记大小写/空格、围栏后无换行、CRLF 与前后说明，
  仍拒绝多个可解析对象；`cd <dir>;`、`cd <dir> &&` 与 `git -C <dir>` 后的相对路径按
  目录解析，修复 `review_file_not_inspected` 假阴性（honor G4 第 6 次尝试的真实根因）。
- **finding 元数据**：保留 `id`（F1–F9999，轮内唯一）、`status`（open/resolved/
  regressed）、`root_cause`/`category`/`related`/`reproduce`；同轮重复 ID 或完全相同的
  `(severity,file,line,summary)` 拒绝（`review_finding_duplicate`）。
- **预检与上下文**：`native_agent.py preflight-commands`（`--file`/`--command`，失败
  exit=2）；`native_agent.py review-context` 输出绑定值、证据、`execution_evidence_unbound`
  漂移、上轮 findings 与建议 scope。
- **审计与指标**：ingest 写 `native_result_ingested`/`native_review_ingested` 历史事件；
  被拒信封追加到任务目录 `review-attempts.jsonl`（只记错误码与绑定值）；`closure-check`
  新增“执行结果引用旧摘要/已归档/未登记证据”非阻断提醒；新增
  `copilot_dispatch.py review-metrics`（有效轮次、首轮 finding、重复 finding、无效信封、
  重复命令/文件、证据包字符数、首轮到 PASS 耗时；正式 REVIEW/G4/原生复核分别计数）。
- **Reviewer Profile**：`agents/reviewer.md` 增加首轮完整性协议、根因归组、稳定 ID、
  证据/严重度门槛、follow-up 复用与不重报、PASS 停止、每轮新会话（续跑会重置
  `totalToolUseCount`）。
- **回放事实**：11 份真实复核信封的 298 条唯一命令在 v6.2 与 v6.3 下均 0 拒绝；宿主仍保留
  的 1 份真实 Reviewer rollout（120 条唯一 Bash 命令）在 v6.2 下 1 条被拒（`awk '…'
  app/build/test-results/*/*.xml`），v6.3 下 0 拒绝。被拒的 `cd … && ls …`（某复核专项 A8）
  原始命令文本未保留，按同一模式补了回归用例。
- **真实端到端（2026-11-14 18:39–18:55 CST，`/tmp/v63-e2e`，risk=high）**：真实
  dispatch → 真实执行子代理 → 4 轮真实 Reviewer → G4 信封 → ingest → `drive` 收口，
  `closure-check` 四门 PASS、任务 CLOSED；`task_seconds=935`。过程事实：
  ① 第 1 轮 `g4_eligible=true`，但复核后核销 C1 改变了 `final_evidence_digest`，收口时
  `native_review_stale`（顺序问题，非机制缺陷），已由 `review-context` 的 `warnings` 与
  `scope_reason` 提前提示“先核销条件再最终复核”；
  ② 第 2 轮 Reviewer 输出漏 `execution_result_sha256`，`review_build` 拒绝（fail-closed 正确，
  属派发提示漏写绑定字段）；
  ③ 第 3 轮实际命令含 `echo '---exit:'$?` 与 `python3 -c`，被只读策略拒绝（`$` 未加引号、
  解释器不在白名单），整轮信封未生成；
  ④ 第 4 轮聚焦复核 18 次真实工具调用、全部通过只读复查，`g4_eligible=true`，目标摘要
  `89423ca7…` 与当前 `final_evidence_digest` 一致。
- **e2e 暴露并已修**：生成阶段被拒此前只打印到终端，现已追加到 `review-attempts.jsonl`
  （`kind=review_build`，带 `work_item_id` 与 `execution_result_sha256` 归因）；
  `agents/reviewer.md` 与 `references/agent-bridge.md` 明确未加引号的 `$`（`$?`/`$VAR`）
  与解释器单行命令（`python3`/`perl`/`node`/`sh -c`）会被拒，取证改用
  `sha256sum`/`grep`/`sed -n`/`head`/`wc`/`diff`/`stat`。
- **指标实测（同任务）**：`native_reviews=2`、`g4_reviews=2`、`invalid_envelopes`
  `{execution:0, review:0, review_build:1}`（`review_command_not_readonly`）、
  `duplicate_findings=0`、`repeated_command_count=0`、`avg_package_chars=5104`。
  已知口径限制：第 1 轮的 PASS 在收口时已过期，但 `review-metrics` 仍按 `pass_round=1`、
  `time_to_pass_seconds=0` 统计，过期性只在 `closure-check`/G4 反映。
- **测试**：活动套件 `192 tests OK`（新增 `tests/test_v63_convergence.py` 26 项）；
  历史套件 `853 tests OK (skipped=31)`。
- **新会话加载验证（2026-11-14）**：桌面 GUI 入口不可用于验证——`<zcode-binary>` 实际是
  Electron 单实例应用，`--help` 会接管/复用现有桌面进程，会干扰用户当前会话，故未启动
  GUI 新会话。改用同版本 CLI 的 headless 新进程
  （`node <zcode-cli-entry> -p … --cwd <repo>`）实测：
  `skills list` 发现 `copilot`；新进程内派生的 Reviewer
  （`agent_9244436c…`，独立 `sess_8e575ffd…`）`profileSnapshot.systemPrompt` 为 16,073 字符，
  含 v6.3 的 `git rev-list`、`paste`、`&&`、`$?`、`python3` 说明，只读工具集
  `[Read, Grep, Glob, Bash]`、`disallowedTools=[Write, Edit]`，并在无绑定时正确返回
  `blocked`；另一次 headless 会话经 Skill 工具加载正文，引用到
  `当前版本 **v6.3.0（ZCode Autonomous Development Edition）**`，证明运行时 SKILL.md
  未被截断。对照：本会话（启动早于同步）的真实 Reviewer `profileSnapshot` 仍是缓存的
  上一版，说明 Profile 在会话/进程启动期缓存，符合"配置修改后需新会话验证"。
- **未验证**：GUI 桌面新会话本身的加载观察（受上述单实例限制），以 headless 新进程替代；
  未在真实生产高风险任务上跑本链路（e2e 只限 `/tmp` 临时目录）；真实模型调用仅限本任务
  授权的 Reviewer/执行子代理与上述新进程加载探针，提交、推送、发布均未执行。

## v6.2.0（2026-11-13）：实战复盘收敛

本批次在用户任务授权下完成，**未提交、未推送、未发布**（用户明确本任务不授权 Git 提交/推送/发布）。

- **动机（5 个真实任务复盘）**：v6.1 自升级、某应用、某广告变现项目（ECPM）、某插屏广告项目、
  某投放链路 激励。真正造成返工的仍是复核机制可避免的摩擦，而不是 Reviewer 判断：
  SDK 工具用绝对路径被前缀白名单误拒（gromore `A10.review.json` 第 22 条命令）、
  无法读取 APK 内 dex（只能 `unzip -l`）、未加引号通配符使整轮复核作废（honor
  `G4-review-notes.md` 第 2/3 轮）、G4 失败只说“复核 A12 不可用”不说明哪里过期、
  任务 CLOSED 后迭代仍为 `RUNNING`、证据重复登记与 `/tmp` 证据无提示、
  收口时无法一眼看清单未提交/未跟踪产物。
- **只读策略补全**：绝对路径只对 `aapt`/`aapt2`/`apksigner`/`dexdump`/`javap` 归一化，
  且 realpath 不得在复核工作区内（工作区根取 cwd 最近的 `.git` 祖先，子目录取证不改变
  边界）；相对路径可执行文件与通用命令绝对路径一律拒绝（防同名二进制冒充）。
  `unzip -p`/`-c` 允许（解压到 stdout，不落盘）；`-P`/`-I`/`-O`（取值选项跨实现行为
  不一致）、非 zipinfo 模式的 `-T`（改写归档时间戳）与 `-d` 拒绝，`-x` 与 `unzip -Z -T`
  允许。未加引号的
  通配符只对没有输出位置参数、也没有写/执行选项的只读族放行（`ls`/`cat`/`sha256sum`/
  `grep`/`diff`/`jq`/`zipinfo`/`aapt2` 等）；`file`/`rg`/`git grep`/`unzip` 与
  `sort`/`uniq`/`xxd`/`sed`/`awk`/`find` 仍要求加引号。写变体继续拒绝。
  7 份有效真实复核信封（共 152 条命令）与 1 份被拒的无效尝试（32 条）在新策略下全部通过，
  v6.1 下 gromore A10 有 1 条被拒。
- **G4 诊断**：`native_review_stale` 的原因列出相对覆盖快照变化的条件/工作项/证据
  （无快照的旧记录明确说明）；`native_review_contract_mismatch` 说明合同改动后需要完整复核。
  判定仍由摘要相等决定，未放宽 fail-closed。
- **迭代生命周期**：`begin_iteration` 结束上一轮（`ENDED`），`close_task` 把未结束迭代
  标记为 `CLOSED` 并补 `ended`；不再出现“已收口但迭代 RUNNING”。
- **证据提醒（非阻断）**：`closure-check` 提示重复登记同一文件、不同 id 内容相同、
  `/tmp`/`/var/tmp`/`/dev/shm` 证据、`evidence/` 下未登记文件；`evidence --add` 幂等时
  明确输出“已存在且摘要一致（未新增）”。
- **只读交付清单**：新增 `handoff` 子命令，输出分支/HEAD/上游/落后领先、已暂存、
  未暂存与未跟踪文件分类（治理记录/任务证据/验收产物/未分类）、任务阶段与证据统计、
  缺失证据文件；只运行 `git --no-optional-locks` 查询，不提交/删除/改忽略规则。
- **独立只读复核**：第一轮原生 Reviewer 指出第一版路径归一化会让任意同名二进制冒充白名单
  命令、未加引号通配符可被 `-` 开头的文件名注入写/执行选项、`unzip -P -l` 实际执行默认
  解包等问题；已按建议收窄策略并补回归用例。第二轮指出工作区判定只比 cwd（子目录取证时
  仓库根二进制仍可执行）、`unzip -T` 会改写归档时间戳、`-I`/`-O` 同样吞取值、对 `-x`
  的拒绝属过度收紧、`--capture-cmd` 提示的“先 --drop”不可行；已改为按 `.git` 祖先取
  工作区根、拒绝 `-T`/`-I`/`-O`、恢复 `-x`、修正提示，并补对应回归用例。第三轮抽查确认
  上述修复生效，另修：工作区包含判定改用 `commonpath`（cwd 为 `/` 时不失效）、同时检查
  未解析符号链接的字面路径、`unzip -Z -T` 不再误拒、capture 用例断言真正的采集目标文件。
  测试由 16 项增至 23 项。
- **测试**：活动套件 `166 tests OK`（`tests/`，新增 `tests/test_v62_convergence.py` 23 项）；
  历史套件 `853 tests OK (skipped=31)`。
- **未验证**：没有启动新的 ZCode GUI 会话观察运行时加载；没有在真实高风险任务上重跑完整
  G4 链路（只用真实信封重放命令校验与本地夹具）；真实模型调用、提交、推送、发布均未执行。

## v6.1.0（2026-11-13）：复核收敛与自主推进优化

本批次在用户任务授权下完成，**未提交、未推送、未发布**（用户明确本任务不授权 Git 提交/推送/发布）。

- **动机（真实任务复盘）**：最近两个真实任务（某广告平台 6 轮复核、honor 7 次 G4 尝试）中，
  主要浪费来自复核机制本身而非 Reviewer 判断质量：只读白名单过窄使 Reviewer 自己作废自己
  （`sed -n`、`for` 循环被判越界）、要求自报命令并与记录逐字比对、每轮重读全部证据、
  BLOCKED 下无法修订合同、已终结工作项修复后无法重验。
- **只读策略**：按实际写风险放宽（`sed -n`/只读 awk/`echo`/`printf`/`jq`/单输入 `xxd`/
  不带 `-C` 的 `file`/`unzip -l`/`aapt dump`/`apksigner verify`/单层字面量 `for` 等）；
  写变体失败关闭：`sed -i/-ni/--in-pl/--fil=`、`sed` 脚本 `w/W/e`、awk 写重定向/管道/
  `system(` 与 `-f/-i/-l/-p/-o/-d`/长选项缩写、`xxd` 第二位置参数、`file -C`、
  `date -sX`/位置参数、`tree -o`、`unzip -d`、`aapt2 link`、`apksigner sign`。
- **命令与文件由宿主派生**：`commands_run`/`files_inspected` 从 rollout 的实际调用得到，
  Reviewer 不再自报；自报但无实际访问痕迹的文件写入 `declared_files_unverified`。
- **增量复核**：`review_scope=incremental` + `--based-on` 只覆盖基线之后新增/变更/归档的证据；
  条件、合同或其他工作项变化拒绝增量。基线必须是可用的 G4 PASS，因此只适用于高风险链路；
  收口时 `governance` 重算覆盖，空增量也拒绝。
- **修复后重验**：`rerun` 把已终结工作项重开为 `pending`，旧结果保留在 `rerun_history`。
- **合同源漂移**：`evaluate`/`status` 返回非阻断 `warnings`；BLOCKED 下允许 `contract --amend`。
- **宿主输出交叉核对**：ingest 重新解析宿主 `outputFile`，与信封 verdict/findings/
  unverified/changed_files 不一致即拒绝（`host_*_changed`）。
- **测试**：活动套件 `143 tests OK`（`tests/`）；历史套件 `853 tests OK (skipped=31)`。
  新增 `tests/test_v61_convergence.py`（17 项）与 `test_native_agent_v6` 的 2 个宿主输出篡改用例。
- **独立只读复核**：三轮原生只读子代理（均非 G4、非正式 REVIEW，未提供 Copilot 信封绑定）。
  第一轮指出 3 个 substantive（`xxd`/`file` 写路径、`sed` 选项缩写与聚簇、awk `system`
  空白/写选项）；第二轮针对性复核又指出 `file --co`、awk 比较表达式误拒/漏拒、
  `unverified`/`changed_files` 仅布尔比对等 7 项；第三轮聚焦前轮修复，发现 3 个真实问题：
  `date -I`/`--iso-8601` 把下一个 token 当作自身取值而跳过位置参数（`date -I 010100002020`
  被放行）、gawk 的 `|& getline` 未匹配、print 参数里的合法 `||`/`>=` 被误拒。三项均已修复
  并补测试（`date` 只有必需取值选项消费下一 token；`|&` 纳入管道检测；`||`/`>=` 不再判写）。
  第三轮同时确认 `sed 'y/a/w/'`、`sed 'a\welcome'` 放行是正确的（不写文件），此前任务清单
  中“必须拒绝”的措辞有误。复核记录不构成 G4 证据。
- **运行时同步（2026-11-13）**：`sync_runtime.py --apply` 先写入 9 个受管理文件（`SKILL.md`、
  4 个 `references/*.md`、`scripts/{copilot_dispatch,governance,native_agent,native_bridge}.py`）
  与 Reviewer；第三轮修复后再次 `--apply` 写入 `scripts/native_bridge.py`，`--check` 均为
  **缺失 0、不同 0、Reviewer 一致**。运行时 `SKILL.md` 14,537 B（<100,000 上限），frontmatter
  可解析，引用的 7 个路径全部存在，运行时脚本 `py_compile` 通过。运行时仍保留 8 个未受管理
  的历史文件（默认保留）。
- **未验证**：没有启动新的 ZCode GUI 会话观察会话内加载；未在真实高风险任务上跑完整
  G4 增量复核链路（只有本地夹具与单元/集成测试）。真实模型调用、提交、推送、发布均未执行。

## v6.0.0（2026-11-12）：SOL 退役与自主化架构升级

本批次在用户任务授权下完成。提交前现场为**未提交、未推送、未发布**；2026-11-12 用户随后明确授权提交与普通快进推送，实际提交 SHA 以 git 历史为准（本节其余「未提交」表述均为提交前的现场记录）。

- **SOL 退役**：`scripts/` 活动路径不再有 `gpt-6.1-sol`、CPA 调用、轮次/预算状态机、
  等待远程模型返回的门禁。`call_gpt.py`、`review_ledger.py`、`reply_protocol.py` 与 13 个
  历史测试移入 `legacy/`；`native_bridge.py` 拒绝 v1 信封。
- **本地治理引擎**：新增 `scripts/governance.py`（schema `copilot-governance/v1`），阶段收敛为
  RUNNING / PARTIAL / PASS / BLOCKED / CLOSED；原子写入 + `fcntl.flock` + `save_seq` CAS。
- **PARTIAL 持续推进**：`copilot_auto.py drive` 在同一调用内派生
  `AUTO-C<n>` / `AUTO-R-<id>` / `AUTO-G4` / `AUTO-G4-INGEST` / `AUTO-REPLAN` / `AUTO-NEXT`
  并继续；`MAX_DRIVE_STEPS=32` 只是防死循环保护（超限退出 12），不构成任务终止条件。
- **CONSULT / PLAN / REVIEW 重定位**：CONSULT 本地分析不阻塞；PLAN 由 ZCode 决定、Copilot
  只登记；正式 REVIEW 只在用户明确授权（`authorize-review --authorized-by user`）时登记，
  不参与 G1–G4。
- **Native Bridge v2**：dispatch/result/review 三信封绑定 task/contract/plan/iteration；
  `g4_eligible` 由 `native_agent.py` 按宿主 metadata/rollout 计算，`ingest` 回填前重新核对；
  G4 仍 fail-closed。
- **测试**：当前套件 `124 tests OK`（`tests/`）；历史套件 `853 tests OK (skipped=31)`
  （`legacy/tests/`：29 个类级历史文档契约 + 2 个方法级跳过，共 31 项；另有 1 项
  `skipIf` 只在无 git 环境跳过）。
- **文档**：`SKILL.md` 重写为 v6（轻量治理 / PARTIAL / 正式 REVIEW opt-in / G4），
  `references/workflow.md`、`runtime.md`、`review.md`、`agent-bridge.md`、`history.md`
  同步；v5 SOL 角色/台账文档加历史横幅；`agents/reviewer.md` 改到 v2 绑定。
- **运行时同步**：`sync_runtime.py --apply` 首批写入 17 个受管理文件，复核修复后增量写入
  10 个（`SKILL.md`、`governance.py`、`copilot_dispatch.py`、`copilot_auto.py`、
  `native_bridge.py`、`native_agent.py`、`lib_common.py`、`references/*`），Reviewer 已写入；
  末次 `--check` 缺失 0、不同 0、Reviewer 一致。运行时目录仍有 8 个 v5 遗留未受管理文件
  （`call_gpt.py`、`reply_protocol.py`、`review_ledger.py` 及 5 个旧测试），当前无任何
  脚本引用它们，默认保留；如需清理需显式 `--prune` 授权。
- **运行时加载验证（静态 + 实际执行）**：运行时 `SKILL.md` 12,136 B（<100,000）、
  frontmatter 完整、description 125 字符、末尾未截断；`references/` 五个 v6 文档存在；
  运行时 `copilot_dispatch.py --version` 输出 `6.0.0`；在临时任务上实测
  `PARTIAL → 派生 AUTO-C1 → RUNNING 工作单`，退出码 10，无任何远程调用。
- **独立只读复核后修复（2026-11-12，同一批次内完成，未提交）**：两轮独立只读审计
  （不同子代理上下文，不是 G4 记录，`g4_eligible=false`，不作为收口依据）确认主线成立，
  并指出以下真实缺陷，均已修复并补测试：
  1. `--risk high` 配默认 `legacy` 后端会永远无法产生可回填的复核（G4 死锁）→ `start`
     改为 fail-fast 要求 `--backend native`；`review.md`「AUTO-G4 不走原生 dispatch」
     的错误说法已改。
  2. 正式 REVIEW 授权 `--authorized-by` 曾有默认值 `user` → 取消默认、改为必填，并绑定
     当前合同指纹；`reopen` 同样要求显式 `--authorized-by user`。文档说明该字段是调用方
     声明而非可验证凭证。
  3. G4 收口只信任状态里的 `g4_eligible` → ingest 时固化宿主记录摘要 `host_sha256`，
     收口重新核对摘要、Reviewer 身份与工具调用记录，新增
     `native_review_host_missing/changed/identity_mismatch/no_tool_calls`。
  4. 文档/CLI 漂移：补 `status` 子命令别名；统一 CLOSED=0；`evidence_sha_mismatch` →
     `evidence_sha_changed`；删除不存在的 `stale_state`；更正同 id 证据幂等语义；
     更正 AUTO-REPLAN 触发范围（`AUTO-G4*` 也计入）。
  5. 活动脚本残留 SOL 死代码：`lib_common.py` 的凭据读取函数与模型 ID 注释 → 迁入
     `legacy/lib_common_legacy.py`，活动脚本不再读取 Provider 配置；架构测试改为覆盖
     `lib_common.py` 并新增凭据读取检查。
  6. `manifests/source-baseline.sha256` 已过期（含已退役脚本）→ 用
     `sync_runtime.py --baseline` 重建为 20 个文件，`sha256sum -c` 全部通过。
  7. 新增 `tests/test_native_agent_v6.py`（`build_review` 的 G4 资格计算、非 pass 复核、
     PARTIAL→工作单、AUTO-REPLAN 升级）与 G4 门禁拒绝分支、授权绑定、`status` 别名、
     高风险后端绑定等测试。
- **未验证**：没有启动新的 ZCode GUI 会话做“会话内实际加载”观察（当前 CLI 无 headless
  入口，启动 GUI 会干扰用户桌面）；已用运行时产物静态校验 + 运行时 CLI 实测替代，二者
  不等于真实会话加载。真实原生 Executor/Reviewer 端到端也未在本批次重新运行。

## 历史阶段（v5 及以前，仅追溯）

以下内容记录 v6 之前的现场状态，涉及 SOL 的表述均为历史；不再作为当前执行依据。

### 当前阶段

- **Phase 3.6**：离线协议、校验、信封生成、异常/幂等测试与兼容性已完成；**完整 Native Bridge
  端到端（C4）仍未执行、未验证**。2026-11-08 在用户授权下完成过 **4 次真实 SOL HTTP 调用**，
  只做链路自检（字段省略、截断识别、一次升档恢复），不构成 C4 验收，也不构成完整业务闭环。
- **Phase 4（部分落地，2026-11-08）**：SOL 自动重判/收口与 PARTIAL 自动推进已实现并通过
  离线全链路测试；Reviewer 自动派发、可信复核记录升级仍未开始，需单独授权。
- **文档与代码口径（2026-11-09）**：`SKILL.md`、`references/role.md`、项目 `AGENTS.md`、
  `docs/PROJECT-CHARTER.md` 的现行文本已统一到 ChatGPT 自定义指令 v5.3.3（v5.3.2 起的批次
  已由同日后续升级取代，见下节「协作基线升级到 v5.3.3」）；历史预算与旧角色
  口径保留并加替代注，不改写当时事实。同日清理了 Python 侧会进入运行时提示的旧预算文字
  （`review_ledger.py` / `call_gpt.py` / `copilot_dispatch.py`），并把独立复核的「最多 2 轮；
  到顶即停」改为证据驱动的无固定次数上限；见下文「本轮收口」。

### 2026-11-11 批次：SOL 指令拆分与 REVIEW 历史证据投影

两项改动已完成验证、同步到运行时并随本批次提交入库（父提交 `775731b`）。

- **SOL 指令拆分**：`references/role.md` 拆为核心（32,887 B）+ 三个模式补充
  （`role-consult.md` 650 B / `role-plan.md` 1,426 B / `role-review.md` 11,699 B），
  `call_gpt.py` 按模式确定性组装为单份 `instructions`；`role-map.md` 只作 v5.0 映射、不注入。
  显式 `--role-file` 仍按单文件原样注入。实测 `instructions` Token：14,102 → CONSULT 9,146 /
  PLAN 9,359 / REVIEW 12,265。原 `role.md` 503 行非空内容逐行核对，除重写后的「判断原则」
  压缩段与改名标题外无缺失；压缩段涉及的每条硬约束已逐条回补（模拟测试档位、未验证项不得写成
  通过、不替用户放行高风险、不机械要求多轮、隔离上下文、不自行断言结果）。
- **历史证据投影**：`review_ledger.py` 渲染层把已验证 PASS 之前的未引用背景证据压成
  「编号 + 原位置 + SHA-256 + 恢复条件」记录，`PROJECTION_VERSION=1`；原始台账与各轮证据包
  字节不变，G2 复现检查不变；`--recover` 按编号 + 摘要精确恢复原文。真实第 14 轮台账（200 条 /
  680,140 字符）实测：全量包 680,488 字符 / `input_tokens` 254,663 → 投影包 158,240 字符 /
  87,731（−65.6%）；重开一条历史证据后 96,590。同台账 + 同策略 + 同版本两次装配结果一致；
  篡改证据包、篡改台账摘要、缺包均失败关闭；14 个原始包 SHA 未变，台账仍 14 轮、无投影字段写入。
  行为探针（非正式 REVIEW）：SOL 对投影条目 E16 回答「正文可见：否」「结论：不能；缺证：E16」。
- **测试与同步**：`872 tests / OK`（含 19 项投影定向测试）；`sync_runtime.py --apply` 写入 10 个
  文件，`--check` 缺失 0 / 不同 0 / Reviewer same；全新 headless 进程加载 Skill 成功并读到投影
  硬约束段与新的引用清单。
- **未验证**：没有发起新的正式 REVIEW，投影在生产轮次中的裁决质量只有离线场景与行为探针证据；
  `role.md` 压缩段的行级改写属人工合并，未逐字比对语义等价的独立复核。

## 源码与运行时

| 项目 | 实际值 |
|---|---|
| 唯一源码 | `<repo>` |
| 运行时 Skill | `~/.zcode/skills/copilot` |
| Reviewer 运行时 | `~/.zcode/agents/reviewer.md` |
| 同步工具 | `scripts-sync/sync_runtime.py` |
| 分发的 Skill 文件 | 21 个（`SKILL.md` + 9 个 `scripts/*.py` + 11 个 `references/*.md`） |
| 分发的 Reviewer 文件 | 1 个（`agents/reviewer.md`） |
| 首次同步 | 2026-11-02 20:25–20:26 CST |
| 同步前备份 | `~/.zcode/backups/copilot-presync-20261102-202527` |
| 最近同步 | 2026-11-10（自评审整改后四次 `--apply`：① `SKILL.md`、`scripts/call_gpt.py`、`scripts/copilot_auto.py`、`scripts/copilot_dispatch.py`；② R2 补同步 `references/agent-bridge.md`、`agents/reviewer.md`、`scripts/native_bridge.py`、`scripts/review_ledger.py`（该次未逐条登记，据运行时与源码逐字节一致复原）；③ `references/agent-bridge.md`、`scripts/copilot_auto.py`、`scripts/copilot_dispatch.py`、`scripts/native_agent.py`；④ `SKILL.md` 精简后再次下发。最终 `--check` 缺失 0 / 不同 0，Reviewer `same`；相对 HEAD 变更的 9 个分发文件运行时全部一致） |

首次同步结果：

- `--check`：缺失 0、不同 0、Reviewer `same`；运行时多出 5 个 `tests/*.py`（未受管理，未删除）。
- `--apply`：写入 0 个文件（源码由运行时当前版本迁移而来，迁移后已一致）。
- 写后复查：Skill 缺失 0、不同 0；Reviewer 一致。
- 独立复核：11 个分发文件 SHA-256 全部一致；Reviewer SHA-256
  `3923c2db43301d2b05096bf6456376d157a92a4097a94eb215431d4f3c58ebeb` 与源码一致。
- `.audit/copilot.jsonl` 哈希未变化（`dbbc8de7…`），未进入源码仓库。
- 仓库内无符号链接。

## 测试基线

- 源码仓库：**849 tests OK**（`python3 -B -m unittest discover -s tests -p 'test_*.py'`，
  进程退出码 0，2026-11-11 第 13 轮实测；本轮新增 18 项：容器结构/幂等、统计键同口径、
  换行分隔符与注释、代码容器可读性、长串线性度，第 12 轮后两批复核与第 13 轮正式 REVIEW
  指出项的回归）。口径：
  R1–R3 后 779，R4 增补 2 项复核文件校验用例后 781；
  F2 围栏收口 782；多轮独立复核指出项补齐后 788；第四轮独立复核指出项补齐后 790；第二轮正式
  REVIEW 整改（重定向 token 边界、schema 逐值校验）后 791；第三轮整改（重复键一律拒绝 +
  legacy 重复键用例）后 792；第四轮整改（重定向转义空白边界 + argv 保真用例）后 793；
  自评审第 2 轮整改后 811；F5 首轮整改后 812；F5-A/B 整改后 814；证据改写披露与统计键
  代码引用整改后 815；转义引号遮蔽与行数不变式整改后 817；第 5 轮正式 REVIEW 的
  F5-A/F5-E/F5-D/F5-F 整改后 820；第 6 轮 F1/F2/F3 整改后 824；第 7 轮
  `--cmd` 混合正文的报告结构整改后 825；第 8 轮最终合并包整改后 826；第 9 轮逐块结构化整改后 827；第 11 轮键名/默认分隔符/嵌套容器与判别力整改后 831；第 13 轮第一批复核整改后 846，第二批复核整改后 847，正式 REVIEW 定向整改（伪标记/字面量越界）后 849。
- 自评审前基线：**728 tests OK**（同一命令，提交 `8ebf299` 的干净检出实测）；
  其中 `tests/test_sync_runtime.py` 19 项只按一次计入。此前记录的「747」是重复计入
  该 19 项（728 + 19）的错误口径，本次以实测 728 为准。
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
  ``<maintainer>``（继承全局配置）。
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
  `~/.zcode/skills/copilot/SKILL.md`；调用 Skill 工具加载正文后引用到
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
  `~/.zcode/backups/copilot-review-elastic-20261104-132205`（含 `SHA256SUMS`）。
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

- **触发（只看历史日志的只读复盘）**：某买量投放项目 ACTIVATION 任务
  `某买量投放任务-20261104`（`/tmp/copilot/honor-v1`）。R2 E21 正文被截断
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
- **当时口径**：协作基线统一为 ChatGPT 自定义指令 **v5.3.2**（`chatgpt-v5.3.2-alignment.md`
  标注为摘要而非原文；后由 v5.3.3 升级，见下节）；补入六角色边界；删除「每轮一次 / 预算 / 配额」这类把默认观察值写成
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
    「copilot：GPT-6.1-SOL 高级工程决策通道」、存在「六角色边界（v5.3.2）」、当时对照为 v5.3.2。
    项目 `AGENTS.md` 在该子会话上下文中已注入。**注意**：这是子代理新上下文加载，**不是新的
    桌面/headless 会话**；本机没有可用的 ZCode headless CLI（`/usr/bin/zcode` 指向 Electron
    桌面应用），因此「新桌面会话加载验证」**未执行**。
- **未验证**：新桌面会话 / headless 进程级加载验证；真实 SOL 对这些文档的复核（本轮只有一次
  真实 PLAN 调用，`6eae8e768f390bbb`）。

## 协作基线升级到 v5.3.3（2026-11-09）

- **范围**：`SKILL.md`、`references/role.md`、项目 `AGENTS.md`、`docs/PROJECT-CHARTER.md`、
  `docs/DECISIONS.md`、`docs/STATUS.md`、`README.md` 与
  `~/.zcode/references/chatgpt-v5.3.3-alignment.md` 的现行基线引用与角色表述。只改文档与规则
  文本，未改业务代码、脚本、测试、状态机、模型路由、调用判据、预算实现与 G1–G4。
- **现行口径**：协作基线为 ChatGPT 自定义指令 **v5.3.3**（`chatgpt-v5.3.3-alignment.md` 为摘要，
  不是原文，也不是权限来源）。明确：ZCode 原生 Agent 是工程执行主体；Copilot 是运行于 ZCode 内的
  协作治理机制；SOL 是 Copilot 按判据调用的高级工程决策通道，不是独立执行 Agent；DesignWise 是
  运行于 ZCode 内的设计方法与质量规范 Skill；ChatGPT 负责产品与业务判断、产品层方案规划、任务交接
  与结果验收。默认闭环按 v5.3.3 表述，简单任务不强制经过 Copilot、SOL 或正式 REVIEW。
- **保留**：v5.3.2、v5.2.0、v5.1.1、v5.0 及更早的版本沿革保留为历史对照；判断原则的 30 条逐条
  映射仍以 v5.0 原文为历史粒度参考。自动推进、PARTIAL 修复、SOL 调用判据、正式 REVIEW 授权门、
  原生独立复核与 G1–G4 未变。
- **验证**：
  - 全量测试 `python3 -B -m unittest discover -s tests -p 'test_*.py'`：**Ran 728 tests, OK**
    （exit 0）。
  - 同步：`sync_runtime.py --apply` 写入 2 个 Skill 文件（`SKILL.md`、`references/role.md`），
    Reviewer 无需更新；复查 `--check` 为**缺失 0 / 不同 0 / Reviewer same**（exit 0），运行时保留
    5 个未受管理 `tests/*.py`（设计上不清理）。
  - 哈希：源与运行时 `SKILL.md` 均为 `8acf7ebb…`，`references/role.md` 均为 `bc698a55…`，
    Reviewer 两侧均为 `9ef6dccf…`。
  - 运行时文本抽查：`SKILL.md` 与 `references/role.md` 均显示「当前协作基线为 v5.3.3」，
    v5.3.2 只出现在历史对照与版本沿革段落。
- **未验证**：未发起真实 SOL 调用；未做新桌面 / headless 会话的 Skill 重新加载验证（本机无
  headless CLI，运行时副本按 SHA-256 与源码一致）。

## 维护清理（2026-11-09）

- **SKILL.md 加载余量**：迁移前 99,829 字节（距 ZCode 100,000 字节加载上限仅 171 字节，超限会静默
  截断尾部）。把「版本与历史」段（5,966 字节）原文迁到 `references/history.md`，SKILL.md 只留当前
  基线、模型基线与历史入口；同时压缩重复的 DesignWise 分工表述。修改后 **95,002 字节**，余量
  **4,998 字节**（约 4.9 KiB）；核心职责、调用判据、安全边界、SOL 规则与「版本与历史」标题均保留，
  v5.3.3 语义未变。
- **源码基线**：`manifests/source-baseline.sha256` 原记录停留在 2026-11-05（13 个文件），此后多个
  提交与工作区改动未刷新，7 个哈希与实际文件不符。用 `sync_runtime.py --baseline` 重建为
  **14 个文件**，`sha256sum -c` 全部成功；旧值保留在 Git 历史，不破坏审计。
- **CLI 阶段描述**：`loop_step` 实际有 **9 个阶段**（含 `REVIEW_PENDING`，优先于 `QUOTA_WAIT`），
  `copilot_dispatch.py` 的 docstring / `--loop-step` 帮助、`copilot_auto.py` 模块说明、SKILL.md §8
  与 `references/role.md` 第 9 条仍写「八阶段」且顺序缺 `REVIEW_PENDING`。只改文案与顺序，未改
  状态机、阶段转换或调用行为。
- **验证**：定向测试（ZCode 加载约束、版本当前/历史分离、role.md 30 条映射、`test_sync_runtime`
  全部、`AutoDocsTest`）24 项 OK；未重跑 728 项全量。同步 `--apply` 写入 5 个文件后 `--check` 为
  **缺失 0 / 不同 0 / Reviewer same**；运行时多出 5 个未受管理 `tests/*.py`（默认保留）。
- **未验证**：未做新桌面会话的 Skill 重新加载验证；未提交、未推送（按任务边界）。

## 实战复盘与失败指引补齐（2026-11-10）

- **样本**：运行时审计 `~/.zcode/skills/copilot/.audit/copilot.jsonl` 共 **144 次**
  真实 SOL 调用（2026-10-25 → 2026-11-10），**126 成功 / 18 失败（12.5%）**；`/tmp/copilot/**`
  下 **16 份**可解析的正式 REVIEW 台账；rili / lele-pedometer-xin / designwise 的 Git 提交与
  两个真实 dispatch 状态（`rili-rules-20261109`、`agents-prose-autoloop-20261108`）。
- **失败构成（均为外部原因或已修复缺陷）**：10 × 429 配额/冷却（2026-10-25 20:58:04–06 十路
  并发，账号 5 小时窗口耗尽，`resets_in_seconds≈15008`）；4 × 输出截断
  `non_success_response`（2026-11-05 → 11-08，`d2ba759` 起改为同轮升档恢复）；1 × TLS 握手
  EOF（2026-11-02 16:11）；1 × 上游 408 `stream closed`（2026-11-08 19:08:57，109,970 ms）；
  2 × 客户端 300s 读超时（2026-11-03 14:56:08、2026-11-06 18:15:59，后者 18:16:14 重发成功）。
- **复核质量**：真实台账未发现证据截断（真实轮次 `truncated_new` 全为 0）、跨任务串线或裁决
  错配；PARTIAL 均带可执行工作项并推进到 EXECUTE；16 份台账中 4 份以 PASS 收口
  （theme-v1-closure、rili-rules-20261109、zcode-global-agents-v5.3.2-r2、
  agents-prose-autoloop-20261108），1 份因真实缺证 BLOCKED（zcode-global-v5.2.0 r2，
  F3/F4 证据与能力缺口）；唯一 UNPARSED 是 2026-10-28 的旧解析器缺陷，2026-11-08 已修复并
  有回归测试（`test_last_real_review_reply_parses_blocked`）。
- **九段上下文预算够用**：两个真实 dispatch 状态的 `brief` 分别为 **10,522 / 5,582 字符**，
  都在 `MAX_RENDER_CHARS=20000` 内，未触发裁剪（含目标、条件、证据索引均完整）。
- **已否决的改动**：不上调 `--timeout` 默认值（实测 `urlopen(timeout=)` 是 socket 读超时而非
  总时长上限，同 call_id 成功耗时可达 530,471 ms）；不改 fail-fast 不重试策略；不改台账逐轮
  携带历史证据的设计。理由与证据见 `DECISIONS.md` 第 23 条与「建议（未采纳）」。
- **已实施改动（本次唯一源码改动）**：`scripts/copilot_auto.py` 的 `NEXT_STEPS["failed"]` 文案，
  由「按退出码处理，不自动重发」改为可执行恢复动作（有 pending 先按 `pending_call` 恢复；退出码 8
  等配额；3/4 且无 pending 时可显式再次 drive；7 不得重试；9 须先修正 CPA 路由再由人工决定是否
  重新发起），失败 `reason` 的「未写入任务状态」改为「本轮未入账：轮次与工作项未变（指纹已落盘）」。
  依据：2026-11-08 19:07 自动闭环第 3 轮因上游 408 返回 `failed`，实际恢复动作是 19:09 重新
  drive、19:10 收口，原文案未覆盖该动作。**未改**状态机、四门、证据协议、重试语义与模型路由。
- **成本观察（未改动）**：台账逐轮携带历史证据使单轮注入持续增长
  （rili-ad-alignment 125,907 → 315,486 字符；honor 67k → 252k 字符）。属保真代价，是否调整
  需产品侧决定，不按省 Token 单方面改。
- **执行侧浪费（非 Copilot 机制）**：2026-11-10 09:46:27 与 09:46:58 两次成功 CONSULT 同
  subject、同 6 份证据，合计约 57.8k tokens；现行「无新增证据不重复请求」规则已覆盖该场景，
  未新增机制。

## 自评审与整改（2026-11-10）

- **组织方式**：先冻结稳定基线（本地 `main` 8ebf299、运行时 4 文件 `--check` 0/0、728 tests OK），
  用稳定基线 Copilot 对自身源码组织**一轮正式 REVIEW**：17 条证据包（源码/测试/运行时/真实审计与
  台账样本，均重新校验摘要）。SOL 首轮裁决 **PARTIAL**，给出 R1–R4 整改要求与验收口径；随后由
  ZCode 原生 Agent 修复、测试与取证，未让在改的源码进入该轮评审证据。
- **已修缺陷（均已复现 → 修复 → 回归测试锁定）**：
  ① 裁决解析（否定词黑名单可被「裁决：PASS（…）」绕过 → 独立、唯一、精确三态词文法）；
  ② 四门轮次（最新 PARTIAL 仍可用旧 PASS 收口 → `latest_judgment_not_pass`，与 `pass_pending` 同源）；
  ③ 恢复 CAS（`load_state` 补默认键导致旧状态必然「被外部修改」→ 基线改取磁盘原文，审计记录两种指纹）；
  ④ 原生复核来源（调用方副本可改写裁决 → 以宿主 `metadata.outputFile` 为唯一真值并绑定
  `task_id/work_item_id/execution_result_sha256`；`files_inspected` 必须对应真实工具调用）；
  ⑤ 回填门禁（`REVIEW_PENDING/QUOTA_WAIT/BLOCKED` 下仍会先写盘 → 预检在 `_apply_manifest` 之前）；
  ⑥ 上下文预算（目标/条件 1500 字符即截断、条件段可被「其余 N 条省略」→ 保护段 6000、单条 800、
  条目永不省略、超限失败关闭；最低档压缩已终态工作项而非抛错）；
  ⑦ 真实性杂项（HTTP 200 携带配额体 → 退出 8 不重发；`--segmentation-plan` 与输入/审计路径别名；
  `include` 只接受 JSON 布尔；自动追加理由不再自称「新增证据」；本地子进程超时不重试）。
  ⑧ 已收口任务回填（`CLOSED` 时 `--result-file` 被静默忽略仍 exit 0 → 整轮拒绝并明确未写入）；
  ⑨ 恢复窗口（调用方 `load → 取基线` 之间的外部写入会被恢复覆盖 → 先取磁盘基线再重载，
  窗口内写入被读入、基线后写入仍由 CAS 拒绝）；
  ⑩ 复核文件冒领（`files_inspected` 仅按文件名匹配，`docs/report.md` 可借同名文件过关 →
  文件名与所在目录名需出现在同一调用记录，或出现完整路径）。第二轮独立复核进一步指出
  单条复合命令可拼凑（`ls docs; cat other/report.md`），已改为按 `;`/`|`/换行拆分的
  **同一命令段**内匹配，并补「复合命令拼凑必须拒绝」「相对路径读取不得误拒」两项用例。
  另修正原生复核测试资产：恢复此前被覆盖丢失的 `test_rejects_non_reviewer_profile`（断言
  改为校验「不是 reviewer」，避免删掉校验仍通过的空转），并补齐同名不同目录的冒领回归；
  `references/agent-bridge.md` 记录单围栏 JSON 容忍边界与文件校验口径；`agents/reviewer.md`
  与实现的 `/dev/null` 允许范围对齐（`2>/dev/null` 与 `>/dev/null`）。
- **未改（有证据的否决）**：`evidence` / `evidence_ids` 是 `reply_protocol` 明示的兼容别名
  （`reply-protocol/v2`），不是缺陷；G4 仍 fail-closed；状态机、证据协议、模型路由、fail-fast 与
  timeout 口径不变（见 `DECISIONS.md` 第 23、24 条）。
- **运行时验证**：`sync_runtime.py --apply` 后 `--check` 缺失 0 / 不同 0、Reviewer `same`；在新进程
  用运行时副本跑通 `start → brief → loop-step → closure-check`（九段齐全、无 PASS 时四门失败关闭，
  返回 `no_pass_round / criteria_missing / criteria_unconfirmed`），并确认运行时 `parse_verdict`
  的严格文法与 `MAX_PROTECTED_SECTION_CHARS=6000` 生效。**本轮 0 次真实 SOL 调用**。
- **独立复核（两轮，均只读新上下文）**：第一轮提出 7 项（CLOSED 回填被静默忽略、恢复
  `load→基线`窗口、`files_inspected` 同名冒领、丢失的 profile 测试、文档与 STATUS 口径冲突），
  已逐项核实并修复；第二轮聚焦增量修复，确认 CLOSED 拒绝先于写盘、CAS 两窗口方向正确、
  冒领拒绝有效、G4/裁决文法/`include`/阶段门禁未被削弱，并额外指出复合命令拼凑、恢复测试
  空转、`DECISIONS` 与 `STATUS` 数字及「严格 JSON」表述矛盾、同步记录不实，均已修复。
  两轮报告**都不能作为 G4 复核信封**：第一轮使用非白名单命令；第二轮虽未改文件
  （`changed_files=[]`、未跟踪文件 0），但取证时使用了 `sed -n`，按 Profile 规则自报为
  blocking。故它们只是独立复核意见，G4 仍 fail-closed。

## SKILL.md 精简（2026-11-10）

- **动机**：精简前 99,487 字节，距 ZCode 正文上限 100,000 只剩 513 字节；再有任何中等规模文档改动
  都会触发**静默截断头部**（加载器行为），属于真实可用性风险，与规则内容无关。
- **方法**：只删重复描述与可由既有权威文本覆盖的复述，**不外移规则、不新增机制、不改脚本行为**，
  也不动触发条件与安全约束本身。
- **五处改动**：
  ① 「六角色边界」中的逐条职责改为指向 `~/.zcode/AGENTS.md` §1 与上文「角色映射」，保留本通道
  必须显式声明的边界（Copilot 不做代码实施 / 不派原生子 Agent、SOL 输出不是授权或复核、drive 单轮
  节流不等于预算）；
  ② 原生 Bridge 只读白名单的示例矩阵（`git grep --ope`、`find -- -delete`、`git -C/-c` 等）指向
  `references/agent-bridge.md`；失败关闭类别、事后 rollout 校验与 `changed_files` 口径原样保留；
  ③ 「判断质量与收口」的 v5.3.3 基线段与调用条件段去重（v5.1.1 及更早统一归历史对照）；
  ④ 「REVIEW 收敛」台账标准日志子条目删去重复推理，保留「非标准日志拒绝新建 / 绑定后不改绑」规则；
  ⑤ 「整改期间的验证效率」删去与「评审档位与缺陷处置」重复的步骤描述，保留基线失败引用、
  既有 E 编号复用与「关键条件必须真实取证」。
- **结果**：99,487 → **98,030 字节**（−1,457），余量 **1,970 字节**；随后按独立复核意见把裁决
  契约文案与 `parse_verdict` 语义对齐（+21 字节），现行 **98,051 字节**、余量 **1,949 字节**。
- **未退化核验**：`781 tests OK`（精简时点计数；现行 793，见上节新增用例。含 `SKILL.md`
  ≤100,000 字节、`description` ≤1024 字符、
  v5.0 六条渠道条款、三档评审、独立复核无固定上限、审计必读与退出码 7 等文档一致性断言）；
  被删的命令矩阵在 `references/agent-bridge.md:221–253` 仍有完整定义；`--check` 缺失 0 / 不同 0。

## Reviewer 加固正式 REVIEW 与整改（2026-11-10）

- **评审对象与基线**：本轮 Reviewer 加固的 18 个未提交修改文件（HEAD `8ebf299`），先冻结在
  781 tests OK、`--check` 0/0、基线 14/14 的快照上再取证；评审期间不改源码。
- **首轮正式 REVIEW（PARTIAL）**：台账 `copilot-native-reviewer-upgrade-v1`，call
  `fe1b0abdac569fc0`，17 条证据。未闭合三项：
  - **F1** 未引用 Shell 通配符（`uniq /tmp/sample-*`）在 `shlex` 解析后仍是单参数，实际执行
    展开成「输入 + 输出」，可绕过只读校验覆盖文件。
  - **F2** 截断的结构化复核报告若用任意合法 JSON 转义写 schema 标识，匹配不到明文标识，
    被降级为 legacy 文本，跳过原始 verdict 一致性检查。
  - **F3** 最终版本的真实 Reviewer 链路与非 Reviewer 独立复核缺可核对原始材料。
- **整改（F1/F2 已关闭）**：F1 改为拒绝引号外一切 shell 元字符并保留引号内字面量与合规复合
  只读命令；F2 改为「声明层 + 格式边界」：整份报告是纯 JSON，或单个代码围栏包裹报告本体（信息串
  不限、收尾围栏同字符且不短于开头、之后只允许空白）；声明了 native schema 的报告在解析失败
  （截断/缺收尾/多围栏/围栏外夹散文）时拒绝且状态不变，解析后没有保留任何 native 声明（如同名
  重复键被 JSON 折叠）同样拒绝；完整围栏内的合法 native JSON 按原始 verdict 约束登记，schema
  字段名与值都忽略大小写与首尾空白，BOM 前缀在解析前剥离。均带回归测试，现行
  **793 tests OK**（详见 `DECISIONS.md` 第 25 条）。
- **独立复核（非 G4；一次定向预检 + 五份只读复核报告，按落盘文件编号）**：
  - 预检（定向，未单独落盘）：核对 F2 围栏判定、F1 元字符范围与 Reviewer 只读 Profile，三项均成立。
  - `independent-review-1.md`（完整 A–E 清单）裁决 **PARTIAL**，7 项未闭合；其中围栏内完整 native
    JSON 被误拒、BOM 前缀被误拒、错误信息把「声明 schema」误称「JSON 形态」、`DECISIONS` 对「散文
    报告」的表述过强、裁决契约文案与 `parse_verdict` 语义不一致，均已修正。
  - `independent-review-2.md`（复验 4 项声明修复 + 3 项残余）**PARTIAL**：指出 `Schema`/`SCHEMA`
    大写键在解析成功后只认精确小写键（可把 `blocked` 登记成 `pass`）、非标准围栏信息串
    （`` ```json extra ``）的截断报告不被识别、闭栏后空行被过度拒绝、闭栏宽度/字符未校验。
  - `independent-review-3.md`（复验 ①②③）**PARTIAL**：指出重复 schema 键只认第一个（大写键声明的
    native schema 被忽略）、围栏前后夹散文或围栏字符混用时转义声明被降级为 legacy、tab 缩进裁决行
    仍被接受。已按「格式边界 + 声明层（转义解码、全量同名键、引号内空白）」统一修复，并补
    `~~~`/四反引号/信息串/空行/大写键/位置用例。
  - `independent-review-4.md`（delta 复验）**PARTIAL**：指出**完全同名**重复键（`{"schema": native,
    "schema": "junk"}`，`json.loads` 折叠为后者）在解析后没有保留声明时直接返回 None；带首尾空白的
    schema 键（`"schema "`）在声明层识别不到；另指出 `DECISIONS` 的「不能靠重复键降级」承诺未实现、
    `STATUS` 的「多围栏拒绝」缺少「声明了 native schema」限定。已实现解析后声明层兜底拒绝、声明层
    字段名允许引号内空白，并同步修正两处文档措辞、补同名重复键与空白键名用例。该轮另确认 A/C/D/F
    成立、无新增自动派发与越界改动。
  - `independent-review-5.md`（收口前 delta 复验）裁决 **PASS**：第 4 份报告两项 fail-open 的修复均
    实测生效（同名重复键 pass/blocked 两种登记都被拒绝、junk-first 对照组仍按 native 约束、空白键名
    截断拒绝且合法空白键按原 verdict 登记），未发现新 fail-open，790 tests OK、18 文件、`--check`
    0/0、基线 14/14。非阻塞观察：兜底分支错误信息对「schema 自报其它值但正文出现标识」诊断不准
    （已改为不断定单一成因的措辞）；散文引用带引号声明仍拒（fail-closed，与既有口径一致）。
  - 未修（记录为残余风险）：rollout 行 `sessionId` 为空仍被容忍（`native_agent.py:231`，真实 rollout
    均带 `sessionId`，未测到利用路径）；`git -C <dir>` 实测可触发目标仓库的 `core.fsmonitor` 脚本
    （`cd <dir>; git status` 同样可达，属 git 自身行为，本次不封堵，需沙箱才能消除）；长选项前缀缩写
    按命令家族登记；`changed_files` 仍是 Reviewer 自报口径（既有边界，未改）。
- **第二轮正式 REVIEW（BLOCKED）与整改**：同台账第 2 轮，call `d5eceae51f98ae65`，21 条新证据
  （33 条合并，208,818 字符），裁决 **BLOCKED**。未闭合三项：
  - **F1（代码）** `/dev/null` 重定向在引号解析前用正则整体替换，`sort -k2>/dev/null -o out in`
    会被改成 `sort -k -o out in`，真实 argv 的 `-o` 被当成 `-k` 的参数值跳过。已改为**只在 token
    边界**消费重定向（`>` 前必须段首/空白，`2>` 的 `2` 前亦然），紧贴参数的写法直接拒绝，不再
    改写命令文本。
  - **F2（代码）** 多个 schema 声明用「最后一个 native 值」决定，`"…/v1 extra"` 可被后续合法声明
    掩盖（顺序相关）。已改为逐值校验：任一含标识的字符串必须精确等于该标识，任一非字符串值含标识
    即拒绝，与键序无关。
  - **F3（缺证/授权）** 最终版本真实 Reviewer 链路（metadata、完整 rollout、调用计数、信封生成与
    接纳记录）仍未提供。SOL 明确这不因「已声明外部前提」自动降为非阻塞，需用户决定：授权最小
    必要的真实绑定调用，或明确调整本次完成条件。
  非阻塞观察已处理：统一 `git -C` fsmonitor 口径（实测可触发）、把独立复核改为按落盘文件编号。
- **F3 已按用户授权补齐（2026-11-11）**：用户选择「授权最小真实链路验证」。真实执行链为
  PLAN（SOL 1 次，`b1bd513bc390ef46`，审计 `call_requested`/`call_result` 各一条、`http 200`、
  `reply_sha256` 与 dispatch 登记值相同）→ 工作项 T1（`d-b4acbe7248864a22ad135832`）→
  Executor 子代理（`agent_4d5a76ce…`，59 次调用）→ `native_agent.py review` 生成
  `copilot-native-review/v1` 信封（`verdict=pass`、`g4_eligible=false`、`result_sha256=a9b036e1…`）
  → `copilot_auto.py ingest` 接纳（`native_reviews` 1 条）。Reviewer 用 `agent_73ba505e…`
  （`reviewer` Profile、16 次宿主记录调用、模型取自 rollout）。前 3 次 Reviewer 运行被
  fail-closed 拒绝（`echo`/`sed` 不在只读白名单、同 agent 续跑导致工具计数不符、findings 路径
  不存在），未登记任何信封；没有修改 Reviewer 输出或手填绑定。
  - **执行偏离（如实记录，不追认为合规）**：T1 的 dispatch `goal` 明写「不导入或执行测试代码」
    并把实测留给 T2，但合同完成条件要求记录 `Ran N` / `OK` 行，执行方据此运行了一次全量测试。
    该动作**没有**覆盖它的既有授权，属计划内的边界冲突导致的偏离；副作用经前后全仓 SHA-256、
    mtime 与 `git status` 比对为空，但不改变「越界执行」的性质。
- **第三轮正式 REVIEW（PARTIAL）与整改**：同台账第 3 轮，24 条新证据（62,581 字符），裁决
  **PARTIAL**。未闭合三项及处理：
  - **F1（缺证）** 上轮只内联了正则与 argv 检查，未给实际消费重定向的 `_split_readonly_segments`；
    要求补完整实现、测试正文，并用公开入口展示边界矩阵。已在第 4 轮证据补齐（完整函数、测试、
    `validate_review_commands` 实际判定矩阵）。
  - **F2（代码，已修）** 完全同名重复键折叠仍可掩盖非法声明
    （`{"schema": "…/v1 extra", "schema": "…/v1"}`）。现登记前用 `object_pairs_hook` 保留原始
    键值对，任意层级重复键一律失败关闭（`verdict` 重复同样拒绝，legacy JSON 亦适用），补 4 个
    反例与 792 项回归。
  - **F3（缺证）** 主体绑定成立，要求离线补取 PLAN 原始来源与审计、Reviewer 完整调用记录与
    调用 ID、宿主原始 stdout、信封生成与接纳的命令/输出/退出码。已在第 4 轮证据补齐（未新增
    PLAN 或 Reviewer 派发；信封重生成与登记文件逐字节相同，ingest 在状态副本上验证幂等后重跑，
    `native_reviews` 仍为 1）。
  - **F4（缺证）** 最终指纹上的 `--check` 与基线需重跑：已重跑，缺失 0 / 不同 0、基线 14/14、
    全量 792 OK。
- **第四轮正式 REVIEW（PARTIAL）与整改**：同台账第 4 轮，27 条新证据，裁决 **PARTIAL**。未闭合
  四项及处理：
  - **F1（代码，已修）** 转义空白被误当 token 边界：`cat /tmp/input\ 2>/dev/null` 中空格被反斜杠
    转义，shell 词法上 `2` 属于普通参数 `/tmp/input 2`，旧实现凭 `buf[-2].isspace()` 判为 fd 2 并
    `pop()`，删除了真实参数字符。现以连续反斜杠奇偶判定空白是否被转义，被转义时只消费重定向本体、
    保留 `2`；`a\\ 2>`（裸空格）仍按 fd 处理，紧贴参数与非法目标继续失败关闭。回归新增 argv
    保真用例（与 bash 实测 argv 逐项比对）与 2 个拒绝用例。
  - **F2（来源，已解释）** PLAN 审计 `reply_sha256=36af7dbc…` 与 `round-1-reply.md`
    `746aa618…` 并非矛盾：审计摘要计算的是 `extract_text()` 返回值，该函数对拼接结果
    `.strip()`；`--out` 落盘写 `text + "\n"`。文件 4,722 字节，去掉唯一尾部 LF 后为 4,721 字节，
    摘要恰为 `36af7dbc…`（`reply_chars=2,299` 是字符数，非字节数）。未改动任何历史记录。
  - **F3（缺证，已补）** rollout 第 16 次调用曾被截断在 `<repo>`：本轮按未截断原文
    导出全部 16 次调用（含 `call_id` 与完整命令）。信封重生成、`cmp`、`ingest` 均给出完整 argv、
    工作目录与独立退出码（0/0/0）；`ingest` 在状态副本上重放得 `duplicate=true`，副本摘要前后
    一致，`native_reviews` 仍为 1。
  - **F4（验证矛盾，已澄清）** 上轮 `TESTS_SUMMARY` 的退出码 1 来自末尾 `grep -c 'Traceback'`
    零命中，不是 unittest 失败：本轮分开记录测试进程退出码（0）、`Ran 793 tests` / `OK` 与
    Traceback 计数（0）。E68 中 `before` 属上一条测试方法的局部变量（`test_schema_declaration_
    cannot_hide_in_odd_shapes` 第 328 行定义、第 385 行使用），`test_multiple_valid_schema_
    declarations_stay_native` 本身不含该断言——上轮证据按行号切片跨过了方法边界，本轮按完整
    方法正文呈现。最终 `--check` 原始输出与退出码随本轮证据提交。
- **第五轮正式 REVIEW（PARTIAL）与整改**：同台账第 5 轮。该轮确认来源摘要、过程记录、测试
  作用域与最终同步补证均已闭合，仅剩一项：`str.isspace()` 把 U+00A0、U+000B 等**非 Shell 空白**
  当作 token 边界，仍会删除普通参数字符 `2`。已把边界判定、转义判定与分段裁剪统一到
  `_SHELL_SPACE`（只有空格与制表符），非 Shell 空白紧贴重定向按「紧贴参数」从严拒绝；新增
  U+00A0/U+000B 的允许（argv 与 bash 逐项一致、字符保留）与拒绝用例，全量 793 项通过。
- **第六轮正式 REVIEW（PASS）**：同台账第 6 轮，15 条证据（27,085 字符）。裁决 **PASS**：
  「R5-LEX 已闭合，无剩余阻塞发现」，此前已确认的来源摘要、过程记录、测试作用域与 SKILL 承载沿用。
  非阻塞限制保持不变：命令校验是事后检测而非执行前沙箱；`changed_files` 仍是 Reviewer 自报；
  NUL 的传输与运行行为未由该轮矩阵验证。该裁决是隔离证据裁决，**不等于 G4 满足**，也不等于
  任务状态 CLOSED；本轮未提交、未推送。
- **F3 保留为已声明缺口（历史）**：生成 `copilot-native-review/v1` 信封需要真实 native dispatch 的
  `plan_call_id` / `plan_reply_sha256` / `work_item_id` 绑定；第 1–2 轮授权不含该调用，因此当时
  不伪造绑定。第 3 轮前用户已明确授权最小真实链路，上述缺口按该授权补齐。
- **运行时**：`--apply` 后 `--check` 缺失 0 / 不同 0、Reviewer `same`、基线 14/14；运行时
  `SKILL.md` 与源码同摘要（98,051 字节、余量 1,949），frontmatter、十二个二级标题、尾段完整，
  新进程探针确认 `MAX_PROTECTED_SECTION_CHARS=6000` 与严格裁决文法生效。

## 自评审第 2 轮：Skill 自身的正式 REVIEW 与整改（2026-11-11）

- **评审对象与基线**：Copilot Skill 源码本身，基线 `d60be34`（`main`，工作区已有 9 个未提交
  修改文件）。评审经真实 Copilot 入口执行：台账
  `.audit/review-ledger-copilot-skill-self-review-20261111.json`，subject
  `copilot-skill-self-review-20261111`，风险档 normal，标准审计日志
  `~/.zcode/skills/copilot/.audit/copilot.jsonl`。取证用冻结基线（`git archive d60be34`）与
  工作树跑同一组差分探针（`/tmp/copilot-repro/repro.py`、`repro2.py`），同进程内比较，避免
  「看代码猜行为」。评审期间不提交、不推送。
- **第 1 轮正式 REVIEW（PARTIAL）**：证据包 18 条（43,849 字符），指出四项：
  - **F1** `files_inspected` 绑定可被同名文件冒领：声明 `/repo-a/docs/report.md` 时
    `cat /docs/report.md` 与 `cat /foreign/repo-a/docs/report.md` 都判为「已检查」，而
    `cat ./docs/report.md` 反被拒绝。
  - **F2** 无 `save_seq` 的旧格式状态可在未传 `expect` 时覆盖他人写入，已关闭任务可被旧快照复活。
  - **F3** 截断升档拿「上次增长后的基线」比较，固定上限附近的抖动会无限升档。
  - **F4** `--timeout 1e300` 为有限正数但不可表示，异常发生在 pending/审计写入之后，未验证。
- **第 2 轮正式 REVIEW（PARTIAL）**：11 条新证据（57,723 字符）。F2/F3/F4 判为闭合；F1 仍有
  残余：绝对声明可由 `/docs/report.md`、`/foreign/repo-a/docs/report.md` 冒领，`./docs/report.md`
  被误拒；另有非阻塞项——`best` 是「上次增长后的基线」而非严格历史最高值。
- **整改（源码，均带回归）**：
  - `native_agent.py`：`_mentions_path` 改为按路径组件边界匹配并保留绝对/相对属性（绝对声明
    要求组件完全相同；相对声明允许被绝对/相对读取以它结尾；`.` 归一无害、`..` 一律不算命中），
    `_assert_files_inspected` 文档同步。15 行判定矩阵（含 4 个绝对/相对与 `./`/`..` 用例）全部
    符合预期，信封层 5 个调用样例的接受/拒绝与预期一致。
  - `copilot_dispatch.py`：`save_state` 对已存在状态默认以快照 `save_seq` 做 CAS（缺省按 0），
    新建路径仍允许无基线写入；相同内容不推进序号；`start` 以「文件不存在」为前提。旧格式快照在
    他人写入后与任务关闭后都被拒绝，磁盘上证据与收口保持不变（SAFE）。
  - `call_gpt.py`：截断升档改按历史最高输出比较（10% 抖动余量），连续两次未突破即
    `output_truncated_exhausted` + 分段计划，不再升档；真实增长仍升档。`--timeout` 出站前用
    socket 探测可表示性，`1e300` 以退出码 2 拒绝且 `audit_records=0`，30s 正常完成。
  - 附带：审计追加补尾行换行；`copilot_auto.py` 在 `loop_step(persist=True)` 后重读状态；
    证据脱敏按白名单放行计数键的数值/布尔/空值，`lambda` 赋值按语句边界只遮蔽函数体内的
    引号字面量。
- **第 3 轮正式 REVIEW（PARTIAL）**：16 条新证据（82,946 字符；合并后 45 条、208,661 字符
  prompt）。F1 残余与「历史最高输出」非阻塞项判为闭合，F2/F3/F4 未被破坏；新指出 **F5**：
  脱敏按 `lambda` 前缀与任意 `*_tokens` 后缀整体豁免，`api_key = lambda: "<真实凭据>"` 与
  `access_tokens = 12345678901234567890` 会原样带出。整改：计数豁免从「任意 `*_tokens`
  后缀」收窄到明确统计键白名单；`lambda` 赋值只遮蔽函数体内的字面量。
- **第 4 轮正式 REVIEW（PARTIAL）**：12 条新证据（44,184 字符）。统计白名单方向获认可，
  F5 仍有三项：**F5-A** lambda 分支用 `lambda\b[^\n]*` 吞掉整行，`…; auth_tokens = 987654321`
  不再进入遮蔽；**F5-B** 幂等豁免按前缀/后缀判断，`"<redacted:12>夹带凭据>"` 整体放行；
  **F5-C** 探针与结果被证据桥再次脱敏，`NO_LEAK` 与布尔值不可核对。整改：
  - lambda 处理改为**语句边界**扫描（引号外 `;`/换行/注释；括号内换行不截断），`_ASSIGN`
    只放行 `lambda` 关键字本身，后续语句照常逐条遮蔽；
  - 幂等豁免只接受完整 `^<redacted:\d+>$`（`_mask_literal` 与带引号值的表达式豁免同口径）；
  - 探针改为不重印敏感赋值的表示：合成 fixture 单独列出、输入以 `<FIXTURE>` 占位给出形状、
    输出为真实脱敏结果、`leaked/masked/idempotent/ok` 为运行时布尔；基线 `d60be34` 与现行
    工作树跑同一脚本对照（基线 lambda 字面量泄露 5 例、现行 0 例，`all_ok=true`）。
- **第 5 轮正式 REVIEW（PARTIAL）**：18 条新证据（45,346 字符；合并后 63 条、305,292 字符
  prompt，`--extra-round-basis security` 接纳）。裁决指出四项：
  - **F5-A** 剩余词法缺口：注释里的撇号（`# don't`）、三引号字面量、跨行表达式仍可能让语句
    边界扫描提前收手，函数体字面量漏遮。
  - **F5-E** JSON 里嵌套转义引号（`\"AAA\\\"`）按第一个 `\"` 配对，取值被截断，尾段原样带出。
  - **F5-D** 披露坐标与裁剪不一致：条目被 `clip` 后行号仍按裁剪前计，但披露措辞没有说明，
    读者会按可见正文核对而核对不上。
  - **F5-F** 证据缺失：`tests` 条目为空（dash 下 `set -o pipefail` 失败且只收 stdout）、
    无 SKILL.md 相对上轮 diff、探针命令与退出码未随包。
- **整改（对应 F5-A/E/D/F，均带回归）**：
  - `lib_common.py`：语句边界扫描改为三引号/注释/括号感知、普通字符串不得跨行、转义引号只影响
    引号内；新增 `_mask_literals_in` 扫描器（三引号、未闭合引号到行尾、`#` 区段内成对字面量），
    字面量遮蔽保留换行个数；`_ASSIGN` 的转义取值分支改贪婪、取本行最后一处配对。
  - `bridge_evidence.py`：披露行按「采集正文行号」给出，条目被裁剪时标注「裁剪前」并写入
    `--manifest.redacted_lines_coordinate`；`collect_cmd` 同时收 stdout 与 stderr（只收 stdout
    会把写 stderr 的命令记成 0B 假空证据）。
  - 证据命令改成显式捕获退出码（`… > log 2>&1; rc=$?; …; exit $rc`），不再依赖 `pipefail`。
- **验证**：全量 **820 tests OK**（退出码 0，86.2s）；18 行差分探针在现行工作树 `all_ok=true`
  （基线 `d60be34` 冻结检出 11 行泄露、`all_ok=false`）；披露探针 `f5_disclosure.py` 在头/中段/
  尾部各埋一处凭据并强制裁剪，`covers_all_three=true`、披露行 `第 6, 17, 38 行（裁剪前采集正文
  行号）`、`coordinate=pre-clip`、无泄露、脱敏后行数不变。详见 `DECISIONS.md` 第 26 条。
- **第 6 轮正式 REVIEW（PARTIAL）**：22 条新证据（72,749 字符；合并后 92 条、305,292→388,269
  字符 prompt，`--extra-round-basis remediation` 接纳）。F5-D 判为**闭合**；F5-A/F5-E/F5-F
  **部分闭合**，新指出三项：
  - **F1** 显式续行（`api_key = lambda: \` 换行后写字面量）在续行处截断语句，下一行字面量漏遮。
  - **F2** 探针报告经证据桥二次脱敏后不再是合法 JSON：`\"` 被当成字符串定界符，结束转义引号
    被一起吞掉；后续行的键（`output`）与判定布尔被连带改写。
  - **F3** 实际执行的命令未入包（条目标题只是标签）；`SKILL.md` 的 diff 左侧是更早的运行时副本
    （日期 11-10），未绑定上一轮版本（E70 指纹 `2c045054…`）。
- **第 6 轮整改（对应 F1/F2/F3，均带回归）**：
  - `lib_common._statement_end`：`\` + 换行按续行跨过去继续扫描；普通字符串在行尾未闭合时
    **就在本行结束语句**，不再越过换行把下一行的 JSON 键/后续语句当函数体遮蔽。
  - `lib_common.redact` 增加结构化路径：整份文本能由同一套标准序列化参数逐字节还原时按 JSON
    结构处理（值逐项脱敏、凭据键下标量整体遮蔽、白名单统计键数值放行、键与结构原样保留），
    结果必须仍可解析且行数不变，否则回退文本路径；嵌套 JSON（字符串里的 JSON）按深度上限递归。
  - `bridge_evidence.collect_cmd`：正文首行写入实际执行的命令（压成单行，避免多行标签伪造条目
    边界），退出码仍在条目标题与正文中。
- **第 6 轮验证**：全量 **824 tests OK**（退出码 0，90.4s）；21 行差分探针现行 `all_ok=true`、
  报告 `report_still_parses=true`/`report_fields_intact=true`/行数稳定，基线 14 行泄露且报告
  不可解析；`SKILL.md` 上一轮快照按编辑链逆替换重建（`2c045054…`，与 E70 一致），diff 两端
  指纹均可复核。
- **第 7 轮正式 REVIEW（PARTIAL）**：23 条新证据（69,549 字符；合并后 114 条、460,991 字符
  prompt，`--extra-round-basis remediation` 接纳）。**F1、F3 闭合**，结构化路径的指定标量回归
  闭合；**F2 部分闭合**：纯 JSON 报告经真实 `--file` 入口已可解析（`all_good=true`，基线对照
  `parsed=false`），但 `--cmd` 条目是「`$ 命令` + 报告 + `EXIT=0`」的混合正文，整份判定不成立、
  回退词法路径后报告再次被破坏（`output` 与判定布尔被替换）。
- **第 7 轮整改（F2 剩余入口）**：`lib_common` 新增 `_redact_embedded_json`——整份判定失败时，
  在正文中找行首 `{`/`[` 起的可解析 JSON 文档（前 32 个候选，取最长），只对该文档按结构脱敏，
  前后包装（命令、退出码、stderr 段）按文本路径处理；结果须行数不变，否则回退。新增回归
  `test_cmd_wrapped_json_report_keeps_structure`；端到端脚本按 `--file` 与 `--cmd` 两个入口分别
  从真实桥生成的包里提取报告、解析并核对键与判定布尔。
- **第 7 轮验证**：全量 **825 tests OK**（退出码 0，87.6s）；`--cmd` 入口现场核对
  `command_recorded=true`、`exit_line_recorded=true`、`parsed=true`、`rows=21`、
  `report_still_parses=true`、`report_fields_intact=true`、`row_ok_values_preserved=true`、
  `all_good=true`；基线同一入口 `rows=0`、`all_good=false`。
- **第 8 轮正式 REVIEW（PARTIAL）**：17 条新证据（42,770 字符；合并后 130 条、473,115 字符
  prompt，`--extra-round-basis remediation` 接纳）。**最小 `--cmd` 入包链路闭合**，基线对照、
  纯文件入口、命令/退出行可追溯闭合；**新发现**：最终合并包（130 条、473k 字符）经发送前
  最后一道 `redact(prompt)` 后，报告再次不可解析、检测键被替换——**损坏点不在单条入包，
  而在合并文本的最后一道脱敏**。
- **第 8 轮整改（最终合并包）**：根因是 `_redact_embedded_json` 按「取最长块 + 对左右递归」
  实现，递归时 `depth + 1`，同一份文本里第 8 个块之后撞上 `_depth < 8` 退回词法路径。改为
  **逐块扫描**：块互为兄弟、间隙各自从同一层脱敏；单个块不可逐字节还原时只让该块退词法
  路径（`_redact_lexical`），不连累同文本其它块。新增回归
  `test_many_embedded_json_blocks_all_keep_structure`（24 块）与
  `test_noncanonical_block_does_not_disable_other_blocks`。
- **第 8 轮整改验证**：全量 **826 tests OK**（退出码 0，84.3s）。按真实链路复现第 8 轮合并文本
  （历史轮次 + 当轮包，`merged_chars=473115`，与台账记录的 473,422 同量级差异来自当轮包重建），
  再执行发送前脱敏：现行树 `newly_broken=0`、`report_blocks_intact` 保持、行数稳定、幂等；
  修复前快照与基线 `d60be34` 均为 `newly_broken=1`（同一份报告被改成不可解析）。
- **第 9 轮正式 REVIEW（PARTIAL）**：16 条新证据（32,977 字符；合并后 144 条、542,988 字符
  prompt，`--extra-round-basis remediation` 接纳）。最终合并包上的**新增结构损坏已闭合**
  （现行 `newly_broken=0`，修复前快照与基线均 `newly_broken=1`）；仍未闭合的是**逐项保真
  比较**：此前只校验 21 行与 `ok` 全真，没有逐行比对全部字段的存在性/类型/值，也没有比较
  报告块自身行数。
- **第 9 轮整改（逐项保真比较）**：合并包检查器改为对原本完整的报告块做快照—比较：行身份
  （`case` 顺序）、每行全部字段的存在性/类型/值、报告级检测键、报告块物理行数，另查总行数
  不变与幂等。复现文本 `merged_chars=473422`（与台账记录一致）。
- **第 9 轮验证**：全量 **827 tests OK**（退出码 0，90.8s）。同一合并输入上：现行树 21 行
  逐项一致、块行数 302→302、幂等、总行数稳定；修复前快照与基线 `d60be34` 的报告块脱敏后
  直接不可解析（`report_unparsable_after=true`）。
- **第 10 轮正式 REVIEW（PASS）**：10 条新证据（24,254 字符；合并后 154 条、569,986 字符
  prompt，`--extra-round-basis remediation` 接纳）。**逐项保真比较闭合**：同一合并输入
  （`merged_chars=473422`）上 21 行身份、231 个字段（存在性/类型/值）、4 个检测键逐项一致，
  报告块 302→302 行，总行数稳定、幂等；对照组（修复前快照、基线 `d60be34`）的报告块脱敏后
  不可解析，说明判据有区分力。裁决原文：**未闭合的关键项：无；下一步：STOP**。
  非阻塞限制：结论只覆盖该指定合并输入与命令锚点；历史坏报告不自愈；检查器未提交覆盖任意
  字段变异/报告增删的完整测试集；脱敏器既有边界与本地证据性质同前。
- **第 11 轮（独立只读复核后的整改，1 blocking + 3 major + 1 minor）**：ZCode 原生 Reviewer 在
  独立上下文复核同一批未提交改动（明确声明不是 native dispatch、`g4_eligible=false`）。逐项
  现场探针复核后确认成立并修复：
  - **blocking（本轮引入的回归）**：结构化路径只处理值，`{"sk-ABCDEFGH12345678": "v"}` 原样
    出站；基线纯词法对任意位置的 token 都遮蔽，属新泄漏。新增 `_mask_json_key`：键名命中
    `_TOKEN_PATTERNS` 时替换为 `<redacted:N>`。
  - **major**：`_JSON_STYLES` 缺 `json.dumps` 默认分隔符（`, ` / `: `），审计 JSONL 与临时脚本
    打印的行因此退词法路径、检测键再次被改坏（正是第 8 轮症状）。补 `ensure_ascii` 真/假两套
    无缩进风格。
  - **major**：第 8 轮两个新回归用例无区分力——把 `redact` 整体替换为 `_redact_lexical` 时仍
    全绿。改为逐字符断言掩码形态（`'api_key = lambda: ("<redacted:13>")'`），并新增键名、
    默认分隔符、嵌套容器三条用例；同一替换下现为 5 条红。
  - **major**：凭据键下的嵌套容器两条路径都不遮蔽（`{"api_key": {"v": "…"}}`）。结构化路径改为
    凭据键下逐叶子遮蔽、容器与行数保留；词法路径的同类缺口仍在，记入残余风险。
  - **minor（证据可核性）**：`secret_free: true` 这类**检测结果字段**被当成凭据键下的标量
    遮成 `<redacted:4>`（布尔不可能承载凭据，遮掉之后复核读不出判定）。两条路径都放行
    布尔与空值，新增回归用例。
  - **minor/口径**：结构化路径的字符串值改为「引号不配平时按首尾引号整体遮蔽」（解析已丢失
    字面量边界信息，逐段遮蔽会在第一个内部引号处收手）；`SKILL.md`/`_redact_json_document` 不再
    声称「宁可退回词法也不交付改坏的证据」，并写明受保护的标准序列化风格；第 8 轮 473,115 与
    台账 473,422 的差异统一解释为复现时重复来源累积差异。
- **第 11 轮验证**：全量 **831 tests OK**（退出码 0，85.0s，日志 sha256 `0892a466…`）；三向对照
  （现行 / 修复前快照 / 基线 `d60be34`）在 `merged_chars=473422` 上仍为 `all_ok` 真 / 假 / 假，
  报告块 21 行、231 字段、4 检测键逐项一致、302→302 行、幂等；判别力对照（`redact` 换成
  `_redact_lexical`）9 条用例中 5 条失败，正常路径 9/9 通过；`--cmd` 入口端到端（默认分隔符报告
  + lambda 字面量）发送前脱敏后无凭据残留且 `"all_ok": true` 保留；性能：1.7MB/154 块 1.27s，
  55k 病态输入 0.08s。仍未提交、未推送。
- **第 11 轮正式 REVIEW（PARTIAL）**：call `e41c37cc091e37b7`，16 条新证据（合并后
  559,109 字符 prompt，`--extra-round-basis remediation` 接纳）。已闭合：键名遮蔽、默认分隔符
  JSON、合并包保真、判别力、测试与现场；**唯一未闭合项**是 `R11-NESTED-LEAF-EVIDENCE`——
  探针只打印顶层键与统计键，无法排除「整棵容器子树被换成一个标记」。属**证据不足**，不是实现
  缺陷；未改产品代码。
- **第 12 轮正式 REVIEW（PASS）**：call `05cc0517d0d9baaa`，6 条聚焦证据（合并后 565,606 字符
  prompt）。补 `nested_assert.py` 逐条断言（12/12）：`api_key` 仍是对象且键为 `nested`、
  `api_key.nested` 是合法标记、`access_token` 仍是长度 2 的数组且 `[0]` 为对象 `.v` 为标记、
  `[1]` 为标记、`max_output_tokens` 仍为 8192、无凭据残留、物理行数不变、幂等；基线 `d60be34`
  同输入在解析阶段即失败（`EXIT=1`）。裁决原文：**未闭合关键项：无；下一步：STOP**。
  非阻塞限制：`source` 展示字段被二次脱敏（按 E169 原输入与 checks 判断）；基线只证明检查能
  拒绝，未逐项测得基线 12 项失败；结论限定于该固定输入与绑定模块。
- **第 11 轮的残余风险已在第 13 轮闭合**：词法路径（非规范排版的 JSON/JSON5/Python repr）对
  凭据键下嵌套容器已逐叶子遮蔽——端到端可达（`--cmd` → `_redact_with_lines` → 词法路径；
  `call_gpt.py` 出站前 `redact(prompt)`）；`Bearer\s+` 跨行匹配改为保留换行个数，行数不变式
  成立。**两轮独立只读复核后的整改**（2026-11-11，均非 G4）：
  - 第 1 轮：键与行首容器之间夹注释/空行（`api_key:  # 说明\n{…}`）曾把块当独立 JSON
    文档而漏遮；YAML flow 裸标量、未闭合 `/*`、整除 `//`、裸值尾部结构亦有缺口。
  - 第 2 轮（F-01…F-12）：块前回看窗口只有 8 行（9 行以上空行、多行块注释、超长间隔都漏）；
    行尾未闭合 `/*` 不认作键头注释；`// … # …` 先按 `#` 截断而读不到键头；值位上的
    `hunter2:x` 被当键名放行；裸标量里的 `#` 被当注释、吞掉同一行后续键值；凭据键下的 URL
    整体保留；`--cmd` 披露文案把「裸标识符按代码处理」写成通用口径；STATUS 计数滞后。
  - 修法：块前回看改为逐段剥离空白/行注释/块注释（窗口 4096 段，耗尽 fail-closed）；行首块
    按「凭据键之下」直接走结构化路径（不再受词法容器的间隔上限约束）；容器扫描用
    `expect_key` 判键位（值位上的冒号不再当键名）、`#` 只在行首/空白/分隔符之后算注释、
    凭据键下的 URL 按数据标量遮蔽；未闭合 `/*` 在键与容器之间按行注释跨过（跳过 `\n` 本身）。
    846→847 tests OK，变异矩阵 12 条用例逐条可红（`never-red: none`）。
  - 第 13 轮正式 REVIEW（第 12 轮之后的新裁决，PARTIAL，不改写第 12 轮）的三项 findings：
    F1 容器扫描只凭 `<redacted:` 前缀放行，伪标记（`<redacted:SYN-MARK-13>`、带载荷）连同
    秘密原样出站；F2 键头写在**字符串字面量内部**（证据包里的测试源码片段）时扫描越过字面量
    扫到远处闭括号，把后续条目连元数据（采集行段/大小/行数/sha256）整段吞进遮蔽区间；
    F3 「8 例 `--cmd` 端到端」只是脚本内构造正文，缺真实桥 CLI 入口证据。
  - 修法：标记分支改为 `_is_redacted_marker` 严格判定（只放行 `<redacted:数字>`，伪标记按数据
    标量遮蔽且行数不变）；新增 `_enclosing_string`，键头落在字面量内时只在字面量内容里递归
    脱敏（`redact(inner, depth+1)`，深度 8 后整段遮蔽），`depth` 贯穿 `_redact_lexical` /
    `_mask_sensitive_containers`；F3 改用真实 `bridge_evidence.py --cmd` 采集 8 份夹具并解析
    生成包逐例核对。849 tests OK，变异矩阵 14 条用例逐条可红（新增「恢复前缀放行」与
    「关掉字面量边界」各只红对应 1 条），探针 42 例现行全通过、基线 38 例不合格。
  剩余边界：容器含代码关键字或字面量在括号内时按代码处理、不遮蔽（`re.compile(r"…")`、
  `[t for t in …]`、`{"api_key": {"v": parse("…")}}`；这类内容是代码不是字面量，遮掉会让证据
  不可复核）；凭据键下无键值的裸标识符集合（`tokens = {a, b}`）也按数据遮蔽（宁可多遮）；
  注释正文按成对字面量遮蔽，注释里未加引号的裸词不遮蔽；键的同一行已有完整值时，其后独立
  行首的 JSON 块按独立文档处理（普通报告不会被整块遮掉）；双层转义形态
  `{\"api_key\": {\"v\": …}}` 不触发容器扫描（记号规则与 `_ASSIGN` 仍生效）；不保证整体仍可解析。
- **未修（记录为残余风险，不与本轮目标冲突）**：legacy `ingest --result-file` 对 failed/blocked
  结果非幂等；`drive` 在 `--out` 写失败时丢弃回复 stdout；`ingest` 的阶段门比 `drive` 弱；
  无裁决的 REVIEW 仍以 `ok:true` 退出 0；`dispatch_plan_rejected` 存在「先保存后审计」的崩溃
  窗口；符号链接 `--state` 会破坏锁身份；`record --status done` 接受空结果；敏感键的非 lambda
  取值表达式（如 `token = get_token("字面量")`）沿用表达式豁免，字面量不遮蔽（既有边界，
  遮蔽环境变量名会降低证据可读性，本轮未改）；结构化路径按块处理的上限为 512 块／4096 次
  候选尝试，超限的剩余部分退词法路径——词法容器遮蔽不受该上限影响（第 13 轮已用 520 块与
  4200 个伪候选验证），但结构化路径的解析深度仍受上限约束。

## 词法脱敏缺口闭合与 SKILL.md 余量（2026-11-11）

- **可达性**：`--cmd` 采集正文与 `call_gpt.py` 出站 prompt 都会走 `redact()`；非规范排版的
  `{"api_key": {"v": "真值"}}` 无法逐字节还原成标准序列化风格，整份退词法路径，而 `_ASSIGN`
  不匹配开括号，秘密原样进入请求体/证据包/审计——**真实可达，按修复处理**。
- **整改**：词法路径新增原地容器扫描器；`_ASSIGN` 裸值在 `}`/`]`/`,` 处截断且尾部结构原样保留
  （标记与结构不再互相吞掉，幂等成立）；分隔符允许冒号后换行，键与容器之间可夹注释
  （`#`/`//`/`/* */`，含多行块注释与行尾未闭合块注释）；`//` 只在行首/空白/分隔符之后算注释
  （`1024//2` 是除号），`#` 同样只在行首/空白/分隔符之后算注释（`abc#def` 是同一个标量），
  未闭合 `/*` 不算注释段；键位由 `expect_key` 判定（值位上的冒号不把裸词当键名）；注释正文按
  成对字面量遮蔽；统计键数值与结构化路径同口径；容器内数据形态的**裸标量**（YAML flow /
  JSON5）与凭据键下的 URL 遮蔽，含代码关键字或括号内字面量时整体按代码处理；
  三个扫描正则加词界前置断言，16K 长串 `redact` 由 44.7s 降到 0.007s（基线 `d60be34` 13s）。
- **验证**：849 tests OK（不引用历史 831/837/838）；变异矩阵 14 条本轮用例逐条都有变异能变红
  （恒等容器扫描 12/14、裸标量放行 4/14、块前回看退化为纯空白 2/14、`//` 全当注释 1/14、
  `#` 全当注释 1/14、未闭合 `/*` 1/14、结构化忽略凭据位 2/14、值位冒号当键 1/14、
  丢弃尾部结构 1/14、恢复前缀放行 1/14、关掉字面量边界 1/14、恒等+旧口径 14/14，
  `never-red: none`）；三向探针 42 例现行全部通过
  （无泄漏、行数稳定、幂等、结构保留），基线 `d60be34` 38 例不合格，仅
  `code_comprehension`/`call_arg_literal`/`inline_value_then_report`/`legal_marker_still_idempotent`
  与基线同为通过（前两者是已记边界，后两者是刻意保留的边界/幂等形态）；`--cmd` 端到端 8 例
  （注释间隔、YAML 裸标量、未闭合注释、整除、9 行空行、行尾未闭合 `/*`、值位冒号 + 标量内 `#`）
  经**真实桥 CLI** 采集：桥退出码 0（8/8 条），证据包内无夹具残留、行数稳定、元数据完整，
  出站 `redact(pack)` 与包内文本逐字节相同（发送阶段不再二次改写）。
- **G4 适用性**：本轮没有 native dispatch / closure 状态，`native_agent.py` 的复核封套固定
  `g4_eligible=false`，高风险派发按 `native_review_missing` fail-closed；G4 不适用于本轮，
  不把正式 REVIEW 或普通只读独立复核冒充 G4。


## 最小化收尾：第 14 轮定向 REVIEW 与交付（2026-11-11 收尾轮）

- **定向正式 REVIEW（第 14 轮，最后一轮）**：`afe4804c9680db6b`，`gpt-6.1-sol`（默认路由，
  `model_identity_ok=true`），`remediation` 依据，新增 13 条 / 沿用 187 条，**裁决 PASS**。
  只裁决第 13 轮遗留的三项 blocking：F1 伪标记放行（改按完整 `<redacted:数字>` 标记校验）、
  F2 字面量内键头越界改写元数据（`_enclosing_string` 边界 + 字面量内递归 + depth 线程）、
  F3 缺真实 `--cmd` 入口证据（8 例走真实 `bridge_evidence.py --cmd`，退出码 0，逐例正文核对）。
  非阻塞观察 2 条（外层 `echo` 退出码不能代替被测退出码，本轮已显式给出
  `F1/F2/F3/META_EXIT`；部分夹具输入经再次脱敏，已结合披露行与模块指纹判断），不影响验收。
  第 1–12 轮结论不改写。
- **测试**：最终代码上 `python3 -B -m unittest discover -s tests -p 'test_*.py'` =
  **849 tests / OK / SUITE_EXIT=0**（日志 sha256 `429b247e…`），不是历史计数。
- **同步**：`--apply` 写入 4 个文件（`SKILL.md`、`scripts/lib_common.py`、
  `scripts/bridge_evidence.py`、`references/review-ledger.md`），`--check`
  **缺失 0 / 不同 0 / Reviewer same**；14 个分发文件逐个 SHA-256 与源码一致
  （`SKILL.md 1ee44090…`、`lib_common 09845be3…`、`review-ledger a6f0c2d2…`）；
  基线 `manifests/source-baseline.sha256` 更新为 15 个文件。
- **新会话加载验收**：全新 headless 进程（`node <zcode-cli-entry> -p …`，
  cwd 为空目录）用 Skill 工具加载成功，正文含 `references/review-ledger.md` 两处引用；
  `SKILL.md` 96,666 字节 / sha `1ee44090…`、Reviewer `8d283b29…`、`review-ledger.md` 47 行 /
  `a6f0c2d2…` 全部与源码一致；`call_gpt.py --help` 与 `review_ledger.py --budget` 退出码 0
  （台账 14 轮）；无 `.bak`/`.orig`/`SKILL.md.*` 残留。
- **G4**：本轮无 native dispatch，G4 不适用；正式 REVIEW 与普通只读独立复核都不冒充 G4。
- **冻结**：本轮之后停止扩展脱敏器理论边界、停止为单个新增测试重开 REVIEW、停止每轮携带
  全部历史证据、停止低价值测试扩充；只有真实安全漏洞、证据损坏、实际运行故障或明确新需求
  才重新立项。

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
- Git 远端：`origin` = `https://github.com/tongsailong/copilot.git`（已配置；收尾轮以普通快进
  推送，推送后以 `git ls-remote` 核实本地 / 跟踪分支 / 远端三方一致，结果见当轮交付报告）；
  后续提交节奏与分支保护策略未定。

## 下一任务

1. 获得一次性授权后执行 Phase 3.6 C4（真实 SOL，预算与请求上限见授权包）；2026-11-08 的 4 次
   局部调用不计入 C4 验收。
2. 在需要时用真实 SOL 复测本轮三项收尾修复：默认不限次升档、未知用量统计、无进展重析
   （需新的一次性授权）。
3. 达成后按固定流程开发：源码修改 → 测试 → 检查 diff → 同步 → `--check` → 新会话加载。
4. Phase 4 剩余范围（Reviewer 自动派发、可信复核记录升级）需单独授权与实现。

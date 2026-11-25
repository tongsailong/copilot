# Copilot — ZCode 原生 Agent 的协作治理 Skill

[![CI](https://github.com/tongsailong/copilot/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/tongsailong/copilot/actions/workflows/ci.yml)

Copilot 是运行在 ZCode 内的**轻量协作治理 Skill**。它把多步骤工程任务变成
可追踪、可恢复、可审计的合同与状态机，同时**不接管工程执行与技术决策**。

## 角色与边界

| 角色 | 职责 | 边界 |
|---|---|---|
| **ZCode 主 Agent** | 现场调查、技术选型、实现、调试、测试、集成与交付 | **唯一工程执行与技术决策主体** |
| **Copilot（本项目）** | 任务合同、完成条件、状态机、证据登记、G1–G3 收口门禁、PARTIAL 续跑、审计 | 是本地脚本与状态，不是模型；不实施工程、不替代技术决策、门禁不可绕过 |
| **认知** | 默认认知引擎：理解目标、方案取舍、风险与再咨询条件（CONSULT / PLAN），用户明确要求时执行一次独立复核（REVIEW） | 零工具、单轮、**advisory**；结论不构成证据、门禁或授权，失败只降级不阻断 |
| 用户 | 目标、优先级、取舍、授权与最终验收 | 提交、推送、发布、付费、生产变更仍须显式授权 |

三条不可混淆的边界：

1. **Copilot 不执行工程**。它记录事实、判定状态、暴露缺口，具体改代码由 ZCode 主 Agent 完成。
2. **认知不是工程执行主体**。它只输出判断，采纳与否由主 Agent 决定，采纳记录用 `cognition-accept` 留痕。
3. **命令不是授权**。`copilot_dispatch.py` 能做的事不代表被授权去做。

## 它做什么

- **任务合同与完成条件**：结构化条件、破坏性修订留痕（`--authorized-by user`）。
- **状态机**：`RUNNING` / `PARTIAL` / `PASS` / `BLOCKED` / `CLOSED`；PARTIAL 是连续执行状态，
  仍有可执行工作时返回 `EXIT_CONTINUE=10` 并派生下一项（`AUTO-C*` / `AUTO-R-*` / `AUTO-FIX-*` …）。
- **证据与绑定**：证据先登记再消费，执行/复核结果绑定 `task/contract/plan/work_item/result sha256`；
  文件型证据重读校验摘要。
- **收口门禁 G1–G3（fail-closed）**：工作项终态、合同/条件/证据完整性、Finding 台账与停摆检查。
- **认知生命周期调度**：A–E 五个认知窗口物化为持久化义务（`state.cognition_schedule`），由生命周期
  命令在自身事务内**有界执行**（每条命令最多 1 个义务 / 1 次请求），`--cognition auto|advise|off` 控制；
  `cognition-stats` 给出显式分母的覆盖率统计。
- **收口时序（v9.6.1）**：`closure_readiness` 是收口 CONSULT 与 G1–G3 的唯一准入——最终工程验证
  完成前不执行收口判断；验证完成 → 认知收口 CONSULT → 主 Agent 用 `cognition-accept` 登记独立判断 →
  G1–G3 收口。同一验证摘要复用结论，摘要变化即旧结论失效。
- **工作项领取**：`drive` 为选中工作项写入短租约（owner / TTL / handoff），避免并发重复执行。
- **恢复与续跑**：`SessionStart` / `UserPromptSubmit` / `Stop` 钩子只读提示未闭合任务与续接命令。
  钩子不写状态；`SessionStart` / `UserPromptSubmit` 内部异常 exit 0（不阻断会话），`Stop`
  守卫保持 0/2 契约、内部异常返 2（不放行，宁可多跑一轮）。
- **项目上下文（只读）**：`context_engine.py` 的 `resolve` / `recall` / `pack` / `capture`，全程
  零模型调用，不新建记忆库、不写知识库。

## 运行环境

- **Python 3.10+**（开发与验证环境 3.12；CI 矩阵 **3.10 / 3.12**）。**仅用标准库**，无第三方运行时依赖。
- **可选依赖**
  - `Pillow`：只有把图片作为证据时必需（完整性校验）。缺失时采集端**拒绝**图片证据，不降级放行。
  - `PyYAML`：只有运行 `legacy/tests` 历史回归时可能用到。
- **ZCode 宿主**：Skill 发现、子 Agent 派发与 `Stop` / `SessionStart` / `UserPromptSubmit` 钩子
  需要 ZCode；纯 CLI 部分（`copilot_dispatch.py` / `copilot_auto.py`）不依赖宿主。
- **认知（可选）**：需要本机存在 OpenAI 兼容端点 `http(s)://127.0.0.1:8317`（Responses API），
  提供唯一认知模型 `gpt-6.2-astra`，且
  `~/.zcode/v2/provider_config.json` 中有对应凭据。非本机地址一律拒绝。
  没有该环境时用 `--cognition off` 完全离线运行，任务照样可以推进到 PASS/BLOCKED。

## 安装

本仓库是**唯一源码**；`~/.zcode/skills/copilot` 是构建产物，只允许从本仓库单向下发。

```bash
git clone https://github.com/tongsailong/copilot.git
cd copilot
python3 -B scripts-sync/sync_runtime.py --check    # 查看与现有运行时的差异
python3 -B scripts-sync/sync_runtime.py --apply    # 单向写入 ~/.zcode/skills/copilot
python3 -B scripts-sync/sync_runtime.py --check    # 期望：缺失 0、不同 0
```

分发范围：`SKILL.md`、`scripts/**/*.py`、`references/**/*.md`。`tests/`、`legacy/`、`docs/`、
`manifests/`、`scripts-sync/`、`README.md`、`AGENTS.md` 不分发。同步原子替换并写后复核 SHA-256。

新开的 ZCode 会话才会加载新版本（Skill 元数据在会话启动时缓存）。

**恢复钩子**（可选，默认不装）：`scripts/copilot_stop_guard.py install --workspace DIR` 写
工作区级 hook 配置，`--user` 写用户级、`--config PATH` 写到指定文件（裸 `install` 会报错）。
属于修改全局/工作区配置，需你自行确认后执行；`uninstall` 可回退。钩子只读任务状态、
不写状态、不消费续跑次数。

## 持续集成（CI）

`.github/workflows/ci.yml` 在 **push 到 `main`** 与 **指向 `main` 的 Pull Request** 上运行，
矩阵为 Python **3.10 / 3.12**，每个 Job 依次执行：

```bash
python3 -m compileall -q scripts scripts-sync tests                    # 编译检查
python3 -B -m unittest discover -s tests -p 'test_*.py'               # 活动套件（本地基线 849 项）
python3 -B scripts-sync/sync_runtime.py --verify-baseline             # manifest / baseline 只读校验
```

CI 的安全边界（外部 PR 的代码视为不可信）：

- 使用普通 `pull_request` 事件，**不使用** `pull_request_target`；
- 顶层 `permissions: contents: read`，`actions/checkout` 设 `persist-credentials: false`；
- 不引用任何 Secrets，不调用认知，不访问本机 Provider、知识库、运行时目录或私有网络；
- 只做只读检查，不执行 `sync_runtime.py --apply`、不写入运行时、不提交、不发布；
- Action 固定到完整 commit SHA，避免可变标签带来的供应链漂移。

`legacy/tests` 有 146 项 v5 契约遗留的失败/错误（99 F + 47 E）与 32 个 skip，**不在 CI 门禁内**，
也不代表当前版本回归。外部贡献者提交 PR 前请在本地跑通上面三条命令。

`main` 受分支保护（Repository Ruleset）：禁止强推与删除，改动必须经 PR 并通过上述两项 Check，
不要求人工审批；具体流程见 [CONTRIBUTING](CONTRIBUTING.md#分支保护与合并流程)。

当前 `main` 的 CI 为通过状态：运行
[`44915220688`](https://github.com/tongsailong/copilot/actions/runs/44915220688) 在 Python
3.10.22 与 3.12.16 上各 `Ran 849 tests` / `OK (skipped=1)`。徽章反映 `main` 最新一次运行结果，
以 Actions 页面为准。

## 使用（最小闭环）

```bash
cd /path/to/your-project

# 1) 建任务与合同（--task-kind 决定认知调用量；机械任务默认 0 调用）
python3 ~/.zcode/skills/copilot/scripts/copilot_dispatch.py start \
  --state .copilot-tasks/demo/state.json \
  --goal "把登录接口的错误码收敛为统一枚举" \
  --criteria-file criteria.json --risk medium --task-kind standard

# 2) 计划 → 执行 → 证据 → 回填
python3 .../copilot_dispatch.py plan --state ... --plan plan.json
python3 .../copilot_dispatch.py evidence --state ... --add --id E1 --file out/test.log
python3 .../copilot_dispatch.py record --state ... --work-item A1 --status done \
  --result-file out/result.json --evidence E1

# 3) 推进与收口（EXIT_EXECUTE=10 表示还有下一步，EXIT_DONE=0 表示已收口）
python3 .../copilot_auto.py drive --state ...
python3 .../copilot_dispatch.py closure --state ... --close
```

完整命令见 `references/workflow.md`；状态机与持续推进语义见 `references/runtime.md`。
所有任务数据（状态、证据、认知原始输出）落在 `.copilot-tasks/`，已被 `.gitignore` 排除。

## 安全边界

- **授权**：commit、push、发布、部署、付费、生产数据与 Provider/安全策略修改都须用户明确授权；
  Copilot 命令本身不构成授权。
- **fail-closed**：证据缺失、绑定不符、门禁未过时一律失败关闭，不用“看起来合理”替代验证；认知结论也不等于证据或授权。
- **不存凭据**：仓库与分发物中不含 API Key、凭据、真实审计日志或任务状态。认知只从本机既有配置
  读取凭据，只允许本机回环出口，失败不重试、不换 Provider、不自动降级模型。
- **不删除历史**：历史审计、证据与只读台账保留，不因退役而改写。

## 已知限制

- **认知模型**固定为 `gpt-6.2-astra`（`reasoning.effort=high`、零工具、单轮），没有
  `--model` 开关、没有第二模型、没有失败回退或自动切换；上游回显不一致即拒绝消费。
  公共环境没有该模型时应显式 `--cognition off`。
- **知识库路径是作者本机默认值**：`context_engine.py` 可用 `--vault-root` 改；
  `knowledge_handoff.py` 的白名单根目录（`ALLOWED_ROOTS`）写死在源码中，换目录需自行调整该常量
  （本版未提供 CLI 开关）。
- **`legacy/tests` 不是全绿**：它验证 v5 历史行为，含 146 个已知失败/错误（99 F + 47 E）与
  32 个 skip，属历史契约保留，不作为当前规则判定依据。当前套件 `tests/` 全绿。
- **CI 只覆盖活动套件**：GitHub Actions 运行 `tests/`、编译检查与 baseline 校验，不运行
  `legacy/tests`（已知非全绿），也不代替真实宿主验证。
- **续跑次数上限由宿主决定**：ZCode 单回合的 Stop 续跑次数有硬上限，超出会被静默忽略；
  钩子无法读取或修改该计数。
- **平台**：脚本面向 POSIX（用到 `fcntl`），Windows 原生环境未验证。

## 目录

```text
copilot/
├── SKILL.md                 # Skill 入口与运行契约（分发）
├── scripts/                 # 运行时脚本（分发）
│   ├── governance.py        # 治理引擎：合同、状态、阶段、G1–G3、PARTIAL 派生
│   ├── copilot_dispatch.py  # CLI：任务、计划、证据、收口、认知/复核
│   ├── copilot_auto.py      # 驱动：drive / ingest
│   ├── cognition_call.py    # 认知 CONSULT/PLAN/REVIEW（唯一远程调用，单轮零工具）
│   ├── cognition_schedule.py# 认知生命周期调度（纯策略、零网络零 I/O）
│   ├── knowledge_handoff.py # 知识交接块（校验 read 摘录来源）
│   ├── context_engine.py    # 项目上下文 resolve/recall/pack/capture（只读）
│   ├── copilot_stop_guard.py# Stop / SessionStart / UserPromptSubmit 钩子
│   └── …                    # native_bridge / bridge_evidence / agent_policy / lib_common
├── references/              # 协议与操作参考（分发）
├── legacy/                  # v5 历史实现与历史测试（含当时 SOL 通道；不分发，仅追溯）
├── tests/                   # 当前开发测试
├── docs/                    # 决策、状态与路线图
├── manifests/               # 源码 SHA-256 基线
└── scripts-sync/            # 源码 → 运行时同步工具
```

## 开发

```bash
python3 -B -m unittest discover -s tests -p 'test_*.py'          # 当前套件（849 项）
python3 -B -m unittest discover -s legacy/tests -p 'test_*.py'   # 历史回归（已知非全绿）
python3 -B scripts-sync/sync_runtime.py --apply                  # 改动后单向下发
python3 -B scripts-sync/sync_runtime.py --baseline --verify-baseline
```

新增行为需要有对应测试；不为通过而放宽断言或删除测试。提交只暂存明确路径。详见
`CONTRIBUTING.md`、`AGENTS.md` 与 `docs/DECISIONS.md`。

## 版本演进

| 版本 | 要点 |
|---|---|
| **v9.10.0** | 认知命名收敛与 SOL 退役：`sol_review.py`/`sol_schedule.py` → `cognition_call.py`/`cognition_schedule.py`，`sol-*` 命令 → `cognition-*`（旧命令名与 `--sol` 别名**一并移除**，不保留跳转表）；状态键统一为 `cognition_calls`/`cognition_reuses`/`cognition_schedule`，`sol_calls`/`sol_reviews`/`sol_advisories`/`sol_reuses`/`sol_backoff`/`sol_schedule` 只读兼容（不迁移、不回写、不再写旧退避单例）；`gpt-6.2-sol` 与 `--model` 彻底移除，只剩 `gpt-6.2-astra`；`knowledge_handoff`/`context_engine` 的 `sol` 目标名改 `cognition`（别名兼容） |
| v9.9.0 | Astra 默认认知引擎迁移与调用效率优化：默认 `gpt-6.2-astra`（当时的 `gpt-6.2-sol` 显式兼容已于 v9.10.0 移除）；记录/审计/复用键带 `provider`+`model_key`；退避按 `provider+model+error_class` 隔离；纯登记/报告/合同命令默认 `advise`，`judgment` 等不再隐式付费调用；`--cognition` 为模型无关别名；复用命中登记 `cognition_reuses`，`cognition-stats` 增加 `by_model` 与 `reuse` 指标 |
| v9.8.0 | 多模态设计认知：认知 `--image` 真实图片输入（最多 6 张、单张 ≤8 MB、合计 ≤24 MB，PNG/JPEG/WebP），审计仅存名称/类型/字节/尺寸/sha256/说明，图片指纹进入请求摘要；无图认知咨询与有图视觉咨询双模式；设计协作协议、创造性候选、采纳台账与知识复用边界；沿用现有 Memory/知识通道，不新建审美记忆系统 |
| **v9.7.1** | 认知记录闭环：`cognition-accept --historical` 只为已收口任务补记复核（`pending` 必须写原因；重复执行幂等、不覆盖既有结论、不据结果一致推断采纳）；调度义务写终态处置（`completed`/`covered`/`skipped`/`failed`/`not-executed`/`unsettled`）与当时策略快照，`not-executed` 无快照即 `unknown`；义务带收口周期编号，重开前后严格分离；锁等待失败留痕但不算失败 |
| v9.7.0 | 认知主动调度：CONSULT/PLAN 可选议程（`agenda`，只读呈现）；材料带历史判断、采纳与未处理议程；收口周期后不再追加 B/D 窗口；`blockage-change` 只由未解除阻塞触发；采纳待回填可见；`cognition-stats` 增加议程/采纳/收口后调用观察值；capture 草稿带认知溯源 |
| v9.6.2 | 覆盖率口径修正（任务级窗口按任务计 1 次，不再出现 `rate=3.0`）；事件分母排除合理跳过；自动调度认知锁等待有界（`AUTO_LOCK_WAIT_S`，`COPILOT_COGNITION_LOCK_TIMEOUT`）；认知网络栈惰性导入（命令冷启动约 −15~20 ms）；文档/帮助与实现对齐、死代码收敛 |
| v9.6.1 | 收口时序：最终工程验证完成才执行收口 CONSULT；验证摘要纳入材料口径；`cognition-accept` 成为收口前的强制步骤 |
| v9.6.0 | 认知 A–E 五阶段义务化调度、`--cognition auto\|advise\|off`、覆盖率统计 |
| v9.5.x | 工作项领取与短租约、阻塞按项成立、SessionStart/UserPromptSubmit 恢复提示 |
| v9.4.0 | 只读 Context Engine（resolve/recall/pack/capture） |
| v9.3.0 | 合同破坏性修订留痕、条件核销自动撤销空包装项、重开不得复用旧 PASS |
| v9.0.0 | 认知驱动：CONSULT / PLAN 生命周期 + `cognition-accept` 采纳记录 |
| v8.5.0 | PARTIAL 强制续跑（`EXIT_CONTINUE=10`） |
| v8.0.0 | G4 原生只读复核退役，门禁收敛为 G1–G3 |
| v7.0.0 | 独立模型复核改为 SOL 零工具单轮复核（advisory） |

完整记录见 `docs/STATUS.md` 与 `docs/DECISIONS.md`。

## 许可证

[MIT](LICENSE) © 2026 tongsailong

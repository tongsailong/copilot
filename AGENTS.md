# AGENTS.md — Copilot 源码仓库规则

本文件只约束 `/media/vdb/projects/copilot` 仓库。用户级全局 `AGENTS.md` 的授权边界与
安全规则优先；本文件只补充项目定位、角色、模型路由、闭环与交付纪律。详细协议以
`SKILL.md`、`references/` 与 `docs/` 为准，本文件不重复展开。

## 1. 项目定位

Copilot 是运行于 ZCode 内的**轻量协作治理 Skill**：负责任务承接、合同与完成条件、状态机、
证据桥接、审计、PARTIAL 持续推进与 G1–G4 收口。ZCode 原生 Agent 是唯一的工程执行与技术
决策主体；用户侧 ChatGPT 负责需求与产品层目标、交接与验收；原生 Reviewer 承担独立复核与
高风险 G4。

- **v6.0.0 起 SOL 决策通道退役**：活动路径不得调用任何远程模型，不得把其他远程大模型设为
  强制决策节点，也不得设置等待远程返回的状态门禁。历史实现归档在 `legacy/`，历史审计、
  裁决、台账与证据只读保留。
- 本仓库是**唯一源码**。`~/.zcode/skills/copilot` 与 `~/.zcode/agents/reviewer.md`
  是构建产物，只能由 `scripts-sync/sync_runtime.py` 单向下发，不得直接编辑。
- 运行时临时修复必须先回迁本仓库，再同步下发；不使用符号链接替代副本。
- 源码与运行时的分工见 `docs/DECISIONS.md`。

## 2. 角色职责

顶层协作角色按 `docs/PROJECT-CHARTER.md`：用户、ChatGPT（用户侧）、Copilot、ZCode 原生
Agent、原生 Reviewer、DesignWise。Executor / Reviewer 是 ZCode 工程执行体系内的子角色，
不是与顶层角色并列的职责。

| 角色 | 职责 | 关键限制 |
|---|---|---|
| ZCode 主代理 | 现场调查、工程规划、技术选型、实现、调试、测试、交付；派发子代理；组织必要的独立复核 | 不擅自改变产品目标、业务规则或授权范围 |
| Executor 子代理 | 按工作项执行具体实现与取证，回报事实 | 有工具但无最终判断权；不得替代 Reviewer |
| Reviewer 子代理 | 在独立上下文基于真实文件、diff、测试日志与原始证据给出复核结论 | 只读工具集（`Read,Grep,Glob,Bash`，禁止 `Write,Edit`）；这是提示词与工具配置层面的要求，不是操作系统沙箱；不得修改代码或代替执行方 |
| Copilot 状态层 | 合同、条件、状态、证据登记、四门、PARTIAL 派生、收口、审计 | 是本地脚本与状态，不是模型；不调用模型、不实施工程、不替代技术决策、门禁不得绕过 |
| 正式 REVIEW | 用户明确要求时的治理记录 | 不是普通开发前置门禁；不能替代 G4；无授权不得伪造 |
| G4 复核 | 高风险任务收口所需的技术复核 | 原生独立只读复核 + 宿主记录重核 + 目标摘要绑定；fail-closed |

**模型独立 ≠ Agent 独立。** 共享同一 Provider/模型不等于共享上下文、会话或判断。
Reviewer 与 Executor 必须是不同的 Agent ID / Child Session / 上下文；Reviewer 的
独立性由独立上下文、只读工具、独立取证与独立报告保证，不由模型名保证。

## 3. 模型路由规则

- **活动路径无远程决策模型**：`governance.py`、`copilot_dispatch.py`、`copilot_auto.py`、
  `native_bridge.py`、`native_agent.py` 不得导入网络客户端或已退役的 SOL 模块
  （`call_gpt` / `review_ledger` / `reply_protocol`）。
- **Executor / Reviewer 继承主代理**：Profile 中省略 `model` 或写 `model: inherit`，
  Provider/模型/推理级别跟随主代理当前会话。不得写死官方 DeepSeek 直连模型或任何
  固定 Provider；`thoughtLevel` 只在钉死具体模型时生效，`inherit` 下忽略。
- **统一出口**：子代理请求经主代理当前 CPA 路由（默认 `127.0.0.1:8317`），不得因
  局部问题回退到官方直连 API，也不得通过修改 CPA Provider、凭据池或全局网络配置绕过。
- **模型名 ≠ 上游身份**：以 `~/.zcode/cli/db/db.sqlite` 的 `model_usage`、
  rollout 的 `model.providerId` / `x-cpa-trace-id` 与 CPA `usage_events` 三方互证为准；
  Agent 自报模型名不算证据。
- **主代理换模型后必须复查**：确认 Executor/Reviewer 仍为 `inherit` 且实际路由一致。
  Agent Profile 在进程/会话启动时缓存，配置修改后须新会话或新进程验证。
- 不修改 Provider、模型路由与 CPA 配置；相关变更须用户单独授权。

## 4. 工程执行规则

1. 修改前核实项目身份、分支、`git status`、相关入口与既有改动；保留他人及任务外改动。
2. 只改与本任务相关的文件；不顺手重构、格式化或修复无关内容。
3. 运行测试：

   ```bash
   python3 -B -m unittest discover -s tests -p 'test_*.py'
   python3 -B -m unittest discover -s legacy/tests -p 'test_*.py'   # 历史回归
   ```

   新增行为应有对应测试；不得为通过而放宽断言或删除测试。`legacy/tests` 只验证 v5
   历史行为，历史文档契约类允许 skip，但实现回归失败必须查明。
4. 检查实际 diff，清除调试残留、秘密和任务外内容。
5. 源码改动后必须同步并复查：

   ```bash
   python3 scripts-sync/sync_runtime.py --apply
   python3 scripts-sync/sync_runtime.py --check   # 缺失 0、不同 0
   ```

   同步同时覆盖 Skill 与 Reviewer；`--prune` 仅在明确要求清理运行时多余文件时使用。
6. 真实模型调用、push、发布、部署均需单独授权；本地 commit 仅在任务明确授权时执行。

## 5. Copilot 闭环规则

- 状态机收敛为 `RUNNING` / `PARTIAL` / `PASS` / `BLOCKED` / `CLOSED`；不得恢复
  `pending_call`、`quota`、`round_seq`、`budget`、`pass_pending` 等远程判定字段。
- **PARTIAL 不是终点**：必须本地派生工作项（`AUTO-C<n>` / `AUTO-R-<id>` / `AUTO-G4` /
  `AUTO-REPLAN` / `AUTO-NEXT`）并继续推进，直到 PASS 或真实 BLOCKED。禁止因轮次、Token、
  内部费用、等待模型、重复评审、子 Agent PARTIAL、首次方案失败或普通测试失败停止。
- `execution_backend` 默认 `legacy`，native 为显式 opt-in；既有协议决定不得擅改。
- G1–G4 保持 fail-closed：证据不足、绑定不符、门禁未过时必须失败关闭，不得用
  “看起来合理”替代验证；普通 Reviewer PASS **不等于** G4，`g4_eligible` 由宿主记录计算。
- 执行/复核必须绑定 task / contract / plan / dispatch / work_item /
  `execution_result_sha256`；证据须先登记且属于本任务，文件型证据会重读校验摘要。
- Reviewer 只读、独立取证、独立报告；不得继承执行方判断，不得因执行方自述而放弃核查。
- 正式 REVIEW 仅在用户明确授权时登记；授权在同一任务合同内持续有效，不得成为普通开发
  门禁，也不得用自检冒充。G4 不依赖正式 REVIEW。
- 不跳过四门、收口与证据校验；未过门不得标记 CLOSED 或 PASS。测试默认使用本地夹具，
  不发起真实外部调用。
- 不重做状态机、四门、自动驱动或证据协议；需要变更时先说明影响并取得授权。

## 6. 修改与交付纪律

- 只暂存明确路径，禁止 `git add .`；提交前检查暂存 diff，确认无秘密、凭据、真实审计
  日志、临时状态或任务外改动。commit 授权不含 push。
- 不提交 `.env*`、API Key、凭据、`.audit/`、运行时状态、数据库、备份或 `__pycache__`；
  这些由 `.gitignore` 与同步工具排除。
- 交付时区分已完成、未完成与未验证；复杂任务按 PASS / PARTIAL / BLOCKED 给出结论，
  并说明测试、同步、未验证项与剩余风险。
- 文档职责单一：长期决策进 `docs/DECISIONS.md`，当前现场状态进 `docs/STATUS.md`，
  执行规则进本文件，Skill 触发与用法进 `SKILL.md`，详细协议进 `references/`。
  同一规则只保留一处权威定义。
- 不修改其他 Skill、全局 `AGENTS.md`、CPA Provider/模型路由/凭据，也不删除历史审计、
  备份或归属不明的内容；发现敏感信息或归属不清时先保护并报告。

## 文档导航

- 项目定位与开发方式：`README.md`
- 角色职责：`docs/PROJECT-CHARTER.md`
- 阶段目标：`docs/ROADMAP.md`
- 已确认 / 建议 / 待决：`docs/DECISIONS.md`
- 当前证据与未验证项：`docs/STATUS.md`
- Skill 入口与运行契约：`SKILL.md`
- 操作命令：`references/workflow.md`
- 阶段机与持续推进：`references/runtime.md`
- 复核与 G4：`references/review.md`
- 原生信封：`references/agent-bridge.md`

# AGENTS.md — Copilot 源码仓库规则

本文件只约束 `/media/vdb/projects/copilot` 仓库。用户级全局 `AGENTS.md` 的授权边界与
安全规则优先；本文件只补充项目定位、角色、模型路由、闭环与交付纪律。详细协议以
`SKILL.md`、`references/` 与 `docs/` 为准，本文件不重复展开。

## 1. 项目定位

Copilot 是 ZCode 上的**任务承接与协作治理层**：按需经 SOL 获取高级工程决策；用户侧 ChatGPT
负责需求与产品层目标、交接与验收，ZCode 原生 Agent 负责工程调查、规划、执行与调度。原生子代理
承担执行与独立复核，Copilot 状态层负责合同、轮次、证据与门禁（G1–G4）。

- 本仓库是**唯一源码**。`~/.zcode/skills/copilot` 与 `~/.zcode/agents/reviewer.md`
  是构建产物，只能由 `scripts-sync/sync_runtime.py` 单向下发，不得直接编辑。
- 运行时临时修复必须先回迁本仓库，再同步下发；不使用符号链接替代副本。
- 源码与运行时的分工见 `docs/DECISIONS.md`（第 1–8 条）。

## 2. 角色职责

顶层协作角色按 `docs/PROJECT-CHARTER.md` 的六角色定义：用户、ChatGPT（用户侧）、Copilot、SOL、
ZCode 原生 Agent、DesignWise。下表只列本仓库直接约束的执行侧与通道侧分工；Executor / Reviewer 是
ZCode 工程执行体系内的子角色，不是与六角色并列的顶层角色。

| 角色 | 职责 | 关键限制 |
|---|---|---|
| SOL（GPT-6.1-SOL High） | 高级工程判断、技术方案取舍、风险分析；依据提交证据给出工程工作安排及工程结果判断 | 零工具；只经 `scripts/call_gpt.py` 走 CPA；不读文件、不跑命令、不取证；意见不是授权、执行结果或独立复核 |
| ZCode 主代理 | 现场调查、工程规划、实现、调试、测试、交付；派发子代理；执行自检与必要的独立复核 | 不擅自改变产品目标、业务规则或授权范围 |
| Executor 子代理 | 按工作项执行具体实现与取证，回报事实 | 有工具但无最终判断权；不得替代 Reviewer |
| Reviewer 子代理 | 在独立上下文基于真实文件、diff、测试日志与原始证据给出复核结论 | 只读工具集（`Read,Grep,Glob,Bash`，禁止 `Write,Edit`）；这是提示词与工具配置层面的要求，不是操作系统沙箱；不得修改代码或代替执行方 |
| Copilot 状态层 | 合同、轮次、证据登记、四门、收口、审计 | 是脚本与状态，不是模型；门禁不得绕过 |

**模型独立 ≠ Agent 独立。** 共享同一 Provider/模型不等于共享上下文、会话或判断。
Reviewer 与 Executor 必须是不同的 Agent ID / Child Session / 上下文；Reviewer 的
独立性由独立上下文、只读工具、独立取证与独立报告保证，不由模型名保证。不得因为
Reviewer 跟随主代理模型就认为它与主代理或执行方“同一个 Agent”。

## 3. 模型路由规则

- **SOL 固定**：`gpt-6.1-sol` + `reasoning.effort=high`，经 CPA 零工具通道调用；
  请求体不得包含 `tools`，不得自动降档。标准 SOL／自动协作通道不得用 `--model` 覆盖模型与路由；
  不带 `--dispatch-state` 的手动一次性调用仍可用 `--model`（见 `SKILL.md` 模型段落），
  按手动模式留痕（`model_source=override`），不属于自动协作通道，也不改变本条默认路由。
  SOL 的模型与路由未经用户明确授权不得修改。
- **Executor / Reviewer 继承主代理**：Profile 中省略 `model` 或写 `model: inherit`，
  Provider/模型/推理级别跟随主代理当前会话。不得写死官方 DeepSeek 直连模型或任何
  固定 Provider；`thoughtLevel` 只在钉死具体模型时生效，`inherit` 下忽略。
- **统一出口**：子代理请求经主代理当前 CPA 路由（默认 `127.0.0.1:8317`），不得因
  局部问题回退到官方直连 API，也不得通过修改 CPA Provider、凭据池或全局网络配置绕过。
- **模型名 ≠ 上游身份**：以 `~/.zcode/cli/db/db.sqlite` 的 `model_usage`、
  rollout 的 `model.providerId` / `x-cpa-trace-id` 与 CPA `usage_events` 三方互证为准；
  Agent 自报模型名不算证据。
- **主代理换模型后必须复查**：确认 Executor/Reviewer 仍为 `inherit` 且实际路由一致。
  Agent Profile 在进程/会话启动时缓存，配置修改后须新会话或新进程验证，不能据当前
  会话的旧行为判定配置无效。

## 4. 工程执行规则

1. 修改前核实项目身份、分支、`git status`、相关入口与既有改动；保留他人及任务外改动。
2. 只改与本任务相关的文件；不顺手重构、格式化或修复无关内容。
3. 运行测试：`python3 -B -m unittest discover -s tests -p 'test_*.py'`。新增行为应有
   对应测试；不得为通过而放宽断言或删除测试。
4. 检查实际 diff，清除调试残留、秘密和任务外内容。
5. 源码改动后必须同步并复查：

   ```bash
   python3 scripts-sync/sync_runtime.py --apply
   python3 scripts-sync/sync_runtime.py --check   # 缺失 0、不同 0
   ```

   同步同时覆盖 Skill 与 Reviewer；`--prune` 仅在明确要求清理运行时多余文件时使用。
6. 真实 GPT/SOL 调用、push、发布、部署均需单独授权；本地 commit 仅在任务明确授权时执行。

## 5. Copilot 闭环规则

- `execution_backend` 默认 `legacy`，native 为显式 opt-in；既有协议决定不得擅改。
- G1–G4 保持 fail-closed：证据不足、绑定不符、门禁未过时必须失败关闭，不得用
  “看起来合理”替代验证；普通 Reviewer PASS **不等于** G4，`g4_eligible=false`。
- 执行/复核必须绑定 task / round / dispatch / work_item /
  `execution_result_sha256`；证据须先登记且属于本任务，文件型证据会重读校验摘要。
- Reviewer 只读、独立取证、独立报告；不得继承执行方判断，不得因执行方自述而放弃核查。
- 不跳过 REVIEW_PENDING、REJUDGE、CLOSURE_CHECK 等流程；未过门不得标记 CLOSED 或
  PASS。真实调用只在授权与配额内进行，测试默认使用 Fake Upstream。
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
- 协议细节：`references/agent-bridge.md`、`references/role.md`

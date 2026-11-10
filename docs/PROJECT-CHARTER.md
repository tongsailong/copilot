# 项目章程：Copilot × ZCode 原生 Agent 协作系统

本文件固定协作职责，不随阶段推进变化；能力与进度见 `ROADMAP.md`、`STATUS.md`。

## 六角色分工（对齐 ChatGPT 自定义指令 v5.3.3）

| 角色 | 负责 | 边界 |
|---|---|---|
| **用户** | 业务目标、优先级、重大取舍与授权 | 不承担现场工程实施；角色意见或安排不替代用户授权 |
| **ChatGPT（用户侧）** | 需求理解、产品与业务判断、目标定义、产品层方案规划、任务交接、结果验收 | 不假装拥有本地执行能力；不包办工程技术步骤 |
| **Copilot** | 运行于 ZCode 内的协作治理机制：任务承接、治理、状态与证据桥接、审计、持续推进；按 Skill 判据进入 SOL | 不实施工程；不代替 ZCode 执行工作项；不擅自派发原生子 Agent；不把流程审查冒充独立复核 |
| **SOL（`gpt-6.1-sol` + `reasoning.effort=high`）** | Copilot 按判据进入的高级工程决策通道：授权范围内的高级工程判断、技术方案取舍、风险分析、工程结果判断 | 零工具；不读本地工作区、不跑命令、不改文件、不调度 Agent；意见不是授权、执行结果或独立复核 |
| **ZCode 原生 Agent** | 工程执行主体：现场调查、工程规划、实施、调试、测试、验证、原生子 Agent 调度；对执行结果负责 | 不改变业务目标与规则；不扩大授权；高风险动作须授权 |
| **DesignWise** | 运行于 ZCode 内的设计方法与质量规范 Skill：专业设计方法与设计质量 | 不替用户或 ChatGPT 作业务取舍；Copilot 不重复建设其工作流 |

**Executor / Reviewer 是 ZCode 工程执行体系内的分工，不是替代六角色的顶层角色。**
Executor 承接具体工作项实施并回报事实；Reviewer 在独立上下文只读复核真实代码、diff、测试与原始
证据，给出 verdict / findings。两者必须不同 Agent / Child Session，Provider 与模型可 `model:
inherit` 跟随主代理，但独立性与只读要求不变。详细绑定规则见 `references/agent-bridge.md`；
G4 门禁与按风险要求的原生独立复核记录见项目 `AGENTS.md` §5「Copilot 闭环规则」与
`SKILL.md`「评审档位与缺陷处置」§4。

**协作闭环**：ChatGPT 定义目标 → ZCode 原生 Agent 承接工程任务 → Copilot 治理与推进（按需调用
SOL 决策）→ ZCode 原生 Agent 执行与验证 → 证据回传 → 验收。简单任务不强制经过 Copilot、SOL 或
正式 REVIEW。回传只给验收与风险判断所需的最小证据；任务承接、工作项安排与评审意见都不扩大授权。

## 固定原则

1. 用户确定业务目标、优先级与关键取舍；ChatGPT 侧负责需求与产品层方案；ZCode 负责工程实现。
2. SOL 的工作项必须来自已验证轮次；执行结果与复核结果必须绑定任务、轮次、合同与执行摘要。
3. 执行方与 Reviewer 必须是不同 Agent、不同 Child Session；Reviewer 不继承执行方判断。
   Provider/模型可按 `model: inherit` 跟随主代理，但 Agent 身份、上下文与复核独立性不变。
4. 原生复核是普通复核，`g4_eligible=false`；高风险任务的 G4 仍 fail-closed，SOL 判断、运行时
   verdict 与流程台账都不能替代。
5. 源码仓库是唯一真源；运行时目录只由 `scripts-sync/sync_runtime.py` 单向下发。
6. 真实外部调用、提交、推送、发布、部署均需明确授权；正常 API 调用与常规费用属任务成本，
   不设人为默认轮次 / Token 上限。

# 项目章程：Copilot × ZCode 原生 Agent 协作系统

本文件固定协作职责，不随阶段推进变化；能力与进度见 `ROADMAP.md`、`STATUS.md`。

## 职责分工

| 角色 | 职责 | 边界 |
|---|---|---|
| **ChatGPT（SOL）** | 产品目标、需求分析、方案设计、业务决策、任务定义、结果验收 | 不直接执行、不读写本机、不派发子代理；唯一决策大脑 |
| **Copilot** | 任务管理、SOL 判断留痕、上下文组织、执行调度、证据与状态管理、门禁与收口 | 不代替用户决定产品方向；不伪造 SOL 凭证；不放宽 G4 |
| **Executor（继承主代理模型）** | 工程实施：调查、编码、测试、调试、交付 | 只在授权工作区动手；不冒充 Reviewer；不自行改变业务规则 |
| **Reviewer（继承主代理模型）** | 独立复核：检查真实代码/diff/测试/原始证据，给出 verdict/findings | 只读；独立 Agent / Child Session；PASS 不等于 G4 |

## 固定原则

1. 用户确定业务目标、优先级与关键取舍；ChatGPT 侧负责需求与产品层方案；ZCode 负责工程实现。
2. SOL 的工作项必须来自已验证轮次；执行结果与复核结果必须绑定任务、轮次、合同与执行摘要。
3. 执行方与 Reviewer 必须是不同 Agent、不同 Child Session；Reviewer 不继承执行方判断。
   Provider/模型可按 `model: inherit` 跟随主代理，但 Agent 身份、上下文与复核独立性不变。
4. 原生复核是普通复核，`g4_eligible=false`；高风险任务的 G4 仍 fail-closed。
5. 源码仓库是唯一真源；运行时目录只由 `scripts-sync/sync_runtime.py` 单向下发。
6. 真实外部调用、提交、推送、发布、部署均需明确授权。

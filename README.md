# Copilot × ZCode 原生 Agent 协作系统

Copilot 是运行于 ZCode 内的**轻量协作治理 Skill**：用户侧 ChatGPT 负责需求与产品层目标、
交接与验收；ZCode 原生 Agent（主代理）是工程执行与技术决策主体，负责现场调查、规划、实现、
调试、测试与验证；Copilot 负责任务合同、完成条件、状态、证据、审计与收口门禁，并用 PARTIAL
派生持续推进任务；SOL 作为默认认知引擎按需提供判断，用户明确要求时可执行一次独立复核。

**v9.4.0 项目上下文与长期记忆衔接**：新增 `scripts/context_engine.py`，把原生项目
Memory、正式 Vault、Copilot Task Memory 与工程现场拼成一条确定性数据流——
`resolve`（按 `sha256[:16]` 定位项目记忆，同名不同路径不串库）、`recall`（白名单有界
召回，零命中/失败/截断可区分，短期缓存随文件变化失效）、`pack`（self/sol/subagent 三档
字符上限的上下文包）、`capture`（带任务与证据溯源的候选草稿，写入仍走
`candidate_write.py`）。SessionStart(resume|compact) 快照追加项目现场与召回/交接命令，
`status` 附非阻断提示；全程只读、零模型调用、不新增记忆库与知识库写入口。详见
`docs/CONTEXT-ENGINE.md` 与 `docs/DECISIONS.md` 第 70 条。

**v9.3.0 治理一致性与续跑绑定加固**：合同修订不再能通过删条件或改弱措辞绕过 G2
（破坏性修订须 `--authorized-by user` 留痕）；条件核销自动撤销其空包装项；任务重开后没有
新证据不得复用旧 PASS；多阻塞按项解除、恢复项家族不嵌套；Hooks 只在命令成功后绑定会话、
任务根查找止于 `.git`、守卫异常 fail-closed；`knowledge_handoff` 改为惰性导入，仓库只保留
跟踪文件时 CLI 仍可用。详见 `docs/STATUS.md` 与 `docs/DECISIONS.md` 第 68 条。

**v9.0.0 SOL-first 默认认知引擎**：SOL 恢复为 `CONSULT` / `PLAN` / `REVIEW` 三模式，
CONSULT 与 PLAN 成为非简单任务的默认认知活动（`THINK` 只作读取别名）。生命周期：
`start` 声明任务类型 → 起点 CONSULT（理解目标/本质/风险/调查方向）→ 调查后 PLAN
（方案比较/优先级/阶段目标）→ 新证据或方案失败时动态 CONSULT → 高复杂阶段复盘 →
收口前一次综合判断；没有硬性次数或 Token 上限，每次必须有明确问题与决策价值。
`sol-accept` 记录主代理的采纳/修改/否决；`review-metrics` 输出覆盖率、重复率、采纳与
usage/latency（缺失标 `unavailable`）。简单/机械任务 0 调用；正式 REVIEW 仍只在用户
明确要求时执行。SOL 零工具、单轮、advisory，不改变阶段、不影响 G1–G3，失败只登记并
降级（退出码 0）不阻断工程推进。

**v8.5.0 PARTIAL 强制续跑**：`continuation` 判定进入 `next`/`status`/`drive` 以及
`record`/`rerun`/`criterion`/`report`/`block`/`unblock`；仍有原始授权内的可执行工作时返回
`EXIT_CONTINUE=10`，阶段报告不再是任务终点。阻塞按工作项成立（`block --work-item`），
单项缺授权不冻结其余工作；`--criterion` 声明被卡住的条件，`unblock` 自动恢复被冻结项；
只有全部剩余工作真实受阻时才整体 BLOCKED。G1–G3、SOL 语义与授权边界不变。

**v8.4.0 SOL 默认认知引擎铺垫（历史）**：v8.4 曾把 CONSULT / PLAN 并入单一的 `THINK`
模式与一份结构化输出；v9 按生命周期重新拆为 CONSULT / PLAN，THINK 保留为读取别名。
简单任务 0 调用；REVIEW 只在用户明确要求时执行。认知材料上限 120,000 字符。

**知识交接扩展（2026-11-18）**：新增 `scripts/knowledge_handoff.py`，把主 Agent 已实际
Read 的 Memory/Vault 卡片渲染为带状态、核验时间与必要摘录的交接块；`sol-think` /
`sol-review` 支持 `--knowledge-file`，`read` 摘录必须能在卡片正文中找到，
`index-only`/`unverified` 不得冒充已读。`read` 是主代理声明 + 脚本核对来源，不证明
宿主同轮工具调用；可选 `read_evidence` 记录会话/工具证据。
子 Agent 使用同一工具生成 `--target subagent`
块并由主 Agent 放入 prompt。该扩展自 2026-11-21 起纳入 Git 与分发基线（提交 `c799a5c`），
干净 checkout 自带；缺少该文件时 CLI 仍可启动，只有使用 `--knowledge-file` 才返回
`KNOWLEDGE_EXTENSION_MISSING`。

**v8.3.0 SOL 主动介入（历史）**：触发判断从“风险标签 + 门禁是否全绿”升级为**确定性
复杂度 + 覆盖判断**，简单任务 0 调用，中高复杂任务一次，只有升级型新信号才追加；v8.4
把当时的 `PLAN`/`CONSULT` 两种模式并入 THINK。

**v8.2.0 SOL 与 G1–G3 融合（历史）**：三种 SOL 模式共用一条调用管线、一份任务上下文
（合同/条件/证据）与一份记录流（`sol_calls`，历史 `sol_reviews`/`sol_advisories` 只读兼容）、
统一审计 `sol-audit.jsonl`；`review-admission` 先算本地 G1–G3。

**v8.1.0 SOL 升级 GPT-6.2（历史）**：SOL 统一 `gpt-6.2-sol`（Responses API `/v1/responses`，既有
本机 CPA `127` 出口）、`reasoning.effort="high"`、`standard` 模式、零工具、单轮；不按角色
降级推理强度，不静默回退旧模型或别名，上游回显的模型/推理强度/模式不一致即拒绝消费。新增
`copilot_dispatch.py sol-advisory --mode CONSULT|PLAN` 提供建议性问题分析与规划辅助，结论写入
`sol_advisories`，不进入 Finding 台账、不影响 G1–G3、不阻断任务；DECIDE 不恢复，ZCode 仍是
唯一工程执行与技术决策主体。

**v8.0.1 安全与效率加固**：SOL 错误详情统一脱敏限长，本机 CPA 出口在解析凭据前无条件
校验并禁用环境代理；远程成功后审计/落盘失败不再被误报为调用失败，可恢复结果避免同材料
重复付费，并加任务级互斥锁；Finding 合并完全幂等，advisory 复核不能把已解决的 authoritative
finding 单方面提级为不可驳回阻断。dry-run 与真实调用共用材料校验。

**v8.0.0 起 G4 原生只读复核正式退役**：删除 `g4_attest.py`、`g4_shadow.py`、
`test_execution.py`、原生复核信封与 `native_agent.py review/review-context/capability`。
收口门禁收敛为 G1–G3（工作项终态、合同/条件/证据与 Finding 台账、无停摆），不再产生
`g4_eligible`、G4 PASS、G4 BLOCKED、`AUTO-G4*` 或 `host_capability_missing`；高风险任务
不再绑定 native 后端。原 G4 的“未闭合 blocking/major 阻断收口”安全职责并入 G2，对所有
风险等级生效。历史 `native_reviews` / `review_attempts` / `g4_attestations` 只读保留用于
审计，不参与新任务准入或收口。

**v7.0.0 起独立模型复核走 SOL 零工具单轮复核（历史）**：主代理完成开发、自检与测试后，
调用 `copilot_dispatch.py sol-review` 一次；v7 使用既有本机 CPA `127` 通道的 `gpt-6.1-sol`
（v8.1.0 起升级为 `gpt-6.2-sol`）。工具定义不会出现在请求体中（并显式
`tool_choice="none"`）。一次复核只发一次请求；失败不重试、不换 Provider。SOL 是 advisory，
不构成收口门禁，也不证明宿主只读权限；修复与验证仍由主代理完成。v8.4 起 REVIEW 只在用户
明确要求时执行，开发期的主动认知建议由 THINK 承担。

**v6.11.0 起命令开销按“一次命令一次快照”收敛**：`sync_runtime.py --apply` 同步后预编译
运行时字节码（文档命令用 `python3 -B`，只禁止写 `.pyc`），单次命令不再重复编译源码；
代码快照、`git_root` 结果在单次命令内复用，工作区只扫描一次；工作区根与 HEAD 合并为一次
`rev-parse`。门禁、漂移检测与证据绑定规则不变。

**v6.0.0 起 SOL 决策通道退役**（v7 恢复一次性 REVIEW，v8.1 恢复建议性 CONSULT/PLAN，v8.2 融合为统一按需认知能力）：
不再有远程模型决策依赖，不再有等待 SOL 返回的状态门禁，也不新增其他远程大模型充当强制
决策节点。历史调用记录、REVIEW 裁决、任务台账与证据原样保留，可只读查看。角色边界见
`docs/PROJECT-CHARTER.md`。

本仓库是**唯一源码**；`~/.zcode/skills/copilot` 是构建产物，只允许从本仓库单向下发。
旧 Reviewer 全局 Profile 已退役。工作区保留一个**通用只读能力载体**
`.zcode/agents/independent-readonly.md`（Read/Grep/Glob，无 Bash/Write/Edit），
只提供宿主级权限，供主代理按需做独立只读调查；它不定义 Reviewer 身份、不参与收口门禁，
普通任务不强制使用。

## 目录

```text
copilot/
├── AGENTS.md                # 项目执行规则（不分发）
├── SKILL.md                 # Skill 入口与运行契约（分发）
├── scripts/                 # 运行时脚本（分发）
│   ├── governance.py        # 本地治理引擎：合同、状态、阶段、G1–G3、PARTIAL 派生
│   ├── copilot_dispatch.py  # CLI：任务、计划、证据、收口、SOL 认知/复核、正式 REVIEW、历史只读
│   ├── copilot_auto.py      # 驱动：drive（推进/工作单/派生）与 ingest（执行信封回填）
│   ├── sol_review.py        # SOL 三模式（CONSULT/PLAN/REVIEW）：请求/解析/审计（唯一远程调用）
│   ├── knowledge_handoff.py # 知识交接块：校验 read 摘录来源并渲染 Memory/Vault → SOL/子 Agent
│   ├── context_engine.py    # 项目上下文：resolve/recall/pack/capture（只读、零模型调用）
│   ├── native_bridge.py     # Native Bridge v2 执行信封协议与校验
│   ├── native_agent.py      # 从宿主运行记录生成正式执行信封
│   ├── agent_policy.py      # Agent 调度建议（只读纯函数）
│   ├── bridge_evidence.py   # 证据采集：脱敏、截断标记、清单
│   └── lib_common.py        # 公共工具
├── references/              # 协议与操作参考（分发）
├── .zcode/agents/independent-readonly.md # 通用只读权限 Profile（不分发）
├── legacy/                  # v5 SOL 实现与历史测试（不分发，仅追溯）
├── tests/                   # 当前开发测试（不分发）
├── docs/                    # 项目管理文档（不分发）
├── .copilot-tasks/          # 治理任务本地数据：状态、证据、SOL 原始输出（不分发、不纳入 Git）
├── manifests/               # 源码 SHA-256 基线（不分发）
└── scripts-sync/            # 源码→运行时同步工具（不分发）
```

## 开发方式

1. 只修改本仓库；不直接编辑运行时副本。
2. 运行当前测试与历史回归：

   ```bash
   cd /media/vdb/projects/copilot
   python3 -B -m unittest discover -s tests -p 'test_*.py'
   python3 -B -m unittest discover -s legacy/tests -p 'test_*.py'
   ```

   `legacy/tests` 只验证 v5 历史行为（含若干历史文档契约跳过），不参与当前规则判定。

3. 检查实际 diff（本地 Git 仓库，`main` 分支保留可回溯提交；提交只含明确路径，
   push/发布/部署需单独授权，详见 `AGENTS.md` 与 `docs/DECISIONS.md`）。
4. 单向同步并复查：

   ```bash
   python3 scripts-sync/sync_runtime.py --check
   python3 scripts-sync/sync_runtime.py --apply
   python3 scripts-sync/sync_runtime.py --check
   ```

   `--check` 退出码：0 = 受管理文件缺失 0、不同 0；1 = 存在漂移；2 = 参数或安全校验错误。
   运行时多出的文件默认保留；`--prune` 必须与 `--apply` 一起显式使用。

5. 新的 ZCode 会话才加载新运行时版本。

## 同步边界

- 分发：`SKILL.md`、`scripts/**/*.py`、`references/**/*.md`。
- 不分发：`legacy/`、`tests/`、`docs/`、`manifests/`、`scripts-sync/`、`README.md`、`AGENTS.md`。
- 永不同步：`.env*`、密钥/凭据、`.audit/` 审计日志、临时状态、`__pycache__`、符号链接。
- 每次运行检查 Skill 受管理文件（`SKILL.md`、`scripts/`、`references/`）的一致性；
  Reviewer Profile 不再分发。
- 同步写入使用同目录临时文件 + `os.replace` 原子替换，写后重新校验 SHA-256。

## 安全与授权

- Copilot 命令不是授权：commit、push、发布、部署、付费、生产数据与 Provider/安全策略修改
  仍须用户明确授权。
- SOL 复核复用既有本机 CPA 配置，不新增 Provider、账户、密钥或付费方案；审计只记脱敏元数据，
  不回显凭据。
- G1–G3 门禁保持 fail-closed；SOL 复核是 advisory，不替代证据、测试或授权判断。
- 不在源码仓库或运行时分发物中保存 API Key、凭据、真实审计日志或临时任务状态。
- 不删除历史审计与证据；历史材料只读保留。

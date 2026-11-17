# Copilot × ZCode 原生 Agent 协作系统

Copilot 是运行于 ZCode 内的**轻量协作治理 Skill**：用户侧 ChatGPT 负责需求与产品层目标、
交接与验收；ZCode 原生 Agent（主代理）是工程执行与技术决策主体，负责现场调查、规划、实现、
调试、测试与验证；Copilot 负责任务合同、完成条件、状态、证据、审计与收口门禁，并用 PARTIAL
派生持续推进任务；高风险 G4 由宿主可证明只读的子代理承担独立复核。

**v7.0.0 起普通复核改为 SOL 零工具单轮复核**：主代理完成开发、自检与测试后，按需调用
`copilot_dispatch.py sol-review` 一次；SOL（既有本机 CPA `127` 通道的 `gpt-6.1-sol`）只
阅读材料并返回结构化问题清单，工具定义不会出现在请求体中（并显式 `tool_choice="none"`）。
一次复核只发一次请求；失败不重试、不换 Provider、不派原生 Reviewer；修复与验证仍由主代理
完成。原生多轮 Reviewer 已退役，`native_agent.py review` / `review-context` / 原生复核
ingest 只保留给高风险 G4。SOL 不是 G4，不能替代 G4 的 fail-closed 判定。

**v6.12.0 起 G4 只读能力按宿主“有效会话状态”准入，并按原定架构限定载体来源**：
ZCode 3.18.2 的内置子代理只有 `Explore`（含 Bash）与 `general-purpose`（全工具），没有
精确只读载体；`.zcode/agents/independent-readonly.md` 是 **project 级自定义 Agent**，
不是内置子代理，因此不作为正式 G4 方案。G4 现在要求 `profileSnapshot.source=built-in`
且子会话 `runtime/execution_state.planEnabled=false`（plan 模式会给子代理注入 Bash）；
项目/用户 Profile 返回 `profile_source_not_builtin`。当前宿主的正式 G4 结论为
**BLOCKED**，技术只读链（capability/attempt/attest）保留但不冒充合规 PASS。

**v6.11.0 起命令开销按“一次命令一次快照”收敛**：`sync_runtime.py --apply` 同步后预编译
运行时字节码（文档命令用 `python3 -B`，只禁止写 `.pyc`），单次命令不再重复编译源码；
代码快照、`git_root` 结果在单次命令内复用，工作区只扫描一次；工作区根与 HEAD 合并为一次
`rev-parse`。门禁、漂移检测与证据绑定规则不变。

**v6.0.0 起 SOL 决策通道退役**（v7 只恢复 REVIEW，不恢复 CONSULT/PLAN）：不再有远程模型
决策依赖，不再有等待 SOL 返回的状态门禁，也不新增其他远程大模型充当强制决策节点。历史调用
记录、REVIEW 裁决、任务台账与证据原样保留，可只读查看。角色边界见
`docs/PROJECT-CHARTER.md`。

本仓库是**唯一源码**；`~/.zcode/skills/copilot` 是构建产物，只允许从本仓库单向下发。
旧 Reviewer 全局 Profile 已退役。工作区保留一个**通用只读能力载体**
`.zcode/agents/independent-readonly.md`（Read/Grep/Glob，无 Bash/Write/Edit），
只提供宿主级权限，不定义 Reviewer 身份或复核流程，普通任务不强制使用。按 v6.12 的载体
来源约束，它是 project 级自定义 Agent，**不是正式 G4 载体**；当前宿主正式 G4 仍为 BLOCKED。

## 目录

```text
copilot/
├── AGENTS.md                # 项目执行规则（不分发）
├── SKILL.md                 # Skill 入口与运行契约（分发）
├── scripts/                 # 运行时脚本（分发）
│   ├── governance.py        # 本地治理引擎：合同、状态、阶段、四门、PARTIAL 派生
│   ├── copilot_dispatch.py  # CLI：任务、计划、证据、收口、正式 REVIEW、历史只读
│   ├── copilot_auto.py      # 驱动：drive（推进/工作单/派生）与 ingest（回填信封）
│   ├── sol_review.py        # SOL 零工具单轮复核：请求/解析/审计（唯一远程调用）
│   ├── native_bridge.py     # Native Bridge v2 协议与信封校验（G4）
│   ├── native_agent.py      # 从宿主运行记录生成正式执行/复核信封
│   ├── agent_policy.py      # Agent 调度建议与宿主权限能力判定（只读纯函数）
│   ├── bridge_evidence.py   # 证据采集：脱敏、截断标记、清单
│   └── lib_common.py        # 公共工具
├── references/              # 协议与操作参考（分发）
├── .zcode/agents/independent-readonly.md # 通用只读权限 Profile（不分发）
├── legacy/                  # v5 SOL 实现与历史测试（不分发，仅追溯）
├── tests/                   # 当前开发测试（不分发）
├── docs/                    # 项目管理文档（不分发）
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
- G4 门禁保持 fail-closed；SOL 复核与普通原生 Reviewer 的结论都不等于 G4。
- 不在源码仓库或运行时分发物中保存 API Key、凭据、真实审计日志或临时任务状态。
- 不删除历史审计与证据；历史材料只读保留。

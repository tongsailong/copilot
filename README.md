# Copilot × ZCode 原生 Agent 协作系统

Copilot 是运行于 ZCode 内的**轻量协作治理 Skill**：用户侧 ChatGPT 负责需求与产品层目标、
交接与验收；ZCode 原生 Agent 是工程执行与技术决策主体，负责现场调查、规划、实现、调试、
测试与验证；Copilot 负责任务合同、完成条件、状态、证据、审计与收口门禁，并用 PARTIAL 派生
持续推进任务；原生只读 Reviewer 承担独立复核与高风险 G4。

**v6.0.0 起 SOL（GPT-6.1-SOL）决策通道退役**：不再有远程模型决策依赖，不再有等待 SOL
返回的状态门禁，也不新增其他远程大模型充当强制决策节点。历史调用记录、REVIEW 裁决、任务
台账与证据原样保留，可只读查看。角色边界见 `docs/PROJECT-CHARTER.md`。

本仓库是**唯一源码**；`~/.zcode/skills/copilot` 与 `~/.zcode/agents/reviewer.md` 是构建
产物，只允许从本仓库单向下发。

## 目录

```text
copilot/
├── AGENTS.md                # 项目执行规则（不分发）
├── SKILL.md                 # Skill 入口与运行契约（分发）
├── scripts/                 # 运行时脚本（分发）
│   ├── governance.py        # 本地治理引擎：合同、状态、阶段、四门、PARTIAL 派生
│   ├── copilot_dispatch.py  # CLI：任务、计划、证据、收口、正式 REVIEW、历史只读
│   ├── copilot_auto.py      # 驱动：drive（推进/工作单/派生）与 ingest（回填信封）
│   ├── native_bridge.py     # Native Bridge v2 协议与信封校验
│   ├── native_agent.py      # 从宿主运行记录生成正式执行/复核信封
│   ├── bridge_evidence.py   # 证据采集：脱敏、截断标记、清单
│   └── lib_common.py        # 公共工具
├── references/              # 协议与操作参考（分发）
├── agents/reviewer.md       # Reviewer Profile（分发到 ~/.zcode/agents/）
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

- 分发：`SKILL.md`、`scripts/**/*.py`、`references/**/*.md`、`agents/reviewer.md`。
- 不分发：`legacy/`、`tests/`、`docs/`、`manifests/`、`scripts-sync/`、`README.md`、`AGENTS.md`。
- 永不同步：`.env*`、密钥/凭据、`.audit/` 审计日志、临时状态、`__pycache__`、符号链接。
- 每次运行同时检查 Skill 与 Reviewer，避免两者版本不一致。
- 同步写入使用同目录临时文件 + `os.replace` 原子替换，写后重新校验 SHA-256。

## 安全与授权

- Copilot 命令不是授权：commit、push、发布、部署、付费、生产数据与 Provider/安全策略修改
  仍须用户明确授权。
- 不修改 Provider、模型、路由、CPA 配置；不新增付费通道。
- G4 门禁保持 fail-closed；普通原生 Reviewer 的 PASS 不等于 G4。
- 不在源码仓库或运行时分发物中保存 API Key、凭据、真实审计日志或临时任务状态。
- 不删除历史审计与证据；历史材料只读保留。

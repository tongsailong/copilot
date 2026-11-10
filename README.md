# Copilot × ZCode 原生 Agent 协作系统

Copilot 是运行于 ZCode 内的协作治理 Skill：用户侧 ChatGPT 负责需求与产品层目标、交接与验收；
ZCode 原生 Agent 是工程执行主体，负责现场调查、规划、执行与调度；SOL（GPT-6.1-SOL）是 Copilot
按判据进入的高级工程决策通道，按需在授权范围内承担高级工程判断与工作安排；Executor 子代理（继承
主代理模型）负责工程实施，独立 Reviewer 负责复核，Copilot 状态层负责合同、轮次、证据与门禁。
角色边界见 `docs/PROJECT-CHARTER.md`。本仓库是**唯一源码**；
`~/.zcode/skills/copilot` 与 `~/.zcode/agents/reviewer.md` 是构建产物，只允许从本仓库单向下发。

## 目录

```text
copilot/
├── AGENTS.md                # 项目执行规则（不分发）
├── SKILL.md                 # Skill 入口与运行契约（分发）
├── scripts/                 # 运行时脚本（分发）
│   ├── call_gpt.py          # CPA / Responses API 调用与审计
│   ├── copilot_dispatch.py  # 状态机、合同、轮次、四门、收口
│   ├── copilot_auto.py      # 自动协作驱动（drive / 证据包 / 回填）
│   ├── native_bridge.py     # Native Bridge 协议与信封校验
│   ├── native_agent.py      # 从宿主运行记录生成正式执行/复核信封
│   ├── bridge_evidence.py   # 证据包整理
│   ├── review_ledger.py     # 复核台账
│   ├── reply_protocol.py    # 回复协议与已消费回复恢复
│   └── lib_common.py        # 公共工具
├── references/              # 协议与角色参考（分发）
├── agents/reviewer.md       # Reviewer Profile（分发到 ~/.zcode/agents/）
├── tests/                   # 开发测试（不分发）
├── docs/                    # 项目管理文档（不分发）
├── manifests/               # 源码 SHA-256 基线（不分发）
└── scripts-sync/            # 源码→运行时同步工具（不分发）
```

## 开发方式

1. 只修改本仓库；不直接编辑运行时副本。
2. 运行测试与代码检查：

   ```bash
   cd /media/vdb/projects/copilot
   python3 -B -m unittest discover -s tests -p 'test_*.py'
   ```

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
- 不分发：`tests/`、`docs/`、`manifests/`、`scripts-sync/`、`README.md`、`AGENTS.md`。
- 永不同步：`.env*`、密钥/凭据、`.audit/` 审计日志、临时状态、`__pycache__`、符号链接。
- 每次运行同时检查 Skill 与 Reviewer，避免两者版本不一致。
- 同步写入使用同目录临时文件 + `os.replace` 原子替换，写后重新校验 SHA-256。

## 安全与授权

- 真实 GPT/SOL 调用、commit、push、发布、部署均需单独授权。
- G4 门禁保持 fail-closed；普通原生 Reviewer 的 PASS 不等于 G4。
- 不在源码仓库或运行时分发物中保存 API Key、凭据、真实审计日志或临时任务状态。

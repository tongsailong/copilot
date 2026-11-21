# 贡献指南

欢迎 Issue 与 Pull Request。本项目是一个**治理层**，正确性优先于功能数量：状态机、门禁与证据
绑定属于安全属性，改动需要比普通工具更严格的验证。

## 环境

- Python 3.10+（验证环境 3.12），仅标准库；可选 `Pillow`（图片证据完整性校验）。
- POSIX 环境（脚本使用 `fcntl`）。
- 不需要 ZCode 宿主即可运行 CLI 与测试；宿主只在验证 Skill 加载与钩子时需要。

## 本地流程

```bash
git clone https://github.com/tongsailong/copilot.git
cd copilot
python3 -m compileall -q scripts scripts-sync tests              # 编译检查
python3 -B -m unittest discover -s tests -p 'test_*.py'          # 当前套件（CI 门禁，基线 849 项）
python3 -B scripts-sync/sync_runtime.py --verify-baseline        # manifest / baseline 只读校验
python3 -B -m unittest discover -s legacy/tests -p 'test_*.py'   # 历史回归（已知非全绿，非门禁）
```

改动源码后必须单向下发并复查（运行时副本 `~/.zcode/skills/copilot` 是构建产物，不要直接编辑）：

```bash
python3 -B scripts-sync/sync_runtime.py --check
python3 -B scripts-sync/sync_runtime.py --apply
python3 -B scripts-sync/sync_runtime.py --check          # 期望：缺失 0、不同 0
python3 -B scripts-sync/sync_runtime.py --baseline       # 分发源码变化后重建基线
python3 -B scripts-sync/sync_runtime.py --verify-baseline
```

## 改动规则

1. **最小范围**：只改与本议题相关的文件，不顺手重构或格式化无关内容。
2. **测试先行**：新增行为要有对应测试；不为通过而放宽断言、删除测试或跳过用例。
3. **不改安全语义**：`EXIT_*` 退出码、门禁 fail-closed、证据绑定、授权边界、SOL advisory 性质
   都属于对外契约，修改需在 Issue 中说明理由与影响。
4. **不引入外部依赖**：运行时保持标准库；确需第三方库时必须是可选依赖，并在缺失时**拒绝**
   相关能力而不是放行。
5. **不改历史**：历史审计、证据与只读台账只读保留；退役模块归档在 `legacy/`。
6. **不提交**：凭据、`.env*`、`.audit/`、`.copilot-tasks/`、运行时状态、数据库、`__pycache__`。

## 持续集成与 PR 验收

PR 指向 `main` 时，`.github/workflows/ci.yml` 会在 Python **3.10 / 3.12** 上运行编译检查、
`tests/` 活动套件与 `--verify-baseline`。合并前这些检查必须通过；本地通过不替代 CI 结果。

- **PR 前自查**：跑通上节前三条命令；新增行为补对应测试；不降低断言强度。
- **不在 PR 中引入凭据、任务状态或运行数据**：`.copilot-tasks/`、`.audit/`、数据库与备份均不提交。
- **CI 不会调用 SOL**：工作流不含 Secrets，也不访问 Provider；本地 SOL 配置与 CI 完全隔离。
- **fork PR 与主仓库使用同一套只读检查**：不需要在 PR 中请求额外权限或私有 runner。
- **`legacy/tests` 不参与门禁**：它保留 v5 契约，已知有失败；不要在 PR 中为“让它变绿”而
  改动历史断言。

## 提交

- 只暂存明确路径（不要 `git add .`），提交前检查 `git diff --cached` 不含秘密或任务外改动。
- 提交信息说明「改了什么 + 为什么 + 验证方式」。本项目沿用 `feat(copilot):`、`fix(copilot):`、
  `docs(copilot):` 前缀。
- 不要在 PR 中混入无关重构。

## 文档

- 长期决策 → `docs/DECISIONS.md`；当前状态与证据 → `docs/STATUS.md`；
  执行规则 → `AGENTS.md`；Skill 用法 → `SKILL.md`；协议细节 → `references/`。
- 同一规则只保留一处权威定义，其他位置引用而不是复制。

## 许可证

通过 PR 提交的贡献按 [MIT](LICENSE) 授权。

# 安全策略

## 报告安全问题

请**不要**用公开 Issue 披露漏洞细节。

仓库的 **Private vulnerability reporting 当前未启用**（2026-11-21 核实：GitHub API
`repos/tongsailong/copilot/private-vulnerability-reporting` 返回 `enabled=false`），因此
Security → Advisories → Report a vulnerability 入口目前不可用。在此之前：

1. 先在公开 Issue 中**只**说明「需要一个私下报告渠道」，不要写影响面、复现步骤或利用细节；
2. 等待维护者启用 Private vulnerability reporting 或提供其它私下渠道后再提交完整报告。

报告请附：影响范围、复现步骤、版本（`copilot_dispatch.py --version`）与必要的脱敏日志片段。
修复发布前请不要公开细节。本项目不承诺响应时间或 SLA，以维护者的实际回复为准。

## 适用范围

本项目是一个**治理层**：它记录任务状态、判定门禁、调用可选的认知模型。安全相关面主要有四类。

### 1. 授权边界（设计上的安全属性）

- Copilot 命令**不构成授权**：commit、push、发布、部署、付费、生产数据变更、Provider 与安全策略
  修改都必须由用户显式授权。
- 门禁 **fail-closed**：证据缺失、绑定不符、门禁未过时一律失败关闭，不接受“看起来合理”。
- 认知结论是 advisory，不构成证据、门禁或授权；采纳与否由主 Agent 记录并负责。

若你发现某个命令能绕过上述边界（例如未经授权的写入、门禁可被空值/伪造输入放行、
历史记录可被静默改写），请按上面流程报告——这类问题优先级最高。

### 2. 凭据与网络

- 仓库与分发包中**不保存**任何凭据、Token、私钥或审计日志；`.env*`、`*.pem`、`*.key`、
  `.audit/`、`.copilot-tasks/`、`*.sqlite` 等均在 `.gitignore` 中排除。
- 唯一的远程调用是认知（`scripts/cognition_call.py`）：只允许 **本机回环** 出口
  （`127.0.0.1` / `localhost` / `::1`，默认端口 `8317`），拒绝带 userinfo、query 或额外路径的端点；
  凭据只从本机既有 ZCode Provider 配置读取，不落盘、不回显、不进入日志。
- 失败不重试、不切换 Provider、不静默降级模型或推理强度；上游回显与请求不一致即拒绝消费。

### 3. 本机数据

- 任务状态、证据与认知原始输出写在项目工作区的 `.copilot-tasks/`，不写入仓库。
- 证据采集会脱敏与截断，并在文件型证据消费时重读校验 SHA-256。
- 图片证据需要 `Pillow` 做完整性校验；缺失时**拒绝**该证据，而不是放行未校验内容。

### 4. 钩子

- `copilot_stop_guard.py` 的 `SessionStart` / `UserPromptSubmit` / `Stop` 钩子**只读**任务状态，
  不写状态、不消费续跑计数，异常时退出码 0（不阻断宿主）。
- `install` 会修改**用户级** hook 配置，属全局配置变更，需你自行确认后执行；`uninstall` 可回退。

### 5. CI 与外部贡献

- 工作流 `.github/workflows/ci.yml` 使用普通 `pull_request` 事件（**不用** `pull_request_target`），
  顶层权限只有 `contents: read`，`actions/checkout` 关闭凭据持久化。
- 不引用任何 Secrets，不调用认知，不访问本机 Provider、知识库、运行时目录或私有 runner；
  fork PR 的代码与主仓库 PR 在同一套无特权托管 runner 上运行。
- Action 固定到完整 commit SHA；CI 只做编译、活动测试与 baseline 只读校验，不执行
  `sync_runtime.py --apply`，不提交、不推送、不发布。
- CI 通过不代表发布授权；发布仍由维护者显式执行。

## 支持的版本

安全修复只针对最新发布版本。当前：**v9.6.1**（`main`）。

## 不在范围内

- 你的 ZCode Provider、模型账户、代理与网络配置本身的安全。
- 你在 `.copilot-tasks/` 与本机知识库中存放的内容（请自行控制访问权限）。

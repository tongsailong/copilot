# 安全策略

## 报告安全问题

请**不要**用公开 Issue 报告安全漏洞。使用 GitHub 的
[Private vulnerability reporting](https://github.com/tongsailong/copilot/security/advisories/new)
（仓库 **Security → Advisories → Report a vulnerability**）提交，并附上：影响范围、复现步骤、
版本（`copilot_dispatch.py --version`）与必要的日志片段（请先脱敏）。

维护者会在确认后回复处理计划。修复发布前请不要公开细节。

## 适用范围

本项目是一个**治理层**：它记录任务状态、判定门禁、调用可选的认知模型。安全相关面主要有四类。

### 1. 授权边界（设计上的安全属性）

- Copilot 命令**不构成授权**：commit、push、发布、部署、付费、生产数据变更、Provider 与安全策略
  修改都必须由用户显式授权。
- 门禁 **fail-closed**：证据缺失、绑定不符、门禁未过时一律失败关闭，不接受“看起来合理”。
- SOL 结论是 advisory，不构成证据、门禁或授权；采纳与否由主 Agent 记录并负责。

若你发现某个命令能绕过上述边界（例如未经授权的写入、门禁可被空值/伪造输入放行、
历史记录可被静默改写），请按上面流程报告——这类问题优先级最高。

### 2. 凭据与网络

- 仓库与分发包中**不保存**任何凭据、Token、私钥或审计日志；`.env*`、`*.pem`、`*.key`、
  `.audit/`、`.copilot-tasks/`、`*.sqlite` 等均在 `.gitignore` 中排除。
- 唯一的远程调用是 SOL（`scripts/sol_review.py`）：只允许 **本机回环** 出口
  （`127.0.0.1` / `localhost` / `::1`，默认端口 `8317`），拒绝带 userinfo、query 或额外路径的端点；
  凭据只从本机既有 ZCode Provider 配置读取，不落盘、不回显、不进入日志。
- 失败不重试、不切换 Provider、不静默降级模型或推理强度；上游回显与请求不一致即拒绝消费。

### 3. 本机数据

- 任务状态、证据与 SOL 原始输出写在项目工作区的 `.copilot-tasks/`，不写入仓库。
- 证据采集会脱敏与截断，并在文件型证据消费时重读校验 SHA-256。
- 图片证据需要 `Pillow` 做完整性校验；缺失时**拒绝**该证据，而不是放行未校验内容。

### 4. 钩子

- `copilot_stop_guard.py` 的 `SessionStart` / `UserPromptSubmit` / `Stop` 钩子**只读**任务状态，
  不写状态、不消费续跑计数，异常时退出码 0（不阻断宿主）。
- `install` 会修改**用户级** hook 配置，属全局配置变更，需你自行确认后执行；`uninstall` 可回退。

## 支持的版本

安全修复只针对最新发布版本。当前：**v9.6.1**（`main`）。

## 不在范围内

- 你的 ZCode Provider、模型账户、代理与网络配置本身的安全。
- 你在 `.copilot-tasks/` 与本机知识库中存放的内容（请自行控制访问权限）。

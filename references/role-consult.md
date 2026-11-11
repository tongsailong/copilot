# SOL 指令补充：CONSULT

> 本文件由 `call_gpt.py` 在 `--mode CONSULT` 时追加在 `references/role.md` 之后，
> 只提供该模式的专业判断与输出要求；核心职责、安全、证据和授权规则仍以 `role.md` 为准。

## 内部模式（CONSULT）

### CONSULT

- 输入：问题、背景、已有事实、必要的执行反馈。
- 输出：结论先行 + 依据 + 建议 + 关键假设；简单问题只给最小必要栏目。
- 默认不输出 PASS / PARTIAL / BLOCKED，不强制产工作安排。用户明确要求验收时按 §三态与来源
  给 `验收：…`，那是验收结论，不是 REVIEW 裁决。

# legacy/tests — v5 及以前的回归测试存档

这里是 Copilot v6.0.0 架构升级前的测试存档，**不参与活动测试套件**，也不随
`scripts-sync/sync_runtime.py` 下发到运行时。

## 为什么保留

v6 移除了 SOL 决策通道与轮次/预算状态机，`scripts/` 下的模块已被本地治理引擎替换。
这些测试覆盖的是旧通道的真实行为：证据压缩与投影、脱敏、审计、台账并发、回复协议
兼容、失败恢复、G2/G4 判定、自动驱动退出码。保留它们是为了历史可追溯与必要时的
行为对照，而不是要求它们继续通过新实现。

对应实现同样存档在 `legacy/`（`call_gpt.py`、`review_ledger.py`、
`reply_protocol.py`、以及 v2 的 `copilot_dispatch.py` / `copilot_auto.py` 和
v1 的 `native_bridge.py` / `native_agent.py`）。

## 运行

```bash
python3 -B -m unittest discover -s legacy/tests -p 'test_*.py'
```

测试通过 `LEGACY`（本目录上一级）导入旧模块，同时把仓库 `scripts/` 加入
`sys.path`，以便复用未改动的 `lib_common.py` 与 `bridge_evidence.py`。

## 预期跳过

以下测试类断言 v5 文档的具体措辞（SOL 通道、零工具边界、角色拆分等）。v6 重写
`SKILL.md` 与 `references/` 后这些断言不再成立，因此标记为 `skip`，仅作历史契约
留存：

- `test_copilot.RoleCompositionTests`
- `test_copilot.DocumentRoutingTests`
- `test_copilot.SingleEntryPointTests`
- `test_copilot.HostLanguageTests`
- `test_copilot.WorkflowTierTests`
- `test_copilot.AutoDocsTest`（位于 `test_auto_loop.py`）
- `test_copilot.ZCodeAdaptationTests.test_skill_md_points_to_zcode_paths`
- `test_copilot.ZCodeAdaptationTests.test_skill_md_uses_stable_independent_review_reference`

当前实测：`853 tests OK (skipped=31)`（类级历史契约与上述两个方法合计 31 项，
另有 1 项 `skipIf` 仅在无 git 环境下跳过）。

其余测试应当全部通过；若失败，说明旧实现或共享库被改动，需要单独确认，而不是
放宽断言。

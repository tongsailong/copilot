#!/usr/bin/env python3
"""v6.1 复核收敛：只读策略、宿主派生、增量复核、rerun 与合同提醒。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import copilot_auto as auto  # noqa: E402
import governance as G  # noqa: E402
import native_agent as NA  # noqa: E402
import native_bridge as NB  # noqa: E402
import v6_support as F  # noqa: E402

CONTRACT = """## 完成条件
- 产出可复核的结果文件
"""


class CodeAssertions(unittest.TestCase):
    def assert_code(self, code: str, func, *args, **kwargs) -> None:
        with self.assertRaises(NB.BridgeError) as ctx:
            func(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))




class RerunTests(F.BridgeBase):
    def _save_dispatch(self) -> None:
        state = G.load_state(self.state_path)
        state["native_dispatch"] = self.dispatch
        G.save_state(self.state_path, state)

    def test_rerun_allows_new_result_for_same_work_item(self):
        self._save_dispatch()
        first = self.execution(result="旧结果")
        auto._ingest_execution(self.state_path, first)
        second = self.execution(result="修复后的新结果")
        self.assert_code("work_item_terminal", NB.validate_execution_result,
                         second, self.dispatch, G.load_state(self.state_path))
        G.rerun_action(self.state_path, "A1", reason="修复后重新验证")
        state = G.load_state(self.state_path)
        action = next(a for a in state["actions"] if a["id"] == "A1")
        self.assertEqual(action["status"], "pending")
        self.assertEqual(action["rerun_count"], 1)
        self.assertEqual(action["rerun_history"][0]["status"], "done")
        self.assertIn("旧结果", action["rerun_history"][0]["result"] or "")
        auto._ingest_execution(self.state_path, second)
        state = G.load_state(self.state_path)
        action = next(a for a in state["actions"] if a["id"] == "A1")
        self.assertEqual(action["status"], "done")
        self.assertIn("修复后的新结果", action["result"])


class ContractAmendmentTests(unittest.TestCase):
    def test_amend_while_blocked_and_source_drift_is_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            contract_file = os.path.join(tmp, "contract.md")
            with open(contract_file, "w", encoding="utf-8") as fh:
                fh.write(CONTRACT)
            G.start_state(path, goal="目标", contract=CONTRACT,
                          contract_source=contract_file)
            G.add_block(path, reason="等待用户决策", needs="用户确认")
            # BLOCKED 下允许修订合同，但不会解除阻塞。
            G.amend_contract(path, contract=CONTRACT, contract_source=contract_file)
            state = G.load_state(path)
            self.assertEqual(G.evaluate(state)["phase"], "BLOCKED")
            self.assertEqual(G.evaluate(state).get("warnings") or [], [])
            with open(contract_file, "w", encoding="utf-8") as fh:
                fh.write(CONTRACT + "\n- 新增条件\n")
            warnings = G.evaluate(state).get("warnings") or []
            self.assertTrue(any("合同源文件已变更" in item for item in warnings),
                            warnings)
            self.assertEqual(G.evaluate(state)["phase"], "BLOCKED")
            # 重新登记后提醒消失，源摘要更新。
            with open(contract_file, encoding="utf-8") as fh:
                updated = fh.read()
            G.amend_contract(path, contract=updated, contract_source=contract_file)
            self.assertEqual(
                G.evaluate(G.load_state(path)).get("warnings") or [], [])








if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""T4 隔离验收运行器（手动执行，不被 test_*.py 自动发现）。

只使用临时目录与本地夹具，不读写生产任务台账/运行时；输出 JSON 报告。
用法：python3 -B tests/t4_acceptance.py [--report /tmp/copilot-t4-report.json]

v8.0.0 起 G4 原生只读复核已退役：验收场景改为覆盖执行绑定、代码快照、
合同策略、G1–G3 门禁、G4 历史只读与 SOL advisory。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

LAYERS = {
    "T0": ["tests.test_docs_v6", "tests.test_architecture"],
    "T1": ["tests.test_host_records", "tests.test_execution_binding",
           "tests.test_code_scope_unified", "tests.test_profile_policy",
           "tests.test_sync_runtime", "tests.test_agent_policy"],
    "T2": ["discover", "-s", "tests", "-p", "test_*.py"],
    "T3": ["discover", "-s", "legacy/tests", "-p", "test_*.py"],
}

# 场景 → 覆盖它的定向测试（真实执行，非文字声明）。
SCENARIOS = {
    "happy_execution_binding": [
        "tests.test_execution_binding.ExecutionBindingFlowTests."
        "test_binding_flows_from_creation_to_consumption"],
    "binding_replay": [
        "tests.test_execution_binding.ExecutionBindingFlowTests."
        "test_consumed_binding_cannot_be_reused"],
    "execution_envelope_tamper": [
        "tests.test_cli_v6.NativeCliTests."
        "test_tampered_execution_envelope_is_rejected"],
    "native_execution_flow": [
        "tests.test_cli_v6.NativeCliTests."
        "test_native_flow_ingests_execution_and_closes_without_review"],
    "submodule_dirty": [
        "tests.test_code_scope_unified.CodeScopeUnifiedTests."
        "test_submodule_dirty_tracked_change_changes_digest"],
    "undeclared_nested_repo": [
        "tests.test_code_scope_unified.CodeScopeUnifiedTests."
        "test_undeclared_nested_repo_dirty_is_included"],
    "contract_layer_cannot_be_lowered": [
        "tests.test_execution_binding.ContractPolicyTests."
        "test_contract_commands_and_layers_cannot_be_lowered",
        "tests.test_execution_binding.ContractPolicyTests."
        "test_tampered_binding_policy_is_rejected"],
    "contract_changed": [
        "tests.test_execution_binding.ExecutionBindingFlowTests."
        "test_contract_change_invalidates_binding"],
    "test_policy_cli": [
        "tests.test_execution_binding.TestPolicyCliTests."
        "test_start_and_amend_accept_test_policy"],
    "g4_retired_high_risk_closes": [
        "tests.test_governance.HighRiskRetirementTests."
        "test_high_risk_closes_without_native_review",
        "tests.test_governance.HighRiskRetirementTests."
        "test_high_risk_accepts_legacy_backend"],
    "historical_g4_records_do_not_gate": [
        "tests.test_governance.HighRiskRetirementTests."
        "test_historical_g4_records_do_not_gate"],
    "finding_ledger_gate_all_risks": [
        "tests.test_governance.HighRiskRetirementTests."
        "test_unresolved_blocking_finding_blocks_high_risk_closure",
        "tests.test_governance.HighRiskRetirementTests."
        "test_verified_finding_no_longer_blocks"],
    "no_auto_g4_derivation": [
        "tests.test_native_agent_v6.NoG4DerivationTests."
        "test_historical_capability_gap_never_derives_g4_work",
        "tests.test_native_agent_v6.NoG4DerivationTests."
        "test_repeated_historical_g4_actions_do_not_trigger_replan"],
    "native_review_envelope_retired": [
        "tests.test_review_efficiency.NativeReviewRetiredTests."
        "test_native_review_envelope_is_retired",
        "tests.test_review_efficiency.NativeReviewRetiredTests."
        "test_legacy_review_envelope_is_retired"],
    "sol_review_is_advisory": [
        "tests.test_sol_review_v7.StateIntegrationTests."
        "test_sol_review_is_advisory_not_a_closure_gate",
        "tests.test_sol_review_v7.AdmissionTests.test_tiers"],
    "sol_zero_tool_single_round": [
        "tests.test_sol_review_v7.ZeroToolTests."
        "test_run_review_sends_exactly_one_zero_tool_request",
        "tests.test_sol_review_v7.SingleRoundTests."
        "test_same_materials_reuse_existing_review"],
    "reviewer_profile_retired": [
        "tests.test_profile_policy.WorkspaceProfileTests."
        "test_legacy_reviewer_profile_is_gone",
        "tests.test_profile_policy.WorkspaceProfileTests."
        "test_agents_dir_has_only_the_generic_profile",
        "tests.test_sync_runtime.RealDistributionTests."
        "test_v6_modules_are_distributed_and_legacy_is_not"],
    "agent_dispatch_advice": [
        "tests.test_agent_policy.DispatchAdviceTests.test_default_is_main",
        "tests.test_agent_policy.AgentPolicyCLITests."
        "test_g4_required_flag_is_removed"],
}


def run_suite(names: list[str]) -> dict:
    start = time.time()
    command = [sys.executable, "-B", "-m", "unittest", *names]
    proc = subprocess.run(command,
                          cwd=ROOT, capture_output=True, text=True)
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-6:]
    return {"ok": proc.returncode == 0,
            "seconds": round(time.time() - start, 1),
            "tail": tail}


def run_names(names: list[str]) -> dict:
    start = time.time()
    proc = subprocess.run([sys.executable, "-B", "-m", "unittest", *names],
                          cwd=ROOT, capture_output=True, text=True)
    return {"ok": proc.returncode == 0, "seconds": round(time.time() - start, 1),
            "tail": (proc.stderr or proc.stdout).strip().splitlines()[-3:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=os.path.join(
        tempfile.gettempdir(), "copilot-t4-report.json"))
    args = ap.parse_args()
    report: dict = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "isolated": True, "layers": {}, "scenarios": {}}
    for layer, names in LAYERS.items():
        report["layers"][layer] = run_suite(names)
    for name, tests in SCENARIOS.items():
        report["scenarios"][name] = run_names(tests)
    report["all_layers_ok"] = all(item["ok"] for item in
                                  report["layers"].values())
    report["all_scenarios_ok"] = all(item["ok"] for item in
                                     report["scenarios"].values())
    report["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(json.dumps({"report": args.report,
                      "all_layers_ok": report["all_layers_ok"],
                      "all_scenarios_ok": report["all_scenarios_ok"]},
                     ensure_ascii=False))
    return 0 if report["all_layers_ok"] and report["all_scenarios_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""T4 隔离验收运行器（手动执行，不被 test_*.py 自动发现）。

只使用临时目录与本地夹具，不读写生产任务台账/运行时；输出 JSON 报告。
用法：python3 -B tests/t4_acceptance.py [--report /tmp/copilot-t4-report.json]
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
    "T1": ["tests.test_review_output", "tests.test_host_records",
           "tests.test_execution_binding", "tests.test_test_execution",
           "tests.test_code_scope_unified", "tests.test_g4_attest",
           "tests.test_g4_shadow", "tests.test_profile_policy"],
    "T2": ["discover", "-s", "tests", "-p", "test_*.py"],
    "T3": ["discover", "-s", "legacy/tests", "-p", "test_*.py"],
}

# 场景 → 覆盖它的定向测试（真实执行，非文字声明）。
SCENARIOS = {
    "happy_executor_reviewer": [
        "tests.test_execution_binding.ExecutionBindingFlowTests."
        "test_binding_flows_from_creation_to_verified_test_evidence"],
    "executor_old_session_reuse": [
        "tests.test_test_execution.TestExecutionTests."
        "test_session_before_binding_is_failed",
        "tests.test_test_execution.TestExecutionTests."
        "test_prompt_without_nonce_is_failed"],
    "reviewer_old_session_reuse": [
        "tests.test_g4_attest.AttemptBindingTests."
        "test_session_precedes_attempt"],
    "executor_command_mismatch": [
        "tests.test_test_execution.TestExecutionTests."
        "test_binding_command_change_is_failed"],
    "test_exit_nonzero": [
        "tests.test_test_execution.TestExecutionTests."
        "test_nonzero_exit_is_failed"],
    "test_report_failure": [
        "tests.test_test_execution.TestExecutionTests."
        "test_junit_failure_does_not_verify_result",
        "tests.test_test_execution.TestExecutionTests."
        "test_junit_failure_element_without_attribute_is_not_verified"],
    "stale_test_artifact": [
        "tests.test_test_execution.TestExecutionTests."
        "test_artifact_drift_is_failed",
        "tests.test_test_execution.TestExecutionTests."
        "test_stale_artifact_is_failed"],
    "source_change_after_execution": [
        "tests.test_test_execution.ExecutionIngestBindingTest."
        "test_execution_envelope_feeds_verified_test_binding"],
    "reviewer_outside_read": [
        "tests.test_g4_attest.EvidenceBoundaryTests."
        "test_outside_workspace_read_rejected"],
    "reviewer_output_invalid": [
        "tests.test_review_output.ReviewOutputTests."
        "test_fence_outside_text_rejected",
        "tests.test_review_output.ReviewOutputTests."
        "test_duplicate_keys_rejected"],
    "raw_output_missing": [
        "tests.test_g4_attest.HostOutputFallbackTests."
        "test_missing_output_file_and_db_fails_closed"],
    "sqlite_missing": [
        "tests.test_test_execution.TestExecutionTests."
        "test_db_missing_is_unverified"],
    "history_after_session_cleanup": [
        "tests.test_g4_attest.HostOutputFallbackTests."
        "test_db_output_is_used_when_output_file_is_pruned"],
    "attempt_replay": [
        "tests.test_g4_attest.AttemptBindingTests.test_consumed_attempt"],
    "binding_replay": [
        "tests.test_execution_binding.ExecutionBindingFlowTests."
        "test_consumed_binding_cannot_be_reused"],
    "submodule_dirty": [
        "tests.test_code_scope_unified.CodeScopeUnifiedTests."
        "test_submodule_dirty_tracked_change_changes_digest"],
    "undeclared_nested_repo": [
        "tests.test_code_scope_unified.CodeScopeUnifiedTests."
        "test_undeclared_nested_repo_dirty_is_included"],
    "ingest_interrupted": [
        "tests.test_g4_shadow.ShadowIngestIntegrationTests."
        "test_save_failure_leaves_no_partial_consumption"],
    "incremental_baseline_invalid": [
        "tests.test_g4_shadow.ShadowBuildTests."
        "test_incremental_baseline_attestation_tamper_is_rejected"],
    "contract_no_test_not_applicable": [
        "tests.test_test_execution.TestExecutionTests."
        "test_contract_no_test_is_not_applicable",
        "tests.test_g4_shadow.CandidateFinalizeTests."
        "test_contract_no_test_not_applicable_is_eligible"],
    "contract_layer_cannot_be_lowered": [
        "tests.test_execution_binding.ContractPolicyTests."
        "test_contract_commands_and_layers_cannot_be_lowered",
        "tests.test_execution_binding.ContractPolicyTests."
        "test_tampered_binding_policy_is_rejected"],
    "contract_report_missing": [
        "tests.test_test_execution.TestExecutionTests."
        "test_contract_report_missing_is_failed"],
    "contract_artifact_missing": [
        "tests.test_test_execution.TestExecutionTests."
        "test_contract_artifact_missing_is_failed",
        "tests.test_test_execution.TestExecutionTests."
        "test_contract_path_outside_workspace_is_failed",
        "tests.test_execution_binding.ContractPolicyTests."
        "test_tampered_contract_report_artifacts_are_rejected"],
    "contract_not_applicable_accepted": [
        "tests.test_g4_shadow.TestExecutionAcceptanceTests."
        "test_contract_no_test_not_applicable_is_accepted",
        "tests.test_g4_shadow.CandidateFinalizeTests."
        "test_not_applicable_with_contract_artifacts_is_rejected"],
    "contract_amend_after_binding": [
        "tests.test_test_execution.TestExecutionTests."
        "test_contract_changed_after_binding_is_failed",
        "tests.test_g4_shadow.CandidateFinalizeTests."
        "test_binding_contract_changed_blocks_candidate",
        "tests.test_g4_shadow.ShadowBuildTests."
        "test_build_passes_current_contract_to_test_execution"],
    "contract_source_without_current_policy": [
        "tests.test_g4_shadow.TestExecutionAcceptanceTests."
        "test_contract_source_without_current_policy_is_rejected",
        "tests.test_g4_shadow.ShadowBuildTests."
        "test_build_rejects_not_applicable_without_current_policy"],
    "require_binding_by_contract": [
        "tests.test_g4_shadow.RequirementPolicyTests."
        "test_contract_policy_requires_binding_even_for_normal_risk"],
    "stored_findings_tamper": [
        "tests.test_g4_attest.StoredVerificationTests."
        "test_tampered_stored_findings_are_rejected",
        "tests.test_g4_attest.StoredVerificationTests."
        "test_legacy_record_without_findings_detail_is_limited"],
    "test_policy_cli": [
        "tests.test_execution_binding.TestPolicyCliTests."
        "test_start_and_amend_accept_test_policy"],
    "contract_changed": [
        "tests.test_execution_binding.ExecutionBindingFlowTests."
        "test_contract_change_invalidates_binding"],
    "formal_g4_requires_candidate": [
        "tests.test_snapshot_binding.SnapshotBindingTests."
        "test_formal_g4_requires_attested_candidate",
        "tests.test_snapshot_binding.SnapshotBindingTests."
        "test_attested_but_ineligible_is_not_formal"],
    "legacy_g4_source_rejected": [
        "tests.test_snapshot_binding.SnapshotBindingTests."
        "test_legacy_g4_source_is_rejected_by_gate"],
    "reviewer_bash_retired": [
        "tests.test_native_agent_v6.BuildReviewTests."
        "test_non_readonly_tool_is_rejected",
        "tests.test_snapshot_binding.SnapshotBindingTests."
        "test_bash_review_is_rejected"],
    "reviewer_profile_retired": [
        "tests.test_profile_policy.WorkspaceProfileTests."
        "test_legacy_reviewer_profile_is_gone",
        "tests.test_profile_policy.WorkspaceProfileTests."
        "test_agents_dir_has_only_the_generic_profile",
        "tests.test_profile_policy.WorkspaceProfileTests."
        "test_profile_disallows_bash_and_writes",
        "tests.test_sync_runtime.RealDistributionTests."
        "test_v6_modules_are_distributed_and_legacy_is_not"],
}


def run_suite(names: list[str]) -> dict:
    start = time.time()
    if names and names[0] == "discover":
        command = [sys.executable, "-B", "-m", "unittest", *names]
        # discover 是 unittest 子命令，不能与模块名混用
        command = [sys.executable, "-B", "-m", "unittest", *names]
    else:
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

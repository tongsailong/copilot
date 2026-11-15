#!/usr/bin/env python3
"""G4 影子取证：对已生成的复核记录运行 g4_attest，但不改变正式 G4 结论。

边界：

* 只读 metadata / SQLite / attempt / 证据 / Reviewer 输出；不创建 consume marker，
  因此不会提前消费 attempt。
* 取证失败或异常只写成 `g4_attestations` 里的失败记录，不抛给 ingest，也不影响
  旧 G4 的放行结果。
* 记录与复核记录在同一次 state 写入中落盘；同一份复核重试时按确定性 shadow_id
  幂等去重，不覆盖已有取证记录。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
from datetime import datetime, timezone

import code_scope
import g4_attest as A
import governance as G
import test_execution as TE

SHADOW_SCHEMA = "copilot-g4-shadow/v1"
POLICY_VERSION = "g4-shadow/v1"


_canonical = code_scope.canonical_json


def _safe_list(value: object) -> list:
    return value if isinstance(value, list) else []


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _shadow_id(review_sha: str, policy_version: str, status: str,
               attestation_id: str, error_code: str) -> str:
    return hashlib.sha256(_canonical({
        "review_result_sha256": review_sha,
        "policy_version": policy_version,
        "status": status,
        "attestation_id": attestation_id,
        "error_code": error_code,
    }).encode("utf-8")).hexdigest()


def _category(agreement: str, old_eligible: bool = False,
              new_eligible: bool = False,
              old_reasons: list | None = None,
              old_host_reproducible: bool = True) -> str:
    if agreement == "new_looser" and not old_host_reproducible:
        # 旧 Reviewer 的宿主 metadata/rollout 已不可复查：旧结论无法用同一
        # 证据口径复核，不能判定新门禁更宽松。
        return "incomparable"
    if agreement == "new_looser" and old_reasons and all(
            ("rollout" in str(reason) or "host_record" in str(reason))
            for reason in old_reasons):
        # 旧门禁是因为宿主证据已不可用才拒绝，不是同一证据口径下的放行差异。
        return "incomparable"
    return {"consistent": "equivalent",
            "new_stricter": "new_stricter",
            "new_looser": "new_looser"}.get(agreement, "incomparable")


def _require_binding(state: dict) -> bool:
    """高风险或合同冻结了测试策略时，TE 必须绑定 execution binding。"""
    policy = (state.get("contract") or {}).get("test_policy") or {}
    return str(state.get("risk") or "").lower() == "high" or bool(
        policy.get("captured"))


def _test_execution_acceptable(test_execution: dict,
                               binding: dict | None,
                               state: dict | None = None) -> bool:
    """TE 可接受：verified，或当前合同显式不要求测试时的 not_applicable。"""
    status = str((test_execution or {}).get("status") or "")
    return status == "verified" or (
        status == "not_applicable"
        and G.contract_no_test_binding(binding, state))


def _summary(record: dict) -> dict:
    """抽取持久化所需字段；保留完整检查项与拒绝原因。"""
    evidence = []
    for item in record.get("evidence") or []:
        if isinstance(item, dict):
            evidence.append({"id": item.get("id", ""),
                             "path": item.get("path", ""),
                             "sha256": item.get("sha256", "")})
    return {
        "attestation_id": str(record.get("attestation_id") or ""),
        "schema_version": str(record.get("schema") or ""),
        "attempt_id": str(record.get("review_attempt_id") or ""),
        "target_sha256": str(record.get("target_sha256") or ""),
        "target_source": str(record.get("target_source") or ""),
        "agent_id": str(record.get("agent_id") or ""),
        "child_session_id": str(record.get("child_session_id") or ""),
        "parent_session_id": str(record.get("parent_session_id") or ""),
        "trace_id": str(record.get("trace_id") or ""),
        "turn_id": str(record.get("turn_id") or ""),
        "profile_snapshot": record.get("profile") or {},
        "model_selection": record.get("model_selection") or {},
        "tool_call_count": record.get("total_tool_use_count"),
        "tool_call_ids": list(record.get("tool_call_ids") or []),
        "evidence_sha256": evidence,
        "code_scope_sha256": str(record.get("code_scope_sha256") or ""),
        "verdict": str(record.get("review_verdict") or ""),
        "review_sha256": str(record.get("review_sha256") or ""),
        "review_schema": str(record.get("review_schema") or ""),
        "review_scope": str(record.get("review_scope") or ""),
        "review_findings": record.get("review_findings"),
        "review_findings_detail": list(
            record.get("review_findings_detail") or []),
        "output_sha256": str(record.get("output_sha256") or ""),
        "checks": list(record.get("checks") or []),
        "rejections": list(record.get("rejections") or []),
        "residuals": list(record.get("residual") or []),
        "limitations": list(record.get("limitations") or []),
        "created_at": str(record.get("checked_at") or ""),
    }


def _metadata_path_for(review: dict) -> str:
    """优先 host.metadata_path；缺失时按 Reviewer agentId 在宿主目录定位。

    非 G4 复核信封允许不带 host；影子取证仍可用真实 metadata 校验，而不是
    因为信封省略 host 就跳过。
    """
    host = review.get("host") or {}
    path = str(host.get("metadata_path") or "").strip()
    reviewer = review.get("reviewer") or {}
    agent = str(reviewer.get("agent_id") or "").strip()
    child = str(reviewer.get("child_session_id") or "").strip()
    if path:
        if not agent or not child:
            return path
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return ""
        if isinstance(data, dict) and str(data.get("agentId") or "") == agent \
                and str(data.get("childSessionId") or "") == child:
            return path
        return ""
    if not agent or not child:
        return ""
    root = os.path.expanduser(os.path.join("~", ".zcode", "cli", "agents"))
    for candidate in glob.glob(os.path.join(root, "*", agent, "metadata.json")):
        try:
            with open(candidate, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and str(data.get("agentId") or "") == agent \
                and str(data.get("childSessionId") or "") == child:
            return candidate
    return ""


def _execution_for(state: dict, review: dict) -> dict | None:
    bound = str(review.get("execution_result_sha256") or "")
    for item in _safe_list(state.get("native_results")):
        if isinstance(item, dict) and str(item.get("result_sha256") or "") == bound:
            return item
    return None


def _executor_for(state: dict, review: dict) -> dict:
    execution = _execution_for(state, review) or {}
    return execution.get("executor") or {}


def _incremental_status(review: dict, state: dict,
                        attempt: dict | None) -> dict | None:
    """重放增量基线：原始 attestation、attempt、coverage 与当前变化集。"""
    if str(review.get("review_scope") or "") != "incremental":
        return None
    based_on = str(review.get("based_on_review_sha256") or "")
    problems: list[str] = []
    base = None
    for item in _safe_list(state.get("native_reviews")):
        if isinstance(item, dict) and \
                str(item.get("result_sha256") or "") == based_on:
            base = item
            break
    base_attempt = None
    if isinstance(base, dict):
        for item in _safe_list(state.get("review_attempts")):
            if isinstance(item, dict) and str(
                    item.get("review_attempt_id") or "") == str(
                    base.get("review_attempt_id") or ""):
                base_attempt = item
                break
    attestations = [item for item in _safe_list(state.get("g4_attestations"))
                    if isinstance(item, dict) and str(
                        item.get("review_result_sha256") or "") == based_on]
    attestation = (attestations[-1].get("attestation") or {}) \
        if len(attestations) == 1 else {}
    shadow_eligible = bool(
        len(attestations) == 1
        and attestations[0].get("status") == "attested"
        and attestations[0].get("new_g4_eligible") is True)
    if not based_on:
        problems.append("baseline_sha_missing")
    elif base is None:
        problems.append("baseline_missing")
    else:
        if base.get("g4_eligible") is not True and not shadow_eligible:
            problems.append("baseline_not_g4_eligible")
        if str(base.get("verdict") or "").lower() != "pass":
            problems.append("baseline_not_pass")
        if str(base.get("task_id") or "") != str(state.get("task_id") or ""):
            problems.append("baseline_cross_task")
        if str(base.get("contract_sha256") or "") != str(
                state.get("contract_sha256") or ""):
            problems.append("baseline_contract_changed")
        if not isinstance(base.get("coverage"), dict) or not base["coverage"]:
            problems.append("baseline_coverage_missing")
        if not isinstance(base_attempt, dict):
            problems.append("baseline_attempt_missing")
        else:
            if not base_attempt.get("consumed"):
                # 未消费的基线 attempt 无法证明该基线复核确实按一次性规则
                # 放行过；影子 attestation 不能替代消费记录，否则等于给
                # 未消费的旧输出补发信任。此时要求调用方做完整复核。
                problems.append("baseline_attempt_not_consumed")
            elif str(base_attempt.get("consumed_by_result_sha256") or "") \
                    != based_on:
                problems.append("baseline_attempt_consumed_by_other")
            base_scope = str(base_attempt.get("code_scope_sha256") or "")
            current_scope = str((attempt or {}).get("code_scope_sha256") or "")
            if not base_scope:
                problems.append("baseline_code_scope_missing")
            elif base_scope != current_scope:
                problems.append("incremental_code_changed")
        if len(attestations) == 0:
            problems.append("baseline_attestation_missing")
        elif len(attestations) > 1:
            problems.append("baseline_attestation_duplicate")
        else:
            summary = attestation
            if str(summary.get("schema_version") or "") != A.SCHEMA:
                problems.append("baseline_attestation_schema")
            if not str(summary.get("attestation_id") or ""):
                problems.append("baseline_attestation_id_missing")
            if str(summary.get("attempt_id") or "") != str(
                    base.get("review_attempt_id") or ""):
                problems.append("baseline_attestation_attempt_mismatch")
            base_target = str(base.get("target_sha256")
                              or (base_attempt or {}).get("target_sha256") or "")
            if str(summary.get("target_sha256") or "") != base_target:
                problems.append("baseline_attestation_target_mismatch")
            if str(summary.get("target_source") or "") != "state":
                # 基线 attestation 必须用当前状态摘要复核过 target；用信封
                # 自报 target 自证的记录不能作为增量基线。
                problems.append("baseline_attestation_target_self_reported")
            if str(summary.get("code_scope_sha256") or "") != str(
                    (base_attempt or {}).get("code_scope_sha256") or ""):
                problems.append("baseline_attestation_code_scope_mismatch")
            if str(summary.get("verdict") or "").lower() != str(
                    base.get("verdict") or "").lower():
                problems.append("baseline_attestation_verdict_mismatch")
            if summary.get("rejections"):
                problems.append("baseline_attestation_rejections")
            checks = [item for item in summary.get("checks") or []
                      if isinstance(item, dict)]
            if checks and any(not item.get("passed") for item in checks):
                problems.append("baseline_attestation_failed_check")
            reviewer = base.get("reviewer") or {}
            if str(summary.get("agent_id") or "") != str(
                    reviewer.get("agent_id") or ""):
                problems.append("baseline_attestation_agent_mismatch")
            if str(summary.get("child_session_id") or "") != str(
                    reviewer.get("child_session_id") or ""):
                problems.append("baseline_attestation_session_mismatch")
            if not str(summary.get("parent_session_id") or ""):
                problems.append("baseline_attestation_parent_missing")
            evidence_att = [item for item in summary.get("evidence_sha256") or []
                            if isinstance(item, dict)]
            coverage = (base or {}).get("coverage") or {}
            base_evidence = coverage.get("evidence") or {}
            for item in evidence_att:
                eid = str(item.get("id") or "")
                recorded = str(item.get("sha256") or "")
                if eid and recorded:
                    current = str(base_evidence.get(eid) or "")
                    if current and not current.startswith(recorded + ":"):
                        problems.append(f"baseline_attestation_evidence:{eid}")
        # 覆盖完整性：变化证据必须逐条列入 manifest（允许超集，禁止未知 id）；
        # 代码/条件/其他工作项不得有未覆盖变化；无任何变化时必须复用基线结论，
        # 不能当作增量复核。否则退回完整复核。
        try:
            import native_bridge as NB
            coverage = base.get("coverage") or {}
            current = NB.review_coverage(state)
            manifest = [str(item) for item in
                        (review.get("evidence_manifest")
                         or (attempt or {}).get("evidence_manifest") or [])]
            base_ev = coverage.get("evidence") or {}
            changed = sorted(eid for eid, sha in current["evidence"].items()
                             if base_ev.get(eid) != sha)
            # 与 native_bridge 的正式增量规则同口径：变化证据必须逐条列入
            # manifest；manifest 可含未变化的已登记证据，但不能含未知 id。
            unknown = [eid for eid in manifest
                       if eid not in current["evidence"]]
            if unknown:
                problems.append("incremental_manifest_unknown")
            missing = [eid for eid in changed if eid not in manifest]
            if missing:
                problems.append("incremental_manifest_incomplete")
            reviewed_wid = str(review.get("work_item_id")
                               or (attempt or {}).get("work_item_id") or "")
            changed_actions_all = sorted(
                aid for aid, fp in current["actions"].items()
                if (coverage.get("actions") or {}).get(aid) != fp)
            changed_actions = [aid for aid in changed_actions_all
                               if aid != reviewed_wid]
            if not changed and not changed_actions_all:
                problems.append("incremental_without_change")
            if changed_actions:
                problems.append("incremental_actions_changed")
            changed_criteria = sorted(
                cid for cid, fp in current["criteria"].items()
                if (coverage.get("criteria") or {}).get(cid) != fp)
            if changed_criteria:
                problems.append("incremental_criteria_changed")
        except Exception as exc:
            problems.append(f"incremental_coverage_error:{type(exc).__name__}")
    return {
        "based_on_review_sha256": based_on,
        "baseline_found": base is not None,
        "baseline_g4_eligible": bool(base and base.get("g4_eligible") is True),
        "baseline_verdict": str((base or {}).get("verdict") or ""),
        "baseline_attestation_id": str(
            (attestation or {}).get("attestation_id") or ""),
        "baseline_attestation_found": len(attestations) == 1,
        "evidence_manifest": list(review.get("evidence_manifest") or []),
        "problems": sorted(set(problems)),
    }


def _test_binding(review: dict, execution: dict | None,
                  test_execution: dict | None = None) -> dict:
    evidence = []
    for item in _safe_list((execution or {}).get("evidence")):
        if isinstance(item, dict):
            evidence.append({"id": item.get("id", ""),
                             "path": item.get("file")
                             or item.get("path", ""),
                             "sha256": item.get("sha256", "")})
    verified = bool(test_execution
                    and test_execution.get("status") == "verified")
    return {
        "result_sha256": str(review.get("execution_result_sha256") or ""),
        "commands_run": list(review.get("commands_run") or []),
        "files_inspected": list(review.get("files_inspected") or []),
        "execution_evidence": evidence,
        "exit_code_bound": verified,
        "limitations": [] if verified else [
            "测试命令退出码/产物绑定未通过 test_execution 校验"],
    }


def _empty_record(review: dict, state: dict, *, status: str, agreement: str,
                  error_code: str = "", error_detail: str = "",
                  target_source: str = "envelope") -> dict:
    review_sha = str(review.get("result_sha256") or "")
    old = review.get("g4_eligible") is True
    record = {
        "schema": SHADOW_SCHEMA,
        "shadow_id": _shadow_id(review_sha, POLICY_VERSION, status, "",
                                error_code),
        "policy_version": POLICY_VERSION,
        "review_result_sha256": review_sha,
        "review_attempt_id": str(review.get("review_attempt_id") or ""),
        "target_sha256": str(review.get("target_sha256") or ""),
        "target_source": target_source,
        "review_scope": str(review.get("review_scope") or ""),
        "old_g4_eligible": old,
        "old_g4_reasons": _safe_list(review.get("g4_reasons")),
        "old_verdict": str(review.get("verdict") or ""),
        "old_reason": _safe_list(review.get("g4_reasons")),
        "old_reason_source": "review.g4_reasons",
        "old_host_reproducible": None,
        "new_g4_eligible": None,
        "new_verdict": "unavailable",
        "new_reason": [],
        "agreement": agreement,
        "difference_category": _category(
            agreement, old, None, _safe_list(review.get("g4_reasons"))),
        "evidence_summary": {},
        "differences": [],
        "status": status,
        "attestation": None,
        "incremental": None,
        "test_execution": None,
        "test_binding": _test_binding(review, _execution_for(state, review)),
        "error": ({"code": error_code, "detail": error_detail}
                  if error_code else None),
        "recorded_at": _now_iso(),
    }
    if not old:
        record["differences"] = ["old_g4_ineligible"]
    return record


def build(review: dict, state: dict, attempt: dict | None, *,
          state_path: str, db: str = "",
          expected_target: str = "") -> dict:
    """运行一次影子取证，始终返回可持久化记录，不抛出取证错误。"""
    try:
        return _build(
            review, state, attempt, state_path=state_path, db=db,
            expected_target=expected_target)
    except Exception as exc:  # 影子不得把任何取证异常升级为 ingest 失败
        return _empty_record(
            review, state, status="failed", agreement="unavailable",
            error_code="shadow_internal_error",
            error_detail=f"{type(exc).__name__}: {exc}",
            target_source="state" if expected_target else "envelope")


def _build(review: dict, state: dict, attempt: dict | None, *,
           state_path: str, db: str = "",
           expected_target: str = "") -> dict:
    source = "state" if expected_target else "envelope"
    attempt_id = str(review.get("review_attempt_id") or "").strip()
    if not attempt_id:
        return _empty_record(review, state, status="skipped",
                             agreement="not_applicable", target_source=source)
    if not isinstance(attempt, dict) or not attempt:
        return _empty_record(review, state, status="failed", agreement="unavailable",
                             error_code="shadow_attempt_missing",
                             error_detail=f"状态中没有 attempt {attempt_id}",
                             target_source=source)
    host = review.get("host") or {}
    metadata = _metadata_path_for(review)
    if not metadata:
        return _empty_record(review, state, status="failed", agreement="unavailable",
                             error_code="shadow_metadata_missing",
                             error_detail="复核信封缺少 host.metadata_path",
                             target_source=source)
    snapshot = attempt.get("code_snapshot") or {}
    workspace = str(snapshot.get("workspace")
                    or host.get("workspace_root") or "").strip()
    if not workspace:
        return _empty_record(review, state, status="failed", agreement="unavailable",
                             error_code="shadow_workspace_missing",
                             error_detail="attempt/host 都没有 workspace",
                             target_source=source)
    executor = _executor_for(state, review)
    task_dir = os.path.dirname(os.path.abspath(state_path))
    try:
        attestation = A.attest(
            metadata=metadata,
            attempt=attempt,
            task_id=str(state.get("task_id") or ""),
            workspace=workspace,
            expected_target=str(expected_target
                                or review.get("target_sha256") or ""),
            expected_output_sha256=str(
                (host or {}).get("output_sha256") or ""),
            db=db or os.path.expanduser("~/.zcode/cli/db/db.sqlite"),
            task_dir=task_dir,
            executor_agent=str(executor.get("agent_id") or ""),
            executor_session=str(executor.get("child_session_id") or ""),
            require_target=True,
            require_executor_identity=True,
            outside_reads="reject",
            consume_marker="",
        )
    except A.AttestError as exc:
        return _empty_record(review, state, status="failed", agreement="unavailable",
                             error_code=exc.code, error_detail=exc.detail,
                             target_source=source)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _empty_record(review, state, status="failed", agreement="unavailable",
                             error_code="shadow_internal_error",
                             error_detail=f"{type(exc).__name__}: {exc}",
                             target_source=source)

    old_eligible = review.get("g4_eligible") is True
    # 旧 Reviewer 的宿主 metadata 仍可读，才有一致证据口径可比较；rollout
    # 等附加证据是否还在由旧 g4_reasons 的拒绝码补充判断。
    old_host_reproducible = bool(os.path.isfile(metadata))
    new_eligible = attestation.get("eligible") is True
    incremental = _incremental_status(review, state, attempt)
    incremental_problems = list((incremental or {}).get("problems") or [])
    if incremental_problems:
        new_eligible = False
    if new_eligible == old_eligible:
        agreement = "consistent"
    elif new_eligible:
        agreement = "new_looser"
    else:
        agreement = "new_stricter"
    differences: list[str] = []
    if old_eligible and not new_eligible:
        differences = ["new_rejection:" + str(item.get("code") or "")
                       for item in attestation.get("rejections") or []
                       if isinstance(item, dict)]
    elif new_eligible and not old_eligible:
        differences = ["old_reason:" + str(item)
                       for item in review.get("g4_reasons") or []]
    if incremental_problems:
        differences.extend("incremental:" + code for code in incremental_problems)
    summary = _summary(attestation)
    # target_source 属于影子记录自身，不由 A.attest 产生；写入持久化摘要，
    # 后续增量重放用它排除“用信封 target 自证”的基线。
    summary["target_source"] = source
    execution = _execution_for(state, review) or {}
    env_binding = execution.get("execution_binding") or {}
    binding_id = str((env_binding or {}).get("binding_id") or "") \
        if isinstance(env_binding, dict) else ""
    binding = next(
        (item for item in _safe_list(state.get("execution_bindings"))
         if isinstance(item, dict)
         and str(item.get("binding_id") or "") == binding_id), None) \
        if binding_id else None
    policy = (state.get("contract") or {}).get("test_policy") or {}
    if str(state.get("risk") or "").lower() == "high" and not policy.get("captured"):
        # 迁移前遗留的高风险状态可能没有合同 test_policy；即使宿主记录与测试
        # 证据都可用，也不能用调用方自选命令形成正式 G4。
        new_eligible = False
        differences.append("test_execution:contract_test_policy_missing")
    require_binding = _require_binding(state)
    try:
        test_execution = TE.build(
            execution, attempt, db=db or os.path.expanduser(
                "~/.zcode/cli/db/db.sqlite"),
            binding=binding, require_binding=require_binding,
            expected_contract_sha256=str(state.get("contract_sha256") or ""))
    except Exception as exc:  # 测试证据异常不得伪装为 verified
        test_execution = {
            "schema": TE.SCHEMA, "status": "unverified",
            "problems": [f"test_execution_internal_error:{type(exc).__name__}"],
            "limitations": [], "binding_id": "",
        }
    te_status = str(test_execution.get("status") or "")
    if not _test_execution_acceptable(test_execution, binding, state):
        new_eligible = False
        if te_status == "not_applicable":
            differences.append("test_execution:not_applicable_invalid")
        for problem in test_execution.get("problems") or ["unverified"]:
            differences.append("test_execution:" + str(problem))
    if new_eligible == old_eligible:
        agreement = "consistent"
    elif new_eligible:
        agreement = "new_looser"
    else:
        agreement = "new_stricter"
    if old_eligible and not new_eligible and not differences:
        differences = ["new_rejection:test_execution"]
    status = "attested"
    record = {
        "schema": SHADOW_SCHEMA,
        "shadow_id": _shadow_id(
            str(review.get("result_sha256") or ""), POLICY_VERSION, status,
            summary["attestation_id"], ""),
        "policy_version": POLICY_VERSION,
        "review_result_sha256": str(review.get("result_sha256") or ""),
        "review_attempt_id": attempt_id,
        "target_sha256": str(review.get("target_sha256") or ""),
        "target_source": source,
        "review_scope": str(review.get("review_scope") or ""),
        "old_g4_eligible": old_eligible,
        "old_g4_reasons": _safe_list(review.get("g4_reasons")),
        "old_verdict": str(review.get("verdict") or ""),
        "old_reason": _safe_list(review.get("g4_reasons")),
        "old_reason_source": "review.g4_reasons",
        "old_host_reproducible": old_host_reproducible,
        "new_g4_eligible": new_eligible,
        "new_verdict": str(summary.get("verdict") or ""),
        "new_reason": [str(item.get("code") or "")
                       for item in attestation.get("rejections") or []
                       if isinstance(item, dict)],
        "agreement": agreement,
        "difference_category": _category(
            agreement, old_eligible, new_eligible,
            _safe_list(review.get("g4_reasons")), old_host_reproducible),
        "evidence_summary": {
            "old": {"g4_eligible": old_eligible,
                    "verdict": str(review.get("verdict") or ""),
                    "reasons": _safe_list(review.get("g4_reasons")),
                    "scope": str(review.get("review_scope") or "")},
            "new": {"g4_eligible": new_eligible,
                    "verdict": str(summary.get("verdict") or ""),
                    "rejections": [str(item.get("code") or "")
                                   for item in attestation.get("rejections") or []
                                   if isinstance(item, dict)],
                    "test_execution": test_execution.get("status"),
                    "profile_source": (summary.get("profile_snapshot") or {}).get("source")},
            "host": {"agent_id": summary.get("agent_id"),
                     "child_session_id": summary.get("child_session_id"),
                     "trace_id": summary.get("trace_id"),
                     "turn_id": summary.get("turn_id")},
        },
        "differences": differences,
        "status": status,
        "attestation": summary,
        "incremental": incremental,
        "test_execution": test_execution,
        "test_binding": _test_binding(review, execution, test_execution),
        "error": None,
        "recorded_at": _now_iso(),
    }
    return record


def append_attestation(state: dict, record: dict) -> dict:
    """幂等追加；相同 shadow_id 的重复结果不覆盖、不重复。"""
    records = state.setdefault("g4_attestations", [])
    if not isinstance(records, list):
        raise ValueError("state.g4_attestations 不是列表")
    for existing in records:
        if isinstance(existing, dict) and \
                existing.get("shadow_id") == record.get("shadow_id"):
            return existing
    records.append(record)
    return record

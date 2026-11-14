#!/usr/bin/env python3
"""任务代码范围快照：由宿主从 git 工作区生成，不依赖 Reviewer 自报。

该模块只读本地 git 状态与文件内容，不调用模型、不写任务状态、不访问网络。
`governance.py` 在创建 review attempt 与 G2/G4 复核时调用它。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess

_EXCLUDE_PREFIXES = (".git/", ".copilot-tasks/", ".audit/")
_EXCLUDE_PARTS = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".venv", "venv",
}
_EXCLUDE_NAMES = {".state.json.lock"}


def _name_allowed(rel: str) -> bool:
    return os.path.basename(rel.replace("\\", "/")) not in _EXCLUDE_NAMES


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _path_allowed(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    if any(rel.startswith(prefix) for prefix in _EXCLUDE_PREFIXES):
        return False
    return _name_allowed(rel) and not any(
        part in _EXCLUDE_PARTS for part in rel.split("/"))


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("GIT_OPTIONAL_LOCKS", "0")
    return subprocess.run(["git", "-C", root, *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, check=False)


def governance_paths(state_path: str) -> list[str]:
    """任务治理文件/目录：不能因为它们被更新而让代码快照自失效。"""
    path = os.path.abspath(state_path)
    task_dir = os.path.dirname(path)
    return [path, path + ".lock",
            os.path.join(task_dir, "review-attempts.jsonl"),
            os.path.join(task_dir, "evidence")]


def git_root(path: str) -> str:
    target = os.path.abspath(path)
    start = target if os.path.isdir(target) else os.path.dirname(target)
    result = _git(start, "rev-parse", "--show-toplevel")
    return os.path.abspath(result.stdout.strip()) if result.returncode == 0 else ""


def evidence_roots(state: dict) -> list[str]:
    return sorted({root for root in (git_root(p) for p in evidence_paths(state))
                   if root})


def evidence_paths(state: dict) -> list[str]:
    """已登记 live 文件证据的绝对路径；代码快照排除它们，避免重复。"""
    paths: list[str] = []
    for entry in state.get("evidence") or []:
        if not isinstance(entry, dict) or entry.get("dropped"):
            continue
        hint = str(entry.get("path_hint") or "").strip()
        if hint and hint != "<inline>" and os.path.isabs(hint):
            paths.append(os.path.abspath(hint))
    return sorted(set(paths))


def capture_code_snapshot(workspace: str = "",
                          exclude_paths: list[str] | None = None,
                          base: str = "") -> dict:
    """读取工作区 git 变更并计算每个文件的 SHA-256。

    覆盖 tracked 修改、untracked、删除、重命名；`.gitignore` 排除构建输出，
    治理临时目录与常见缓存目录另行排除。已登记证据由证据快照单独绑定，这里
    默认排除，避免“新增证据文件”被误判为源码变化。
    """
    workspace = os.path.abspath(os.path.expanduser(workspace or os.getcwd()))
    excluded = {os.path.abspath(os.path.expanduser(p))
                for p in (exclude_paths or [])}
    snapshot: dict = {
        "workspace": workspace,
        "git_root": "",
        "git_head": "",
        "git_base": "",
        "available": False,
        "reason": "",
        "files": [],
        "submodules": [],
        "excluded_paths": sorted(excluded),
        "digest": "",
    }
    top = _git(workspace, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        snapshot["reason"] = "not_git_repository"
        return snapshot
    root = os.path.abspath(top.stdout.strip())
    snapshot["git_root"] = root
    head = _git(root, "rev-parse", "HEAD")
    if head.returncode == 0:
        snapshot["git_head"] = head.stdout.strip()
    base_entries: dict[str, dict] = {}
    base = str(base or "").strip()
    if base:
        resolved = _git(root, "rev-parse", base + "^{commit}")
        if resolved.returncode != 0:
            snapshot["reason"] = "base_invalid"
            return snapshot
        snapshot["git_base"] = resolved.stdout.strip()
        diff = _git(root, "diff", "--name-status", "-z", "--find-renames",
                    snapshot["git_base"], "--")
        if diff.returncode != 0:
            snapshot["reason"] = "git_diff_failed"
            return snapshot
        parts = diff.stdout.split("\0")
        i = 0
        while i < len(parts):
            status_code = parts[i]
            i += 1
            if not status_code:
                continue
            code = status_code[:1]
            if code in ("R", "C"):
                old = parts[i] if i < len(parts) else ""
                new = parts[i + 1] if i + 1 < len(parts) else ""
                i += 2
                if new:
                    base_entries[new] = {"status": code, "old_path": old}
            else:
                name = parts[i] if i < len(parts) else ""
                i += 1
                if name:
                    base_entries[name] = {"status": code, "old_path": ""}
    status = _git(root, "status", "--porcelain=v1", "-z",
                  "--untracked-files=all")
    if status.returncode != 0:
        snapshot["reason"] = "git_status_failed"
        return snapshot
    tokens = status.stdout.split("\0")
    merged: dict[str, dict] = dict(base_entries)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        xy = token[:2]
        rel = token[3:] if len(token) > 3 else ""
        old_rel = ""
        if xy[:1] in ("R", "C") or xy[1:2] in ("R", "C"):
            if index < len(tokens):
                old_rel = tokens[index]
                index += 1
        if rel:
            merged[rel] = {"status": xy, "old_path": old_rel}
    files: list[dict] = []
    for rel, meta in merged.items():
        if not rel or not _path_allowed(rel):
            continue
        path = os.path.abspath(os.path.join(root, rel))
        if os.path.commonpath([root, path]) != root:
            continue
        if any(path == item or path.startswith(item + os.sep)
               for item in excluded):
            continue
        entry = {"path": rel.replace("\\", "/"), "status": meta["status"],
                 "sha256": "", "size": 0}
        if meta.get("old_path"):
            entry["old_path"] = meta["old_path"].replace("\\", "/")
        if os.path.islink(path):
            target = os.readlink(path)
            entry["kind"] = "symlink"
            entry["sha256"] = _sha256_text(target)
            entry["size"] = len(target.encode("utf-8"))
        elif os.path.isfile(path):
            entry["kind"] = "file"
            entry["sha256"] = _sha256_file(path)
            entry["size"] = os.path.getsize(path)
        elif os.path.exists(path):
            entry["kind"] = "other"
        else:
            entry["kind"] = "missing"
        files.append(entry)
    files.sort(key=lambda item: (item["path"], item["status"]))
    submodules = _git(root, "submodule", "status", "--recursive")
    if submodules.returncode == 0:
        snapshot["submodules"] = [line for line in
                                  submodules.stdout.splitlines() if line.strip()]
    snapshot["files"] = files
    snapshot["available"] = True
    snapshot["digest"] = _sha256_text(_canonical_json({
        "git_root": root,
        "git_head": snapshot["git_head"],
        "git_base": snapshot["git_base"],
        "files": files,
        "submodules": snapshot["submodules"],
    }))
    return snapshot


def code_scope_problems(attempt: dict, state: dict | None = None, *,
                        check_current: bool = True) -> list[str]:
    """attempt 冻结的代码范围必须存在，且当前工作区未发生漂移。"""
    if not isinstance(attempt, dict):
        return ["native_review_code_scope_missing"]
    snapshot = attempt.get("code_snapshot")
    if not isinstance(snapshot, dict) or not snapshot:
        return ["native_review_code_scope_missing"]
    if not snapshot.get("available"):
        return ["native_review_code_scope_unavailable"]
    if not check_current:
        return []
    excluded = set(str(p) for p in snapshot.get("excluded_paths") or [])
    if state is not None:
        excluded.update(evidence_paths(state))
    current = capture_code_snapshot(
        str(snapshot.get("workspace") or ""), exclude_paths=sorted(excluded),
        base=str(snapshot.get("git_base") or ""))
    if not current.get("available"):
        return ["native_review_code_scope_unavailable"]
    if str(current.get("digest") or "") != str(snapshot.get("digest") or ""):
        return ["native_review_code_scope_drift"]
    return []

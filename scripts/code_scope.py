#!/usr/bin/env python3
"""任务代码范围快照：由宿主从 git 工作区生成，不依赖 Reviewer 自报。

该模块只读本地 git 状态与文件内容，不调用模型、不写任务状态、不访问网络。
`governance.py` 在生成执行快照与质量报告代码范围时调用它。
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess

from lib_common import sha256_file, sha256_text

_EXCLUDE_PREFIXES = (".git/", ".copilot-tasks/", ".audit/")
_EXCLUDE_PARTS = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "review-outputs",
    "node_modules", ".venv", "venv",
}
_EXCLUDE_NAMES = {".state.json.lock"}

# 单进程内的 git 根缓存（成功结果）；不跨进程，不写盘。
_GIT_ROOT_CACHE: dict[str, str] = {}


def _name_allowed(rel: str) -> bool:
    return os.path.basename(rel.replace("\\", "/")) not in _EXCLUDE_NAMES


def _sha256_text(text: str) -> str:
    return sha256_text(text)


def _sha256_file(path: str) -> str:
    return sha256_file(path)


def snapshot_digest(snapshot: dict) -> str:
    """权威快照摘要；capture 与取证器必须共用这一实现。"""
    return _sha256_text(_canonical_json({
        "git_root": str(snapshot.get("git_root") or ""),
        "git_head": str(snapshot.get("git_head") or ""),
        "git_base": str(snapshot.get("git_base") or ""),
        "files": snapshot.get("files") or [],
        "submodules": snapshot.get("submodules") or [],
        "submodule_worktrees": snapshot.get("submodule_worktrees") or [],
        "nested_repos": snapshot.get("nested_repos") or [],
    }))


def canonical_json(value: object) -> str:
    """确定性 JSON：代码快照与绑定 id 的唯一序列化口径。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


_canonical_json = canonical_json


def _path_allowed(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    if any(rel.startswith(prefix) for prefix in _EXCLUDE_PREFIXES):
        return False
    return _name_allowed(rel) and not any(
        part in _EXCLUDE_PARTS for part in rel.split("/"))


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("GIT_OPTIONAL_LOCKS", "0")
    # 不让调用方环境把 git 重定向到另一个仓库/索引/对象库。
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_COMMON_DIR", "GIT_NAMESPACE"):
        env.pop(key, None)
    return subprocess.run(["git", "-C", root, *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, check=False)


def governance_paths(state_path: str) -> list[str]:
    """任务治理文件/目录：不能因为它们被更新而让代码快照自失效。"""
    path = os.path.abspath(state_path)
    task_dir = os.path.dirname(path)
    return [path, os.path.join(task_dir,
                               "." + os.path.basename(path) + ".lock"),
            os.path.join(task_dir, "review-attempts.jsonl"),
            os.path.join(task_dir, "evidence"),
            os.path.join(task_dir, "review-outputs")]


def git_root(path: str) -> str:
    """路径所属 git 仓库根。

    结果在单进程内按起始目录缓存（`git rev-parse` 每次约 3ms，而一个任务可能
    对每个证据路径各查一次）。只缓存成功结果：非仓库目录不缓存，避免测试或
    新建仓库场景读到旧判断。
    """
    target = os.path.abspath(path)
    start = target if os.path.isdir(target) else os.path.dirname(target)
    cached = _GIT_ROOT_CACHE.get(start)
    if cached is not None:
        return cached
    result = _git(start, "rev-parse", "--show-toplevel")
    root = os.path.abspath(result.stdout.strip()) if result.returncode == 0 else ""
    if root:
        _GIT_ROOT_CACHE[start] = root
    return root


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


def _is_tracked(root: str, path: str) -> bool:
    rel = os.path.relpath(path, root).replace("\\", "/")
    if rel == ".." or rel.startswith("../"):
        return False
    result = _git(root, "ls-files", "--error-unmatch", "--", ":(literal)" + rel)
    if result.returncode == 0:
        return True
    # 索引被 git rm --cached 后仍可能在 HEAD 中；tracked 源文件不能因此被排除。
    head = _git(root, "ls-tree", "-r", "--name-only", "HEAD", "--",
                ":(literal)" + rel)
    return head.returncode == 0 and rel in head.stdout.splitlines()


def safe_path_excludes(paths: list[str], workspace: str,
                       base: str = "") -> list[str]:
    """给定路径中可排除的部分：仅未跟踪产物；跟踪文件一律不排除。"""
    root = git_root(workspace)
    if not root:
        return []
    out: list[str] = []
    for path in paths:
        if not str(path or "").strip():
            continue
        absolute = os.path.abspath(os.path.expanduser(str(path)))
        path_root = git_root(absolute) or root
        try:
            inside = os.path.commonpath([path_root, absolute]) == path_root
        except ValueError:
            inside = False
        tracked = _is_tracked(path_root, absolute)
        if not tracked and path_root != root:
            tracked = _is_tracked(root, absolute)
        if not inside or not tracked:
            out.append(absolute)
    return sorted(set(out))


def safe_evidence_excludes(state: dict, workspace: str,
                           base: str = "") -> list[str]:
    """执行/质量代码快照可排除的 evidence：仅限未被 git 跟踪的证据产物。

    被 git 跟踪的源文件即使登记为 evidence，也不能从代码快照消失；未跟踪的
    证据文件（日志/截图/输出）仍排除，避免新增证据被误判为源码变化。
    """
    return safe_path_excludes(evidence_paths(state), workspace, base)


_SUBMODULE_LINE = re.compile(
    r"^\s*[+-U]?([0-9a-f]{40}|[0-9a-f]{64})\s+(\S+)")


def _submodule_dirty_state(repo: str, rel: str, excluded: set[str],
                           depth: int = 0) -> dict:
    """递归读取 submodule 工作区真实状态（HEAD + tracked/untracked 内容）。

    git 无法可靠读取时返回 `error`，由调用方 fail-closed；不静默忽略内部改动。
    """
    state: dict = {"path": rel, "head": "", "dirty": False, "files": [],
                   "nested": [], "error": ""}
    if depth > 8:
        state["error"] = "submodule_depth_exceeded"
        return state
    if not os.path.isdir(repo):
        state["error"] = "not_a_directory"
        return state
    if not os.path.exists(os.path.join(repo, ".git")):
        state["error"] = "not_initialized"
        return state
    head = _git(repo, "rev-parse", "HEAD")
    if head.returncode != 0:
        state["error"] = "head_unreadable"
        return state
    state["head"] = head.stdout.strip()
    nested_lines = _git(repo, "submodule", "status", "--recursive")
    if nested_lines.returncode != 0:
        state["error"] = "nested_status_failed"
        return state
    declared: set[str] = set()
    for line in nested_lines.stdout.splitlines():
        match = _SUBMODULE_LINE.match(str(line))
        if match:
            declared.add(match.group(2))
    for rel_nested in sorted(declared):
        nested_repo = os.path.realpath(os.path.join(repo, rel_nested))
        if os.path.commonpath([os.path.realpath(repo), nested_repo]) != \
                os.path.realpath(repo):
            state["nested"].append({
                "path": rel.rstrip("/") + "/" + rel_nested,
                "head": "", "dirty": False, "files": [], "nested": [],
                "error": "path_outside_root"})
            continue
        state["nested"].append(_submodule_dirty_state(
            nested_repo, rel.rstrip("/") + "/" + rel_nested,
            excluded, depth + 1))
    status = _git(repo, "status", "--porcelain=v1", "-z",
                  "--untracked-files=all")
    if status.returncode != 0:
        state["error"] = "status_failed"
        return state
    tokens = status.stdout.split("\0")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        xy = token[:2]
        name = token[3:] if len(token) > 3 else ""
        old = ""
        if xy[:1] in ("R", "C") or xy[1:2] in ("R", "C"):
            if index < len(tokens):
                old = tokens[index]
                index += 1
        if not name:
            continue
        path = os.path.abspath(os.path.join(repo, name))
        if os.path.commonpath([repo, path]) != repo:
            continue
        if not _path_allowed(name):
            continue
        if any(path == item or path.startswith(item + os.sep)
               for item in excluded):
            continue
        if name in declared or any(
                name == item or name.startswith(item.rstrip("/") + "/")
                for item in declared):
            continue
        file_state = {"path": name.replace("\\", "/"), "status": xy,
                      "kind": "missing", "sha256": "", "size": 0}
        if old:
            file_state["old_path"] = old.replace("\\", "/")
        if os.path.islink(path):
            target = os.readlink(path)
            file_state.update({"kind": "symlink",
                               "sha256": _sha256_text(target),
                               "size": len(target.encode("utf-8"))})
        elif os.path.isfile(path):
            file_state.update({"kind": "file",
                               "sha256": _sha256_file(path),
                               "size": os.path.getsize(path)})
        elif os.path.exists(path):
            file_state["kind"] = "other"
        state["files"].append(file_state)
    state["files"].sort(key=lambda item: (item["path"], item["status"]))
    # 未声明为 submodule 的嵌套 Git 仓库：按 status 路径向上找仓库根并递归。
    nested_roots = _nested_repo_roots(
        repo, [item["path"] for item in state["files"]], excluded)
    # 被 .gitignore 隐藏的未声明仓库不会出现在 status 里；在 submodule/嵌套
    # 仓库内部也要做一次文件系统扫描，否则其改动不进入摘要。
    try:
        scanned = _scan_nested_git_dirs(repo, excluded, declared)
    except ValueError as exc:
        state["error"] = str(exc) or "nested_repo_scan_failed"
        return state
    existing = {item["path"] for item in state["nested"]}
    real_repo = os.path.realpath(repo)
    # status 路径找到的仓库根优先登记；随后再补文件系统扫描发现、但不在
    # status 里的被忽略仓库（含更深嵌套）。
    for inner_rel, inner_repo in nested_roots:
        full_rel = rel.rstrip("/") + "/" + inner_rel
        if full_rel in existing:
            continue
        existing.add(full_rel)
        state["nested"].append(_submodule_dirty_state(
            inner_repo, full_rel, excluded, depth + 1))
    for inner_rel in scanned:
        full_rel = rel.rstrip("/") + "/" + inner_rel
        if full_rel in existing or any(
                full_rel.startswith(item.rstrip("/") + "/")
                for item in existing):
            continue
        inner_repo = os.path.realpath(os.path.join(repo, inner_rel))
        if os.path.commonpath([real_repo, inner_repo]) != real_repo:
            state["error"] = "nested_repo_outside_root"
            return state
        existing.add(full_rel)
        state["nested"].append(_submodule_dirty_state(
            inner_repo, full_rel, excluded, depth + 1))
    state["nested"].sort(key=lambda item: item["path"])
    state["dirty"] = bool(state["files"] or state["nested"])
    return state


def _submodule_worktrees(root: str, lines: list[str],
                         excluded: set[str]) -> list[dict]:
    out: list[dict] = []
    for line in lines:
        match = _SUBMODULE_LINE.match(str(line))
        if not match:
            continue
        rel = match.group(2)
        repo = os.path.realpath(os.path.join(root, rel))
        if os.path.commonpath([os.path.realpath(root), repo]) != os.path.realpath(root):
            out.append({"path": rel, "head": "", "dirty": False,
                        "files": [], "nested": [],
                        "error": "path_outside_root"})
            continue
        out.append(_submodule_dirty_state(repo, rel, excluded))
    out.sort(key=lambda item: item["path"])
    return out


def _has_git_dir(path: str) -> bool:
    return os.path.exists(os.path.join(path, ".git"))


def _scan_nested_git_dirs(root: str, excluded: set[str],
                          declared: set[str]) -> list[str]:
    """扫描工作区内含 `.git` 的目录（含 .gitignore 隐藏的嵌套仓库）。

    深度或数量超过上限、或遇到越界 realpath 时抛 ValueError，由调用方
    fail-closed；不静默截断（否则更深的嵌套仓库改动不会进入摘要）。
    """
    real_root = os.path.realpath(root)
    excluded = {os.path.realpath(item) for item in excluded}
    found: list[str] = []
    for current, dirs, _files in os.walk(root, followlinks=False):
        real = os.path.realpath(current)
        if os.path.commonpath([real_root, real]) != real_root:
            raise ValueError("nested_repo_scan_outside_root")
        rel = os.path.relpath(current, root).replace("\\", "/")
        if rel != "." and (rel in declared or any(
                rel.startswith(item.rstrip("/") + "/") for item in declared)):
            dirs[:] = []
            continue
        dirs[:] = [name for name in dirs
                   if name not in (".git", "node_modules", "__pycache__")
                   and not os.path.islink(os.path.join(current, name))
                   and not any(os.path.realpath(os.path.join(current, name))
                               == item or
                               os.path.realpath(os.path.join(current, name))
                               .startswith(item + os.sep)
                               for item in excluded)]
        depth = 0 if rel == "." else len(rel.split("/"))
        if depth >= 8:
            if dirs:
                raise ValueError("nested_repo_scan_depth_exceeded")
            continue
        has_git = os.path.exists(os.path.join(current, ".git"))
        if has_git and current != root:
            if any(rel == item or rel.startswith(item.rstrip("/") + "/")
                   for item in found):
                continue
            found.append(rel)
            if len(found) > 64:
                raise ValueError("nested_repo_scan_overflow")
    return found


def _nested_repo_roots(root: str, rel_paths: list[str],
                       excluded: set[str]) -> list[tuple[str, str]]:
    """找出未声明为 submodule 的嵌套 Git 仓库根（含 .git 文件形式）。

    从每个变更路径向上找最近的嵌套仓库；返回 [(rel, realpath)]，外层嵌套
    优先，内层留给 `_submodule_dirty_state` 递归。
    """
    real_root = os.path.realpath(root)
    found: dict[str, str] = {}
    for rel in rel_paths:
        candidate = os.path.abspath(os.path.join(root, rel.rstrip("/")))
        if str(rel).endswith("/") and os.path.isdir(candidate):
            current = candidate
        else:
            current = os.path.dirname(candidate)
        while current and os.path.commonpath([real_root, current]) == real_root:
            if current != root and _has_git_dir(current):
                found[os.path.relpath(current, root)] = current
                break
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    roots: list[tuple[str, str]] = []
    for rel in sorted(found):
        real = os.path.realpath(found[rel])
        if any(rel == parent or rel.startswith(parent.rstrip("/") + "/")
               for parent, _ in roots):
            continue
        if any(real == item or real.startswith(item + os.sep)
               for item in excluded):
            continue
        if os.path.commonpath([real_root, real]) != real_root:
            raise ValueError("nested_repo_outside_root")
        roots.append((rel.replace("\\", "/"), real))
    return roots


def capture_code_snapshot(workspace: str = "",
                          exclude_paths: list[str] | None = None,
                          base: str = "",
                          cache: dict | None = None) -> dict:
    """读取工作区 git 变更并计算每个文件的 SHA-256。

    覆盖 tracked 修改、untracked、删除、重命名；`.gitignore` 排除构建输出，
    治理临时目录与常见缓存目录另行排除。证据文件是否排除由调用方决定：质量报告
    不排除已登记证据；只有未被 git 跟踪的证据产物才可排除，已跟踪源文件即使登记为
    证据也必须进入快照。证据内容由独立证据快照绑定。

    `cache` 是**单次命令内**的复用容器（同一进程内同一工作区的快照只算一次）。
    默认 `None` 表示不缓存：库调用方与测试需要看到调用时刻的真实工作区状态，
    不能继承旧快照。命中缓存时返回深拷贝，避免调用方改写污染后续读取。
    """
    workspace = os.path.abspath(os.path.expanduser(workspace or os.getcwd()))
    excluded = {os.path.abspath(os.path.expanduser(p))
                for p in (exclude_paths or [])}
    cache_key = None
    if cache is not None:
        cache_key = ("code_snapshot", workspace, tuple(sorted(excluded)),
                     str(base or "").strip())
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            return copy.deepcopy(cached)
    snapshot: dict = {
        "workspace": workspace,
        "git_root": "",
        "git_head": "",
        "git_base": "",
        "available": False,
        "reason": "",
        "files": [],
        "submodules": [],
        "submodule_worktrees": [],
        "nested_repos": [],
        "excluded_paths": sorted(excluded),
        "digest": "",
    }
    # 一次 git 调用同时取工作区根与 HEAD；失败时才分开重试（空仓库没有 HEAD）。
    top = _git(workspace, "rev-parse", "--show-toplevel", "HEAD")
    if top.returncode != 0:
        only_top = _git(workspace, "rev-parse", "--show-toplevel")
        if only_top.returncode != 0:
            snapshot["reason"] = "not_git_repository"
            return snapshot
        top_lines = [only_top.stdout.strip()]
    else:
        top_lines = [line.strip() for line in top.stdout.splitlines()
                     if line.strip()]
    root = os.path.abspath(top_lines[0]) if top_lines and top_lines[0] else ""
    if not root:
        snapshot["reason"] = "not_git_repository"
        return snapshot
    snapshot["git_root"] = root
    if len(top_lines) > 1 and top_lines[1]:
        snapshot["git_head"] = top_lines[1]
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
    # `.gitmodules` 缺失但 index 里有 gitlink（160000）时，本调用会以 128 失败。
    # 这是畸形仓库：旧行为是快照不可用（fail-closed），不能为了省一次子进程把
    # 未初始化/未声明的 gitlink 目录静默移出代码范围。因此这里保持无条件调用。
    submodules = _git(root, "submodule", "status", "--recursive")
    if submodules.returncode != 0:
        snapshot["reason"] = "submodule_status_failed"
        return snapshot
    snapshot["submodules"] = [line for line in
                              submodules.stdout.splitlines() if line.strip()]
    snapshot["files"] = files
    declared_rels = {match.group(2) for match in
                     (_SUBMODULE_LINE.match(str(line))
                      for line in snapshot["submodules"]) if match}
    nested_candidates = [item["path"] for item in files
                         if item["path"] not in declared_rels]
    try:
        scanned = _scan_nested_git_dirs(root, excluded, declared_rels)
        nested_candidates.extend(item.rstrip("/") + "/" for item in scanned)
        nested_roots = _nested_repo_roots(
            root, nested_candidates, excluded)
    except ValueError as exc:
        snapshot["reason"] = str(exc)
        return snapshot
    snapshot["nested_repos"] = [
        _submodule_dirty_state(real, rel, excluded)
        for rel, real in nested_roots]
    snapshot["submodule_worktrees"] = _submodule_worktrees(
        root, snapshot["submodules"], excluded)
    def _has_error(items: list) -> bool:
        for item in items:
            if not isinstance(item, dict):
                return True
            if str(item.get("error") or ""):
                return True
            if _has_error(item.get("nested") or []):
                return True
        return False
    if _has_error(snapshot["submodule_worktrees"]) or \
            _has_error(snapshot["nested_repos"]):
        snapshot["reason"] = "submodule_state_unreadable"
        return snapshot
    snapshot["available"] = True
    snapshot["digest"] = snapshot_digest(snapshot)
    if cache_key is not None:
        cache[cache_key] = copy.deepcopy(snapshot)
    return snapshot


def _snapshot_known_paths(items: object) -> set[str]:
    """收集快照中顶层/子模块/嵌套仓库的文件相对路径（path 均为仓库根相对）。"""
    out: set[str] = set()
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        base = str(item.get("path") or "").strip("/")
        for entry in item.get("files") or []:
            if isinstance(entry, dict) and entry.get("path"):
                out.add((base + "/" + str(entry["path"])).strip("/"))
        out |= _snapshot_known_paths(item.get("nested") or [])
    return out


def code_scope_problems(attempt: dict, state: dict | None = None, *,
                        check_current: bool = True,
                        cache: dict | None = None) -> list[str]:
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
        # 只动态排除“attempt 之后新增的未跟踪证据”；attempt 时已存在并进入
        # 快照的未跟踪产物不能排除，否则执行后才登记的证据会让执行/复核摘要
        # 再次不一致。新证据本身由 final_evidence_digest / 增量 manifest 处理。
        root = str(snapshot.get("git_root") or snapshot.get("workspace") or "")
        known = {str(item.get("path") or "")
                 for item in snapshot.get("files") or []
                 if isinstance(item, dict)}
        known |= _snapshot_known_paths(snapshot.get("submodule_worktrees"))
        known |= _snapshot_known_paths(snapshot.get("nested_repos"))
        for path in evidence_paths(state):
            absolute = os.path.abspath(path)
            try:
                inside = bool(root) and os.path.commonpath(
                    [root, absolute]) == root
            except ValueError:
                inside = False
            if inside and os.path.relpath(absolute, root).replace(
                    "\\", "/") in known:
                continue
            if not inside or safe_path_excludes(
                    [absolute], str(snapshot.get("workspace") or ""),
                    str(snapshot.get("git_base") or "")):
                excluded.add(absolute)
    current = capture_code_snapshot(
        str(snapshot.get("workspace") or ""), exclude_paths=sorted(excluded),
        base=str(snapshot.get("git_base") or ""), cache=cache)
    if not current.get("available"):
        return ["native_review_code_scope_unavailable"]
    if str(current.get("digest") or "") != str(snapshot.get("digest") or ""):
        return ["native_review_code_scope_drift"]
    return []

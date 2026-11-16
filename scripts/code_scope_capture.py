#!/usr/bin/env python3
"""在 verify 生成质量报告时捕获代码范围快照（只读、不写文件）。

verify 报告必须把输出中的 `commit`、`dirty`、`code_scope_sha256` 与
`files`（变更路径）写入 `subject`。Copilot 在 `report --verify-report`
接入时会用同一快照实现重新计算并拒绝不一致的报告；收口时再次重算检测漂移。

用法::

    python3 -B scripts/code_scope_capture.py --workspace <repo> \
        [--state <state.json>] [--exclude <path> ...]

`--state` 用于排除任务治理文件；**不排除已登记证据文件**——证据位于工作区
内且发生变更时同样属于代码范围，不能借“已登记为证据”从快照消失。该命令只读
git 与文件内容，不写状态、不写文件。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import code_scope as CS  # noqa: E402


def _dirty(snapshot: dict) -> bool:
    if snapshot.get("files"):
        return True

    def any_dirty(items: object) -> bool:
        if not isinstance(items, list):
            return False
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("dirty"):
                return True
            if any_dirty(item.get("nested") or []):
                return True
        return False

    return any_dirty(snapshot.get("submodule_worktrees")) or \
        any_dirty(snapshot.get("nested_repos"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="code_scope_capture.py",
        description="捕获 verify 报告使用的代码范围快照（只读）")
    ap.add_argument("--workspace", required=True, help="实际 git 工作区")
    ap.add_argument("--state", default="",
                    help="任务状态文件（可选，用于排除治理文件；不排除已登记证据）")
    ap.add_argument("--exclude", action="append", default=[],
                    help="额外排除路径（可重复）")
    args = ap.parse_args(argv)
    workspace = os.path.abspath(os.path.expanduser(args.workspace))
    if not os.path.isdir(workspace):
        print(f"错误：工作区不存在：{workspace}", file=sys.stderr)
        return 2
    excludes = [os.path.abspath(os.path.expanduser(p)) for p in args.exclude]
    if args.state:
        try:
            sys.path.insert(0, HERE)
            import governance as G  # noqa: E402
            G.load_state(os.path.abspath(args.state))
            excludes.extend(CS.governance_paths(os.path.abspath(args.state)))
        except Exception as exc:  # noqa: BLE001 - 状态不可读时直接报错
            print(f"错误：无法读取任务状态 {args.state}：{exc}", file=sys.stderr)
            return 2
    snapshot = CS.capture_code_snapshot(
        workspace, exclude_paths=sorted(set(excludes)))
    if not snapshot.get("available"):
        print("错误：代码范围不可读取：" + str(snapshot.get("reason") or "unknown"),
              file=sys.stderr)
        return 2
    files_paths: list[str] = []
    for item in snapshot.get("files") or []:
        for key in ("path", "old_path"):
            value = str(item.get(key) or "")
            if value and value not in files_paths:
                files_paths.append(value)
    payload = {
        "schema": "copilot-code-scope/v1",
        "workspace": snapshot.get("workspace"),
        "git_root": snapshot.get("git_root"),
        "commit": snapshot.get("git_head"),
        "dirty": _dirty(snapshot),
        "code_scope_sha256": snapshot.get("digest"),
        "files_paths": files_paths,
        "files": [
            {key: item.get(key) for key in
             ("path", "old_path", "status", "kind", "sha256", "size")
             if item.get(key) is not None}
            for item in snapshot.get("files") or []
        ],
        "submodule_dirty": any(
            isinstance(item, dict) and (item.get("dirty") or item.get("error"))
            for item in snapshot.get("submodule_worktrees") or []),
        "nested_repo_dirty": any(
            isinstance(item, dict) and (item.get("dirty") or item.get("error"))
            for item in snapshot.get("nested_repos") or []),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""sync_runtime.py — Copilot 源码 → ZCode 运行时副本的单向同步工具。

唯一源码：本仓库（`Project/copilot`）。运行时副本是构建产物：

    ~/.zcode/skills/copilot        ← SKILL.md、scripts/、references/

旧 Reviewer 全局 Profile 已退役；工作区只读权限载体
（`.zcode/agents/independent-readonly.md`）属于项目工作区配置，不属于本工具的
分发范围。

用法：

    python3 scripts-sync/sync_runtime.py --check
    python3 scripts-sync/sync_runtime.py --apply
    python3 scripts-sync/sync_runtime.py --apply --prune
    python3 scripts-sync/sync_runtime.py --baseline

退出码：0 = 一致或写入成功；1 = 存在漂移或写入失败；2 = 参数、路径或安全校验错误。

默认不删除运行时多出的文件；`--prune` 必须与 `--apply` 一起显式使用。

分发范围：只允许 SKILL.md、scripts/、references/。
tests/、docs/、manifests/、scripts-sync/、README.md、AGENTS.md 属于源码仓库的
开发与项目管理内容，不进入运行时。`.env*`、凭据、`.audit/` 审计日志、`__pycache__`、
临时状态和符号链接一律不同步——`.audit/` 即使出现在运行时目标里也会被忽略且不会被
`--prune` 删除。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_SKILL_DEST = "~/.zcode/skills/copilot"

# 参与分发的 Skill 顶层条目；其余源码目录/文件不进入运行时。
DISTRIBUTED_TOP = ("SKILL.md", "scripts", "references")
BASELINE_PATH = "manifests/source-baseline.sha256"

# 运行时目标里允许存在但不受管理的目录：不参与比较，也不会被 --prune 删除。
SKIP_DIR_NAMES = {
    "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
    ".audit", ".zcode", ".idea", ".vscode", "node_modules",
}
SKIP_SUFFIXES = (".pyc", ".pyo", ".swp", ".tmp", ".bak", "~", ".lock")
SKIP_NAMES = {".DS_Store", "state.json", "dispatch.json", "dry-run.json"}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")


class SyncError(Exception):
    """安全校验或同步失败；CLI 以退出码 2 结束。"""


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_secret(name: str) -> bool:
    lowered = name.lower()
    if lowered == ".env" or lowered.startswith(".env."):
        return True
    if lowered.endswith(SECRET_SUFFIXES):
        return True
    return "credential" in lowered or "secret" in lowered


def _is_skipped_file(name: str) -> bool:
    return (name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES)
            or _is_secret(name))


def _ensure_regular(path: str, label: str) -> None:
    if os.path.islink(path):
        raise SyncError(f"{label} 是符号链接，拒绝同步：{path}")
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        raise SyncError(f"{label} 无法读取：{path}（{exc}）") from exc
    if not stat.S_ISREG(mode):
        raise SyncError(f"{label} 不是普通文件：{path}")


def _walk_files(root: str, label: str):
    """遍历 root 下的普通文件，拒绝符号链接；跳过受管跳过项。"""
    if os.path.islink(root):
        raise SyncError(f"{label} 是符号链接，拒绝同步：{root}")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        keep = []
        for name in sorted(dirnames):
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                raise SyncError(f"{label} 含符号链接目录，拒绝同步：{path}")
            if name not in SKIP_DIR_NAMES:
                keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if _is_skipped_file(name) or name in SKIP_DIR_NAMES:
                continue
            _ensure_regular(path, label)
            yield path


def source_manifest(root: str) -> dict[str, str]:
    """供分发的源码文件 → SHA-256。只读取 DISTRIBUTED_TOP 下的内容。"""
    root = os.path.abspath(root)
    result: dict[str, str] = {}
    for top in DISTRIBUTED_TOP:
        path = os.path.join(root, top)
        if not os.path.exists(path):
            raise SyncError(f"源码缺少分发条目：{top}（{root}）")
        if os.path.islink(path):
            raise SyncError(f"源码分发条目是符号链接，拒绝同步：{path}")
        if os.path.isfile(path):
            _ensure_regular(path, "源码文件")
            result[top] = sha256(path)
            continue
        for file_path in _walk_files(path, "源码目录"):
            rel = os.path.relpath(file_path, root).replace(os.sep, "/")
            result[rel] = sha256(file_path)
    return result


def target_manifest(root: str) -> dict[str, str]:
    """运行时目标中所有受管理文件 → SHA-256；跳过项不参与比较。"""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise SyncError(f"运行时目标不是目录：{root}")
    result: dict[str, str] = {}
    for file_path in _walk_files(root, "运行时目标"):
        rel = os.path.relpath(file_path, root).replace(os.sep, "/")
        result[rel] = sha256(file_path)
    return result


def compare_maps(src: dict[str, str], dst: dict[str, str]):
    missing = sorted(set(src) - set(dst))
    different = sorted(k for k in set(src) & set(dst) if src[k] != dst[k])
    extra = sorted(set(dst) - set(src))
    return missing, different, extra


def _safe_rel(rel: str) -> str:
    norm = os.path.normpath(rel).replace(os.sep, "/")
    if norm.startswith("/") or norm in ("", ".") or norm == ".." or norm.startswith("../"):
        raise SyncError(f"不安全的目标相对路径：{rel!r}")
    return norm


def _safe_dest(root: str, rel: str) -> str:
    rel = _safe_rel(rel)
    root_real = os.path.realpath(root)
    dest = os.path.join(root_real, *rel.split("/"))
    parent = os.path.dirname(dest)
    probe = parent
    while not os.path.exists(probe):
        probe = os.path.dirname(probe)
    probe_real = os.path.realpath(probe)
    try:
        common = os.path.commonpath([probe_real, root_real])
    except ValueError:
        common = ""
    # probe 必须是目标根（或其内部）或目标根的祖先：前者说明父目录已存在且在根内，
    # 后者说明目标根尚未创建、沿现有祖先创建即可；两种都不允许符号链接把路径带出根外。
    if common not in (probe_real, root_real):
        raise SyncError(f"目标路径越出运行时目录：{dest}")
    if os.path.islink(dest):
        raise SyncError(f"目标文件是符号链接，拒绝写入：{dest}")
    return dest


def copy_atomic(src: str, dest: str) -> None:
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".sync-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        shutil.copymode(src, tmp)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _assert_disjoint(source: str, target: str) -> None:
    source_real = os.path.realpath(source)
    target_real = os.path.realpath(target)
    try:
        common = os.path.commonpath([source_real, target_real])
    except ValueError:
        return
    if common in (source_real, target_real):
        raise SyncError(
            f"源码与运行时目标相同或互为祖先/后代，拒绝同步：{source_real} ↔ {target_real}")


def apply_tree(source_root: str, target_root: str, prune: bool = False) -> dict[str, int]:
    source_root = os.path.abspath(source_root)
    target_root = os.path.abspath(target_root)
    _assert_disjoint(source_root, target_root)
    src = source_manifest(source_root)
    if os.path.isdir(target_root):
        dst = target_manifest(target_root)
    else:
        dst = {}
    missing, different, extra = compare_maps(src, dst)
    for rel in missing + different:
        copy_atomic(os.path.join(source_root, rel),
                    _safe_dest(target_root, rel))
    pruned = 0
    if prune:
        for rel in extra:
            dest = _safe_dest(target_root, rel)
            _ensure_regular(dest, "待清理的运行时文件")
            os.remove(dest)
            pruned += 1
    # 写入后必须重新校验，不允许“命令成功但内容不同”。
    src_after = source_manifest(source_root)
    dst_after = target_manifest(target_root) if os.path.isdir(target_root) else {}
    m2, d2, _ = compare_maps(src_after, dst_after)
    if m2 or d2:
        raise SyncError(f"同步后仍有漂移：缺失 {len(m2)}，不同 {len(d2)}")
    return {"written": len(missing) + len(different), "pruned": pruned,
            "extra": len(extra) - pruned}


def _looks_like_copilot_skill(target: str) -> bool:
    skill_md = os.path.join(target, "SKILL.md")
    if not os.path.isfile(skill_md):
        return False
    try:
        with open(skill_md, encoding="utf-8", errors="replace") as fh:
            return re.search(r"(?m)^name:\s*copilot\s*$", fh.read(4096)) is not None
    except OSError:
        return False


def write_baseline(repo_root: str) -> tuple[str, int]:
    src = source_manifest(repo_root)
    out = os.path.join(repo_root, BASELINE_PATH)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    lines = [f"{digest}  {rel}\n" for rel, digest in sorted(src.items())]
    fd, tmp = tempfile.mkstemp(prefix=".baseline-", dir=os.path.dirname(out))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return out, len(src)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Copilot 源码 → 运行时单向下发")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只检查漂移（默认）")
    mode.add_argument("--apply", action="store_true", help="写入缺失/不同的文件")
    mode.add_argument("--baseline", action="store_true",
                      help=f"记录分发文稿的 SHA-256 基线到 {BASELINE_PATH}")
    ap.add_argument("--prune", action="store_true",
                    help="配合 --apply：删除 Skill 运行时里源码不存在的文件（默认关闭）")
    ap.add_argument("--skill-dest", default=DEFAULT_SKILL_DEST,
                    help=f"Skill 运行时目录（默认 {DEFAULT_SKILL_DEST}）")
    ap.add_argument("--force", action="store_true",
                    help="目标还不是 Copilot skill 时仍允许写入（仅首次安装/测试用）")
    args = ap.parse_args(argv)

    if args.prune and not args.apply:
        print("错误：--prune 只能与 --apply 一起使用。", file=sys.stderr)
        return 2

    if args.baseline:
        try:
            path, count = write_baseline(REPO_ROOT)
        except (SyncError, OSError) as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
        print(f"已写入基线：{path}（{count} 个文件）")
        return 0

    skill_dest = os.path.abspath(os.path.expanduser(args.skill_dest))

    try:
        _assert_disjoint(REPO_ROOT, skill_dest)
        src = source_manifest(REPO_ROOT)
        if os.path.isdir(skill_dest):
            dst = target_manifest(skill_dest)
        else:
            dst = {}
    except (SyncError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    missing, different, extra = compare_maps(src, dst)
    print(f"源码：{REPO_ROOT}（分发 {len(src)} 个文件）")
    print(f"Skill：{skill_dest}")
    print(f"缺失：{len(missing)} · 不同：{len(different)} · "
          f"运行时不参与分发/多出：{len(extra)}")
    for rel in missing:
        print(f"  + {rel}")
    for rel in different:
        print(f"  ~ {rel}")
    for rel in extra:
        print(f"  - {rel}（未受管理，默认保留）")

    if not args.apply:
        if missing or different:
            print("\n结论：存在漂移。确认后用 --apply 单向写入。")
            return 1
        print("\n结论：受管理文件一致（缺失 0，不同 0）。")
        return 0

    if not args.force and not _looks_like_copilot_skill(skill_dest):
        print(f"错误：{skill_dest} 不像 Copilot skill（缺 SKILL.md 或 name: copilot）；"
              "确认目标后用 --force。", file=sys.stderr)
        return 2

    try:
        result = apply_tree(REPO_ROOT, skill_dest, prune=args.prune)
    except (SyncError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print(f"\n已写入 Skill：{result['written']} 个文件"
          + (f"，清理 {result['pruned']} 个" if args.prune else "；未清理运行时多出文件"))
    if result["extra"] and not args.prune:
        print(f"运行时仍有 {result['extra']} 个未受管理文件（默认保留）。")

    # 写后复查：受管理文件必须一致。
    try:
        src2 = source_manifest(REPO_ROOT)
        dst2 = target_manifest(skill_dest)
        m2, d2, _ = compare_maps(src2, dst2)
    except (SyncError, OSError) as exc:
        print(f"错误：同步后复查失败：{exc}", file=sys.stderr)
        return 1
    if m2 or d2:
        print(f"错误：同步后复查不一致（Skill 缺失 {len(m2)}，不同 {len(d2)}）。",
              file=sys.stderr)
        return 1
    print("同步后复查：Skill 缺失 0、不同 0。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

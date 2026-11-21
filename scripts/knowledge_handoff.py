#!/usr/bin/env python3
"""知识交接块：把主 Agent 已核验的 Memory/Vault 知识转成可审阅的 Markdown。

设计边界：

- 只接受 Memory/Vault 正式目录内的卡片；不是通用文件读取器。
- `read` 状态的摘录必须真实出现在卡片正文中，防止把路径或摘要冒充已读。
- `index-only` / `unverified` 明确标注，不能携带“已确认”结论。
- 输出只含必要摘录，默认上限 8,000 字符；SOL 与子 Agent 共用同一份结构。

用法：

    knowledge_handoff.py build --input handoff.json --output handoff.md
    knowledge_handoff.py build --input handoff.json --output - --target subagent
    knowledge_handoff.py check --input handoff.json

输入 JSON::

    {
      "entries": [
        {
          "id": "K1",
          "path": "/media/vdb/knowledge/codex-knowledge/.../card.md",
          "kind": "vault",
          "status": "read",
          "verified_at": "2026-11-18",
          "read_evidence": "可选的同轮读取证据（会话 ID / 工具调用），脚本不验证",
          "decision": "已确认的历史决策",
          "constraints": ["约束一", "约束二"],
          "excerpt": "卡片正文中的原文片段",
          "impact": "对当前任务的影响"
        }
      ]
    }
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import lib_common as C  # noqa: E402

MAX_ENTRIES = 5
MAX_ENTRY_EXCERPT = 1600
TARGET_LIMITS = {"sol": 8000, "subagent": 5000}
STATUSES = {"read", "index-only", "unverified"}
KINDS = {"memory", "vault", "file"}
ALLOWED_ROOTS = (
    os.path.realpath("/media/vdb/knowledge/codex-knowledge"),
    os.path.realpath(os.path.expanduser("~/.zcode/cli/memories/projects")),
)
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,16}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+\-Z]*)?$")


class HandoffError(Exception):
    """校验失败；CLI 以 `{ok:false, code, message}` 输出并退出 2。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(code: str, message: str) -> None:
    raise HandoffError(code, message)


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _load_json(path: str) -> dict:
    if path == "-":
        text = sys.stdin.read()
    else:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            _fail("INPUT_UNREADABLE", f"无法读取交接输入 {path}: {exc}")
    try:
        data = json.loads(text)
    except ValueError as exc:
        _fail("INPUT_INVALID_JSON", f"交接输入不是合法 JSON: {exc}")
    if not isinstance(data, dict):
        _fail("INPUT_INVALID", "交接输入必须是 JSON 对象。")
    return data


def _check_date(value: str) -> None:
    if not value or not _DATE_RE.match(value.strip()):
        _fail("VERIFIED_AT_INVALID", f"verified_at 不是日期: {value!r}")
    try:
        date.fromisoformat(value.strip()[:10])
    except ValueError:
        _fail("VERIFIED_AT_INVALID", f"verified_at 不是有效日期: {value!r}")


def _resolve_card(path: str) -> str:
    if not path or not os.path.isabs(path):
        _fail("PATH_INVALID", "path 必须是绝对路径。")
    real = os.path.realpath(path)
    if not any(real == root or real.startswith(root + os.sep)
               for root in ALLOWED_ROOTS):
        _fail("OUT_OF_SCOPE",
              "知识交接只允许 Memory/Vault 正式目录内的卡片。")
    if C.is_sensitive_path(real):
        _fail("SENSITIVE_PATH", "拒绝把可能含凭据的文件作为知识交接。")
    return real


def _infer_kind(path: str) -> str:
    real = os.path.realpath(path)
    if real.startswith(os.path.realpath(ALLOWED_ROOTS[0]) + os.sep):
        return "vault"
    if real.startswith(os.path.realpath(ALLOWED_ROOTS[1]) + os.sep):
        return "memory"
    return "file"


def _string_list(value: object, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        _fail("FIELD_INVALID", f"{field} 必须是字符串数组。")
    result = [v.strip() for v in value if v.strip()]
    if len(result) > 5:
        _fail("FIELD_INVALID", f"{field} 最多 5 条。")
    return result


def _load_entry(raw: object, index: int) -> dict:
    if not isinstance(raw, dict):
        _fail("ENTRY_INVALID", f"第 {index} 个 entry 不是对象。")
    entry_id = str(raw.get("id") or f"K{index}").strip()
    if not _ID_RE.match(entry_id):
        _fail("ENTRY_ID_INVALID", f"entry id 不合法: {entry_id!r}")
    path = str(raw.get("path") or "").strip()
    real = _resolve_card(path)
    status = str(raw.get("status") or "").strip()
    if status not in STATUSES:
        _fail("STATUS_INVALID", f"{entry_id} 的 status 必须是 {sorted(STATUSES)}。")
    if status in {"read", "index-only"} and not os.path.isfile(real):
        _fail("CARD_NOT_FOUND", f"{entry_id} 的卡片不存在: {path}")
    kind = str(raw.get("kind") or _infer_kind(real)).strip()
    if kind not in KINDS:
        _fail("KIND_INVALID", f"{entry_id} 的 kind 必须是 {sorted(KINDS)}。")
    verified_at = str(raw.get("verified_at") or "").strip()
    if status in {"read", "index-only"}:
        _check_date(verified_at)
    elif verified_at:
        _check_date(verified_at)
    decision = str(raw.get("decision") or "").strip()
    constraints = _string_list(raw.get("constraints"), f"{entry_id}.constraints")
    impact = str(raw.get("impact") or "").strip()
    if not impact:
        _fail("IMPACT_REQUIRED", f"{entry_id} 缺少 impact（对当前任务的影响）。")
    excerpt = str(raw.get("excerpt") or "").strip()
    read_evidence = str(raw.get("read_evidence") or "").strip()
    if len(read_evidence) > 300:
        _fail("READ_EVIDENCE_TOO_LARGE",
              f"{entry_id} 的 read_evidence 超过 300 字符。")
    if status == "read":
        if not decision:
            _fail("DECISION_REQUIRED", f"{entry_id} 是 read，必须写已确认的决策。")
        if not excerpt:
            _fail("EXCERPT_REQUIRED",
                  f"{entry_id} 是 read，必须附卡片正文摘录；路径不等于已传递正文。")
        if len(excerpt) > MAX_ENTRY_EXCERPT:
            _fail("EXCERPT_TOO_LARGE",
                  f"{entry_id} 摘录 {len(excerpt)} 字符，单条上限 {MAX_ENTRY_EXCERPT}。")
        if not os.path.isfile(real):
            _fail("CARD_NOT_FOUND", f"{entry_id} 的卡片不存在: {path}")
        try:
            with open(real, "r", encoding="utf-8") as fh:
                body = fh.read()
        except OSError as exc:
            _fail("CARD_UNREADABLE", f"{entry_id} 无法读取卡片: {exc}")
        if _norm_ws(excerpt) not in _norm_ws(body):
            _fail("EXCERPT_NOT_FOUND",
                  f"{entry_id} 的摘录未在卡片正文中找到；拒绝把路径或摘要冒充已读。")
    elif status == "index-only":
        if excerpt or decision:
            _fail("INDEX_ONLY_CLAIM",
                  f"{entry_id} 是 index-only，不得携带正文摘录或已确认决策。")
    else:  # unverified
        if decision and not excerpt:
            _fail("UNVERIFIED_CLAIM",
                  f"{entry_id} 是 unverified，写入决策时必须附来源摘录。")
    return {
        "id": entry_id,
        "path": real,
        "kind": kind,
        "status": status,
        "verified_at": verified_at,
        "decision": decision,
        "constraints": constraints,
        "excerpt": excerpt,
        "read_evidence": read_evidence,
        "impact": impact,
    }


def load_handoff(path: str) -> list[dict]:
    data = _load_json(path)
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        _fail("ENTRIES_REQUIRED", "交接输入必须包含非空 entries 数组。")
    if len(entries) > MAX_ENTRIES:
        _fail("TOO_MANY_ENTRIES", f"最多 {MAX_ENTRIES} 条知识交接，当前 {len(entries)} 条。")
    loaded = [_load_entry(raw, i + 1) for i, raw in enumerate(entries)]
    ids = [e["id"] for e in loaded]
    if len(ids) != len(set(ids)):
        _fail("ENTRY_ID_DUPLICATE", "entry id 不能重复。")
    return loaded


def _status_text(entry: dict) -> str:
    status = entry["status"]
    if status == "read":
        return ("read（主代理声明已读；脚本已核对摘录出现在卡片正文；"
                f"核验时间 {entry['verified_at']}）")
    if status == "index-only":
        return ("index-only（仅索引，未读正文；不得作为结论；"
                f"索引核验时间 {entry['verified_at']}）")
    return "unverified（未核实；不得作为当前事实）"


def render(entries: list[dict], target: str = "sol") -> str:
    if target not in TARGET_LIMITS:
        _fail("TARGET_INVALID", f"target 必须是 {sorted(TARGET_LIMITS)}。")
    total_chars = sum(len(e["excerpt"]) for e in entries)
    limit = TARGET_LIMITS[target]
    if total_chars > limit:
        _fail("EXCERPT_BUDGET",
              f"摘录合计 {total_chars} 字符，{target} 上限 {limit}；请筛选必要内容。")
    lines = [
        "<!-- copilot-knowledge-handoff -->",
        f"## 知识交接（主 Agent 核验 · {target}）",
        "",
        "> 状态：read=主代理声明已读且脚本核对摘录来源（不证明宿主工具调用）；"
        "index-only=仅索引未读；unverified=未核实。",
        "> 路径只用于追溯；SOL/子 Agent 只能使用下方摘录，不能假设自己读过卡片。",
        "",
    ]
    for entry in entries:
        title = os.path.basename(entry["path"])
        lines.append(f"### {entry['id']} [{entry['kind']}] {title}")
        lines.append(f"- 路径：`{entry['path']}`")
        lines.append(f"- 状态：{_status_text(entry)}")
        if entry.get("read_evidence"):
            lines.append(f"- 阅读证据：{C.redact(entry['read_evidence'])}")
        if entry["decision"]:
            label = "待核实线索" if entry["status"] == "unverified" else "历史决策"
            lines.append(f"- {label}：{C.redact(entry['decision'])}")
        if entry["constraints"]:
            lines.append("- 适用约束：" + "；".join(
                C.redact(item) for item in entry["constraints"]))
        lines.append(f"- 对当前任务的影响：{C.redact(entry['impact'])}")
        if entry["excerpt"]:
            label = "未核实摘录" if entry["status"] == "unverified" else "摘录"
            lines.append(f"- {label}：")
            for line in entry["excerpt"].splitlines():
                lines.append(f"  > {C.redact(line)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_atomic(path: str, text: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".handoff-", dir=parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def render_file(path: str, target: str = "sol") -> str:
    return render(load_handoff(path), target=target)


def block_sha256(text: str) -> str:
    return C.sha256_text(text)


def _summary(entries: list[dict], text: str, target: str) -> dict:
    return {
        "ok": True,
        "target": target,
        "entries": len(entries),
        "read": sum(1 for e in entries if e["status"] == "read"),
        "index_only": sum(1 for e in entries if e["status"] == "index-only"),
        "unverified": sum(1 for e in entries if e["status"] == "unverified"),
        "excerpt_chars": sum(len(e["excerpt"]) for e in entries),
        "block_chars": len(text),
        "sha256": C.sha256_text(text),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建/校验知识交接块")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="校验并输出 Markdown 交接块")
    build.add_argument("--input", required=True, help="交接 JSON；- 表示 stdin")
    build.add_argument("--output", required=True, help="输出 Markdown；- 表示 stdout")
    build.add_argument("--target", choices=sorted(TARGET_LIMITS), default="sol")
    check = sub.add_parser("check", help="只校验，输出 JSON 摘要")
    check.add_argument("--input", required=True, help="交接 JSON；- 表示 stdin")
    check.add_argument("--target", choices=sorted(TARGET_LIMITS), default="sol")
    args = parser.parse_args(argv)
    try:
        entries = load_handoff(args.input)
        text = render(entries, target=args.target)
        if args.command == "check":
            print(json.dumps(_summary(entries, text, args.target),
                             ensure_ascii=False, sort_keys=True))
            return 0
        if args.output == "-":
            sys.stdout.write(text)
        else:
            _write_atomic(args.output, text)
        summary = _summary(entries, text, args.target)
        summary["output"] = args.output
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except HandoffError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "message": exc.message},
                         ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

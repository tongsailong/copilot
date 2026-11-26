#!/usr/bin/env python3
"""用户输入收件箱：把“用户这一回合说了什么”变成可审计、可处置的任务事实。

为什么需要独立文件而不是写 state.json：
- UserPromptSubmit 钩子在用户回合开始时运行，职责是只读提示，不应改写治理状态；
- 但“用户批准/授权/补充条件”如果没有落地，主代理漏跑一次 `unblock`，授权就会
  随上下文一起丢失（v9.13 之前的真实故障）。
- 因此这里用任务目录下的追加式 `.user-inputs.jsonl` 记录原始输入与处置结果：
  钩子只追加一条原始事件（失败放过），治理命令负责 `consume`/`dismiss`。
  Stop 守卫据此判断“本回合还有未处置的用户输入”，在结束前把控制流交回主代理。

口径：
- 不推断授权。`consume` 必须由主代理显式给出 `--block-id`，并把用户原话写入
  阻塞的 `authorization_quote`；本模块只保证原话、时间、会话、处置可复核。
- 不把普通寒暄变成强制工作项：`actionable` 只由“继续/授权/决策类措辞”或
  “命中某个未解除阻塞的条件词”决定；不 actionalbe 的输入只记录，不拦截 Stop。
- 失败不影响用户回合：钩子侧任何异常都放行（调用方负责 try/except）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

INBOX_FILE = ".user-inputs.jsonl"
SCHEMA = "copilot-user-input/v1"
MAX_TEXT_CHARS = 4000
MAX_FILE_BYTES = 512 * 1024
KEEP_DISPOSITIONS = 40
DEDUPE_WINDOW_S = 300

# 明确表示“继续/授权/决策”的措辞。命中即视为可处置输入；否定词（不要/暂不/
# 停止）同样需要处置（用户可能是在撤销授权或要求停下），因此一并计入。
ACTION_WORDS = (
    "继续", "恢复", "下一步", "接着", "开始", "执行", "推进", "往下",
    "批准", "同意", "授权", "允许", "放行", "可以", "没问题", "按此",
    "按这个", "按你说的", "就这么", "照此", "好的", "行", "确认", "通过",
    "补上", "补齐", "已登录", "登录了", "已完成", "办好了", "处理好了",
    "停止", "暂停", "取消", "不用", "不要", "暂不", "先不",
)
# 纯寒暄/确认收到：不构成任务处置，避免每句“谢谢”都拦一次 Stop。
LOW_SIGNAL = (
    "你好", "您好", "谢谢", "多谢", "收到", "好的谢谢", "嗯", "哦", "ok",
    "okay", "好", "行吧", "辛苦了",
)
TOKEN_RE = re.compile(r"[0-9A-Za-z_./+-]{2,}|[\u4e00-\u9fff]{2,}")
STOP_TOKENS = {
    "需要", "提供", "确认", "用户", "后台", "条件", "授权", "决策", "资源",
    "工作项", "完成", "原因", "处理", "支持", "进行", "已经", "现在", "一个",
    "这个", "那个", "问题", "接口", "数据", "平台", "功能", "检查", "验证",
    "无法", "不能", "必须", "应该", "可以", "是否", "以及", "或者", "并且",
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _task_dir(state_path: str) -> str:
    return os.path.dirname(os.path.abspath(state_path))


def inbox_path(state_path: str) -> str:
    return os.path.join(_task_dir(state_path), INBOX_FILE)


def _load_raw(path: str) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _write_raw(path: str, rows: list[dict]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".user-inputs-", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _append(path: str, row: dict) -> None:
    """追加一行；先做一次轻量压缩，避免无限增长。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    if size > MAX_FILE_BYTES:
        _compact(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _compact(path: str) -> None:
    rows = _load_raw(path)
    records = [row for row in rows if row.get("op") in (None, "", "record")]
    dispositions = [row for row in rows if row.get("op") in ("consume", "dismiss")]
    pending_ids = {str(row.get("input_id") or "") for row in records}
    pending_ids -= {str(row.get("input_id") or "") for row in dispositions}
    keep = [row for row in records
            if str(row.get("input_id") or "") in pending_ids][-50:]
    keep += records[-10:]
    keep += dispositions[-KEEP_DISPOSITIONS:]
    # 去重但保持原始顺序
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for row in keep:
        key = (str(row.get("op") or "record"), str(row.get("input_id") or ""),
               str(row.get("at") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    try:
        _write_raw(path, out)
    except OSError:
        pass


def events(state_path: str) -> list[dict]:
    """只返回输入事件（不含处置行），按时间正序。"""
    return [row for row in _load_raw(inbox_path(state_path))
            if row.get("op") in (None, "", "record")
            and str(row.get("input_id") or "")]


def dispositions(state_path: str) -> dict[str, dict]:
    """input_id → 最近一次处置行。"""
    out: dict[str, dict] = {}
    for row in _load_raw(inbox_path(state_path)):
        if row.get("op") not in ("consume", "dismiss"):
            continue
        iid = str(row.get("input_id") or "")
        if iid:
            out[iid] = row
    return out


def pending(state_path: str) -> list[dict]:
    """未处置的输入事件（按时间正序）。"""
    done = dispositions(state_path)
    return [row for row in events(state_path)
            if str(row.get("input_id") or "") not in done]


def get(state_path: str, input_id: str) -> tuple[dict, dict]:
    iid = str(input_id or "").strip()
    for row in events(state_path):
        if str(row.get("input_id") or "") == iid:
            return row, dispositions(state_path).get(iid, {})
    return {}, {}


def _tokens(text: str) -> set[str]:
    return {token for token in TOKEN_RE.findall(text or "")
            if token not in STOP_TOKENS}


def candidates(state: dict, text: str) -> list[dict]:
    """把输入与未解除阻塞做确定性匹配；只用于给建议命令，不自动解除。"""
    text_tokens = _tokens(text)
    rows: list[dict] = []
    for block in state.get("blocks") or []:
        if not isinstance(block, dict) or block.get("resolved_at"):
            continue
        bid = str(block.get("block_id") or block.get("id") or "")
        needs = str(block.get("needs") or block.get("need") or "")
        reason = str(block.get("reason") or "")
        need_tokens = _tokens(needs) | _tokens(reason)
        hits = sorted(text_tokens & need_tokens)
        score = len(hits)
        if bid and bid in (text or ""):
            score += 10
            hits = [bid, *hits]
        rows.append({
            "block_id": bid,
            "scope": str(block.get("scope") or ""),
            "work_items": [str(item) for item in block.get("work_items") or []],
            "reason": reason,
            "needs": needs,
            "score": score,
            "matched": hits[:6],
        })
    rows.sort(key=lambda row: (-row["score"], row["block_id"]))
    return rows


def _is_actionable(text: str, matches: list[dict]) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    if lowered in {word.lower() for word in LOW_SIGNAL}:
        return False
    if any(row.get("score", 0) > 0 for row in matches):
        return True
    return any(word in lowered for word in ACTION_WORDS)


def actionable(text: str, state: dict | None = None) -> bool:
    """这条用户输入是否构成需要处置的任务事实（授权/决策/继续/命中阻塞条件）。

    钩子在写收件箱前先用它过滤寒暄，避免每句“谢谢”都变成待处置输入。
    """
    matches = candidates(state or {}, text) if state else []
    return _is_actionable(text, matches)


def record(state_path: str, text: str, *, session: str = "",
           source: str = "user", task_id: str = "",
           state: dict | None = None) -> dict:
    """记录一条用户输入；返回事件（含 actionable / candidates）。

    幂等：同一会话在 300 秒内重复提交同一句话（宿主重试/重复钩子）只保留一条。
    """
    body = (text or "").strip()[:MAX_TEXT_CHARS]
    if not body:
        return {}
    digest = _sha(body)
    path = inbox_path(state_path)
    existing = events(state_path)
    for row in reversed(existing):
        if (str(row.get("text_sha256") or "") == digest
                and str(row.get("session") or "") == str(session or "")
                and _within(row.get("at"), DEDUPE_WINDOW_S)):
            return row
    iid = "U1"
    for row in existing:
        value = str(row.get("input_id") or "")
        match = re.fullmatch(r"U(\d+)", value)
        if match:
            iid = f"U{max(int(iid[1:]), int(match.group(1))) + 1}"
    matches = candidates(state or {}, body) if state else []
    actionable = _is_actionable(body, matches)
    row = {
        "schema": SCHEMA,
        "input_id": iid,
        "task_id": str(task_id or (state or {}).get("task_id") or ""),
        "session": str(session or ""),
        "source": str(source or "user"),
        "at": _now(),
        "text": body,
        "text_sha256": digest,
        "chars": len(body),
        "actionable": bool(actionable),
        "candidate_blocks": [match["block_id"] for match in matches
                             if match.get("score", 0) > 0][:5],
    }
    _append(path, row)
    return row


def _within(value: object, seconds: int) -> bool:
    stamp = str(value or "")
    if not stamp:
        return False
    try:
        at = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - at < timedelta(seconds=seconds)


def _write_disposition(state_path: str, row: dict) -> dict:
    _append(inbox_path(state_path), row)
    return row


def consume(state_path: str, input_id: str, *, block_ids: list[str],
            reason: str, by: str = "user", decision_id: str = "",
            authorization_source: str = "") -> dict:
    """标记输入已处置，并记录它解除了哪些阻塞（授权原话由调用方写入 block）。"""
    event, done = get(state_path, input_id)
    if not event:
        raise ValueError(f"输入事件不存在：{input_id}")
    if done:
        return done
    row = {
        "schema": SCHEMA,
        "op": "consume",
        "input_id": str(input_id),
        "at": _now(),
        "by": str(by or "user"),
        "block_ids": [str(item) for item in block_ids if str(item or "")],
        "decision_id": str(decision_id or ""),
        "reason": (reason or "")[:MAX_TEXT_CHARS],
        "authorization_source": str(authorization_source or ""),
    }
    return _write_disposition(state_path, row)


def dismiss(state_path: str, input_id: str, *, reason: str,
            by: str = "zcode") -> dict:
    event, done = get(state_path, input_id)
    if not event:
        raise ValueError(f"输入事件不存在：{input_id}")
    if done:
        return done
    row = {
        "schema": SCHEMA,
        "op": "dismiss",
        "input_id": str(input_id),
        "at": _now(),
        "by": str(by or "zcode"),
        "reason": (reason or "")[:MAX_TEXT_CHARS],
    }
    return _write_disposition(state_path, row)


def actionable_pending(state_path: str) -> list[dict]:
    """需要在本回合处置的输入：actionable 且未处置。"""
    return [row for row in pending(state_path) if row.get("actionable")]


def quote(text: str, limit: int = 300) -> str:
    body = (text or "").strip().replace("\n", " ")
    return body if len(body) <= limit else body[:limit - 1] + "…"


def suggested_commands(state_path: str, state: dict, event: dict,
                       matches: list[dict] | None = None) -> list[str]:
    """按候选阻塞给出可直接执行的处置命令（不含 <占位符> 的仅唯一命中时）。"""
    matches = matches if matches is not None else candidates(state, event.get("text", ""))
    state_q = state_path
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "copilot_dispatch.py")
    lines: list[str] = []
    scored = [row for row in matches if row.get("score", 0) > 0]
    if len(scored) == 1:
        lines.append(
            f"python3 -B {script} input --state {state_q} --consume "
            f"{event.get('input_id')} --block-id {scored[0]['block_id']} "
            "--reason \"<依据：用户原话满足了哪条 needs>\"")
    elif scored:
        for row in scored[:3]:
            lines.append(
                f"python3 -B {script} input --state {state_q} --consume "
                f"{event.get('input_id')} --block-id {row['block_id']} "
                "--reason \"<依据>\"")
    else:
        lines.append(
            f"python3 -B {script} input --state {state_q} --consume "
            f"{event.get('input_id')} --block-id <B...> --reason \"<依据>\"")
    lines.append(
        f"python3 -B {script} input --state {state_q} --dismiss "
        f"{event.get('input_id')} --reason \"<与任务无关/不构成授权>\"")
    return lines

#!/usr/bin/env python3
"""工程任务首轮自动召回：确定性分类、有界检索、去重注入。

目标不是“每次都检索”，而是让真实工程任务在开始时自然拿到一次与任务相关的历史经验，
并让同一会话的后续回合复用已召回结果：

* 分类：只按提示词与工作区判定“是否工程任务”，闲聊/短指令/纯知识问答不触发；
* 检索：复用 `context_engine.recall` 的白名单与预算（Vault 正式区 + 当前项目 Memory），
  零模型调用；失败、零命中、截断分别可观察；
* 去重：同一会话同一项目只做一次完整召回；出现新主题时最多增量一次；
* 交付：注入精简结论（路径/行号/核验时间/片段），不注入整卡。

本模块只读知识库，状态写入 `~/.zcode/copilot-runs/context-recall/`（会话去重与事件日志），
不写 Vault、不写项目 Memory、不改 Copilot 任务状态。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in os.sys.path:
    os.sys.path.insert(0, HERE)

STATE_SCHEMA = "copilot-recall-state/v1"
DEFAULT_STATE_ROOT = os.path.expanduser("~/.zcode/copilot-runs/context-recall")
DEFAULT_TIME_BUDGET = 1.2
DEFAULT_MAX_LINES = 40
DEFAULT_MAX_FILES = 1200
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
INJECT_LIMIT = 1400
MAX_TERMS = 8
MAX_SNIPPETS = 5
MAX_MEMORY_SNIPPETS = 3
MAX_VAULT_SNIPPETS = 2
MAX_FILES = 3
INCREMENTAL_COOLDOWN_S = 60
SESSION_RECALL_CAP = 4

# 领域词表：命中即视为强信号，并把变体归一为一个检索词。
LEXICON: dict[str, tuple[str, ...]] = {
    "memory": ("memory", "记忆", "MEMORY.md", "项目记忆", "记忆库"),
    "recall": ("recall", "召回", "知识检索", "检索知识"),
    "capture": ("capture", "沉淀", "知识沉淀", "落库", "写卡", "候选"),
    "vault": ("vault", "知识库", "codex-knowledge"),
    "hook": ("hook", "钩子", "userpromptsubmit", "sessionstart", "stop 钩子"),
    "copilot": ("copilot", "治理"),
    "context-engine": ("context_engine", "context engine", "上下文引擎"),
    "knowledge-manager": ("knowledge-manager", "knowledge manager"),
    "test": ("测试", "单测", "回归", "unittest", "pytest"),
    "bug": ("bug", "报错", "错误", "异常", "崩溃", "失败", "error", "exception", "traceback"),
    "build": ("构建", "编译", "build", "gradle", "cmake", "打包"),
    "deploy": ("部署", "发布", "deploy", "release"),
    "git": ("git", "commit", "提交", "分支", "merge", "rebase", "冲突"),
    "adb": ("adb", "真机", "装包", "安装包"),
    "sign": ("签名", "keystore", "signing"),
    "perf": ("性能", "耗时", "延迟", "token", "performance", "latency"),
    "refactor": ("重构", "refactor", "拆分"),
    "fix": ("修复", "排查", "定位", "复现", "修好", "investigate", "debug"),
    "config": ("配置", "config", "设置", "settings", "环境变量"),
    "android": ("android", "安卓", "apk"),
    "subagent": ("子agent", "子 agent", "subagent", "explore", "general-purpose"),
    "skill": ("skill", "技能", "SKILL.md"),
    "agent": ("agent", "代理", "主代理"),
    "prompt": ("prompt", "提示词"),
    "session": ("session", "会话", "resume", "compact", "压缩", "续跑"),
    "permission": ("权限", "permission", "授权"),
    "security": ("安全", "security", "敏感", "凭据", "secret", "脱敏"),
    "ci": ("ci", "github actions", "workflow", "流水线"),
    "docs": ("readme", "文档", "readme.md", "changelog", "注释", "docstring"),
    "install": ("安装", "install", "装包", "setup"),
    "python": ("python", "python3"),
}

STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "when", "where",
    "what", "how", "why", "please", "help", "you", "your", "our", "are", "was",
    "were", "have", "has", "not", "but", "can", "could", "should", "would", "will",
    "just", "then", "than", "them", "they", "there", "here", "about", "after",
    "before", "need", "make", "use", "using", "get", "set", "let", "one", "two",
    "true", "false", "none", "null", "ok", "okay",
}
SOCIAL_RE = re.compile(
    r"^(你好|您好|hi|hello|hey|谢谢|感谢|thanks|thank you|ok|okay|好的|好|收到|"
    r"嗯|可以|行|对|继续|continue|go on)[。.!！?？~～\s]*$", re.I)
PURE_QUESTION_RE = re.compile(
    r"^(什么是|啥是|解释一下|解释下|介绍一下|说明一下|区别是什么|有什么区别|"
    r"如何理解|怎么理解)", re.I)
ACTION_RE = re.compile(
    r"(帮我|请帮|请你|实现|修复|排查|检查|分析|重构|优化|升级|迁移|部署|安装|配置|"
    r"生成|写一个|加一个|去掉|定位|复现|看看这个|看下这个|review|fix|implement|debug|"
    r"investigate|add |update |change |refactor)", re.I)
CODE_OR_PATH_RE = re.compile(
    r"(```|`[^`]{2,}`|\b[\w./-]+\.(?:py|js|ts|tsx|md|json|sh|bash|yml|yaml|java|kt|"
    r"go|rs|c|cpp|h|apk|gradle|toml|ini)\b)")
COMMAND_RE = re.compile(
    r"(^|\s)(?:git|python3?|pip|npm|pnpm|yarn|gradle|adb|bash|sh|zsh|curl|docker|"
    r"systemctl|ls|cd|grep|sed|awk|find|make|node)\s", re.I)
ENGLISH_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{2,}")
CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
CJK_GENERIC = {
    "这个", "那个", "我们", "你们", "他们", "可以", "需要", "问题", "一个", "进行",
    "使用", "没有", "如果", "就是", "不是", "已经", "现在", "然后", "因为", "所以",
    "但是", "或者", "以及", "还是", "这样", "那样", "什么", "为什么", "怎么", "如何",
    "帮我", "请你", "看看", "一下", "时候", "东西", "地方", "方式", "情况",
}
# 这些词在跨项目知识里普遍出现，命中它们不足以证明与当前任务相关。
GENERIC_TERMS = {
    "test", "bug", "fix", "build", "git", "config", "perf", "docs", "install",
    "deploy", "python", "ci", "session", "skill", "agent", "prompt", "permission",
    "security", "android", "subagent", "refactor",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _digest(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:n]


def _git_root(cwd: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd,
            capture_output=True, text=True, timeout=0.8, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    root = (proc.stdout or "").strip()
    return root if proc.returncode == 0 and os.path.isdir(root) else ""


def classify(prompt: str) -> dict:
    """只按文本判断是否值得召回；不访问文件系统。"""
    text = (prompt or "").strip()
    if not text:
        return {"engineering": False, "reason": "empty", "strong": 0, "terms": []}
    if text.startswith("/") and "\n" not in text:
        return {"engineering": False, "reason": "slash-command", "strong": 0, "terms": []}
    if SOCIAL_RE.match(text):
        return {"engineering": False, "reason": "social", "strong": 0, "terms": []}
    strong = set()
    lowered = text.lower()
    for canonical, variants in LEXICON.items():
        if any(v in lowered for v in variants):
            strong.add(canonical)
    has_code = bool(CODE_OR_PATH_RE.search(text))
    has_cmd = bool(COMMAND_RE.search(text))
    action = bool(ACTION_RE.search(text))
    pure_question = bool(PURE_QUESTION_RE.match(text))
    if pure_question and not (has_code or action):
        return {"engineering": False, "reason": "pure-question",
                "strong": len(strong), "terms": sorted(strong)}
    if len(strong) >= 2:
        return {"engineering": True, "reason": "multi-signal",
                "strong": len(strong), "terms": sorted(strong)}
    if strong and (action or has_code or has_cmd):
        return {"engineering": True, "reason": "signal+action",
                "strong": len(strong), "terms": sorted(strong)}
    if (has_code or has_cmd) and action:
        return {"engineering": True, "reason": "code+action",
                "strong": len(strong), "terms": sorted(strong)}
    if strong and len(text) >= 12:
        return {"engineering": True, "reason": "signal",
                "strong": len(strong), "terms": sorted(strong)}
    return {"engineering": False, "reason": "no-signal",
            "strong": len(strong), "terms": sorted(strong)}


def extract_terms(prompt: str, *, limit: int = MAX_TERMS) -> tuple[list[str], set[str]]:
    """返回 (检索词, 强检索词集合)。强词用于命中过滤，避免通用短语带来噪声。"""
    text = (prompt or "").strip()
    lowered = text.lower()
    ordered: list[str] = []
    strong: set[str] = set()

    def add(term: str, *, is_strong: bool) -> None:
        term = term.strip().strip(".,;:()[]{}<>\"'")
        if not term or len(term) > 40 or term.lower() in STOPWORDS:
            return
        if term not in ordered:
            ordered.append(term)
        if is_strong:
            strong.add(term)

    for canonical, variants in LEXICON.items():
        if any(v in lowered for v in variants):
            add(canonical, is_strong=True)
    for token in ENGLISH_RE.findall(text):
        token = token.strip("/:.,;")
        if token.lower() in STOPWORDS or len(token) < 3:
            continue
        # 不把长十六进制串/疑似凭据带进检索与状态。
        if len(token) >= 32 and re.fullmatch(r"[0-9a-fA-F]+", token):
            continue
        add(token, is_strong=True)
    if not strong:
        grams: list[str] = []
        for run in CJK_RUN_RE.findall(text):
            for i in range(0, max(0, len(run) - 2)):
                gram = run[i:i + 3]
                if gram in CJK_GENERIC:
                    continue
                if any(ch in gram for ch in "的了和与及或"):
                    continue
                if gram not in grams:
                    grams.append(gram)
        for gram in grams[:3]:
            add(gram, is_strong=False)
    return ordered[:limit], strong


def _safe_session(session: str) -> str:
    return _digest(session or "anonymous", 24)


def state_path(session: str, *, state_root: str = "") -> str:
    root = state_root or os.path.expanduser(
        os.environ.get("COPILOT_RECALL_STATE_DIR", DEFAULT_STATE_ROOT))
    return os.path.join(root, "sessions", _safe_session(session) + ".json")


def load_state(session: str, *, state_root: str = "") -> dict:
    path = state_path(session, state_root=state_root)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=1))
    try:
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _append_event(record: dict, *, state_root: str = "") -> None:
    root = state_root or os.path.expanduser(
        os.environ.get("COPILOT_RECALL_STATE_DIR", DEFAULT_STATE_ROOT))
    path = os.path.join(root, "events.jsonl")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 512 * 1024:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _label(hit: dict) -> str:
    rel = str(hit.get("rel") or hit.get("path") or "")
    if hit.get("source") == "memory":
        rel = rel.removeprefix("memory/")
        return f"Memory/{rel}"
    return f"Vault/{rel}"


def _render(result: dict, *, project: dict, mode: str) -> str:
    status = str(result.get("status") or "unknown")
    hits = list(result.get("hits") or [])
    if not hits:
        return ""
    head = (result.get("git") or {}).get("head") or ""
    selected: list[dict] = []
    files: list[str] = []
    memory_n = vault_n = 0
    # 先项目 Memory（同项目已隔离），再跨项目 Vault；两个来源都保留名额。
    for source, quota in (("memory", MAX_MEMORY_SNIPPETS), ("vault", MAX_VAULT_SNIPPETS)):
        for hit in hits:
            if str(hit.get("source") or "") != source:
                continue
            used = memory_n if source == "memory" else vault_n
            if used >= quota:
                break
            path = str(hit.get("path") or "")
            if path not in files:
                if len(files) >= MAX_FILES:
                    continue
                files.append(path)
            selected.append(hit)
            if source == "memory":
                memory_n += 1
            else:
                vault_n += 1
    if not selected:
        return ""
    note = ""
    if status == "partial":
        note = "（部分来源不可读，结果不完整）"
    elif status == "budget_exhausted":
        note = "（命中多于展示上限，仅展示高相关片段）"
    lines = [
        "<copilot-recall>",
        f"[自动召回·{mode}] 项目 {project.get('basename') or project.get('root')}"
        f" @ {(head or '未知')[:12]}；检索状态 {status}{note}",
        "历史线索（已实际读取，可直接复用；不必重复扫描）：",
    ]
    for hit in selected:
        meta = hit.get("meta") or {}
        tags = []
        verified = str(meta.get("last_verified") or "").strip()
        tags.append("核验 " + verified if verified else "核验未标注")
        commit = str(meta.get("source_commit") or "").strip()
        commit_sha = commit if re.fullmatch(r"[0-9a-fA-F]{7,40}", commit) else ""
        if commit_sha:
            drift = bool(head) and not (head.startswith(commit_sha)
                                        or commit_sha.startswith(head))
            tags.append(f"基线 {commit_sha[:8]}"
                        + ("（与当前 HEAD 不同，按现场核验）" if drift else ""))
        if str(meta.get("status") or "") not in ("", "active"):
            tags.append(str(meta.get("status")))
        host = str(meta.get("host_version") or "").strip()
        if host and len(host) <= 32:
            tags.append("适用 " + host)
        snippet = re.sub(r"\s+", " ", str(hit.get("snippet") or "")).strip()
        if len(snippet) > 150:
            snippet = snippet[:147] + "…"
        lines.append(f"- {_label(hit)}:L{hit.get('line')} · " + " · ".join(tags)
                     + f" · {snippet}")
    lines.append("使用规则：与当前代码/运行结果冲突时以现场为准并标注冲突；卡片是历史线索，"
                 "不是当前状态权威；已读内容不要重复整卡加载。")
    lines.append("</copilot-recall>")
    text = "\n".join(lines)
    if len(text) <= INJECT_LIMIT:
        return text
    kept = lines[:3]
    for line in lines[3:-2]:
        trial = "\n".join(kept + [line, lines[-2], lines[-1]])
        if len(trial) > INJECT_LIMIT:
            break
        kept.append(line)
    return "\n".join(kept + [lines[-2], lines[-1]])


def session_recall_lines(session: str, project_root: str, *, state_root: str = "",
                         limit: int = 2) -> list[str]:
    """恢复注入：返回本会话在该项目已召回的精简条目，不重新检索。"""
    state = load_state(session, state_root=state_root)
    project = (state.get("projects") or {}).get(_digest(os.path.realpath(project_root), 16))
    if not project:
        return []
    top = list(project.get("top") or [])[:limit]
    if not top:
        return []
    lines = [f"- 已自动召回 {project.get('recall_count', 0)} 次；以下结论已进入过本会话，"
             "恢复后直接复用，不重复检索："]
    for item in top:
        lines.append(f"  - {item.get('label')} · {item.get('snippet')}")
    return lines


def maybe_inject(payload: dict, *, memory_root: str = "", vault_root: str = "",
                 state_root: str = "", time_budget: float = DEFAULT_TIME_BUDGET,
                 now: float | None = None) -> dict:
    """UserPromptSubmit 主入口。任何异常都返回 injected=False，不阻断用户回合。"""
    started = time.monotonic()
    prompt = str(payload.get("prompt") or "")
    cwd = str(payload.get("cwd") or os.getcwd())
    session = str(payload.get("session_id") or payload.get("sessionId") or "")
    outcome = {"injected": False, "text": "", "reason": "", "status": "",
               "terms": [], "hits": 0, "elapsed_ms": 0, "state_path": ""}
    memory_root = memory_root or os.environ.get("COPILOT_RECALL_MEMORY_ROOT", "")
    vault_root = vault_root or os.environ.get("COPILOT_RECALL_VAULT_ROOT", "")
    state_root = state_root or os.environ.get("COPILOT_RECALL_STATE_DIR", "")
    try:
        if not session:
            outcome["reason"] = "no-session"
            return outcome
        verdict = classify(prompt)
        if not verdict["engineering"]:
            outcome["reason"] = verdict["reason"]
            return outcome
        root = _git_root(cwd)
        if not root:
            # 没有项目身份就不召回：知识库经验离开具体项目容易被误用。
            outcome["reason"] = "no-project-root"
            return outcome
        terms, strong = extract_terms(prompt)
        if not terms:
            outcome["reason"] = "no-terms"
            return outcome
        project_key = _digest(os.path.realpath(root), 16)
        path = state_path(session, state_root=state_root)
        state = load_state(session, state_root=state_root)
        projects = state.setdefault("projects", {})
        prior = projects.get(project_key) or {}
        prior_terms = set(prior.get("strong") or prior.get("terms") or [])
        cur_strong = {_digest(t, 12) for t in strong}
        new_strong = cur_strong - prior_terms
        overlap = len(cur_strong & prior_terms) / max(1, len(cur_strong))
        mode = "initial"
        if prior:
            last = float(prior.get("last_at_epoch") or 0)
            if overlap >= 0.5 and not new_strong:
                outcome["reason"] = "repeat"
                return outcome
            if int(prior.get("recall_count") or 0) >= SESSION_RECALL_CAP:
                outcome["reason"] = "session-cap"
                return outcome
            # 明显换主题（多个新专有词且与上次重合低）允许立即增量召回；
            # 同主题追问走已召回内容，不再检索。
            fresh_topic = len(new_strong) >= 2 and overlap < 0.5
            if (not fresh_topic and now is not None
                    and now - last < INCREMENTAL_COOLDOWN_S):
                outcome["reason"] = "cooldown"
                return outcome
            mode = "incremental"

        import context_engine as CE  # 惰性导入：非工程提示不付加载成本
        project = CE.resolve(
            root, cwd=cwd,
            memory_root=memory_root or CE.DEFAULT_MEMORY_ROOT,
            vault_root=vault_root or CE.DEFAULT_VAULT_ROOT,
            git_timeout=0.8)
        cache_dir = os.path.join(state_root or os.path.expanduser(
            os.environ.get("COPILOT_RECALL_STATE_DIR", DEFAULT_STATE_ROOT)),
            "cache", project_key)
        result = CE.recall(
            project, terms, scope=["vault", "memory"],
            max_lines=DEFAULT_MAX_LINES, max_files=DEFAULT_MAX_FILES,
            max_bytes=DEFAULT_MAX_BYTES, time_budget=time_budget,
            cache_dir=cache_dir, ttl=600.0)
        hits = []
        specific = [t for t in strong if t.lower() not in GENERIC_TERMS]

        def mentions_specific(hit: dict) -> bool:
            hay = (str(hit.get("path") or "") + " "
                   + str(hit.get("snippet") or "")).lower()
            return any(t.lower() in hay for t in specific)

        for hit in result.get("hits") or []:
            score = int(hit.get("score") or 0)
            if hit.get("source") == "memory":
                # 项目 Memory 已按 git 根隔离，弱相关条目也属于同项目上下文。
                if score >= 2:
                    hits.append(hit)
            elif score >= 3 or (score >= 2 and specific and mentions_specific(hit)):
                # 跨项目 Vault：多词命中，或命中了任务里的专有词（路径/模块名/
                # 领域名），才认为与当前任务相关；避免通用词带进无关项目卡片。
                hits.append(hit)
        # 同一路径最多保留 2 条，避免整卡刷屏。
        hits.sort(key=lambda h: (0 if h.get("source") == "memory" else 1,
                                 -int(h.get("score") or 0)))
        kept, per_file = [], {}
        for hit in hits:
            path_key = str(hit.get("path") or "")
            if per_file.get(path_key, 0) >= 2:
                continue
            per_file[path_key] = per_file.get(path_key, 0) + 1
            kept.append(hit)
            if len(kept) >= DEFAULT_MAX_LINES:
                break
        result["hits"] = kept
        outcome["status"] = str(result.get("status") or "")
        outcome["hits"] = len(kept)
        outcome["terms"] = terms
        text = _render(result, project=project["project"], mode=mode)
        outcome["state_path"] = path
        if not text:
            outcome["reason"] = "zero_hit" if result.get("status") == "zero_hit" else "no-hits"
            _append_event({
                "at": _now_iso(), "session": _safe_session(session),
                "project": project_key, "mode": mode,
                "decision": outcome["reason"], "status": outcome["status"],
                "terms": len(terms), "strong": len(strong), "hits": 0,
                "injected_chars": 0,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }, state_root=state_root)
            return outcome
        delivered = [{"path": str(h.get("path") or ""), "line": h.get("line"),
                      "snippet_sha": _digest(str(h.get("snippet") or ""), 12)}
                     for h in kept]
        top = [{"label": _label(h),
                "snippet": re.sub(r"\s+", " ", str(h.get("snippet") or ""))[:180]}
               for h in kept[:2]]
        projects[project_key] = {
            "root": os.path.realpath(root),
            "recall_count": int(prior.get("recall_count") or 0) + 1,
            "last_at": _now_iso(),
            "last_at_epoch": (now if now is not None else time.time()),
            "strong": sorted(prior_terms | cur_strong),
            "delivered": (list(prior.get("delivered") or []) + delivered)[-24:],
            "top": top,
        }
        state["schema"] = STATE_SCHEMA
        state["session_hash"] = _safe_session(session)
        _write_state(path, state)
        outcome.update({"injected": True, "text": text, "reason": mode})
        _append_event({
            "at": _now_iso(), "session": _safe_session(session),
            "project": project_key, "mode": mode, "decision": "injected",
            "status": outcome["status"], "terms": len(terms),
            "strong": len(strong), "hits": len(kept),
            "injected_chars": len(text),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }, state_root=state_root)
        return outcome
    except Exception as exc:  # noqa: BLE001 - 召回失败不得阻断用户回合
        outcome["reason"] = f"error:{type(exc).__name__}"
        _append_event({
            "at": _now_iso(), "session": _safe_session(session),
            "decision": "failed", "error": type(exc).__name__,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }, state_root=state_root)
        return outcome
    finally:
        outcome["elapsed_ms"] = int((time.monotonic() - started) * 1000)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="工程任务自动召回（确定性、只读）")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--session", default="inspect")
    parser.add_argument("--memory-root", default="")
    parser.add_argument("--vault-root", default="")
    parser.add_argument("--state-root", default="")
    parser.add_argument("--time-budget", type=float, default=DEFAULT_TIME_BUDGET)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    outcome = maybe_inject({"prompt": args.prompt, "cwd": args.cwd,
                            "session_id": args.session},
                           memory_root=args.memory_root, vault_root=args.vault_root,
                           state_root=args.state_root, time_budget=args.time_budget)
    if args.json:
        print(json.dumps(outcome, ensure_ascii=False, sort_keys=True))
    else:
        print(outcome["text"] or f"[recall] {outcome['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""copilot — 现场证据桥接：把主代理（ZCode）的现场产物整理成 ChatGPT 可读、可核验的证据包。

定位：主执行方（ZCode 主代理）在真实环境里定位产物、提取必要信息、形成结构化证据；
本脚本只做「取证 + 整理 + 脱敏 + 预算裁剪 + 落盘」，不做判断、不下结论、不写 PASS。

核心纪律（与 SKILL.md 一致）：
  1. 证据必须来自真实现场（文件内容、命令原始输出），不转发执行方的自评摘要。
  2. 凭据类文件默认拒绝整读，只产出元数据（路径 / 大小 / sha256），需 --allow-sensitive 才读且强制脱敏。
  3. 一切内容出站前脱敏；超出预算的内容按头尾保留并显式标注省略量，不静默丢证。
  4. 未纳入证据包的来源写入清单并标注原因，禁止把「没给」当成「已通过」。

图片证据（--image，或 --file 命中受支持图片魔数时自动识别）：
  证据包里只落**元数据 + 机器可读引用**（类型 / 大小 / 尺寸 / sha256 / 来源路径 / 版本说明），
  不含 base64、不含十六进制摘要；真实像素由 call_gpt.py 读取同一份文件后作为 `input_image` 内联。
  格式按魔数判定（不信任扩展名），支持 png/jpeg/webp/gif（CPA 实测）；其他格式明确失败。

退出码：0 成功 / 2 用法或采集错误
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import re
import signal
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_common import (  # noqa: E402
    SUPPORTED_IMAGE_MIMES,
    human_bytes,
    image_dimensions,
    is_sensitive_path,
    now_iso,
    redact,
    sha256_file,
    sha256_text,
    sniff_image_mime,
)

EXIT_OK, EXIT_USAGE = 0, 2
BINARY_SNIFF = 8192
IMAGE_HEAD = 64
IMAGE_MARKER = "copilot-image"  # call_gpt.py 依此注释行识别图片证据引用
RANGE_RE = re.compile(r"^(\d+)(?:/(\d+)?)?$")
# 证据包格式版本：v2 起包裹围栏长度按正文自适应（见 _fence_for），正文不会再被
# 自己的围栏提前闭合。旧采集器固定用三反引号，包本身存在结构歧义（正文里的
# `### E<n> —` 与图片注释都可能泄漏到围栏外），无法靠结构推断可靠区分
# 「合法的第二个条目」与「泄漏出来的伪造条目」，因此按版本显式拒绝旧格式。
PACK_FORMAT_FIELD = "证据包格式"
PACK_FORMAT_VERSION = 2


class Item:
    def __init__(self, kind: str, source: str, method: str) -> None:
        self.kind = kind
        self.source = source
        self.method = method
        self.text = ""
        self.lines: int | None = None
        self.bytes: int | None = None
        self.digest: str | None = None
        self.included = True
        self.truncated = False
        self.omitted_chars = 0
        self.reason = ""
        self.sensitive = False
        self.image: dict | None = None
        # 出站脱敏改写了哪些行（1 起）。被改写却不声明，复核会把包里的代码
        # 当成磁盘上的代码。
        self.redacted_lines: list[int] = []

    def as_manifest(self, idx: int) -> dict:
        return {
            "id": f"E{idx}",
            "kind": self.kind,
            "source": self.source,
            "method": self.method,
            "lines": self.lines,
            "bytes": self.bytes,
            "sha256": self.digest,
            "included": self.included,
            "truncated": self.truncated,
            "omitted_chars": self.omitted_chars,
            "sensitive": self.sensitive,
            "reason": self.reason,
            "image": self.image,
            "redacted_lines": self.redacted_lines,
            "redacted_lines_coordinate": "pre-clip" if self.truncated else "body",
        }


def die(message: str) -> None:
    """用法 / 采集错误：明确失败（退出码 2），不以空证据包静默通过。"""
    print(f"copilot: {message}", file=sys.stderr)
    sys.exit(EXIT_USAGE)


def _fence_for(text: str) -> str:
    """选一段比正文里任何围栏都长的围栏，正文无法提前闭合它。

    正文常含 Markdown 示例（三反引号、缩进代码块、`~` 围栏）。固定用三个反引号包裹时，
    正文里的一行围栏就会提前结束包裹，后面的内容按「围栏外」解析——其中的 `### E<n> —`
    会被当成新证据条目，图片注释还会触发本地读图。

    识别口径必须与解析端完全一致（`review_ledger.iter_fence_flags`：先 strip 再匹配，
    反引号与 `~` 都算，长度不限），否则生成端以为「正文没有围栏」而选了 3 个反引号，
    解析端却按缩进围栏错位——那正是这条修复最初引入的逃逸路径。
    """
    longest = 0
    for line in text.splitlines():
        m = re.match(r"^(`{3,}|~{3,})", line.strip())
        if m:
            longest = max(longest, len(m.group(1)))
    return "`" * max(3, longest + 1)


def _one_line(value: object) -> str:
    """把元数据字段压成单行：来源 / 采集方式 / 备注不允许多行。

    这些值常来自 CLI（`--cmd` 的标签、`--dir-glob`、路径），多行输入会让
    第二行变成 `### E<n> — ` 标题，从而在包里注入幽灵条目、甚至把真实正文
    归到伪造来源名下。标识符本来就该是单行，换行压成空格最直接。
    """
    return str(value).replace("\r", " ").replace("\n", " ")


def _escape_structure(text: str) -> str:
    """转义自由文本里的结构标记行（标题、围栏）。

    `--goal`、`--note` 与图片 note 是调用方提供的普通文本，渲染后位于包裹围栏**之外**。
    若其中出现 `### E<n> — ` 行，会被解析端当成新证据条目（幽灵条目）；
    出现未闭合围栏还会把后续内容吞掉。加一个反斜杠转义既能消除结构歧义，
    也不改变这些文本的可见内容。
    """
    out: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^(\s*)(#{1,6}\s|`{3,}|~{3,})", line)
        if m:
            indent = m.group(1)
            line = indent + "\\" + line[len(indent):]
        out.append(line)
    return "\n".join(out)


def _ranges(nums: list[int]) -> str:
    """把行号压成区间文本（`3-5, 9`），避免披露行自己撑长证据包。"""
    out: list[str] = []
    start = prev = None
    for n in sorted(set(nums)):
        if start is None:
            start = prev = n
            continue
        if n == prev + 1:
            prev = n
            continue
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = n
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ", ".join(out)


def _redact_with_lines(text: str) -> tuple[str, list[int]]:
    """脱敏并记录被改写的行号（1 起）。

    脱敏是出站硬要求，但它是**有损**的：源码注释里举的凭据例子、测试夹具里
    拼出来的合成串，都会被当作凭据改写。包内内容因此可能与磁盘不一致。
    不声明这一点，复核就会把包里的代码当成原始代码，据此得出的结论没有依据。
    这里只记录行号，原文件指纹仍由条目 sha256 / `--attest` 独立给出。
    """
    cleaned = redact(text)
    if cleaned == text:
        return cleaned, []
    before, after = text.split("\n"), cleaned.split("\n")
    if len(before) != len(after):
        # 行数变了就无法逐行对齐，保守地把整段标为已改写。
        return cleaned, list(range(1, len(after) + 1))
    return cleaned, [i for i, (a, b) in enumerate(zip(before, after), 1) if a != b]


def _marker(total: int, omitted: int, head: int, tail: int) -> str:
    return (
        f"\n\n... [copilot: 省略 {omitted} 字符（原文 {total} 字符，"
        f"此处仅保留头 {head} + 尾 {tail}）——如需该区间请用 --file 'PATH::A/B' 精确补取] ...\n\n"
    )


def clip(text: str, max_chars: int, marker: bool = True) -> tuple[str, bool, int]:
    """头尾保留式裁剪：结果总长（含省略标记）不超过 max_chars。

    标记本身也占字符，因此先按预留标记空间反算头/尾配额，避免"裁剪后反而超预算"。
    `omitted` 是**实际被丢弃的字符数**（= 原长 − 保留的头尾），不是"超出预算的量"——
    两者不同，标记中的数字必须与"保留头 X + 尾 Y"自洽，否则读者一算就出现矛盾。
    `marker=False` 用于已经是裁剪产物的文本（避免嵌套出两个省略标记）。
    """
    if len(text) <= max_chars:
        return text, False, 0
    if max_chars <= 0:
        return "", True, len(text)
    if not marker:
        return text[:max_chars], True, len(text) - max_chars

    budget = max_chars
    for _ in range(6):
        head = max(1, int(budget * 0.6))
        tail = max(1, budget - head)
        mk = _marker(len(text), len(text) - head - tail, head, tail)
        new_budget = max_chars - len(mk)
        if new_budget == budget or new_budget < 2:
            break
        budget = new_budget
    if budget < 2:
        return text[:max_chars], True, len(text) - max_chars

    head = max(1, int(budget * 0.6))
    tail = max(1, budget - head)
    out = text[:head] + _marker(len(text), len(text) - head - tail, head, tail) + text[-tail:]
    while len(out) > max_chars and tail > 1:
        tail -= 1
        out = text[:head] + _marker(len(text), len(text) - head - tail, head, tail) + text[-tail:]
    if len(out) > max_chars:
        # 预算小到连省略标记都放不下：硬截断也必须守住 max_chars。
        return text[:max_chars], True, len(text) - max_chars
    return out, True, len(text) - head - tail


def snip(text: str, start: int, end: int, path: str, digest: str) -> Item:
    """按 1-based 闭区间行号取片段。

    digest 必须是源文件的原始字节指纹（与全文模式同一口径）；不要对替换解码后的
    文本重新计算，否则同一来源因取法不同会得到两个 sha256。
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    lines = raw.decode("utf-8", "replace").splitlines()
    lo = max(1, start)
    hi = min(len(lines), end) if end else len(lines)
    chunk = "\n".join(lines[lo - 1:hi])
    it = Item("file-lines", path, f"行 {lo}-{hi}（全文 {len(lines)} 行）")
    it.text = chunk
    it.lines = len(lines)
    it.bytes = len(raw)
    it.digest = digest
    return it


def grep_file(text: str, pattern: str, context: int, path: str, total_lines: int,
              size: int, digest: str) -> Item:
    import re
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise SystemExit(f"copilot: 正则非法 {pattern!r}: {exc}")
    lines = text.splitlines()
    keep: set[int] = set()
    hits = 0
    for i, line in enumerate(lines):
        if rx.search(line):
            hits += 1
            for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                keep.add(j)
    picked = sorted(keep)
    body = "\n".join(f"{i + 1}: {lines[i]}" for i in picked)
    it = Item("file-grep", path,
              f"正则 {pattern!r}，命中 {hits} 行，上下文 ±{context} 行，取 {len(picked)} 行（全文 {total_lines} 行）")
    it.text = body
    it.lines = total_lines
    it.bytes = size
    it.digest = digest
    return it


def collect_image(args, spec: str, default_note: str = "") -> Item:
    """图片证据：按魔数校验格式，只落元数据 + 机器可读引用（不含像素数据）。

    像素不在证据包里（也不在 stdout / manifest / 审计里）——由 call_gpt.py 在调用时
    读取同一份文件，校验 sha256 后作为 `input_image` 内联。这样证据包可安全落盘与审计，
    而 GPT 拿到的是真实像素，不是路径、描述或 base64 文本。
    """
    path = spec
    note = default_note
    if "::" in spec:
        path, _, note = spec.partition("::")
        note = note.strip() or default_note
    if not os.path.isfile(path):
        die(f"图片证据不存在或不是文件：{path}")
    size = os.path.getsize(path)
    digest = sha256_file(path)
    if is_sensitive_path(path):
        it = Item("image-refused", path, "凭据类文件名：默认拒绝纳入图片证据")
        it.bytes = size
        it.digest = digest
        it.included = False
        it.sensitive = True
        it.reason = ("文件名命中凭据类模式；未读取像素。确需时把该图另存为普通文件名再采集，"
                     "不要用 --allow-sensitive 放行图片")
        return it
    if size > args.image_max_bytes:
        die(
            f"图片超出单张上限：{path} 为 {human_bytes(size)}，"
            f"上限 {human_bytes(args.image_max_bytes)}（--image-max-bytes 可调）。"
            "请先自行生成尺寸合适的副本，不要以文字描述替代像素。"
        )
    with open(path, "rb") as fh:
        head = fh.read(IMAGE_HEAD)
    mime = sniff_image_mime(head)
    if mime is None:
        die(
            f"{path} 的魔数不是受支持的图片格式（支持 {', '.join(SUPPORTED_IMAGE_MIMES)}）。"
            "格式按文件头判定，不看扩展名；转换后再作为图片证据采集。"
        )
    width, height, err = image_dimensions(path)
    if err and not width:
        die(f"图片无法纳入证据：{path} — {err}")

    it = Item("image", path,
              "真实图片：像素由 call_gpt.py 作为 input_image 内联（非路径、非文字描述、非 base64 文本）")
    it.bytes = size
    it.digest = digest
    it.image = {
        "source": os.path.abspath(path),
        "mime": mime,
        "bytes": size,
        "sha256": digest,
        "width": width,
        "height": height,
        "note": note,
    }
    if err:
        it.reason = err
    return it


def collect_file(args, spec: str) -> Item:
    path = spec
    mode = "file"
    rng = None
    pat = None
    if "::" in spec:
        path, _, tail = spec.partition("::")
        m = RANGE_RE.match(tail)
        if m:
            rng = (int(m.group(1)), int(m.group(2) or 0))
            mode = "lines"
        elif len(tail) >= 2 and tail.startswith("/") and tail.endswith("/"):
            pat = tail[1:-1]
            mode = "grep"
        else:
            raise SystemExit(
                f"copilot: --file 后缀只能是 ::A/B（行区间）或 ::/正则/，收到 {tail!r}"
            )
    if not os.path.isfile(path):
        raise SystemExit(f"copilot: 证据源不存在或不是文件：{path}")
    size = os.path.getsize(path)
    digest = sha256_file(path)
    if is_sensitive_path(path) and not args.allow_sensitive:
        it = Item("sensitive-refused", path, "凭据类文件：默认拒绝整读")
        it.bytes = size
        it.digest = digest
        it.included = False
        it.sensitive = True
        it.reason = "文件名命中凭据类模式；未读取内容。确需时用 --allow-sensitive（读取后强制脱敏）"
        it.text = ""
        return it

    with open(path, "rb") as fh:
        head = fh.read(BINARY_SNIFF)
    if sniff_image_mime(head):
        # 真图片：按图片证据处理（元数据 + 调用时内联像素），不再退化成十六进制摘要。
        # 行区间/正则后缀对图片无意义，此处只用 path。
        return collect_image(args, path)
    if b"\x00" in head:
        it = Item("binary", path, f"二进制文件，仅取前 {BINARY_SNIFF} 字节十六进制摘要")
        it.bytes = size
        it.digest = digest
        it.text = head[:512].hex(" ", 1)
        it.included = True
        it.reason = "二进制内容不作为可判定证据，只证明文件存在与指纹"
        return it

    text = open(path, "r", encoding="utf-8", errors="replace").read()
    total_lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    if mode == "lines" and rng:
        return snip(text, rng[0], rng[1], path, digest)
    if mode == "grep" and pat:
        return grep_file(text, pat, args.grep_context, path, total_lines, size, digest)
    it = Item("file", path, f"全文读取（{total_lines} 行）")
    it.text = text
    it.lines = total_lines
    it.bytes = size
    it.digest = digest
    if is_sensitive_path(path):
        it.sensitive = True
        it.reason = "--allow-sensitive 已放行；内容已强制脱敏"
    return it


def collect_cmd(args, spec: str) -> Item:
    label = spec
    if "::" in spec:
        label, _, cmd = spec.partition("::")
    else:
        cmd = spec
        label = cmd if len(cmd) <= 60 else cmd[:57] + "..."
    try:
        # 独立进程组：超时必须终止 shell 派生的全部后代，不能只杀直接子进程，
        # 否则命令返回「超时」后后台任务仍在跑（并可能继续写入被测现场）。
        proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=args.cwd or None, start_new_session=True,
        )
    except OSError as exc:
        it = Item("cmd", label, "命令无法启动")
        it.included = False
        it.reason = f"命令启动失败：{exc}"
        it.text = ""
        return it
    timed_out = False
    try:
        out, err = proc.communicate(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                proc.kill()
            except OSError:
                pass
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", ""
    out = out or ""
    err = err or ""
    text = out + (("\n[stderr]\n" + err) if err else "")
    # 实际执行的命令写进正文：条目标题只是标签（长命令会被截断），复核光看标题
    # 无法还原参数、重定向与退出码传播方式，也就无法判断这段输出到底是谁产生的。
    # 命令压成单行：多行命令的第二行可能形如 `### E9 — …`，在包里伪造条目边界。
    body = f"$ {_one_line(cmd)}\n{text}" if text else f"$ {_one_line(cmd)}\n（无输出）"
    if timed_out:
        it = Item("cmd", label, f"真实执行（超时 {args.timeout}s，已终止进程组）")
        # 超时输出是不完整的：可以被诊断引用，但不能作为「任务通过」的证据。
        # 与旧行为一致，放到「未纳入证据」；同时如实写明是否已产生输出。
        it.included = False
        it.text = body
        it.lines = body.count("\n")
        it.bytes = len(body.encode("utf-8"))
        it.digest = sha256_text(body)
        it.reason = ("命令超时，已终止本轮派生的进程组；"
                     + (f"已产生 {len(text)} 字符输出，因不完整未纳入证据，不得作为通过依据"
                        if text else "未产生任何输出"))
        return it
    it = Item("cmd", label, f"真实执行，退出码 {proc.returncode}；cwd={args.cwd or os.getcwd()}")
    # stdout 与 stderr 都要入包：测试框架、告警、`copilot:` 诊断普遍写 stderr，
    # 只收 stdout 会得到「退出码 0、0B」的空条目，看起来像「命令没有输出」。
    it.text = body
    it.lines = body.count("\n")
    it.bytes = len(body.encode("utf-8"))
    it.digest = sha256_text(body)
    it.reason = "" if proc.returncode == 0 else f"非零退出码 {proc.returncode}（原始输出如上）"
    return it


def _git_out(repo: str, args: list[str], timeout: int = 60) -> str:
    try:
        proc = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError:
        raise SystemExit("copilot: --attest 需要 git，但当前环境找不到 git 可执行文件")
    except subprocess.TimeoutExpired:
        raise SystemExit(f"copilot: --attest 读取 {repo} 超时（git {' '.join(args)}）")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise SystemExit(f"copilot: --attest 读取 {repo} 失败"
                         f"（git {' '.join(args)}）：{detail}")
    return proc.stdout


def _is_git_worktree(repo: str) -> bool:
    try:
        proc = subprocess.run(["git", "-C", repo, "rev-parse", "--is-inside-work-tree"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def collect_attest(args, spec: str) -> Item:
    """机器生成代码绑定证据：HEAD、工作区状态、diff --stat 与逐文件 sha256。

    复核反复卡在「被审的是哪份代码」：执行方用文字自述 commit 与改动，审方无法核对。
    这里直接调用 git 采集绑定事实。非 git 工作区或 git 不可用时失败关闭——没有绑定
    证据的复核会退化成对自述的信任，而自述不是证据。
    """
    repo, _, label = spec.partition("::")
    repo = os.path.abspath(repo)
    if not os.path.isdir(repo):
        raise SystemExit(f"copilot: --attest 目标不是目录：{repo}")
    if not _is_git_worktree(repo):
        raise SystemExit(f"copilot: --attest 要求 git 工作区，但 {repo} 不是；"
                         "代码绑定证据不能用文件清单或文字自述替代")
    toplevel = _git_out(repo, ["rev-parse", "--show-toplevel"]).strip()
    head = _git_out(repo, ["rev-parse", "HEAD"]).strip()
    branch = _git_out(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    commit = _git_out(repo, ["log", "-1", "--format=%h %ad %an", "--date=iso"]).strip()
    subject = _git_out(repo, ["log", "-1", "--format=%s"]).strip()
    porcelain = _git_out(toplevel, ["-c", "status.relativePaths=false",
                                    "status", "--porcelain=v1", "-z"])
    tokens = [t for t in porcelain.split("\0") if t]
    own = label.strip() or toplevel

    changed: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if len(tok) >= 4 and tok[2] == " ":
            code, path = tok[:2], tok[3:]
            # rename/copy 的目标路径是紧随其后的独立条目（-z 不做引号转义）。
            if code[0] in "RC" and i < len(tokens):
                path = tokens[i]
                i += 1
        else:
            code, path = "??", tok
        changed.append((code, path))

    state_text = ("clean（无未提交改动）" if not changed
                  else f"{len(changed)} 项未提交改动")
    lines = [
        f"- 工作区：`{toplevel}`（git 工作区，由 git 直接读取）",
        f"- HEAD：`{head}`",
        f"- 分支：`{branch or '（游离 HEAD）'}`",
        f"- HEAD 提交：{commit}",
        f"- HEAD 提交说明：{subject}",
        f"- 工作区状态：{state_text}",
        "- status --porcelain=v1 -z（NUL 分隔已展开为换行；rename 记录目标路径）：",
    ]
    lines += [f"  {code} {path}" for code, path in changed] or ["  （无）"]
    lines.append("- 逐文件 sha256（工作区当前内容）：")
    seen: set[str] = set()
    for code, path in changed:
        if path in seen:
            continue
        seen.add(path)
        full = os.path.join(toplevel, path)
        if os.path.isfile(full):
            lines.append(f"  - {code} `{path}`：`{sha256_file(full)}`")
        else:
            lines.append(f"  - {code} `{path}`：（不是普通文件：目录或已删除）")
    if not seen:
        lines.append("  （无，工作区与 HEAD 一致）")
    stat = _git_out(toplevel, ["diff", "--stat", "HEAD"]).strip()
    lines += ["- diff --stat HEAD（已跟踪文件的未提交改动）：", "  " + (stat or "（无）")]

    body = "\n".join(lines) + "\n"
    it = Item("git-attest", own,
              "机器生成：git rev-parse / status --porcelain=v1 -z / diff --stat / "
              "逐文件 sha256（非执行方转述）")
    it.text = body
    it.lines = body.count("\n")
    it.bytes = len(body.encode("utf-8"))
    it.digest = sha256_text(body)
    return it


def collect_dir(args, spec: str) -> Item:
    root = spec
    if not os.path.isdir(root):
        raise SystemExit(f"copilot: 证据源不是目录：{root}")
    pattern = args.dir_glob or "**/*"
    names = sorted(
        os.path.relpath(p, root)
        for p in globmod.glob(os.path.join(root, pattern), recursive=True)
        if os.path.isfile(p)
    )
    shown = names[: args.dir_limit]
    body = "\n".join(shown)
    if len(names) > len(shown):
        body += f"\n... [仅列前 {len(shown)} / 共 {len(names)} 个文件] ..."
    it = Item("dir", root, f"目录清单 glob={pattern}，共 {len(names)} 个文件")
    it.text = body
    it.lines = len(shown)
    it.digest = sha256_text("\n".join(names))
    return it


def collect_json(args, spec: str) -> Item:
    if "::" not in spec:
        raise SystemExit(f"copilot: --json 需要写成 PATH::dot.path，收到 {spec!r}")
    path, _, dotpath = spec.partition("::")
    if not os.path.isfile(path):
        raise SystemExit(f"copilot: 证据源不存在：{path}")
    digest = sha256_file(path)
    size = os.path.getsize(path)
    # 与 --file 入口同一套敏感源策略：JSON 采集是另一个读盘入口，
    # 不能因为换了入口就绕过凭据类文件的默认拒读。
    if is_sensitive_path(path) and not args.allow_sensitive:
        it = Item("sensitive-refused", path, "凭据类文件：默认拒绝整读")
        it.bytes = size
        it.digest = digest
        it.included = False
        it.sensitive = True
        it.reason = "文件名命中凭据类模式；未读取内容。确需时用 --allow-sensitive（读取后强制脱敏）"
        it.text = ""
        return it
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        data = json.load(fh)
    node = data
    parts = [p for p in dotpath.split(".") if p]
    for part in parts:
        if isinstance(node, list):
            node = node[int(part)]
        elif isinstance(node, dict):
            node = node[part]
        else:
            raise SystemExit(f"copilot: 路径 {dotpath!r} 在 {part!r} 处不可下钻")
    # 保留最后一级键名：只序列化裸值会丢掉「apiKey/password」这类敏感键上下文，
    # 后续 redact() 就无法识别它是凭据。多包一层同名键既保留结构，也不改变取值。
    wrapper = parts[-1] if parts else "<root>"
    body = json.dumps({wrapper: node}, ensure_ascii=False, indent=2)
    it = Item("json", path, f"JSON 子路径 {dotpath or '<root>'}（源文件整体 sha256 已记录）")
    it.text = body
    it.lines = body.count("\n") + 1
    it.digest = digest
    it.bytes = size
    if is_sensitive_path(path):
        it.sensitive = True
        it.reason = "--allow-sensitive 已放行；内容已强制脱敏"
    return it


def collect_stdin(args, spec: str) -> Item:
    data = sys.stdin.read()
    it = Item("stdin", spec or "stdin", "调用方通过 stdin 内联提供的材料")
    it.text = data
    it.lines = data.count("\n")
    it.digest = sha256_text(data)
    return it


def render(items: list[Item], goal: str, notes: list[str], cwd: str) -> tuple[str, list[dict]]:
    manifest: list[dict] = []
    out: list[str] = [
        "# copilot 证据包（由执行方主代理从真实现场采集）",
        "",
        f"- {PACK_FORMAT_FIELD}：{PACK_FORMAT_VERSION}",
        f"- 生成时间：{now_iso()}",
        f"- 采集工作区：{_one_line(redact(cwd))}",
        "- 采集方：ZCode 主代理（主执行方）。以下内容为原始产物或原始命令输出，非执行方结论摘要。",
        "",
        "> 使用纪律：本包内不含任何 PASS/完成结论。判断只能建立在下方证据条目之上；",
        "> 未纳入证据包的来源列在「未纳入证据」，不得据此推断为通过。",
        "",
    ]
    if goal.strip():
        out += ["## 本轮目标 / 待决问题（调用方提供）", "",
                _escape_structure(redact(goal.strip())), ""]
    out += ["## 证据条目", ""]
    indexed = list(enumerate(items, 1))
    for idx, it in indexed:
        # 元数据同样可能夹带凭据（命令标签、超时原因、目标文本），出站前统一脱敏。
        # 图片的机器引用在下方注释里，必须保留原路径，否则调用端读不到像素。
        item = it.as_manifest(idx)
        for key in ("source", "method", "reason"):
            item[key] = redact(str(item.get(key) or ""))
        if isinstance(item.get("image"), dict) and item["image"].get("note"):
            item["image"] = {**item["image"],
                             "note": redact(str(item["image"]["note"]))}
        manifest.append(item)
        if not it.included:
            continue
        if it.kind == "image" and it.image:
            img = it.image
            out += [
                f"### E{idx} — image: {_one_line(redact(it.source))}",
                "",
                f"- 采集方式：{_one_line(redact(it.method))}",
                f"- MIME：`{img['mime']}`（按文件头魔数判定，非扩展名）",
                f"- 大小：{human_bytes(img['bytes'])}",
                f"- 尺寸：{img['width']}×{img['height']}",
                f"- sha256：`{img['sha256']}`",
            ]
            if img.get("note"):
                out.append(f"- 版本/上下文：{_escape_structure(redact(str(img['note'])))}")
            if it.reason:
                out.append(f"- 备注：{_one_line(redact(it.reason))}")
            out += [
                "- 像素传递：本条目**没有**可读文本内容。真实像素由调用方读取上述路径的文件、",
                "  校验 sha256 一致后作为 `input_image` 内联；若读不到同一份像素，调用方必须显式失败，",
                "  不得改用路径、文字描述或 base64 文本代替。",
                "",
                f"<!-- {IMAGE_MARKER} "
                + json.dumps(
                    {**it.image, "id": f"E{idx}",
                     **({"note": redact(str(it.image["note"]))}
                        if it.image.get("note") else {})},
                    ensure_ascii=False, sort_keys=True)
                + " -->",
                "",
            ]
            continue
        out += [
            f"### E{idx} — {it.kind}: {_one_line(redact(it.source))}",
            "",
            f"- 采集方式：{_one_line(redact(it.method))}",
        ]
        if it.bytes is not None:
            out.append(f"- 大小：{human_bytes(it.bytes)}")
        if it.lines is not None:
            out.append(f"- 行数：{it.lines}")
        if it.digest:
            out.append(f"- sha256：`{it.digest}`")
        if it.truncated:
            out.append(f"- 截断：省略 {it.omitted_chars} 字符（见正文标注）")
        if it.redacted_lines:
            # 行号在脱敏时按采集正文计算；裁剪会插入省略标记并丢掉中段，因此条目被截断时
            # 必须写明坐标是「裁剪前」，不能继续标成当前正文的行号（会指错行）。
            scope = "裁剪前采集正文行号" if it.truncated else "采集正文行号"
            out.append(f"- 脱敏改写：第 {_ranges(it.redacted_lines)} 行（{scope}）"
                       "与原始内容不一致，凭据形态片段与凭据键下的容器叶子被遮蔽"
                       "（括号、键名、布尔/空值、注释与行数保留；代码容器内的裸标识符"
                       "与括号内调用实参不遮蔽）；原文件指纹见 sha256 / --attest")
        if it.reason:
            out.append(f"- 备注：{_one_line(redact(it.reason))}")
        fence = _fence_for(it.text)
        out += ["", f"{fence}text", it.text.rstrip("\n"), fence, ""]
    omitted = [(idx, it) for idx, it in indexed if not it.included]
    if omitted:
        out += ["## 未纳入证据（禁止据此判为通过）", ""]
        for idx, it in omitted:
            out.append(f"- E{idx} `{_one_line(redact(it.source))}`："
                       f"{_one_line(redact(it.reason or '未纳入'))}")
        out.append("")
    if notes:
        out += ["## 执行方说明（非证据，不得作为判定依据）", ""]
        out += [f"- {_escape_structure(redact(str(n)))}" for n in notes]
        out.append("")
    return "\n".join(out) + "\n", manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="把现场产物整理成 ChatGPT 可读的证据包")
    ap.add_argument("--file", action="append", default=[], metavar="PATH[::A/B|/REGEX/]",
                    help="文件；可加 ::A/B 取行区间，或 ::/正则/ 只取命中行及上下文；"
                         "命中 png/jpeg/webp/gif 魔数时自动按图片证据处理")
    ap.add_argument("--image", action="append", default=[], metavar="PATH[::版本或上下文说明]",
                    help="图片证据（真实像素，由 call_gpt.py 作为 input_image 内联）；"
                         "格式按魔数校验，支持 png/jpeg/webp/gif")
    ap.add_argument("--cmd", action="append", default=[], metavar="[LABEL::]SHELL",
                    help="真实执行命令并采集原始输出（含退出码）")
    ap.add_argument("--dir", action="append", default=[], metavar="PATH", help="目录文件清单")
    ap.add_argument("--json", action="append", default=[], metavar="PATH::dot.path",
                    help="从 JSON 提取子结构")
    ap.add_argument("--attest", action="append", default=[], metavar="REPO[::LABEL]",
                    help="机器生成代码绑定证据：HEAD、工作区状态、未提交 diff --stat 与"
                         "逐文件 sha256（复核需要确认被审代码时使用；非 git 工作区直接失败）")
    ap.add_argument("--stdin", action="store_true", help="把 stdin 作为一份内联证据")
    ap.add_argument("--goal-file", default="", help="本轮目标/待决问题文本，附在证据包顶部")
    ap.add_argument("--goal", default="", help="同上，直接给文本")
    ap.add_argument("--note", action="append", default=[], help="执行方说明（显式标注为非证据）")
    ap.add_argument("--grep-context", type=int, default=3)
    ap.add_argument("--dir-glob", default="")
    ap.add_argument("--dir-limit", type=int, default=200)
    ap.add_argument("--timeout", type=float, default=120.0, help="单条命令超时秒数")
    ap.add_argument("--cwd", default="", help="命令采集的工作目录（默认当前目录）")
    ap.add_argument("--per-source-chars", type=int, default=20000)
    ap.add_argument("--max-chars", type=int, default=60000, help="证据包总字符预算")
    ap.add_argument("--image-max-bytes", type=int, default=8 << 20,
                    help="单张图片证据大小上限（默认 8MB），超出即明确失败")
    ap.add_argument("--allow-sensitive", action="store_true",
                    help="放行凭据类文件读取（读取后强制脱敏；默认拒绝整读）")
    ap.add_argument("--out", default="", help="证据包 Markdown 输出路径（默认 stdout）")
    ap.add_argument("--manifest", default="", help="证据清单 JSON 输出路径")
    args = ap.parse_args()

    cwd = os.path.abspath(args.cwd or os.getcwd())
    goal = args.goal
    if args.goal_file:
        with open(args.goal_file, "r", encoding="utf-8", errors="replace") as fh:
            goal = fh.read()

    items: list[Item] = []
    for spec in args.file:
        items.append(collect_file(args, spec))
    for spec in args.image:
        items.append(collect_image(args, spec))
    for spec in args.cmd:
        items.append(collect_cmd(args, spec))
    for spec in args.dir:
        items.append(collect_dir(args, spec))
    for spec in args.json:
        items.append(collect_json(args, spec))
    for spec in args.attest:
        items.append(collect_attest(args, spec))
    if args.stdin:
        items.append(collect_stdin(args, "stdin"))

    if not items:
        print("copilot: 未指定任何证据源"
              "（--file/--image/--cmd/--dir/--json/--attest/--stdin）。", file=sys.stderr)
        return EXIT_USAGE

    # 脱敏（记录被改写的行）→ 单源裁剪
    for it in items:
        if it.text:
            it.text, it.redacted_lines = _redact_with_lines(it.text)
            it.text, it.truncated, it.omitted_chars = clip(it.text, args.per_source_chars)

    # 总预算：按顺序纳入；整条放不下的来源整体移出并记录（不静默丢弃、不半截截取）
    total = 0
    for it in items:
        if not it.included:
            continue
        if total + len(it.text) > args.max_chars:
            it.included = False
            it.reason = (it.reason + "；" if it.reason else "") + \
                "超出证据包总预算未纳入，需用更精确的 --file::A/B / --file::/REGEX/ / --json 补取"
        else:
            total += len(it.text)

    body, manifest = render(items, goal, args.note, cwd)

    # 截断必须显式可见：包内条目正文不完整时，复核会把「没看到」当成「没有」。
    # 这里在采集端就给出计数与告警，避免一路静默到复核裁决。
    truncated = [it for it in items if it.included and it.truncated]
    omitted_total = sum(it.omitted_chars for it in truncated)
    if truncated:
        truncated_ids = {id(it) for it in truncated}
        names = "、".join(f"E{idx}（{it.source}）"
                         for idx, it in enumerate(items, 1) if id(it) in truncated_ids)
        print(f"copilot: 警告：{len(truncated)} 条证据被截断，共省略 {omitted_total} 字符："
              f"{names}。这些条目只能作为线索；REVIEW 前需重新采集或显式放行。",
              file=sys.stderr)

    manifest_doc = {
        "generated_at": now_iso(),
        "cwd": cwd,
        "evidence_chars": total,
        "items_total": len(items),
        "items_included": sum(1 for i in items if i.included),
        "items_truncated": len(truncated),
        "omitted_chars": omitted_total,
        "images_included": sum(1 for i in items if i.included and i.kind == "image"),
        "items": manifest,
    }

    if args.manifest:
        with open(args.manifest, "w", encoding="utf-8") as fh:
            json.dump(manifest_doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(body)
        suffix = f"，{len(truncated)} 条被截断" if truncated else ""
        print(f"copilot: 证据包已写入 {args.out}"
              f"（纳入 {manifest_doc['items_included']}/{len(items)} 条，{total} 字符{suffix}）",
              file=sys.stderr)
    else:
        print(body)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

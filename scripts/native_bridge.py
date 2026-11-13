#!/usr/bin/env python3
"""Agent Bridge v2：ZCode 本地计划 → 原生执行/复核 → 治理状态。

本模块只做**协议结构与绑定校验**，不调度子代理、不调用模型、不改任务状态：

* DISPATCH：把 `governance.py` 中已登记的计划工作项交给宿主，绑定
  task / contract / plan（不再绑定任何远程模型轮次）；
* RESULT：执行方回传实际结果、原始证据文件及其 SHA-256、运行元数据；
* REVIEW：独立 Reviewer 回传复核结论，绑定它检查过的执行结果 SHA；G4 资格
  由宿主运行记录（Profile、实际工具调用、独立子会话）计算，不由 Reviewer
  自述。

安全边界
--------
1. 所有信封都绑定 task_id / contract_sha256 / plan_id / plan_sha256；执行与
   复核还绑定 dispatch_id / work_item_id。
2. 执行结果必须由当前 dispatch 的工作项产生；证据文件必须存在且 SHA-256 一致。
3. 复核必须晚于对应执行结果，且 Reviewer 不能是执行方本人（Agent ID /
   Child Session ID 任一相同即拒绝）。
4. Reviewer 不得改动文件；`changed_files` 非空即视为污染并拒绝。
5. PASS 不得带 blocking finding 或未验证事项。
6. 本模块的校验是**本地完整性与绑定校验**，不是密码学签名，也不是宿主可信
   身份证明；`g4_eligible` 在 ingest 时由 `native_agent.py` 按宿主 metadata/rollout
   重新计算，治理层收口时再核对固化下来的宿主记录摘要与身份一致性。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from datetime import datetime

DISPATCH_SCHEMA = "copilot-native-dispatch/v2"
RESULT_SCHEMA = "copilot-native-result/v2"
REVIEW_SCHEMA = "copilot-native-review/v2"
SCHEMAS = (DISPATCH_SCHEMA, RESULT_SCHEMA, REVIEW_SCHEMA)
LEGACY_SCHEMAS = ("copilot-native-dispatch/v1", "copilot-native-result/v1",
                  "copilot-native-review/v1")

EXECUTION_STATUSES = ("done", "failed", "blocked", "dropped")
REVIEW_VERDICTS = ("pass", "partial", "blocked")
# full：完整复核；incremental：在一条既有合格复核之上只复核新增/变更证据。
REVIEW_SCOPES = ("full", "incremental")
FINDING_SEVERITIES = ("blocking", "major", "minor", "info")
NATIVE_ASSIGNEES = ("zcode", "deepseek")
EXECUTOR_KINDS = ("main", "subagent")
REVIEWER_TOOLS = ("Read", "Grep", "Glob", "Bash")

EVIDENCE_ID_RE = re.compile(r"^E[1-9][0-9]{0,3}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Reviewer 可以用 Bash 读取现场，但只允许这些只读前缀。真正的写保护仍由
# Reviewer Profile 的 instructions + 宿主工具权限负责；这里是第二道失败关闭。
# v6.1 起不再要求「命令形式完美」：白名单覆盖复核实际需要的只读工具，写操作由
# 命令族检查与 shell 层重定向/元字符拒绝负责；`sed -i`、`sort -o`、`unzip` 解包
# 等仍失败关闭。复核不因命令形式自我作废。
_READONLY_COMMAND_PREFIXES = (
    "git diff", "git status", "git log", "git show", "git grep",
    "git rev-parse", "git ls-files", "rg ", "grep ", "cat ",
    "head ", "tail ", "wc ", "ls ", "find ", "stat ", "sha256sum ",
    "diff ", "cmp ", "sort ", "cd ", "uniq ", "cut ", "tr ", "nl ",
    "basename ", "dirname ", "realpath ", "readlink ", "md5sum ", "sha1sum ",
    # 只写 stdout / 只读取的常用检查工具（v6.1）
    "echo ", "printf ", "pwd", "date ", "file ", "xxd ", "od ", "strings ",
    "jq ", "tree ", "du ", "test ",
    # 带参数级写检查的命令族（见 _family_readonly_checks）
    "sed ", "awk ", "unzip ", "zipinfo ",
    # Android/Java 只读检查（复核 APK/dex 的常规工具）
    "aapt ", "aapt2 ", "apksigner ", "dexdump ", "javap ",
)
# 这些命令族本身只读，但带写参数/子命令时会改工作区，必须在 argv 层再检查。
# `date -s` 改系统时间；`tree -o` 写输出文件；`unzip` 默认解包；`aapt2 link`
# 生成文件；`apksigner sign` 改写 APK。只读用法继续放行，写用法失败关闭。
_TREE_WRITE_OPTIONS = frozenset({"-o", "--output"})
_UNZIP_LIST_OPTIONS = ("-l", "-v", "-t", "-Z")
_AAPT_READONLY_SUBCOMMANDS = frozenset({"dump", "list", "badging", "version"})
_AAPT2_READONLY_SUBCOMMANDS = frozenset({"dump", "version"})
_APKSIGNER_READONLY_SUBCOMMANDS = frozenset({"verify"})
# xxd 的第二个位置参数是输出文件；带值选项先按值消费，避免把值误当位置参数。
_XXD_VALUE_OPTIONS = frozenset({"-c", "-g", "-l", "-s", "-o", "-n"})
# sed/awk 的长选项一律按白名单匹配：GNU getopt 接受无歧义缩写（`--in-pl`、
# `--fil=`），只比较全名会被绕过。白名单不含写文件、加载脚本或加载扩展的选项。
_SED_READONLY_LONG_OPTIONS = frozenset({
    "quiet", "silent", "regexp-extended", "separate", "unbuffered", "posix",
    "debug", "sandbox", "follow-symlinks", "binary", "null-data", "help",
    "version",
})
_AWK_READONLY_LONG_OPTIONS = frozenset({
    "field-separator", "assign", "sandbox", "posix", "traditional",
    "characters-as-bytes", "csv", "optimize", "re-interval", "bignum",
    "non-decimal-data", "use-lc-numeric", "lint", "help", "version",
})
# awk 程序里的写操作只可能出现在 print/printf 的输出重定向（`>`、`>>`、`|`）
# 或 `"cmd" | getline`（执行外部命令）；比较运算（`NR > 1`、`||`）与字符串
# 字面量里的符号不是写。按语句与字符串边界扫描，避免把只读程序误判成写。
_AWK_PRINT_RE = re.compile(r"(?<![A-Za-z0-9_])printf?(?![A-Za-z0-9_])")
_AWK_PIPE_GETLINE_RE = re.compile(r"(?<!\|)\|&?[ \t]*getline")
_AWK_SYSTEM_RE = re.compile(r"system[ \t]*\(")
# sed 脚本的 `w`/`W`/`e` 命令写文件或执行外部程序；`s///w file`、`s///e` 的
# 标志位同样如此。按脚本语法扫描，避免把 `s/\w/x/` 这类正则误判成写命令。
_SED_WRITE_COMMANDS = frozenset("wWe")
_SED_SUB_WRITE_FLAGS = frozenset("wWe")
# 分号/管道是只读复核里常见的组合形式（`find ... | sort; stat ...`）。这里按
# **引号感知**拆分后逐段校验，只把顶层 `;` / `|` 当作分隔符；`&&`、重定向、
# 命令替换、反引号、换行一律拒绝。花括号也拒绝：bash 的 brace expansion 会把
# `{-,-}delete` 展开成 `-delete`，校验时看不到危险谓词。未引用的 `*`/`?`/`[`
# 同样拒绝：文件名展开会改变实际参数（`uniq a-*` 可能展开成输入+输出文件），
# 校验参数/参数个数就不再等于实际执行参数。拆分不改变只读边界：每一段仍走
# 同一白名单。
_COMMAND_SHELL_META = "&><`$\n\r{}*?["
# Shell 的 token 分隔空白**只有**空格与制表符（换行已被拒绝）。Python 的
# `str.isspace()` 还认 U+00A0、U+000B 等 Unicode 空白，但 bash 会把它们当作普通
# 词内字符：`input<NBSP>2>/dev/null` 的 `2` 属于参数，按 isspace 判定会再次删掉
# 普通 argv 字符。所有 token 边界与分段裁剪统一用这个集合。
_SHELL_SPACE = " \t"
# `git -C <dir>` 只切换目录，不写文件；只剥离大写 `-C` 及其路径值，后续子命令
# 仍由白名单校验（小写 `-c` 可注入 external pager，必须落回拒绝路径）。
_GIT_C_PREFIX_RE = re.compile(
    r"^git[ \t]+-C[ \t]+(?:'[^']*'|\"[^\"]*\"|[^\s'\"]+)[ \t]+")
# 目标是 /dev/null 的重定向不写工作区，是只读检查里常见的噪声抑制；只放行
# 独立成 token 的 `>` / `2>` 且目标恰为 /dev/null，其他重定向仍然失败关闭。
# 必须在 token 边界上匹配：`sort -k2>/dev/null` 里的 `2` 是参数 `-k2` 的一部分，
# 整体替换会把校验用文本改成 `sort -k -o out`，使真实 argv 里的 `-o` 被当成
# `-k` 的参数值跳过（校验参数 ≠ 实际 argv）。
_NULL_REDIRECT_AT_RE = re.compile(r">[ \t]*/dev/null(?=[ \t]|[;|]|$)")
# 引号会骗过纯文本正则：`git diff "--output=/tmp/x"` 在 shell 展开后仍是写文件。
# 每段用 shlex 取 argv，对参数 token 做一次与引号无关的拒绝。危险参数只按 argv
# 判定，不再用纯文本搜整段：否则 `grep -e rm`、`git grep -e '-O'` 这类“参数值”
# 会被误伤。顶层命令名仍由白名单限制。
_ARGV_DENY_RE = re.compile(
    r"^(?:--output(?:=|$)|--output-file(?:=|$)|--ext-diff$|--textconv$|"
    r"--open-files-in-pager(?:=|$)|--pre(?:=|$)|--pre-glob(?:=|$)|"
    r"--hostname-bin(?:=|$)|--search-zip(?:=|$)|"
    r"-delete$|-exec(?:dir)?$|-ok(?:dir)?$|-fprint(?:0|f)?$|-fprintf$|-fls$)")
# GNU getopt 与 git parse-options 允许长选项唯一前缀缩写：`sort --o=/tmp/x`
# 会写文件，`git grep --ope='...'` 会执行外部程序。只匹配完整名不够，按命令族
# 列出危险完整名，token 是其前缀同样拒绝；精确匹配的合法选项先放行。
_ABBREV_FORBIDDEN = {
    "git grep": ("--open-files-in-pager", "--textconv"),
    "sort": ("--output", "--compress-program", "--temporary-directory"),
}
_ABBREV_ALLOW_EXACT = {
    "git grep": ("--only-matching",),
    "sort": ("--check",),
}
# 短选项聚簇：o/T 写文件。t/k/S 自身带参，其后的字符是参数值，不是选项。
_SORT_SHORT_WRITE = frozenset("oT")
_SORT_SHORT_VALUE = frozenset("tkS")
# git grep 短选项带参：e/f/m/A/B/C 之后的字符属于参数值（如 -eO 是匹配 "O"）。
_GIT_GREP_SHORT_VALUE = frozenset("efmABC")
# rg 的 -z 会按扩展名调用外部解压程序；e/f/g/t/T/A/B/C/m/j/M/E/r/d 带参。
_RG_SHORT_VALUE = frozenset("efgtTABCmjMErd")
# 分离形式（`-e PATTERN`、`sort -t SEP`）的选项参数值不是选项，不再当作命令参数
# 扫描，避免 `git grep -e '-O'`、`sort -t -o` 这类只读用法被误拒。
_VALUE_TOKENS = {
    "sort": frozenset({"-t", "-k", "-S"}),
    "git grep": frozenset({"-e", "-f", "-m", "-A", "-B", "-C"}),
    "rg": frozenset({"-e", "-f", "-g", "-t", "-T", "-A", "-B", "-C", "-m",
                     "-j", "-M", "-E", "-r", "-d"}),
}
# 这些路径不得作为 Reviewer 的 inspected 文件：设备/管道/目录不是可复核内容。
_NON_REGULAR_PREFIXES = ("/dev/", "/proc/", "/sys/")


class BridgeError(ValueError):
    """可预期的协议/绑定失败。code 供 CLI、测试和审计稳定引用。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)


def _fail(code: str, message: str) -> None:
    raise BridgeError(code, message)


def _obj(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        _fail("schema_invalid", f"{name} 必须是对象（实际 {type(value).__name__}）。")
    return value


def _list(value: object, name: str) -> list:
    if not isinstance(value, list):
        _fail("schema_invalid", f"{name} 必须是数组（实际 {type(value).__name__}）。")
    return value


def _text(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        _fail("schema_invalid", f"{name} 必须是字符串。")
    text = value.strip()
    if not text and not allow_empty:
        _fail("schema_invalid", f"{name} 不能为空。")
    return text


def _int(value: object, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        _fail("schema_invalid", f"{name} 必须是 >= {minimum} 的整数。")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if not _SHA256_RE.match(text):
        _fail("hash_invalid", f"{name} 必须是 64 位小写 SHA-256。")
    return text


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def envelope_sha256(envelope: dict) -> str:
    """信封内容摘要：排除 result_sha256 自身，防止自指。"""
    body = {k: v for k, v in envelope.items() if k != "result_sha256"}
    return sha256_text(canonical_json(body))


def classify_native_result(envelope: object) -> str:
    """返回 execution / review；未知 schema 失败关闭。"""
    obj = _obj(envelope, "native result")
    schema = str(obj.get("schema") or "").strip()
    if schema == RESULT_SCHEMA:
        return "execution"
    if schema == REVIEW_SCHEMA:
        return "review"
    if schema in LEGACY_SCHEMAS:
        _fail("schema_retired",
              f"{schema} 属于 v1 SOL 通道，已退役；历史记录请用 legacy-status 只读查看。")
    if schema == DISPATCH_SCHEMA:
        _fail("schema_invalid", "dispatch 信封不能作为结果回填。")
    _fail("schema_invalid", f"未知 native result schema：{schema or '-'}。")


def _state_action(state: dict, work_item_id: str) -> dict:
    for action in state.get("actions") or []:
        if isinstance(action, dict) and str(action.get("id") or "") == work_item_id:
            return action
    _fail("work_item_unknown", f"状态中不存在工作项 {work_item_id}。")
    return {}


def _require_state_credential(state: dict) -> tuple[str, str]:
    task_id = str(state.get("task_id") or "").strip()
    contract = str(state.get("contract_sha256") or "").strip()
    if not task_id or not contract:
        _fail("credential_missing",
              "状态缺少 task_id / contract_sha256，不能使用 Agent Bridge。")
    return task_id, contract


def _plan_record(state: dict, plan_id: str) -> dict:
    for plan in state.get("plans") or []:
        if isinstance(plan, dict) and str(plan.get("plan_id") or "") == plan_id:
            return plan
    _fail("plan_unknown", f"状态中没有计划 {plan_id}。")
    return {}


def _plan_payload(state: dict, plan: dict) -> dict:
    """按计划登记顺序重建计划正文，用于重算摘要、发现状态被改写。"""
    by_id = {str(a.get("id") or ""): a for a in state.get("actions") or []
             if isinstance(a, dict)}
    actions = []
    for aid in plan.get("action_ids") or []:
        action = by_id.get(str(aid))
        if not isinstance(action, dict):
            _fail("plan_drift", f"计划 {plan.get('plan_id')} 的工作项 {aid} 已不存在。")
        actions.append({
            "id": str(action.get("id") or ""),
            "title": str(action.get("title") or ""),
            "assignee": str(action.get("assignee") or ""),
            "goal": str(action.get("goal") or ""),
            "constraints": str(action.get("constraints") or ""),
            "expected_evidence": str(action.get("expected_evidence") or ""),
            "depends_on": [str(d) for d in action.get("depends_on") or []],
        })
    return {"summary": str(plan.get("summary") or ""), "actions": actions}


def _plan_sha(state: dict, plan: dict) -> str:
    return sha256_text(canonical_json(_plan_payload(state, plan)))


def _check_plan(state: dict, plan_id: object, plan_sha: object) -> dict:
    pid = _text(plan_id, "plan_id")
    sha = _sha(plan_sha, "plan_sha256")
    plan = _plan_record(state, pid)
    if str(plan.get("sha256") or "") != sha:
        _fail("plan_mismatch", f"计划 {pid} 的摘要与登记不一致。")
    actual = _plan_sha(state, plan)
    if actual != sha:
        _fail("plan_drift",
              f"计划 {pid} 的工作项在登记后被改写；拒绝按漂移后的计划执行。")
    return plan


def _latest_plan(state: dict) -> dict:
    plans = [p for p in state.get("plans") or [] if isinstance(p, dict)]
    if not plans:
        _fail("plan_missing", "状态没有已登记计划；请先 plan 再生成派发。")
    return plans[-1]


def _dispatch_ids(task_id: str, plan_id: str, plan_sha: str, wid: str) -> tuple[str, str]:
    binding = [task_id, plan_id, plan_sha, wid]
    return ("d-" + sha256_text(canonical_json(binding))[:24],
            "ik-" + sha256_text(canonical_json(binding + ["idempotency"]))[:32])


def build_dispatch(state: dict, actions: list[dict], plan: dict | None = None,
                   existing: dict | None = None, *, created_at: str = "") -> dict:
    """生成绑定本地计划的 dispatch 信封。

    `actions` 必须来自 `ready_actions(state)`（或同样经过状态校验的工作项）。
    每个工作项得到稳定且唯一的 `dispatch_id` / `idempotency_key`。
    """
    if state.get("execution_backend") != "native":
        _fail("backend_not_native",
              "只有 execution_backend=native 的任务才能生成原生 dispatch。")
    task_id, contract = _require_state_credential(state)
    plan = plan or _latest_plan(state)
    plan_id = _text(plan.get("plan_id"), "plan.plan_id")
    plan_sha = _sha(plan.get("sha256"), "plan.sha256")
    _check_plan(state, plan_id, plan_sha)
    if not isinstance(actions, list) or not actions:
        _fail("dispatch_empty", "没有可派发的工作项；不能生成空 dispatch。")
    iteration_id = f"I{int(state.get('iteration_seq') or 0)}"

    items: list[dict] = []
    seen: set[str] = set()
    for idx, action in enumerate(actions, 1):
        if not isinstance(action, dict):
            _fail("schema_invalid", f"actions[{idx}] 不是对象。")
        wid = _text(action.get("id"), f"actions[{idx}].id")
        if wid in seen:
            _fail("dispatch_duplicate", f"工作项 {wid} 在同一 dispatch 中重复。")
        seen.add(wid)
        state_action = _state_action(state, wid)
        if str(state_action.get("plan_id") or "") != plan_id:
            _fail("plan_mismatch", f"{wid} 不属于计划 {plan_id}。")
        item_did, idem = _dispatch_ids(task_id, plan_id, plan_sha, wid)
        item = {
            "work_item_id": wid,
            "dispatch_id": item_did,
            "idempotency_key": idem,
            "title": str(action.get("title") or "").strip(),
            "assignee": str(action.get("assignee") or "zcode").strip().lower(),
            "goal": str(action.get("goal") or "").strip(),
            "constraints": str(action.get("constraints") or "").strip(),
            "expected_evidence": str(action.get("expected_evidence") or "").strip(),
            "depends_on": [str(d).strip() for d in (action.get("depends_on") or [])
                           if str(d).strip()],
        }
        if not item["title"]:
            _fail("schema_invalid", f"{wid} 缺少 title。")
        if item["assignee"] not in NATIVE_ASSIGNEES:
            _fail("assignee_unsupported",
                  f"{wid} 的 assignee={item['assignee']!r} 不能走原生后端。")
        items.append(item)
    if isinstance(existing, dict):
        prev = {str(i.get("work_item_id") or ""): i
                for i in existing.get("actions") or [] if isinstance(i, dict)}
        for item in items:
            old = prev.get(item["work_item_id"])
            if isinstance(old, dict) and str(old.get("dispatch_id") or "") not in (
                    "", item["dispatch_id"]):
                _fail("dispatch_id_drift",
                      f"{item['work_item_id']} 既有 dispatch_id 与当前计划绑定不一致；"
                      "拒绝覆盖。")

    batch = "b-" + sha256_text(canonical_json(
        [task_id, plan_id, plan_sha, sorted(i["dispatch_id"] for i in items)]))[:24]
    return {
        "schema": DISPATCH_SCHEMA,
        "dispatch_id": batch,
        "task_id": task_id,
        "contract_sha256": contract,
        "plan_id": plan_id,
        "plan_sha256": plan_sha,
        "iteration_id": iteration_id,
        "created_at": created_at or _now(),
        "actions": items,
    }


def validate_dispatch(envelope: object, state: dict, *,
                      require_state: bool = True) -> dict:
    """校验 dispatch 结构、计划绑定，以及工作项是否被改写。"""
    env = _obj(envelope, "dispatch")
    schema = str(env.get("schema") or "").strip()
    if schema in LEGACY_SCHEMAS:
        _fail("schema_retired",
              f"{schema} 属于 v1 SOL 通道，已退役；不能用于 v6 派发。")
    if schema != DISPATCH_SCHEMA:
        _fail("schema_invalid", f"dispatch schema 必须是 {DISPATCH_SCHEMA}。")
    task_id, contract = _require_state_credential(state)
    if str(env.get("task_id") or "").strip() != task_id:
        _fail("task_mismatch", "dispatch.task_id 与当前任务不一致。")
    if str(env.get("contract_sha256") or "").strip() != contract:
        _fail("contract_mismatch", "dispatch.contract_sha256 与当前合同不一致。")
    plan = _check_plan(state, env.get("plan_id"), env.get("plan_sha256"))
    plan_id = str(plan["plan_id"])
    plan_sha = str(plan["sha256"])
    _text(env.get("dispatch_id"), "dispatch.dispatch_id")
    _text(env.get("created_at"), "dispatch.created_at", allow_empty=True)
    raw_actions = _list(env.get("actions"), "dispatch.actions")
    if not raw_actions:
        _fail("dispatch_empty", "dispatch.actions 不能为空。")
    seen_wid: set[str] = set()
    seen_did: set[str] = set()
    seen_key: set[str] = set()
    out_actions: list[dict] = []
    for idx, raw in enumerate(raw_actions, 1):
        item = _obj(raw, f"dispatch.actions[{idx}]")
        wid = _text(item.get("work_item_id"), f"dispatch.actions[{idx}].work_item_id")
        did = _text(item.get("dispatch_id"), f"dispatch.actions[{idx}].dispatch_id")
        key = _text(item.get("idempotency_key"),
                    f"dispatch.actions[{idx}].idempotency_key")
        if wid in seen_wid:
            _fail("dispatch_duplicate", f"dispatch 内工作项 {wid} 重复。")
        if did in seen_did:
            _fail("dispatch_duplicate", f"dispatch 内 dispatch_id {did} 重复。")
        if key in seen_key:
            _fail("dispatch_duplicate", f"dispatch 内 idempotency_key {key} 重复。")
        seen_wid.add(wid)
        seen_did.add(did)
        seen_key.add(key)
        action = _state_action(state, wid) if require_state else None
        if require_state:
            if str(action.get("plan_id") or "") != plan_id:
                _fail("plan_mismatch", f"{wid} 不属于计划 {plan_id}。")
        expect_did, expect_key = _dispatch_ids(task_id, plan_id, plan_sha, wid)
        if did != expect_did or key != expect_key:
            _fail("dispatch_id_drift",
                  f"{wid} 的 dispatch_id / idempotency_key 与计划绑定不一致。")
        fields = {
            "title": _text(item.get("title"), f"dispatch.actions[{idx}].title"),
            "assignee": _text(item.get("assignee"),
                              f"dispatch.actions[{idx}].assignee").lower(),
            "goal": _text(item.get("goal"), f"dispatch.actions[{idx}].goal",
                          allow_empty=True),
            "constraints": _text(item.get("constraints"),
                                 f"dispatch.actions[{idx}].constraints",
                                 allow_empty=True),
            "expected_evidence": _text(item.get("expected_evidence"),
                                       f"dispatch.actions[{idx}].expected_evidence",
                                       allow_empty=True),
            "depends_on": [str(d).strip() for d in
                           _list(item.get("depends_on") or [],
                                 f"dispatch.actions[{idx}].depends_on")
                           if str(d).strip()],
        }
        if fields["assignee"] not in NATIVE_ASSIGNEES:
            _fail("assignee_unsupported",
                  f"{wid} 的 assignee={fields['assignee']!r} 不能走原生后端。")
        if require_state:
            for key_name, value in fields.items():
                actual = ([str(d).strip() for d in action.get("depends_on") or []]
                          if key_name == "depends_on"
                          else str(action.get(key_name) or "").strip())
                if actual != value:
                    _fail("dispatch_action_mismatch",
                          f"{wid} 的 {key_name} 与已登记工作项不一致；"
                          "主代理不得改写计划内容。")
        out_actions.append({"work_item_id": wid, "dispatch_id": did,
                            "idempotency_key": key, **fields})
    return {
        "schema": DISPATCH_SCHEMA,
        "dispatch_id": str(env["dispatch_id"]).strip(),
        "task_id": task_id,
        "contract_sha256": contract,
        "plan_id": plan_id,
        "plan_sha256": plan_sha,
        "iteration_id": str(env.get("iteration_id") or "").strip(),
        "created_at": str(env.get("created_at") or ""),
        "actions": out_actions,
    }


def _validate_executor(executor: object, name: str) -> dict:
    obj = _obj(executor, name)
    kind = _text(obj.get("kind"), f"{name}.kind").lower()
    if kind not in EXECUTOR_KINDS:
        _fail("schema_invalid", f"{name}.kind 只能是 {' / '.join(EXECUTOR_KINDS)}。")
    agent_id = str(obj.get("agent_id") or "").strip()
    child = str(obj.get("child_session_id") or "").strip()
    profile = str(obj.get("profile_id") or "").strip()
    model = str(obj.get("model_reported") or "").strip()
    # C2：正式 native 执行必须可追溯到宿主实际运行记录。主代理没有 Agent ID /
    # Child Session ID，无法提供这两项身份，因此不得冒充 native 执行方。
    if kind != "subagent":
        _fail("executor_identity_missing",
              "native 执行结果必须由原生子代理产生（kind=subagent），"
              "并提供 agent_id / child_session_id / profile_id；"
              "主代理没有可核对的 Agent/Child Session 身份。")
    if not agent_id or not child or not profile:
        _fail("executor_identity_missing",
              "子代理执行结果必须提供 agent_id / child_session_id / profile_id。")
    if profile == "reviewer":
        _fail("reviewer_as_executor", "reviewer Profile 不得承担执行工作项。")
    return {"kind": "subagent", "agent_id": agent_id,
            "child_session_id": child, "profile_id": profile,
            "model_reported": model or None}


def _validate_timing(timing: object) -> dict:
    obj = _obj(timing, "timing")
    duration = _int(obj.get("duration_ms"), "timing.duration_ms")
    return {
        "started_at": str(obj.get("started_at") or "").strip(),
        "finished_at": str(obj.get("finished_at") or "").strip(),
        "duration_ms": duration,
    }


def _validate_usage(usage: object) -> dict:
    if usage is None:
        return {}
    obj = _obj(usage, "usage")
    out = {}
    for key in ("total_tokens", "input_tokens", "output_tokens", "tool_use_count"):
        if key not in obj:
            continue
        out[key] = _int(obj[key], f"usage.{key}")
    return out


def _validate_evidence(raw: object, state: dict, *, check_files: bool) -> list[dict]:
    items = _list(raw, "evidence")
    if not items:
        _fail("evidence_missing", "执行结果必须至少包含一条原始证据文件。")
    task_id = str(state.get("task_id") or "")
    existing = {str(e.get("id") or ""): e for e in (state.get("evidence") or [])
                if isinstance(e, dict)}
    out: list[dict] = []
    seen: set[str] = set()
    for idx, item in enumerate(items, 1):
        obj = _obj(item, f"evidence[{idx}]")
        eid = _text(obj.get("id"), f"evidence[{idx}].id")
        if not EVIDENCE_ID_RE.match(eid):
            _fail("evidence_invalid", f"evidence[{idx}].id={eid!r} 非法（应形如 E1）。")
        if eid in seen:
            _fail("evidence_duplicate", f"证据 id {eid} 在结果中重复。")
        seen.add(eid)
        path = _text(obj.get("file"), f"evidence[{idx}].file")
        if not os.path.isabs(path):
            _fail("evidence_invalid", f"{eid} 的证据文件必须是绝对路径：{path}")
        digest = _sha(obj.get("sha256"), f"evidence[{idx}].sha256")
        if check_files:
            if not os.path.isfile(path):
                _fail("evidence_missing", f"{eid} 的证据文件不存在或不是普通文件：{path}")
            try:
                actual = sha256_file(path)
            except OSError as exc:
                _fail("evidence_unreadable", f"{eid} 无法读取：{exc}")
            if actual != digest:
                _fail("evidence_sha_mismatch",
                      f"{eid} 的文件 SHA-256 与结果声明不一致（可能被篡改）。")
        old = existing.get(eid)
        if isinstance(old, dict):
            if str(old.get("task_id") or "") != task_id:
                _fail("evidence_foreign", f"证据 {eid} 不属于任务 {task_id}。")
            if str(old.get("sha256") or "") != digest:
                _fail("evidence_conflict",
                      f"证据 {eid} 已登记且摘要不同；不得静默替换，请使用新 id。")
        kind = str(obj.get("kind") or "file").strip()[:64]
        note = str(obj.get("note") or "").strip()[:500]
        out.append({"id": eid, "file": path, "sha256": digest,
                    "kind": kind or "file", "note": note})
    return out



def _validate_files_inspected(raw: object, *, check_files: bool) -> list[str]:
    """校验自报的 inspected 文件清单；允许为空。

    v6.1 起复核命令与实际读取由宿主 rollout 派生，`files_inspected` 只是补充
    声明：只做路径合法性检查，不再要求至少一条或多条声明匹配。G4 的证据覆盖
    仍由宿主实际工具调用计算，不能靠这里放行。
    """
    items = _list(raw or [], "files_inspected")
    out: list[str] = []
    for idx, value in enumerate(items, 1):
        path = _text(value, f"files_inspected[{idx}]")
        if not os.path.isabs(path):
            _fail("review_inspection_invalid",
                  f"files_inspected[{idx}] 必须是绝对路径：{path}")
        if path.startswith(_NON_REGULAR_PREFIXES):
            _fail("review_inspection_invalid",
                  f"files_inspected[{idx}] 不是普通文件：{path}")
        if check_files and not os.path.isfile(path):
            _fail("review_inspection_invalid",
                  f"Reviewer 声称检查的文件不存在：{path}")
        if path not in out:
            out.append(path)
    return out


def _normalize_git_dir(segment: str) -> str:
    """剥离只切换目录的 `git -C <dir>`，其余子命令仍走只读白名单。

    `-C` 只改变后续 git 命令的工作目录，本身不写文件；不剥离的话
    `git -C <dir> show` 会被前缀白名单误拒，而子进程无法靠 `cd` 保持目录。
    只接受大写 `-C <路径>`：小写 `-c <k=v>` 可注入 `core.pager` 等执行外部程序，
    剥不掉，继续落在白名单外。
    """
    while True:
        match = _GIT_C_PREFIX_RE.match(segment)
        if not match:
            return segment
        segment = "git " + segment[match.end():]


def _strip_git_dir(argv: list[str]) -> list[str]:
    if not argv or os.path.basename(argv[0]) != "git":
        return argv
    idx = 1
    while idx + 1 < len(argv) and argv[idx] == "-C":
        idx += 2
    return [argv[0]] + argv[idx:]


def _is_readonly_prefix(segment: str) -> bool:
    segment = _normalize_git_dir(segment)
    for prefix in _READONLY_COMMAND_PREFIXES:
        if prefix.endswith(" "):
            if segment == prefix.rstrip() or segment.startswith(prefix):
                return True
        elif segment == prefix or segment.startswith(prefix + " "):
            return True
    return False


def _space_is_escaped(buf: list[str], at: int) -> bool:
    """`buf[at]` 处的 shell 空白是否被反斜杠转义（连续反斜杠数为奇数）。

    `cat /tmp/input\\ 2>/dev/null` 里空格被转义，shell 词法上 `2` 属于普通参数
    `/tmp/input 2`，不是文件描述符编号；把它当 fd 删掉会让校验 argv 与实际 argv
    不一致。`a\\\\ 2>`（转义的反斜杠 + 裸空格）则相反，仍是 fd 编号。
    """
    if at < 0 or at >= len(buf) or buf[at] not in _SHELL_SPACE:
        return False
    count = 0
    idx = at - 1
    while idx >= 0 and buf[idx] == "\\":
        count += 1
        idx -= 1
    return count % 2 == 1


def _consume_null_redirect(cmd: str, idx: int, buf: list[str], name: str) -> int:
    """消费一个独立成 token 的 `>/dev/null` 或 `2>/dev/null`，返回新下标。

    只接受 token 边界上的重定向：`>` 前必须是段首或 shell 空白；`2>` 的 `2` 前也必须是
    段首或**未转义**的 shell 空白（此时 `2` 是文件描述符编号，从缓冲区移除）。紧贴参数
    的写法（`sort -k2>/dev/null`）在 shell 中属于参数值，删掉它会让校验用 argv 与
    实际执行 argv 不一致，直接失败关闭。被转义的空格不构成 token 边界：
    `input\\ 2>/dev/null` 中的 `2` 属于普通参数，只消费重定向本体。Unicode 空白
    （如 U+00A0）不是 shell 分隔符，前后紧贴 `2` 同样按「紧贴参数」拒绝。
    """
    fd_two = False
    if buf and buf[-1] == "2" and (len(buf) == 1 or buf[-2] in _SHELL_SPACE):
        fd_two = len(buf) >= 2 and not _space_is_escaped(buf, len(buf) - 2)
    elif buf and buf[-1] not in _SHELL_SPACE:
        _fail("review_command_not_readonly",
              f"{name} 含紧贴参数的重定向（校验参数会与实际 argv 不一致）：{cmd}")
    match = _NULL_REDIRECT_AT_RE.match(cmd, idx)
    if not match:
        _fail("review_command_not_readonly",
              f"{name} 只允许指向 /dev/null 的重定向：{cmd}")
    if fd_two:
        buf.pop()
    return match.end()


def _split_readonly_segments(cmd: str, name: str) -> list[str]:
    """把顶层 `;` / `|` 拆成独立命令段；引号内的分隔符保持原样。

    只做拆分与拒绝，不做 shell 展开：`&&`、指向 /dev/null 之外的重定向、
    反引号、`$`、换行和未闭合引号都直接失败关闭。拆分后的每一段仍由
    `_validate_commands` 逐段套用只读
    前缀与破坏性参数校验。
    """
    segments: list[str] = []
    if not cmd.strip():
        _fail("review_command_not_readonly", f"{name} 是空命令。")
    buf: list[str] = []
    quote: str | None = None
    idx = 0
    while idx < len(cmd):
        ch = cmd[idx]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
                buf.append(ch)
                idx += 1
                continue
            if ch == "\\":
                if idx + 1 >= len(cmd):
                    _fail("review_command_not_readonly",
                          f"{name} 以未转义的反斜杠结尾：{cmd}")
                nxt = cmd[idx + 1]
                if nxt in "\n\r":
                    _fail("review_command_not_readonly",
                          f"{name} 使用反斜杠续行，拒绝隐藏命令边界：{cmd}")
                buf.append(ch)
                buf.append(nxt)
                idx += 2
                continue
            if ch == ">":
                idx = _consume_null_redirect(cmd, idx, buf, name)
                continue
            if ch in (";", "|"):
                if ch == "|" and idx + 1 < len(cmd) and cmd[idx + 1] == "|":
                    _fail("review_command_not_readonly",
                          f"{name} 含 `||` 控制操作符：{cmd}")
                segment = "".join(buf).strip(_SHELL_SPACE)
                if not segment:
                    _fail("review_command_not_readonly",
                          f"{name} 含空命令片段（多余分隔符）：{cmd}")
                segments.append(segment)
                buf = []
                idx += 1
                continue
            if ch in _COMMAND_SHELL_META:
                _fail("review_command_not_readonly",
                      f"{name} 含 shell 元字符 {ch!r}，可能改变工作区：{cmd}")
            buf.append(ch)
            idx += 1
            continue
        if quote == "'":
            if ch in "\n\r":
                _fail("review_command_not_readonly",
                      f"{name} 的单引号字符串含换行：{cmd}")
            buf.append(ch)
            if ch == "'":
                quote = None
            idx += 1
            continue
        # 双引号内仍禁止命令替换；反斜杠只转义下一个字符（不含换行）。
        if ch in "\n\r":
            _fail("review_command_not_readonly",
                  f"{name} 的双引号字符串含换行：{cmd}")
        if ch == "\\":
            if idx + 1 >= len(cmd):
                _fail("review_command_not_readonly",
                      f"{name} 以未转义的反斜杠结尾：{cmd}")
            nxt = cmd[idx + 1]
            if nxt in "\n\r":
                _fail("review_command_not_readonly",
                      f"{name} 使用反斜杠续行，拒绝隐藏命令边界：{cmd}")
            buf.append(ch)
            buf.append(nxt)
            idx += 2
            continue
        if ch in "`$":
            _fail("review_command_not_readonly",
                  f"{name} 含命令替换 {ch!r}：{cmd}")
        buf.append(ch)
        if ch == '"':
            quote = None
        idx += 1
    if quote is not None:
        _fail("review_command_not_readonly",
              f"{name} 的引号未闭合：{cmd}")
    segment = "".join(buf).strip(_SHELL_SPACE)
    if not segment:
        _fail("review_command_not_readonly",
              f"{name} 为空或只有空白：{cmd}")
    segments.append(segment)
    return segments


def _short_option_write_checks(argv: list[str], start: int, *, name: str,
                               segment: str, value_tokens: frozenset[str],
                               value_chars: frozenset[str],
                               write_chars: frozenset[str],
                               label: str) -> None:
    """逐字符解析短选项，跳过 `--` 之后与分离形式的选项参数值。"""
    after_dd = False
    skip_next = False
    for token in argv[start:]:
        if skip_next:
            skip_next = False
            continue
        if token == "--" and not after_dd:
            after_dd = True
            continue
        if after_dd or not token.startswith("-") or token.startswith("--"):
            continue
        if token in value_tokens:
            skip_next = True
            continue
        for ch in token[1:]:
            if ch in write_chars:
                _fail("review_command_not_readonly",
                      f"{name} 的 {label} 含写文件/执行外部程序短选项 "
                      f"{token!r}（原命令：{segment}）")
            if ch in value_chars:
                break


def _sed_script_writes(script: str) -> bool:
    """扫描 sed 脚本里的 `w`/`W`/`e` 命令与 `s///w`/`s///e` 标志。

    正则里的 `w`（如 `s/\\w/x/`）不是命令，不能误判；命令只出现在地址之后，
    因此先跳过行号、`,`、`$`、`!` 与 `/regex/` 地址，再看命令字符。
    """
    idx, size = 0, len(script)
    while idx < size:
        ch = script[idx]
        if ch in " \t\n;{}":
            idx += 1
            continue
        while idx < size and script[idx] in "0123456789$,! \t":
            idx += 1
        if idx < size and script[idx] == "/":
            idx += 1
            while idx < size:
                if script[idx] == "\\":
                    idx += 2
                    continue
                if script[idx] == "/":
                    idx += 1
                    break
                idx += 1
            while idx < size and script[idx] in "! \t":
                idx += 1
        if idx >= size:
            break
        ch = script[idx]
        if ch in _SED_WRITE_COMMANDS:
            return True
        if ch == "s":
            delim = script[idx + 1] if idx + 1 < size else ""
            if delim and delim not in "\n\\":
                pos, fields = idx + 2, 0
                while pos < size and fields < 2:
                    if script[pos] == "\\":
                        pos += 2
                        continue
                    if script[pos] == delim:
                        fields += 1
                    pos += 1
                while pos < size and script[pos] not in "\n;{}":
                    if script[pos] in _SED_SUB_WRITE_FLAGS:
                        return True
                    pos += 1
                idx = pos
                continue
        if ch == "y":
            # y/src/dst/ 与 s/// 结构相同：两个字段都被分隔符包裹，内容是文本，
            # 不是命令；跳过整个 transliterate 命令。
            delim = script[idx + 1] if idx + 1 < size else ""
            if delim and delim not in "\n\\":
                pos, fields = idx + 2, 0
                while pos < size and fields < 2:
                    if script[pos] == "\\":
                        pos += 2
                        continue
                    if script[pos] == delim:
                        fields += 1
                    pos += 1
                idx = pos
                continue
        if ch in "aic":
            # a\/i\/c\ 后面直到行尾是插入/追加/替换文本；其中的 w/e 不是命令。
            pos = idx + 1
            while pos < size and script[pos] != "\n":
                pos += 1
            idx = pos
            continue
        idx += 1
    return False


def _sed_program_writes(tokens: list[str], *, name: str, segment: str) -> None:
    programs: list[str] = []
    positionals: list[str] = []
    has_expression = False
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if token == "--":
            positionals.extend(tokens[idx + 1:])
            break
        if token.startswith("--"):
            option, has_value, value = token[2:].partition("=")
            if len(option) >= 2 and "expression".startswith(option):
                if has_value:
                    programs.append(value)
                elif idx + 1 < len(tokens):
                    programs.append(tokens[idx + 1])
                    idx += 1
                else:
                    _fail("review_command_not_readonly",
                          f"{name} 的 sed --expression 缺少脚本：{segment}")
                has_expression = True
                idx += 1
                continue
            if len(option) >= 2 and "line-length".startswith(option):
                idx += 1 if has_value else 2
                continue
            if option not in _SED_READONLY_LONG_OPTIONS:
                if len(option) >= 2 and "in-place".startswith(option):
                    _fail("review_command_not_readonly",
                          f"{name} 的 sed 使用就地编辑（-i/--in-place）：{segment}")
                if len(option) >= 2 and "file".startswith(option):
                    _fail("review_command_not_readonly",
                          f"{name} 的 sed 从文件加载脚本，无法确认只读：{segment}")
                _fail("review_command_not_readonly",
                      f"{name} 的 sed 长选项 --{option} 不在只读白名单：{segment}")
            idx += 1
            continue
        if token.startswith("-") and token != "-":
            flags = token[1:]
            # -e 之后整个剩余部分是脚本，不能按选项字母扫描（`-es/i/x/` 是
            # 合法脚本，不是就地编辑）；只有 -e 之前的字母才是选项。
            if "e" in flags:
                before_e = flags.split("e", 1)[0]
            else:
                before_e = flags
            if "i" in before_e:
                _fail("review_command_not_readonly",
                      f"{name} 的 sed 使用就地编辑（-i/--in-place）：{segment}")
            if "f" in before_e:
                _fail("review_command_not_readonly",
                      f"{name} 的 sed 从文件加载脚本，无法确认只读：{segment}")
            if "e" in flags:
                rest = flags[flags.index("e") + 1:]
                if rest:
                    programs.append(rest)
                elif idx + 1 < len(tokens):
                    programs.append(tokens[idx + 1])
                    idx += 1
                else:
                    _fail("review_command_not_readonly",
                          f"{name} 的 sed -e 缺少脚本：{segment}")
                has_expression = True
            elif "l" in before_e and not before_e[before_e.index("l") + 1:]:
                idx += 1  # -l 后跟独立的行宽参数
            idx += 1
            continue
        positionals.append(token)
        idx += 1
    if not has_expression and positionals:
        programs.append(positionals[0])
    for program in programs:
        if _sed_script_writes(program):
            _fail("review_command_not_readonly",
                  f"{name} 的 sed 脚本含写文件/执行外部程序命令 "
                  f"（w/W/e）：{segment}")


def _awk_strip_strings(program: str) -> str:
    """把 awk 字符串字面量替换为空串（保留引号位置），用于命令/管道检测。"""
    out: list[str] = []
    idx, size = 0, len(program)
    while idx < size:
        if program[idx] == '"':
            out.append('""')
            idx += 1
            while idx < size:
                if program[idx] == "\\":
                    idx += 2
                    continue
                if program[idx] == '"':
                    idx += 1
                    break
                idx += 1
            continue
        out.append(program[idx])
        idx += 1
    return "".join(out)


def _awk_in_string(program: str, pos: int) -> bool:
    """pos 是否位于 awk 字符串字面量内部（用于排除字符串里的 print 字样）。"""
    idx, in_string = 0, False
    while idx < pos:
        ch = program[idx]
        if in_string:
            if ch == "\\":
                idx += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        idx += 1
    return in_string


def _awk_print_redirects(program: str) -> bool:
    """只检查 print/printf 语句顶层的输出重定向与管道。

    比较运算（`NR > 1`）、逻辑或（`||`）与括号内的比较表达式不是写操作；
    `print (a > b)` 里的 `>` 位于括号内，也不判写。
    """
    for match in _AWK_PRINT_RE.finditer(program):
        if _awk_in_string(program, match.start()):
            continue
        idx = match.end()
        depth = 0
        while idx < len(program):
            ch = program[idx]
            if ch == '"':
                idx += 1
                while idx < len(program):
                    if program[idx] == "\\":
                        idx += 2
                        continue
                    if program[idx] == '"':
                        idx += 1
                        break
                    idx += 1
                continue
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth = max(0, depth - 1)
            elif depth == 0 and ch in ";\n}":
                break
            elif depth == 0 and ch == ">":
                # `>` 是输出重定向；`>=` 是比较运算符，不写文件。
                if program.startswith("=", idx + 1):
                    idx += 2
                    continue
                return True
            elif depth == 0 and ch == "|":
                # `||` 是逻辑或；`|` 后跟命令名或 `|&` 才是管道。
                if program.startswith("|", idx + 1):
                    idx += 2
                    continue
                return True
            idx += 1
    return False


def _awk_program_writes(tokens: list[str], *, name: str, segment: str) -> None:
    programs: list[str] = []
    positionals: list[str] = []
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if token == "--":
            positionals.extend(tokens[idx + 1:])
            break
        if token.startswith("--"):
            option, has_value, _ = token[2:].partition("=")
            if option not in _AWK_READONLY_LONG_OPTIONS:
                _fail("review_command_not_readonly",
                      f"{name} 的 awk 长选项 --{option} 不在只读白名单"
                      f"（可能是写文件/加载脚本）：{segment}")
            idx += 1 if has_value or option not in ("field-separator", "assign") else 2
            continue
        if token.startswith("-") and token != "-":
            flag = token[1:2]
            if flag in ("F", "v"):
                idx += 1 if len(token) > 2 else 2
                continue
            if flag in ("b", "s", "M") and len(token) == 2:
                idx += 1
                continue
            if flag == "W" and token[2:] in ("", "version"):
                if token[2:] == "version":
                    idx += 1
                elif idx + 1 < len(tokens) and tokens[idx + 1] == "version":
                    idx += 2
                else:
                    _fail("review_command_not_readonly",
                          f"{name} 的 awk -W 只允许 version：{segment}")
                continue
            _fail("review_command_not_readonly",
                  f"{name} 的 awk 短选项 -{flag} 不在只读白名单"
                  f"（可能是写文件/加载脚本）：{segment}")
        positionals.append(token)
        idx += 1
    if positionals:
        programs.append(positionals[0])
    for program in programs:
        # 反斜杠续行会把 `system\` + 换行 + `(` 拆开，先归一化再匹配。
        normalized = program.replace("\\\n", "")
        stripped = _awk_strip_strings(normalized)
        if _AWK_SYSTEM_RE.search(stripped):
            _fail("review_command_not_readonly",
                  f"{name} 的 awk 程序执行外部命令（system）：{segment}")
        if _AWK_PIPE_GETLINE_RE.search(stripped):
            _fail("review_command_not_readonly",
                  f"{name} 的 awk 程序通过管道执行外部命令：{segment}")
        if _awk_print_redirects(normalized):
            _fail("review_command_not_readonly",
                  f"{name} 的 awk print/printf 语句含输出重定向/管道：{segment}")


def _subcommand_check(argv: list[str], allowed: frozenset[str], *,
                      family: str, name: str, segment: str) -> None:
    for token in argv[1:]:
        if token.startswith("-"):
            continue
        if token not in allowed:
            _fail("review_command_not_readonly",
                  f"{name} 的 {family} 子命令 {token!r} 不是只读用法；"
                  f"只允许 {', '.join(sorted(allowed))}：{segment}")
        return
    _fail("review_command_not_readonly",
          f"{name} 的 {family} 缺少只读子命令：{segment}")


def _family_readonly_checks(argv: list[str], family: str, *, name: str,
                            segment: str) -> None:
    """对白名单里“本身只读、但带写参数”的命令族做 argv 级检查。"""
    tokens = argv[1:]
    if family == "date":
        idx = 0
        while idx < len(tokens):
            token = tokens[idx]
            if token.startswith("--"):
                option, has_value, _ = token[2:].partition("=")
                if len(option) >= 1 and "set".startswith(option):
                    _fail("review_command_not_readonly",
                          f"{name} 的 date 在设置系统时间（-s/--set）：{segment}")
                # 只有必需取值的选项才消费下一个 token；`--iso-8601[=FMT]` 的
                # 参数必须紧贴（写成 `--iso-8601 FMT` 时 FMT 仍是位置参数）。
                takes_value = len(option) >= 2 and sum(
                    1 for candidate in ("date", "file", "reference", "rfc-3339")
                    if candidate.startswith(option)) == 1
                idx += 1 if has_value or not takes_value else 2
                continue
            if token.startswith("-") and token != "-":
                flags = token[1:]
                take_next = False
                for pos, flag in enumerate(flags):
                    if flag == "s":
                        _fail("review_command_not_readonly",
                              f"{name} 的 date 在设置系统时间（-s/--set）：{segment}")
                    if flag == "I":
                        # `-I[FMT]` 的可选参数只能紧贴（余下字符即格式）；
                        # 绝不消费下一个 token。
                        break
                    if flag in "dfr":
                        # 聚簇开关里只有 `-d/-f/-r` 取值；位于末尾时才吃下一个 token。
                        take_next = pos == len(flags) - 1
                        break
                idx += 2 if take_next else 1
                continue
            if not token.startswith("+"):
                # `date MMDDhhmm[[CC]YY]` 是设置日期/时间的写法；只放行输出格式。
                _fail("review_command_not_readonly",
                      f"{name} 的 date 位置参数会设置日期/时间：{segment}")
            idx += 1
    elif family == "tree":
        for token in tokens:
            if token in _TREE_WRITE_OPTIONS or token.startswith("--output="):
                _fail("review_command_not_readonly",
                      f"{name} 的 tree 写入输出文件（-o/--output）：{segment}")
    elif family == "xxd":
        positionals: list[str] = []
        idx = 0
        while idx < len(tokens):
            token = tokens[idx]
            if token == "--":
                positionals.extend(tokens[idx + 1:])
                break
            if token.startswith("-") and token != "-":
                idx += 2 if token in _XXD_VALUE_OPTIONS else 1
                continue
            positionals.append(token)
            idx += 1
        if len(positionals) > 1:
            _fail("review_command_not_readonly",
                  f"{name} 的 xxd 第二个位置参数是输出文件：{segment}")
    elif family == "file":
        for token in tokens:
            if token.startswith("--"):
                option = token[2:].split("=", 1)[0]
                # `--co` 已是 --compile 的唯一缩写（--checking-printout 以 "ch"
                # 开头，不构成竞争），两字母也必须拒绝。
                if option.startswith("co"):
                    _fail("review_command_not_readonly",
                          f"{name} 的 file 编译 magic 文件会写入磁盘"
                          f"（-C/--compile）：{segment}")
            elif token.startswith("-") and token != "-" and "C" in token[1:]:
                _fail("review_command_not_readonly",
                      f"{name} 的 file 编译 magic 文件会写入磁盘"
                      f"（-C/--compile）：{segment}")
    elif family == "unzip":
        shorts = [t for t in tokens
                  if t.startswith("-") and not t.startswith("--") and t != "-"]
        if not any(t.startswith(_UNZIP_LIST_OPTIONS) for t in shorts):
            _fail("review_command_not_readonly",
                  f"{name} 的 unzip 不是列目录模式（需要 -l/-v/-t/-Z）：{segment}")
        for token in tokens:
            if token == "-d" or token.startswith("--directory") or (
                    token.startswith("-") and not token.startswith("--")
                    and "d" in token[1:]):
                _fail("review_command_not_readonly",
                      f"{name} 的 unzip 指定解包目录（-d/--directory）：{segment}")
    elif family == "aapt":
        _subcommand_check(argv, _AAPT_READONLY_SUBCOMMANDS, family=family,
                          name=name, segment=segment)
    elif family == "aapt2":
        _subcommand_check(argv, _AAPT2_READONLY_SUBCOMMANDS, family=family,
                          name=name, segment=segment)
    elif family == "apksigner":
        _subcommand_check(argv, _APKSIGNER_READONLY_SUBCOMMANDS, family=family,
                          name=name, segment=segment)
    elif family == "sed":
        _sed_program_writes(tokens, name=name, segment=segment)
    elif family == "awk":
        _awk_program_writes(tokens, name=name, segment=segment)


def _argv_readonly_checks(segment: str, name: str) -> None:
    """对 shell 展开后的 argv 做一次引号无关的写操作/外部程序检查。"""
    try:
        argv = shlex.split(segment, posix=True)
    except ValueError as exc:
        _fail("review_command_not_readonly",
              f"{name} 的命令无法解析引号：{exc}（原命令：{segment}）")
    if not argv:
        _fail("review_command_not_readonly", f"{name} 是空命令。")
    argv = _strip_git_dir(argv)
    head = os.path.basename(argv[0])
    family = f"git {argv[1]}" if head == "git" and len(argv) > 1 else head
    value_tokens = _VALUE_TOKENS.get(family, frozenset())
    after_dd = False
    skip_next = False
    for token in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        # find 的 `--` 只是选项终止符，不是表达式终止符：GNU find/bfs 仍会把
        # 其后的 `-delete`/`-exec`/`-fprint` 当谓词执行，因此 find 不跳过
        # `--` 之后的 token。其他命令族的 `--` 之后是操作数，按只读放行。
        if token == "--" and family != "find":
            after_dd = True
            continue
        if after_dd:
            continue
        if token in value_tokens:
            skip_next = True
            continue
        if _ARGV_DENY_RE.match(token):
            _fail("review_command_not_readonly",
                  f"{name} 含破坏性参数 {token!r}（原命令：{segment}）")
        if token.startswith("--"):
            opt = token.split("=", 1)[0]
            if opt not in _ABBREV_ALLOW_EXACT.get(family, ()):
                for bad in _ABBREV_FORBIDDEN.get(family, ()):
                    if bad == opt or bad.startswith(opt):
                        _fail("review_command_not_readonly",
                              f"{name} 的 {family} 使用危险长选项缩写/全名 "
                              f"{token!r}（原命令：{segment}）")
    if family == "sort":
        _short_option_write_checks(
            argv, 1, name=name, segment=segment,
            value_tokens=_VALUE_TOKENS["sort"],
            value_chars=_SORT_SHORT_VALUE, write_chars=_SORT_SHORT_WRITE,
            label="sort")
    if family == "git grep":
        _short_option_write_checks(
            argv, 2, name=name, segment=segment,
            value_tokens=_VALUE_TOKENS["git grep"],
            value_chars=_GIT_GREP_SHORT_VALUE,
            write_chars=frozenset("O"), label="git grep")
    if family == "rg":
        _short_option_write_checks(
            argv, 1, name=name, segment=segment,
            value_tokens=_VALUE_TOKENS["rg"],
            value_chars=_RG_SHORT_VALUE, write_chars=frozenset("z"),
            label="rg")
    if family == "uniq":
        # uniq 的第二个位置参数是输出文件；`-`、/dev/stdout 等标准输出仍算只读。
        # 带参选项（-f/-s/-w 等）先跳过其值，避免把选项参数当成文件。
        value_opts = {"-f", "-s", "-w", "--skip-fields", "--skip-chars",
                      "--check-chars"}
        stdout_targets = {"-", "/dev/stdout", "/dev/fd/1", "/proc/self/fd/1"}
        positional: list[str] = []
        after_dd = False
        idx = 1
        while idx < len(argv):
            token = argv[idx]
            if not after_dd and token == "--":
                after_dd = True
                idx += 1
                continue
            if not after_dd and token.startswith("-") and token != "-":
                if token in value_opts and idx + 1 < len(argv):
                    idx += 2
                    continue
                idx += 1
                continue
            positional.append(token)
            idx += 1
        if len(positional) > 1 and positional[1] not in stdout_targets:
            _fail("review_command_not_readonly",
                  f"{name} 的 uniq 带输出文件参数：{positional[1]!r}")
    _family_readonly_checks(argv, family, name=name, segment=segment)


def _validate_readonly_command(cmd: str, name: str) -> None:
    for segment in _split_readonly_segments(cmd, name):
        if not _is_readonly_prefix(segment):
            _fail("review_command_not_readonly",
                  f"{name} 的片段不在只读命令白名单：{segment}（原命令：{cmd}）")
        _argv_readonly_checks(segment, name)


# 复核里常见的批量检查写法：`for f in a b; do sha256sum "$f"; done`。它本身
# 只是把同一只读命令作用到若干字面参数上，不引入外部输入，因此按字面量展开后
# 逐条校验。通配符、命令替换、变量拼接仍然拒绝：展开后的实际参数必须可见。
_LOOP_COMMAND_RE = re.compile(
    r"^for[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]+in(?:[ \t]+([^;\n]*?))?[ \t]*;"
    r"[ \t]*do[ \t]+(.+?)[ \t]*;[ \t]*done$", re.S)
LOOP_MAX_EXPANSIONS = 64
_LOOP_WORD_FORBIDDEN = "$`*?[]{};|><&'\"\\\n\r"


def _loop_expansions(cmd: str, name: str) -> list[str] | None:
    """把单层 `for VAR in <字面量...>; do <body>; done` 展开成具体命令。

    返回 None 表示不是循环；返回命令行列表表示展开结果。任何不可见输入
    （通配符、命令替换、变量拼接）都失败关闭，避免“校验看到的参数”与
    “实际执行的参数”不一致。
    """
    match = _LOOP_COMMAND_RE.match(cmd)
    if not match:
        return None
    var, words, body = match.group(1), match.group(2) or "", match.group(3)
    try:
        parts = shlex.split(words, posix=True)
    except ValueError as exc:
        _fail("review_command_not_readonly",
              f"{name} 的 for 循环参数无法解析：{exc}（原命令：{cmd}）")
    if not parts:
        _fail("review_command_not_readonly",
              f"{name} 的 for 循环没有字面量参数：{cmd}")
    if len(parts) > LOOP_MAX_EXPANSIONS:
        _fail("review_command_not_readonly",
              f"{name} 的 for 循环参数超过 {LOOP_MAX_EXPANSIONS} 个，"
              f"请改用单条命令：{cmd}")
    expanded_patterns = (
        re.compile(r"\$\{" + re.escape(var) + r"\}"),
        re.compile(r"\$" + re.escape(var) + r"(?![A-Za-z0-9_])"),
    )
    out: list[str] = []
    for word in parts:
        bad = [ch for ch in word if ch in _LOOP_WORD_FORBIDDEN]
        if bad:
            _fail("review_command_not_readonly",
                  f"{name} 的 for 循环参数含不可见展开 {bad[0]!r}：{cmd}")
        text = body
        for pattern in expanded_patterns:
            text = pattern.sub(lambda _m, value=word: value, text)
        out.append(text)
    return out


def _validate_commands(raw: object) -> list[str]:
    items = _list(raw or [], "commands_run")
    out: list[str] = []
    for idx, value in enumerate(items, 1):
        name = f"commands_run[{idx}]"
        if not isinstance(value, str):
            _fail("schema_invalid", f"{name} 必须是字符串。")
        # 只裁 Shell 空白：Unicode 空白（如 U+00A0）是 argv 的一部分，通用
        # `.strip()` 会删除实际参数字符，使校验 argv 与 Bash argv 不一致。
        cmd = value.strip(_SHELL_SPACE + "\r\n")
        if not cmd:
            _fail("schema_invalid", f"{name} 不能为空。")
        expansions = _loop_expansions(cmd, name)
        if expansions is None:
            _validate_readonly_command(cmd, name)
        else:
            for expanded in expansions:
                _validate_readonly_command(expanded, name)
        # 保留原始命令文本：review 信封的 result_sha256 绑定的是 Reviewer 原始输出。
        out.append(cmd)
    return out


def validate_review_commands(raw: object) -> list[str]:
    """公开只读校验入口：供 native_agent 复核宿主记录的实际 Bash 命令。"""
    return _validate_commands(raw)


def _validate_findings(raw: object, *, check_files: bool) -> list[dict]:
    items = _list(raw or [], "findings")
    out: list[dict] = []
    for idx, value in enumerate(items, 1):
        obj = _obj(value, f"findings[{idx}]")
        severity = _text(obj.get("severity"), f"findings[{idx}].severity").lower()
        if severity not in FINDING_SEVERITIES:
            _fail("review_finding_invalid",
                  f"findings[{idx}].severity 只能是 {', '.join(FINDING_SEVERITIES)}。")
        summary = _text(obj.get("summary"), f"findings[{idx}].summary")
        file = str(obj.get("file") or "").strip()
        evidence = str(obj.get("evidence") or "").strip()
        if severity in ("blocking", "major"):
            if not file or not evidence:
                _fail("review_finding_invalid",
                      f"{severity} finding 必须给出 file 与 evidence（具体证据）。")
        if file and check_files and os.path.isabs(file) and not os.path.exists(file):
            _fail("review_finding_invalid",
                  f"findings[{idx}].file 不存在：{file}")
        line = obj.get("line")
        if line is not None:
            line = _int(line, f"findings[{idx}].line", minimum=1)
        out.append({"severity": severity, "summary": summary,
                    "file": file, "line": line, "evidence": evidence})
    return out


def _validate_reviewer(reviewer: object) -> dict:
    obj = _obj(reviewer, "reviewer")
    kind = _text(obj.get("kind") or "subagent", "reviewer.kind").lower()
    if kind != "subagent":
        _fail("reviewer_invalid", "Reviewer 必须是独立子代理（kind=subagent）。")
    agent_id = _text(obj.get("agent_id"), "reviewer.agent_id")
    child = _text(obj.get("child_session_id"), "reviewer.child_session_id")
    profile = _text(obj.get("profile_id"), "reviewer.profile_id")
    if profile != "reviewer":
        _fail("reviewer_profile_invalid",
              f"Reviewer 必须使用专用 reviewer Profile（实际 {profile!r}）。")
    model = _text(obj.get("model_reported"), "reviewer.model_reported")
    return {
        "kind": "subagent",
        "agent_id": agent_id,
        "child_session_id": child,
        "profile_id": profile,
        "model_reported": model,
    }



def validate_execution_result(envelope: object, dispatch: dict | None, state: dict, *,
                              check_files: bool = True) -> dict:
    """校验执行结果：计划绑定、结果摘要、证据文件、执行方元数据。"""
    env = _obj(envelope, "execution result")
    schema = str(env.get("schema") or "").strip()
    if schema in LEGACY_SCHEMAS:
        _fail("schema_retired",
              f"{schema} 属于 v1 SOL 通道，已退役；不能用于 v6 回填。")
    if schema != RESULT_SCHEMA:
        _fail("schema_invalid", f"execution schema 必须是 {RESULT_SCHEMA}。")
    if not isinstance(dispatch, dict):
        _fail("dispatch_missing", "状态没有 native_dispatch；不能回填执行结果。")
    task_id, contract = _require_state_credential(state)
    if str(env.get("task_id") or "").strip() != task_id:
        _fail("task_mismatch", "执行结果 task_id 与当前任务不一致。")
    if str(env.get("contract_sha256") or "").strip() != contract:
        _fail("contract_mismatch", "执行结果 contract_sha256 与当前合同不一致。")
    plan_id = _text(env.get("plan_id"), "execution.plan_id")
    plan_sha = _sha(env.get("plan_sha256"), "execution.plan_sha256")
    if plan_id != str(dispatch.get("plan_id") or ""):
        _fail("plan_mismatch", "执行结果 plan_id 与当前 dispatch 不一致。")
    if plan_sha != str(dispatch.get("plan_sha256") or "").lower():
        _fail("plan_mismatch", "执行结果 plan_sha256 与当前 dispatch 不一致。")
    _check_plan(state, plan_id, plan_sha)
    wid = _text(env.get("work_item_id"), "execution.work_item_id")
    did = _text(env.get("dispatch_id"), "execution.dispatch_id")
    items = {str(i.get("work_item_id") or ""): i
             for i in dispatch.get("actions") or [] if isinstance(i, dict)}
    item = items.get(wid)
    if not isinstance(item, dict):
        _fail("work_item_unknown", f"当前 dispatch 不含工作项 {wid}。")
    if str(item.get("dispatch_id") or "") != did:
        _fail("dispatch_mismatch",
              f"执行结果 dispatch_id={did} 与工作项 {wid} 的派发 ID 不一致。")
    idem_key = _text(env.get("idempotency_key"), "execution.idempotency_key")
    if idem_key != str(item.get("idempotency_key") or ""):
        _fail("idempotency_mismatch",
              f"执行结果 idempotency_key 与工作项 {wid} 的派发键不一致。")
    iteration_id = str(env.get("iteration_id") or "").strip()
    if iteration_id != str(dispatch.get("iteration_id") or ""):
        _fail("iteration_mismatch", "执行结果 iteration_id 与当前 dispatch 不一致。")
    action = _state_action(state, wid)
    if str(action.get("status") or "") in ("done", "dropped"):
        old_shas = {
            str(rec.get("result_sha256") or "")
            for rec in state.get("native_results") or []
            if isinstance(rec, dict) and str(rec.get("work_item_id") or "") == wid
        }
        declared = str(env.get("result_sha256") or "").lower()
        if declared not in old_shas:
            _fail("work_item_terminal",
                  f"{wid} 已终结（{action.get('status')}）；不同结果不得覆盖历史。")
    status = _text(env.get("status"), "execution.status").lower()
    if status not in EXECUTION_STATUSES:
        _fail("invalid_status",
              f"执行结果 status={status!r} 非法；只能是 {', '.join(EXECUTION_STATUSES)}。")
    summary = _text(env.get("summary"), "execution.summary")
    result = _text(env.get("result"), "execution.result")
    executor = _validate_executor(env.get("executor"), "execution.executor")
    timing = _validate_timing(env.get("timing"))
    usage = _validate_usage(env.get("usage"))
    evidence = _validate_evidence(env.get("evidence"), state, check_files=check_files)
    declared_sha = _sha(env.get("result_sha256"), "execution.result_sha256")
    actual_sha = envelope_sha256(env)
    if declared_sha != actual_sha:
        _fail("result_sha_mismatch",
              "执行结果 result_sha256 与信封内容不一致（可能被篡改）。")
    return {
        "schema": RESULT_SCHEMA,
        "task_id": task_id,
        "contract_sha256": contract,
        "plan_id": plan_id,
        "plan_sha256": plan_sha,
        "iteration_id": iteration_id,
        "dispatch_id": did,
        "idempotency_key": idem_key,
        "work_item_id": wid,
        "status": status,
        "summary": summary,
        "result": result,
        "evidence": evidence,
        "executor": executor,
        "timing": timing,
        "usage": usage,
        "result_sha256": declared_sha,
    }


def _validate_host_record(raw: object, *, required: bool) -> dict:
    """校验宿主运行记录引用；G4 复核必须有可重新核对的宿主文件。"""
    if raw is None:
        if required:
            _fail("g4_host_record_missing",
                  "g4_eligible=true 的复核必须附宿主 metadata/outputFile/rollout 记录。")
        return {}
    obj = _obj(raw, "host")
    out: dict = {}
    for key in ("metadata_path", "output_file", "rollout_path"):
        path = _text(obj.get(key), f"host.{key}")
        if not os.path.isabs(path):
            _fail("g4_host_record_invalid", f"host.{key} 必须是绝对路径：{path}")
        if not os.path.isfile(path):
            _fail("g4_host_record_invalid", f"host.{key} 不存在：{path}")
        out[key] = path
    calls = _list(obj.get("tool_calls"), "host.tool_calls")
    if required and not calls:
        _fail("g4_tool_calls_missing",
              "G4 复核必须提供 Reviewer 实际工具调用记录；不能只有自述。")
    normalized = []
    for idx, call in enumerate(calls, 1):
        item = _obj(call, f"host.tool_calls[{idx}]")
        name = _text(item.get("name"), f"host.tool_calls[{idx}].name")
        if name not in REVIEWER_TOOLS:
            _fail("g4_tool_not_readonly",
                  f"G4 复核出现非只读工具 {name!r}；只允许 {', '.join(REVIEWER_TOOLS)}。")
        normalized.append({"name": name,
                           "count": _int(item.get("count") or 0,
                                         f"host.tool_calls[{idx}].count")})
    out["tool_calls"] = normalized
    out["agent_id"] = str(obj.get("agent_id") or "").strip()
    out["child_session_id"] = str(obj.get("child_session_id") or "").strip()
    return out


def _action_fingerprint(action: dict) -> str:
    """工作项覆盖指纹：与 final_evidence_digest 的动作项口径保持一致。"""
    return sha256_text(canonical_json([
        action.get("status"),
        sha256_text(str(action.get("result") or "")),
        sorted(action.get("evidence_ids") or []),
    ]))


def review_coverage(state: dict) -> dict:
    """复核覆盖快照：证据摘要、工作项指纹与条件状态。

    增量复核只允许在“未覆盖的变化”都能被本轮逐条点出时使用；这份快照在
    ingest 时固化，后续据它计算变化集，避免把旧结论套到新状态上。
    """
    evidence: dict[str, str] = {}
    for entry in state.get("evidence") or []:
        if isinstance(entry, dict) and str(entry.get("id") or ""):
            # 归档（dropped）也是覆盖状态的一部分：若不进入指纹，基线后归档
            # 证据 + 改动工作项结果会被误判为“无需覆盖新证据”的增量复核。
            evidence[str(entry["id"])] = "%s:%s" % (
                str(entry.get("sha256") or ""),
                "dropped" if entry.get("dropped") else "live")
    actions: dict[str, str] = {}
    for action in state.get("actions") or []:
        if not isinstance(action, dict):
            continue
        if str(action.get("origin") or "") == "g4-review":
            continue
        actions[str(action.get("id") or "")] = _action_fingerprint(action)
    criteria: dict[str, str] = {}
    for criterion in (state.get("contract") or {}).get("criteria") or []:
        if not isinstance(criterion, dict):
            continue
        criteria[str(criterion.get("criterion_id") or "")] = sha256_text(
            canonical_json([criterion.get("status"),
                            sorted([[r.get("id"), r.get("sha256")]
                                    for r in criterion.get("evidence_refs") or []])]))
    return {"evidence": evidence, "actions": actions, "criteria": criteria}


def _validate_review_scope(env: dict, state: dict, *, contract: str,
                           work_item_id: str) -> tuple[str, str, list[str]]:
    """校验复核范围；增量复核必须绑定一条可用基线并覆盖全部变化。"""
    scope = str(env.get("review_scope") or "full").strip().lower()
    if scope not in REVIEW_SCOPES:
        _fail("review_scope_invalid",
              f"review_scope 只能是 {', '.join(REVIEW_SCOPES)}（实际 {scope!r}）。")
    if scope == "full":
        if env.get("based_on_review_sha256") or env.get("evidence_manifest"):
            _fail("review_scope_invalid",
                  "review_scope=full 不应带 based_on_review_sha256 / "
                  "evidence_manifest；增量复核请显式声明 review_scope=incremental。")
        return scope, "", []
    based_on = _sha(env.get("based_on_review_sha256"),
                    "review.based_on_review_sha256")
    base = None
    for record in state.get("native_reviews") or []:
        if isinstance(record, dict) and str(record.get("result_sha256") or "") == based_on:
            base = record
            break
    if base is None:
        _fail("incremental_base_missing",
              f"增量复核的基线 {based_on[:16]}… 不在本任务复核记录中；"
              "请先回填基线复核，或改用完整复核。")
    if str(base.get("contract_sha256") or "") != contract:
        _fail("incremental_base_contract_mismatch",
              "基线复核属于旧合同；合同已变更，必须做完整复核。")
    if str(base.get("verdict") or "").strip().lower() != "pass" or base.get(
            "g4_eligible") is not True:
        _fail("incremental_base_ineligible",
              "基线复核不是可用的 G4 PASS 结论；不能作为增量基础。")
    if any(isinstance(f, dict) and str(f.get("severity") or "") == "blocking"
           for f in base.get("findings") or []):
        _fail("incremental_base_ineligible", "基线复核仍有 blocking finding。")
    base_coverage = base.get("coverage")
    if not isinstance(base_coverage, dict) or not base_coverage:
        _fail("incremental_base_coverage_missing",
              "基线复核缺少覆盖快照（旧记录）；请做一次完整复核后再增量。")
    current = review_coverage(state)
    base_evidence = base_coverage.get("evidence") or {}
    base_actions = base_coverage.get("actions") or {}
    base_criteria = base_coverage.get("criteria") or {}
    changed_evidence = sorted(
        eid for eid, sha in current["evidence"].items()
        if base_evidence.get(eid) != sha)
    changed_actions = sorted(
        aid for aid, fp in current["actions"].items()
        if base_actions.get(aid) != fp)
    changed_criteria = sorted(
        cid for cid, fp in current["criteria"].items()
        if base_criteria.get(cid) != fp)
    if changed_criteria:
        _fail("incremental_criteria_changed",
              "完成条件或其核销状态在基线与本轮之间发生变化（"
              f"{', '.join(changed_criteria)}）；改动范围超出增量复核，"
              "必须做完整复核。")
    manifest = [str(item).strip()
                for item in _list(env.get("evidence_manifest") or [],
                                  "review.evidence_manifest")
                if str(item).strip()]
    unknown = [eid for eid in manifest if eid not in current["evidence"]]
    if unknown:
        _fail("incremental_manifest_unknown",
              f"evidence_manifest 含未登记的证据 id：{', '.join(unknown)}。")
    if changed_evidence:
        if not manifest:
            _fail("incremental_manifest_missing",
                  "本轮新增/变更证据必须逐条列入 evidence_manifest："
                  f"{', '.join(changed_evidence)}。")
        missing = [eid for eid in changed_evidence if eid not in manifest]
        if missing:
            _fail("incremental_evidence_uncovered",
                  "增量复核未覆盖新增/变更证据：" + ", ".join(missing) +
                  "；请检查这些证据或改做完整复核。")
    others = [aid for aid in changed_actions if aid != work_item_id]
    if others:
        _fail("incremental_scope_too_narrow",
              "基线之后还有其他工作项的结论变化（" + ", ".join(others) +
              "）；增量复核只覆盖被复核的工作项，请做完整复核。")
    if not changed_evidence and not changed_actions:
        _fail("incremental_without_change",
              "相对基线没有任何变化，无需增量复核；如需复述结论请复用基线。")
    return scope, based_on, manifest


def validate_review_result(envelope: object, dispatch: dict | None, execution: dict,
                           state: dict, *, check_files: bool = True) -> dict:
    """校验独立复核：绑定执行结果 SHA、Reviewer 独立性、只读性与 PASS 门槛。"""
    env = _obj(envelope, "review result")
    schema = str(env.get("schema") or "").strip()
    if schema in LEGACY_SCHEMAS:
        _fail("schema_retired",
              f"{schema} 属于 v1 SOL 通道，已退役；不能用于 v6 复核。")
    if schema != REVIEW_SCHEMA:
        _fail("schema_invalid", f"review schema 必须是 {REVIEW_SCHEMA}。")
    if not isinstance(dispatch, dict):
        _fail("dispatch_missing", "状态没有 native_dispatch；不能回填复核结果。")
    task_id, contract = _require_state_credential(state)
    if str(env.get("task_id") or "").strip() != task_id:
        _fail("task_mismatch", "复核结果 task_id 与当前任务不一致。")
    if str(env.get("contract_sha256") or "").strip() != contract:
        _fail("contract_mismatch", "复核结果 contract_sha256 与当前合同不一致。")
    plan_id = _text(env.get("plan_id"), "review.plan_id")
    plan_sha = _sha(env.get("plan_sha256"), "review.plan_sha256")
    if plan_id != str(dispatch.get("plan_id") or ""):
        _fail("plan_mismatch", "复核结果 plan_id 与当前 dispatch 不一致。")
    if plan_sha != str(dispatch.get("plan_sha256") or "").lower():
        _fail("plan_mismatch", "复核结果 plan_sha256 与当前 dispatch 不一致。")
    _check_plan(state, plan_id, plan_sha)
    wid = _text(env.get("work_item_id"), "review.work_item_id")
    did = _text(env.get("dispatch_id"), "review.dispatch_id")
    items = {str(i.get("work_item_id") or ""): i
             for i in dispatch.get("actions") or [] if isinstance(i, dict)}
    item = items.get(wid)
    if not isinstance(item, dict):
        _fail("work_item_unknown", f"当前 dispatch 不含工作项 {wid}。")
    if str(item.get("dispatch_id") or "") != did:
        _fail("dispatch_mismatch",
              f"复核结果 dispatch_id={did} 与工作项 {wid} 的派发 ID 不一致。")
    if str(execution.get("work_item_id") or "") != wid:
        _fail("review_binding_mismatch", "复核工作项与被执行工作项不一致。")
    if str(execution.get("dispatch_id") or "") != did:
        _fail("review_binding_mismatch", "复核 dispatch_id 与执行结果不一致。")
    exec_sha = _sha(execution.get("result_sha256"), "execution.result_sha256")
    bound_sha = _sha(env.get("execution_result_sha256"),
                     "review.execution_result_sha256")
    if bound_sha != exec_sha:
        _fail("review_binding_mismatch",
              "复核绑定的 execution_result_sha256 与已入账执行结果不一致。")
    reviewer = _validate_reviewer(env.get("reviewer"))
    executor = execution.get("executor") or {}
    if not isinstance(executor, dict):
        _fail("schema_invalid", "执行结果缺少 executor 元数据。")
    for key in ("agent_id", "child_session_id"):
        exec_value = str(executor.get(key) or "").strip()
        rev_value = str(reviewer.get(key) or "").strip()
        if exec_value and rev_value and exec_value == rev_value:
            _fail("executor_reviewer_same",
                  f"Reviewer 与执行方的 {key} 相同；独立复核必须使用独立子会话。")
    if str(executor.get("profile_id") or "") == "reviewer":
        _fail("reviewer_as_executor", "执行方不得使用 reviewer Profile。")
    changed = _list(env.get("changed_files") or [], "review.changed_files")
    if changed:
        _fail("review_contaminated",
              "Reviewer 报告了 changed_files；只读复核不得改动工作区。")
    files = _validate_files_inspected(env.get("files_inspected"),
                                      check_files=check_files)
    commands = _validate_commands(env.get("commands_run"))
    findings = _validate_findings(env.get("findings"), check_files=check_files)
    unverified_raw = _list(env.get("unverified") or [], "review.unverified")
    unverified = [str(x).strip() for x in unverified_raw if str(x).strip()]
    risks_raw = _list(env.get("residual_risks") or [], "review.residual_risks")
    residual = [str(x).strip() for x in risks_raw if str(x).strip()]
    verdict = _text(env.get("verdict"), "review.verdict").lower()
    if verdict not in REVIEW_VERDICTS:
        _fail("invalid_verdict",
              f"复核 verdict={verdict!r} 只能是 {', '.join(REVIEW_VERDICTS)}。")
    g4_eligible = env.get("g4_eligible")
    if not isinstance(g4_eligible, bool):
        _fail("schema_invalid", "review.g4_eligible 必须是布尔值。")
    target_sha = ""
    if g4_eligible:
        target_sha = _sha(env.get("target_sha256"), "review.target_sha256")
        if verdict != "pass":
            _fail("g4_requires_pass", "g4_eligible=true 的复核必须 verdict=pass。")
        if unverified:
            _fail("g4_with_unverified", "G4 复核仍有未验证事项；不能标记可用。")
        if any(f["severity"] == "blocking" for f in findings):
            _fail("g4_with_blocking", "G4 复核存在 blocking finding；不能标记可用。")
    host = _validate_host_record(env.get("host"), required=g4_eligible)
    scope, based_on, manifest = _validate_review_scope(
        env, state, contract=contract, work_item_id=wid)
    if verdict == "pass" and any(f["severity"] == "blocking" for f in findings):
        _fail("pass_with_blocking_finding",
              "复核 verdict=pass 但存在 blocking finding；必须改判或补齐。")
    if verdict == "pass" and unverified:
        _fail("pass_with_unverified",
              "复核 verdict=pass 但仍有未验证事项；不得猜测 PASS。")
    declared_sha = _sha(env.get("result_sha256"), "review.result_sha256")
    actual_sha = envelope_sha256(env)
    if declared_sha != actual_sha:
        _fail("result_sha_mismatch",
              "复核结果 result_sha256 与信封内容不一致（可能被篡改）。")
    record = {
        "schema": REVIEW_SCHEMA,
        "task_id": task_id,
        "contract_sha256": contract,
        "plan_id": plan_id,
        "plan_sha256": plan_sha,
        "dispatch_id": did,
        "work_item_id": wid,
        "execution_result_sha256": exec_sha,
        "target_sha256": target_sha,
        "reviewer": reviewer,
        "host": host,
        "verdict": verdict,
        "findings": findings,
        "files_inspected": files,
        "commands_run": commands,
        "changed_files": [],
        "unverified": unverified,
        "residual_risks": residual,
        "review_scope": scope,
        "based_on_review_sha256": based_on,
        "evidence_manifest": manifest,
        # 覆盖快照固化在复核记录上，供后续增量复核计算“哪些证据/工作项/条件
        # 在基线之后发生了变化”，而不是重新猜测历史状态。
        "coverage": review_coverage(state),
        "g4_eligible": g4_eligible,
        "result_sha256": declared_sha,
    }
    declared = [str(item).strip()
                for item in _list(env.get("declared_files_unverified") or [],
                                  "review.declared_files_unverified")
                if str(item).strip()]
    if declared:
        # Reviewer 自报但宿主记录里没有实际检查痕迹的文件：保留在台账里供审计，
        # 不影响 G4 判定（G4 只看宿主派生出的实际访问记录）。
        record["declared_files_unverified"] = declared
    return record

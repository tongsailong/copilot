#!/usr/bin/env python3
"""Agent 调度与权限能力判定（纯函数，不派发子代理、不调用模型）。

本模块只回答两个问题：

1. 宿主 `profileSnapshot` 实际给了这个子代理什么能力？能否作为 G4 只读复核执行者？
2. 当前工作应该由主代理直接做，还是值得派发内置 `Explore` / `general-purpose`？

它**不创建 Agent、不调度、不写状态**；调度决定仍由 ZCode 主代理作出。Copilot 只提供
可核对的判定口径，避免用 Profile 名称、Prompt 声明或“看起来只读”替代宿主策略证据。

判定只看宿主有效工具策略，不看 Profile 名称、来源或路径；内置 Agent 名称只用于诊断
说明与文档对照。ZCode 3.18.2 的内置能力（宿主源码与真实 metadata/DB 记录）：

* `Explore`：`Bash,Glob,Grep,Read,WebFetch,WebSearch,TodoWrite`，`injectAgentsMd=false`；
  Bash 在宿主记录里恒为 `read_only=0`，因此**不是**只读代理，只能用于调查。
* `general-purpose`：`tools=["*"]`，`injectAgentsMd=true`；继承主会话工具（含 MCP）与
  写入能力，不能当作只读复核者。
* 没有按派发覆盖工具的参数，也没有 OS 级只读沙箱；G4 的“精确 Read/Grep/Glob”只能由
  宿主强制 `tools=[Read,Grep,Glob]` 且禁用 `Write/Edit/Bash` 的 Profile 证明。
"""
from __future__ import annotations

import native_bridge as NB

# 宿主版本与内置能力基线。升级 ZCode 后必须用真实 metadata/DB 重新核对，不能假设不变。
HOST_VERSION = "3.18.2"
BUILTIN_AGENTS = {
    "Explore": {
        "tools": ("Bash", "Glob", "Grep", "Read", "WebFetch", "WebSearch",
                  "TodoWrite"),
        "inject_agents_md": False,
        "inherits_mcp": False,
        "note": "只读调查助手；含 Bash，宿主记录 read_only=0，非只读代理。",
    },
    "general-purpose": {
        "tools": ("*",),
        "inject_agents_md": True,
        "inherits_mcp": True,
        "note": "全工具代理；可写、可执行命令并继承 MCP。",
    },
}
# G4 只读工具集（与 native_bridge.REVIEWER_TOOLS 一致）。
READ_ONLY_TOOLS = tuple(NB.REVIEWER_TOOLS)
# 宿主 3.18.2 的写工具集合是 Write/Edit/ApplyPatch/Bash；这里额外并入执行类
# （js*）、代理类（Agent/Task）与旧版编辑工具。集合只用于**逐工具调用**的证据
# 判定；Profile 级“能不能写”走 `_can_write` 的未知工具 fail-closed 口径。
WRITE_CAPABLE_TOOLS = frozenset({
    "Bash", "Write", "Edit", "ApplyPatch", "NotebookEdit", "MultiEdit",
    "Agent", "Task", "js", "js_reset", "js_add_node_module_dir",
})
# 明确只读、即使出现在 Profile 也不构成写能力的工具。Read/Grep/Glob 直接取
# G4 只读集合，避免两处定义漂移；不在本集合内的任何工具（未来新增工具、
# ApplyPatch、js、Agent、Skill）一律按“可能写入”处理。
KNOWN_READ_ONLY_TOOLS = frozenset(
    set(READ_ONLY_TOOLS) | {"WebFetch", "WebSearch", "TodoRead", "TodoWrite"})


def _tool_names(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _can_write(tools: list[str], policy: dict) -> bool:
    """Profile 是否可能写文件/执行命令；未知工具一律视为可能写入。"""
    if "*" in tools:
        return True
    if any(name not in KNOWN_READ_ONLY_TOOLS for name in tools):
        return True
    if policy.get("mcpServers") or policy.get("skills"):
        return True
    return False


def tool_call_is_write(name: str, read_only: int = 0) -> tuple[bool, bool]:
    """逐调用判定 (是否写能力, 是否无法分类)。

    `read_only=1` 为宿主明确记录的只读调用；`0` 为写/执行；其它值（含 NULL
    归一化的 -1）表示宿主未分类，交给调用方作为告警而不是静默放行。
    """
    tool = str(name or "")
    if tool in WRITE_CAPABLE_TOOLS or tool.startswith("mcp__"):
        return True, False
    if tool in KNOWN_READ_ONLY_TOOLS:
        return False, False
    if read_only == 0:
        return True, False
    return False, True


def _base_classification(codes: list[str] | None = None,
                         reasons: list[str] | None = None,
                         remedy: str = "") -> dict:
    base_codes = codes or ["profile_snapshot_missing"]
    base_reasons = reasons or ["缺少宿主 profileSnapshot，无法证明实际权限"]
    return {
        "name": "",
        "source": "",
        "path": "",
        "tools": [],
        "disallowed_tools": [],
        "permission_mode": "",
        "role": "unknown",
        "can_write": False,
        "g4_executor": False,
        "codes": base_codes,
        "reasons": base_reasons,
        "remedy": remedy or ("没有宿主有效权限策略时保持 fail-closed；不得用 Profile "
                             "名称或 Prompt 声明代替权限证据。"),
        "builtin": "",
    }


def _remedy_for_codes(codes: list[str]) -> str:
    """按偏离项给出可执行的补救说明，避免“工具集不精确”掩盖真实原因。"""
    remedies = {
        "profile_tools_empty": "Profile 的 tools=[] 在宿主语义里等于继承父工具，"
                               "必须写精确工具集。",
        "profile_tools_not_exact": "tools 必须精确等于 Read/Grep/Glob，"
                                   "多一个工具都不能算只读复核资格。",
        "profile_disallowed_missing": "disallowedTools 必须显式包含 Write/Edit/Bash。",
        "profile_permission_mode": "permissionMode 只能是 default/plan 或留空。",
        "profile_skills_not_allowed": "Profile 启用了 skills，G4 要求无 skills。",
        "profile_mcp_servers": "Profile 启用了 MCP，G4 要求无 MCP。",
    }
    parts = [remedies.get(code, code) for code in codes]
    if not parts:
        parts = ["当前 Profile 不满足 G4 只读能力。"]
    return ("；".join(parts) + " 保持 fail-closed，不要用名称或 Prompt 声明"
            "替代宿主策略。")


def classify_profile(snapshot: object) -> dict:
    """把宿主 `profileSnapshot` 归类为可执行/只读/未知，并给出 G4 判定。

    返回的 `g4_executor` 与 `native_bridge.reviewer_policy_problems` 完全一致：
    `tools` 必须精确等于 `Read/Grep/Glob`，显式禁用 `Write/Edit/Bash`，无 skills/MCP；
    名称、来源、路径只作记录事实。
    """
    if not isinstance(snapshot, dict) or not snapshot:
        return _base_classification()
    if "tools" not in snapshot:
        return _base_classification(
            ["profile_tools_missing"],
            ["宿主 profileSnapshot 缺少 tools 字段，无法证明实际权限"],
            "Profile 快照没有 tools 字段时视为无有效权限策略；重新派发并读取宿主 "
            "metadata 的完整 profileSnapshot。")
    policy = NB.normalize_profile_policy(snapshot)
    tools = _tool_names(policy.get("tools"))
    problems = NB.reviewer_policy_problems(policy)
    codes = [code for code, _detail in problems]
    reasons = [f"{code}: {detail}" for code, detail in problems]
    can_write = _can_write(tools, policy)
    name = str(policy.get("name") or "").strip()
    source = str(policy.get("source") or "").strip()
    builtin = BUILTIN_AGENTS.get(name) if source in ("built-in", "") else None

    if not problems:
        role = "read_only"
    elif "*" in tools:
        role = "unrestricted"
    elif can_write:
        role = "write_capable"
    elif tools:
        # 工具都只读但策略仍不满足复核（permissionMode/skills/禁用项）：可读，
        # 但不能算“只读复核资格”。
        role = "read_only_conditional"
    else:
        role = "unknown"

    if not problems:
        codes = ["g4_ready"]
        reasons = ["宿主有效策略精确为 Read/Grep/Glob，并禁用 Write/Edit/Bash"]
        remedy = ("满足 G4 只读能力要求；仍需 review attempt、宿主记录与原始输出"
                  "绑定，正式资格由 g4_attest 决定。")
    elif builtin is not None:
        if name == "Explore":
            codes.append("g4_tools_include_bash")
            reasons.append("内置 Explore 含 Bash：可写文件/执行命令，宿主记录恒为 "
                           "read_only=0")
            remedy = ("内置 Explore 含 Bash（可写文件/执行命令，宿主记录恒为 "
                      "read_only=0），不是只读代理，只能用于调查；不得作为 G4 "
                      "执行者。宿主没有按派发覆盖工具的参数，必须保持 fail-closed。")
        else:
            codes.append("g4_tools_unrestricted")
            reasons.append("内置 general-purpose 为 tools=[*]，可写、可执行并继承 MCP")
            remedy = ("内置 general-purpose 不能当作只读复核者；需要独立复核时使用"
                      "宿主强制只读的能力载体，否则如实报告 G4 能力缺口。")
    elif "*" in tools:
        codes.append("g4_tools_unrestricted")
        remedy = ("该 Profile 为全工具集，不能证明只读；G4 需要精确 "
                  "Read/Grep/Glob 且禁用 Write/Edit/Bash。")
    elif can_write:
        codes.append("g4_tools_write_capable")
        remedy = ("该 Profile 含写入/执行/MCP 能力，不能证明只读；G4 需要精确 "
                  "Read/Grep/Glob 且禁用 Write/Edit/Bash。")
    else:
        remedy = _remedy_for_codes(codes)

    return {
        "name": name,
        "source": source,
        "path": str(policy.get("path") or "").strip(),
        "tools": tools,
        "disallowed_tools": _tool_names(policy.get("disallowedTools")),
        "permission_mode": str(policy.get("permissionMode") or "").strip(),
        "role": role,
        "can_write": can_write,
        "g4_executor": not problems,
        "codes": codes,
        "reasons": reasons,
        "remedy": remedy,
        "builtin": name if builtin is not None else "",
    }


def capability_report(metadata: object, metadata_path: str = "") -> dict:
    """从宿主 metadata 生成只读能力报告（不派发、不改状态）。"""
    data = metadata if isinstance(metadata, dict) else {}
    snapshot = data.get("profileSnapshot")
    classification = (classify_profile(snapshot) if isinstance(snapshot, dict)
                      else _base_classification())
    profile_id = str(data.get("profileId") or "").strip()
    snapshot_name = str(classification.get("name") or "").strip()
    warnings: list[str] = []
    if not isinstance(snapshot, dict):
        warnings.append("profile_snapshot_missing")
    elif "tools" not in snapshot:
        warnings.append("profile_tools_missing")
    elif profile_id and snapshot_name and profile_id != snapshot_name:
        warnings.append("profile_id_mismatch")
    if isinstance(snapshot, dict):
        mode = snapshot.get("permissionMode")
        if isinstance(mode, str) and mode != mode.strip():
            warnings.append("permission_mode_whitespace")
    status = str(data.get("status") or "").strip()
    if status and status != "completed":
        warnings.append("agent_not_completed")
    return {
        "schema": "copilot-agent-capability/v1",
        "host_version": HOST_VERSION,
        "metadata_path": str(metadata_path or ""),
        "agent_id": str(data.get("agentId") or "").strip(),
        "child_session_id": str(data.get("childSessionId") or "").strip(),
        "profile_id": profile_id,
        "status": status,
        "classification": classification,
        "warnings": warnings,
        "g4": {
            "executor": bool(classification.get("g4_executor")),
            "precheck_only": True,
            "codes": list(classification.get("codes") or []),
            "remedy": str(classification.get("remedy") or ""),
        },
        "note": ("只读诊断：本报告不代表 G4 已通过。`g4.executor` 只是宿主 Profile "
                 "策略预检；Profile 文件存在性/正文一致性、会话与 prompt 绑定、"
                 "工具调用与原始输出仍由 g4_attest 独立核验。"),
    }


def dispatch_advice(*, needs_independent_context: bool = False,
                    parallelizable: bool = False,
                    independent_deliverable: bool = False,
                    write_conflict: bool = False,
                    saves_time: bool = False,
                    research_only: bool = False,
                    subagent_unavailable: bool = False) -> dict:
    """按五问给出调度建议：主代理默认执行，子代理按需。

    参数对应五问：1 独立上下文 → `needs_independent_context`；2 真正并行 →
    `parallelizable`；3 独立交付物 → `independent_deliverable`；4 总时间收益 →
    `saves_time`；5 是否值得多一次调用与核对 → `saves_time`（并受
    `write_conflict`/`subagent_unavailable` 直接否决）。

    判定顺序（先否决、后选择）：子代理不可用 → 主代理；写入冲突 → 主代理；
    只读调查 → 内置 `Explore`（绝不交给全工具代理）；独立交付物 → 内置
    `general-purpose`（需并发且有收益，否则主代理）；其余 → 主代理。

    这是**建议**，不是调度器：不创建任务、不派发 Agent、不设调用比例或预算。
    主代理可以结合现场证据否决，但否决理由应是不值得独立上下文，而不是为了
    “体现 Agent 协作”。
    """
    if subagent_unavailable:
        return {
            "executor": "main",
            "reason": "子代理不可用/权限不满足；主代理继续可直接完成的工作，不原样重派",
            "prompt_requirements": [],
        }
    if write_conflict:
        # 两类子代理都可能写（Explore 含 Bash，general-purpose 为全工具），
        # 与主代理写入范围冲突时并行集成成本高于收益。
        return {
            "executor": "main",
            "reason": "写入范围与主代理冲突，subagent 可能写入同一批文件，并行收益被集成成本抵消",
            "prompt_requirements": [],
        }
    if research_only:
        # 只读调查不能交给全工具/MCP 代理；Explore 只调查（其 Bash 用于读取型命令）。
        return {
            "executor": "Explore",
            "reason": "只读调查：不修改工程文件，交由内置 Explore 在独立上下文完成",
            "prompt_requirements": [
                "调查目标、文件范围与需要回答的问题",
                "必要的项目背景与规则（内置 Explore 不注入 AGENTS.md）",
                "期望证据：真实文件路径、行号、命令输出或代码片段",
                "只调查不修改；无法确认时如实说明",
            ],
        }
    if independent_deliverable:
        if not parallelizable or not saves_time:
            return {
                "executor": "main",
                "reason": "没有可并行的独立交付物，或并行收益不足以抵消上下文与集成成本",
                "prompt_requirements": [],
            }
        return {
            "executor": "general-purpose",
            "reason": "存在边界明确、可独立交付且并行收益明显的工作",
            "prompt_requirements": [
                "明确工作范围、文件边界与完成条件",
                "确认与主代理正在修改的文件没有不可控冲突",
                "要求返回改动文件、测试结果与原始证据",
                "主代理负责集成、最终测试与验收；不把交付判断交给子代理",
            ],
        }
    if needs_independent_context:
        return {
            "executor": "Explore",
            "reason": "需要独立上下文的只读调查，不需要修改工程文件",
            "prompt_requirements": [
                "调查目标、文件范围与需要回答的问题",
                "必要的项目背景与规则（内置 Explore 不注入 AGENTS.md）",
                "期望证据：真实文件路径、行号、命令输出或代码片段",
                "只调查不修改；无法确认时如实说明",
            ],
        }
    return {
        "executor": "main",
        "reason": "主代理直接执行更快；没有独立上下文或并行收益",
        "prompt_requirements": [],
    }


__all__ = [
    "HOST_VERSION",
    "BUILTIN_AGENTS",
    "READ_ONLY_TOOLS",
    "KNOWN_READ_ONLY_TOOLS",
    "WRITE_CAPABLE_TOOLS",
    "classify_profile",
    "capability_report",
    "dispatch_advice",
    "tool_call_is_write",
]

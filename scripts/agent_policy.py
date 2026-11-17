#!/usr/bin/env python3
"""Agent 调度建议（纯函数，不派发子代理、不调用模型、不写状态）。

本模块只回答一个问题：当前工作应该由主代理直接做，还是值得派发内置
`Explore` / `general-purpose`？调度决定仍由 ZCode 主代理作出；Copilot 只提供
可核对的判定口径。

ZCode 3.18.2 的内置能力（宿主源码与真实 metadata/DB 记录）：

* `Explore`：`Bash,Glob,Grep,Read,WebFetch,WebSearch,TodoWrite`，
  `injectAgentsMd=false`；Bash 在宿主记录里恒为 `read_only=0`，只能用于调查。
* `general-purpose`：`tools=["*"]`，`injectAgentsMd=true`；继承主会话工具（含 MCP）
  与写入能力。

v8 起 G4 原生只读复核已退役：本模块不再判定 Profile 是否满足 G4 只读载体。
"""
from __future__ import annotations

# 宿主版本与内置能力基线。升级 ZCode 后必须用真实 metadata/DB 重新核对，不能假设不变。
HOST_VERSION = "3.18.2"
BUILTIN_AGENTS = {
    "Explore": {
        "tools": ("Bash", "Glob", "Grep", "Read", "WebFetch", "WebSearch",
                  "TodoWrite"),
        "inject_agents_md": False,
        "inherits_mcp": False,
        "note": "只读调查助手；含 Bash，宿主记录 read_only=0，非只读沙箱。",
    },
    "general-purpose": {
        "tools": ("*",),
        "inject_agents_md": True,
        "inherits_mcp": True,
        "note": "全工具代理；可写、可执行命令并继承 MCP。",
    },
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
    "dispatch_advice",
]

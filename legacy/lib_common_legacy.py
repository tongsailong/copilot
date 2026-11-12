#!/usr/bin/env python3
"""v5 SOL 通道专用的凭据解析（已退役，仅历史实现与回归测试使用）。

v6 活动脚本不读取任何 Provider 配置或 apiKey；这些函数只在
`legacy/call_gpt.py` 的历史调用链与 `legacy/tests` 的回归断言中使用。
不得从 `scripts/` 下的活动模块导入本文件。
"""
from __future__ import annotations

import json
import os

# 凭据解析链（按序尝试，只返回值，绝不回显、绝不进 argv）：
#   1. 环境变量（调用方传入的名字，如 CUSTOM_127_API_KEY）
#   2. Reasonix 全局 .env（迁移兼容：本机两套宿主共存）
#   3. ZCode 原生 provider 配置：取指向本机 CPA（baseUrl 含 8317 或规则体含 cpa）
#      的规则的 apiKey 字段。
ENV_FILE = os.path.expanduser("~/.reasonix/.env")
ZCODE_PROVIDER_FILE = os.path.expanduser("~/.zcode/v2/provider_config.json")


def read_zcode_provider_key(config_path: str | None = None) -> str | None:
    """从 ZCode provider 配置解析本机 CPA 网关的 apiKey；找不到返回 None。"""
    path = config_path or ZCODE_PROVIDER_FILE
    try:
        with open(path, "r", encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, ValueError):
        return None
    cfg = config.get("config") if isinstance(config, dict) else None
    rules = ((cfg or {}).get("providerConfigRules") or {}).get("providerRules") or []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule_config = rule.get("config") or {}
        base_url = str((rule_config.get("api") or {}).get("baseUrl") or "")
        if "8317" not in base_url and "cpa" not in json.dumps(rule, ensure_ascii=False).lower():
            continue
        key = str((rule_config.get("access") or {}).get("apiKey") or "").strip()
        if key:
            return key
    return None


def read_env_value(path: str, key: str) -> str | None:
    """从 KEY=VALUE 形式的 .env 读取值；缺失或格式不符返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    v = v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                        v = v[1:-1]
                    return v or None
    except FileNotFoundError:
        return None
    return None


def resolve_api_key(env_name: str) -> str | None:
    """环境变量 → Reasonix .env → ZCode provider 配置。只返回值，由调用方决定如何失败。"""
    return (os.environ.get(env_name)
            or read_env_value(ENV_FILE, env_name)
            or read_zcode_provider_key())

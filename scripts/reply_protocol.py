#!/usr/bin/env python3
"""copilot-plan 回复协议兼容层。

模型输出可能把完成条件写成 ``id`` / ``evidence`` / ``evidence_ids``，而状态层只认
``criterion_id`` / ``evidence_refs``。本模块把这类**同义字段**规范化成宿主契约，
同时保留原始字段，供审计追溯；它不推断任何业务判断，也不改写回复正文。

约束：

* 纯函数：不读文件、不写状态、不联网；同一输入重复调用结果一致（幂等）。
* 只做无歧义字段映射；规范字段与旧字段同时存在且值冲突时失败关闭。
* 只接受 ``pending`` / ``met`` / ``not_met``，``met`` 必须带非空证据引用。
* 未知字段原样保留，不能被规范化层静默丢弃。

本模块不依赖 ``copilot_dispatch``，避免循环导入；完成条件/证据 id 的语法与
``copilot_dispatch`` 保持一致，并由测试锁定。
"""
from __future__ import annotations

import copy
import re

# 规范化器版本必须随兼容规则变化而递增；恢复审计会记录该值。
# v2：登记 evidence_ids（历史回复实际使用过的第三个证据字段名）。
NORMALIZER_VERSION = "reply-protocol/v2"

CRITERION_ID_RE = re.compile(r"^C[1-9][0-9]{0,2}$")
EVIDENCE_ID_RE = re.compile(r"^E[1-9][0-9]{0,3}$")
CRITERION_STATUSES = ("pending", "met", "not_met")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# 规范字段 -> 旧字段元组。只登记已有真实输出证据的同义词，不做猜测性扩展。
# 别名顺序只影响审计里记录的 from 名称，不影响取值优先级：多个别名同时出现时
# 必须等值，否则按冲突拒绝。
FIELD_ALIASES = (
    ("criterion_id", ("id",)),
    ("evidence_refs", ("evidence", "evidence_ids")),
)
_ALIASES = dict(FIELD_ALIASES)

_MISSING = object()


class ProtocolError(ValueError):
    """结构化回复不符合协议；``code`` 供调用方稳定分类。"""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise ProtocolError(code, detail)


def _check_id(value: object, *, path: str, kind: str, pattern: re.Pattern) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("field_type_invalid", f"{path} 必须是 {kind} 字符串。")
    value = value.strip()
    if not pattern.match(value):
        _fail("field_value_invalid", f"{path} 的 {kind} 值非法：{value!r}。")
    return value


def _validate_refs(value: object, *, path: str) -> list:
    if not isinstance(value, list):
        _fail("field_type_invalid", f"{path} 必须是数组。")
    out = []
    for index, ref in enumerate(value):
        ref_path = f"{path}[{index}]"
        if isinstance(ref, str):
            rid = _check_id(ref, path=ref_path, kind="证据 id",
                           pattern=EVIDENCE_ID_RE)
            out.append(rid)
            continue
        if not isinstance(ref, dict):
            _fail("field_type_invalid",
                  f"{ref_path} 必须是证据 id 字符串或对象。")
        rid = _check_id(ref.get("id"), path=ref_path + ".id", kind="证据 id",
                        pattern=EVIDENCE_ID_RE)
        given = ref.get("sha256", "")
        if given is not None and not isinstance(given, str):
            _fail("field_type_invalid", f"{ref_path}.sha256 必须是字符串。")
        given = str(given or "").strip()
        if given and not _SHA256_RE.match(given):
            _fail("field_value_invalid",
                  f"{ref_path}.sha256 必须是 64 位十六进制摘要。")
        out.append(dict(ref, id=rid))
    return out


def _resolve(item: dict, canonical: str, aliases: tuple[str, ...], *,
             index: int, mapping: list[dict]) -> object:
    """取规范字段的值；只有旧字段时映射，任一字段冲突时失败关闭。

    ``aliases`` 来自 ``FIELD_ALIASES``，是唯一权威来源。多个别名同时出现时必须
    等值；不等值时拒绝猜测，避免把两份不同证据引用静默合并。
    """
    canonical_value = item.get(canonical, _MISSING)
    present = [(alias, item[alias]) for alias in aliases if alias in item]

    if canonical_value is not _MISSING:
        for alias, alias_value in present:
            if alias_value != canonical_value:
                _fail(
                    "field_conflict",
                    f"criteria[{index}] 的 {canonical}={canonical_value!r} 与 "
                    f"{alias}={alias_value!r} 冲突；拒绝猜测哪一个为准。")
        return canonical_value

    if not present:
        return _MISSING

    first_alias, first_value = present[0]
    for alias, alias_value in present[1:]:
        if alias_value != first_value:
            _fail(
                "field_conflict",
                f"criteria[{index}] 的 {first_alias}={first_value!r} 与 "
                f"{alias}={alias_value!r} 冲突；拒绝猜测哪一个为准。")
    item[canonical] = copy.deepcopy(first_value)
    mapping.append({
        "criterion_index": index,
        "from": first_alias,
        "to": canonical,
        "value": copy.deepcopy(first_value),
    })
    return item[canonical]


def normalize_plan(plan: dict) -> dict:
    """返回 ``{plan, mapping, version}``；不修改入参。

    ``plan`` 是深拷贝后的规范化结果，原始字段（``id`` / ``evidence``）保留。
    ``mapping`` 只记录**实际发生**的旧字段到规范字段映射，便于恢复审计。
    """
    if not isinstance(plan, dict):
        _fail("plan_not_object", "copilot-plan 必须是 JSON 对象。")
    out = copy.deepcopy(plan)
    mapping: list[dict] = []

    criteria = out.get("criteria", _MISSING)
    # 旧实现把显式 null 视同「未给 criteria」；兼容层保持这一行为，避免把
    # 原本可解析的回复变成解析失败。只有给错类型才失败关闭。
    if criteria is _MISSING or criteria is None:
        return {"plan": out, "mapping": mapping, "version": NORMALIZER_VERSION}
    if not isinstance(criteria, list):
        _fail("field_type_invalid", "criteria 必须是数组。")

    for index, item in enumerate(criteria):
        path = f"criteria[{index}]"
        if not isinstance(item, dict):
            _fail("field_type_invalid", f"{path} 必须是对象。")

        raw_id = _resolve(item, "criterion_id", _ALIASES["criterion_id"],
                          index=index, mapping=mapping)
        if raw_id is _MISSING:
            _fail("field_missing", f"{path} 缺少 criterion_id（兼容旧字段 id）。")
        cid = _check_id(raw_id, path=path + ".criterion_id",
                        kind="完成条件 id", pattern=CRITERION_ID_RE)
        if item.get("criterion_id") is not _MISSING:
            item["criterion_id"] = cid

        status = item.get("status", _MISSING)
        if status is _MISSING:
            _fail("field_missing", f"{path} 缺少 status。")
        if not isinstance(status, str) or status.strip().lower() not in CRITERION_STATUSES:
            _fail("field_value_invalid",
                  f"{path}.status 非法：{status!r}；只能是 "
                  + " / ".join(CRITERION_STATUSES) + "。")
        item["status"] = status.strip().lower()

        refs = _resolve(item, "evidence_refs", _ALIASES["evidence_refs"],
                        index=index, mapping=mapping)
        if refs is _MISSING:
            if item["status"] == "met":
                _fail("field_missing",
                      f"{path} 判定 met 但没有 evidence_refs（兼容旧字段 "
                      "evidence / evidence_ids）。")
            refs = []
        validated = _validate_refs(refs, path=path + ".evidence_refs")
        item["evidence_refs"] = validated

    return {"plan": out, "mapping": mapping, "version": NORMALIZER_VERSION}

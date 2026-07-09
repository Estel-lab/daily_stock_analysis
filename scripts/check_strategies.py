#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""strategies/*.yaml 策略技能定义校验。

校验内容：
1. YAML 可解析，必填字段（name/display_name/description/instructions）非空
2. category 取值合法（trend/pattern/reversal/framework）
3. name 全局唯一、符合 ^[a-z][a-z0-9_]*$、与文件名一致
4. name/display_name/aliases 相互之间全局无冲突（否则 NL 选择器无法唯一路由）
5. market_regimes 取值合法（与 src/agent/skills/router.py 的 _detect_regime 保持一致）
6. required_tools 必须是 src/agent/tools/*.py 中实际注册的工具名（静态扫描）
7. core_rules 取值 1-7；default_priority 为整数；default_active/default_router 为布尔

用法：
    python scripts/check_strategies.py            # 校验并输出结果，有错误时 exit 1
仅使用 PyYAML 与标准库，可在最小环境运行。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STRATEGIES_DIR = PROJECT_ROOT / "strategies"
TOOLS_DIR = PROJECT_ROOT / "src" / "agent" / "tools"

VALID_CATEGORIES = {"trend", "pattern", "reversal", "framework"}
# 与 src/agent/skills/router.py::_detect_regime 的返回值保持一致
VALID_REGIMES = {"trending_up", "trending_down", "sideways", "volatile", "sector_hot"}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
REQUIRED_FIELDS = ("name", "display_name", "description", "instructions")

# 匹配 ToolDefinition(name="...") 与 @tool(name="...") 两种注册写法
_TOOL_NAME_RE = re.compile(
    r"""(?:ToolDefinition\(|@?tool\()\s*\n?\s*name\s*=\s*["']([a-z0-9_]+)["']"""
)


def scan_registered_tools() -> set:
    """静态扫描 agent 工具注册名，避免 CI 校验依赖完整运行时。"""
    names: set = set()
    for py_file in TOOLS_DIR.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        names.update(_TOOL_NAME_RE.findall(text))
    return names


def check_file(path: Path, tools: set, errors: list) -> dict | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        errors.append(f"{path.name}: YAML 解析失败: {exc}")
        return None
    if not isinstance(data, dict):
        errors.append(f"{path.name}: 顶层必须是映射，实际为 {type(data).__name__}")
        return None

    for field in REQUIRED_FIELDS:
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{path.name}: 必填字段 `{field}` 缺失或为空")

    name = str(data.get("name") or "")
    if name and not NAME_RE.match(name):
        errors.append(f"{path.name}: name `{name}` 不符合 ^[a-z][a-z0-9_]*$")
    if name and path.stem != name:
        errors.append(f"{path.name}: 文件名应与 name `{name}` 一致")

    category = data.get("category", "trend")
    if category not in VALID_CATEGORIES:
        errors.append(f"{path.name}: category `{category}` 不在 {sorted(VALID_CATEGORIES)}")

    regimes = data.get("market_regimes") or []
    if not isinstance(regimes, list):
        errors.append(f"{path.name}: market_regimes 必须是列表")
    else:
        for regime in regimes:
            if regime not in VALID_REGIMES:
                errors.append(
                    f"{path.name}: market_regimes 含未知值 `{regime}`"
                    f"（合法值：{sorted(VALID_REGIMES)}）"
                )

    required_tools = data.get("required_tools") or []
    if not isinstance(required_tools, list):
        errors.append(f"{path.name}: required_tools 必须是列表")
    else:
        for tool_name in required_tools:
            if tool_name not in tools:
                errors.append(
                    f"{path.name}: required_tools 引用了未注册的工具 `{tool_name}`"
                )

    core_rules = data.get("core_rules") or []
    if not isinstance(core_rules, list) or any(
        not isinstance(rule, int) or not 1 <= rule <= 7 for rule in core_rules
    ):
        errors.append(f"{path.name}: core_rules 必须是 1-7 的整数列表")

    priority = data.get("default_priority", 100)
    if not isinstance(priority, int):
        errors.append(f"{path.name}: default_priority 必须是整数")
    for flag in ("default_active", "default_router"):
        if flag in data and not isinstance(data[flag], bool):
            errors.append(f"{path.name}: {flag} 必须是布尔值")

    aliases = data.get("aliases") or []
    if not isinstance(aliases, list):
        errors.append(f"{path.name}: aliases 必须是列表")
        aliases = []
    return {
        "file": path.name,
        "name": name,
        "display_name": str(data.get("display_name") or ""),
        "aliases": [str(a) for a in aliases],
    }


def check_global_uniqueness(entries: list, errors: list) -> None:
    """name/display_name/alias 三类标识符全局不得互相冲突。"""
    seen: dict = {}
    for entry in entries:
        identifiers = (
            [("name", entry["name"]), ("display_name", entry["display_name"])]
            + [("alias", alias) for alias in entry["aliases"]]
        )
        for kind, ident in identifiers:
            key = ident.strip().lower()
            if not key:
                continue
            if key in seen and seen[key][0] != entry["file"]:
                other_file, other_kind = seen[key]
                errors.append(
                    f"{entry['file']}: {kind} `{ident}` 与 {other_file} 的 {other_kind} 冲突"
                )
            else:
                seen[key] = (entry["file"], kind)


def main() -> int:
    yaml_files = sorted(STRATEGIES_DIR.glob("*.yaml"))
    if not yaml_files:
        print(f"未在 {STRATEGIES_DIR} 找到任何策略 YAML")
        return 1

    tools = scan_registered_tools()
    if not tools:
        print(f"警告：未能从 {TOOLS_DIR} 扫描到任何注册工具，跳过 required_tools 校验")

    errors: list = []
    entries: list = []
    for path in yaml_files:
        entry = check_file(path, tools or set(), errors)
        if entry:
            entries.append(entry)
    if tools:
        check_global_uniqueness(entries, errors)

    print(f"已校验 {len(yaml_files)} 个策略文件（注册工具 {len(tools)} 个）")
    if errors:
        print(f"\n发现 {len(errors)} 个问题：")
        for err in errors:
            print(f"  ✗ {err}")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())

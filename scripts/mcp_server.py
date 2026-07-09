# -*- coding: utf-8 -*-
"""DSA MCP Server：把 DSA 的行情/分析/搜索工具暴露为 MCP 工具。

让 Claude Code / Claude Desktop 等 MCP 客户端直接消费 DSA 的数据层
（多源 fallback 的行情、K 线、技术指标、新闻检索、板块排行、回测摘要），
供 UZI-Skill、ai-berkshire 等 agent 工作流取数，替代各自维护爬虫。

工具集与 Agent 问股共用同一注册表（src/agent/factory.get_tool_registry），
本脚本只做 MCP 协议包装，不新增数据实现。全部工具均为只读查询。

依赖（可选，不装不影响 DSA 主流程）：
    pip install mcp

接入 Claude Code：
    claude mcp add dsa -- python /path/to/daily_stock_analysis/scripts/mcp_server.py

环境变量：
- DSA_MCP_TOOLS: 逗号分隔的工具名白名单（留空暴露全部注册工具）
- 数据源相关配置与 DSA 本体一致（.env / 环境变量）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - 依赖提示路径
    print("缺少 mcp SDK，请先安装：pip install mcp", file=sys.stderr)
    sys.exit(1)


def build_server() -> FastMCP:
    """从 DSA 工具注册表构建 MCP server。"""
    from src.agent.factory import get_tool_registry

    registry = get_tool_registry()
    allowlist = {
        name.strip()
        for name in os.getenv("DSA_MCP_TOOLS", "").split(",")
        if name.strip()
    }

    server = FastMCP(
        "dsa",
        instructions=(
            "DSA 股票数据与分析工具（A股/港股/美股，多数据源自动 fallback）。"
            "行情与新闻为只读查询；输出仅供研究参考，不构成投资建议。"
        ),
    )
    exposed = 0
    for tool_def in registry.list_tools():
        if allowlist and tool_def.name not in allowlist:
            continue
        server.add_tool(
            tool_def.handler,
            name=tool_def.name,
            description=tool_def.description,
        )
        exposed += 1
    print(f"DSA MCP server：暴露 {exposed} 个工具", file=sys.stderr)
    return server


if __name__ == "__main__":
    build_server().run()

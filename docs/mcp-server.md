# DSA MCP Server

把 DSA 的行情 / 分析 / 搜索工具通过 [MCP](https://modelcontextprotocol.io) 协议暴露给
Claude Code、Claude Desktop 等 MCP 客户端，让外部 agent 工作流（如 UZI-Skill、
ai-berkshire 研究框架）直接消费 DSA 的多源 fallback 数据层，无需各自维护爬虫。

## 特点

- **零平行实现**：工具集与 Agent 问股共用同一注册表（`src/agent/factory.get_tool_registry`），
  新增 agent 工具自动出现在 MCP 端；
- **只读**：全部工具均为查询类（行情、K 线、技术指标、基本面、新闻、板块排行、
  回测摘要、组合快照），不暴露任何写操作；
- **可选依赖**：`mcp` SDK 未加入 `requirements.txt`，不安装不影响 DSA 主流程。

## 使用

```bash
pip install mcp

# 接入 Claude Code
claude mcp add dsa -- python /path/to/daily_stock_analysis/scripts/mcp_server.py

# 手动运行（stdio 传输）
python scripts/mcp_server.py
```

数据源配置与 DSA 本体一致（`.env` / 环境变量）；未配置 token 型数据源时
使用内置免费源。

## 配置

| 环境变量 | 说明 |
| --- | --- |
| `DSA_MCP_TOOLS` | 逗号分隔的工具名白名单；留空暴露全部注册工具（当前 18 个） |

## 工具清单（节选）

`get_realtime_quote`、`get_daily_history`、`get_stock_info`、`get_chip_distribution`、
`analyze_trend`、`search_stock_news`、`get_sector_rankings`、`get_analysis_context`、
`get_skill_backtest_summary`、`get_portfolio_snapshot` 等。完整列表以启动日志为准。

> 输出仅供研究参考，不构成投资建议。

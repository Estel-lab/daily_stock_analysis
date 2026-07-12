# 建议质量闭环：AI 模拟盘 / 建议校准 / 多空对抗

本专题覆盖三个围绕「AI 建议到底可不可信」的能力：

1. **AI 模拟盘**：把每日分析建议确定性重放成虚拟组合，用净值曲线度量建议的择时价值；
2. **建议校准报告**：把回测结果按市场 / 建议 / 模型分层统计胜率，并反馈进个股分析 Prompt；
3. **多空对抗分析**：对同一只股票生成独立的多头论证与空头论证，由裁判 LLM 输出裁决与置信度。

三者均为 fail-open 设计：数据不足、依赖不可用或配置关闭时静默跳过，不影响主分析流程。

## AI 模拟盘（Paper Trading）

- 源码：`src/services/paper_trading_service.py`
- 输出：投研周报（`scripts/weekly_report.py`，每周一推送）中的「🧪 AI 模拟盘」段

### 语义

- **无持久化状态**：每次从分析历史（`analysis_history`）+ 本地日线库（`stock_daily`）
  重新重放，天然适配 GitHub Actions 等临时环境（历史数据库依赖每日任务的缓存链）。
- **目标状态映射**：与回测引擎 `BacktestEngine.infer_position_recommendation` 完全一致——
  偏多类建议（买入/加仓/持有）→ 持有（long），偏空/观望类建议（卖出/减仓/观望/回避）→ 空仓（cash）。
- **成交假设**：信号当日收盘价成交（与回测 anchor 语义一致），双边收取固定比例手续费；
  信号日无该股行情时顺延到下一根可用日线成交；资金不足的买入信号直接放弃（不远期挂单）。
- **基准**：同一标的池「等权买入持有」（每票自首个信号日起持有到期末），对照策略净值
  可以直接看出择时建议是增强还是拖累。

### 配置（`.env` 可选项）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `PAPER_TRADING_ENABLED` | `true` | 关闭后周报不再输出模拟盘段 |
| `PAPER_TRADING_INITIAL_CAPITAL` | `100000` | 初始资金 |
| `PAPER_TRADING_POSITION_PCT` | `10` | 单票目标仓位（占当前净值 %） |
| `PAPER_TRADING_COMMISSION_PCT` | `0.1` | 单边手续费率（%） |
| `PAPER_TRADING_LOOKBACK_DAYS` | `365` | 回放窗口（天） |

### 边界

- 模拟盘只消费已入库的分析历史与日线；历史库为空（如 Actions 缓存丢失）时该段自动省略。
- 净值为「按建议机械执行」的理论值，未包含滑点、涨跌停无法成交、最小交易单位等约束，
  仅用于度量建议质量，不构成投资建议。

## 建议校准报告（分层胜率）

- 源码：`src/services/calibration_service.py`
- 输出：
  - 投研周报「🎯 建议校准报告」段：整体 + 分市场 / 分建议 / 分模型胜率；
  - 个股分析 Prompt「📈 历史校准」提示（`signal_review_service.build_calibration_hint`）
    在原有整体/本股胜率基础上，追加该股所属市场层与偏多类建议层的胜率。

### 语义

- 数据源为已完成的回测结果（`backtest_results`，`eval_status=completed`），只读不触发回测；
- 模型维度取自 `analysis_history.raw_result` 中的 `model_used` 字段；
- 胜率按 `win / (win + loss)` 计算（neutral 不参与分母）；
- 完成样本数 `< 5` 的分层不展示、不注入 Prompt，避免小样本噪声误导模型。

## 多空对抗分析（Bull vs Bear Debate）

- 源码：`src/services/debate_service.py`，接入点为个股分析 pipeline Step 7.8
  （决策护栏之后、历史保存之前，`src/core/pipeline.py`）
- 输出：追加在报告「综合分析摘要」末尾的紧凑 Markdown 段，并写入
  `dashboard["debate"]` 结构化字段（随 `raw_result` 持久化）

### 语义

- **opt-in**：默认关闭，`DEBATE_ANALYSIS_ENABLED=true` 开启；每股额外消耗 3 次 LLM 调用
  （多头论证、空头论证、裁判裁决），LLM 优先走 Agent 同款 LiteLLM 多渠道路由；
  未配置 LiteLLM 渠道但配置了 generation-only 本地 CLI 后端
  （`GENERATION_BACKEND=claude_code_cli` / `opencode_cli`）时，自动回退到该后端
  执行纯文本辩论调用（辩论不需要 tool-calling）；
- **只追加观点**：不修改评分、操作建议与决策字段，不与 phase / 大盘环境护栏冲突；
- 裁决输出 `verdict`（bull/bear/neutral）与 `confidence`（0-100），
  裁判 JSON 解析失败、任一环节超时或 LLM 不可用时整段静默跳过。

### 配置（`.env` 可选项）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `DEBATE_ANALYSIS_ENABLED` | `false` | 开启多空对抗分析 |
| `DEBATE_CALL_TIMEOUT` | `60` | 单次 LLM 调用超时（秒） |
| `DEBATE_NEWS_CHARS` | `1500` | 注入辩论上下文的新闻摘要截断长度 |

## 验证

```bash
python -m pytest tests/test_paper_trading_service.py tests/test_calibration_service.py tests/test_debate_service.py -q
python scripts/weekly_report.py --dry-run
```

# 动量+价值选股筛（含标普500全市场扫描）

DSA 复用 [ai-berkshire](https://github.com/xbtlin/ai-berkshire) 的 `tools/stock_screener.py`
（动量发现 + 6 维价值验证）对一批标的做每日扫描，把买入/观察信号经 DSA 通知层推送到
所有已配置渠道（企业微信/飞书/Telegram/邮件等）。screener 实现保留在 ai-berkshire 仓库，
DSA 侧 `scripts/screener_notify.py` 只做编排、universe 供给与推送。

## 两层框架

1. **动量发现**：60 日新高 + 放量（实时价，任意标的可算）。
2. **6 维价值验证**：营收加速、毛利率扩张、盈利惊喜、营收高增长、毛利率健康、毛利连续改善；
   数据来自基本面缓存（见下）。
3. **信号分级**：`BUY_8%` / `BUY_5%` / `BUY_3%` / `WATCH`，红灯大盘自动降级（`SCREENER_MARKET_LIGHT_ENABLED`）。

## 扫描范围（universe）

优先级：`SCREENER_STOCKS`（显式覆盖） > `SCREENER_UNIVERSE=sp500` > `STOCK_LIST`。

| 场景 | 配置 |
|---|---|
| 扫自选股（默认） | 不设 `SCREENER_UNIVERSE`，用 `STOCK_LIST` |
| 扫标普500全市场 | `SCREENER_UNIVERSE=sp500` |
| 临时指定标的 | `SCREENER_STOCKS=NVDA,AAPL` |

标普500 成分股读 `data/universe/sp500.csv`。仓库内含 bootstrap 种子；完整最新成分由
`scripts/refresh_sp500.py`（抓维基百科公开成分表）刷新。`SCREENER_UNIVERSE_LIMIT` 可设扫描上限
（限速护栏，默认不限）。

## 标普500 基本面补全

价值验证需要每只的营收同比/毛利率/EPS超预期。全市场 500 只不可能手工录入，由
`scripts/populate_sp500_fundamentals.py` 批量补全：

- 数据来自 DSA 现有 `data_provider/yfinance_fundamental_adapter`（营收同比、毛利率、ROE）；
  EPS 超预期尽力从 yfinance earnings 数据补充，拿不到记 0（不伪造超预期）。
- 产物 `data/universe/sp500_fundamentals.json`（screener 读取格式）。选股时
  `screener_notify.py` 在 sp500 模式下把该快照并集合并进 screener 的 `data/fundamentals.json`，
  不覆盖 ai-berkshire 已有的自选股基本面。
- 增量续跑：默认跳过最新季度报告日在 `--max-age-days`（默认 25 天）内的标的；`--force` 全量重算。
- 单只失败 fail-open：跳过不中断整批；营收同比/毛利率任一缺失的标的不写入。

## 常用命令

```bash
# 本地：扫标普500并干跑（不推送）
SCREENER_UNIVERSE=sp500 python scripts/screener_notify.py --dry-run

# 刷新成分 + 批量补全基本面（需外网）
python scripts/refresh_sp500.py
python scripts/populate_sp500_fundamentals.py            # 增量
python scripts/populate_sp500_fundamentals.py --limit 20 # 抽样验证
```

## 自动化（GitHub Actions）

| 工作流 | 触发 | 作用 |
|---|---|---|
| `01-screener.yml` | 交易日美股收盘后 / 手动 | 扫描并推送；仓库变量 `SCREENER_UNIVERSE=sp500` 或手动输入 `universe=sp500` 时扫标普500 |
| `03-sp500-fundamentals.yml` | 每周六 / 手动 | 刷新成分 + 批量补全基本面，提交 `data/universe/` 快照 |

每日选股只读已提交的基本面快照（轻），周级刷新负责重的 500 次数据拉取，二者解耦。

## 已知边界

- **动量优先**：500 只全跑动量（实时价），价值验证只对有基本面缓存的标的生效；缺缓存的仅动量、
  多评为 `WATCH`。周级基本面刷新覆盖越全，价值验证越完整。
- **EPS 超预期**：yfinance 不在基本面适配层暴露，作为可选增强单独尽力获取，缺失记 0。
- **限速**：标普500 需 ~500 次数据源调用；补全脚本 `--sleep` 限速、失败 fail-open，
  选股可用 `SCREENER_UNIVERSE_LIMIT` 设上限。
- 所有信号仅供研究参考，不构成投资建议。

## 回滚

不设 `SCREENER_UNIVERSE`（或置空）即回到原自选股扫描；标普500 相关全是旁路脚本与独立工作流，
不改动 DSA 主分析流水线、通知层与数据源。

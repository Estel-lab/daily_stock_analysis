# -*- coding: utf-8 -*-
"""
===================================
财报日历服务（美股）
===================================

职责：
1. 通过 yfinance 查询美股个股的下一次财报日期；
2. 距财报 N 天内时生成一行提示文本，供注入个股分析的 news_context；
3. 失败或无数据时返回 None（fail-open，不阻塞分析主流程）。

仅对美股代码生效（调用方负责用 is_us_stock_code 判断）。
进程内缓存查询结果，避免同一次运行内重复请求。
"""

import logging
import threading
from datetime import date, datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 距财报该天数以内才提示（太远的财报对当日情绪判断无意义）
EARNINGS_HINT_WINDOW_DAYS = 14
# 财报发布后该天数以内视为"财报点评"窗口
EARNINGS_POST_WINDOW_DAYS = 3

# 缓存 (上一次财报日期, 下一次财报日期)，均可能为 None
_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()


def _coerce_date(value: Any) -> Optional[date]:
    """把 yfinance 可能返回的多种日期类型统一为 date。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    # pandas.Timestamp 等具有 to_pydatetime 的类型
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        try:
            return to_pydatetime().date()
        except Exception:
            return None
    return None


def _fetch_earnings_dates(code: str) -> tuple:
    """查询 (上一次财报日期, 下一次财报日期)；失败返回 (None, None)。按代码缓存于进程内。

    "上一次"来自 yfinance calendar 原始日期中早于今天的最近一条；yfinance 通常只
    返回临近的财报日期，因此刚发布后的短窗口内可以命中，更早的历史财报拿不到
    （对财报点评窗口足够）。
    """
    ticker = code.upper().strip()
    with _cache_lock:
        if ticker in _cache:
            return _cache[ticker]

    recent: Optional[date] = None
    upcoming: Optional[date] = None
    try:
        import yfinance as yf

        calendar = yf.Ticker(ticker).calendar
        raw_dates = []
        if isinstance(calendar, dict):
            raw = calendar.get("Earnings Date") or calendar.get("earnings_date") or []
            raw_dates = raw if isinstance(raw, (list, tuple)) else [raw]
        today = date.today()
        parsed = sorted(d for d in (_coerce_date(v) for v in raw_dates) if d is not None)
        future = [d for d in parsed if d >= today]
        past = [d for d in parsed if d < today]
        upcoming = future[0] if future else None
        recent = past[-1] if past else None
    except Exception as e:
        logger.debug("Earnings calendar fetch failed for %s: %s", ticker, e)

    with _cache_lock:
        _cache[ticker] = (recent, upcoming)
    return recent, upcoming


def fetch_next_earnings_date(code: str) -> Optional[date]:
    """查询下一次财报日期；无数据或失败返回 None。结果按代码缓存于进程内。"""
    return _fetch_earnings_dates(code)[1]


def get_upcoming_earnings_line(
    code: str,
    stock_name: str = "",
    *,
    window_days: int = EARNINGS_HINT_WINDOW_DAYS,
) -> Optional[str]:
    """
    距下一次财报 window_days 天以内时返回提示行，否则返回 None。

    输出示例：
        📅 财报提示: AAPL 将于 2026-07-20 发布财报（距今 12 天）。
        财报前的高热度多为投机性押注，财报后才反映真实反应，请在情绪判断中考虑该事件。
    """
    next_date = fetch_next_earnings_date(code)
    if next_date is None:
        return None
    days_left = (next_date - date.today()).days
    if days_left < 0 or days_left > window_days:
        return None
    display = f"{stock_name}({code.upper()})" if stock_name else code.upper()
    return (
        f"📅 财报提示: {display} 将于 {next_date.isoformat()} 发布财报（距今 {days_left} 天）。"
        "财报前的舆情热度多含投机性押注，财报公布后才反映真实市场反应，请在情绪与风险判断中考虑该事件。"
    )


def get_earnings_window_context(
    code: str,
    stock_name: str = "",
    *,
    pre_window_days: int = EARNINGS_HINT_WINDOW_DAYS,
    post_window_days: int = EARNINGS_POST_WINDOW_DAYS,
) -> Optional[str]:
    """
    财报窗口上下文块：财报前 pre_window_days 天内返回"财报前瞻"指令块，
    财报后 post_window_days 天内返回"财报点评"指令块，其余情况返回 None。

    指令块注入 news_context 后，要求 LLM 在报告中生成对应小节，
    替代旧的单行提示（旧接口 get_upcoming_earnings_line 保留兼容）。
    """
    recent, upcoming = _fetch_earnings_dates(code)
    today = date.today()
    display = f"{stock_name}({code.upper()})" if stock_name else code.upper()

    if upcoming is not None:
        days_left = (upcoming - today).days
        if 0 <= days_left <= pre_window_days:
            return (
                f"📅 财报窗口（前瞻）: {display} 将于 {upcoming.isoformat()} 发布财报"
                f"（距今 {days_left} 天）。请在报告中加入「财报前瞻」小节，覆盖：\n"
                "1) 市场对本次财报的核心预期与分歧点（从新闻上下文提取；无相关信息时明确说明）；\n"
                "2) 事件风险：财报前舆情热度多含投机性押注、隐含波动上升，谨防单边重仓押注财报结果；\n"
                "3) 操作纪律：给出仓位与买卖点建议时必须显式考虑财报不确定性。"
            )

    if recent is not None:
        days_since = (today - recent).days
        if 0 < days_since <= post_window_days:
            return (
                f"📊 财报窗口（点评）: {display} 已于 {recent.isoformat()} 发布财报"
                f"（{days_since} 天前）。请在报告中加入「财报点评」小节，覆盖：\n"
                "1) 从新闻上下文提取实际业绩与预期的对比（营收/EPS/指引）；"
                "信息不足时写明「财报细节待确认」，禁止编造数字；\n"
                "2) 明确说明本次财报是否改变对该股的评分与观点，并给出理由；\n"
                "3) 财报后的价格反应比财报前的预期博弈更能反映真实定价，短线判断以财报后走势确认为准。"
            )

    return None


def clear_cache() -> None:
    """清空进程内缓存（供测试使用）。"""
    with _cache_lock:
        _cache.clear()

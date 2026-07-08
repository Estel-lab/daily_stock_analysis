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

_cache: Dict[str, Optional[date]] = {}
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


def fetch_next_earnings_date(code: str) -> Optional[date]:
    """查询下一次财报日期；无数据或失败返回 None。结果按代码缓存于进程内。"""
    ticker = code.upper().strip()
    with _cache_lock:
        if ticker in _cache:
            return _cache[ticker]

    result: Optional[date] = None
    try:
        import yfinance as yf

        calendar = yf.Ticker(ticker).calendar
        raw_dates = []
        if isinstance(calendar, dict):
            raw = calendar.get("Earnings Date") or calendar.get("earnings_date") or []
            raw_dates = raw if isinstance(raw, (list, tuple)) else [raw]
        today = date.today()
        upcoming = sorted(
            d for d in (_coerce_date(v) for v in raw_dates) if d is not None and d >= today
        )
        result = upcoming[0] if upcoming else None
    except Exception as e:
        logger.debug("Earnings calendar fetch failed for %s: %s", ticker, e)
        result = None

    with _cache_lock:
        _cache[ticker] = result
    return result


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


def clear_cache() -> None:
    """清空进程内缓存（供测试使用）。"""
    with _cache_lock:
        _cache.clear()

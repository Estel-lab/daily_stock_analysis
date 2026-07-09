# -*- coding: utf-8 -*-
"""
美股期权情绪服务（yfinance 免费期权链）

计算最近到期日的 Put/Call 比（按未平仓量与成交量），生成情绪提示行注入分析上下文。
P/C(OI) > 1.2 偏防御/看空对冲增多；< 0.7 投机看多情绪偏热。失败返回 None（fail-open）。
"""

import logging
import threading
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_cache: Dict[str, Optional[str]] = {}
_lock = threading.Lock()


def get_options_sentiment_line(code: str, stock_name: str = "") -> Optional[str]:
    ticker = code.upper().strip()
    with _lock:
        if ticker in _cache:
            return _cache[ticker]

    result: Optional[str] = None
    try:
        import yfinance as yf

        tk = yf.Ticker(ticker)
        expirations = tk.options or []
        if expirations:
            chain = tk.option_chain(expirations[0])
            put_oi = float(chain.puts["openInterest"].fillna(0).sum())
            call_oi = float(chain.calls["openInterest"].fillna(0).sum())
            put_vol = float(chain.puts["volume"].fillna(0).sum())
            call_vol = float(chain.calls["volume"].fillna(0).sum())
            if call_oi > 0:
                pc_oi = put_oi / call_oi
                pc_vol = put_vol / call_vol if call_vol > 0 else None
                if pc_oi > 1.2:
                    mood = "看跌对冲/防御需求偏高"
                elif pc_oi < 0.7:
                    mood = "投机看多情绪偏热（注意拥挤风险）"
                else:
                    mood = "多空押注均衡"
                display = f"{stock_name}({ticker})" if stock_name else ticker
                vol_part = f"，当日成交 P/C {pc_vol:.2f}" if pc_vol is not None else ""
                result = (
                    f"🎲 期权情绪: {display} 最近到期日({expirations[0]}) "
                    f"Put/Call 未平仓比 {pc_oi:.2f}{vol_part} — {mood}。"
                )
    except Exception as e:
        logger.debug("Options sentiment fetch failed for %s: %s", ticker, e)

    with _lock:
        _cache[ticker] = result
    return result


def clear_cache() -> None:
    with _lock:
        _cache.clear()

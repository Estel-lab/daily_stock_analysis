# -*- coding: utf-8 -*-
"""
===================================
财报日历命令
===================================

查询自选股或指定标的的下一次财报日期（数据源 yfinance，仅支持美股）。
"""

import logging
import re
from datetime import date
from typing import List

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse

logger = logging.getLogger(__name__)

# 一次查询的标的上限（yfinance 逐个请求，避免长时间阻塞回复）
MAX_CODES = 8

_US_TICKER_RE = re.compile(r"^[A-Za-z][A-Za-z.\-]{0,9}$")


class EarningsCommand(BotCommand):
    """
    财报日历命令

    用法：
        /earnings              - 查询自选股中美股标的的财报日期
        /earnings AAPL NVDA    - 查询指定标的
    """

    @property
    def name(self) -> str:
        return "earnings"

    @property
    def aliases(self) -> List[str]:
        return ["er", "财报", "财报日历"]

    @property
    def description(self) -> str:
        return "查询下一次财报日期（美股）"

    @property
    def usage(self) -> str:
        return "/earnings [股票代码...]"

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        from src.config import get_config
        from src.services.earnings_calendar_service import fetch_next_earnings_date

        codes = [a.strip().upper() for a in args if a.strip()]
        if not codes:
            config = get_config()
            codes = [c for c in (config.stock_list or []) if c.strip()]
        if not codes:
            return BotResponse.markdown_response(
                "未指定标的且自选股为空。\n用法：`/earnings AAPL NVDA`"
            )

        us_codes = [c for c in codes if _US_TICKER_RE.match(c)]
        skipped = [c for c in codes if c not in us_codes]
        us_codes = us_codes[:MAX_CODES]
        if not us_codes:
            return BotResponse.markdown_response(
                "⚠️ 财报日历目前仅支持美股代码（如 AAPL）。\n"
                f"以下标的暂不支持：{', '.join(skipped)}"
            )

        today = date.today()
        lines = [f"# 📅 财报日历 {today.isoformat()}", ""]
        for code in us_codes:
            next_date = fetch_next_earnings_date(code)
            if next_date is None:
                lines.append(f"- **{code}**：暂无财报日期数据")
            else:
                days_left = (next_date - today).days
                marker = " ⚠️ 财报临近" if days_left <= 7 else ""
                lines.append(
                    f"- **{code}**：{next_date.isoformat()}（距今 {days_left} 天）{marker}"
                )
        if skipped:
            lines.append("")
            lines.append(f"> 跳过非美股标的：{', '.join(skipped)}（暂不支持）")

        return BotResponse.markdown_response("\n".join(lines))

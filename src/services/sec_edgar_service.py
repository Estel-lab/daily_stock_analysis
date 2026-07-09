# -*- coding: utf-8 -*-
"""
===================================
SEC EDGAR 公告服务（美股）
===================================

职责：
1. 通过 SEC 官方免费 API 查询美股个股近期公告（8-K 重大事件、10-Q/10-K 财报、Form 4 内部人交易、13D/G 举牌）；
2. 生成注入 news_context 的提示行（第一手权威证据，零噪音）；
3. 失败或无数据返回 None（fail-open）。

SEC 要求请求携带标识性 User-Agent；无需 API Key。
ticker→CIK 映射与公告列表均做进程内缓存，每次运行每票至多 1 次公告请求。
"""

import logging
import threading
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_USER_AGENT = "daily-stock-analysis/1.0 (github.com/Estel-lab/daily_stock_analysis)"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_TIMEOUT = 10
# 关注的公告类型及中文说明
_FORMS_OF_INTEREST = {
    "8-K": "重大事件",
    "10-Q": "季报",
    "10-K": "年报",
    "4": "内部人交易",
    "SC 13D": "举牌(13D)",
    "SC 13G": "举牌(13G)",
}
# 只提示最近 N 天内的公告
FILING_WINDOW_DAYS = 7

_cik_map: Optional[Dict[str, int]] = None
_filings_cache: Dict[str, Optional[str]] = {}
_lock = threading.Lock()


def _get(url: str):
    return requests.get(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"}, timeout=_TIMEOUT)


def _load_cik_map() -> Dict[str, int]:
    global _cik_map
    with _lock:
        if _cik_map is not None:
            return _cik_map
    mapping: Dict[str, int] = {}
    try:
        resp = _get(_TICKERS_URL)
        if resp.status_code == 200:
            for item in resp.json().values():
                ticker = str(item.get("ticker", "")).upper()
                cik = item.get("cik_str")
                if ticker and cik:
                    mapping[ticker] = int(cik)
    except Exception as e:
        logger.debug("SEC ticker map fetch failed: %s", e)
    with _lock:
        _cik_map = mapping
    return mapping


def get_recent_filings_line(code: str, stock_name: str = "") -> Optional[str]:
    """返回近 7 天公告提示行；无公告/失败返回 None。"""
    ticker = code.upper().strip()
    with _lock:
        if ticker in _filings_cache:
            return _filings_cache[ticker]

    result: Optional[str] = None
    try:
        cik = _load_cik_map().get(ticker)
        if cik:
            resp = _get(_SUBMISSIONS_URL.format(cik=cik))
            if resp.status_code == 200:
                recent = (resp.json().get("filings") or {}).get("recent") or {}
                forms: List[str] = recent.get("form") or []
                dates: List[str] = recent.get("filingDate") or []
                cutoff = date.today() - timedelta(days=FILING_WINDOW_DAYS)
                hits = []
                form4_count = 0
                for form, filed in zip(forms, dates):
                    form_norm = str(form).strip()
                    if form_norm not in _FORMS_OF_INTEREST:
                        continue
                    try:
                        filed_date = datetime.strptime(filed, "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    if filed_date < cutoff:
                        continue
                    if form_norm == "4":
                        form4_count += 1
                        continue
                    hits.append(f"{form_norm}({_FORMS_OF_INTEREST[form_norm]}) {filed}")
                if form4_count:
                    hits.append(f"Form 4(内部人交易) x{form4_count} 近{FILING_WINDOW_DAYS}天")
                if hits:
                    display = f"{stock_name}({ticker})" if stock_name else ticker
                    result = (
                        f"📄 SEC 公告（官方一手信息，近{FILING_WINDOW_DAYS}天）: {display} — "
                        + "；".join(hits[:6])
                        + "。8-K/内部人集中交易是强事件信号，请优先于媒体转述采信。"
                    )
    except Exception as e:
        logger.debug("SEC filings fetch failed for %s: %s", ticker, e)

    with _lock:
        _filings_cache[ticker] = result
    return result


def clear_cache() -> None:
    global _cik_map
    with _lock:
        _cik_map = None
        _filings_cache.clear()

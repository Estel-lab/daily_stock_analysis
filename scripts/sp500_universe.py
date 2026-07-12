# -*- coding: utf-8 -*-
"""标普500成分股 universe 的读取与路径约定。

供 `screener_notify.py`（选股扫描范围）与 `populate_sp500_fundamentals.py`
（基本面批量补全）共用，避免两处各写一份 universe 解析。

约定：
- 成分股快照默认在 ``data/universe/sp500.csv``（由 ``scripts/refresh_sp500.py`` 生成/刷新）；
  运行时只读该文件，离线安全；成分变动慢（一年数次），按周级刷新即可。
- 基本面快照默认在 ``data/universe/sp500_fundamentals.json``
  （由 ``scripts/populate_sp500_fundamentals.py`` 生成，screener 读取格式）。
- 代码统一规整为 Yahoo/yfinance 兼容写法（如 ``BRK.B`` -> ``BRK-B``），
  与 `screener_notify.to_yahoo_symbol`、`fetch_prices_curl` 的下游一致。
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_UNIVERSE_FILE = PROJECT_ROOT / "data" / "universe" / "sp500.csv"
DEFAULT_FUNDAMENTALS_FILE = PROJECT_ROOT / "data" / "universe" / "sp500_fundamentals.json"


def universe_file() -> Path:
    """成分股 CSV 路径；`SP500_UNIVERSE_FILE` 可覆盖。"""
    return Path(os.getenv("SP500_UNIVERSE_FILE") or DEFAULT_UNIVERSE_FILE)


def fundamentals_snapshot_file() -> Path:
    """基本面快照 JSON 路径；`SP500_FUNDAMENTALS_FILE` 可覆盖。"""
    return Path(os.getenv("SP500_FUNDAMENTALS_FILE") or DEFAULT_FUNDAMENTALS_FILE)


def normalize_symbol(symbol: str) -> str:
    """成分代码规整为 Yahoo/yfinance 兼容写法（BRK.B -> BRK-B）。"""
    return (symbol or "").strip().upper().replace(".", "-")


def load_sp500_tickers(path: Optional[Path] = None) -> List[str]:
    """读取成分股代码列表（Yahoo 兼容写法，去重保序）。

    Raises:
        FileNotFoundError: 成分文件不存在时，提示先运行刷新脚本。
    """
    path = path or universe_file()
    if not path.exists():
        raise FileNotFoundError(
            f"未找到标普500成分文件 {path}，请先运行 `python scripts/refresh_sp500.py` 生成"
        )
    tickers: List[str] = []
    seen = set()
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("symbol") or row.get("Symbol") or "").strip()
            if not raw:
                continue
            sym = normalize_symbol(raw)
            if sym and sym not in seen:
                seen.add(sym)
                tickers.append(sym)
    return tickers

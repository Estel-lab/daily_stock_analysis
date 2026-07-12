# -*- coding: utf-8 -*-
"""刷新标普500成分股快照 ``data/universe/sp500.csv``（按市值权重排序）。

数据来源：slickcharts.com/sp500（公开、免密钥），按市值权重降序给出全部 503 个成分
（含 GOOGL/GOOG 等双重股权）。**按权重排序**是刻意选择：``SCREENER_UNIVERSE_LIMIT=N``
截断即「扫描市值最大的前 N 只」，符合「先看最值得关注的大盘股」的语义。

运行时机：成分与权重变动慢，按需或由 ``03-sp500-fundamentals.yml`` 定时刷新；
DSA 选股运行时只读生成的 CSV，离线安全。抓取失败时保留仓库内已提交快照（fail-open）。

用法：
    python scripts/refresh_sp500.py            # 拉取并覆盖 data/universe/sp500.csv
    python scripts/refresh_sp500.py --dry-run  # 只打印抓到的条数与样例，不写文件
"""

from __future__ import annotations

import argparse
import csv
import sys
from io import StringIO
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sp500_universe import DEFAULT_UNIVERSE_FILE, normalize_symbol  # noqa: E402

SLICKCHARTS_URL = "https://www.slickcharts.com/sp500"
REQUEST_TIMEOUT = 30
MIN_EXPECTED = 400  # 成分应约 503；显著偏少视为页面结构变化，提示人工核对


def fetch_constituents() -> list[dict]:
    """抓取并解析成分股表（按市值权重降序）；返回 [{symbol,name,rank}]。

    用 requests 显式取 HTML（自动走 HTTPS_PROXY），再交给 pandas.read_html 解析，
    避免 read_html 内部直连绕过代理。slickcharts 按权重排序，保持行序即为权重序。
    """
    import pandas as pd  # noqa: PLC0415
    import requests  # noqa: PLC0415

    headers = {"User-Agent": "Mozilla/5.0 (compatible; DSA-sp500-refresh/1.0)"}
    resp = requests.get(SLICKCHARTS_URL, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    tables = pd.read_html(StringIO(resp.text))
    target = next(
        (df for df in tables if {"Symbol", "Company"}.issubset(set(map(str, df.columns)))),
        None,
    )
    if target is None:
        raise RuntimeError("slickcharts 成分表未找到（页面结构可能变化）")

    rows: list[dict] = []
    seen = set()
    for _, record in target.iterrows():
        symbol = normalize_symbol(str(record.get("Symbol", "")))
        if not symbol or symbol.lower() == "nan" or symbol in seen:
            continue
        seen.add(symbol)
        rows.append(
            {
                "symbol": symbol,
                "name": str(record.get("Company", "")).strip(),
                "rank": len(rows) + 1,  # 行序即市值权重序，rank 1 = 市值最大
            }
        )
    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["symbol", "name", "rank"])
        writer.writeheader()
        writer.writerows(rows)  # 保持权重序，不再字母排序


def main() -> int:
    parser = argparse.ArgumentParser(description="刷新标普500成分股快照（按市值权重排序）")
    parser.add_argument("--dry-run", action="store_true", help="只打印条数与样例，不写文件")
    parser.add_argument("--output", default=str(DEFAULT_UNIVERSE_FILE), help="输出 CSV 路径")
    args = parser.parse_args()

    try:
        rows = fetch_constituents()
    except Exception as exc:  # noqa: BLE001 - 抓取失败明确报错并保留原快照
        print(f"抓取标普500成分失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"抓取到 {len(rows)} 只成分股（按市值权重排序）")
    if len(rows) < MIN_EXPECTED:
        print(
            f"警告：成分股数量偏少（{len(rows)} < {MIN_EXPECTED}），"
            "可能页面结构变化，请人工核对后再覆盖",
            file=sys.stderr,
        )
        return 1
    if args.dry_run:
        for row in rows[:10]:
            print(f"  #{row['rank']:<3} {row['symbol']:<8} {row['name']}")
        print("  ...(dry-run 未写文件)")
        return 0

    write_csv(rows, Path(args.output))
    print(f"已写入 {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""批量补全标普500成分股基本面，供 ai-berkshire stock_screener 的 6 维价值验证使用。

数据来源：DSA 现有 ``data_provider/yfinance_fundamental_adapter``（营收同比 / 毛利率 / ROE）；
EPS 超预期尽力从 yfinance earnings 数据补充，拿不到则记 0（不伪造超预期）。

产物：
- ``data/universe/sp500_fundamentals.json``：DSA 侧提交的基本面快照（screener 读取格式：
  ``{ticker: {"quarters": {report_date: {label, rev_yoy, gm, eps_beat}}}}``）。
- 若配置 ``AI_BERKSHIRE_DIR``，同时把快照 merge 进 screener 的 ``data/fundamentals.json``，
  便于本地直接选股（不覆盖已有人工维护的自选股基本面，只做并集）。

设计约束（与仓库稳定性护栏一致）：
- 单只失败 fail-open：跳过并计入失败数，不中断整批。
- 增量续跑：复用已有快照，默认跳过最新季度报告日在 ``--max-age-days`` 内的标的；``--force`` 全量重算。
- 限速：``--sleep`` 控制单只间隔，避免第三方数据源限速。
- 只写有效数值：营收同比 / 毛利率任一缺失的标的不写入（screener 比较 None 会抛错）。

用法：
    python scripts/populate_sp500_fundamentals.py               # 增量补全全量
    python scripts/populate_sp500_fundamentals.py --limit 20    # 只处理前 20 只（验证）
    python scripts/populate_sp500_fundamentals.py --dry-run     # 不写文件
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sp500_universe import fundamentals_snapshot_file, load_sp500_tickers  # noqa: E402


def _quarter_label(report_date: Optional[str]) -> str:
    if not report_date:
        return "TTM"
    try:
        dt = datetime.strptime(report_date[:10], "%Y-%m-%d")
    except ValueError:
        return "TTM"
    return f"Q{(dt.month - 1) // 3 + 1} {dt.year}"


def fetch_eps_beat(symbol: str) -> Optional[float]:
    """尽力从 yfinance earnings 数据取最近一期已公布 EPS 超预期（%）；失败返回 None。

    yfinance 不在 fundamental 适配层暴露 EPS 超预期，这里作为价值验证 6 维之一
    的补充数据单独尽力获取；拿不到时上层记 0（不伪造超预期）。
    """
    try:
        import yfinance as yf  # noqa: PLC0415

        ticker = yf.Ticker(symbol)
        if not hasattr(ticker, "get_earnings_dates"):
            return None
        frame = ticker.get_earnings_dates(limit=8)
        if frame is None or frame.empty:
            return None
        col = next(
            (c for c in frame.columns if "surprise" in str(c).lower() and "%" in str(c)),
            None,
        ) or next((c for c in frame.columns if "surprise" in str(c).lower()), None)
        if col is None:
            return None
        series = frame[col].dropna()  # 只取已公布（有实际值）的期次
        if series.empty:
            return None
        return round(float(series.iloc[0]), 2)
    except Exception:  # noqa: BLE001 - EPS 超预期为可选增强，失败 fail-open
        return None


def build_quarter(adapter: Any, symbol: str) -> Optional[dict]:
    """用 DSA 基本面适配层组装单季记录；关键字段缺失返回 None（fail-open 跳过）。"""
    bundle = adapter.get_fundamental_bundle(symbol)
    growth = bundle.get("growth") or {}
    rev_yoy = growth.get("revenue_yoy")
    gross_margin = growth.get("gross_margin")
    if rev_yoy is None or gross_margin is None:
        return None
    report = (bundle.get("earnings") or {}).get("financial_report") or {}
    report_date = (report.get("report_date") or datetime.now(timezone.utc).date().isoformat())[:10]
    eps_beat = fetch_eps_beat(symbol)
    return {
        "report_date": report_date,
        "record": {
            "label": _quarter_label(report_date),
            "rev_yoy": round(float(rev_yoy), 2),
            "gm": round(float(gross_margin), 2),
            "eps_beat": round(float(eps_beat), 2) if eps_beat is not None else 0.0,
        },
    }


def _load_snapshot(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 快照损坏时从空开始重建
        return {}


def _latest_report_date(entry: dict) -> Optional[str]:
    quarters = (entry or {}).get("quarters") or {}
    return max(quarters) if quarters else None


def sync_to_screener(funds: Dict[str, Any]) -> None:
    """把快照 merge 进 ai-berkshire screener 的 fundamentals.json（并集，不覆盖自选股）。"""
    berkshire_dir = os.getenv("AI_BERKSHIRE_DIR")
    if not berkshire_dir:
        return
    fund_file = Path(berkshire_dir) / "data" / "fundamentals.json"
    existing = _load_snapshot(fund_file)
    for symbol, entry in funds.items():
        merged = existing.get(symbol) or {"quarters": {}}
        merged.setdefault("quarters", {}).update(entry.get("quarters", {}))
        existing[symbol] = merged
    fund_file.parent.mkdir(parents=True, exist_ok=True)
    fund_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已同步 {len(funds)} 只基本面到 screener 缓存：{fund_file}")


def main() -> int:
    parser = argparse.ArgumentParser(description="批量补全标普500基本面")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只（0=全部）")
    parser.add_argument("--sleep", type=float, default=0.5, help="单只间隔秒（限速）")
    parser.add_argument(
        "--max-age-days", type=int, default=25,
        help="最新季度报告日在该天数内则跳过（增量）；--force 忽略",
    )
    parser.add_argument("--force", action="store_true", help="忽略增量，全量重算")
    parser.add_argument("--dry-run", action="store_true", help="不写文件")
    parser.add_argument("--output", default=str(fundamentals_snapshot_file()), help="输出快照路径")
    args = parser.parse_args()

    try:
        tickers = load_sp500_tickers()
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.limit > 0:
        tickers = tickers[: args.limit]

    from data_provider.yfinance_fundamental_adapter import (  # noqa: PLC0415
        YfinanceFundamentalAdapter,
    )

    adapter = YfinanceFundamentalAdapter()
    output_path = Path(args.output)
    funds = _load_snapshot(output_path)  # 复用已有快照：增量 + 累积多季度历史
    today = datetime.now(timezone.utc).date()

    updated = skipped_fresh = failed = 0
    total = len(tickers)
    for index, symbol in enumerate(tickers, 1):
        entry = funds.get(symbol) or {"quarters": {}}
        if not args.force:
            last = _latest_report_date(entry)
            if last:
                try:
                    age = (today - datetime.strptime(last, "%Y-%m-%d").date()).days
                    if age <= args.max_age_days:
                        skipped_fresh += 1
                        continue
                except ValueError:
                    pass
        try:
            quarter = build_quarter(adapter, symbol)
        except Exception as exc:  # noqa: BLE001 - 单只失败 fail-open
            failed += 1
            print(f"[{index}/{total}] {symbol:<8} 失败(fail-open): {type(exc).__name__}: {exc}")
            continue
        if quarter is None:
            failed += 1
            print(f"[{index}/{total}] {symbol:<8} 关键字段缺失，跳过")
            continue
        entry.setdefault("quarters", {})[quarter["report_date"]] = quarter["record"]
        funds[symbol] = entry
        updated += 1
        rec = quarter["record"]
        print(
            f"[{index}/{total}] {symbol:<8} {quarter['report_date']} "
            f"营收{rec['rev_yoy']}% 毛利{rec['gm']}% EPS超预期{rec['eps_beat']}%"
        )
        if args.sleep > 0:
            time.sleep(args.sleep)

    print(
        f"\n完成：更新 {updated} · 跳过(数据较新) {skipped_fresh} · "
        f"失败/缺失 {failed} · 总计 {total}"
    )
    if args.dry_run:
        print("dry-run 未写文件")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(funds, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 DSA 基本面快照：{output_path}")
    sync_to_screener(funds)
    return 0


if __name__ == "__main__":
    sys.exit(main())

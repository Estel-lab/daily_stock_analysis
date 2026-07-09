# -*- coding: utf-8 -*-
"""每周投研周报。

汇总"周"粒度信息并经通知层推送（report 路由）：
1. 本周个股分析摘要：近 7 天分析记录，按最新评分排序，标注周内评分变化；
2. 本周选股筛信号统计：从 data/screener_signals.jsonl 汇总分级分布
   （逐信号收益复盘在每周一的选股筛推送中，本报告不重复拉价）；
3. 下周财报日历：自选股美股标的未来 7 天内的财报日期。

数据来源全部是已有模块（HistoryService / earnings_calendar_service /
信号历史文件），本脚本只做组装。依赖历史数据库（workflow 中先恢复缓存）。

用法：
    python scripts/weekly_report.py            # 生成并推送
    python scripts/weekly_report.py --dry-run  # 只打印
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

WEEK_DAYS = 7
MAX_STOCK_ROWS = 20
MAX_EARNINGS_ROWS = 10


def build_analysis_section(today: date) -> Optional[str]:
    """近 7 天个股分析摘要：每只股票取最新记录，并对比周初评分。"""
    from src.services.history_service import HistoryService

    start = (today - timedelta(days=WEEK_DAYS)).isoformat()
    result = HistoryService().get_history_list(
        report_type="stock", start_date=start, end_date=today.isoformat(), limit=200
    )
    items = result.get("items") or []
    if not items:
        return None

    by_code: Dict[str, List[dict]] = defaultdict(list)
    for item in items:
        code = item.get("stock_code") or ""
        if code:
            by_code[code].append(item)

    rows = []
    for code, records in by_code.items():
        records.sort(key=lambda r: r.get("created_at") or "")
        latest, earliest = records[-1], records[0]
        score = latest.get("sentiment_score")
        first_score = earliest.get("sentiment_score")
        delta = ""
        if isinstance(score, (int, float)) and isinstance(first_score, (int, float)) \
                and len(records) > 1 and score != first_score:
            arrow = "↑" if score > first_score else "↓"
            delta = f"（周内 {first_score}→{score} {arrow}）"
        rows.append((
            score if isinstance(score, (int, float)) else -1,
            f"- **{latest.get('stock_name') or code}({code})** "
            f"评分 {score if score is not None else '?'}{delta} · "
            f"{latest.get('operation_advice') or latest.get('action_label') or ''}",
        ))
    rows.sort(key=lambda r: r[0], reverse=True)

    lines = [f"## 📋 本周个股分析（{len(by_code)} 只，按最新评分排序）", ""]
    lines.extend(text for _, text in rows[:MAX_STOCK_ROWS])
    if len(rows) > MAX_STOCK_ROWS:
        lines.append(f"- ……另有 {len(rows) - MAX_STOCK_ROWS} 只，见 Web 工作台历史")
    return "\n".join(lines)


def build_signal_section(today: date) -> Optional[str]:
    """本周选股筛信号统计（分级分布，不拉价）。"""
    history_file = Path(
        os.getenv("SCREENER_HISTORY_FILE", PROJECT_ROOT / "data" / "screener_signals.jsonl")
    )
    if not history_file.exists():
        return None
    week_start = today - timedelta(days=WEEK_DAYS)
    grade_counter: Counter = Counter()
    tickers_by_grade: Dict[str, List[str]] = defaultdict(list)
    for line in history_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            record_date = datetime.strptime(record["date"], "%Y-%m-%d").date()
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if week_start <= record_date <= today:
            grade = record.get("grade", "?")
            grade_counter[grade] += 1
            if record.get("ticker") not in tickers_by_grade[grade]:
                tickers_by_grade[grade].append(record["ticker"])
    if not grade_counter:
        return None

    lines = ["## 📡 本周选股筛信号", ""]
    for grade in sorted(grade_counter):
        tickers = "、".join(tickers_by_grade[grade][:8])
        lines.append(f"- {grade}：{grade_counter[grade]} 次（{tickers}）")
    lines.append("")
    lines.append("> 逐信号收益复盘见每周一选股筛推送")
    return "\n".join(lines)


def build_earnings_section(today: date) -> Optional[str]:
    """下周财报日历：自选股美股标的未来 7 天的财报日期。"""
    from src.config import get_config
    from src.services.earnings_calendar_service import fetch_next_earnings_date
    from data_provider.us_index_mapping import is_us_stock_code

    codes = [c for c in (get_config().stock_list or []) if is_us_stock_code(c)]
    rows = []
    for code in codes[:30]:
        next_date = fetch_next_earnings_date(code)
        if next_date is None:
            continue
        days_left = (next_date - today).days
        if 0 <= days_left <= WEEK_DAYS:
            rows.append((next_date, f"- **{code.upper()}**：{next_date.isoformat()}（{days_left} 天后）"))
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])
    lines = ["## 📅 下周财报日历", ""]
    lines.extend(text for _, text in rows[:MAX_EARNINGS_ROWS])
    return "\n".join(lines)


def build_weekly_report(today: Optional[date] = None) -> Optional[str]:
    today = today or date.today()
    sections = [
        build_analysis_section(today),
        build_signal_section(today),
        build_earnings_section(today),
    ]
    sections = [s for s in sections if s]
    if not sections:
        return None
    header = (
        f"# 🗓️ 投研周报 "
        f"{(today - timedelta(days=WEEK_DAYS)).isoformat()} ~ {today.isoformat()}"
    )
    return "\n\n".join([header] + sections)


def main() -> int:
    parser = argparse.ArgumentParser(description="投研周报生成与推送")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不推送")
    args = parser.parse_args()

    report = build_weekly_report()
    if not report:
        print("本周无任何可汇总内容（历史库为空且无信号记录），跳过周报")
        return 0

    if args.dry_run:
        print(report)
        return 0

    from src.notification import get_notification_service

    ok = get_notification_service().send(report, route_type="report")
    print(f"周报推送{'成功' if ok else '失败'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

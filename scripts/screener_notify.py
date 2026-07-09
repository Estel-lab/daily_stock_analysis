# -*- coding: utf-8 -*-
"""动量+价值选股筛推送脚本。

复用 ai-berkshire 仓库的 tools/stock_screener.py（动量发现 + 6 维价值验证），
对 DSA 自选股（STOCK_LIST）执行每日扫描，将买入/观察信号通过 DSA 通知层
推送到所有已配置渠道（企业微信/飞书/Telegram/邮件等）。

设计约束：
- screener 代码保留在 ai-berkshire 仓库，本脚本只做编排与推送，不复制实现。
- 无信号时默认静默（SCREENER_NOTIFY_EMPTY=true 可改为推送空结果）。
- 无法映射为 Yahoo Finance 代码的标的跳过，并在推送尾部注明。

环境变量：
- AI_BERKSHIRE_DIR: ai-berkshire 仓库检出路径（默认 ./ai-berkshire）
- SCREENER_STOCKS: 覆盖扫描标的（逗号/空格分隔，DSA 代码格式）；为空时用 STOCK_LIST
- SCREENER_NOTIFY_EMPTY: 无信号时也推送（默认 false）

用法：
    python scripts/screener_notify.py            # 扫描并推送
    python scripts/screener_notify.py --dry-run  # 只打印，不推送
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.services.stock_list_parser import split_stock_list  # noqa: E402

_HK_CODE_RE = re.compile(r"^(?:hk)?(\d{4,5})(?:\.hk)?$", re.IGNORECASE)
_CN_CODE_RE = re.compile(r"^\d{6}$")
_US_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z.\-]{0,9}$")

GRADE_ORDER = {"BUY_8%": 0, "BUY_5%": 1, "BUY_3%": 2, "WATCH": 3}
GRADE_EMOJI = {
    "BUY_8%": "🔴",
    "BUY_5%": "🟡",
    "BUY_3%": "🟢",
    "WATCH": "👀",
}


def to_yahoo_symbol(code: str) -> Tuple[Optional[str], Optional[str]]:
    """将 DSA 自选股代码转换为 Yahoo Finance 代码。

    Returns:
        (yahoo_symbol, skip_reason)：转换成功时 skip_reason 为 None。
    """
    raw = (code or "").strip()
    if not raw:
        return None, "空代码"

    hk_match = _HK_CODE_RE.match(raw)
    if raw.lower().startswith("hk") and hk_match:
        return f"{int(hk_match.group(1)):04d}.HK", None

    if _CN_CODE_RE.match(raw):
        if raw.startswith("6"):
            return f"{raw}.SS", None
        if raw.startswith(("0", "3")):
            return f"{raw}.SZ", None
        return None, "北交所/场外代码 Yahoo 不支持"

    # 已带市场后缀（.HK/.T/.KS/.TW 等）的代码 Yahoo 大多兼容，原样透传
    if "." in raw:
        return raw.upper(), None

    if _US_CODE_RE.match(raw):
        return raw.upper(), None

    return None, "无法识别的代码格式"


def load_screener(berkshire_dir: Path):
    """从 ai-berkshire 检出目录加载 stock_screener 模块。"""
    tools_dir = berkshire_dir / "tools"
    if not (tools_dir / "stock_screener.py").exists():
        raise FileNotFoundError(
            f"未找到 {tools_dir / 'stock_screener.py'}，"
            "请确认 ai-berkshire 仓库已检出到 AI_BERKSHIRE_DIR 指定路径"
        )
    sys.path.insert(0, str(tools_dir))
    import stock_screener  # noqa: PLC0415

    return stock_screener


def resolve_tickers() -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """解析扫描标的。

    Returns:
        (可扫描的 [(原始代码, yahoo代码)], 跳过的 [(原始代码, 原因)])
    """
    raw_value = os.getenv("SCREENER_STOCKS") or os.getenv("STOCK_LIST") or ""
    codes = split_stock_list(raw_value)
    scannable: List[Tuple[str, str]] = []
    skipped: List[Tuple[str, str]] = []
    seen = set()
    for code in codes:
        yahoo, reason = to_yahoo_symbol(code)
        if yahoo is None:
            skipped.append((code, reason or "未知原因"))
        elif yahoo not in seen:
            seen.add(yahoo)
            scannable.append((code, yahoo))
    return scannable, skipped


def format_signal_line(result: dict) -> List[str]:
    """把单个信号渲染为推送 Markdown 行。"""
    ticker = result["ticker"]
    grade = result["grade"]
    momentum = result.get("momentum") or {}
    value = result.get("value")
    emoji = GRADE_EMOJI.get(grade, "")
    advice = result.get("advice") or ""

    lines = [
        f"**{ticker}** {emoji} {result.get('reason', grade)}{f' · {advice}' if advice else ''}",
        f"- 现价 ${momentum.get('close', '?')} · 30日 +{momentum.get('pct_30d', '?')}%"
        f" · 放量 {momentum.get('vol_ratio', '?')}x",
    ]
    if value:
        checks = value.get("checks") or {}
        checks_str = " ".join(f"{'✅' if ok else '❌'}{name}" for name, ok in checks.items())
        fund = value.get("fund") or {}
        lines.append(
            f"- 基本面({value.get('fund_label', '?')})："
            f"营收 {fund.get('rev_yoy', '?')}% · 毛利 {fund.get('gm', '?')}%"
            f" · EPS 超预期 {fund.get('eps_beat', '?')}%"
        )
        if checks_str:
            lines.append(f"- 6维验证：{checks_str}")
        if value.get("independent_pass"):
            lines.append(f"- ★ 独立条件通过：{value.get('independent_reason', '')}")
    elif grade == "WATCH":
        lines.append("- ⚠️ 仅动量信号：基本面未录入，需在 ai-berkshire 补充后才能完成 6 维验证")
    return lines


def build_message(
    buy_signals: List[dict],
    watch_signals: List[dict],
    scanned_count: int,
    skipped: List[Tuple[str, str]],
) -> str:
    """组装推送 Markdown。"""
    today = datetime.now().strftime("%Y-%m-%d")
    parts = [
        f"# 📡 动量+价值选股筛 {today}",
        "",
        f"框架：60日新高+放量 → 6维价值验证 → 信号分级 | 扫描 {scanned_count} 个标的",
        "",
    ]
    if buy_signals:
        parts.append(f"## 🎯 买入信号（{len(buy_signals)}）")
        for result in sorted(buy_signals, key=lambda r: GRADE_ORDER.get(r["grade"], 9)):
            parts.extend(format_signal_line(result))
            parts.append("")
    else:
        parts.extend(["## 无买入信号", ""])

    if watch_signals:
        parts.append(f"## 👀 观察池（{len(watch_signals)}，动量触发待补基本面）")
        for result in watch_signals:
            parts.extend(format_signal_line(result))
            parts.append("")

    if skipped:
        skipped_str = "、".join(f"{code}（{reason}）" for code, reason in skipped)
        parts.append(f"> 跳过：{skipped_str}")

    parts.append("> 信号来源：ai-berkshire stock_screener · 仅供研究参考，不构成投资建议")
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="动量+价值选股筛推送")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不发送通知")
    args = parser.parse_args()

    berkshire_dir = Path(os.getenv("AI_BERKSHIRE_DIR", PROJECT_ROOT / "ai-berkshire"))
    screener = load_screener(berkshire_dir)

    scannable, skipped = resolve_tickers()
    if not scannable:
        print("未配置可扫描标的（SCREENER_STOCKS / STOCK_LIST 均为空或全部无法映射），退出")
        for code, reason in skipped:
            print(f"  跳过 {code}: {reason}")
        return 0

    print(f"扫描 {len(scannable)} 个标的：{', '.join(y for _, y in scannable)}")
    buy_signals: List[dict] = []
    watch_signals: List[dict] = []
    failed: List[str] = []
    for original, yahoo in scannable:
        result = screener.scan_ticker(yahoo, verbose=False)
        if result is None:
            failed.append(yahoo)
            print(f"  {yahoo:<10} 数据获取失败")
            continue
        print(f"  {yahoo:<10} {result['grade']:<8} {result['reason']}")
        if result["grade"].startswith("BUY"):
            buy_signals.append(result)
        elif result["grade"] == "WATCH":
            watch_signals.append(result)

    if failed:
        skipped = skipped + [(t, "价格数据获取失败") for t in failed]

    output_dir = PROJECT_ROOT / "reports" / "screener"
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = output_dir / f"screener_{datetime.now().strftime('%Y%m%d')}.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "date": datetime.now().isoformat(),
                "buy": buy_signals,
                "watch": watch_signals,
                "skipped": skipped,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"结果快照：{snapshot_path}")

    has_signal = bool(buy_signals or watch_signals)
    notify_empty = os.getenv("SCREENER_NOTIFY_EMPTY", "false").lower() == "true"
    if not has_signal and not notify_empty:
        print("无信号且未开启 SCREENER_NOTIFY_EMPTY，跳过推送")
        return 0

    message = build_message(buy_signals, watch_signals, len(scannable), skipped)
    if args.dry_run:
        print("\n===== dry-run 推送内容 =====\n")
        print(message)
        return 0

    from src.notification import get_notification_service  # noqa: PLC0415

    service = get_notification_service()
    ok = service.send(
        message,
        route_type="alert",
        dedup_key=f"screener:{datetime.now().strftime('%Y%m%d')}",
    )
    print(f"通知发送{'成功' if ok else '失败（无可用渠道或全部渠道失败）'}")
    return 0 if ok or not has_signal else 1


if __name__ == "__main__":
    sys.exit(main())

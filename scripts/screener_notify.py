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
- SCREENER_STOCKS: 覆盖扫描标的（逗号/空格分隔，DSA 代码格式）；优先级最高
- SCREENER_UNIVERSE: 扫描范围。留空=用 STOCK_LIST；sp500=扫标普500成分股
  （读 data/universe/sp500.csv，见 scripts/refresh_sp500.py / populate_sp500_fundamentals.py）
- SCREENER_UNIVERSE_LIMIT: 扫描标的上限（默认 0=不限；用于全市场扫描时的限速护栏）
- SCREENER_NOTIFY_EMPTY: 无信号时也推送（默认 false）
- SCREENER_HISTORY_FILE: 信号历史 jsonl 路径（默认 data/screener_signals.jsonl）
- SCREENER_REVIEW: true 时强制附加历史信号复盘段（默认仅周一附加）
- SCREENER_MARKET_LIGHT_ENABLED: 大盘红绿灯联动（默认 true）；红灯市场的 BUY 信号
  降级为观察且不触发自动深研（回测依据：熊市中动量信号 20 日胜率仅 22%）

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
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

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


def sync_sp500_fundamentals(berkshire_dir: Path) -> None:
    """把 DSA 侧标普500基本面快照 merge 进 screener 的 fundamentals.json。

    CI 每次全新检出 ai-berkshire，其 fundamentals.json 只含人工维护的自选股；
    标普500 的基本面由 DSA 快照（scripts/populate_sp500_fundamentals.py 生成并提交）
    在扫描前合并进来（并集，不覆盖自选股），使 6 维价值验证可用。
    缺快照时 fail-open：价值验证退化为仅动量（信号多为 WATCH）。
    """
    from sp500_universe import fundamentals_snapshot_file  # noqa: PLC0415

    snapshot = fundamentals_snapshot_file()
    if not snapshot.exists():
        print(f"未找到标普500基本面快照 {snapshot}，价值验证退化为仅动量（fail-open）")
        return
    try:
        snap = json.loads(snapshot.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 快照损坏不应中断选股
        print(f"标普500基本面快照解析失败（fail-open）：{type(exc).__name__}: {exc}")
        return

    fund_file = berkshire_dir / "data" / "fundamentals.json"
    try:
        existing = json.loads(fund_file.read_text(encoding="utf-8")) if fund_file.exists() else {}
    except Exception:  # noqa: BLE001
        existing = {}
    for symbol, entry in snap.items():
        current = existing.get(symbol) or {"quarters": {}}
        current.setdefault("quarters", {}).update(entry.get("quarters", {}))
        existing[symbol] = current
    fund_file.parent.mkdir(parents=True, exist_ok=True)
    fund_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已合并 {len(snap)} 只标普500基本面到 screener 缓存")


def resolve_universe_codes() -> Tuple[List[str], str]:
    """按优先级解析扫描范围的原始代码列表。

    优先级：SCREENER_STOCKS（显式覆盖） > SCREENER_UNIVERSE=sp500（读成分文件） > STOCK_LIST。

    Returns:
        (原始代码列表, 来源标签)
    """
    explicit = os.getenv("SCREENER_STOCKS")
    if explicit:
        return split_stock_list(explicit), "SCREENER_STOCKS"

    universe = (os.getenv("SCREENER_UNIVERSE") or "").strip().lower()
    if universe == "sp500":
        from sp500_universe import load_sp500_tickers  # noqa: PLC0415

        return load_sp500_tickers(), "sp500"

    return split_stock_list(os.getenv("STOCK_LIST") or ""), "STOCK_LIST"


def resolve_tickers() -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """解析扫描标的。

    Returns:
        (可扫描的 [(原始代码, yahoo代码)], 跳过的 [(原始代码, 原因)])
    """
    codes, _source = resolve_universe_codes()
    limit_raw = os.getenv("SCREENER_UNIVERSE_LIMIT", "").strip()
    if limit_raw.isdigit() and int(limit_raw) > 0:
        codes = codes[: int(limit_raw)]
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
    review_section: Optional[str] = None,
    light_notes: Optional[List[str]] = None,
) -> str:
    """组装推送 Markdown。"""
    today = datetime.now().strftime("%Y-%m-%d")
    parts = [
        f"# 📡 动量+价值选股筛 {today}",
        "",
        f"框架：60日新高+放量 → 6维价值验证 → 信号分级 | 扫描 {scanned_count} 个标的",
        "",
    ]
    if light_notes:
        parts.extend(light_notes)
        parts.append("")
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

    if review_section:
        parts.append(review_section)
        parts.append("")

    if skipped:
        skipped_str = "、".join(f"{code}（{reason}）" for code, reason in skipped)
        parts.append(f"> 跳过：{skipped_str}")

    parts.append("> 信号来源：ai-berkshire stock_screener · 仅供研究参考，不构成投资建议")
    return "\n".join(parts)


LIGHT_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
MARKET_LABEL = {"us": "美股", "hk": "港股", "cn": "A股"}


def infer_market(yahoo_symbol: str) -> str:
    """由 Yahoo 代码推断市场（用于选择对应的大盘红绿灯）。"""
    symbol = (yahoo_symbol or "").upper()
    if symbol.endswith((".SS", ".SZ")):
        return "cn"
    if symbol.endswith(".HK"):
        return "hk"
    return "us"


def fetch_market_lights(markets: set) -> Dict[str, dict]:
    """获取各市场当前大盘红绿灯快照；单市场失败 fail-open（跳过该市场）。"""
    if os.getenv("SCREENER_MARKET_LIGHT_ENABLED", "true").lower() == "false":
        return {}
    lights: Dict[str, dict] = {}
    for region in sorted(markets):
        try:
            from src.services.market_light_service import build_current_snapshot  # noqa: PLC0415

            snapshot = build_current_snapshot(region)
            if snapshot and snapshot.get("status"):
                lights[region] = snapshot
                print(
                    f"大盘红绿灯 {region}: {snapshot['status']}"
                    f"（{snapshot.get('label', '')} score={snapshot.get('score')}）"
                )
        except Exception as exc:
            print(f"大盘红绿灯 {region} 获取失败（fail-open）: {exc}")
    return lights


def apply_market_light(
    buy_signals: List[dict],
    watch_signals: List[dict],
    lights: Dict[str, dict],
) -> Tuple[List[dict], List[dict], List[str]]:
    """红灯市场的 BUY 信号降级为观察（保留 raw_grade 供历史分层）。

    回测依据（reports/选股筛回测-20260710.md）：熊市（2022）中动量突破信号
    20 日胜率仅 22%、平均 -8.2%，红灯环境下不给仓位建议。
    """
    notes: List[str] = []
    for region, snapshot in lights.items():
        emoji = LIGHT_EMOJI.get(snapshot["status"], "")
        notes.append(
            f"{emoji} {MARKET_LABEL.get(region, region)}大盘：{snapshot.get('label') or snapshot['status']}"
        )

    red_markets = {r for r, s in lights.items() if s["status"] == "red"}
    if not red_markets:
        return buy_signals, watch_signals, notes

    kept_buy: List[dict] = []
    for signal in buy_signals:
        market = signal.get("market") or infer_market(signal["ticker"])
        if market in red_markets:
            signal["raw_grade"] = signal["grade"]
            signal["grade"] = "WATCH"
            signal["demoted_by_light"] = True
            signal["reason"] = f"大盘红灯降级（原 {signal['raw_grade']}，{signal['reason']}）"
            signal["advice"] = "红灯环境不建议追突破"
            watch_signals.append(signal)
        else:
            kept_buy.append(signal)
    demoted = len(buy_signals) - len(kept_buy)
    if demoted:
        notes.append(
            f"⚠️ {demoted} 个 BUY 信号因大盘红灯降级为观察"
            "（回测：熊市中动量信号 20 日胜率仅 22%）"
        )
    return kept_buy, watch_signals, notes


def default_history_path() -> Path:
    return Path(
        os.getenv("SCREENER_HISTORY_FILE", PROJECT_ROOT / "data" / "screener_signals.jsonl")
    )


def append_signal_history(
    buy_signals: List[dict],
    watch_signals: List[dict],
    path: Path,
    signal_date: Optional[str] = None,
) -> int:
    """把当日信号追加到 jsonl 历史（append-only，供复盘胜率统计）。"""
    signal_date = signal_date or datetime.now().strftime("%Y-%m-%d")
    lines = []
    for result in buy_signals + watch_signals:
        momentum = result.get("momentum") or {}
        value = result.get("value")
        entry = {
            "date": signal_date,
            "ticker": result["ticker"],
            "grade": result["grade"],
            "close": momentum.get("close"),
            "score": (value or {}).get("score"),
        }
        if result.get("raw_grade"):
            entry["raw_grade"] = result["raw_grade"]  # 红灯降级前的原始分级，供分层复盘
        if result.get("market"):
            entry["market"] = result["market"]
        lines.append(json.dumps(entry, ensure_ascii=False))
    if lines:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return len(lines)


# 复盘窗口：信号至少 5 天（≈一周）才计收益，最多回看 45 天；单次最多拉 30 个现价
REVIEW_MIN_AGE_DAYS = 5
REVIEW_MAX_AGE_DAYS = 45
REVIEW_MAX_TICKERS = 30


def build_outcome_review(screener, path: Path, today: Optional[datetime] = None) -> Optional[str]:
    """历史 BUY 信号复盘段：逐条收益 + 按分级汇总胜率；无足龄样本返回 None。"""
    if not path.exists():
        return None
    today = today or datetime.now()
    aged: List[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not str(record.get("grade", "")).startswith("BUY") or not record.get("close"):
            continue
        try:
            age = (today - datetime.strptime(record["date"], "%Y-%m-%d")).days
        except (KeyError, ValueError):
            continue
        if REVIEW_MIN_AGE_DAYS <= age <= REVIEW_MAX_AGE_DAYS:
            record["age_days"] = age
            aged.append(record)
    if not aged:
        return None

    aged = aged[-REVIEW_MAX_TICKERS:]
    current_price: dict = {}
    for ticker in {r["ticker"] for r in aged}:
        prices = screener.fetch_prices_curl(ticker, days=10)
        if prices:
            current_price[ticker] = prices[-1]["close"]

    reviewed = []
    for record in aged:
        now_price = current_price.get(record["ticker"])
        if not now_price:
            continue
        record["return_pct"] = (now_price / float(record["close"]) - 1) * 100
        reviewed.append(record)
    if not reviewed:
        return None

    lines = [
        "## 📈 历史信号复盘（信号后至今收益）",
        "",
    ]
    by_grade: dict = {}
    for record in sorted(reviewed, key=lambda r: r["return_pct"], reverse=True):
        ret = record["return_pct"]
        emoji = "🟢" if ret >= 0 else "🔴"
        lines.append(
            f"- {emoji} **{record['ticker']}** {record['grade']}"
            f"（{record['date']}，{record['age_days']} 天前）→ {ret:+.1f}%"
        )
        by_grade.setdefault(record["grade"], []).append(ret)
    lines.append("")
    for grade in sorted(by_grade):
        rets = by_grade[grade]
        win_rate = sum(1 for r in rets if r > 0) / len(rets) * 100
        avg = sum(rets) / len(rets)
        lines.append(
            f"- {grade}：样本 {len(rets)} · 胜率 {win_rate:.0f}% · 平均 {avg:+.1f}%"
        )
    lines.append("")
    lines.append(f"> 复盘窗口：信号后 {REVIEW_MIN_AGE_DAYS}-{REVIEW_MAX_AGE_DAYS} 天；样本少时结论仅供参考")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="动量+价值选股筛推送")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不发送通知")
    args = parser.parse_args()

    berkshire_dir = Path(os.getenv("AI_BERKSHIRE_DIR", PROJECT_ROOT / "ai-berkshire"))
    screener = load_screener(berkshire_dir)

    # 标普500 全市场扫描：先把 DSA 侧基本面快照合并进 screener 缓存，再扫描
    if (os.getenv("SCREENER_UNIVERSE") or "").strip().lower() == "sp500" and not os.getenv(
        "SCREENER_STOCKS"
    ):
        sync_sp500_fundamentals(berkshire_dir)

    try:
        scannable, skipped = resolve_tickers()
    except FileNotFoundError as exc:
        print(f"选股 universe 配置错误：{exc}", file=sys.stderr)
        return 1
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
        result["dsa_code"] = original  # 保留 DSA 代码，供强信号触发深度分析
        result["market"] = infer_market(yahoo)
        print(f"  {yahoo:<10} {result['grade']:<8} {result['reason']}")
        if result["grade"].startswith("BUY"):
            buy_signals.append(result)
        elif result["grade"] == "WATCH":
            watch_signals.append(result)

    if failed:
        skipped = skipped + [(t, "价格数据获取失败") for t in failed]

    # 大盘红绿灯联动：红灯市场的 BUY 信号降级（fail-open）
    signal_markets = {s["market"] for s in buy_signals + watch_signals if s.get("market")}
    lights = fetch_market_lights(signal_markets) if signal_markets else {}
    buy_signals, watch_signals, light_notes = apply_market_light(
        buy_signals, watch_signals, lights
    )

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

    # BUY 信号标的（DSA 代码）写入触发文件，供 workflow 决定是否触发深度分析
    trigger_path = output_dir / "trigger_stocks.txt"
    buy_codes = list(dict.fromkeys(
        s.get("dsa_code") or s["ticker"] for s in buy_signals
    ))
    trigger_path.write_text(",".join(buy_codes), encoding="utf-8")
    if buy_codes:
        print(f"BUY 信号标的：{','.join(buy_codes)}（触发文件：{trigger_path}）")

    history_file = default_history_path()
    appended = append_signal_history(buy_signals, watch_signals, history_file)
    if appended:
        print(f"已追加 {appended} 条信号到历史：{history_file}")

    # 每周一（或 SCREENER_REVIEW=true）附加历史信号复盘
    review_section = None
    force_review = os.getenv("SCREENER_REVIEW", "").lower() == "true"
    if force_review or datetime.now().weekday() == 0:
        review_section = build_outcome_review(screener, history_file)
        if review_section:
            print("已生成历史信号复盘段")

    has_signal = bool(buy_signals or watch_signals)
    notify_empty = os.getenv("SCREENER_NOTIFY_EMPTY", "false").lower() == "true"
    if not has_signal and not notify_empty and not review_section:
        print("无信号且未开启 SCREENER_NOTIFY_EMPTY，跳过推送")
        return 0

    message = build_message(
        buy_signals, watch_signals, len(scannable), skipped,
        review_section=review_section, light_notes=light_notes,
    )
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

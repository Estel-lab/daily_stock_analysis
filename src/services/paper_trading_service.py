# -*- coding: utf-8 -*-
"""
===================================
AI 模拟盘（Paper Trading）服务
===================================

职责：
1. 把历史分析建议（AnalysisHistory.operation_advice）确定性回放成一个
   长仓虚拟组合：建议偏多（bullish/hold → long）时持有，偏空/观望
   （bearish/wait → cash）时空仓，语义与 BacktestEngine.infer_position_recommendation
   完全一致；
2. 用本地日线库（stock_daily）逐日盯市生成净值曲线，并给出同一标的池的
   「等权买入持有」基准做对照，回答“择时建议有没有价值”；
3. 输出周报可直接拼装的 Markdown 段（build_paper_trading_section）。

设计要点：
- 无持久化可变状态：每次从分析历史 + 日线重放，避免 Actions 等临时环境
  的状态漂移；分析历史与日线均已随每日任务入库。
- 成交假设：信号当日收盘价成交（与回测引擎 anchor 语义一致），
  双边收取固定比例手续费；信号日无行情时顺延到下一根可用日线。
- 全链路 fail-open：无信号、无行情、配置关闭时返回 None，调用方静默跳过。

配置（环境变量，均可选）：
- PAPER_TRADING_ENABLED         默认 true
- PAPER_TRADING_INITIAL_CAPITAL 初始资金，默认 100000
- PAPER_TRADING_POSITION_PCT    单票目标仓位（占当前净值 %），默认 10
- PAPER_TRADING_COMMISSION_PCT  单边手续费率（%），默认 0.1
- PAPER_TRADING_LOOKBACK_DAYS   回放窗口（天），默认 365
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from src.core.backtest_engine import BacktestEngine
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)

MARKET_REVIEW_CODE = "market_review"
MAX_SIGNAL_ROWS = 2000
MIN_CASH_EPSILON = 1.0


def _env_float(name: str, default: float, *, minimum: Optional[float] = None) -> float:
    try:
        value = float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        value = default
    if minimum is not None and value < minimum:
        return default
    return value


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        value = default
    return value if value >= minimum else default


def is_paper_trading_enabled() -> bool:
    return (os.getenv("PAPER_TRADING_ENABLED", "true") or "true").strip().lower() == "true"


@dataclass
class PaperTrade:
    """一笔模拟成交记录。"""

    trade_date: date
    code: str
    name: str
    side: str  # buy / sell
    price: float
    value: float
    commission: float
    pnl_pct: Optional[float] = None  # 仅 sell 有值

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.trade_date.isoformat(),
            "code": self.code,
            "name": self.name,
            "side": self.side,
            "price": round(self.price, 4),
            "value": round(self.value, 2),
            "commission": round(self.commission, 2),
            "pnl_pct": round(self.pnl_pct, 2) if self.pnl_pct is not None else None,
        }


@dataclass
class _Position:
    code: str
    name: str
    shares: float
    entry_price: float
    entry_date: date


@dataclass
class _SignalDay:
    """某只股票某天的目标持仓状态（long/cash），由当天最新一条分析建议决定。"""

    signal_date: date
    code: str
    name: str
    target: str  # long / cash


@dataclass
class PaperPortfolioSummary:
    """模拟盘重放结果。"""

    start_date: date
    end_date: date
    initial_capital: float
    position_pct: float
    commission_pct: float
    final_nav: float
    total_return_pct: float
    baseline_return_pct: Optional[float]
    max_drawdown_pct: float
    nav_series: List[Dict[str, Any]] = field(default_factory=list)
    trades: List[PaperTrade] = field(default_factory=list)
    holdings: List[Dict[str, Any]] = field(default_factory=list)
    closed_trades: int = 0
    closed_win_rate_pct: Optional[float] = None

    @property
    def excess_return_pct(self) -> Optional[float]:
        if self.baseline_return_pct is None:
            return None
        return round(self.total_return_pct - self.baseline_return_pct, 2)


class PaperTradingService:
    """从分析历史确定性重放出虚拟组合。"""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def _load_signal_days(self, lookback_days: int) -> List[_SignalDay]:
        """加载分析历史并压缩成「每票每天一个目标状态」。"""
        try:
            rows = self.db.get_analysis_history(days=lookback_days, limit=MAX_SIGNAL_ROWS)
        except Exception as exc:
            logger.warning("模拟盘加载分析历史失败: %s", exc)
            return []

        # (code, date) -> 最新一条记录（rows 默认按时间倒序，第一条即最新）
        latest: Dict[tuple, _SignalDay] = {}
        for row in rows:
            code = str(getattr(row, "code", "") or "").strip()
            if not code or code == MARKET_REVIEW_CODE:
                continue
            if getattr(row, "report_type", None) == MARKET_REVIEW_CODE:
                continue
            created_at = getattr(row, "created_at", None)
            if created_at is None:
                continue
            advice = getattr(row, "operation_advice", None)
            if not advice:
                continue
            key = (code.upper(), created_at.date())
            if key in latest:
                continue
            recommendation = BacktestEngine.infer_position_recommendation(advice)
            latest[key] = _SignalDay(
                signal_date=created_at.date(),
                code=code.upper(),
                name=str(getattr(row, "name", "") or code),
                target="long" if recommendation == "long" else "cash",
            )
        signals = sorted(latest.values(), key=lambda s: (s.signal_date, s.code))
        return signals

    def _load_price_map(
        self, codes: List[str], start_date: date, end_date: date
    ) -> Dict[str, Dict[date, float]]:
        """加载每票的收盘价序列；代码格式差异通过回测同款候选列表兜底。"""
        from src.services.backtest_service import BacktestService

        price_map: Dict[str, Dict[date, float]] = {}
        for code in codes:
            candidates = BacktestService._build_daily_code_candidates(code) or [code]
            best: Dict[date, float] = {}
            for candidate in candidates:
                try:
                    bars = self.db.get_data_range(candidate, start_date, end_date)
                except Exception as exc:
                    logger.debug("模拟盘读取日线失败(%s): %s", candidate, exc)
                    continue
                series = {
                    bar.date: float(bar.close)
                    for bar in bars
                    if bar.close is not None and float(bar.close) > 0
                }
                if len(series) > len(best):
                    best = series
            if best:
                price_map[code] = best
            else:
                logger.debug("模拟盘无 %s 日线数据，跳过该标的", code)
        return price_map

    # ------------------------------------------------------------------
    # 重放
    # ------------------------------------------------------------------

    def simulate(self, *, lookback_days: Optional[int] = None) -> Optional[PaperPortfolioSummary]:
        """重放分析建议，返回模拟盘结果；无信号或无行情时返回 None。"""
        lookback = lookback_days or _env_int("PAPER_TRADING_LOOKBACK_DAYS", 365)
        initial_capital = _env_float("PAPER_TRADING_INITIAL_CAPITAL", 100000.0, minimum=1.0)
        position_pct = _env_float("PAPER_TRADING_POSITION_PCT", 10.0, minimum=0.1)
        commission_pct = _env_float("PAPER_TRADING_COMMISSION_PCT", 0.1, minimum=0.0)

        signals = self._load_signal_days(lookback)
        if not signals:
            return None

        start_date = signals[0].signal_date
        end_date = date.today()
        codes = sorted({s.code for s in signals})
        price_map = self._load_price_map(codes, start_date, end_date)
        if not price_map:
            return None

        # 交易日轴：所有标的日线日期的并集
        all_dates = sorted({d for series in price_map.values() for d in series})
        if not all_dates:
            return None

        # 每票未消费信号队列（升序）；信号到期后更新目标状态，
        # 在该股有可用日线的第一天完成调和（买/卖），避免行情晚于信号时丢单
        pending: Dict[str, List[_SignalDay]] = {}
        for signal in signals:
            pending.setdefault(signal.code, []).append(signal)

        cash = initial_capital
        positions: Dict[str, _Position] = {}
        trades: List[PaperTrade] = []
        nav_series: List[Dict[str, Any]] = []
        last_price: Dict[str, float] = {}
        target_state: Dict[str, _SignalDay] = {}
        dirty: set = set()
        # 等权买入持有基准：每票首个信号日的可用收盘价为基准入场价
        baseline_entry: Dict[str, float] = {}
        first_signal_date: Dict[str, date] = {
            code: queue[0].signal_date for code, queue in pending.items()
        }
        commission_rate = commission_pct / 100.0

        def mark_nav() -> float:
            value = cash
            for pos in positions.values():
                price = last_price.get(pos.code, pos.entry_price)
                value += pos.shares * price
            return value

        for current in all_dates:
            # 更新当日已知价格
            today_priced: set = set()
            for code, series in price_map.items():
                price = series.get(current)
                if price is not None:
                    last_price[code] = price
                    today_priced.add(code)
                    if code not in baseline_entry and first_signal_date.get(code) is not None \
                            and current >= first_signal_date[code]:
                        baseline_entry[code] = price

            # 消费到期信号 → 更新目标状态
            for code, queue in pending.items():
                while queue and queue[0].signal_date <= current:
                    target_state[code] = queue.pop(0)
                    dirty.add(code)

            # 调和：只在该股当日有行情时执行；先卖后买（腾出资金）
            actionable = sorted(code for code in dirty if code in today_priced)
            for code in actionable:
                signal = target_state[code]
                if signal.target == "cash":
                    pos = positions.get(code)
                    if pos is not None:
                        price = last_price[code]
                        value = pos.shares * price
                        fee = value * commission_rate
                        cash += value - fee
                        pnl_pct = (
                            (price / pos.entry_price - 1.0) * 100.0 if pos.entry_price else None
                        )
                        trades.append(
                            PaperTrade(
                                trade_date=current,
                                code=code,
                                name=signal.name,
                                side="sell",
                                price=price,
                                value=value,
                                commission=fee,
                                pnl_pct=pnl_pct,
                            )
                        )
                        del positions[code]
                    dirty.discard(code)

            for code in actionable:
                signal = target_state[code]
                if signal.target != "long":
                    continue
                if code in positions:
                    dirty.discard(code)
                    continue
                price = last_price[code]
                if price <= 0:
                    dirty.discard(code)
                    continue
                budget = min(mark_nav() * position_pct / 100.0, cash)
                if budget <= MIN_CASH_EPSILON:
                    # 资金不足：放弃该信号（不无限期挂单，避免远期以偏离价格成交）
                    dirty.discard(code)
                    continue
                fee = budget * commission_rate
                invest = budget - fee
                shares = invest / price
                cash -= budget
                positions[code] = _Position(
                    code=code,
                    name=signal.name,
                    shares=shares,
                    entry_price=price,
                    entry_date=current,
                )
                trades.append(
                    PaperTrade(
                        trade_date=current,
                        code=code,
                        name=signal.name,
                        side="buy",
                        price=price,
                        value=invest,
                        commission=fee,
                    )
                )
                dirty.discard(code)

            nav_series.append({"date": current.isoformat(), "nav": round(mark_nav(), 2)})

        final_nav = mark_nav()
        total_return_pct = (final_nav / initial_capital - 1.0) * 100.0

        baseline_return_pct = self._baseline_return_pct(baseline_entry, last_price)
        max_drawdown_pct = self._max_drawdown_pct([point["nav"] for point in nav_series])

        closed = [t for t in trades if t.side == "sell" and t.pnl_pct is not None]
        wins = sum(1 for t in closed if t.pnl_pct > 0)
        closed_win_rate = round(wins / len(closed) * 100.0, 1) if closed else None

        holdings = []
        for pos in positions.values():
            price = last_price.get(pos.code, pos.entry_price)
            holdings.append(
                {
                    "code": pos.code,
                    "name": pos.name,
                    "entry_date": pos.entry_date.isoformat(),
                    "entry_price": round(pos.entry_price, 4),
                    "last_price": round(price, 4),
                    "pnl_pct": round((price / pos.entry_price - 1.0) * 100.0, 2)
                    if pos.entry_price
                    else None,
                }
            )
        holdings.sort(key=lambda h: h["code"])

        return PaperPortfolioSummary(
            start_date=all_dates[0],
            end_date=all_dates[-1],
            initial_capital=initial_capital,
            position_pct=position_pct,
            commission_pct=commission_pct,
            final_nav=round(final_nav, 2),
            total_return_pct=round(total_return_pct, 2),
            baseline_return_pct=baseline_return_pct,
            max_drawdown_pct=max_drawdown_pct,
            nav_series=nav_series,
            trades=trades,
            holdings=holdings,
            closed_trades=len(closed),
            closed_win_rate_pct=closed_win_rate,
        )

    @staticmethod
    def _baseline_return_pct(
        baseline_entry: Dict[str, float], last_price: Dict[str, float]
    ) -> Optional[float]:
        ratios = [
            last_price[code] / entry
            for code, entry in baseline_entry.items()
            if entry > 0 and last_price.get(code)
        ]
        if not ratios:
            return None
        return round((sum(ratios) / len(ratios) - 1.0) * 100.0, 2)

    @staticmethod
    def _max_drawdown_pct(navs: List[float]) -> float:
        peak = float("-inf")
        max_dd = 0.0
        for nav in navs:
            peak = max(peak, nav)
            if peak > 0:
                max_dd = min(max_dd, (nav / peak - 1.0) * 100.0)
        return round(max_dd, 2)


def build_paper_trading_section(today: Optional[date] = None) -> Optional[str]:
    """生成周报「AI 模拟盘」Markdown 段；无数据或功能关闭时返回 None。"""
    if not is_paper_trading_enabled():
        return None
    try:
        summary = PaperTradingService().simulate()
    except Exception as exc:
        logger.warning("模拟盘重放失败: %s", exc)
        return None
    if summary is None:
        return None

    today = today or date.today()
    lines = [
        "## 🧪 AI 模拟盘（建议自动执行）",
        "",
        (
            f"- 期间: {summary.start_date.isoformat()} ~ {summary.end_date.isoformat()}"
            f"（初始资金 {summary.initial_capital:,.0f}，单票 {summary.position_pct:g}% 仓位，"
            f"单边手续费 {summary.commission_pct:g}%）"
        ),
    ]
    nav_line = (
        f"- 策略收益: {summary.total_return_pct:+.2f}%"
        f"（净值 {summary.final_nav / summary.initial_capital:.3f}）"
    )
    if summary.baseline_return_pct is not None:
        nav_line += (
            f" | 等权买入持有基准: {summary.baseline_return_pct:+.2f}%"
            f" | 超额: {summary.excess_return_pct:+.2f}pp"
        )
    lines.append(nav_line)
    stats_line = f"- 最大回撤: {summary.max_drawdown_pct:.2f}%"
    if summary.closed_trades:
        stats_line += (
            f" | 已平仓 {summary.closed_trades} 笔"
            f"，胜率 {summary.closed_win_rate_pct:.1f}%"
        )
    lines.append(stats_line)

    if summary.holdings:
        parts = [
            f"{h['name']}({h['code']}) {h['pnl_pct']:+.1f}%"
            if h.get("pnl_pct") is not None
            else f"{h['name']}({h['code']})"
            for h in summary.holdings[:8]
        ]
        lines.append(f"- 当前持仓（{len(summary.holdings)} 只）: " + "、".join(parts))
    else:
        lines.append("- 当前持仓: 空仓")

    recent = [t for t in summary.trades if (today - t.trade_date).days <= 7]
    if recent:
        parts = []
        for trade in recent[-6:]:
            label = "买入" if trade.side == "buy" else "卖出"
            extra = (
                f", {trade.pnl_pct:+.1f}%"
                if trade.side == "sell" and trade.pnl_pct is not None
                else ""
            )
            parts.append(
                f"{label} {trade.name}({trade.code}) @ {trade.price:g}"
                f" ({trade.trade_date.strftime('%m-%d')}{extra})"
            )
        lines.append("- 本周交易: " + "、".join(parts))

    lines.append("")
    lines.append(
        "> 模拟盘按每日分析建议确定性重放（偏多→持有，偏空/观望→空仓），"
        "信号日收盘价成交；仅用于度量建议质量，不构成投资建议。"
    )
    return "\n".join(lines)

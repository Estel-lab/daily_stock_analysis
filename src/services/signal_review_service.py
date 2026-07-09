# -*- coding: utf-8 -*-
"""
===================================
信号复盘摘要服务
===================================

职责：
1. 汇总自动回测（BacktestService）的整体表现指标；
2. 生成一段可直接推送的「信号复盘」Markdown 摘要（命中率、方向准确率、平均收益等）；
3. 无回测数据（历史不足 min_age_days）时返回 None，调用方静默跳过。

供 main.py 每日任务在配置的复盘日（默认周五）推送，让系统的建议质量可度量。
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "N/A"


def build_signal_review_text(summary: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    构建信号复盘摘要文本；无可用回测汇总时返回 None。

    Args:
        summary: 可选，直接传入 BacktestService.get_summary(scope="overall") 的结果（供测试）；
                 不传时实时查询。
    """
    if summary is None:
        try:
            from src.services.backtest_service import BacktestService

            summary = BacktestService().get_summary(scope="overall", code=None)
        except Exception as e:
            logger.debug("Signal review summary fetch failed: %s", e)
            return None

    if not summary:
        return None
    completed = summary.get("completed_count") or 0
    if completed <= 0:
        return None

    eval_window = summary.get("eval_window_days")
    window_str = f"{eval_window} 个交易日" if eval_window else "评估窗口内"
    lines = [
        "## 📊 信号复盘（历史建议表现）",
        "",
        f"- 已评估样本: {completed} 条（另有 {summary.get('insufficient_count') or 0} 条数据不足）",
        f"- 胜率: {_fmt_pct(summary.get('win_rate_pct'))} | 方向准确率: {_fmt_pct(summary.get('direction_accuracy_pct'))}",
        f"- 平均个股收益（{window_str}）: {_fmt_pct(summary.get('avg_stock_return_pct'))}"
        f" | 平均模拟策略收益: {_fmt_pct(summary.get('avg_simulated_return_pct'))}",
    ]
    advice_breakdown = summary.get("advice_breakdown") or {}
    if isinstance(advice_breakdown, dict) and advice_breakdown:
        parts = []
        for advice, item in list(advice_breakdown.items())[:4]:
            if isinstance(item, dict):
                parts.append(f"{advice} {_fmt_pct(item.get('win_rate_pct'))}")
        if parts:
            lines.append(f"- 分建议胜率: {' | '.join(parts)}")
    lines.append("")
    lines.append("> 胜率长期低于 50% 时应下调对报告建议的信任权重；样本 <20 条时结论仅供参考。")
    return "\n".join(lines)


def build_calibration_hint(code: str) -> Optional[str]:
    """
    构建个股历史校准提示（回测胜率反馈进分析 prompt）；无回测数据返回 None。
    """
    try:
        from src.services.backtest_service import BacktestService

        service = BacktestService()
        overall = service.get_summary(scope="overall", code=None) or {}
        stock = service.get_summary(scope="stock", code=code) or {}
    except Exception as e:
        logger.debug("Calibration hint fetch failed for %s: %s", code, e)
        return None

    parts = []
    if (overall.get("completed_count") or 0) > 0:
        parts.append(
            f"系统整体历史胜率 {_fmt_pct(overall.get('win_rate_pct'))}"
            f"（样本 {overall.get('completed_count')}）"
        )
    if (stock.get("completed_count") or 0) > 0:
        parts.append(
            f"本股历史胜率 {_fmt_pct(stock.get('win_rate_pct'))}"
            f"（样本 {stock.get('completed_count')}）"
        )
    if not parts:
        return None
    return (
        "📈 历史校准（本系统过往建议的回测表现）: "
        + "；".join(parts)
        + "。胜率明显低于 50% 时请下调建议置信度并偏保守。"
    )

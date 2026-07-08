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

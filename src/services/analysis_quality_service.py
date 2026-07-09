# -*- coding: utf-8 -*-
"""
分析质量守护：情绪-价格背离检测 与 LLM 点位幻觉校验

1. detect_sentiment_price_divergence: 历史评分趋势与短期价格方向的确定性背离检测，
   命中时生成提示行注入分析 prompt（比让 LLM 自行发现可靠）。
2. verify_report_price_levels: 对比 LLM 报告中的 MA 点位与实际计算值，
   偏差超阈值时返回警告文本供追加到风险提示。
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 评分上升/下降的判定阈值
SCORE_TREND_THRESHOLD = 10
# 价格相对 MA5 的强弱判定阈值（%）
PRICE_VS_MA5_THRESHOLD = 1.0
# LLM 点位与实际值的允许偏差（%）
LEVEL_DEVIATION_THRESHOLD_PCT = 2.0


def detect_sentiment_price_divergence(
    prev_scores: List[int],
    close: Optional[float],
    ma5: Optional[float],
) -> Optional[str]:
    """
    检测情绪与价格背离。prev_scores 为历史评分（最新在前，需 ≥2 条）。

    规则：近两次评分显著上升(≥10)但价格已走弱（close 低于 MA5 超阈值）→ 情绪超前于价格；
         评分显著下降但价格仍强（close 高于 MA5 超阈值）→ 情绪滞后/过度悲观。
    """
    if len(prev_scores) < 2 or close is None or ma5 is None or not ma5:
        return None
    score_delta = prev_scores[0] - prev_scores[1]
    price_vs_ma5_pct = (close - ma5) / ma5 * 100
    if score_delta >= SCORE_TREND_THRESHOLD and price_vs_ma5_pct <= -PRICE_VS_MA5_THRESHOLD:
        return (
            f"⚠️ 情绪-价格背离: 近两次情绪评分 +{score_delta}，但价格已跌破 MA5 "
            f"({price_vs_ma5_pct:+.1f}%)。情绪升温未获价格确认，警惕多头陷阱，建议偏保守。"
        )
    if score_delta <= -SCORE_TREND_THRESHOLD and price_vs_ma5_pct >= PRICE_VS_MA5_THRESHOLD:
        return (
            f"⚠️ 情绪-价格背离: 近两次情绪评分 {score_delta}，但价格仍站稳 MA5 上方 "
            f"({price_vs_ma5_pct:+.1f}%)。悲观情绪未获价格确认，可能是过度反应。"
        )
    return None


def verify_report_price_levels(
    dashboard: Optional[Dict[str, Any]],
    actual: Dict[str, Optional[float]],
) -> Optional[str]:
    """
    校验 LLM 报告点位（dashboard.data_perspective.price_position 的 ma5/ma10/ma20）
    与实际计算值的偏差；超过阈值返回警告文本，全部正常返回 None。
    """
    try:
        price_position = ((dashboard or {}).get("data_perspective") or {}).get("price_position") or {}
        problems = []
        for field in ("ma5", "ma10", "ma20"):
            actual_value = actual.get(field)
            reported = price_position.get(field)
            if actual_value is None or reported is None:
                continue
            try:
                reported_value = float(str(reported).replace("$", "").replace("元", "").strip())
            except (TypeError, ValueError):
                continue
            if actual_value <= 0:
                continue
            deviation_pct = abs(reported_value - actual_value) / actual_value * 100
            if deviation_pct > LEVEL_DEVIATION_THRESHOLD_PCT:
                problems.append(
                    f"{field.upper()} 报告值 {reported_value:.2f} vs 实际 {actual_value:.2f}"
                    f"（偏差 {deviation_pct:.1f}%）"
                )
        if problems:
            return "⚠️ 点位校验: " + "；".join(problems) + "。具体点位请以实际行情为准。"
        return None
    except Exception as e:
        logger.debug("Price level verification failed: %s", e)
        return None

# -*- coding: utf-8 -*-
"""
美股情绪指标（VIX）单元测试

覆盖：
1. 波动率指数不得混入 Market Light 指数涨跌均值（防止暴跌日 VIX 飙升把情绪分拉成强势）；
2. 情绪指标 Prompt 区块的档位解读与单日异动标记；
3. 无 VIX 数据时区块为空（fail-open）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

for optional_module in ("litellm", "json_repair", "newspaper", "google.generativeai", "google.genai", "anthropic"):
    try:
        __import__(optional_module)
    except ModuleNotFoundError:
        sys.modules[optional_module] = mock.MagicMock()

from src.core.market_profile import US_PROFILE
from src.market_analyzer import MarketAnalyzer, MarketIndex, MarketOverview


def _make_us_analyzer() -> MarketAnalyzer:
    ma = MarketAnalyzer.__new__(MarketAnalyzer)
    ma.profile = US_PROFILE
    ma.region = "us"
    cfg = mock.MagicMock()
    cfg.report_language = "zh"
    ma.config = cfg
    return ma


def _crash_day_overview() -> MarketOverview:
    """美股暴跌日：三大指数大跌，VIX 飙升"""
    return MarketOverview(
        date="2026-07-08",
        indices=[
            MarketIndex(code="^GSPC", name="标普500指数", current=5400, change_pct=-2.1),
            MarketIndex(code="^IXIC", name="纳斯达克综合指数", current=17000, change_pct=-2.8),
            MarketIndex(code="^DJI", name="道琼斯工业指数", current=40000, change_pct=-1.9),
            MarketIndex(code="^VIX", name="VIX恐慌指数", current=32.5, change_pct=+18.0),
        ],
    )


class TestVolatilityIndexExclusion(unittest.TestCase):
    def test_vix_excluded_from_index_score(self):
        ma = _make_us_analyzer()
        scores = ma._build_market_light_scores(_crash_day_overview())
        # 三大指数平均 -2.27%，若混入 VIX(+18%) 平均会变成 +2.8% → 分数 >80 完全反向
        self.assertLess(scores["dimensions"]["index"]["score"], 40)
        self.assertTrue(scores["dimensions"]["index"]["available"])

    def test_directional_changes_helper_filters_vix(self):
        ma = _make_us_analyzer()
        changes = ma._directional_index_changes(_crash_day_overview())
        self.assertEqual(len(changes), 3)
        self.assertNotIn(18.0, changes)


class TestSentimentIndicatorsBlock(unittest.TestCase):
    def test_panic_regime_with_spike_flag(self):
        ma = _make_us_analyzer()
        block = ma._build_sentiment_indicators_block(_crash_day_overview(), "zh")
        self.assertIn("## 情绪指标", block)
        self.assertIn("VIX恐慌指数: 32.50 (+18.00%)", block)
        self.assertIn("恐慌区间", block)
        self.assertIn("⚠️ 波动率单日异动", block)

    def test_calm_regime_no_spike(self):
        ma = _make_us_analyzer()
        overview = MarketOverview(
            date="2026-07-08",
            indices=[
                MarketIndex(code="^GSPC", name="标普500指数", current=5600, change_pct=0.4),
                MarketIndex(code="^VIX", name="VIX恐慌指数", current=13.2, change_pct=-2.0),
            ],
        )
        block = ma._build_sentiment_indicators_block(overview, "zh")
        self.assertIn("低波动区间", block)
        self.assertNotIn("⚠️", block)

    def test_english_variant(self):
        ma = _make_us_analyzer()
        block = ma._build_sentiment_indicators_block(_crash_day_overview(), "en")
        self.assertIn("## Sentiment Indicators", block)
        self.assertIn("panic zone", block)
        self.assertIn("One-day volatility spike", block)

    def test_empty_without_vix(self):
        ma = _make_us_analyzer()
        overview = MarketOverview(
            date="2026-07-08",
            indices=[MarketIndex(code="^GSPC", name="标普500指数", current=5600, change_pct=0.4)],
        )
        self.assertEqual(ma._build_sentiment_indicators_block(overview, "zh"), "")


if __name__ == "__main__":
    unittest.main()

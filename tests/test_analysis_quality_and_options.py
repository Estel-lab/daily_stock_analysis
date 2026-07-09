# -*- coding: utf-8 -*-
"""期权情绪、情绪-价格背离、点位校验测试"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pandas as pd

from src.services import options_sentiment_service as opt
from src.services.analysis_quality_service import (
    detect_sentiment_price_divergence,
    verify_report_price_levels,
)


class TestOptionsSentiment(unittest.TestCase):
    def setUp(self):
        opt.clear_cache()

    def _mock_yf(self, put_oi, call_oi):
        yf = mock.MagicMock()
        tk = yf.Ticker.return_value
        tk.options = ["2026-07-17"]
        chain = mock.MagicMock()
        chain.puts = pd.DataFrame({"openInterest": [put_oi], "volume": [100]})
        chain.calls = pd.DataFrame({"openInterest": [call_oi], "volume": [200]})
        tk.option_chain.return_value = chain
        return yf

    def test_bearish_hedging(self):
        with mock.patch.dict(sys.modules, {"yfinance": self._mock_yf(1500, 1000)}):
            line = opt.get_options_sentiment_line("AAPL", "Apple")
        self.assertIn("Put/Call 未平仓比 1.50", line)
        self.assertIn("防御需求偏高", line)

    def test_speculative_bullish(self):
        with mock.patch.dict(sys.modules, {"yfinance": self._mock_yf(500, 1000)}):
            line = opt.get_options_sentiment_line("NVDA")
        self.assertIn("投机看多情绪偏热", line)

    def test_failure_returns_none(self):
        yf = mock.MagicMock()
        yf.Ticker.side_effect = RuntimeError("down")
        with mock.patch.dict(sys.modules, {"yfinance": yf}):
            self.assertIsNone(opt.get_options_sentiment_line("AAPL"))


class TestDivergence(unittest.TestCase):
    def test_sentiment_up_price_weak(self):
        line = detect_sentiment_price_divergence([70, 55], close=98.0, ma5=100.0)
        self.assertIn("情绪-价格背离", line)
        self.assertIn("+15", line)
        self.assertIn("多头陷阱", line)

    def test_sentiment_down_price_strong(self):
        line = detect_sentiment_price_divergence([40, 60], close=103.0, ma5=100.0)
        self.assertIn("过度反应", line)

    def test_no_divergence(self):
        self.assertIsNone(detect_sentiment_price_divergence([60, 58], close=101.0, ma5=100.0))
        self.assertIsNone(detect_sentiment_price_divergence([70], close=98.0, ma5=100.0))


class TestLevelVerification(unittest.TestCase):
    def test_deviation_flagged(self):
        dashboard = {"data_perspective": {"price_position": {"ma5": "308", "ma10": 300.0}}}
        actual = {"ma5": 315.2, "ma10": 301.0, "ma20": None}
        warning = verify_report_price_levels(dashboard, actual)
        self.assertIn("MA5 报告值 308.00 vs 实际 315.20", warning)
        self.assertNotIn("MA10", warning)

    def test_all_ok_returns_none(self):
        dashboard = {"data_perspective": {"price_position": {"ma5": 100.5}}}
        self.assertIsNone(verify_report_price_levels(dashboard, {"ma5": 100.0}))

    def test_missing_data_silent(self):
        self.assertIsNone(verify_report_price_levels(None, {"ma5": 100.0}))
        self.assertIsNone(verify_report_price_levels({}, {}))


if __name__ == "__main__":
    unittest.main()

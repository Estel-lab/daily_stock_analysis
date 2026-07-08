# -*- coding: utf-8 -*-
"""
财报日历服务与信号复盘摘要服务单元测试
"""
import os
import sys
import unittest
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services import earnings_calendar_service as ecs
from src.services.signal_review_service import build_signal_review_text


class TestEarningsCalendar(unittest.TestCase):
    def setUp(self):
        ecs.clear_cache()

    def _mock_yf(self, earnings_dates):
        yf = mock.MagicMock()
        yf.Ticker.return_value.calendar = {"Earnings Date": earnings_dates}
        return yf

    def test_hint_within_window(self):
        target = date.today() + timedelta(days=10)
        with mock.patch.dict(sys.modules, {"yfinance": self._mock_yf([target])}):
            line = ecs.get_upcoming_earnings_line("AAPL", "Apple")
        self.assertIsNotNone(line)
        self.assertIn("📅 财报提示", line)
        self.assertIn("Apple(AAPL)", line)
        self.assertIn(target.isoformat(), line)
        self.assertIn("距今 10 天", line)

    def test_no_hint_beyond_window(self):
        target = date.today() + timedelta(days=30)
        with mock.patch.dict(sys.modules, {"yfinance": self._mock_yf([target])}):
            self.assertIsNone(ecs.get_upcoming_earnings_line("AAPL", "Apple"))

    def test_past_dates_ignored(self):
        past = date.today() - timedelta(days=5)
        with mock.patch.dict(sys.modules, {"yfinance": self._mock_yf([past])}):
            self.assertIsNone(ecs.get_upcoming_earnings_line("AAPL", "Apple"))

    def test_fetch_failure_returns_none(self):
        yf = mock.MagicMock()
        yf.Ticker.side_effect = RuntimeError("network down")
        with mock.patch.dict(sys.modules, {"yfinance": yf}):
            self.assertIsNone(ecs.get_upcoming_earnings_line("AAPL", "Apple"))

    def test_result_cached_per_ticker(self):
        target = date.today() + timedelta(days=3)
        yf = self._mock_yf([target])
        with mock.patch.dict(sys.modules, {"yfinance": yf}):
            ecs.get_upcoming_earnings_line("NVDA")
            ecs.get_upcoming_earnings_line("NVDA")
        self.assertEqual(yf.Ticker.call_count, 1)

    def test_string_date_coerced(self):
        target = (date.today() + timedelta(days=7)).isoformat()
        with mock.patch.dict(sys.modules, {"yfinance": self._mock_yf([target])}):
            line = ecs.get_upcoming_earnings_line("MU", "Micron")
        self.assertIsNotNone(line)
        self.assertIn("距今 7 天", line)


class TestSignalReview(unittest.TestCase):
    def test_builds_summary_text(self):
        summary = {
            "completed_count": 42,
            "insufficient_count": 6,
            "eval_window_days": 10,
            "win_rate_pct": 57.1,
            "direction_accuracy_pct": 61.9,
            "avg_stock_return_pct": 1.8,
            "avg_simulated_return_pct": 2.3,
            "advice_breakdown": {
                "买入": {"win_rate_pct": 60.0},
                "持有": {"win_rate_pct": 52.0},
            },
        }
        text = build_signal_review_text(summary)
        self.assertIsNotNone(text)
        self.assertIn("## 📊 信号复盘", text)
        self.assertIn("已评估样本: 42 条", text)
        self.assertIn("胜率: 57.1%", text)
        self.assertIn("方向准确率: 61.9%", text)
        self.assertIn("买入 60.0%", text)

    def test_none_without_completed_samples(self):
        self.assertIsNone(build_signal_review_text({"completed_count": 0}))
        self.assertIsNone(build_signal_review_text({}))

    def test_none_when_summary_fetch_fails(self):
        with mock.patch(
            "src.services.backtest_service.BacktestService",
            side_effect=RuntimeError("db missing"),
        ):
            self.assertIsNone(build_signal_review_text())


if __name__ == "__main__":
    unittest.main()

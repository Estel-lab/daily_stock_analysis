# -*- coding: utf-8 -*-
"""美股筹码分布估算器与 yfinance 行情字段补充测试"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.us_chip_estimator import estimate_chip_distribution


def _df(prices, volumes):
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=len(prices)).date,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
        "close": prices,
        "volume": volumes,
    })


class TestUsChipEstimator(unittest.TestCase):
    def test_price_above_all_history_means_high_profit(self):
        # 全部筹码在 100 附近成交，现价 150 → 获利比例接近 1
        chip = estimate_chip_distribution(_df([100.0] * 30, [1000] * 30), 150.0, "AAPL")
        self.assertIsNotNone(chip)
        self.assertGreater(chip.profit_ratio, 0.95)
        self.assertAlmostEqual(chip.avg_cost, 100.0, delta=2.0)
        self.assertEqual(chip.source, "yfinance_estimated")

    def test_price_below_all_history_means_low_profit(self):
        chip = estimate_chip_distribution(_df([100.0] * 30, [1000] * 30), 60.0, "MU")
        self.assertLess(chip.profit_ratio, 0.05)

    def test_recent_volume_weighted_higher(self):
        # 前 30 天在 100，后 30 天在 200，等量成交；时间衰减应使平均成本偏向 200
        prices = [100.0] * 30 + [200.0] * 30
        chip = estimate_chip_distribution(_df(prices, [1000] * 60), 210.0, "NVDA")
        self.assertGreater(chip.avg_cost, 150.0)

    def test_band_ordering(self):
        rng = np.random.default_rng(7)
        prices = list(100 + rng.normal(0, 10, 60).cumsum().clip(-50, 80))
        chip = estimate_chip_distribution(_df(prices, [1000] * 60), float(prices[-1]), "QQQ")
        self.assertIsNotNone(chip)
        self.assertLessEqual(chip.cost_90_low, chip.cost_70_low)
        self.assertLessEqual(chip.cost_70_high, chip.cost_90_high)
        self.assertGreaterEqual(chip.concentration_90, chip.concentration_70)

    def test_insufficient_data_returns_none(self):
        self.assertIsNone(estimate_chip_distribution(_df([100.0] * 5, [1000] * 5), 100.0, "AAPL"))
        self.assertIsNone(estimate_chip_distribution(_df([100.0] * 30, [1000] * 30), 0, "AAPL"))


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""AI 模拟盘服务单元测试（确定性重放、基准对比、fail-open）。"""

import os
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import Config
from src.services.paper_trading_service import (
    PaperTradingService,
    build_paper_trading_section,
)
from src.storage import AnalysisHistory, DatabaseManager, StockDaily

CODE = "600519"


class PaperTradingServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_paper_trading.db")
        self._original_env = {
            key: os.environ.get(key)
            for key in (
                "ENV_FILE",
                "DATABASE_PATH",
                "PAPER_TRADING_ENABLED",
                "PAPER_TRADING_INITIAL_CAPITAL",
                "PAPER_TRADING_POSITION_PCT",
                "PAPER_TRADING_COMMISSION_PCT",
                "PAPER_TRADING_LOOKBACK_DAYS",
            )
        }
        env_path = os.path.join(self._temp_dir.name, ".env")
        with open(env_path, "w", encoding="utf-8") as env_file:
            env_file.write("STOCK_LIST=600519\n")
        os.environ["ENV_FILE"] = env_path
        os.environ["DATABASE_PATH"] = self._db_path
        os.environ["PAPER_TRADING_INITIAL_CAPITAL"] = "100000"
        os.environ["PAPER_TRADING_POSITION_PCT"] = "10"
        os.environ["PAPER_TRADING_COMMISSION_PCT"] = "0"
        os.environ.pop("PAPER_TRADING_ENABLED", None)
        os.environ.pop("PAPER_TRADING_LOOKBACK_DAYS", None)

        Config._instance = None
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()

        # 近 10 天内的三根日线：100 -> 110 -> 120
        self.d0 = date.today() - timedelta(days=6)
        self.d1 = self.d0 + timedelta(days=1)
        self.d2 = self.d0 + timedelta(days=2)
        with self.db.get_session() as session:
            for target_date, close in ((self.d0, 100.0), (self.d1, 110.0), (self.d2, 120.0)):
                session.add(
                    StockDaily(
                        code=CODE,
                        date=target_date,
                        open=close,
                        high=close,
                        low=close,
                        close=close,
                        volume=1000,
                    )
                )
            session.commit()

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config._instance = None
        for key, value in self._original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._temp_dir.cleanup()

    def _add_analysis(self, advice: str, on_date: date, code: str = CODE) -> None:
        with self.db.get_session() as session:
            session.add(
                AnalysisHistory(
                    query_id=f"q-{code}-{on_date.isoformat()}-{advice}",
                    code=code,
                    name="贵州茅台",
                    report_type="simple",
                    sentiment_score=70,
                    operation_advice=advice,
                    analysis_summary="test",
                    created_at=datetime.combine(on_date, time(18, 0)),
                )
            )
            session.commit()

    def test_buy_then_sell_round_trip(self):
        self._add_analysis("买入", self.d0)
        self._add_analysis("卖出", self.d2)

        summary = PaperTradingService().simulate()
        self.assertIsNotNone(summary)
        self.assertEqual(len(summary.trades), 2)

        buy, sell = summary.trades
        self.assertEqual(buy.side, "buy")
        self.assertEqual(buy.price, 100.0)
        self.assertEqual(sell.side, "sell")
        self.assertEqual(sell.price, 120.0)
        self.assertAlmostEqual(sell.pnl_pct, 20.0, places=4)

        # 10% 仓位买入 10000（无手续费），120 卖出 → 净值 = 100000 + 10000 * 0.2
        self.assertAlmostEqual(summary.final_nav, 102000.0, places=2)
        self.assertAlmostEqual(summary.total_return_pct, 2.0, places=2)
        # 基准：首个信号日 100 入场，最后 120 → +20%
        self.assertAlmostEqual(summary.baseline_return_pct, 20.0, places=2)
        self.assertEqual(summary.holdings, [])
        self.assertEqual(summary.closed_trades, 1)
        self.assertAlmostEqual(summary.closed_win_rate_pct, 100.0, places=1)

    def test_wait_advice_stays_in_cash(self):
        self._add_analysis("观望", self.d0)
        summary = PaperTradingService().simulate()
        self.assertIsNotNone(summary)
        self.assertEqual(summary.trades, [])
        self.assertAlmostEqual(summary.final_nav, 100000.0, places=2)

    def test_signal_before_first_bar_defers_to_first_bar(self):
        # 信号在首根日线之前一天：应在首根可用日线（d0，收盘 100）成交，而不是丢单
        self._add_analysis("买入", self.d0 - timedelta(days=1))
        summary = PaperTradingService().simulate()
        self.assertIsNotNone(summary)
        self.assertEqual(len(summary.trades), 1)
        self.assertEqual(summary.trades[0].side, "buy")
        self.assertEqual(summary.trades[0].price, 100.0)
        self.assertEqual(len(summary.holdings), 1)
        self.assertAlmostEqual(summary.holdings[0]["pnl_pct"], 20.0, places=2)

    def test_hold_advice_keeps_position(self):
        self._add_analysis("买入", self.d0)
        self._add_analysis("持有", self.d1)
        summary = PaperTradingService().simulate()
        self.assertEqual(len(summary.trades), 1)
        self.assertEqual(len(summary.holdings), 1)

    def test_market_review_rows_ignored(self):
        self._add_analysis("买入", self.d0, code="market_review")
        summary = PaperTradingService().simulate()
        self.assertIsNone(summary)

    def test_empty_history_returns_none(self):
        self.assertIsNone(PaperTradingService().simulate())
        self.assertIsNone(build_paper_trading_section())

    def test_section_disabled_via_env(self):
        self._add_analysis("买入", self.d0)
        os.environ["PAPER_TRADING_ENABLED"] = "false"
        try:
            self.assertIsNone(build_paper_trading_section())
        finally:
            os.environ.pop("PAPER_TRADING_ENABLED", None)

    def test_section_markdown_contains_metrics(self):
        self._add_analysis("买入", self.d0)
        self._add_analysis("卖出", self.d2)
        section = build_paper_trading_section(today=self.d2)
        self.assertIsNotNone(section)
        self.assertIn("AI 模拟盘", section)
        self.assertIn("策略收益", section)
        self.assertIn("等权买入持有基准", section)
        self.assertIn("本周交易", section)

    def test_commission_applied(self):
        os.environ["PAPER_TRADING_COMMISSION_PCT"] = "1"
        try:
            self._add_analysis("买入", self.d0)
            summary = PaperTradingService().simulate()
        finally:
            os.environ["PAPER_TRADING_COMMISSION_PCT"] = "0"
        self.assertIsNotNone(summary)
        buy = summary.trades[0]
        # 预算 10000，1% 手续费 → 实际投入 9900
        self.assertAlmostEqual(buy.commission, 100.0, places=2)
        self.assertAlmostEqual(buy.value, 9900.0, places=2)


if __name__ == "__main__":
    unittest.main()

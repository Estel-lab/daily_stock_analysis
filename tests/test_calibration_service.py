# -*- coding: utf-8 -*-
"""建议校准服务单元测试（分层胜率统计、Prompt 提示、周报段）。"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import Config
from src.services.calibration_service import (
    MIN_BUCKET_SAMPLES,
    build_calibration_report,
    build_market_calibration_hint,
    compute_calibration_stats,
)
from src.storage import AnalysisHistory, BacktestResult, DatabaseManager


class CalibrationServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._temp_dir.name, "test_calibration.db")
        self._original_env = {
            key: os.environ.get(key) for key in ("ENV_FILE", "DATABASE_PATH")
        }
        env_path = os.path.join(self._temp_dir.name, ".env")
        with open(env_path, "w", encoding="utf-8") as env_file:
            env_file.write("STOCK_LIST=600519\n")
        os.environ["ENV_FILE"] = env_path
        os.environ["DATABASE_PATH"] = self._db_path

        Config._instance = None
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config._instance = None
        for key, value in self._original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._temp_dir.cleanup()

    def _seed(self, *, code: str, advice: str, outcome: str, model: str, index: int) -> None:
        analysis_date = date.today() - timedelta(days=30 + index)
        with self.db.get_session() as session:
            history = AnalysisHistory(
                query_id=f"q-{code}-{index}",
                code=code,
                name=code,
                report_type="simple",
                sentiment_score=70,
                operation_advice=advice,
                analysis_summary="test",
                raw_result=json.dumps({"model_used": model}, ensure_ascii=False),
                created_at=datetime.now() - timedelta(days=30 + index),
            )
            session.add(history)
            session.flush()
            session.add(
                BacktestResult(
                    analysis_history_id=history.id,
                    code=code,
                    analysis_date=analysis_date,
                    eval_window_days=10,
                    engine_version="v1",
                    eval_status="completed",
                    evaluated_at=datetime.now(),
                    operation_advice=advice,
                    outcome=outcome,
                    stock_return_pct=3.0 if outcome == "win" else -3.0,
                )
            )
            session.commit()

    def _seed_bucket(self, *, code: str, advice: str, model: str, wins: int, losses: int, offset: int = 0) -> None:
        for i in range(wins):
            self._seed(code=code, advice=advice, outcome="win", model=model, index=offset + i)
        for i in range(losses):
            self._seed(
                code=code, advice=advice, outcome="loss", model=model, index=offset + wins + i
            )

    def test_empty_returns_none(self):
        self.assertIsNone(compute_calibration_stats())
        self.assertIsNone(build_calibration_report())
        self.assertIsNone(build_market_calibration_hint("600519"))

    def test_layered_stats(self):
        # A股 买入：4 胜 2 负；美股 卖出：1 胜 5 负
        self._seed_bucket(code="600519", advice="买入", model="gemini/g2", wins=4, losses=2)
        self._seed_bucket(code="AAPL", advice="卖出", model="openai/gpt", wins=1, losses=5, offset=100)

        stats = compute_calibration_stats()
        self.assertIsNotNone(stats)
        self.assertEqual(stats["overall"]["completed"], 12)

        cn = stats["by_market"]["cn"]
        self.assertEqual(cn["completed"], 6)
        self.assertAlmostEqual(cn["win_rate_pct"], 66.67, places=1)

        us = stats["by_market"]["us"]
        self.assertEqual(us["completed"], 6)
        self.assertAlmostEqual(us["win_rate_pct"], 16.67, places=1)

        self.assertIn("买入", stats["by_advice"])
        self.assertIn("gemini/g2", stats["by_model"])
        self.assertEqual(stats["by_model"]["gemini/g2"]["completed"], 6)

    def test_market_hint_requires_min_samples(self):
        self._seed_bucket(code="600519", advice="买入", model="m", wins=2, losses=1)
        self.assertLess(3, MIN_BUCKET_SAMPLES)
        self.assertIsNone(build_market_calibration_hint("600519"))

    def test_market_hint_content(self):
        self._seed_bucket(code="600519", advice="买入", model="m", wins=4, losses=2)
        hint = build_market_calibration_hint("600519")
        self.assertIsNotNone(hint)
        self.assertIn("A股市场历史胜率", hint)
        self.assertIn("66.7%", hint)
        self.assertIn("偏多类建议整体胜率", hint)
        # 其他市场的股票不应命中 A股层
        self.assertIsNone(build_market_calibration_hint("AAPL"))

    def test_calibration_report_markdown(self):
        self._seed_bucket(code="600519", advice="买入", model="gemini/g2", wins=4, losses=2)
        self._seed_bucket(code="AAPL", advice="卖出", model="openai/gpt", wins=1, losses=5, offset=100)
        report = build_calibration_report()
        self.assertIsNotNone(report)
        self.assertIn("建议校准报告", report)
        self.assertIn("分市场", report)
        self.assertIn("分建议", report)
        self.assertIn("分模型", report)
        self.assertIn("gemini/g2", report)

    def test_calibration_hint_integration(self):
        """signal_review_service.build_calibration_hint 应追加分层提示。"""
        from src.services.signal_review_service import build_calibration_hint

        self._seed_bucket(code="600519", advice="买入", model="m", wins=4, losses=2)
        # build_calibration_hint 需要 BacktestSummary（整体胜率），此处直接跑一次汇总
        from src.services.backtest_service import BacktestService

        BacktestService()._recompute_summaries(
            touched_codes=["600519"], eval_window_days=10, engine_version="v1"
        )
        hint = build_calibration_hint("600519")
        self.assertIsNotNone(hint)
        self.assertIn("历史校准", hint)
        self.assertIn("A股市场历史胜率", hint)


if __name__ == "__main__":
    unittest.main()

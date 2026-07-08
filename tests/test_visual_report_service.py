# -*- coding: utf-8 -*-
"""可视化 HTML 报告服务单元测试"""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.visual_report_service import build_visual_report_html


def _result(code, name, score, advice, decision, trend="看多"):
    return SimpleNamespace(
        code=code, name=name, sentiment_score=score,
        operation_advice=advice, decision_type=decision, trend_prediction=trend,
    )


class TestVisualReport(unittest.TestCase):
    def test_contains_charts_tables_and_data(self):
        results = [
            _result("AAPL", "Apple", 78, "买入", "buy"),
            _result("MU", "Micron", 35, "减仓", "sell", "看空"),
        ]
        history = {"AAPL": [{"created_at": "2026-07-07T05:30:00", "sentiment_score": 50}]}
        html = build_visual_report_html(results, history, report_date="2026-07-08")

        self.assertIn("chart-scores", html)
        self.assertIn("chart-history", html)          # 有历史时输出趋势图
        self.assertIn("<table>", html)                # 可访问性数据表
        self.assertIn("Apple", html)
        self.assertIn('"score": 78', html)
        self.assertIn("2026-07-07", html)             # 历史点进入数据
        self.assertIn("prefers-color-scheme: dark", html)

    def test_no_history_placeholder(self):
        html = build_visual_report_html(
            [_result("QQQ", "Invesco QQQ", 55, "持有", "hold")], {}, report_date="2026-07-08"
        )
        self.assertNotIn('id="chart-history"', html)
        self.assertIn("历史数据积累中", html)

    def test_script_close_escaped(self):
        results = [_result("AAPL", "Evil</script><b>", 50, "持有", "hold")]
        html = build_visual_report_html(results, {}, report_date="2026-07-08")
        payload = html.split('id="report-data"')[1]
        self.assertNotIn("</script><b>", payload.split("</script>")[0])

    def test_empty_results(self):
        html = build_visual_report_html([], {}, report_date="2026-07-08")
        self.assertIn("2026-07-08", html)


class TestScoreBar(unittest.TestCase):
    def test_bar_rendering(self):
        for optional_module in ("litellm", "json_repair"):
            import unittest.mock as m
            try:
                __import__(optional_module)
            except ModuleNotFoundError:
                sys.modules[optional_module] = m.MagicMock()
        from src.notification import NotificationService

        self.assertEqual(NotificationService._score_bar(78), "▰▰▰▰▰▰▰▰▱▱")
        self.assertEqual(NotificationService._score_bar(0), "▱▱▱▱▱▱▱▱▱▱")
        self.assertEqual(NotificationService._score_bar(100), "▰▰▰▰▰▰▰▰▰▰")
        self.assertEqual(NotificationService._score_bar(None), "")


if __name__ == "__main__":
    unittest.main()

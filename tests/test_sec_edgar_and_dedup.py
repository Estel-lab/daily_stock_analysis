# -*- coding: utf-8 -*-
"""SEC EDGAR 公告服务、跨引擎去重与历史校准提示测试"""
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

from datetime import date, timedelta

from src.services import sec_edgar_service as sec
from src.services.signal_review_service import build_calibration_hint
from src.search_service import SearchService, SearchResponse, SearchResult, _normalize_news_keys


class TestSecEdgar(unittest.TestCase):
    def setUp(self):
        sec.clear_cache()

    def _mock_get(self, filings):
        def fake_get(url, **kwargs):
            resp = mock.MagicMock()
            resp.status_code = 200
            if "company_tickers" in url:
                resp.json.return_value = {"0": {"ticker": "AAPL", "cik_str": 320193}}
            else:
                resp.json.return_value = {"filings": {"recent": filings}}
            return resp
        return fake_get

    def test_recent_8k_and_form4_summarized(self):
        today = date.today().isoformat()
        filings = {"form": ["8-K", "4", "4", "10-Q", "S-8"],
                   "filingDate": [today] * 5}
        with mock.patch.object(sec, "_get", side_effect=self._mock_get(filings)):
            line = sec.get_recent_filings_line("AAPL", "Apple")
        self.assertIsNotNone(line)
        self.assertIn("8-K(重大事件)", line)
        self.assertIn("Form 4(内部人交易) x2", line)
        self.assertIn("10-Q(季报)", line)
        self.assertNotIn("S-8", line)

    def test_old_filings_ignored(self):
        old_date = (date.today() - timedelta(days=30)).isoformat()
        filings = {"form": ["8-K"], "filingDate": [old_date]}
        with mock.patch.object(sec, "_get", side_effect=self._mock_get(filings)):
            self.assertIsNone(sec.get_recent_filings_line("AAPL"))

    def test_network_failure_returns_none(self):
        with mock.patch.object(sec, "_get", side_effect=RuntimeError("down")):
            self.assertIsNone(sec.get_recent_filings_line("AAPL"))


class TestCrossEngineDedup(unittest.TestCase):
    def _resp(self, provider, items):
        return SearchResponse(
            query="q", provider=provider, success=True,
            results=[SearchResult(title=t, url=u, snippet="s" * 10, source=provider) for t, u in items],
        )

    def test_same_story_across_dimensions_kept_once(self):
        svc = object.__new__(SearchService)
        intel = {
            "latest_news": self._resp("Tavily", [("Apple beats earnings estimates", "https://a.com/1")]),
            "market_analysis": self._resp("SerpAPI", [
                ("Apple beats earnings estimates", "https://b.com/2"),  # 同标题不同链接
                ("Fresh unique analysis", "https://c.com/3"),
            ]),
        }
        out = SearchService.format_intel_report(svc, intel, "Apple")
        self.assertEqual(out.count("Apple beats earnings estimates"), 1)
        self.assertIn("Fresh unique analysis", out)

    def test_normalize_key_ignores_query_params(self):
        self.assertEqual(
            _normalize_news_keys("t", "https://a.com/x?utm=1")[0],
            _normalize_news_keys("t2", "https://a.com/x/")[0],
        )


class TestCalibrationHint(unittest.TestCase):
    def test_hint_with_data(self):
        fake = mock.MagicMock()
        fake.get_summary.side_effect = [
            {"completed_count": 30, "win_rate_pct": 58.0},
            {"completed_count": 4, "win_rate_pct": 75.0},
        ]
        with mock.patch("src.services.backtest_service.BacktestService", return_value=fake):
            hint = build_calibration_hint("AAPL")
        self.assertIn("整体历史胜率 58.0%", hint)
        self.assertIn("本股历史胜率 75.0%", hint)

    def test_silent_without_data(self):
        fake = mock.MagicMock()
        fake.get_summary.return_value = {"completed_count": 0}
        with mock.patch("src.services.backtest_service.BacktestService", return_value=fake):
            self.assertIsNone(build_calibration_hint("AAPL"))


if __name__ == "__main__":
    unittest.main()

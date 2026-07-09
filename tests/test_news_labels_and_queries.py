# -*- coding: utf-8 -*-
"""新闻时效/来源分层标注与美股英文查询测试"""
import os, sys, unittest
from datetime import datetime
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from unittest import mock
for optional_module in ("litellm", "json_repair", "newspaper", "google.generativeai", "google.genai", "anthropic"):
    try:
        __import__(optional_module)
    except ModuleNotFoundError:
        sys.modules[optional_module] = mock.MagicMock()
from src.search_service import news_freshness_label, news_source_tier_label
from src.core.market_profile import US_PROFILE


class TestNewsLabels(unittest.TestCase):
    def test_freshness_hours_and_days(self):
        now = datetime(2026, 7, 9, 12, 0, 0)
        self.assertEqual(news_freshness_label("2026-07-09 09:00:00", now), "3小时前")
        self.assertEqual(news_freshness_label("2026-07-07", now), "2天前")
        self.assertEqual(news_freshness_label(None, now), "")
        self.assertEqual(news_freshness_label("garbage", now), "")

    def test_source_tier(self):
        self.assertEqual(news_source_tier_label("Bloomberg"), "权威源")
        self.assertEqual(news_source_tier_label(None, "https://www.reuters.com/markets/x"), "权威源")
        self.assertEqual(news_source_tier_label("Some Blog", "https://blog.example.com"), "")


class TestUsProfileQueries(unittest.TestCase):
    def test_us_queries_are_english(self):
        for q in US_PROFILE.news_queries:
            self.assertFalse(any('一' <= ch <= '鿿' for ch in q), q)


if __name__ == "__main__":
    unittest.main()

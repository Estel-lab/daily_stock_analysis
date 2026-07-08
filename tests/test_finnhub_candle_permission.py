# -*- coding: utf-8 -*-
"""Finnhub 免费 plan 无 candle 权限（403）时的跳过行为测试"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.base import DataFetchError
from data_provider.finnhub_fetcher import FinnhubFetcher


def _make_fetcher() -> FinnhubFetcher:
    fetcher = FinnhubFetcher.__new__(FinnhubFetcher)
    fetcher._api_key = "test-key"
    fetcher._candle_access_denied = False
    fetcher.random_sleep = lambda *a, **k: None
    return fetcher


class TestFinnhubCandlePermission(unittest.TestCase):
    @mock.patch("data_provider.finnhub_fetcher.requests.get")
    def test_403_sets_denied_and_skips_next_calls(self, mock_get):
        resp = mock.MagicMock()
        resp.status_code = 403
        mock_get.return_value = resp
        fetcher = _make_fetcher()

        with self.assertRaises(DataFetchError) as ctx:
            fetcher._fetch_raw_data("AAPL", "2026-05-01", "2026-07-08")
        self.assertIn("no candle permission", str(ctx.exception))
        self.assertTrue(fetcher._candle_access_denied)

        # 后续股票不再发出 HTTP 请求，直接快速失败进入下一数据源
        with self.assertRaises(DataFetchError):
            fetcher._fetch_raw_data("NVDA", "2026-05-01", "2026-07-08")
        self.assertEqual(mock_get.call_count, 1)

    @mock.patch("data_provider.finnhub_fetcher.requests.get")
    def test_success_path_unaffected(self, mock_get):
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "s": "ok", "c": [10.0], "h": [11.0], "l": [9.0],
            "o": [9.5], "t": [1751932800], "v": [1000],
        }
        resp.raise_for_status.return_value = None
        mock_get.return_value = resp
        fetcher = _make_fetcher()

        df = fetcher._fetch_raw_data("AAPL", "2026-07-01", "2026-07-08")
        self.assertFalse(fetcher._candle_access_denied)
        self.assertEqual(len(df), 1)


if __name__ == "__main__":
    unittest.main()

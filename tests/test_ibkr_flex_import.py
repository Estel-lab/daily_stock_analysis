# -*- coding: utf-8 -*-
"""IBKR Flex 成交导入：XML 解析、CSV 转换与 ibkr 券商解析器端到端。"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "import_ibkr_flex.py"
_spec = importlib.util.spec_from_file_location("import_ibkr_flex", _SCRIPT)
ibkr_flex = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ibkr_flex)

FIXTURE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="dsa-trades" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U1234567" fromDate="20260701" toDate="20260708">
      <Trades>
        <Trade assetCategory="STK" symbol="AAPL" tradeDate="20260702"
               buySell="BUY" quantity="100" tradePrice="212.34"
               tradeID="1111" currency="USD"/>
        <Trade assetCategory="STK" symbol="NVDA" tradeDate="20260707"
               buySell="SELL" quantity="-50" tradePrice="181.20"
               tradeID="2222" currency="USD"/>
        <Trade assetCategory="OPT" symbol="AAPL 260918C00230000" tradeDate="20260707"
               buySell="BUY" quantity="1" tradePrice="5.10"
               tradeID="3333" currency="USD"/>
        <Trade assetCategory="STK" symbol="" tradeDate="20260707"
               buySell="BUY" quantity="10" tradePrice="1.00"
               tradeID="4444" currency="USD"/>
      </Trades>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""


class TestParseTrades(unittest.TestCase):
    def test_extracts_stock_trades_and_skips_others(self):
        rows, skipped = ibkr_flex.parse_trades(FIXTURE_XML)
        self.assertEqual(len(rows), 2)
        self.assertEqual(skipped, 2)  # 期权 1 条 + 空 symbol 1 条

    def test_sell_quantity_normalized_to_absolute(self):
        rows, _ = ibkr_flex.parse_trades(FIXTURE_XML)
        nvda = next(r for r in rows if r["Symbol"] == "NVDA")
        self.assertEqual(float(nvda["Quantity"]), 50.0)
        self.assertEqual(nvda["Buy/Sell"], "SELL")


class TestIbkrParserEndToEnd(unittest.TestCase):
    def test_csv_flows_through_ibkr_broker_parser(self):
        from src.services.portfolio_import_service import PortfolioImportService

        rows, _ = ibkr_flex.parse_trades(FIXTURE_XML)
        csv_text = ibkr_flex.rows_to_csv(rows)

        service = PortfolioImportService.__new__(PortfolioImportService)
        service._parser_registry = {}
        service._broker_alias_map = {}
        service._init_default_parsers()

        parsed = service.parse_trade_csv(broker="ibkr", content=csv_text.encode("utf-8"))
        self.assertEqual(parsed["broker"], "ibkr")
        self.assertEqual(parsed["record_count"], 2)
        self.assertEqual(parsed["error_count"], 0)

        by_symbol = {r["symbol"]: r for r in parsed["records"]}
        self.assertEqual(by_symbol["AAPL"]["side"], "buy")
        self.assertEqual(by_symbol["NVDA"]["side"], "sell")
        self.assertEqual(str(by_symbol["AAPL"]["trade_date"]), "2026-07-02")
        self.assertAlmostEqual(by_symbol["NVDA"]["price"], 181.20)
        self.assertEqual(by_symbol["NVDA"]["quantity"], 50)

    def test_ibkr_alias_registered(self):
        from src.services.portfolio_import_service import PortfolioImportService

        service = PortfolioImportService.__new__(PortfolioImportService)
        service._parser_registry = {}
        service._broker_alias_map = {}
        service._init_default_parsers()
        brokers = {b["broker"]: b for b in service.list_supported_brokers()}
        self.assertIn("ibkr", brokers)
        self.assertIn("ib", brokers["ibkr"]["aliases"])


if __name__ == "__main__":
    unittest.main()

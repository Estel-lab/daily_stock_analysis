# -*- coding: utf-8 -*-
"""选股筛大盘红绿灯联动：市场推断、红灯降级与消息渲染。"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "screener_notify.py"
_spec = importlib.util.spec_from_file_location("screener_notify_ml", _SCRIPT)
sn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sn)


def _buy(ticker, market, grade="BUY_5%"):
    return {
        "ticker": ticker,
        "market": market,
        "grade": grade,
        "reason": "标准仓（4/6）",
        "advice": "建议5%仓位",
        "momentum": {"close": 100.0, "pct_30d": 12.0, "vol_ratio": 2.0},
        "value": None,
    }


class TestInferMarket(unittest.TestCase):
    def test_market_inference(self):
        self.assertEqual(sn.infer_market("600519.SS"), "cn")
        self.assertEqual(sn.infer_market("000001.SZ"), "cn")
        self.assertEqual(sn.infer_market("0700.HK"), "hk")
        self.assertEqual(sn.infer_market("NVDA"), "us")


class TestApplyMarketLight(unittest.TestCase):
    def test_red_light_demotes_only_that_market(self):
        buy = [_buy("NVDA", "us"), _buy("0700.HK", "hk")]
        watch = []
        lights = {
            "us": {"status": "red", "label": "红灯", "score": 20},
            "hk": {"status": "green", "label": "绿灯", "score": 80},
        }
        kept, watch_out, notes = sn.apply_market_light(buy, watch, lights)
        self.assertEqual([s["ticker"] for s in kept], ["0700.HK"])
        self.assertEqual(len(watch_out), 1)
        demoted = watch_out[0]
        self.assertEqual(demoted["grade"], "WATCH")
        self.assertEqual(demoted["raw_grade"], "BUY_5%")
        self.assertTrue(demoted["demoted_by_light"])
        self.assertTrue(any("降级" in n for n in notes))

    def test_green_and_yellow_keep_signals(self):
        buy = [_buy("NVDA", "us")]
        lights = {"us": {"status": "yellow", "label": "黄灯", "score": 55}}
        kept, watch_out, notes = sn.apply_market_light(buy, [], lights)
        self.assertEqual(len(kept), 1)
        self.assertEqual(watch_out, [])
        self.assertTrue(any("黄灯" in n or "🟡" in n for n in notes))

    def test_no_lights_passthrough(self):
        buy = [_buy("NVDA", "us")]
        kept, watch_out, notes = sn.apply_market_light(buy, [], {})
        self.assertEqual(kept, buy)
        self.assertEqual(notes, [])


class TestLightFetchToggle(unittest.TestCase):
    def test_disabled_by_env(self):
        with mock.patch.dict(os.environ, {"SCREENER_MARKET_LIGHT_ENABLED": "false"}):
            self.assertEqual(sn.fetch_market_lights({"us"}), {})

    def test_fetch_failure_fail_open(self):
        # 用 sys.modules 桩替换服务模块，避免测试环境加载真实数据源依赖
        stub = mock.MagicMock()
        stub.build_current_snapshot.side_effect = RuntimeError("data source down")
        with mock.patch.dict(os.environ, {"SCREENER_MARKET_LIGHT_ENABLED": "true"}), \
                mock.patch.dict(sys.modules, {"src.services.market_light_service": stub}):
            self.assertEqual(sn.fetch_market_lights({"us"}), {})


class TestMessageRendering(unittest.TestCase):
    def test_light_notes_rendered_and_demoted_signal_in_watch(self):
        buy = [_buy("NVDA", "us")]
        lights = {"us": {"status": "red", "label": "红灯·谨慎", "score": 20}}
        kept, watch_out, notes = sn.apply_market_light(buy, [], lights)
        msg = sn.build_message(kept, watch_out, 1, [], light_notes=notes)
        self.assertIn("🔴 美股大盘：红灯·谨慎", msg)
        self.assertIn("无买入信号", msg)
        self.assertIn("大盘红灯降级（原 BUY_5%", msg)
        self.assertIn("红灯环境不建议追突破", msg)

    def test_history_entry_records_raw_grade_and_market(self):
        import json
        import tempfile
        buy = [_buy("NVDA", "us")]
        lights = {"us": {"status": "red", "label": "红灯", "score": 20}}
        _, watch_out, _ = sn.apply_market_light(buy, [], lights)
        path = Path(tempfile.mkdtemp()) / "h.jsonl"
        sn.append_signal_history([], watch_out, path, signal_date="2026-07-10")
        entry = json.loads(path.read_text().strip())
        self.assertEqual(entry["grade"], "WATCH")
        self.assertEqual(entry["raw_grade"], "BUY_5%")
        self.assertEqual(entry["market"], "us")


if __name__ == "__main__":
    unittest.main()

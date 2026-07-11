# -*- coding: utf-8 -*-
"""多空对抗分析服务单元测试（裁决解析、fail-open、报告格式化）。"""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.debate_service import (
    DebateOutcome,
    build_context_digest,
    format_debate_block,
    is_debate_enabled,
    run_debate_analysis,
)


class FakeLLM:
    """按调用顺序返回 bull / bear / judge 三段回复的假适配器。"""

    is_available = True

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def call_text(self, messages, **kwargs):
        self.calls.append(messages)
        content = self.replies.pop(0) if self.replies else None
        return SimpleNamespace(content=content, provider="gemini", model="gemini/g2")


JUDGE_JSON = '{"verdict": "bull", "confidence": 72, "summary": "多方数据更扎实"}'


class DebateServiceTestCase(unittest.TestCase):
    def _run(self, replies, **kwargs):
        llm = FakeLLM(replies)
        outcome = run_debate_analysis(
            code="600519",
            stock_name="贵州茅台",
            enhanced_context={
                "today": {"close": 100, "ma5": 98, "ma10": 96, "ma20": 95, "pct_chg": 1.2},
                "realtime": {"price": 101, "change_pct": 1.0},
            },
            news_context="利好消息" * 10,
            operation_advice="买入",
            sentiment_score=75,
            llm=llm,
            **kwargs,
        )
        return outcome, llm

    def test_happy_path(self):
        outcome, llm = self._run(["1. 多方论点", "1. 空方论点", JUDGE_JSON])
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.verdict, "bull")
        self.assertEqual(outcome.confidence, 72)
        self.assertEqual(outcome.bull_case, "1. 多方论点")
        self.assertEqual(outcome.bear_case, "1. 空方论点")
        self.assertEqual(outcome.judge_summary, "多方数据更扎实")
        self.assertEqual(outcome.model_used, "gemini/g2")
        self.assertEqual(len(llm.calls), 3)
        # 裁判 prompt 中应同时包含双方论证
        judge_prompt = llm.calls[2][1]["content"]
        self.assertIn("多方论点", judge_prompt)
        self.assertIn("空方论点", judge_prompt)

    def test_judge_json_in_code_fence(self):
        fenced = f"结论如下：\n```json\n{JUDGE_JSON}\n```"
        outcome, _ = self._run(["bull case", "bear case", fenced])
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.verdict, "bull")

    def test_invalid_verdict_returns_none(self):
        bad = '{"verdict": "sideways", "confidence": 50, "summary": "?"}'
        outcome, _ = self._run(["bull", "bear", bad])
        self.assertIsNone(outcome)

    def test_judge_parse_failure_returns_none(self):
        outcome, _ = self._run(["bull", "bear", "无法给出 JSON"])
        self.assertIsNone(outcome)

    def test_bull_case_failure_returns_none(self):
        outcome, llm = self._run([None, "bear", JUDGE_JSON])
        self.assertIsNone(outcome)
        self.assertEqual(len(llm.calls), 1)

    def test_unavailable_llm_returns_none(self):
        llm = FakeLLM([])
        llm.is_available = False
        outcome = run_debate_analysis(
            code="600519", stock_name="贵州茅台", llm=llm
        )
        self.assertIsNone(outcome)
        self.assertEqual(llm.calls, [])

    def test_confidence_clamped(self):
        over = '{"verdict": "bear", "confidence": 250, "summary": "s"}'
        outcome, _ = self._run(["bull", "bear", over])
        self.assertEqual(outcome.confidence, 100)

    def test_context_digest_contains_data(self):
        digest = build_context_digest(
            code="600519",
            stock_name="贵州茅台",
            enhanced_context={"today": {"close": 100, "ma5": 98}},
            news_context="x" * 5000,
            operation_advice="买入",
            sentiment_score=75,
        )
        self.assertIn("贵州茅台(600519)", digest)
        self.assertIn("收盘 100", digest)
        self.assertIn("建议 买入", digest)
        self.assertIn("已截断", digest)

    def test_format_debate_block(self):
        outcome = DebateOutcome(
            verdict="bull",
            confidence=72,
            bull_case="- 论点A\n- 论点B",
            bear_case="- 论点C",
            judge_summary="多方更扎实",
            model_used="gemini/g2",
        )
        block = format_debate_block(outcome)
        self.assertIn("多空对抗裁决: 偏多（置信度 72/100）", block)
        self.assertIn("裁决要点: 多方更扎实", block)
        self.assertIn("论点A / 论点B", block)
        self.assertIn("不改变上方评分与操作建议", block)

    def test_is_debate_enabled_from_config(self):
        self.assertTrue(is_debate_enabled(SimpleNamespace(debate_analysis_enabled=True)))
        self.assertFalse(is_debate_enabled(SimpleNamespace(debate_analysis_enabled=False)))

    def test_is_debate_enabled_from_env_default_off(self):
        original = os.environ.pop("DEBATE_ANALYSIS_ENABLED", None)
        try:
            self.assertFalse(is_debate_enabled())
            os.environ["DEBATE_ANALYSIS_ENABLED"] = "true"
            self.assertTrue(is_debate_enabled())
        finally:
            if original is None:
                os.environ.pop("DEBATE_ANALYSIS_ENABLED", None)
            else:
                os.environ["DEBATE_ANALYSIS_ENABLED"] = original


if __name__ == "__main__":
    unittest.main()

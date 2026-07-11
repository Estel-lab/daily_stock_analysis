# -*- coding: utf-8 -*-
"""
===================================
多空对抗分析（Bull vs Bear Debate）服务
===================================

职责：
1. 对同一只股票并行构建「多头论证」与「空头论证」两份独立观点，
   再由第三次 LLM 调用担任裁判，输出多空裁决与 0-100 置信度；
2. 生成可追加到分析报告的紧凑 Markdown 段（format_debate_block），
   并把结构化结果写入 dashboard["debate"] 供下游消费。

设计要点：
- opt-in：由 DEBATE_ANALYSIS_ENABLED 控制（默认关闭），LLM 走
  Agent 同款 LLMToolAdapter 路由栈（LiteLLM 多渠道 fallback）；
- 只追加观点，不修改评分 / 建议 / 决策字段，避免与既有决策护栏
  （phase / market context guardrail）产生契约冲突；
- 全链路 fail-open：LLM 不可用、超时、裁决 JSON 解析失败均返回 None。

配置（环境变量，均可选）：
- DEBATE_ANALYSIS_ENABLED    默认 false
- DEBATE_CALL_TIMEOUT        单次 LLM 调用超时秒数，默认 60
- DEBATE_NEWS_CHARS          注入辩论上下文的新闻摘要截断长度，默认 1500
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

VERDICTS = ("bull", "bear", "neutral")
_VERDICT_LABELS = {"bull": "偏多", "bear": "偏空", "neutral": "中性"}

_BULL_SYSTEM = (
    "你是一位坚定但专业的多头研究员。你的任务是基于给定数据，"
    "为做多该股票构建最有说服力的论证。要求：3-5 条论点，每条必须"
    "引用上下文中的具体数据或事实，不允许套话；如果数据确实不支持做多，"
    "允许明确说明论证乏力。只输出论点列表，不要输出结论建议。"
)
_BEAR_SYSTEM = (
    "你是一位坚定但专业的空头研究员。你的任务是基于给定数据，"
    "为看空/回避该股票构建最有说服力的论证。要求：3-5 条论点，每条必须"
    "引用上下文中的具体数据或事实，不允许套话；如果数据确实不支持看空，"
    "允许明确说明论证乏力。只输出论点列表，不要输出结论建议。"
)
_JUDGE_SYSTEM = (
    "你是一位中立的投资裁判。给定同一只股票的多头论证与空头论证，"
    "评估哪一方证据更扎实（数据具体性、逻辑闭环、风险覆盖），"
    "输出严格 JSON（不要包含其他文本）：\n"
    '{"verdict": "bull|bear|neutral", "confidence": 0-100 的整数, '
    '"summary": "80 字以内的中文裁决要点"}'
)


def is_debate_enabled(config: Any = None) -> bool:
    if config is not None and hasattr(config, "debate_analysis_enabled"):
        return bool(getattr(config, "debate_analysis_enabled"))
    return (os.getenv("DEBATE_ANALYSIS_ENABLED", "false") or "false").strip().lower() == "true"


@dataclass
class DebateOutcome:
    """一次多空对抗的结果。"""

    verdict: str  # bull / bear / neutral
    confidence: int  # 0-100
    bull_case: str
    bear_case: str
    judge_summary: str
    model_used: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "verdict_label": _VERDICT_LABELS.get(self.verdict, self.verdict),
            "confidence": self.confidence,
            "bull_case": self.bull_case,
            "bear_case": self.bear_case,
            "judge_summary": self.judge_summary,
            "model_used": self.model_used,
        }


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "N/A"
    return str(value)


def build_context_digest(
    *,
    code: str,
    stock_name: str,
    enhanced_context: Optional[Dict[str, Any]] = None,
    news_context: Optional[str] = None,
    operation_advice: Optional[str] = None,
    sentiment_score: Optional[int] = None,
) -> str:
    """把分析上下文压缩成辩论双方共用的紧凑数据摘要。"""
    context = enhanced_context or {}
    today = context.get("today") or {}
    realtime = context.get("realtime") or {}
    lines = [
        f"标的: {stock_name}({code})",
        (
            f"当日行情: 收盘 {_fmt(today.get('close'))} | 涨跌 {_fmt(today.get('pct_chg'))}%"
            f" | MA5 {_fmt(today.get('ma5'))} | MA10 {_fmt(today.get('ma10'))}"
            f" | MA20 {_fmt(today.get('ma20'))} | 量比 {_fmt(today.get('volume_ratio'))}"
        ),
    ]
    if realtime:
        lines.append(
            f"实时: 价格 {_fmt(realtime.get('price'))} | 涨跌 {_fmt(realtime.get('change_pct'))}%"
            f" | PE {_fmt(realtime.get('pe_ratio'))} | PB {_fmt(realtime.get('pb_ratio'))}"
            f" | 换手 {_fmt(realtime.get('turnover_rate'))}%"
        )
    if operation_advice or sentiment_score is not None:
        lines.append(
            f"系统当前观点: 建议 {_fmt(operation_advice)} | 评分 {_fmt(sentiment_score)}/100"
        )
    if news_context:
        max_chars = _env_int("DEBATE_NEWS_CHARS", 1500)
        excerpt = str(news_context).strip()
        if len(excerpt) > max_chars:
            excerpt = excerpt[:max_chars] + "…（已截断）"
        lines.append("近期资讯摘要:")
        lines.append(excerpt)
    return "\n".join(lines)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 回复中提取首个 JSON 对象（容忍 ```json 包裹与前后杂文）。"""
    if not text:
        return None
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", candidate, re.DOTALL)
        if brace:
            candidate = brace.group(0)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _call_text(
    llm, system_prompt: str, user_prompt: str, *, timeout: float
) -> tuple[Optional[str], str]:
    """调用 LLM 返回 (文本, 模型名)；失败返回 (None, "")。"""
    response = llm.call_text(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.5,
        timeout=timeout,
    )
    content = getattr(response, "content", None)
    provider = getattr(response, "provider", "")
    model = str(getattr(response, "model", "") or "")
    if provider == "error" or not content or not str(content).strip():
        return None, model
    return str(content).strip(), model


def run_debate_analysis(
    *,
    code: str,
    stock_name: str,
    enhanced_context: Optional[Dict[str, Any]] = None,
    news_context: Optional[str] = None,
    operation_advice: Optional[str] = None,
    sentiment_score: Optional[int] = None,
    llm: Any = None,
) -> Optional[DebateOutcome]:
    """
    执行一轮多空对抗：多头论证 + 空头论证 + 裁判裁决。

    Args:
        llm: 可注入的 LLM 适配器（需实现 call_text），默认使用 LLMToolAdapter。

    Returns:
        DebateOutcome；LLM 不可用或任一环节失败时返回 None（fail-open）。
    """
    if llm is None:
        try:
            from src.agent.llm_adapter import LLMToolAdapter

            llm = LLMToolAdapter()
        except Exception as exc:
            logger.warning("Debate LLM adapter init failed: %s", exc)
            return None
    if hasattr(llm, "is_available") and not llm.is_available:
        logger.info("Debate skipped: LLM adapter unavailable")
        return None

    timeout = _env_float("DEBATE_CALL_TIMEOUT", 60.0)
    digest = build_context_digest(
        code=code,
        stock_name=stock_name,
        enhanced_context=enhanced_context,
        news_context=news_context,
        operation_advice=operation_advice,
        sentiment_score=sentiment_score,
    )

    bull_case, _ = _call_text(llm, _BULL_SYSTEM, digest, timeout=timeout)
    if not bull_case:
        logger.warning("Debate bull case generation failed for %s", code)
        return None
    bear_case, _ = _call_text(llm, _BEAR_SYSTEM, digest, timeout=timeout)
    if not bear_case:
        logger.warning("Debate bear case generation failed for %s", code)
        return None

    judge_prompt = (
        f"{digest}\n\n=== 多头论证 ===\n{bull_case}\n\n=== 空头论证 ===\n{bear_case}"
    )
    judge_text, model_used = _call_text(llm, _JUDGE_SYSTEM, judge_prompt, timeout=timeout)
    payload = _extract_json(judge_text or "")
    if not payload:
        logger.warning("Debate judge JSON parse failed for %s", code)
        return None

    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        logger.warning("Debate judge returned invalid verdict for %s: %r", code, verdict)
        return None
    try:
        confidence = int(payload.get("confidence"))
    except (TypeError, ValueError):
        confidence = 50
    confidence = max(0, min(100, confidence))
    summary = str(payload.get("summary") or "").strip()

    return DebateOutcome(
        verdict=verdict,
        confidence=confidence,
        bull_case=bull_case,
        bear_case=bear_case,
        judge_summary=summary,
        model_used=model_used,
    )


def format_debate_block(outcome: DebateOutcome) -> str:
    """把辩论结果格式化为可追加到报告的紧凑 Markdown 段。"""
    label = _VERDICT_LABELS.get(outcome.verdict, outcome.verdict)
    lines = [
        f"🥊 **多空对抗裁决: {label}（置信度 {outcome.confidence}/100）**",
    ]
    if outcome.judge_summary:
        lines.append(f"- 裁决要点: {outcome.judge_summary}")
    lines.append(f"- 多方论证: {_first_lines(outcome.bull_case)}")
    lines.append(f"- 空方论证: {_first_lines(outcome.bear_case)}")
    lines.append("> 多空对抗为独立观点补充，不改变上方评分与操作建议。")
    return "\n".join(lines)


def _first_lines(text: str, *, max_chars: int = 300) -> str:
    """取论证正文压缩为单行摘要（换行合并、超长截断）。"""
    merged = " / ".join(
        segment.strip(" -•*") for segment in str(text).splitlines() if segment.strip()
    )
    if len(merged) > max_chars:
        merged = merged[:max_chars] + "…"
    return merged

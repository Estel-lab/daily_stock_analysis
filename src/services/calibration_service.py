# -*- coding: utf-8 -*-
"""
===================================
建议校准服务（分层胜率统计）
===================================

职责：
1. 把已完成的回测结果（BacktestResult）按市场 / 建议 / 模型分层，
   统计各层胜率与样本数，回答“哪类建议、哪个模型、哪个市场更可信”；
2. 为个股分析 Prompt 提供比整体胜率更细的「历史校准」补充
   （build_market_calibration_hint，供 signal_review_service 组装）；
3. 为周报输出「建议校准报告」Markdown 段（build_calibration_report）。

设计要点：
- 只读消费回测结果，不触发回测计算；模型名从 analysis_history.raw_result
  的 model_used 字段解析（Issue #528 起持久化）；
- 小样本层（completed < MIN_BUCKET_SAMPLES）不在提示与报告中展示，避免噪声；
- 全链路 fail-open：无回测数据返回 None，调用方静默跳过。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 分层桶最小完成样本数，低于该值的桶不展示
MIN_BUCKET_SAMPLES = 5
# 最多回看多少条回测结果（近期优先）
MAX_RESULT_ROWS = 1000

_MARKET_LABELS = {
    "cn": "A股",
    "hk": "港股",
    "us": "美股",
    "jp": "日股",
    "kr": "韩股",
    "tw": "台股",
}


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "N/A"


def _bucket_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    completed = len(rows)
    win = sum(1 for r in rows if r["outcome"] == "win")
    loss = sum(1 for r in rows if r["outcome"] == "loss")
    neutral = completed - win - loss
    denominator = win + loss
    return {
        "completed": completed,
        "win": win,
        "loss": loss,
        "neutral": neutral,
        "win_rate_pct": round(win / denominator * 100.0, 2) if denominator else None,
    }


def _market_for_code(code: Optional[str]) -> str:
    try:
        from src.core.trading_calendar import get_market_for_stock

        return get_market_for_stock(str(code or "")) or "unknown"
    except Exception:
        return "unknown"


def _model_from_raw_result(raw_result: Any) -> str:
    if not raw_result:
        return "unknown"
    try:
        payload = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
    except (json.JSONDecodeError, TypeError):
        return "unknown"
    if isinstance(payload, dict):
        model = str(payload.get("model_used") or "").strip()
        if model:
            return model
    return "unknown"


def compute_calibration_stats(*, limit: int = MAX_RESULT_ROWS) -> Optional[Dict[str, Any]]:
    """
    汇总近 limit 条已完成回测结果的分层胜率。

    Returns:
        {
            "overall": {...},
            "by_market": {"cn": {...}, ...},
            "by_advice": {"买入": {...}, ...},
            "by_model": {"gemini/gemini-2.0-flash": {...}, ...},
            "eval_window_days": int | None,
        }
        无可用数据时返回 None。
    """
    try:
        from src.config import get_config
        from src.repositories.backtest_repo import BacktestRepository

        config = get_config()
        engine_version = str(getattr(config, "backtest_engine_version", "v1"))
        repo = BacktestRepository()
        rows, _total = repo.get_results_paginated(
            code=None,
            engine_version=engine_version,
            days=None,
            offset=0,
            limit=max(1, min(int(limit), MAX_RESULT_ROWS)),
        )
    except Exception as exc:
        logger.debug("Calibration stats fetch failed: %s", exc)
        return None

    samples: List[Dict[str, Any]] = []
    eval_windows: List[int] = []
    for row in rows:
        result = row[0]
        raw_result = row[5] if len(row) > 5 else None
        if getattr(result, "eval_status", None) != "completed":
            continue
        outcome = str(getattr(result, "outcome", "") or "").strip()
        if outcome not in ("win", "loss", "neutral"):
            continue
        samples.append(
            {
                "outcome": outcome,
                "market": _market_for_code(getattr(result, "code", None)),
                "advice": str(getattr(result, "operation_advice", "") or "unknown").strip()
                or "unknown",
                "model": _model_from_raw_result(raw_result),
            }
        )
        if getattr(result, "eval_window_days", None) is not None:
            eval_windows.append(int(result.eval_window_days))

    if not samples:
        return None

    def group_by(key: str) -> Dict[str, Dict[str, Any]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for sample in samples:
            grouped.setdefault(sample[key], []).append(sample)
        stats = {value: _bucket_stats(bucket) for value, bucket in grouped.items()}
        return dict(
            sorted(stats.items(), key=lambda item: (-item[1]["completed"], item[0]))
        )

    return {
        "overall": _bucket_stats(samples),
        "by_market": group_by("market"),
        "by_advice": group_by("advice"),
        "by_model": group_by("model"),
        "eval_window_days": max(set(eval_windows), key=eval_windows.count)
        if eval_windows
        else None,
    }


def build_market_calibration_hint(code: str) -> Optional[str]:
    """
    构建按市场分层的校准补充句（供个股分析 Prompt 的历史校准段追加）。

    只在该股所属市场样本充足时返回一句紧凑中文提示，否则返回 None。
    """
    stats = compute_calibration_stats()
    if not stats:
        return None
    market = _market_for_code(code)
    bucket = (stats.get("by_market") or {}).get(market)
    if not bucket or bucket["completed"] < MIN_BUCKET_SAMPLES:
        return None
    label = _MARKET_LABELS.get(market, market)
    parts = [
        f"{label}市场历史胜率 {_fmt_pct(bucket['win_rate_pct'])}（样本 {bucket['completed']}）"
    ]
    # 补充偏多类建议（买入/加仓等）在该市场的整体表现参考
    advice_stats = stats.get("by_advice") or {}
    bullish_rows = {
        advice: item
        for advice, item in advice_stats.items()
        if _is_bullish_advice(advice) and item["completed"] >= MIN_BUCKET_SAMPLES
    }
    if bullish_rows:
        win = sum(item["win"] for item in bullish_rows.values())
        loss = sum(item["loss"] for item in bullish_rows.values())
        completed = sum(item["completed"] for item in bullish_rows.values())
        denominator = win + loss
        if denominator:
            parts.append(
                f"偏多类建议整体胜率 {_fmt_pct(win / denominator * 100.0)}"
                f"（样本 {completed}）"
            )
    return "；".join(parts)


def _is_bullish_advice(advice: str) -> bool:
    try:
        from src.core.backtest_engine import BacktestEngine

        return BacktestEngine.infer_direction_expected(advice) == "up"
    except Exception:
        return False


def build_calibration_report() -> Optional[str]:
    """生成周报「建议校准报告」Markdown 段；无回测数据时返回 None。"""
    stats = compute_calibration_stats()
    if not stats:
        return None
    overall = stats["overall"]
    if overall["completed"] < MIN_BUCKET_SAMPLES:
        return None

    window = stats.get("eval_window_days")
    window_str = f"{window} 个交易日" if window else "评估窗口"
    lines = [
        "## 🎯 建议校准报告（分层胜率）",
        "",
        (
            f"- 整体: 胜率 {_fmt_pct(overall['win_rate_pct'])}"
            f"（完成样本 {overall['completed']}，评估窗口 {window_str}）"
        ),
    ]

    def bucket_line(title: str, buckets: Dict[str, Dict[str, Any]], *, label_map=None, top=4):
        rows = [
            (value, item)
            for value, item in buckets.items()
            if item["completed"] >= MIN_BUCKET_SAMPLES and value != "unknown"
        ][:top]
        if not rows:
            return None
        parts = []
        for value, item in rows:
            label = (label_map or {}).get(value, value)
            parts.append(
                f"{label} {_fmt_pct(item['win_rate_pct'])}（{item['completed']}）"
            )
        return f"- 分{title}: " + " | ".join(parts)

    for line in (
        bucket_line("市场", stats.get("by_market") or {}, label_map=_MARKET_LABELS),
        bucket_line("建议", stats.get("by_advice") or {}),
        bucket_line("模型", stats.get("by_model") or {}, top=3),
    ):
        if line:
            lines.append(line)

    lines.append("")
    lines.append(
        "> 胜率按 win/(win+loss) 计算，括号内为完成样本数；"
        f"样本 <{MIN_BUCKET_SAMPLES} 的分层不展示。持续低于 50% 的层应下调信任权重。"
    )
    return "\n".join(lines)

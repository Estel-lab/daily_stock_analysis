# -*- coding: utf-8 -*-
"""
===================================
美股筹码分布估算器
===================================

美股无公开的筹码分布数据源，这里用日线量价近似估算：
1. 把每日成交量均匀摊到当日 [low, high] 价格区间；
2. 按时间衰减加权（越久远的筹码越可能已换手）；
3. 汇总成价格-筹码分布，计算获利比例、平均成本与 90%/70% 成本区间。

结果为估算值（source 标记 estimated），仅反映量价结构近似，
不等价于 A 股基于换手率迭代的精确筹码模型。
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

from .realtime_types import ChipDistribution

logger = logging.getLogger(__name__)

# 每日筹码时间衰减系数（约 60 个交易日后权重降至 ~16%）
DECAY_PER_DAY = 0.97
# 价格分箱数量
PRICE_BINS = 120


def estimate_chip_distribution(
    df: pd.DataFrame,
    current_price: float,
    code: str,
    source: str = "yfinance_estimated",
) -> Optional[ChipDistribution]:
    """
    从日线数据估算筹码分布。

    Args:
        df: 含 high/low/volume 列的日线 DataFrame（时间升序）
        current_price: 当前价格（通常取最新收盘价）
        code: 股票代码
        source: 数据来源标记

    Returns:
        ChipDistribution；数据不足（少于 20 根 K 线）或异常时返回 None
    """
    try:
        required = {"high", "low", "volume"}
        if df is None or len(df) < 20 or not required.issubset(df.columns):
            return None
        if not current_price or current_price <= 0:
            return None

        highs = df["high"].astype(float).to_numpy()
        lows = df["low"].astype(float).to_numpy()
        volumes = df["volume"].astype(float).to_numpy()
        n = len(df)

        price_min = float(np.nanmin(lows))
        price_max = float(np.nanmax(highs))
        if not np.isfinite(price_min) or not np.isfinite(price_max) or price_max <= price_min:
            return None

        edges = np.linspace(price_min, price_max, PRICE_BINS + 1)
        centers = (edges[:-1] + edges[1:]) / 2
        dist = np.zeros(PRICE_BINS)

        for i in range(n):
            vol = volumes[i]
            if not np.isfinite(vol) or vol <= 0:
                continue
            lo, hi = lows[i], highs[i]
            if not np.isfinite(lo) or not np.isfinite(hi):
                continue
            weight = vol * (DECAY_PER_DAY ** (n - 1 - i))
            if hi <= lo:
                idx = int(np.clip(np.searchsorted(edges, lo) - 1, 0, PRICE_BINS - 1))
                dist[idx] += weight
                continue
            # 成交量均匀摊到当日价格区间覆盖的分箱
            start = int(np.clip(np.searchsorted(edges, lo) - 1, 0, PRICE_BINS - 1))
            end = int(np.clip(np.searchsorted(edges, hi) - 1, 0, PRICE_BINS - 1))
            span = end - start + 1
            dist[start:end + 1] += weight / span

        total = dist.sum()
        if total <= 0:
            return None

        pdf = dist / total
        cdf = np.cumsum(pdf)

        profit_ratio = float(pdf[centers < current_price].sum())
        avg_cost = float((pdf * centers).sum())

        def _band(low_q: float, high_q: float):
            lo_price = float(centers[int(np.searchsorted(cdf, low_q))])
            hi_price = float(centers[min(int(np.searchsorted(cdf, high_q)), PRICE_BINS - 1)])
            denom = hi_price + lo_price
            concentration = (hi_price - lo_price) / denom if denom > 0 else 0.0
            return lo_price, hi_price, concentration

        cost_90_low, cost_90_high, concentration_90 = _band(0.05, 0.95)
        cost_70_low, cost_70_high, concentration_70 = _band(0.15, 0.85)

        return ChipDistribution(
            code=code,
            date=str(df["date"].iloc[-1]) if "date" in df.columns else "",
            source=source,
            profit_ratio=round(profit_ratio, 4),
            avg_cost=round(avg_cost, 2),
            cost_90_low=round(cost_90_low, 2),
            cost_90_high=round(cost_90_high, 2),
            concentration_90=round(concentration_90, 4),
            cost_70_low=round(cost_70_low, 2),
            cost_70_high=round(cost_70_high, 2),
            concentration_70=round(concentration_70, 4),
        )
    except Exception as e:
        logger.debug("[US筹码估算] %s 估算失败: %s", code, e)
        return None

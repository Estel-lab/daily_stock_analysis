# -*- coding: utf-8 -*-
"""晨星公允价值质量筛：全美股 → 被低估 + 有护城河 Top-N，经 DSA 通知层推送。

复用 ai-berkshire 仓库 `tools/morningstar_fair_value.py`（抓晨星筛选器 API：公允价值、
星级、护城河评级），在 DSA 侧计算潜在涨幅、按质量过滤排序，推送 Top-N，并写出「终选」
标的清单供后续四大师深度分析（`main.py --stocks`）消费。这是价值优先漏斗的第一段（① 质量筛
+ ② 精选），不依赖动量突破，天天有结果。

设计约束：
- 数据抓取与 API 逻辑保留在 ai-berkshire，本脚本只做编排、过滤、排序与推送。
- 抓取失败 fail-open：无数据时按配置静默或提示，不中断。
- 无信号不伪造；过滤阈值全部可配置。

环境变量：
- AI_BERKSHIRE_DIR: ai-berkshire 检出路径（默认 ./ai-berkshire）
- MS_MIN_UPSIDE: 最小潜在涨幅%（默认 15）
- MS_MIN_STARS: 最小星级（默认 4）
- MS_MOATS: 允许的护城河评级，逗号分隔（默认 Wide,Narrow）
- MS_TOP_N: 推送的候选数（默认 15）
- MS_FINALISTS: 写入终选清单的数量，供深度分析（默认 3）
- MS_MAX_PAGES: 抓取页数上限（限速护栏，默认 0=不限）

用法：
    python scripts/morningstar_screen.py            # 抓取、过滤、推送
    python scripts/morningstar_screen.py --dry-run  # 只打印，不推送
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

MOAT_ORDER = {"Wide": 0, "Narrow": 1, "None": 2, "": 3}


def load_morningstar(berkshire_dir: Path):
    """从 ai-berkshire 检出目录加载 morningstar_fair_value 模块（复用其抓取逻辑）。"""
    tools_dir = berkshire_dir / "tools"
    if not (tools_dir / "morningstar_fair_value.py").exists():
        raise FileNotFoundError(
            f"未找到 {tools_dir / 'morningstar_fair_value.py'}，"
            "请确认 ai-berkshire 已检出到 AI_BERKSHIRE_DIR 指定路径"
        )
    sys.path.insert(0, str(tools_dir))
    import morningstar_fair_value  # noqa: PLC0415

    return morningstar_fair_value


def fetch_all_rows(mod: Any, max_pages: int) -> List[dict]:
    """用 ai-berkshire 模块的 fetch_page 抓全部（或前 max_pages 页）原始记录。"""
    first = mod.fetch_page(1)
    total = int(first.get("total", 0) or 0)
    rows: List[dict] = list(first.get("rows", []) or [])
    page_size = getattr(mod, "PAGE_SIZE", 100)
    total_pages = (total + page_size - 1) // page_size if total else 1
    if max_pages > 0:
        total_pages = min(total_pages, max_pages)
    for page in range(2, total_pages + 1):
        data = None
        for attempt in range(2):  # 超时/失败重试一次，减少数据缺口
            try:
                data = mod.fetch_page(page)
                break
            except Exception as exc:  # noqa: BLE001 - 单页失败 fail-open，继续后续页
                if attempt == 0:
                    time.sleep(1)
                    continue
                print(f"  第 {page} 页抓取失败（fail-open，已重试）: {type(exc).__name__}")
        if data is None:
            continue
        page_rows = data.get("rows", []) or []
        if not page_rows:
            break
        rows.extend(page_rows)
        time.sleep(0.3)
    return rows


def diagnose(candidates: List[dict], cfg: Dict[str, Any]) -> str:
    """0 命中时的过滤诊断：分别统计各单条件通过数，定位是哪个阈值卡死。"""
    total = len(candidates)
    have_moat = sum(1 for c in candidates if c["moat"] in cfg["moats"])
    pass_upside = sum(1 for c in candidates if c["upside_pct"] >= cfg["min_upside"])
    pass_stars = sum(1 for c in candidates if c["stars"] is None or c["stars"] >= cfg["min_stars"])
    return (
        f"过滤诊断（共 {total} 只有效记录）：护城河∈{sorted(cfg['moats'])} {have_moat} 只 · "
        f"潜在涨幅≥{cfg['min_upside']}% {pass_upside} 只 · 星级≥{cfg['min_stars']} {pass_stars} 只。"
        f"命中需同时满足三者；可调低 MS_MIN_UPSIDE / MS_MIN_STARS 或放宽 MS_MOATS。"
    )


def build_candidates(mod: Any, rows: List[dict]) -> List[dict]:
    """把晨星原始记录整理为候选（含潜在涨幅），字段来源于 ai-berkshire 模块契约。"""
    out: List[dict] = []
    for row in rows:
        fair_value = row.get("FairValueEstimate")
        close = row.get("ClosePrice")
        if not fair_value or not close or close <= 0:
            continue
        out.append(
            {
                "ticker": mod.extract_ticker(row.get("TenforeId", "")),
                "name": row.get("Name", ""),
                "close": round(float(close), 2),
                "fair_value": round(float(fair_value), 2),
                "upside_pct": round((fair_value - close) / close * 100, 1),
                "stars": _to_int(row.get("StarRatingM255")),
                "moat": row.get("EconomicMoat", "") or "",
                "uncertainty": row.get("AssessmentOfFairValueUncertainty", "") or "",
                "sector": row.get("SectorName", "") or "",
                "industry": row.get("IndustryName", "") or "",
            }
        )
    return out


def filter_and_rank(candidates: List[dict], min_upside: float, min_stars: int,
                    moats: set) -> List[dict]:
    """质量过滤（护城河 + 星级 + 潜在涨幅）后按 护城河→潜在涨幅 排序。"""
    kept = [
        c for c in candidates
        if c["ticker"] and c["moat"] in moats and c["upside_pct"] >= min_upside
        and (c["stars"] is None or c["stars"] >= min_stars)
    ]
    kept.sort(key=lambda c: (MOAT_ORDER.get(c["moat"], 9), -c["upside_pct"]))
    return kept


def pick_finalists(ranked: List[dict], count: int) -> List[dict]:
    """精选终选：按排序取前 count，尽量分散行业（同行业最多 1 家）以贴近行业漏斗意图。"""
    finalists: List[dict] = []
    seen_sectors: set = set()
    for c in ranked:
        sector = c["sector"] or c["ticker"]
        if sector in seen_sectors:
            continue
        finalists.append(c)
        seen_sectors.add(sector)
        if len(finalists) >= count:
            break
    if len(finalists) < count:  # 行业不够分散时用剩余高分候选补足
        for c in ranked:
            if c not in finalists:
                finalists.append(c)
            if len(finalists) >= count:
                break
    return finalists


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def format_message(ranked: List[dict], top_n: int, total: int, finalists: List[dict]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    parts = [
        f"# 🏛️ 晨星质量筛（被低估 + 有护城河）{today}",
        "",
        f"框架：全美股公允价值 → 护城河/星级过滤 → 潜在涨幅排序 | 命中 {total} 只，取前 {top_n}",
        "",
    ]
    for i, c in enumerate(ranked[:top_n], 1):
        stars = "★" * c["stars"] if c["stars"] else "N/A"
        parts.append(
            f"{i}. **{c['ticker']}** {c['name'][:28]} · {c['moat']}护城河 {stars}"
            f" · 潜在涨幅 **+{c['upside_pct']}%**"
            f"（现价 ${c['close']} / 公允 ${c['fair_value']}）· {c['industry'][:18]}"
        )
    if finalists:
        codes = " ".join(c["ticker"] for c in finalists)
        parts += ["", f"🎯 终选（拟深度分析）：{codes}"]
    parts += ["", "> 数据来源：Morningstar 公允价值 · 仅供研究参考，不构成投资建议"]
    return "\n".join(parts)


def resolve_config() -> Dict[str, Any]:
    return {
        "min_upside": float(os.getenv("MS_MIN_UPSIDE", "5") or 5),
        "min_stars": int(os.getenv("MS_MIN_STARS", "4") or 4),
        "moats": {m.strip() for m in (os.getenv("MS_MOATS", "Wide,Narrow")).split(",") if m.strip()},
        "top_n": int(os.getenv("MS_TOP_N", "15") or 15),
        "finalists": int(os.getenv("MS_FINALISTS", "3") or 3),
        "max_pages": int(os.getenv("MS_MAX_PAGES", "0") or 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="晨星公允价值质量筛 + 推送")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不发送通知")
    args = parser.parse_args()
    cfg = resolve_config()

    berkshire_dir = Path(os.getenv("AI_BERKSHIRE_DIR", PROJECT_ROOT / "ai-berkshire"))
    mod = load_morningstar(berkshire_dir)

    print("抓取晨星公允价值数据...")
    rows = fetch_all_rows(mod, cfg["max_pages"])
    if not rows:
        print("未获取到晨星数据（fail-open），退出")
        return 0
    candidates = build_candidates(mod, rows)
    ranked = filter_and_rank(candidates, cfg["min_upside"], cfg["min_stars"], cfg["moats"])
    print(f"共 {len(candidates)} 只有效记录，过滤后命中 {len(ranked)} 只")
    if not ranked:
        print(diagnose(candidates, cfg))
        print("过滤后无命中标的（阈值过严或市场整体高估），退出")
        return 0

    finalists = pick_finalists(ranked, cfg["finalists"])

    output_dir = PROJECT_ROOT / "reports" / "morningstar"
    output_dir.mkdir(parents=True, exist_ok=True)
    finalists_path = output_dir / "finalists.txt"
    finalists_path.write_text(",".join(c["ticker"] for c in finalists), encoding="utf-8")
    print(f"终选标的：{','.join(c['ticker'] for c in finalists)}（写入 {finalists_path}）")

    message = format_message(ranked, cfg["top_n"], len(ranked), finalists)
    if args.dry_run:
        print("\n===== dry-run 推送内容 =====\n")
        print(message)
        return 0

    from src.notification import get_notification_service  # noqa: PLC0415

    service = get_notification_service()
    ok = service.send(
        message,
        route_type="alert",
        dedup_key=f"morningstar:{datetime.now().strftime('%Y%m%d')}",
    )
    print(f"通知发送{'成功' if ok else '失败（无可用渠道或全部渠道失败）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

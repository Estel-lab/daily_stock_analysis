#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IBKR Flex Query 成交导入。

通过 IBKR Flex Web Service（token 鉴权的 HTTP 接口，无需运行 TWS/网关）
拉取成交记录，转换为 DSA `ibkr` 券商解析器可识别的 CSV：
- 默认只生成 CSV（Web 工作台「持仓导入」选择"盈透证券"上传即可）；
- 加 `--commit --account-id N` 时直接经 PortfolioImportService 入库（带去重）。

前置（一次性，IBKR 网页端设置）：
1. Performance & Reports -> Flex Queries -> 新建 Activity Flex Query，
   Sections 勾选 Trades（字段至少含 Symbol/TradeDate/Buy/Sell/Quantity/
   TradePrice/TradeID），格式选 XML；
2. Settings -> Flex Web Service -> 启用并生成 token。

环境变量：
  IBKR_FLEX_TOKEN      Flex Web Service token
  IBKR_FLEX_QUERY_ID   Flex Query ID

用法：
  python scripts/import_ibkr_flex.py                       # 生成 CSV
  python scripts/import_ibkr_flex.py --output my.csv
  python scripts/import_ibkr_flex.py --commit --account-id 1
  python scripts/import_ibkr_flex.py --from-xml stmt.xml   # 离线转换已下载的 XML
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent

FLEX_SEND_URL = (
    "https://gdcdyn.interactivebrokers.com/Universal/servlet/"
    "FlexStatementService.SendRequest"
)
# ErrorCode 1019 = 报表生成中，需轮询重试
RETRYABLE_ERROR_CODES = {"1019"}
POLL_ATTEMPTS = 6
POLL_INTERVAL_SECONDS = 10

CSV_HEADER = [
    "TradeDate", "Symbol", "Buy/Sell", "Quantity",
    "TradePrice", "TradeID", "Currency", "AssetCategory",
]
# 只导入股票成交；期权/期货等代码无法映射为 DSA 标的
IMPORTABLE_ASSET_CATEGORIES = {"STK", ""}


def _http_get(url: str, params: Dict[str, str], timeout: int = 30) -> str:
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full_url, headers={"User-Agent": "dsa-ibkr-flex/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def request_statement(token: str, query_id: str) -> str:
    """两步 Flex Web Service：SendRequest 拿引用码，GetStatement 轮询取报表 XML。"""
    send_xml = _http_get(FLEX_SEND_URL, {"t": token, "q": query_id, "v": "3"})
    root = ET.fromstring(send_xml)
    status = (root.findtext("Status") or "").strip()
    if status != "Success":
        raise RuntimeError(
            f"SendRequest 失败: {root.findtext('ErrorCode')} "
            f"{root.findtext('ErrorMessage')}"
        )
    ref_code = (root.findtext("ReferenceCode") or "").strip()
    get_url = (root.findtext("Url") or "").strip()
    if not ref_code or not get_url:
        raise RuntimeError(f"SendRequest 响应缺少 ReferenceCode/Url: {send_xml[:200]}")

    last_error = ""
    for attempt in range(POLL_ATTEMPTS):
        stmt = _http_get(get_url, {"t": token, "q": ref_code, "v": "3"})
        if "<FlexQueryResponse" in stmt:
            return stmt
        try:
            err_root = ET.fromstring(stmt)
            code = (err_root.findtext("ErrorCode") or "").strip()
            last_error = (
                f"{code} {(err_root.findtext('ErrorMessage') or '').strip()}"
            )
            if code not in RETRYABLE_ERROR_CODES:
                raise RuntimeError(f"GetStatement 失败: {last_error}")
        except ET.ParseError:
            last_error = stmt[:200]
        print(f"报表生成中，{POLL_INTERVAL_SECONDS}s 后重试（{attempt + 1}/{POLL_ATTEMPTS}）")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise RuntimeError(f"GetStatement 重试耗尽: {last_error}")


def parse_trades(statement_xml: str) -> Tuple[List[Dict[str, str]], int]:
    """从 Flex XML 提取股票成交行；返回 (成交行, 跳过的非股票成交数)。"""
    root = ET.fromstring(statement_xml)
    rows: List[Dict[str, str]] = []
    skipped = 0
    for trade in root.iter("Trade"):
        category = (trade.get("assetCategory") or "").strip().upper()
        if category not in IMPORTABLE_ASSET_CATEGORIES:
            skipped += 1
            continue
        symbol = (trade.get("symbol") or "").strip()
        side = (trade.get("buySell") or "").strip()
        if not symbol or not side:
            skipped += 1
            continue
        rows.append({
            "TradeDate": (trade.get("tradeDate") or "").strip(),
            "Symbol": symbol,
            "Buy/Sell": side,
            # IBKR 卖出 quantity 为负数，DSA 用 side 表达方向，统一取绝对值
            "Quantity": str(abs(float(trade.get("quantity") or 0))),
            "TradePrice": (trade.get("tradePrice") or trade.get("price") or "").strip(),
            "TradeID": (trade.get("tradeID") or trade.get("ibExecID") or "").strip(),
            "Currency": (trade.get("currency") or "").strip(),
            "AssetCategory": category,
        })
    return rows, skipped


def rows_to_csv(rows: List[Dict[str, str]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_HEADER)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def commit_rows(csv_text: str, account_id: int, dry_run: bool) -> None:
    sys.path.insert(0, str(PROJECT_ROOT))
    from src.services.portfolio_import_service import PortfolioImportService

    service = PortfolioImportService()
    parsed = service.parse_trade_csv(broker="ibkr", content=csv_text.encode("utf-8"))
    print(
        f"解析: {parsed['record_count']} 条成交, 跳过 {parsed['skipped_count']}, "
        f"错误 {parsed['error_count']}"
    )
    result = service.commit_trade_records(
        account_id=account_id,
        broker="ibkr",
        records=parsed["records"],
        dry_run=dry_run,
    )
    print(f"入库结果: {result}")


def main() -> int:
    parser = argparse.ArgumentParser(description="IBKR Flex Query 成交导入")
    parser.add_argument("--output", help="输出 CSV 路径（默认 data/ibkr_trades_<日期>.csv）")
    parser.add_argument("--from-xml", help="离线模式：直接转换本地 Flex XML 文件")
    parser.add_argument("--commit", action="store_true", help="直接入库（需 --account-id）")
    parser.add_argument("--account-id", type=int, help="DSA 组合账户 ID")
    parser.add_argument("--dry-run", action="store_true", help="入库演练，不写数据库")
    args = parser.parse_args()

    if args.commit and args.account_id is None:
        parser.error("--commit 需要 --account-id")

    if args.from_xml:
        statement_xml = Path(args.from_xml).read_text(encoding="utf-8")
    else:
        token = os.getenv("IBKR_FLEX_TOKEN", "").strip()
        query_id = os.getenv("IBKR_FLEX_QUERY_ID", "").strip()
        if not token or not query_id:
            print("未配置 IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID（配置方式见脚本头部注释）")
            return 1
        statement_xml = request_statement(token, query_id)

    rows, skipped = parse_trades(statement_xml)
    print(f"提取股票成交 {len(rows)} 条（跳过非股票资产 {skipped} 条）")
    if not rows:
        return 0

    csv_text = rows_to_csv(rows)
    output = Path(
        args.output
        or PROJECT_ROOT / "data" / f"ibkr_trades_{datetime.now().strftime('%Y%m%d')}.csv"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(csv_text, encoding="utf-8")
    print(f"CSV 已写入: {output}（Web 工作台持仓导入选择「盈透证券」上传）")

    if args.commit:
        commit_rows(csv_text, args.account_id, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

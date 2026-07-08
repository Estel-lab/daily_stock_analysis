# -*- coding: utf-8 -*-
"""
===================================
报告数据质量检查（非阻断观测项）
===================================

职责：
1. 扫描 reports/ 下指定日期（默认当天）的报告文件；
2. 检查文件是否存在、是否疑似空报告（体积过小）；
3. 统计每个个股章节内的 N/A 密度，识别「当日行情 / 数据透视」大面积缺数据的情况；
4. 通过 GitHub Actions 注解（::warning::）与 Step Summary 输出结果。

默认非阻断：始终 exit 0，仅输出警告，避免数据源偶发抖动影响每日推送；
传入 --strict 时发现任何警告将 exit 1（供本地排查或后续升级为阻断使用）。

用法：
    python scripts/check_report_quality.py [--reports-dir reports] [--date YYYYMMDD] [--strict]
"""

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

# 单个个股章节内 N/A 达到该数量即认为该股票核心数据大面积缺失
SECTION_NA_THRESHOLD = 8
# 报告文件小于该字节数视为疑似空报告
MIN_REPORT_BYTES = {
    "dashboard": 1000,
    "market_review": 500,
    "report": 300,
}


def split_stock_sections(text: str) -> List[Tuple[str, str]]:
    """按二级标题拆分个股章节，返回 (标题, 章节内容) 列表。"""
    sections = []
    matches = list(re.finditer(r"^## +(.+)$", text, flags=re.MULTILINE))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((m.group(1).strip(), text[start:end]))
    return sections


def check_file(path: Path, kind: str) -> List[str]:
    """检查单个报告文件，返回警告列表。"""
    warnings = []
    if not path.exists():
        warnings.append(f"{path.name}: 文件不存在")
        return warnings

    size = path.stat().st_size
    min_bytes = MIN_REPORT_BYTES.get(kind, 300)
    if size < min_bytes:
        warnings.append(f"{path.name}: 疑似空报告（{size} 字节 < {min_bytes} 字节阈值）")
        return warnings

    text = path.read_text(encoding="utf-8", errors="replace")

    if kind == "dashboard":
        total_na = text.count("N/A")
        for title, body in split_stock_sections(text):
            na_count = body.count("N/A")
            if na_count >= SECTION_NA_THRESHOLD:
                warnings.append(
                    f"{path.name}: 章节「{title}」N/A x{na_count}，"
                    f"当日行情/数据透视疑似大面积缺数据（多为历史行情获取失败）"
                )
        if total_na and not warnings:
            print(f"  ℹ️ {path.name}: N/A 共 {total_na} 处，未超过单章节阈值（美股免费源缺量比/换手率属正常）")

    return warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="报告数据质量检查")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--strict", action="store_true", help="发现警告时以非零码退出")
    args = parser.parse_args()

    reports_dir = Path(args.reports_dir)
    targets = [
        (reports_dir / f"dashboard_{args.date}.md", "dashboard"),
        (reports_dir / f"market_review_{args.date}.md", "market_review"),
        (reports_dir / f"report_{args.date}.md", "report"),
    ]

    print(f"===== 报告数据质量检查 ({args.date}) =====")
    all_warnings = []
    summary_rows = []
    for path, kind in targets:
        warnings = check_file(path, kind)
        size = path.stat().st_size if path.exists() else 0
        status = "⚠️" if warnings else ("✅" if path.exists() else "➖")
        summary_rows.append(f"| {path.name} | {size} | {status} |")
        for w in warnings:
            all_warnings.append(w)
            print(f"::warning title=报告数据质量::{w}")

    if not all_warnings:
        print("✅ 全部检查通过")

    step_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(f"\n### 📋 报告数据质量检查 ({args.date})\n\n")
            f.write("| 文件 | 字节 | 状态 |\n| --- | --- | --- |\n")
            f.write("\n".join(summary_rows) + "\n")
            for w in all_warnings:
                f.write(f"- ⚠️ {w}\n")

    if all_warnings and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

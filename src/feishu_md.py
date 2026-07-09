# -*- coding: utf-8 -*-
"""
飞书文档 Markdown 结构解析（SDK 无关）

把报告 Markdown 解析为结构化段落列表，供 feishu_doc 映射为 docx 块：
- 标题（#/##/###）与分割线
- **加粗** 拆分为带样式的文本段（run）
- Markdown 表格转为「**首列**: 其余列」的可读行（飞书 docx 块不支持直接写 markdown 表格）
- 引用（>）与列表（-）清理前缀

输出格式：[(kind, runs)]，kind ∈ heading1/heading2/heading3/divider/text，
runs 为 [(content, bold)] 列表；divider 的 runs 为空列表。
"""

import re
from typing import List, Tuple

Run = Tuple[str, bool]
Segment = Tuple[str, List[Run]]

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def parse_inline_runs(text: str) -> List[Run]:
    """把含 **加粗** 的行拆为 (内容, 是否加粗) 段列表。"""
    runs: List[Run] = []
    pos = 0
    for m in _BOLD_RE.finditer(text):
        if m.start() > pos:
            runs.append((text[pos:m.start()], False))
        runs.append((m.group(1), True))
        pos = m.end()
    if pos < len(text):
        runs.append((text[pos:], False))
    return runs or [("", False)]


def _is_table_separator(line: str) -> bool:
    body = line.strip().strip("|")
    return bool(body) and all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in body.split("|"))


def _split_table_cells(line: str) -> List[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_markdown_structure(md_text: str) -> List[Segment]:
    """解析 Markdown 为结构化段落列表。"""
    segments: List[Segment] = []
    lines = md_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # 表格块：连续的 | 开头行
        if line.startswith("|") and line.endswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            data_rows = [l for l in table_lines if not _is_table_separator(l)]
            if not data_rows:
                continue
            header = _split_table_cells(data_rows[0])
            body_rows = data_rows[1:]
            if not body_rows:
                # 只有表头：按普通加粗行输出
                segments.append(("text", parse_inline_runs(" | ".join(header))))
                continue
            for row_line in body_rows:
                cells = _split_table_cells(row_line)
                first = cells[0] if cells else ""
                rest = " | ".join(cells[1:]) if len(cells) > 1 else ""
                runs: List[Run] = []
                # 首列加粗作为标签，其余列为内容（首列自身的 ** 标记先剥离）
                first_plain = "".join(part for part, _ in parse_inline_runs(first))
                runs.append((first_plain, True))
                if rest:
                    runs.append((": ", False))
                    runs.extend(parse_inline_runs(rest))
                segments.append(("text", runs))
            continue

        if line.startswith("### "):
            segments.append(("heading3", parse_inline_runs(line[4:])))
        elif line.startswith("## "):
            segments.append(("heading2", parse_inline_runs(line[3:])))
        elif line.startswith("# "):
            segments.append(("heading1", parse_inline_runs(line[2:])))
        elif line.startswith("---"):
            segments.append(("divider", []))
        elif line.startswith("> "):
            segments.append(("text", parse_inline_runs(line[2:])))
        elif line.startswith("- "):
            segments.append(("text", parse_inline_runs("• " + line[2:])))
        else:
            segments.append(("text", parse_inline_runs(line)))
        i += 1
    return segments

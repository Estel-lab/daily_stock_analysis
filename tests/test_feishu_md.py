# -*- coding: utf-8 -*-
"""飞书 Markdown 结构解析测试（针对文档中加粗/表格显示为源码的问题）"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.feishu_md import parse_inline_runs, parse_markdown_structure


class TestInlineRuns(unittest.TestCase):
    def test_bold_split(self):
        runs = parse_inline_runs("**🟡 持有** | 看多")
        self.assertEqual(runs, [("🟡 持有", True), (" | 看多", False)])

    def test_multiple_bold(self):
        runs = parse_inline_runs("a **b** c **d**")
        self.assertEqual(runs, [("a ", False), ("b", True), (" c ", False), ("d", True)])

    def test_no_bold(self):
        self.assertEqual(parse_inline_runs("plain"), [("plain", False)])


class TestMarkdownStructure(unittest.TestCase):
    def test_report_snippet_renders_without_raw_markers(self):
        md = (
            "**🟡 持有** | 看多\n\n"
            "> **一句话决策**: 趋势多头但量能萎缩。\n\n"
            "⏰ **时效性**: 明日盘中\n\n"
            "| 持仓情况 | 操作建议 |\n"
            "|---------|---------|\n"
            "| 🆕 **空仓者** | 不宜追涨，等待回踩确认。 |\n"
            "| 💼 **持仓者** | 趋势完好，可继续持有。 |\n"
        )
        segments = parse_markdown_structure(md)
        flat = "".join(content for _, runs in segments for content, _ in runs)
        # 原始 markdown 标记不应泄漏到渲染内容
        self.assertNotIn("**", flat)
        self.assertNotIn("|--", flat)
        self.assertNotIn("> ", flat)

        # 表格行转为「首列加粗: 其余列」
        table_rows = [runs for kind, runs in segments if runs and runs[0] == ("🆕 空仓者", True)]
        self.assertEqual(len(table_rows), 1)
        self.assertIn(("不宜追涨，等待回踩确认。", False), table_rows[0])

        # 引用行的加粗保留为样式段
        quote_rows = [runs for _, runs in segments if ("一句话决策", True) in runs]
        self.assertEqual(len(quote_rows), 1)

    def test_headings_and_divider(self):
        segments = parse_markdown_structure("# H1\n## H2\n### H3\n---\ntext")
        kinds = [k for k, _ in segments]
        self.assertEqual(kinds, ["heading1", "heading2", "heading3", "divider", "text"])

    def test_list_prefix_converted(self):
        segments = parse_markdown_structure("- item one")
        self.assertEqual(segments[0][1][0], ("• item one", False))

    def test_table_separator_variants(self):
        md = "| a | b |\n| :--- | ---: |\n| x | y |"
        segments = parse_markdown_structure(md)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0][1][0], ("x", True))


if __name__ == "__main__":
    unittest.main()

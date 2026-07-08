# -*- coding: utf-8 -*-
"""
===================================
可视化 HTML 报告服务
===================================

职责：
1. 把当日分析结果与评分历史渲染为自包含 HTML 报告（ECharts CDN 交互图表）；
2. 图表不可用（无 JS/CDN 失败）时数据表始终可读（可访问性兜底）；
3. 供 main.py 保存到 reports/ 并上传到飞书文件夹。

图表规范：单轴、细条、状态色仅用于买/卖/观望语义并配文字标签，
折线按固定槽位取分类色（不随序列数量重排）。
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 分类色（固定槽位顺序，light/dark 已通过对比度与色觉分离校验）
_CATEGORICAL_LIGHT = ["#2a78d6", "#1baf7a", "#eda100", "#008300", "#4a3aa7", "#e34948", "#e87ba4", "#eb6834"]
_CATEGORICAL_DARK = ["#3987e5", "#199e70", "#c98500", "#008300", "#9085e9", "#e66767", "#d55181", "#d95926"]
# 状态色（买入/观望/卖出，两种模式同值）
_STATUS = {"buy": "#0ca30c", "hold": "#fab219", "sell": "#d03b3b"}


def _decision_type(result: Any) -> str:
    value = str(getattr(result, "decision_type", "") or "hold")
    return value if value in _STATUS else "hold"


def _advice_text(result: Any) -> str:
    return str(getattr(result, "operation_advice", "") or "")


def build_visual_report_html(
    results: List[Any],
    history_by_code: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    report_date: Optional[str] = None,
) -> str:
    """构建自包含可视化 HTML 报告。results 为空时返回仅含说明的最小页面。"""
    report_date = report_date or datetime.now().strftime("%Y-%m-%d")
    history_by_code = history_by_code or {}

    sorted_results = sorted(results, key=lambda r: getattr(r, "sentiment_score", 0))
    stocks = []
    for r in sorted_results:
        code = str(getattr(r, "code", ""))
        history = [
            {
                "date": str(item.get("created_at", ""))[:10],
                "score": item.get("sentiment_score"),
            }
            for item in reversed(history_by_code.get(code, []) or [])
            if item.get("sentiment_score") is not None
        ]
        history.append({"date": report_date, "score": getattr(r, "sentiment_score", None)})
        stocks.append(
            {
                "code": code,
                "name": str(getattr(r, "name", "") or code),
                "score": getattr(r, "sentiment_score", 0),
                "advice": _advice_text(r),
                "decision": _decision_type(r),
                "trend": str(getattr(r, "trend_prediction", "") or ""),
                "history": history,
            }
        )

    has_history = any(len(s["history"]) > 1 for s in stocks)
    # 防止 JSON 中出现 </script> 提前闭合
    data_json = json.dumps(
        {
            "date": report_date,
            "stocks": stocks,
            "hasHistory": has_history,
            "palette": {"light": _CATEGORICAL_LIGHT, "dark": _CATEGORICAL_DARK, "status": _STATUS},
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")

    table_rows = "\n".join(
        f"<tr><td>{s['name']}</td><td>{s['code']}</td><td>{s['score']}</td>"
        f"<td>{s['advice']}</td><td>{s['trend']}</td></tr>"
        for s in reversed(stocks)
    )
    history_rows = "\n".join(
        f"<tr><td>{s['name']}({s['code']})</td><td>"
        + " → ".join(f"{p['date'][5:]}:{p['score']}" for p in s["history"])
        + "</td></tr>"
        for s in stocks
        if len(s["history"]) > 1
    )
    history_section = (
        f"""
  <section>
    <h2>评分历史趋势</h2>
    <div id="chart-history" class="chart" role="img" aria-label="个股评分历史折线图"></div>
    <details><summary>数据表</summary>
      <table><thead><tr><th>股票</th><th>评分序列</th></tr></thead><tbody>{history_rows}</tbody></table>
    </details>
  </section>"""
        if has_history
        else """
  <section>
    <h2>评分历史趋势</h2>
    <p class="muted">历史数据积累中（首次运行后次日起可见趋势）。</p>
  </section>"""
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{report_date} 决策仪表盘可视化</title>
<style>
  :root {{ --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --line: #e5e4e0; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --line: #3a3a37; }}
  }}
  body {{ margin: 0 auto; max-width: 860px; padding: 20px 16px 48px; background: var(--surface);
         color: var(--ink); font: 15px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }}
  h1 {{ font-size: 20px; }} h2 {{ font-size: 16px; margin: 28px 0 8px; }}
  .muted {{ color: var(--ink-2); }}
  .chart {{ width: 100%; height: 320px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; margin-top: 8px; }}
  th, td {{ border-bottom: 1px solid var(--line); padding: 6px 10px; text-align: left; }}
  th {{ color: var(--ink-2); font-weight: 600; }}
  .legend-note {{ font-size: 12px; color: var(--ink-2); }}
  details {{ margin-top: 8px; }} summary {{ cursor: pointer; color: var(--ink-2); }}
</style>
</head>
<body>
  <h1>📊 {report_date} 决策仪表盘可视化</h1>
  <p class="muted">评分 0-100：&gt;70 强烈看多 · &gt;60 看多 · 40-60 震荡 · &lt;40 看空。图表加载失败时请看下方数据表。</p>

  <section>
    <h2>个股综合评分</h2>
    <div id="chart-scores" class="chart" role="img" aria-label="个股综合评分条形图"></div>
    <p class="legend-note">条形颜色代表操作建议：🟢 买入 · 🟡 观望 · 🔴 卖出（颜色仅辅助，建议文字见表格与条形标签）</p>
    <table><thead><tr><th>名称</th><th>代码</th><th>评分</th><th>建议</th><th>趋势</th></tr></thead>
    <tbody>{table_rows}</tbody></table>
  </section>
{history_section}

  <p class="muted" style="margin-top:32px">生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · daily_stock_analysis</p>

<script id="report-data" type="application/json">{data_json}</script>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<script>
(function () {{
  if (typeof echarts === "undefined") return;  // CDN 失败时保留表格
  var data = JSON.parse(document.getElementById("report-data").textContent);
  var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  var ink = dark ? "#ffffff" : "#0b0b0b", ink2 = dark ? "#c3c2b7" : "#52514e",
      line = dark ? "#3a3a37" : "#e5e4e0";
  var axis = {{ axisLine: {{ lineStyle: {{ color: line }} }}, axisLabel: {{ color: ink2 }},
               splitLine: {{ lineStyle: {{ color: line }} }} }};

  var scores = echarts.init(document.getElementById("chart-scores"));
  scores.setOption({{
    backgroundColor: "transparent",
    grid: {{ left: 8, right: 48, top: 8, bottom: 8, containLabel: true }},
    tooltip: {{ trigger: "item", formatter: function (p) {{
      var s = data.stocks[p.dataIndex];
      return s.name + "(" + s.code + ")<br/>评分 " + s.score + " · " + s.advice;
    }} }},
    xAxis: Object.assign({{ type: "value", max: 100 }}, axis),
    yAxis: Object.assign({{ type: "category",
      data: data.stocks.map(function (s) {{ return s.name + "(" + s.code + ")"; }} ) }}, axis),
    series: [{{
      type: "bar", barWidth: 14,
      itemStyle: {{ borderRadius: [0, 4, 4, 0],
        color: function (p) {{ return data.palette.status[data.stocks[p.dataIndex].decision]; }} }},
      label: {{ show: true, position: "right", color: ink,
        formatter: function (p) {{ return p.value + " " + data.stocks[p.dataIndex].advice; }} }},
      data: data.stocks.map(function (s) {{ return s.score; }})
    }}]
  }});

  if (data.hasHistory) {{
    var palette = dark ? data.palette.dark : data.palette.light;
    var dates = [];
    data.stocks.forEach(function (s) {{ s.history.forEach(function (p) {{
      if (dates.indexOf(p.date) < 0) dates.push(p.date); }}); }});
    dates.sort();
    var hist = echarts.init(document.getElementById("chart-history"));
    hist.setOption({{
      backgroundColor: "transparent",
      grid: {{ left: 8, right: 16, top: 40, bottom: 8, containLabel: true }},
      tooltip: {{ trigger: "axis" }},
      legend: {{ top: 0, textStyle: {{ color: ink2 }} }},
      xAxis: Object.assign({{ type: "category", data: dates }}, axis),
      yAxis: Object.assign({{ type: "value", min: 0, max: 100 }}, axis),
      series: data.stocks.map(function (s, i) {{
        return {{
          name: s.name + "(" + s.code + ")", type: "line",
          lineStyle: {{ width: 2 }}, symbolSize: 8,
          color: palette[i % palette.length], connectNulls: true,
          data: dates.map(function (d) {{
            var hit = s.history.filter(function (p) {{ return p.date === d; }})[0];
            return hit ? hit.score : null;
          }})
        }};
      }})
    }});
    window.addEventListener("resize", function () {{ hist.resize(); }});
  }}
  window.addEventListener("resize", function () {{ scores.resize(); }});
}})();
</script>
</body>
</html>
"""

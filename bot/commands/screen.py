# -*- coding: utf-8 -*-
"""
===================================
选股筛命令
===================================

手动触发动量+价值选股筛（复用 scripts/screener_notify.py 的扫描与消息组装，
底层引擎来自 ai-berkshire 仓库的 tools/stock_screener.py）。

依赖：本机存在 ai-berkshire 检出（AI_BERKSHIRE_DIR 指定路径，
默认 <项目根>/ai-berkshire）。未配置时命令给出启用指引而不报错。
"""

import importlib.util
import logging
import os
import threading
from pathlib import Path
from typing import List

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MAX_CODES = 20

# 同一时间只允许一个扫描在跑（扫描逐标的请求 Yahoo，耗时随标的数线性增长）
_scan_lock = threading.Lock()


def _load_screener_glue():
    """加载 scripts/screener_notify.py 模块（scripts/ 不是包，用文件路径加载）。"""
    glue_path = PROJECT_ROOT / "scripts" / "screener_notify.py"
    spec = importlib.util.spec_from_file_location("screener_notify", glue_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScreenCommand(BotCommand):
    """
    选股筛命令

    用法：
        /screen                - 扫描全部自选股
        /screen NVDA MU 600519 - 扫描指定标的
    """

    @property
    def name(self) -> str:
        return "screen"

    @property
    def aliases(self) -> List[str]:
        return ["scr", "选股", "筛选"]

    @property
    def description(self) -> str:
        return "动量+价值选股筛（60日新高+放量 → 6维验证）"

    @property
    def usage(self) -> str:
        return "/screen [股票代码...]"

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        from src.config import get_config

        berkshire_dir = Path(
            os.getenv("AI_BERKSHIRE_DIR", PROJECT_ROOT / "ai-berkshire")
        )
        if not (berkshire_dir / "tools" / "stock_screener.py").exists():
            return BotResponse.markdown_response(
                "⚠️ 选股筛引擎未安装。\n\n"
                "该命令依赖 ai-berkshire 仓库的 stock_screener：\n"
                "```\ngit clone https://github.com/Estel-lab/ai-berkshire.git\n```\n"
                f"并将 `AI_BERKSHIRE_DIR` 指向检出路径（当前查找位置：{berkshire_dir}）"
            )

        codes = [a.strip().upper() for a in args if a.strip()]
        if not codes:
            config = get_config()
            codes = [c for c in (config.stock_list or []) if c.strip()]
        if not codes:
            return BotResponse.markdown_response(
                "未指定标的且自选股为空。\n用法：`/screen NVDA MU 600519`"
            )
        codes = codes[:MAX_CODES]

        if not _scan_lock.acquire(blocking=False):
            return BotResponse.markdown_response("⚠️ 已有选股扫描在执行中，请稍后再试。")

        thread = threading.Thread(
            target=self._run_scan,
            args=(message, codes, berkshire_dir),
            daemon=True,
        )
        try:
            thread.start()
        except Exception as exc:
            _scan_lock.release()
            logger.error("[ScreenCommand] 扫描线程启动失败: %s", exc)
            return BotResponse.error_response("选股扫描启动失败，请稍后重试")

        return BotResponse.markdown_response(
            f"✅ **选股扫描已启动**（{len(codes)} 个标的）\n\n"
            "框架：60日新高+放量 → 6维价值验证 → 信号分级\n"
            "完成后将推送结果。"
        )

    def _run_scan(self, message: BotMessage, codes: List[str], berkshire_dir: Path) -> None:
        try:
            glue = _load_screener_glue()
            screener = glue.load_screener(berkshire_dir)

            scannable = []
            skipped = []
            seen = set()
            for code in codes:
                yahoo, reason = glue.to_yahoo_symbol(code)
                if yahoo is None:
                    skipped.append((code, reason or "未知原因"))
                elif yahoo not in seen:
                    seen.add(yahoo)
                    scannable.append(yahoo)

            buy_signals = []
            watch_signals = []
            for yahoo in scannable:
                result = screener.scan_ticker(yahoo, verbose=False)
                if result is None:
                    skipped.append((yahoo, "价格数据获取失败"))
                    continue
                result["market"] = glue.infer_market(yahoo)
                if result["grade"].startswith("BUY"):
                    buy_signals.append(result)
                elif result["grade"] == "WATCH":
                    watch_signals.append(result)

            # 大盘红绿灯联动：与定时推送同一语义（红灯降级，fail-open）
            markets = {s["market"] for s in buy_signals + watch_signals}
            lights = glue.fetch_market_lights(markets) if markets else {}
            buy_signals, watch_signals, light_notes = glue.apply_market_light(
                buy_signals, watch_signals, lights
            )

            content = glue.build_message(
                buy_signals, watch_signals, len(scannable), skipped,
                light_notes=light_notes,
            )
            self._reply(message, content)
        except Exception as exc:
            logger.error("[ScreenCommand] 扫描失败: %s", exc, exc_info=True)
            self._reply(message, f"❌ 选股扫描失败：{exc}")
        finally:
            _scan_lock.release()

    @staticmethod
    def _reply(message: BotMessage, content: str) -> None:
        from src.notification import NotificationService

        notifier = NotificationService(source_message=message)
        if notifier.is_available():
            notifier.send(content, route_type="alert")
        else:
            logger.warning("[ScreenCommand] 无可用通知渠道，扫描结果未送达")

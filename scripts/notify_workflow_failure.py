#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Actions workflow 失败告警（dead-man's switch）。

在 workflow 的 `if: failure()` 步骤中调用，直接向飞书/企业微信 webhook
推送失败通知。**只用 Python 标准库**：失败可能发生在依赖安装之前，
不能假设 requests / DSA 通知层可用。

环境变量：
- FEISHU_WEBHOOK_URL / FEISHU_WEBHOOK_SECRET（可选签名）
- WECHAT_WEBHOOK_URL
- WORKFLOW_NAME / RUN_URL：告警内容（由 workflow 传入）

任一渠道成功即 exit 0；两渠道都未配置时打印提示并 exit 0（不再级联失败）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request


def _post_json(url: str, payload: dict, timeout: int = 10) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            print(f"POST {url[:40]}... -> {resp.status}: {body[:200]}")
            return 200 <= resp.status < 300
    except Exception as exc:
        print(f"POST {url[:40]}... failed: {exc}")
        return False


def _feishu_sign(secret: str, timestamp: int) -> str:
    """飞书自定义机器人签名：HmacSHA256(timestamp + '\\n' + secret, msg=空)。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send_feishu(text: str) -> bool:
    url = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not url:
        return False
    payload: dict = {"msg_type": "text", "content": {"text": text}}
    secret = os.getenv("FEISHU_WEBHOOK_SECRET", "").strip()
    if secret:
        timestamp = int(time.time())
        payload["timestamp"] = str(timestamp)
        payload["sign"] = _feishu_sign(secret, timestamp)
    return _post_json(url, payload)


def send_wechat(text: str) -> bool:
    url = os.getenv("WECHAT_WEBHOOK_URL", "").strip()
    if not url:
        return False
    return _post_json(url, {"msgtype": "text", "text": {"content": text}})


def main() -> int:
    workflow = os.getenv("WORKFLOW_NAME", "unknown workflow")
    run_url = os.getenv("RUN_URL", "")
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    text = (
        f"🚨 GitHub Actions 运行失败\n"
        f"任务: {workflow}\n"
        f"时间: {now}\n"
        f"日志: {run_url}\n"
        f"（今日推送可能缺失，请检查后手动重跑）"
    )

    feishu_ok = send_feishu(text)
    wechat_ok = send_wechat(text)
    if not os.getenv("FEISHU_WEBHOOK_URL") and not os.getenv("WECHAT_WEBHOOK_URL"):
        print("未配置 FEISHU_WEBHOOK_URL / WECHAT_WEBHOOK_URL，跳过失败告警")
        return 0
    if feishu_ok or wechat_ok:
        print("失败告警已送达")
        return 0
    print("失败告警发送失败（所有已配置渠道均未成功）")
    return 1


if __name__ == "__main__":
    sys.exit(main())

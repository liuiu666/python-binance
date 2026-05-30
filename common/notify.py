"""
通知模块 — 钉钉 Webhook + Telegram Bot API
支持 Markdown 格式消息, 自动签名
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import time
import urllib.parse
from typing import Optional

import httpx

from common.config import settings
from common.logger import get_logger

logger = get_logger(__name__)


class DingTalkNotifier:
    """
    钉钉机器人通知
    使用加签模式 (Sign Secret)
    """

    def __init__(self) -> None:
        self.webhook = settings.dingtalk_webhook
        self.secret = settings.dingtalk_secret
        self._enabled = bool(self.webhook)

    def _build_url(self) -> str:
        """生成带签名的 Webhook URL"""
        if not self.secret:
            return self.webhook

        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(
            self.secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return f"{self.webhook}&timestamp={timestamp}&sign={sign}"

    async def send_markdown(self, title: str, text: str) -> bool:
        """
        发送 Markdown 格式消息

        Args:
            title: 消息标题
            text: Markdown 正文

        Returns:
            是否发送成功
        """
        if not self._enabled:
            logger.debug("notify.dingtalk_skipped", reason="not_configured")
            return False

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    self._build_url(),
                    json={
                        "msgtype": "markdown",
                        "markdown": {"title": title, "text": text},
                    },
                )
                data = resp.json()
                if data.get("errcode") == 0:
                    logger.debug("notify.dingtalk_sent", title=title)
                    return True
                else:
                    logger.warning("notify.dingtalk_error", response=data)
                    return False
        except Exception:
            logger.exception("notify.dingtalk_exception")
            return False


class TelegramNotifier:
    """
    Telegram Bot 通知
    """

    def __init__(self) -> None:
        self.bot_token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id
        self._enabled = bool(self.bot_token and self.chat_id)

    async def send_message(self, text: str) -> bool:
        """
        发送消息 (支持 MarkdownV2 / HTML)

        Args:
            text: 消息正文

        Returns:
            是否发送成功
        """
        if not self._enabled:
            logger.debug("notify.telegram_skipped", reason="not_configured")
            return False

        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    url,
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                    },
                )
                data = resp.json()
                if data.get("ok"):
                    logger.debug("notify.telegram_sent")
                    return True
                else:
                    logger.warning("notify.telegram_error", response=data)
                    return False
        except Exception:
            logger.exception("notify.telegram_exception")
            return False


class Notifier:
    """
    统一通知入口
    同时推送钉钉和 Telegram
    """

    def __init__(self) -> None:
        self.dingtalk = DingTalkNotifier()
        self.telegram = TelegramNotifier()

    async def send(self, title: str, text: str) -> None:
        """
        同时发送到所有已配置的渠道

        Args:
            title: 消息标题 (钉钉使用)
            text: 消息正文 (Markdown / HTML)
        """
        await self.dingtalk.send_markdown(title, text)
        await self.telegram.send_message(text)

    async def notify_trade(
        self,
        action: str,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        pnl: Optional[float] = None,
        reason: str = "",
    ) -> None:
        """发送交易通知"""
        direction = "做多 📈" if side == "BUY" else "做空 📉"
        lines = [
            f"### {'平仓' if action == 'CLOSE' else '开仓'}通知",
            f"**交易对**: {symbol}",
            f"**方向**: {direction}",
            f"**数量**: {quantity}",
            f"**价格**: {price}",
        ]
        if pnl is not None:
            emoji = "🟢" if pnl >= 0 else "🔴"
            lines.append(f"**盈亏**: {emoji} {pnl:+.2f} USDT")
        if reason:
            lines.append(f"**原因**: {reason}")

        await self.send(
            title=f"{action} {symbol} {side}",
            text="\n\n".join(lines),
        )

    async def notify_alert(self, level: str, message: str) -> None:
        """发送系统告警"""
        emoji = {"INFO": "ℹ️", "WARN": "⚠️", "ERROR": "🚨"}.get(level, "📢")
        text = f"### 系统告警\n\n{emoji} **[{level}]** {message}"
        await self.send(title=f"[{level}] 系统告警", text=text)


# 全局单例
notifier = Notifier()

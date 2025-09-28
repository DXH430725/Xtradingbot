from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple

import aiohttp


class TelegramNotifier:
    """Lightweight async Telegram client for strategy notifications."""

    def __init__(self, *, bot_token: str, chat_id: str, logger: Optional[logging.Logger] = None) -> None:
        if not bot_token or not chat_id:
            raise ValueError("bot_token and chat_id required")
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.log = logger or logging.getLogger("mm_bot.execution.telegram")
        self._session: Optional[aiohttp.ClientSession] = None

    @classmethod
    def from_file(cls, path: str | Path, *, logger: Optional[logging.Logger] = None) -> "TelegramNotifier":
        token, chat_id = load_telegram_keys(path)
        if not token or not chat_id:
            raise ValueError(f"telegram keys missing in {path}")
        return cls(bot_token=token, chat_id=chat_id, logger=logger)

    async def ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def send(self, message: str) -> None:
        session = await self.ensure_session()
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": message}
        try:
            async with session.post(url, json=payload, timeout=5) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    self.log.debug("telegram send failed status=%s body=%s", resp.status, text[:200])
        except Exception as exc:
            self.log.debug("telegram send error: %s", exc)


def load_telegram_keys(path: str | Path) -> Tuple[Optional[str], Optional[str]]:
    token = chat_id = None
    path = Path(path)
    if not path.exists():
        return None, None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            value = value.strip().strip('"')
            if key in {"bot_token", "token", "telegram_token"}:
                token = value
            elif key in {"chat_id", "telegram_chat", "channel"}:
                chat_id = value
    except Exception:
        return None, None
    return token, chat_id


__all__ = ["TelegramNotifier", "load_telegram_keys"]

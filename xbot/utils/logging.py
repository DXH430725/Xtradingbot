from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_EXCLUDE = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
}


def _extract_extras(record: logging.LogRecord) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for k, v in record.__dict__.items():
        if k in DEFAULT_EXCLUDE:
            continue
        # Skip internal attributes
        if k.startswith("_"):
            continue
        data[k] = v
    return data


class JsonFormatter(logging.Formatter):
    """Structured JSON formatter with extra fields under 'extra'."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "level": record.levelname,
            "msg": record.getMessage(),
            "logger": record.name,
            "ts": record.created,
        }
        extras = _extract_extras(record)
        if extras:
            payload["extra"] = extras
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True)


class HumanFormatter(logging.Formatter):
    """Console-friendly formatter with concise summaries for key messages."""

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        logger_name = record.name
        msg = record.getMessage()
        extras = _extract_extras(record)
        summary = self._summarize(msg, extras)
        if summary:
            return f"[{ts}] ({logger_name}) {msg} | {summary}"
        # Generic: flatten extras as k=v pairs (short)
        parts = []
        for k, v in extras.items():
            try:
                text = json.dumps(v, ensure_ascii=False)
                if len(text) > 120:
                    text = text[:117] + "..."
            except Exception:
                text = str(v)
            parts.append(f"{k}={text}")
        tail = " ".join(parts)
        return f"[{ts}] ({logger_name}) {msg}{(' | ' + tail) if tail else ''}"

    @staticmethod
    def _to_float(x: Any, default: float = 0.0) -> float:
        try:
            return float(x)
        except Exception:
            return default

    def _summarize(self, msg: str, extras: Dict[str, Any]) -> str:
        # ws_snapshot summary
        if msg == "ws_snapshot":
            phase = extras.get("phase")
            positions = extras.get("positions") or {}
            trades = extras.get("trades") or {}
            balances = extras.get("balances") or {}
            trade_count = 0
            try:
                for v in trades.values():
                    trade_count += len(v or [])
            except Exception:
                trade_count = 0
            return f"phase={phase} pos={len(positions)} trades={trade_count} balances={len(balances)}"

        # order_update summary (Backpack private stream)
        if msg == "order_update":
            data = extras.get("data") or {}
            sym = data.get("s") or data.get("symbol") or "?"
            side = data.get("S") or data.get("side") or "?"
            side = "BUY" if str(side).lower().startswith("bid") else ("SELL" if str(side).lower().startswith("ask") else side)
            status = data.get("X") or data.get("status") or "?"
            price = data.get("p") or data.get("L") or data.get("price")
            filled = self._to_float(data.get("z") or data.get("executedQuantity"))
            qty = self._to_float(data.get("q") or data.get("quantity"))
            remaining = max(0.0, qty - filled) if qty else 0.0
            return f"{sym} {price} {side} filled={filled} remaining={remaining} status={status}"

        # tracking_done summary
        if msg == "tracking_done":
            attempts = extras.get("attempts") or []
            filled_i = extras.get("filled_base_i")
            return f"attempts={len(attempts)} filled_i={filled_i}"

        # limit order summaries
        if msg in ("limit_order_open", "close_market_submitted"):
            price = extras.get("price") or extras.get("price_i")
            size = extras.get("size") or extras.get("size_i")
            coi = extras.get("coi")
            return f"price={price} size={size}{(' coi=' + str(coi)) if coi is not None else ''}"

        return ""


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Console: human-readable
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(HumanFormatter())

    # File: structured JSON lines
    logs_dir = Path("logs")
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    file_path = logs_dir / "app.jsonl"
    file_handler = logging.FileHandler(file_path, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())

    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)


def get_logger(name: str, *, level: Optional[str] = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if level:
        logger.setLevel(level.upper())
    return logger


__all__ = ["setup_logging", "get_logger", "JsonFormatter", "HumanFormatter"]

"""Persistent state for confirmed active trades and pending alert details."""
import json
import os
import threading
from datetime import datetime
from config import TIMEZONE, ACTIVE_TRADES_FILE, PENDING_ALERTS_FILE

_file_lock = threading.Lock()


def _load(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save(path: str, data: dict) -> None:
    with _file_lock:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2, default=str)
            os.replace(tmp, path)
        except Exception as e:
            print(f"[ActiveTrades] Save error ({path}): {e}")


def load_trades() -> dict:
    return _load(ACTIVE_TRADES_FILE)


def save_trade(trade: dict) -> None:
    trades = load_trades()
    trades[trade["symbol"]] = trade
    _save(ACTIVE_TRADES_FILE, trades)


def remove_trade(symbol: str) -> None:
    trades = load_trades()
    if symbol in trades:
        del trades[symbol]
        _save(ACTIVE_TRADES_FILE, trades)


def update_trade_fields(symbol: str, fields: dict) -> None:
    trades = load_trades()
    if symbol in trades:
        trades[symbol].update(fields)
        _save(ACTIVE_TRADES_FILE, trades)


def get_all_trades() -> dict:
    return load_trades()


def save_pending_alert(result: dict) -> None:
    pending = _load(PENDING_ALERTS_FILE)
    pending[result["symbol"]] = {
        "symbol":       result.get("symbol"),
        "price":        result.get("price"),
        "stop_loss":    result.get("stop_loss"),
        "price_target": result.get("price_target"),
        "signal":       result.get("signal"),
        "trade_type":   result.get("trade_type"),
        "scanner":      result.get("scanner", ""),
        "conviction":   result.get("conviction", "SPECULATIVE"),
        "position_size": result.get("position_size", 0),
        "rsi":          result.get("rsi", 0),
        "atr":          result.get("atr", 0),
        "alerted_at":   datetime.now(TIMEZONE).isoformat(),
    }
    _save(PENDING_ALERTS_FILE, pending)


def get_pending_alert(symbol: str) -> dict:
    return _load(PENDING_ALERTS_FILE).get(symbol, {})


def get_all_pending_alerts() -> dict:
    return _load(PENDING_ALERTS_FILE)


def clear_pending_alerts() -> None:
    _save(PENDING_ALERTS_FILE, {})

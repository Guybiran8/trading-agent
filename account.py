"""Account tracking — balance, positions, daily P&L."""
import json
import os
import threading
from datetime import datetime, date
from config import (
    ACCOUNT_FILE, STARTING_BALANCE,
    HIGH_CONVICTION_SIZE, SPECULATIVE_SIZE,
    BALANCE_THRESHOLD_MEDIUM, BALANCE_THRESHOLD_LOW,
    MAX_TRADE_MEDIUM, MAX_TRADE_LOW,
    MAX_OPEN_TRADES, MAX_DEPLOYED, TIMEZONE,
)

_lock = threading.Lock()

_DEFAULT = {
    "starting_balance":  STARTING_BALANCE,
    "current_balance":   STARTING_BALANCE,
    "total_pnl":         0.0,
    "daily_pnl":         {},
    "open_positions":    {},
    "last_updated":      "",
}


def _load() -> dict:
    if os.path.exists(ACCOUNT_FILE):
        try:
            with open(ACCOUNT_FILE) as f:
                data = json.load(f)
            data.setdefault("daily_pnl", {})
            return data
        except Exception:
            pass
    return _DEFAULT.copy()


def _save(data: dict) -> None:
    with _lock:
        tmp = ACCOUNT_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, ACCOUNT_FILE)


def get_balance() -> float:
    return float(_load().get("current_balance", STARTING_BALANCE))


def get_position_size(conviction: str) -> float:
    balance = get_balance()
    if balance < BALANCE_THRESHOLD_LOW:
        cap = MAX_TRADE_LOW
    elif balance < BALANCE_THRESHOLD_MEDIUM:
        cap = MAX_TRADE_MEDIUM
    else:
        cap = None
    base = HIGH_CONVICTION_SIZE if conviction == "HIGH" else SPECULATIVE_SIZE
    return min(base, cap) if cap else base


def get_risk_amount(conviction: str) -> float:
    pct = 0.02 if conviction == "HIGH" else 0.01
    return round(get_balance() * pct, 2)


def get_deployed() -> float:
    data = _load()
    return sum(float(p.get("size", 0)) for p in data.get("open_positions", {}).values())


def can_open_trade() -> bool:
    data     = _load()
    open_pos = data.get("open_positions", {})
    deployed = sum(float(p.get("size", 0)) for p in open_pos.values())
    return len(open_pos) < MAX_OPEN_TRADES and deployed < MAX_DEPLOYED


def update_balance(new_balance: float) -> None:
    data = _load()
    data["current_balance"] = round(new_balance, 2)
    data["last_updated"]    = datetime.now(TIMEZONE).isoformat()
    _save(data)


def record_daily_pnl(pnl_dollar: float) -> None:
    data     = _load()
    today    = date.today().isoformat()
    daily    = data.setdefault("daily_pnl", {})
    daily[today] = round(float(daily.get(today, 0)) + pnl_dollar, 2)
    data["last_updated"] = datetime.now(TIMEZONE).isoformat()
    _save(data)


def get_daily_pnl() -> float:
    data  = _load()
    today = date.today().isoformat()
    return float(data.get("daily_pnl", {}).get(today, 0))


def add_position(symbol: str, size: float, entry: float, trade_type: str = "SWING") -> None:
    data = _load()
    data.setdefault("open_positions", {})[symbol] = {
        "size":       round(size, 2),
        "entry":      round(entry, 2),
        "trade_type": trade_type,
        "opened":     datetime.now(TIMEZONE).isoformat(),
    }
    data["last_updated"] = datetime.now(TIMEZONE).isoformat()
    _save(data)


def close_position(symbol: str, exit_price: float, is_short: bool = False) -> float:
    data = _load()
    pos  = data.get("open_positions", {}).pop(symbol, None)
    pnl  = 0.0
    if pos:
        entry  = float(pos.get("entry", exit_price))
        size   = float(pos.get("size", 0))
        shares = size / entry if entry else 0
        raw    = (exit_price - entry) * shares
        if is_short:
            raw = -raw
        pnl = round(raw, 2)
        data["current_balance"] = round(float(data.get("current_balance", STARTING_BALANCE)) + pnl, 2)
        data["total_pnl"]       = round(float(data.get("total_pnl", 0)) + pnl, 2)

        today = date.today().isoformat()
        daily = data.setdefault("daily_pnl", {})
        daily[today] = round(float(daily.get(today, 0)) + pnl, 2)

        data["last_updated"] = datetime.now(TIMEZONE).isoformat()
        _save(data)
    return pnl


def get_summary() -> dict:
    data     = _load()
    open_pos = data.get("open_positions", {})
    deployed = sum(float(p.get("size", 0)) for p in open_pos.values())
    balance  = float(data.get("current_balance", STARTING_BALANCE))
    starting = float(data.get("starting_balance", STARTING_BALANCE))
    return {
        "balance":        balance,
        "starting":       starting,
        "deployed":       round(deployed, 2),
        "available":      round(balance - deployed, 2),
        "total_pnl":      round(float(data.get("total_pnl", 0)), 2),
        "daily_pnl":      get_daily_pnl(),
        "pnl_pct":        round((balance - starting) / starting * 100, 2) if starting else 0,
        "open_count":     len(open_pos),
        "open_positions": open_pos,
    }


def ensure_account_file() -> None:
    if not os.path.exists(ACCOUNT_FILE):
        _save(_DEFAULT.copy())

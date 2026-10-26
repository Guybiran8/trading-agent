"""Exit rules applied to every alert before it is sent (2026-10-26 fix).

- Stop at least STOP_MIN_ATR x daily ATR(14) from the alert price (only ever widened).
  When the stop is widened the position is shrunk so the dollar risk stays the same.
- Target exactly TARGET_ATR x ATR from the alert price.
- Time exit after MAX_HOLD_DAYS trading days (entry day = day 1), new trades only:
  trades without "max_hold_days" (opened before this fix) are never time-exited.

k = 2.5 and N = 10 were picked on in-sample backtest days only
(backtest/strategies.py: swing_new).
"""
from datetime import date, datetime, time as dtime

import numpy as np

STOP_MIN_ATR  = 1.5
TARGET_ATR    = 2.5
MAX_HOLD_DAYS = 10
TIME_EXIT_AT  = dtime(15, 50)        # on the last day, exit near the close (ET)

SHORT_SIGNALS = ("SHORT", "STRONG SELL", "SELL")


def daily_atr(symbol: str, n: int = 14) -> float:
    """Simple n-day mean true range from daily bars (same as indicators.py). 0.0 if unavailable."""
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period="2mo", interval="1d")
        if df is None or len(df) < n + 1:
            return 0.0
        h, l, c = df["High"], df["Low"], df["Close"]
        pc = c.shift(1)
        tr = np.maximum(h - l, np.maximum((h - pc).abs(), (l - pc).abs()))
        atr = float(tr.iloc[-n:].mean())
        return atr if np.isfinite(atr) and atr > 0 else 0.0
    except Exception:
        return 0.0


def apply_exit_rules(result: dict, atr: float | None = None) -> bool:
    """Set stop / target / max_hold_days on an alert in place.

    Uses result["atr"] when present and positive, else `atr`, else fetches it.
    Stores the pre-rule stop as "original_stop" and the risk ratio
    old_stop_distance / new_stop_distance as "risk_scale" (<= 1).
    Returns False (alert should be skipped) when there is no usable ATR or price."""
    price = float(result.get("price") or 0)
    if price <= 0:
        return False
    if not atr:
        atr = float(result.get("atr") or 0) or daily_atr(result.get("symbol", ""))
    if not atr or atr <= 0:
        return False

    is_short = result.get("signal", "") in SHORT_SIGNALS
    side     = -1 if is_short else 1
    old_stop = float(result.get("stop_loss") or 0)
    old_dist = side * (price - old_stop) if old_stop else 0.0
    min_dist = STOP_MIN_ATR * atr

    new_dist = max(old_dist, min_dist)
    result["original_stop"] = old_stop
    result["stop_loss"]     = round(price - side * new_dist, 2)
    result["price_target"]  = round(price + side * TARGET_ATR * atr, 2)
    result["atr"]           = round(atr, 4)
    result["risk_scale"]    = old_dist / new_dist if old_dist > 0 else 1.0
    result["max_hold_days"] = MAX_HOLD_DAYS
    return True


def scale_position(size: float, risk_scale: float) -> float:
    """Shrink a position so dollar risk is unchanged after a wider stop."""
    return round(size * min(max(risk_scale, 0.0), 1.0), 2)


def trading_day_number(entry_day: date, today: date) -> int:
    """1 on the entry day, 2 on the next weekday, ... (weekends skipped, holidays not)."""
    return int(np.busday_count(entry_day, today)) + 1


def time_exit_due(trade: dict, now: datetime) -> bool:
    """True once a new-style trade reaches the close of its last allowed day."""
    max_days = trade.get("max_hold_days")
    started  = trade.get("entry_time") or trade.get("confirmed_at")
    if not max_days or not started:
        return False
    try:
        entry_day = datetime.fromisoformat(str(started)).date()
    except ValueError:
        return False
    day_n = trading_day_number(entry_day, now.date())
    return day_n > int(max_days) or (day_n == int(max_days) and now.time() >= TIME_EXIT_AT)

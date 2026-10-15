"""Daily loss limit — stops new alerts if confirmed trades lose $500+ in one day.

Also provides correlation warning when opening new position in same sector.
"""
import threading
from datetime import datetime, date
from config import DAILY_LOSS_LIMIT, TIMEZONE

_lock = threading.Lock()
_daily_pnl: dict = {}        # date_str -> float
_limit_hit_today: date = None


def record_trade_pnl(pnl_dollar: float) -> None:
    today = date.today()
    with _lock:
        today_str = today.isoformat()
        _daily_pnl[today_str] = _daily_pnl.get(today_str, 0.0) + pnl_dollar
        if _daily_pnl[today_str] <= -DAILY_LOSS_LIMIT:
            global _limit_hit_today
            _limit_hit_today = today


def is_daily_limit_hit() -> bool:
    today = date.today()
    with _lock:
        return _limit_hit_today == today


def get_daily_pnl() -> float:
    today = date.today().isoformat()
    with _lock:
        return _daily_pnl.get(today, 0.0)


def reset_day() -> None:
    with _lock:
        global _limit_hit_today
        _limit_hit_today = None


def check_correlation(symbol: str) -> dict:
    """Check if there are open trades in the same sector as symbol.

    Returns dict with: has_warn, level ('single'/'double'), open_syms
    """
    try:
        import yfinance as yf
        from active_trades import get_all_trades
        from config import SECTOR_ETFS

        info   = yf.Ticker(symbol).info
        sector = info.get("sector", "")
        if not sector:
            return {"has_warn": False}

        trades   = get_all_trades()
        same_sec = []
        for sym, t in trades.items():
            if sym == symbol:
                continue
            try:
                inf2    = yf.Ticker(sym).info
                sec2    = inf2.get("sector", "")
                if sec2 == sector:
                    same_sec.append(sym)
            except Exception:
                pass

        if not same_sec:
            return {"has_warn": False}

        level = "double" if len(same_sec) >= 2 else "single"
        return {
            "has_warn":  True,
            "level":     level,
            "sector":    sector,
            "open_syms": same_sec,
        }
    except Exception:
        return {"has_warn": False}

"""Sector rotation detection — runs at 9:00 AM and refreshes every 30 minutes.

Ranks all 11 sector ETFs by 5-day performance.
Top 3 sectors get +20 bonus, bottom 3 get -20 penalty in all scanners.
"""
import yfinance as yf
import threading
from datetime import datetime, date
from config import TIMEZONE, SECTOR_ETFS

_lock = threading.Lock()
_rankings: list = []        # sorted list of (name, etf, pct_5d)
_top_3_etfs: set = set()    # ETF tickers of top 3
_bottom_3_etfs: set = set() # ETF tickers of bottom 3
_sector_of: dict = {}       # symbol -> sector name (cache)
_last_run: date = None


def run_sector_rotation() -> dict:
    global _rankings, _top_3_etfs, _bottom_3_etfs, _last_run

    results = []
    for name, etf in SECTOR_ETFS.items():
        try:
            df = yf.Ticker(etf).history(period="2mo", interval="1d")
            if df is None or df.empty or len(df) < 5:
                continue
            pct_5d = round((float(df["Close"].iloc[-1]) - float(df["Close"].iloc[-6])) / float(df["Close"].iloc[-6]) * 100, 2)
            pct_1d = round((float(df["Close"].iloc[-1]) - float(df["Close"].iloc[-2])) / float(df["Close"].iloc[-2]) * 100, 2)
            results.append({"name": name, "etf": etf, "pct_5d": pct_5d, "pct_1d": pct_1d})
        except Exception:
            continue

    results.sort(key=lambda x: x["pct_5d"], reverse=True)

    with _lock:
        _rankings = results
        _top_3_etfs    = {r["etf"] for r in results[:3]}
        _bottom_3_etfs = {r["etf"] for r in results[-3:]}
        _last_run = date.today()

    print(f"[SectorRotation] Top: {[r['name'] for r in results[:3]]} | Bottom: {[r['name'] for r in results[-3:]]}")
    return {
        "rankings":  results,
        "top_3":     results[:3],
        "bottom_3":  results[-3:],
    }


def get_sector_score(symbol: str) -> tuple:
    """Return (score_bonus, sector_name, rank) for a symbol."""
    sector = _get_sector(symbol)
    if not sector:
        return 0, "Unknown", 0

    etf = SECTOR_ETFS.get(sector, "")
    with _lock:
        if etf in _top_3_etfs:
            rank = next((i + 1 for i, r in enumerate(_rankings) if r["etf"] == etf), 0)
            return 20, sector, rank
        if etf in _bottom_3_etfs:
            rank = next((i + 1 for i, r in enumerate(_rankings) if r["etf"] == etf), 0)
            return -20, sector, rank
        rank = next((i + 1 for i, r in enumerate(_rankings) if r["etf"] == etf), 0)
        return 0, sector, rank


def _get_sector(symbol: str) -> str:
    with _lock:
        if symbol in _sector_of:
            return _sector_of[symbol]
    try:
        info   = yf.Ticker(symbol).info
        sector = info.get("sector", "")
        with _lock:
            _sector_of[symbol] = sector
        return sector
    except Exception:
        return ""


def get_top_sectors() -> list:
    with _lock:
        return [{"name": r["name"], "pct_5d": r["pct_5d"], "pct_1d": r["pct_1d"]} for r in _rankings[:3]]


def get_bottom_sectors() -> list:
    with _lock:
        return [{"name": r["name"], "pct_5d": r["pct_5d"], "pct_1d": r["pct_1d"]} for r in _rankings[-3:]]


def get_all_rankings() -> list:
    with _lock:
        return list(_rankings)


def is_stale() -> bool:
    with _lock:
        return _last_run != date.today() or not _rankings

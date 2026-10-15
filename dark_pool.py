"""Dark pool / institutional block trade detector.

Uses Polygon trades API for large off-exchange transactions.
Falls back to large-volume-bar detection via yfinance.
"""
import requests
import yfinance as yf
from datetime import datetime, date
from config import POLYGON_API_KEY, TIMEZONE

_cache: dict = {}
_cache_date: date = None


def _reset_cache():
    global _cache, _cache_date
    today = date.today()
    if _cache_date != today:
        _cache      = {}
        _cache_date = today


def _check_polygon_trades(symbol: str) -> dict:
    try:
        url = (
            f"https://api.polygon.io/v3/trades/{symbol}"
            f"?limit=50&order=desc&sort=timestamp&apiKey={POLYGON_API_KEY}"
        )
        r = requests.get(url, timeout=5)
        if r.status_code != 200:
            return {}

        trades = r.json().get("results", [])
        large_trades = []
        for t in trades:
            size   = int(t.get("size", 0))
            price  = float(t.get("price", 0))
            dollar = size * price
            if size >= 100_000 or dollar >= 1_000_000:
                large_trades.append({"size": size, "price": price, "dollar": dollar})

        if not large_trades:
            return {}

        total_shares = sum(t["size"] for t in large_trades)
        total_dollar = sum(t["dollar"] for t in large_trades)
        return {
            "detected":     True,
            "trade_count":  len(large_trades),
            "total_shares": total_shares,
            "total_dollar": total_dollar,
            "detail":       f"{len(large_trades)} block trade(s), ~${total_dollar/1e6:.1f}M total",
        }
    except Exception:
        return {}


def _check_volume_bars(symbol: str) -> dict:
    try:
        df = yf.Ticker(symbol).history(period="2d", interval="1m")
        if df is None or df.empty or len(df) < 20:
            return {}

        avg_vol = float(df["Volume"].mean())
        if avg_vol <= 0:
            return {}

        large_bars = []
        for i, row in df.tail(30).iterrows():
            vol = float(row["Volume"])
            price = float(row["Close"])
            dollar = vol * price
            if vol >= 100_000 or dollar >= 1_000_000:
                if vol > avg_vol * 3:
                    large_bars.append({"vol": vol, "dollar": dollar})

        if not large_bars:
            return {}

        total_dollar = sum(b["dollar"] for b in large_bars)
        return {
            "detected":    True,
            "trade_count": len(large_bars),
            "total_dollar": total_dollar,
            "detail":      f"{len(large_bars)} large block(s), ~${total_dollar/1e6:.1f}M",
        }
    except Exception:
        return {}


def check_dark_pool(symbol: str) -> dict:
    _reset_cache()
    if symbol in _cache:
        return _cache[symbol]

    result = _check_polygon_trades(symbol)
    if not result:
        result = _check_volume_bars(symbol)
    if not result:
        result = {"detected": False, "detail": ""}

    _cache[symbol] = result
    return result


def get_dark_pool_flag(symbol: str) -> tuple:
    """Returns (detected: bool, detail_str: str)."""
    data = check_dark_pool(symbol)
    if data.get("detected"):
        return True, data.get("detail", "Dark pool activity detected")
    return False, ""

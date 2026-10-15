import os
import sqlite3
import threading
import time
import pandas as pd
import requests
import yfinance as yf
import yfinance.cache as _yf_cache
from datetime import datetime, timedelta
from config import POLYGON_API_KEY, TIMEZONE


class _MemoryTzCache:
    """Thread-safe in-memory replacement for yfinance's sqlite ticker->timezone cache.

    yfinance's sqlite caches open one connection per thread. Threads that exit without
    closing it leak a file descriptor for good (SQLite won't release it while another
    connection in the process holds a lock on the file), and yf.download() of a ticker
    list spawns its own internal threads that we can never close. Over days that hit the
    1024 open-files limit. One shared dict means no per-thread connections at all.
    """

    def __init__(self, seed: dict):
        self._d = dict(seed)
        self._lock = threading.Lock()

    def lookup(self, key):
        with self._lock:
            return self._d.get(key)

    def store(self, key, value):
        with self._lock:
            if value is None:
                self._d.pop(key, None)
            else:
                self._d[key] = value


def _install_memory_yf_caches() -> None:
    """Swap yfinance's sqlite caches for in-memory ones, seeded once from the tz cache file."""
    seed = {}
    path = os.path.join(_yf_cache._TzDBManager.get_location(), "tkr-tz.db")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            seed = dict(conn.execute("SELECT key, value FROM _tz_kv WHERE value IS NOT NULL"))
        finally:
            conn.close()
    except Exception:
        pass
    _yf_cache._TzCacheManager._tz_cache = _MemoryTzCache(seed)
    _yf_cache._CookieCacheManager._Cookie_cache = _yf_cache._CookieCacheDummy()
    _yf_cache._ISINCacheManager._isin_cache = _yf_cache._ISINCacheDummy()


_install_memory_yf_caches()


def close_yf_thread_cache() -> None:
    """Release this thread's yfinance sqlite cache connections (tz/cookie/isin).

    Now a no-op safety net: _install_memory_yf_caches() means no sqlite connections are
    opened. Kept so existing callers don't need to change.
    """
    try:
        from yfinance.cache import _TzDBManager, _CookieDBManager, _ISINDBManager
        for mgr in (_TzDBManager, _CookieDBManager, _ISINDBManager):
            try:
                mgr.close_db()
            except Exception:
                pass
    except Exception:
        pass


def run_and_release(fn, *args, **kwargs):
    """Run fn(*args, **kwargs) then release this thread's yfinance cache connections.

    Use this to wrap every ThreadPoolExecutor.submit() call whose worker touches
    yfinance, so the worker thread doesn't leak a file descriptor when it exits.
    """
    try:
        return fn(*args, **kwargs)
    finally:
        close_yf_thread_cache()


def get_stock_data(symbol, period="6mo", interval="1d"):
    try:
        df = yf.download(symbol, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df is None or df.empty or len(df) < 30:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.loc[:, ~df.columns.duplicated()]
        required = ["Open", "High", "Low", "Close", "Volume"]
        if not all(c in df.columns for c in required):
            return None
        df.dropna(inplace=True)
        return df
    except Exception:
        return None


def get_intraday_1m(symbol: str) -> pd.DataFrame:
    try:
        df = yf.Ticker(symbol).history(period="2d", interval="1m")
        if df is None or df.empty:
            return pd.DataFrame()
        today = datetime.now(TIMEZONE).date()
        df = df[df.index.tz_convert(TIMEZONE).date == today] if df.index.tz else df
        return df
    except Exception:
        return pd.DataFrame()


def get_intraday_5m(symbol: str) -> pd.DataFrame:
    try:
        df = yf.Ticker(symbol).history(period="5d", interval="5m")
        return df if df is not None and not df.empty else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def get_current_price(symbol: str) -> float:
    try:
        info = yf.Ticker(symbol).fast_info
        return float(getattr(info, "last_price", 0) or 0)
    except Exception:
        return 0.0


# ── Polygon rate limiter ──────────────────────────────────────────────────────
# This account is on Polygon's 5-requests/minute tier (confirmed empirically: a 6th
# rapid call returns 429). Every Polygon call in this module goes through
# _polygon_rate_limit() so callers never need to think about pacing themselves —
# it blocks just long enough to stay under the limit, rather than the caller
# guessing a sleep duration or getting silently 429'd.
_polygon_call_times: list = []
_polygon_rate_lock = threading.Lock()
POLYGON_MAX_CALLS_PER_MIN = 5


def _polygon_rate_limit() -> None:
    with _polygon_rate_lock:
        now = time.time()
        while _polygon_call_times and now - _polygon_call_times[0] > 60:
            _polygon_call_times.pop(0)
        if len(_polygon_call_times) >= POLYGON_MAX_CALLS_PER_MIN:
            sleep_for = 60 - (now - _polygon_call_times[0]) + 0.1
        else:
            sleep_for = 0
    if sleep_for > 0:
        time.sleep(sleep_for)
    with _polygon_rate_lock:
        _polygon_call_times.append(time.time())


def get_polygon_bars(symbol: str, from_dt: datetime, to_dt: datetime,
                      multiplier: int = 1, timespan: str = "minute") -> list:
    """Fetch OHLC bars from Polygon for [from_dt, to_dt], rate-limited to this
    account's plan. Returns a list of dicts with keys t (ms epoch UTC), o, h, l, c, v,
    sorted ascending. Empty list on any failure (bad symbol, no data, network error)."""
    _polygon_rate_limit()
    try:
        frm = from_dt.strftime("%Y-%m-%d")
        to = to_dt.strftime("%Y-%m-%d")
        url = (f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/"
               f"{multiplier}/{timespan}/{frm}/{to}"
               f"?adjusted=true&sort=asc&limit=50000&apiKey={POLYGON_API_KEY}")
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return []
        results = r.json().get("results", []) or []
        # Polygon's range is whole calendar days; trim to the exact [from_dt, to_dt]
        # window so callers never see bars from before from_dt (e.g. 04:00 premarket
        # bars for a 13:05 alert). A bar is kept only if it STARTS at/after from_dt.
        lo_ms = from_dt.timestamp() * 1000
        hi_ms = to_dt.timestamp() * 1000
        return [b for b in results if lo_ms <= b.get("t", 0) <= hi_ms]
    except Exception:
        return []


def get_polygon_snapshot(symbol: str) -> dict:
    try:
        url = (f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/"
               f"{symbol}?apiKey={POLYGON_API_KEY}")
        r = requests.get(url, timeout=5).json()
        t = r.get("ticker", {})
        day = t.get("day", {})
        return {
            "symbol":     symbol,
            "price":      t.get("lastTrade", {}).get("p", 0) or day.get("c", 0),
            "open":       day.get("o", 0),
            "high":       day.get("h", 0),
            "low":        day.get("l", 0),
            "volume":     day.get("v", 0),
            "change_pct": t.get("todaysChangePerc", 0),
        }
    except Exception:
        return {}


def get_polygon_news(limit: int = 50, published_after_minutes: int = 65) -> list:
    try:
        cutoff = (datetime.utcnow() - timedelta(minutes=published_after_minutes))
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        url = (f"https://api.polygon.io/v2/reference/news"
               f"?limit={limit}&order=desc&sort=published_utc"
               f"&published_utc.gte={cutoff_str}&apiKey={POLYGON_API_KEY}")
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return []
        items = r.json().get("results", [])
        return items
    except Exception:
        return []


def get_ticker_news(symbol: str, limit: int = 5) -> list:
    try:
        url = (f"https://api.polygon.io/v2/reference/news"
               f"?ticker={symbol}&limit={limit}&order=desc"
               f"&sort=published_utc&apiKey={POLYGON_API_KEY}")
        r = requests.get(url, timeout=5)
        return r.json().get("results", []) if r.status_code == 200 else []
    except Exception:
        return []


def is_halted(symbol: str) -> bool:
    try:
        snap = get_polygon_snapshot(symbol)
        return snap.get("volume", 1) == 0 and snap.get("price", 0) == 0
    except Exception:
        return False


def get_sp500_symbols() -> list:
    try:
        url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv"
        df  = pd.read_csv(url)
        return df["Symbol"].tolist()
    except Exception:
        return []


def _fetch_nasdaq_api(exchange: str) -> list:
    try:
        url = (f"https://api.nasdaq.com/api/screener/stocks"
               f"?tableonly=true&limit=5000&exchange={exchange}")
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        rows = r.json().get("data", {}).get("table", {}).get("rows", [])
        return [row["symbol"].strip() for row in rows
                if row.get("symbol", "").strip().isalpha()
                and 1 <= len(row["symbol"].strip()) <= 5]
    except Exception:
        return []


def get_all_symbols() -> list:
    sp500   = get_sp500_symbols()
    nasdaq  = _fetch_nasdaq_api("nasdaq")
    nyse    = _fetch_nasdaq_api("nyse")
    combined = list(set(sp500 + nasdaq + nyse))
    print(f"Universe: {len(combined)} symbols (S&P500 + NASDAQ + NYSE)")
    return combined


def get_earnings_date(symbol: str):
    try:
        cal = yf.Ticker(symbol).calendar
        if cal is None:
            return None
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date", [])
            if dates:
                return pd.Timestamp(dates[0]).date()
        return None
    except Exception:
        return None

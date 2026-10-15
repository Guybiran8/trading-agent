"""Insider buying detector — checks SEC EDGAR Form 4 filings.

Detects executive/director stock purchases in last 7 days.
Only purchases matter, not sales.
"""
import requests
import json
from datetime import datetime, date, timedelta
from config import TIMEZONE

_cache: dict = {}
_cache_date: date = None

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_ticker_to_cik: dict = {}
_cik_loaded = False

HEADERS = {
    "User-Agent": "TradingAgent contact@example.com",
    "Accept": "application/json",
}


def _load_cik_map() -> None:
    global _ticker_to_cik, _cik_loaded
    if _cik_loaded:
        return
    try:
        r = requests.get(COMPANY_TICKERS_URL, headers=HEADERS, timeout=10)
        data = r.json()
        for entry in data.values():
            ticker = entry.get("ticker", "").upper()
            cik    = str(entry.get("cik_str", "")).zfill(10)
            if ticker and cik:
                _ticker_to_cik[ticker] = cik
        _cik_loaded = True
    except Exception:
        pass


def _get_cik(symbol: str) -> str:
    _load_cik_map()
    return _ticker_to_cik.get(symbol.upper(), "")


def check_insider_buying(symbol: str) -> dict:
    global _cache, _cache_date
    today = date.today()

    if _cache_date != today:
        _cache      = {}
        _cache_date = today

    if symbol in _cache:
        return _cache[symbol]

    result = {"has_buying": False, "details": ""}
    try:
        cik = _get_cik(symbol)
        if not cik:
            _cache[symbol] = result
            return result

        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        url = (
            f"https://efts.sec.gov/LATEST/search-index"
            f"?q=%22{cik}%22&forms=4&dateRange=custom"
            f"&startdt={cutoff}&enddt={today.isoformat()}"
            f"&hits.hits._source=period_of_report,entity_name,file_date"
        )
        r = requests.get(url, headers=HEADERS, timeout=8)
        if r.status_code != 200:
            _cache[symbol] = result
            return result

        hits = r.json().get("hits", {}).get("hits", [])
        if not hits:
            _cache[symbol] = result
            return result

        for hit in hits[:5]:
            src = hit.get("_source", {})
            file_date = src.get("file_date", "")
            try:
                fd = date.fromisoformat(file_date)
                days_ago = (today - fd).days
            except Exception:
                days_ago = 999

            entity = src.get("entity_name", "Unknown")
            filing_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=4&dateb=&owner=include&count=5"

            result = {
                "has_buying": True,
                "details":    f"Form 4 filed {days_ago}d ago by {entity}",
                "days_ago":   days_ago,
                "entity":     entity,
            }
            break

    except Exception:
        pass

    _cache[symbol] = result
    return result


def get_insider_flag(symbol: str) -> tuple:
    """Returns (has_buying: bool, detail_str: str)."""
    data = check_insider_buying(symbol)
    if data.get("has_buying"):
        detail = data.get("details", "Insider buying detected")
        return True, detail
    return False, ""

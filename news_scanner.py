"""News scanner — Polygon news API, every 60 seconds all day."""
from datetime import datetime, timedelta
from data_fetcher import get_polygon_news, get_ticker_news
from config import TIMEZONE

POSITIVE_KEYWORDS = [
    "earnings beat", "beat estimates", "raised guidance", "fda approval",
    "approved", "contract won", "partnership", "acquisition", "merger",
    "buyout", "price target increase", "insider buying",
    "short squeeze", "record revenue", "record earnings", "breakthrough",
    "positive trial", "deal signed", "award contract",
]

NEGATIVE_KEYWORDS = [
    "dilution", "secondary offering", "public offering", "sec investigation",
    "lawsuit", "guidance cut", "guidance lowered", "guidance reduced",
    "earnings miss", "missed estimates", "bankruptcy", "fraud",
    "recall", "warning letter", "subpoena", "downgrade",
    "fda rejection", "complete response letter",
]

WAR_KEYWORDS = [
    "military strike launched", "airstrike on", "missile attack on",
    "invasion begins", "war declared", "military offensive launched",
    "ceasefire declared", "armed forces attack", "nato activates",
    "bombs dropped", "ground invasion",
]
FED_KEYWORDS = [
    "federal reserve raises rates", "federal reserve cuts rates",
    "fed raises rates", "fed cuts rates", "fomc decision announced",
    "interest rate hike announced", "interest rate cut announced",
    "fed funds rate raised", "fed funds rate cut",
]
TRADE_KEYWORDS = [
    "tariff increase", "trade deal", "trade war", "sanctions imposed",
    "export ban", "import duty",
]
OIL_KEYWORDS = [
    "oil price surge", "crude oil spike", "opec cut", "brent crude",
    "energy price spike",
]
INFLATION_KEYWORDS = [
    "cpi report", "ppi report", "consumer price index", "inflation data",
    "inflation report", "core inflation",
]

DEFENSE_STOCKS   = ["LMT", "RTX", "NOC", "GD", "BA", "LDOS", "CACI", "SAIC"]
OIL_STOCKS       = ["XOM", "CVX", "COP", "OXY", "SLB", "HAL", "PSX", "MPC"]
GOLD_STOCKS      = ["GLD", "GDX", "NEM", "GOLD", "AEM", "WPM"]
BANK_STOCKS      = ["JPM", "BAC", "GS", "MS", "WFC", "C", "USB", "PNC"]
ENERGY_STOCKS    = ["XLE", "XOM", "CVX", "COP", "NEE", "DUK"]
RATE_SENS_STOCKS = ["TLT", "IEF", "VNQ", "XLRE", "XLF"]

_flagged_stocks: dict = {}
_news_events: list = []


def scan_news() -> dict:
    global _flagged_stocks, _news_events
    now = datetime.now(TIMEZONE)

    articles = get_polygon_news(limit=50, published_after_minutes=65)
    new_flagged = {}
    new_events = []

    for article in articles:
        pub_str = article.get("published_utc", "")
        title   = article.get("title", "") or ""
        desc    = article.get("description", "") or ""
        tickers = article.get("tickers", []) or []
        text    = (title + " " + desc).lower()

        try:
            pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            minutes_old = (datetime.now(tz=pub_dt.tzinfo) - pub_dt).total_seconds() / 60
        except Exception:
            minutes_old = 999

        if minutes_old < 30:
            age_score = 40
            age_label = "BREAKING"
        elif minutes_old < 120:
            age_score = 25
            age_label = "RECENT"
        else:
            age_score = 10
            age_label = "OLDER"

        pos_hits = [kw for kw in POSITIVE_KEYWORDS if kw in text]
        neg_hits = [kw for kw in NEGATIVE_KEYWORDS if kw in text]
        high_risk = bool(neg_hits)
        news_score = age_score + (30 if pos_hits else 0) + (-40 if neg_hits else 0)

        macro_type = None
        benefiting = []
        if any(kw in text for kw in WAR_KEYWORDS):
            macro_type = "WAR_ESCALATION"
            benefiting = DEFENSE_STOCKS + OIL_STOCKS + GOLD_STOCKS
        elif any(kw in text for kw in FED_KEYWORDS):
            macro_type = "FED_DECISION"
            benefiting = BANK_STOCKS + RATE_SENS_STOCKS
        elif any(kw in text for kw in TRADE_KEYWORDS):
            macro_type = "TRADE_NEWS"
            benefiting = tickers[:10]
        elif any(kw in text for kw in OIL_KEYWORDS):
            macro_type = "OIL_MOVE"
            benefiting = OIL_STOCKS + ENERGY_STOCKS
        elif any(kw in text for kw in INFLATION_KEYWORDS):
            macro_type = "INFLATION_DATA"
            benefiting = BANK_STOCKS + RATE_SENS_STOCKS

        all_targets = list(set(tickers + benefiting))
        for sym in all_targets:
            if not sym or not sym.replace(".", "").isalpha() or len(sym) > 5:
                continue
            sym = sym.upper()
            direction = "bearish" if high_risk else "bullish"
            if sym not in new_flagged or new_flagged[sym]["score"] < news_score:
                new_flagged[sym] = {
                    "symbol":      sym,
                    "headline":    title[:120],
                    "score":       news_score,
                    "age_label":   age_label,
                    "minutes_old": round(minutes_old, 1),
                    "high_risk":   high_risk,
                    "direction":   direction,
                    "macro_type":  macro_type,
                    "pos_hits":    pos_hits[:3],
                    "neg_hits":    neg_hits[:3],
                    "flagged_at":  now.isoformat(),
                }

        if pos_hits or macro_type or (neg_hits and tickers):
            new_events.append({
                "title":       title,
                "tickers":     tickers,
                "minutes_old": round(minutes_old, 1),
                "age_label":   age_label,
                "macro_type":  macro_type,
                "benefiting":  benefiting,
                "score":       news_score,
                "high_risk":   high_risk,
            })

    _flagged_stocks = new_flagged
    _news_events    = new_events

    is_major_macro = any(
        e.get("macro_type") for e in new_events if e.get("minutes_old", 999) < 30
    )

    return {
        "events":         new_events,
        "flagged":        new_flagged,
        "is_major_macro": is_major_macro,
        "count":          len(new_events),
    }


def get_flagged_stocks() -> dict:
    return _flagged_stocks.copy()


def get_stock_news_score(symbol: str) -> dict:
    flagged = _flagged_stocks.get(symbol)
    if flagged:
        return flagged

    articles = get_ticker_news(symbol, limit=5)
    if not articles:
        return {"score": 0, "headline": "", "high_risk": False, "minutes_old": 999}

    best = None
    for article in articles:
        pub_str = article.get("published_utc", "")
        title   = article.get("title", "") or ""
        desc    = article.get("description", "") or ""
        text    = (title + " " + desc).lower()
        try:
            pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            minutes_old = (datetime.now(tz=pub_dt.tzinfo) - pub_dt).total_seconds() / 60
        except Exception:
            minutes_old = 999

        pos_hits = [kw for kw in POSITIVE_KEYWORDS if kw in text]
        neg_hits = [kw for kw in NEGATIVE_KEYWORDS if kw in text]
        if minutes_old < 30:
            age_s = 40
        elif minutes_old < 120:
            age_s = 25
        elif minutes_old < 480:
            age_s = 10
        else:
            age_s = 0
        score = age_s + (30 if pos_hits else 0) + (-40 if neg_hits else 0)
        if best is None or score > best["score"]:
            best = {
                "score":       score,
                "headline":    title[:120],
                "high_risk":   bool(neg_hits),
                "minutes_old": round(minutes_old, 1),
                "pos_hits":    pos_hits[:3],
                "neg_hits":    neg_hits[:3],
            }

    return best or {"score": 0, "headline": "", "high_risk": False, "minutes_old": 999}


def has_negative_news_7d(symbol: str) -> bool:
    articles = get_ticker_news(symbol, limit=10)
    for article in articles:
        pub_str = article.get("published_utc", "")
        try:
            pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            days_old = (datetime.now(tz=pub_dt.tzinfo) - pub_dt).total_seconds() / 86400
        except Exception:
            days_old = 999
        if days_old > 7:
            continue
        text = (article.get("title", "") + " " + (article.get("description") or "")).lower()
        if any(kw in text for kw in NEGATIVE_KEYWORDS):
            return True
    return False


def has_positive_news_7d(symbol: str) -> bool:
    articles = get_ticker_news(symbol, limit=10)
    for article in articles:
        pub_str = article.get("published_utc", "")
        try:
            pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            days_old = (datetime.now(tz=pub_dt.tzinfo) - pub_dt).total_seconds() / 86400
        except Exception:
            days_old = 999
        if days_old > 7:
            continue
        text = (article.get("title", "") + " " + (article.get("description") or "")).lower()
        if any(kw in text for kw in POSITIVE_KEYWORDS):
            return True
    return False

"""Market regime detection: BULL, BEAR, CHOPPY, NEWS_DRIVEN."""
import yfinance as yf
from indicators import add_indicators
from config import VIX_BULL_MAX, VIX_BEAR_MIN

_cached_regime = None
_regime_ts = None

SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLC", "XLB"]


def detect_regime(force_refresh: bool = False) -> dict:
    global _cached_regime, _regime_ts
    from datetime import datetime
    from config import TIMEZONE
    now = datetime.now(TIMEZONE)

    if not force_refresh and _cached_regime and _regime_ts:
        age = (now - _regime_ts).total_seconds()
        if age < 900:
            return _cached_regime

    result = _compute_regime()
    _cached_regime = result
    _regime_ts = now
    return result


def _compute_regime() -> dict:
    try:
        spy_df = yf.Ticker("SPY").history(period="3mo", interval="1d")
        vix_df = yf.Ticker("^VIX").history(period="1mo", interval="1d")
    except Exception:
        return _default_regime("CHOPPY", "Could not fetch market data")

    if spy_df is None or spy_df.empty or len(spy_df) < 50:
        return _default_regime("CHOPPY", "Insufficient SPY data")

    spy_df = add_indicators(spy_df)
    last   = spy_df.iloc[-1]

    spy_price = float(last["Close"])
    ema20     = float(last["EMA20"])
    ema50     = float(last["EMA50"])

    spy_vs_ema20 = "above" if spy_price > ema20 else "below"
    spy_vs_ema50 = "above" if spy_price > ema50 else "below"

    # VIX
    vix = 20.0
    vix_direction = "flat"
    if vix_df is not None and not vix_df.empty and len(vix_df) >= 2:
        vix = float(vix_df["Close"].iloc[-1])
        lookback = vix_df["Close"].iloc[-5] if len(vix_df) >= 5 else vix_df["Close"].iloc[0]
        pct = (vix - float(lookback)) / float(lookback) if float(lookback) else 0
        if pct > 0.05:
            vix_direction = "rising"
        elif pct < -0.05:
            vix_direction = "falling"

    # Market structure: higher highs / lower lows over last 20 days
    highs = spy_df["High"].tail(20).values
    lows  = spy_df["Low"].tail(20).values
    mid   = 10
    hh = highs[mid:].max() > highs[:mid].max()
    hl = lows[mid:].min()  > lows[:mid].min()
    lh = highs[mid:].max() < highs[:mid].max()
    ll = lows[mid:].min()  < lows[:mid].min()

    if hh and hl:
        market_structure = "higher_highs"
    elif lh and ll:
        market_structure = "lower_lows"
    else:
        market_structure = "mixed"

    # Sector money flow
    bullish_sectors = 0
    for etf in SECTOR_ETFS:
        try:
            df = yf.Ticker(etf).history(period="1mo", interval="1d")
            if df is None or df.empty or len(df) < 10:
                continue
            df  = add_indicators(df)
            etf_last = df.iloc[-1]
            if float(etf_last["Close"]) > float(etf_last["EMA20"]):
                bullish_sectors += 1
        except Exception:
            pass

    total_sectors = max(1, len(SECTOR_ETFS))
    if bullish_sectors >= int(total_sectors * 0.6):
        sector_flow = "positive"
    elif bullish_sectors <= int(total_sectors * 0.3):
        sector_flow = "negative"
    else:
        sector_flow = "mixed"

    # Score bull vs bear signals
    bull = sum([
        spy_vs_ema20 == "above",
        spy_vs_ema50 == "above",
        vix < VIX_BULL_MAX,
        market_structure == "higher_highs",
        sector_flow == "positive",
    ])
    bear = sum([
        spy_vs_ema20 == "below",
        spy_vs_ema50 == "below",
        vix > VIX_BEAR_MIN,
        market_structure == "lower_lows",
        sector_flow == "negative",
    ])

    if bull >= 4:
        regime = "BULL"
    elif bear >= 4:
        regime = "BEAR"
    else:
        regime = "CHOPPY"

    return {
        "regime":           regime,
        "vix":              round(vix, 1),
        "vix_direction":    vix_direction,
        "spy_price":        round(spy_price, 2),
        "ema20":            round(ema20, 2),
        "ema50":            round(ema50, 2),
        "spy_vs_ema20":     spy_vs_ema20,
        "spy_vs_ema50":     spy_vs_ema50,
        "market_structure": market_structure,
        "sector_flow":      sector_flow,
        "bullish_sectors":  bullish_sectors,
        "bull_signals":     bull,
        "bear_signals":     bear,
    }


def _default_regime(regime: str, reason: str) -> dict:
    return {
        "regime":           regime,
        "vix":              20.0,
        "vix_direction":    "flat",
        "spy_price":        0.0,
        "ema20":            0.0,
        "ema50":            0.0,
        "spy_vs_ema20":     "unknown",
        "spy_vs_ema50":     "unknown",
        "market_structure": "mixed",
        "sector_flow":      "mixed",
        "bullish_sectors":  0,
        "bull_signals":     0,
        "bear_signals":     0,
        "note":             reason,
    }


def mark_news_driven(regime: dict) -> dict:
    r = regime.copy()
    r["regime"] = "NEWS_DRIVEN"
    return r


def regime_summary(r: dict) -> str:
    regime = r.get("regime", "UNKNOWN")
    vix    = r.get("vix", 0)
    spy    = r.get("spy_price", 0)
    vs20   = r.get("spy_vs_ema20", "?")
    vs50   = r.get("spy_vs_ema50", "?")
    flow   = r.get("sector_flow", "?")
    struct = r.get("market_structure", "?")
    vdir   = r.get("vix_direction", "?")
    return (
        f"Regime: {regime} | SPY ${spy} ({vs20} EMA20, {vs50} EMA50) | "
        f"VIX {vix} {vdir} | Sectors {flow} | Structure {struct}"
    )

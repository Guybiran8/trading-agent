"""Short scanner — only active when market regime is BEAR.

Finds failed rallies into resistance with exhausted momentum.
"""
import yfinance as yf
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from config import TIMEZONE, MAX_ALERTS_PER_SCAN, MIN_GAIN_PCT
from indicators import add_indicators
from news_scanner import has_negative_news_7d
from data_fetcher import run_and_release

_short_alerted_today: set = set()
_short_alerted_date: date = None
_short_count_today: int = 0

MIN_SHORT_SCORE = 70


def reset_if_new_day():
    global _short_alerted_today, _short_alerted_date, _short_count_today
    today = date.today()
    if _short_alerted_date != today:
        _short_alerted_today = set()
        _short_alerted_date  = today
        _short_count_today   = 0


def already_alerted(symbol: str) -> bool:
    reset_if_new_day()
    return symbol in _short_alerted_today


def mark_alerted(symbol: str):
    global _short_count_today
    _short_alerted_today.add(symbol)
    _short_count_today += 1


def daily_limit_reached() -> bool:
    reset_if_new_day()
    return _short_count_today >= MAX_ALERTS_PER_SCAN


def _get_sector_weakness(symbol: str) -> int:
    SECTOR_MAP = {
        "Technology": "XLK", "Financial Services": "XLF",
        "Healthcare": "XLV", "Consumer Cyclical": "XLY",
        "Energy": "XLE", "Industrials": "XLI",
    }
    try:
        info   = yf.Ticker(symbol).info
        sector = info.get("sector", "")
        etf    = SECTOR_MAP.get(sector)
        if not etf:
            return 0
        df = yf.Ticker(etf).history(period="3mo", interval="1d")
        if df is None or df.empty or len(df) < 50:
            return 0
        df   = add_indicators(df)
        last = df.iloc[-1]
        if float(last["EMA20"]) < float(last["EMA50"]) < float(last["EMA150"]):
            return 15
        return 0
    except Exception:
        return 0


def analyze_short_stock(symbol: str) -> dict | None:
    try:
        df = yf.Ticker(symbol).history(period="9mo", interval="1d", auto_adjust=True)
        if df is None or df.empty or len(df) < 80:
            return None

        df.columns = [c.strip() for c in df.columns]
        req = ["Open", "High", "Low", "Close", "Volume"]
        if not all(c in df.columns for c in req):
            return None
        df = df[req].copy()
        df.dropna(inplace=True)
        if len(df) < 80:
            return None

        df   = add_indicators(df)
        last = df.iloc[-1]

        close  = float(last["Close"])
        ema20  = float(last["EMA20"])
        ema50  = float(last["EMA50"])
        ema150 = float(last["EMA150"])

        # REQUIRED: confirmed downtrend EMA20 < EMA50 < EMA150
        if not (ema20 < ema50 < ema150):
            return None

        rsi = float(last["RSI"])

        # Look for RSI that bounced to 60-65 and is now rolling over
        rsi_recent = df["RSI"].tail(10).values
        rsi_peak   = max(rsi_recent)
        rsi_rolled = rsi_peak >= 58 and rsi < rsi_peak - 5

        # Stock bouncing into resistance — failed rally
        res = float(last["Resistance"])
        dist_to_res = (res - close) / res
        near_resistance = 0 <= dist_to_res < 0.05

        # Volume higher on down days than up days (last 15 days)
        up_vol   = []
        down_vol = []
        for i in range(-15, 0):
            try:
                if float(df["Close"].iloc[i]) < float(df["Open"].iloc[i]):
                    down_vol.append(float(df["Volume"].iloc[i]))
                else:
                    up_vol.append(float(df["Volume"].iloc[i]))
            except Exception:
                pass
        vol_bearish = (
            bool(up_vol) and bool(down_vol) and
            sum(down_vol) / len(down_vol) > sum(up_vol) / len(up_vol)
        )

        # Sector weak and getting weaker
        sector_weak = _get_sector_weakness(symbol)

        # Negative news or recent earnings miss
        has_neg_news = has_negative_news_7d(symbol)

        # At least 10% downside to EMA50 (which is below close since downtrend)
        potential_short = (close - ema50) / close * 100 if close > ema50 else 0

        # Need support level for stop
        support   = float(last["Support"])
        atr       = float(last["ATR"]) if "ATR" in df.columns else close * 0.02
        stop_loss = round(res * 1.02, 2)
        target    = round(ema50, 2) if ema50 < close else round(close * 0.9, 2)

        if target >= close:
            target = round(close * 0.9, 2)

        risk   = abs(stop_loss - close)
        reward = abs(close - target)
        rr     = round(reward / risk, 2) if risk > 0 else 0

        if rr < 1.5:
            return None

        if reward / close * 100 < MIN_GAIN_PCT:
            return None

        score = 0
        components = {"downtrend_base": 30}
        score += components["downtrend_base"]
        components["rsi_rollover"] = 20 if rsi_rolled else 0
        score += components["rsi_rollover"]
        components["near_resistance"] = 15 if near_resistance else 0
        score += components["near_resistance"]
        components["volume_bearish"] = 15 if vol_bearish else 0
        score += components["volume_bearish"]
        components["sector_weak"] = sector_weak if sector_weak else 0
        score += components["sector_weak"]
        components["negative_news"] = 10 if has_neg_news else 0
        score += components["negative_news"]

        if score < MIN_SHORT_SCORE:
            return None

        reasons = [
            f"Downtrend confirmed EMA20({ema20:.2f}) < EMA50({ema50:.2f}) < EMA150({ema150:.2f})",
        ]
        if rsi_rolled:
            reasons.append(f"RSI peaked at {rsi_peak:.0f}, now rolling over to {rsi:.1f}")
        if near_resistance:
            reasons.append(f"Bouncing into resistance ${res:.2f} — failed rally setup")
        if vol_bearish:
            reasons.append("Volume higher on down days — distribution")
        if has_neg_news:
            reasons.append("Negative news overhang")

        try:
            vol_today = float(yf.Ticker(symbol).fast_info.last_volume or 0)
            if vol_today == 0:
                return None
        except Exception:
            return None

        return {
            "symbol":       symbol,
            "signal":       "SHORT",
            "scanner":      "SHORT",
            "trade_type":   "SWING TRADE",
            "score":        score,
            "score_components": components,
            "conviction":   "HIGH" if score >= 90 else "SPECULATIVE",
            "price":        round(close, 2),
            "stop_loss":    stop_loss,
            "price_target": target,
            "rr_ratio":     rr,
            "rsi":          round(rsi, 1),
            "atr":          round(atr, 2),
            "reasons":      reasons,
            "flags":        ["BEAR REGIME SHORT"],
        }

    except Exception:
        return None


def run_short_scanner(symbols: list, alerted_today: set) -> list:
    """Run short scanner (BEAR regime only). Max 3 shorts per day."""
    reset_if_new_day()

    if daily_limit_reached():
        print("[Short] Daily limit reached. Skipping.")
        return []

    now = datetime.now(TIMEZONE)
    print(f"\n=== SHORT SCANNER {now.strftime('%I:%M %p ET')} ===")

    eligible = [s for s in symbols if s not in alerted_today]
    print(f"  Scanning {len(eligible)} symbols for short setups...")

    results = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(run_and_release, analyze_short_stock, s): s for s in eligible}
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                results.append(r)

    results.sort(key=lambda x: x["score"], reverse=True)
    remaining = MAX_ALERTS_PER_SCAN - _short_count_today
    top = results[:remaining]

    if not top:
        print("  No short setups found")
        return []

    print(f"  Found {len(results)} short setups, returning top {len(top)}")
    return top

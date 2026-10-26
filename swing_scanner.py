"""Swing trade scanner — runs every 30 minutes during market hours.

Finds stocks coiling for big moves. Uses accumulation detection and strict
trend filters.
"""
import yfinance as yf
import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from config import TIMEZONE, MIN_SWING_SCORE, MAX_ALERTS_PER_SCAN, MIN_GAIN_PCT
from indicators import add_indicators
from news_scanner import has_negative_news_7d, has_positive_news_7d
from data_fetcher import get_earnings_date, run_and_release

_swing_alerted_today: set = set()
_swing_alerted_date: date = None
_swing_count_today: int = 0


def reset_if_new_day():
    global _swing_alerted_today, _swing_alerted_date, _swing_count_today
    today = date.today()
    if _swing_alerted_date != today:
        _swing_alerted_today = set()
        _swing_alerted_date  = today
        _swing_count_today   = 0


def already_alerted(symbol: str) -> bool:
    reset_if_new_day()
    return symbol in _swing_alerted_today


def mark_alerted(symbol: str):
    """Called by the scheduler for each swing alert actually emitted (3/day cap)."""
    global _swing_count_today
    reset_if_new_day()
    _swing_alerted_today.add(symbol)
    _swing_count_today += 1


def daily_limit_reached() -> bool:
    reset_if_new_day()
    return _swing_count_today >= MAX_ALERTS_PER_SCAN


def _trend_filter(df: pd.DataFrame) -> bool:
    """All must pass: Price > EMA20, EMA20 > EMA50, EMA50 > EMA150."""
    try:
        last = df.iloc[-1]
        return (
            float(last["Close"])  > float(last["EMA20"]) and
            float(last["EMA20"])  > float(last["EMA50"]) and
            float(last["EMA50"])  > float(last["EMA150"])
        )
    except Exception:
        return False


def _accumulation_score(df: pd.DataFrame) -> int:
    """Score accumulation signals. Max 35 points."""
    score = 0
    try:
        lows   = df["Low"].values
        highs  = df["High"].values
        closes = df["Close"].values
        opens  = df["Open"].values
        vol    = df["Volume"].values

        # Higher lows for 10 consecutive days
        last10_lows = lows[-10:]
        if len(last10_lows) >= 10:
            higher_lows = all(last10_lows[i] > last10_lows[i-1] for i in range(1, 10))
            if higher_lows:
                score += 10

        # Volume on up days > volume on down days (last 15 days)
        up_vol   = []
        down_vol = []
        for i in range(-15, 0):
            try:
                if closes[i] > opens[i]:
                    up_vol.append(vol[i])
                else:
                    down_vol.append(vol[i])
            except IndexError:
                pass
        if up_vol and down_vol:
            avg_up   = sum(up_vol) / len(up_vol)
            avg_down = sum(down_vol) / len(down_vol)
            if avg_up > avg_down:
                score += 10

        # Bollinger Bands tightening for 5+ days
        if "BB_width" in df.columns:
            bw = df["BB_width"].dropna().values
            if len(bw) >= 6:
                tightening = all(bw[-i-1] < bw[-i-2] for i in range(5))
                if tightening:
                    score += 8

        # Tight range: less than 8% range for last 10 days
        if len(df) >= 10:
            last10 = df.tail(10)
            price_range = (last10["High"].max() - last10["Low"].min()) / last10["Low"].min() * 100
            if price_range < 8.0:
                score += 7

    except Exception:
        pass

    return min(35, score)


def _check_institutional(df: pd.DataFrame) -> bool:
    """Detect institutional block trades in last 5 days (vol > 2x average)."""
    try:
        vol     = df["Volume"]
        avg_vol = vol.rolling(20).mean()
        last5   = df.tail(5)
        last5_avg = avg_vol.tail(5)
        for i in range(len(last5)):
            try:
                if float(last5["Volume"].iloc[i]) > float(last5_avg.iloc[i]) * 2:
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _entry_quality_score(df: pd.DataFrame) -> tuple:
    """
    Score entry quality. Returns (score, breakout_level, room_pct).
    Max 25 points for pullback + 15 for breakout proximity.
    """
    total   = 0
    bo_level = 0.0
    room_pct = 0.0

    try:
        last  = df.iloc[-1]
        close = float(last["Close"])
        ema20 = float(last["EMA20"])

        # Pullback to within 3% of EMA20 from above
        dist_ema20 = (close - ema20) / ema20
        if 0 < dist_ema20 < 0.03:
            total += 25
        elif 0 < dist_ema20 < 0.06:
            total += 10

        # Volume drying up during pullback (last 5 days below 20-day avg)
        vol_avg = float(last["Vol_MA20"]) if "Vol_MA20" in df.columns else float(df["Volume"].mean())
        recent_vol = float(df["Volume"].tail(5).mean())
        if recent_vol < vol_avg * 0.8:
            total += 5

        # Find nearest resistance above (breakout level within 5%)
        res = float(last["Resistance"]) if "Resistance" in df.columns else close * 1.1
        dist_to_res = (res - close) / close * 100
        if dist_to_res <= 5.0 and dist_to_res > 0:
            total += 15
            bo_level = round(res, 2)

            # Room above breakout: need 20%+ with no major resistance
            hist_highs = df["High"].values
            upper_res = [h for h in hist_highs if h > res * 1.01]
            if upper_res:
                next_res = min(upper_res)
                room_pct = (next_res - res) / res * 100
            else:
                room_pct = 30.0

    except Exception:
        pass

    return total, round(bo_level, 2), round(room_pct, 1)


def _earnings_check(symbol: str) -> tuple:
    """Returns (skip, flag_warning, flag_positive)."""
    try:
        today       = date.today()
        earn_date   = get_earnings_date(symbol)
        if earn_date is None:
            return False, False, False
        days_away = (earn_date - today).days
        if days_away < 0:
            days_since = abs(days_away)
            if days_since <= 7:
                return False, False, True
            return False, False, False
        if days_away <= 3:
            return True, False, False
        if days_away <= 7:
            return False, True, False
    except Exception:
        pass
    return False, False, False


def _get_sector_strength(symbol: str) -> int:
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
        if float(last["EMA20"]) > float(last["EMA50"]) > float(last["EMA150"]):
            return 15
        if float(last["EMA20"]) > float(last["EMA50"]):
            return 8
        return 0
    except Exception:
        return 0


def analyze_swing_stock(symbol: str) -> dict | None:
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

        df = add_indicators(df)
        if df is None or len(df) < 10:
            return None

        # STEP 0: Daily range filter — reject dead stocks, SPACs, preferreds near NAV
        if len(df) >= 20:
            last20 = df.tail(20)
            closes = last20["Close"].values.astype(float)
            highs  = last20["High"].values.astype(float)
            lows   = last20["Low"].values.astype(float)
            avg_range_pct = float(((highs - lows) / closes).mean()) * 100
            if avg_range_pct < 3.0:
                return None

        # STEP 1: Trend filter — all must pass
        if not _trend_filter(df):
            return None

        # STEP 2: Earnings check
        skip_earn, warn_earn, pos_earn = _earnings_check(symbol)
        if skip_earn:
            return None

        # STEP 3: Negative news check
        if has_negative_news_7d(symbol):
            return None

        # STEP 4: Accumulation score
        accum = _accumulation_score(df)

        # STEP 5: Entry quality
        entry_q, bo_level, room_pct = _entry_quality_score(df)

        # STEP 6: Room above breakout
        if room_pct < 20:
            return None

        # STEP 7: Sector strength
        sector_pts = _get_sector_strength(symbol)

        # STEP 8: Institutional flow
        inst_pts = 15 if _check_institutional(df) else 0

        # STEP 9: News catalyst
        has_pos_news = has_positive_news_7d(symbol)
        news_pts     = 20 if has_pos_news else 0

        # Total score
        trend_pts = 25
        score = trend_pts + accum + entry_q + sector_pts + inst_pts + news_pts
        components = {
            "trend": trend_pts, "accumulation": accum, "entry_quality": entry_q,
            "sector": sector_pts, "institutional": inst_pts, "news": news_pts,
        }

        if score < MIN_SWING_SCORE:
            return None

        last  = df.iloc[-1]
        close = float(last["Close"])
        atr   = float(last["ATR"]) if "ATR" in df.columns else close * 0.02

        stop_loss  = round(close - atr * 1.5, 2)
        if not bo_level or bo_level < close:
            bo_level = round(close * 1.04, 2)
        target     = round(bo_level * (1 + room_pct / 100 * 0.8), 2)
        risk       = abs(close - stop_loss)
        reward     = abs(target - close)
        rr         = round(reward / risk, 2) if risk > 0 else 0

        if reward / close * 100 < MIN_GAIN_PCT:
            return None

        conviction = "HIGH" if score >= 110 else "SPECULATIVE"

        reasons = [
            f"Trend aligned EMA20>EMA50>EMA150, score {score}",
            f"Accumulation {accum}/35 — {'strong' if accum >= 25 else 'moderate'}",
            f"Breakout level ${bo_level:.2f}, {room_pct:.0f}% room above",
        ]
        if pos_earn:
            reasons.append("Earnings just passed — held strong, positive signal")
        if warn_earn:
            reasons.append("EARNINGS in 3-7 days — recommend smaller size")
        if has_pos_news:
            reasons.append("Positive news in last 7 days")
        if inst_pts:
            reasons.append("Institutional block trades detected")

        flags = []
        if warn_earn:
            flags.append("EARNINGS 3-7 DAYS — size small")
        if inst_pts:
            flags.append("INSTITUTIONAL ACCUMULATION")
        if has_pos_news:
            flags.append("POSITIVE NEWS CATALYST")

        return {
            "symbol":        symbol,
            "signal":        "STRONG BUY",
            "scanner":       "SWING",
            "trade_type":    "SWING TRADE",
            "score":         score,
            "score_components": components,
            "conviction":    conviction,
            "price":         round(close, 2),
            "stop_loss":     stop_loss,
            "price_target":  target,
            "rr_ratio":      rr,
            "rsi":           round(float(last["RSI"]), 1) if "RSI" in df.columns else 0,
            "atr":           round(atr, 2),
            "bo_level":      bo_level,
            "room_pct":      room_pct,
            "accum_score":   accum,
            "has_pos_news":  has_pos_news,
            "warn_earnings": warn_earn,
            "reasons":       reasons,
            "flags":         flags,
        }

    except Exception:
        return None


def run_swing_scanner(symbols: list, alerted_today: set) -> list:
    """Run the swing scanner. Returns top setups (max 3 per day)."""
    reset_if_new_day()

    if daily_limit_reached():
        print("[Swing] Daily limit reached (3 alerts). Skipping.")
        return []

    now = datetime.now(TIMEZONE)
    print(f"\n=== SWING TRADE SCANNER {now.strftime('%I:%M %p ET')} ===")

    # Pre-filter: only stocks with some volume today and not already alerted
    eligible = [s for s in symbols if s not in alerted_today]
    print(f"  Scanning {len(eligible)} eligible symbols...")

    results = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(run_and_release, analyze_swing_stock, s): s for s in eligible}
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                results.append(r)

    results.sort(key=lambda x: x["score"], reverse=True)

    remaining_slots = MAX_ALERTS_PER_SCAN - _swing_count_today
    top = results[:remaining_slots]

    if not top:
        print(f"  No swing setups found above score {MIN_SWING_SCORE}")
        return []

    print(f"  Found {len(results)} setups, returning top {len(top)}")
    return top

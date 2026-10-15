"""Day trade scanner — runs every 60 seconds 9:30 AM to 4:00 PM ET.

Mode 1: Breakout detection (real breakouts above tested resistance)
Mode 2: News-driven detection (volume confirms news)
"""
import yfinance as yf
import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from config import TIMEZONE, MIN_DAY_SCORE, MAX_ALERTS_PER_SCAN, MIN_GAIN_PCT
from indicators import add_indicators
from news_scanner import get_stock_news_score, get_flagged_stocks
from data_fetcher import get_all_symbols, run_and_release

_alerted_today: set = set()
_alerted_date: date = None


def _reset_if_new_day():
    global _alerted_today, _alerted_date
    today = date.today()
    if _alerted_date != today:
        _alerted_today = set()
        _alerted_date = today


def already_alerted(symbol: str) -> bool:
    _reset_if_new_day()
    return symbol in _alerted_today


def mark_alerted(symbol: str):
    _alerted_today.add(symbol)


def _get_spy_intraday_pct() -> float:
    try:
        df = yf.Ticker("SPY").history(period="1d", interval="1m")
        if df.empty:
            return 0.0
        open_p = float(df["Open"].iloc[0])
        curr   = float(df["Close"].iloc[-1])
        return round((curr - open_p) / open_p * 100, 2) if open_p else 0.0
    except Exception:
        return 0.0


def _get_sector_moving(symbol: str) -> bool:
    SECTOR_MAP = {
        "Technology": "XLK", "Financial Services": "XLF", "Healthcare": "XLV",
        "Consumer Cyclical": "XLY", "Energy": "XLE", "Industrials": "XLI",
    }
    try:
        info   = yf.Ticker(symbol).info
        sector = info.get("sector", "")
        etf    = SECTOR_MAP.get(sector)
        if not etf:
            return False
        df = yf.Ticker(etf).history(period="1d", interval="1m")
        if df.empty:
            return False
        open_p = float(df["Open"].iloc[0])
        curr   = float(df["Close"].iloc[-1])
        return abs(curr - open_p) / open_p > 0.005 if open_p else False
    except Exception:
        return False


def _find_resistance_levels(df_daily: pd.DataFrame, window: int = 30) -> list:
    """Find price levels tested at least twice as resistance in the last `window` days."""
    if len(df_daily) < window:
        window = len(df_daily)
    highs = df_daily["High"].tail(window).values.tolist()

    clusters = []
    for level in highs:
        if not level or level != level:
            continue
        count = sum(1 for h in highs if abs(h - level) / level < 0.005)
        if count >= 2:
            clusters.append(level)

    if not clusters:
        return []

    clusters.sort()
    merged = [clusters[0]]
    for lv in clusters[1:]:
        if abs(lv - merged[-1]) / merged[-1] < 0.008:
            merged[-1] = (merged[-1] + lv) / 2
        else:
            merged.append(lv)
    return merged


def _time_of_day_vol_avg(df_1m: pd.DataFrame, current_bar_idx: int) -> float:
    """Approximate time-of-day volume baseline using rolling session average."""
    if df_1m.empty or len(df_1m) < 3:
        return 0.0
    recent_vol = df_1m["Volume"].tail(10)
    return float(recent_vol.mean()) if len(recent_vol) else 0.0


def _analyze_breakout(symbol: str, spy_pct: float) -> dict | None:
    """Check for a real breakout above a resistance level that was tested 2+ times."""
    try:
        # Get daily data for resistance levels
        df_daily = yf.Ticker(symbol).history(period="3mo", interval="1d")
        if df_daily is None or df_daily.empty or len(df_daily) < 20:
            return None

        # Get intraday 1-min data
        df_1m = yf.Ticker(symbol).history(period="2d", interval="1m")
        if df_1m is None or df_1m.empty or len(df_1m) < 10:
            return None

        # Filter to today only
        today = datetime.now(TIMEZONE).date()
        if hasattr(df_1m.index, "tz_convert"):
            idx_et = df_1m.index.tz_convert(TIMEZONE)
        else:
            idx_et = df_1m.index
        today_mask = [i.date() == today for i in idx_et]
        df_today = df_1m[today_mask]
        if df_today.empty or len(df_today) < 5:
            return None

        current_price = float(df_today["Close"].iloc[-1])
        open_price    = float(df_today["Open"].iloc[0])
        intraday_pct  = round((current_price - open_price) / open_price * 100, 2) if open_price else 0

        # Must have some move
        if abs(intraday_pct) < 2.0:
            return None

        # Find resistance levels from daily chart
        res_levels = _find_resistance_levels(df_daily, window=30)
        if not res_levels:
            return None

        # Check if current price just broke above any resistance level
        broken_level = None
        for level in res_levels:
            if current_price > level * 1.001:
                # Find when it first crossed above this level today
                crosses = []
                for i, row in df_today.iterrows():
                    if float(row["Close"]) > level:
                        crosses.append(True)
                    else:
                        crosses.append(False)

                if not any(crosses):
                    continue

                first_cross = next((i for i, x in enumerate(crosses) if x), None)
                if first_cross is None:
                    continue

                bars_since_break = len(crosses) - first_cross
                if bars_since_break > 45:
                    continue

                # Must hold for 2+ consecutive bars above level
                post_break = [float(df_today["Close"].iloc[i]) for i in range(first_cross, len(df_today))]
                if len(post_break) < 2:
                    continue
                held_2_bars = all(p > level for p in post_break)

                # Volume at breakout vs time-of-day average
                break_vol   = float(df_today["Volume"].iloc[first_cross]) if first_cross < len(df_today) else 0
                tod_avg     = _time_of_day_vol_avg(df_today, first_cross)
                vol_ratio   = break_vol / tod_avg if tod_avg > 0 else 0

                # Reject if giving back more than 15% of the move
                move_size   = current_price - level
                give_back   = (level + move_size) - current_price
                give_back_pct = give_back / move_size if move_size > 0 else 0
                if give_back_pct > 0.15:
                    continue

                broken_level = {
                    "level":          round(level, 2),
                    "bars_since":     bars_since_break,
                    "vol_ratio":      round(vol_ratio, 1),
                    "give_back_pct":  round(give_back_pct * 100, 1),
                    "held_2_bars":    held_2_bars,
                    "intraday_pct":   intraday_pct,
                }
                break

        if not broken_level:
            return None

        level = broken_level["level"]
        bars  = broken_level["bars_since"]
        vol_r = broken_level["vol_ratio"]

        # Find next resistance above breakout (room to run)
        upper_levels = [r for r in res_levels if r > level * 1.01]
        if upper_levels:
            next_res = min(upper_levels)
            room_pct = (next_res - current_price) / current_price * 100
        else:
            room_pct = 25.0

        # News score
        news = get_stock_news_score(symbol)
        news_score_val = news.get("score", 0)
        has_news = news_score_val > 20
        no_catalyst_big_move = abs(intraday_pct) > 15 and not has_news

        # Score the setup
        score = 0
        components = {}

        # Volume at breakout
        if vol_r >= 10:
            components["volume"] = 60
        elif vol_r >= 5:
            components["volume"] = 40
        elif vol_r >= 3:
            components["volume"] = 25
        else:
            components["volume"] = 0
        score += components["volume"]

        # Freshness
        if bars <= 15:
            components["freshness"] = 35
        elif bars <= 30:
            components["freshness"] = 20
        elif bars <= 45:
            components["freshness"] = 5
        else:
            components["freshness"] = -50
        score += components["freshness"]

        # Holding above breakout
        if broken_level["give_back_pct"] < 5:
            components["holding"] = 25
        elif broken_level["give_back_pct"] < 15:
            components["holding"] = 0
        else:
            components["holding"] = -30
        score += components["holding"]

        # Room to next resistance
        if room_pct >= 20:
            components["room"] = 35
        elif room_pct >= 10:
            components["room"] = 20
        else:
            components["room"] = -20
        score += components["room"]

        # Relative strength vs SPY
        stock_pct = intraday_pct
        if stock_pct > spy_pct + 1:
            components["relative_strength"] = 20
        elif stock_pct < spy_pct - 1:
            components["relative_strength"] = -20
        else:
            components["relative_strength"] = 0
        score += components["relative_strength"]

        # Sector moving
        components["sector"] = 15 if _get_sector_moving(symbol) else 0
        score += components["sector"]

        # News catalyst
        if has_news:
            components["news"] = 30
        elif no_catalyst_big_move:
            components["news"] = -15
        else:
            components["news"] = 0
        score += components["news"]

        if score < MIN_DAY_SCORE:
            return None

        potential_gain = room_pct
        if potential_gain < MIN_GAIN_PCT:
            return None

        # Enforce minimum 1.5% stop distance; skip if stop too close
        raw_stop = round(level * 0.97, 2)
        stop_dist_pct = abs(current_price - raw_stop) / current_price * 100 if current_price else 0
        if stop_dist_pct < 1.5:
            raw_stop = round(current_price * 0.985, 2)
        if abs(current_price - raw_stop) / current_price * 100 < 0.5:
            return None

        stop_loss  = raw_stop
        target     = round(current_price * (1 + room_pct / 100 * 0.7), 2)
        risk       = abs(current_price - stop_loss)
        reward     = abs(target - current_price)
        rr         = round(reward / risk, 2) if risk > 0.001 else 0

        if not has_news:
            news_line = "NO CATALYST — trade with caution"
        else:
            news_line = f"NEWS: {news.get('headline','')[:60]}"

        reasons = [
            f"Breakout above ${level:.2f} (tested 2+ times), vol {vol_r:.1f}x",
            f"Move {intraday_pct:+.1f}% intraday, {bars} bars since break",
            f"{room_pct:.0f}% room to next resistance",
            news_line,
        ]

        return {
            "symbol":       symbol,
            "signal":       "STRONG BUY",
            "scanner":      "DAY_BREAKOUT",
            "trade_type":   "DAY TRADE",
            "score":        score,
            "score_components": components,
            "conviction":   "HIGH" if score >= 100 else "SPECULATIVE",
            "price":        round(current_price, 2),
            "stop_loss":    stop_loss,
            "price_target": target,
            "rr_ratio":     rr,
            "intraday_pct": intraday_pct,
            "vol_ratio":    vol_r,
            "room_pct":     round(room_pct, 1),
            "news_score":   news_score_val,
            "news_headline": news.get("headline", ""),
            "has_news":     has_news,
            "news_headline": news.get("headline", "") or "NO CATALYST — trade with caution",
            "reasons":      reasons,
            "flags":        [],
        }

    except Exception as e:
        return None


def _analyze_news_driven(symbol: str, news_info: dict, spy_pct: float) -> dict | None:
    """Mode 2: News-driven detection — confirm volume and price response."""
    try:
        if news_info.get("high_risk"):
            return None
        if news_info.get("direction") == "bearish":
            return None

        news_score_val = news_info.get("score", 0)
        if news_score_val < 40:
            return None

        df_1m = yf.Ticker(symbol).history(period="2d", interval="1m")
        if df_1m is None or df_1m.empty or len(df_1m) < 10:
            return None

        today = datetime.now(TIMEZONE).date()
        if hasattr(df_1m.index, "tz_convert"):
            idx_et = df_1m.index.tz_convert(TIMEZONE)
        else:
            idx_et = df_1m.index
        today_mask = [i.date() == today for i in idx_et]
        df_today = df_1m[today_mask]
        if df_today.empty or len(df_today) < 5:
            return None

        current_price = float(df_today["Close"].iloc[-1])
        open_price    = float(df_today["Open"].iloc[0])
        intraday_pct  = round((current_price - open_price) / open_price * 100, 2) if open_price else 0

        # Volume must be surging
        recent_vol = float(df_today["Volume"].tail(3).mean())
        avg_vol    = float(df_today["Volume"].mean())
        vol_ratio  = recent_vol / avg_vol if avg_vol > 0 else 0

        # Price must be responding correctly to news direction
        if intraday_pct < 2.0:
            return None

        # Must be early (news under 30 min = best)
        minutes_old = news_info.get("minutes_old", 999)

        score = 0
        components = {"news_base": 30}
        score += components["news_base"]

        if vol_ratio >= 5:
            components["volume"] = 30
        elif vol_ratio >= 3:
            components["volume"] = 20
        elif vol_ratio >= 2:
            components["volume"] = 10
        else:
            components["volume"] = 0
        score += components["volume"]

        if minutes_old < 15:
            components["freshness"] = 35
        elif minutes_old < 30:
            components["freshness"] = 25
        elif minutes_old < 60:
            components["freshness"] = 10
        else:
            components["freshness"] = 0
        score += components["freshness"]

        components["intraday_move"] = min(20, int(intraday_pct * 2))
        score += components["intraday_move"]

        components["relative_strength"] = 20 if intraday_pct > spy_pct + 1 else 0
        score += components["relative_strength"]

        if score < MIN_DAY_SCORE:
            return None

        if intraday_pct < MIN_GAIN_PCT:
            return None

        stop_loss  = round(current_price * 0.985, 2)
        target     = round(current_price * (1 + intraday_pct / 100 * 0.5 + 0.03), 2)
        risk       = abs(current_price - stop_loss)
        reward     = abs(target - current_price)
        rr         = round(reward / risk, 2) if risk > 0.001 else 0

        headline = news_info.get("headline", "") or "NO CATALYST — trade with caution"

        reasons = [
            f"NEWS DRIVEN: {headline[:70]}",
            f"Vol {vol_ratio:.1f}x surge confirming news ({minutes_old:.0f} min old)",
            f"Move {intraday_pct:+.1f}% responding correctly",
        ]

        return {
            "symbol":       symbol,
            "signal":       "STRONG BUY",
            "scanner":      "DAY_NEWS",
            "trade_type":   "DAY TRADE",
            "score":        score,
            "score_components": components,
            "conviction":   "HIGH" if score >= 100 else "SPECULATIVE",
            "price":        round(current_price, 2),
            "stop_loss":    stop_loss,
            "price_target": target,
            "rr_ratio":     rr,
            "intraday_pct": intraday_pct,
            "vol_ratio":    round(vol_ratio, 1),
            "room_pct":     round(intraday_pct * 0.5 + 3, 1),
            "news_score":   news_score_val,
            "news_headline": news_info.get("headline", ""),
            "has_news":     True,
            "reasons":      reasons,
            "flags":        ["NEWS DRIVEN"],
        }

    except Exception:
        return None


def _double_check(symbol: str, result: dict, spy_pct: float) -> bool:
    """Re-verify the setup before sending the alert."""
    try:
        df = yf.Ticker(symbol).history(period="1d", interval="1m")
        if df is None or df.empty:
            return False

        current = float(df["Close"].iloc[-1])
        entry   = result["price"]
        stop    = result["stop_loss"]

        if current < stop:
            return False

        recent_vol = float(df["Volume"].tail(3).mean())
        avg_vol    = float(df["Volume"].mean())
        vol_ok = recent_vol >= avg_vol * 0.5

        intraday_pct = result.get("intraday_pct", 0)
        rs_ok = (intraday_pct - spy_pct) >= -3

        target = result["price_target"]
        room_pct = (target - current) / current * 100 if current else 0
        room_ok = room_pct >= MIN_GAIN_PCT

        return vol_ok and rs_ok and room_ok

    except Exception:
        return False


def _fast_movers(symbols: list) -> list:
    """Batch download to find intraday movers >2% quickly."""
    movers = []
    try:
        chunk = symbols[:500]
        data = yf.download(
            chunk, period="2d", interval="1d",
            progress=False, auto_adjust=True, group_by="ticker",
        )
        for sym in chunk:
            try:
                if isinstance(data.columns, pd.MultiIndex):
                    close_s  = data[sym]["Close"].dropna()
                    vol_s    = data[sym]["Volume"].dropna()
                else:
                    close_s  = data["Close"].dropna()
                    vol_s    = data["Volume"].dropna()

                if len(close_s) < 2:
                    continue
                prev_c  = float(close_s.iloc[-2])
                curr_c  = float(close_s.iloc[-1])
                curr_v  = float(vol_s.iloc[-1])
                change  = (curr_c - prev_c) / prev_c * 100 if prev_c else 0

                if abs(change) >= 2.0 and curr_v > 0:
                    movers.append({"symbol": sym, "change_pct": change, "volume": curr_v})
            except Exception:
                continue
    except Exception:
        pass
    movers.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
    return movers[:100]


def run_day_scanner(symbols: list, alerted_today: set) -> list:
    """Run both breakout and news-driven day trade scanners. Return top alerts."""
    now = datetime.now(TIMEZONE)
    print(f"\n=== DAY TRADE SCANNER {now.strftime('%I:%M %p ET')} ===")

    spy_pct  = _get_spy_intraday_pct()
    flagged  = get_flagged_stocks()

    candidates = []

    # Fast mover pre-filter
    movers = _fast_movers(symbols)
    mover_syms = {m["symbol"] for m in movers if m["change_pct"] > 0}

    # All symbols to scan = movers + news-flagged
    all_candidates = list(mover_syms | set(flagged.keys()))
    all_candidates = [s for s in all_candidates if s not in alerted_today]

    print(f"  {len(movers)} movers, {len(flagged)} news-flagged, {len(all_candidates)} unique candidates")

    results = []

    def analyze(sym):
        # Mode 2 first (news priority)
        if sym in flagged:
            r = _analyze_news_driven(sym, flagged[sym], spy_pct)
            if r:
                return r
        # Mode 1: breakout detection
        if sym in mover_syms:
            return _analyze_breakout(sym, spy_pct)
        return None

    with ThreadPoolExecutor(max_workers=15) as ex:
        futs = {ex.submit(run_and_release, analyze, s): s for s in all_candidates}
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                results.append(r)

    # Double-check all candidates
    verified = []
    for r in results:
        if _double_check(r["symbol"], r, spy_pct):
            verified.append(r)

    # Rank by score
    verified.sort(key=lambda x: x["score"], reverse=True)
    top = verified[:MAX_ALERTS_PER_SCAN]

    # Only alert if best score >= MIN_DAY_SCORE (never force)
    if not top or top[0]["score"] < MIN_DAY_SCORE:
        print(f"  No high-quality setups found (best score: {top[0]['score'] if top else 0})")
        return []

    print(f"  Found {len(verified)} verified setups, alerting top {len(top)}")
    return top

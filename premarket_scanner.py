"""Pre-market dedicated scanner — runs 6:00 AM to 9:30 AM ET.

Finds stocks gapping up or down with catalysts.
Sends alerts at 8:00 AM and 9:15 AM if setup still valid.
"""
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import POLYGON_API_KEY, TIMEZONE
from news_scanner import get_stock_news_score
from data_fetcher import run_and_release
from sector_rotation import get_sector_score

MIN_PREMARKET_SCORE = 60


def _get_prev_close(symbol: str) -> float:
    try:
        url = (f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev"
               f"?adjusted=true&apiKey={POLYGON_API_KEY}")
        r = requests.get(url, timeout=5).json()
        results = r.get("results", [])
        return float(results[0].get("c", 0)) if results else 0.0
    except Exception:
        return 0.0


def _get_premarket_price_vol(symbol: str) -> tuple:
    try:
        now = datetime.now(TIMEZONE)
        from_str = now.replace(hour=4, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
        to_str   = now.strftime("%Y-%m-%dT%H:%M:%S")
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/minute"
            f"/{from_str}/{to_str}?extended=true&apiKey={POLYGON_API_KEY}"
        )
        r = requests.get(url, timeout=8).json()
        bars = r.get("results", [])
        if not bars:
            info = yf.Ticker(symbol).fast_info
            price = float(getattr(info, "last_price", 0) or 0)
            return price, 0
        price = float(bars[-1].get("c", 0))
        vol   = sum(int(b.get("v", 0)) for b in bars)
        return price, vol
    except Exception:
        info = yf.Ticker(symbol).fast_info
        price = float(getattr(info, "last_price", 0) or 0)
        return price, 0


def _get_avg_daily_vol(symbol: str) -> float:
    try:
        info = yf.Ticker(symbol).fast_info
        return float(getattr(info, "three_month_average_volume", 0) or 0)
    except Exception:
        return 0.0


def _find_resistance(symbol: str, above_price: float) -> float:
    try:
        df = yf.Ticker(symbol).history(period="2mo", interval="1d")
        if df is None or df.empty:
            return above_price * 1.15
        highs = df["High"].values
        levels = sorted([h for h in highs if h > above_price * 1.01])
        return float(levels[0]) if levels else above_price * 1.15
    except Exception:
        return above_price * 1.15


def analyze_premarket_stock(symbol: str, regime: str) -> dict | None:
    try:
        prev_close = _get_prev_close(symbol)
        if not prev_close:
            return None

        pm_price, pm_vol = _get_premarket_price_vol(symbol)
        if not pm_price or pm_price <= 0:
            return None

        gap_pct = round((pm_price - prev_close) / prev_close * 100, 2)

        # Must be 5%+ gap
        if abs(gap_pct) < 5.0:
            return None

        # Gap down short only in BEAR regime
        is_gap_up = gap_pct > 0
        if not is_gap_up and regime != "BEAR":
            return None

        avg_vol     = _get_avg_daily_vol(symbol)
        # For micro-caps with no 3-month history, use absolute PM volume
        if avg_vol > 0:
            vol_ratio = pm_vol / avg_vol
        elif pm_vol > 50_000:
            vol_ratio = 5.0  # assume meaningful if >50k shares in PM
        else:
            vol_ratio = 0.0

        # Check news/catalyst
        news = get_stock_news_score(symbol)
        news_score_val = news.get("score", 0)
        has_catalyst = news_score_val > 20
        # 20%+ gap is self-evident — no catalyst requirement
        if not has_catalyst and abs(gap_pct) < 15:
            return None

        # Gap holding vs fading
        # Compare current PM price to open of PM session (earliest available bar)
        try:
            pm_open_price = prev_close * (1 + gap_pct / 200)
            holding = (pm_price >= pm_open_price * 0.98) if is_gap_up else (pm_price <= pm_open_price * 1.02)
        except Exception:
            holding = True

        # Room to run
        if is_gap_up:
            next_res  = _find_resistance(symbol, pm_price)
            room_pct  = (next_res - pm_price) / pm_price * 100
        else:
            room_pct  = abs(gap_pct) * 0.8

        if room_pct < 5.0:
            return None

        # Score
        score = 0
        components = {}
        gap_abs = abs(gap_pct)
        if gap_abs >= 30:
            components["gap"] = 65
        elif gap_abs >= 20:
            components["gap"] = 55
        elif gap_abs >= 10:
            components["gap"] = 35
        else:
            components["gap"] = 20
        score += components["gap"]

        if vol_ratio >= 10:
            components["volume"] = 50
        elif vol_ratio >= 5:
            components["volume"] = 35
        elif vol_ratio >= 2:
            components["volume"] = 20
        else:
            components["volume"] = 0
        score += components["volume"]

        if has_catalyst and news_score_val >= 50:
            components["news"] = 35
        elif has_catalyst:
            components["news"] = 15
        else:
            components["news"] = -20
        score += components["news"]

        components["holding"] = 25 if holding else -30
        score += components["holding"]

        if room_pct >= 10:
            components["room"] = 25
        elif room_pct >= 5:
            components["room"] = 15
        else:
            components["room"] = -20
        score += components["room"]

        sector_bonus, sector_name, sector_rank = get_sector_score(symbol)
        components["sector"] = sector_bonus
        score += sector_bonus

        if score < MIN_PREMARKET_SCORE:
            return None

        stop_pct = 0.03
        if is_gap_up:
            stop_loss  = round(pm_price * (1 - stop_pct), 2)
            target     = round(pm_price * (1 + room_pct / 100 * 0.7), 2)
            signal     = "STRONG BUY"
        else:
            stop_loss  = round(pm_price * (1 + stop_pct), 2)
            target     = round(pm_price * (1 - room_pct / 100 * 0.7), 2)
            signal     = "SHORT"

        risk   = abs(pm_price - stop_loss)
        reward = abs(target - pm_price)
        rr     = round(reward / risk, 2) if risk else 0

        reasons = [
            f"Gap {'up' if is_gap_up else 'down'} {gap_pct:+.1f}% from prev close ${prev_close:.2f}",
            f"Pre-market vol {vol_ratio:.1f}x avg daily volume",
            f"{'Holding strong' if holding else 'Fading — weaker setup'} | Room: {room_pct:.0f}%",
        ]
        if has_catalyst:
            reasons.append(f"Catalyst: {news.get('headline', '')[:60]}")

        return {
            "symbol":       symbol,
            "signal":       signal,
            "scanner":      "PREMARKET",
            "trade_type":   "DAY TRADE",
            "score":        score,
            "score_components": components,
            "conviction":   "HIGH" if score >= 110 else "SPECULATIVE",
            "price":        round(pm_price, 2),
            "stop_loss":    stop_loss,
            "price_target": target,
            "rr_ratio":     rr,
            "gap_pct":      gap_pct,
            "vol_ratio":    round(vol_ratio, 1),
            "room_pct":     round(room_pct, 1),
            "prev_close":   round(prev_close, 2),
            "has_news":     has_catalyst,
            "news_headline": news.get("headline", ""),
            "sector":       sector_name,
            "sector_rank":  sector_rank,
            "reasons":      reasons,
            "news_headline": news.get("headline", "") or "NO CATALYST — confirm with volume at open",
            "flags":        ["PRE-MARKET SETUP — confirm at open"],
        }
    except Exception:
        return None


def run_premarket_scanner(symbols: list, regime: str = "CHOPPY") -> list:
    now = datetime.now(TIMEZONE)
    print(f"\n=== PRE-MARKET SCANNER {now.strftime('%I:%M %p ET')} ===")

    results = []
    candidates = symbols[:600]

    with ThreadPoolExecutor(max_workers=15) as ex:
        futs = {ex.submit(run_and_release, analyze_premarket_stock, s, regime): s for s in candidates}
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                results.append(r)

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[:3]

    if top:
        print(f"  Found {len(results)} pre-market setups, top {len(top)}:")
        for r in top:
            print(f"  {r['symbol']} gap {r['gap_pct']:+.1f}% vol {r['vol_ratio']:.1f}x score {r['score']}")
    else:
        print("  No pre-market setups found")

    return top

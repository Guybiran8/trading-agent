"""After-hours scanner — runs 4:05 PM to 8:00 PM ET.

Finds earnings-driven and news-driven moves after market close.
"""
import yfinance as yf
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from config import TIMEZONE
from news_scanner import get_stock_news_score
from data_fetcher import run_and_release

MIN_AH_SCORE = 60
_alerted_ah_today: set = set()
_alerted_ah_date: date = None


def _reset_if_new_day():
    global _alerted_ah_today, _alerted_ah_date
    today = date.today()
    if _alerted_ah_date != today:
        _alerted_ah_today = set()
        _alerted_ah_date  = today


def already_alerted(symbol: str) -> bool:
    _reset_if_new_day()
    return symbol in _alerted_ah_today


def mark_alerted(symbol: str):
    _alerted_ah_today.add(symbol)


def _get_ah_move(symbol: str) -> dict | None:
    try:
        df = yf.Ticker(symbol).history(period="5d", interval="1d")
        if df is None or df.empty or len(df) < 2:
            return None

        prev_close = float(df["Close"].iloc[-2])
        today_close = float(df["Close"].iloc[-1])

        info = yf.Ticker(symbol).fast_info
        ah_price = float(getattr(info, "last_price", today_close) or today_close)

        if not prev_close:
            return None

        ah_pct = round((ah_price - today_close) / today_close * 100, 2)
        if abs(ah_pct) < 4.0:
            return None

        vol = float(getattr(info, "last_volume", 0) or 0)
        avg = float(getattr(info, "three_month_average_volume", 1) or 1)
        vol_ratio = round(vol / avg, 1) if avg > 0 else 0

        return {
            "symbol":      symbol,
            "price":       ah_price,
            "today_close": today_close,
            "prev_close":  prev_close,
            "ah_pct":      ah_pct,
            "vol_ratio":   vol_ratio,
        }
    except Exception:
        return None


def analyze_ah_stock(symbol: str) -> dict | None:
    move = _get_ah_move(symbol)
    if not move:
        return None

    ah_pct    = move["ah_pct"]
    vol_ratio = move["vol_ratio"]
    price     = move["price"]

    news = get_stock_news_score(symbol)
    news_score = news.get("score", 0)
    has_catalyst = news_score > 20

    score = 0
    components = {"move_magnitude": min(40, int(abs(ah_pct) * 2))}
    score += components["move_magnitude"]

    if vol_ratio >= 5:
        components["volume"] = 30
    elif vol_ratio >= 2:
        components["volume"] = 15
    else:
        components["volume"] = 0
    score += components["volume"]

    components["news"] = 25 if has_catalyst else 0
    score += components["news"]

    if score < MIN_AH_SCORE:
        return None

    is_gap_up = ah_pct > 0
    stop_loss  = round(price * (0.95 if is_gap_up else 1.05), 2)
    target     = round(price * (1 + abs(ah_pct) / 200 + 0.03) if is_gap_up else price * (1 - abs(ah_pct) / 200 - 0.03), 2)
    risk       = abs(price - stop_loss)
    reward     = abs(target - price)
    rr         = round(reward / risk, 2) if risk else 0

    reasons = [
        f"After-hours move {ah_pct:+.1f}% from close ${move['today_close']:.2f}",
        f"Volume {vol_ratio:.1f}x avg — {'earnings/news driven' if has_catalyst else 'possible catalyst'}",
        "Watch pre-market continuation for confirmation",
    ]
    if has_catalyst:
        reasons.append(f"Catalyst: {news.get('headline','')[:60]}")

    return {
        "symbol":       symbol,
        "signal":       "STRONG BUY" if is_gap_up else "STRONG SELL",
        "scanner":      "AFTERHOURS",
        "trade_type":   "DAY TRADE",
        "score":        score,
        "score_components": components,
        "conviction":   "HIGH" if score >= 80 else "SPECULATIVE",
        "price":        round(price, 2),
        "stop_loss":    stop_loss,
        "price_target": target,
        "rr_ratio":     rr,
        "intraday_pct": ah_pct,
        "vol_ratio":    vol_ratio,
        "room_pct":     round(abs(ah_pct) * 0.5 + 3, 1),
        "has_news":     has_catalyst,
        "news_headline": news.get("headline", ""),
        "reasons":      reasons,
        "flags":        ["AFTER-HOURS — watch pre-market for continuation"],
    }


def run_afterhours_scanner(symbols: list, alerted_today: set) -> list:
    now = datetime.now(TIMEZONE)
    hour = now.hour
    minute = now.minute

    if not ((hour == 16 and minute >= 5) or (17 <= hour < 20)):
        return []

    print(f"\n=== AFTER-HOURS SCANNER {now.strftime('%I:%M %p ET')} ===")

    eligible = [s for s in symbols if s not in alerted_today]
    results  = []

    with ThreadPoolExecutor(max_workers=15) as ex:
        futs = {ex.submit(run_and_release, analyze_ah_stock, s): s for s in eligible[:400]}
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                results.append(r)

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[:3]

    if top:
        print(f"  After-hours: {len(results)} movers, alerting top {len(top)}")
    else:
        print("  No after-hours setups")

    return top

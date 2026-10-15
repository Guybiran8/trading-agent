"""Open Drive Detector + 9:30 AM First-Minute Open Scan.

Open Drive: stock up 3%+ in first 15 min, every 5-min candle green,
no pullback > 1%, volume consistent/increasing. Alert at 9:45 AM.

Open Scan: at 9:30 AM find stocks with abnormal first-minute activity.
"""
import yfinance as yf
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from config import TIMEZONE
from data_fetcher import run_and_release

_open_drive_sent: date = None
_open_scan_sent: date = None


def _reset():
    pass


def _analyze_open_drive(symbol: str) -> dict | None:
    try:
        df_5m = yf.Ticker(symbol).history(period="2d", interval="5m")
        if df_5m is None or df_5m.empty or len(df_5m) < 3:
            return None

        today = datetime.now(TIMEZONE).date()
        if hasattr(df_5m.index, "tz_convert"):
            idx_et = df_5m.index.tz_convert(TIMEZONE)
        else:
            idx_et = df_5m.index
        today_mask = [i.date() == today for i in idx_et]
        df = df_5m[today_mask]

        if len(df) < 3:
            return None

        opens  = df["Open"].values.astype(float)
        closes = df["Close"].values.astype(float)
        highs  = df["High"].values.astype(float)
        lows   = df["Low"].values.astype(float)
        vols   = df["Volume"].values.astype(float)
        n      = len(df)

        first_open    = opens[0]
        current_close = closes[-1]
        total_move    = (current_close - first_open) / first_open * 100 if first_open else 0

        if total_move < 3.0:
            return None

        # Every 5-min candle must be green
        if not all(closes[i] > opens[i] for i in range(n)):
            return None

        # No pullback more than 1% from any running high
        running_high = first_open
        for i in range(n):
            running_high = max(running_high, highs[i])
            if running_high > 0 and lows[i] < running_high * 0.99:
                return None

        # Volume consistent or increasing (each bar at least 50% of previous)
        for i in range(1, n):
            if vols[i] < vols[i-1] * 0.50:
                return None

        stop_loss = round(float(lows.min()) * 0.99, 2)
        target    = round(current_close * (1 + total_move / 100 * 0.5), 2)
        risk      = abs(current_close - stop_loss)
        reward    = abs(target - current_close)
        rr        = round(reward / risk, 2) if risk > 0.001 else 0

        stop_pct = abs(current_close - stop_loss) / current_close * 100 if current_close else 0
        if stop_pct < 1.5:
            stop_loss = round(current_close * 0.985, 2)
            risk = abs(current_close - stop_loss)
            rr = round(reward / risk, 2) if risk > 0.001 else 0

        try:
            from news_scanner import get_stock_news_score
            news = get_stock_news_score(symbol)
            has_news = news.get("score", 0) > 20
            catalyst = (news.get("headline", "") or "").strip() or "NO CATALYST — trade with caution"
        except Exception:
            has_news = False
            catalyst = "NO CATALYST — trade with caution"

        vol_trend = "increasing" if vols[-1] >= vols[0] else "consistent"
        reasons = [
            f"OPEN DRIVE — up {total_move:+.1f}% from open ${first_open:.2f}, {n} green 5-min candles",
            f"No pullback > 1% at any point, volume {vol_trend}",
            catalyst if not has_news else f"NEWS: {catalyst[:70]}",
        ]

        components = {
            "drive_base": 70,
            "move_magnitude": int(total_move * 3),
            "news": 20 if has_news else 0,
        }
        return {
            "symbol":       symbol,
            "signal":       "STRONG BUY",
            "scanner":      "OPEN_DRIVE",
            "trade_type":   "DAY TRADE",
            "score":        min(130, components["drive_base"] + components["move_magnitude"] + components["news"]),
            "score_components": components,
            "conviction":   "HIGH" if total_move >= 5 else "SPECULATIVE",
            "price":        round(current_close, 2),
            "stop_loss":    stop_loss,
            "price_target": target,
            "rr_ratio":     rr,
            "intraday_pct": round(total_move, 2),
            "vol_ratio":    round(float(vols[-1]) / float(vols[0]), 1) if vols[0] > 0 else 1.0,
            "room_pct":     round(total_move * 0.5, 1),
            "has_news":     has_news,
            "news_headline": catalyst,
            "reasons":      reasons,
            "flags":        ["OPEN DRIVE — momentum sustained from open"],
        }
    except Exception:
        return None


def run_open_drive(symbols: list, alerted_today: set) -> list:
    global _open_drive_sent
    today = date.today()

    now = datetime.now(TIMEZONE)
    if not (now.hour == 9 and 42 <= now.minute <= 48):
        return []

    if _open_drive_sent == today:
        return []

    _open_drive_sent = today
    print(f"\n=== OPEN DRIVE SCANNER {now.strftime('%I:%M %p ET')} ===")

    eligible = [s for s in symbols if s not in alerted_today]
    results  = []

    with ThreadPoolExecutor(max_workers=15) as ex:
        futs = {ex.submit(run_and_release, _analyze_open_drive, s): s for s in eligible[:600]}
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                results.append(r)

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[:3]

    if top:
        print(f"  Open drives: {[r['symbol'] for r in top]}")
    else:
        print("  No open drive setups found")

    return top


def _analyze_open_scan(symbol: str) -> dict | None:
    """Check first-minute abnormal activity at 9:30 AM open."""
    try:
        df_1m = yf.Ticker(symbol).history(period="2d", interval="1m")
        if df_1m is None or df_1m.empty:
            return None

        today = datetime.now(TIMEZONE).date()
        if hasattr(df_1m.index, "tz_convert"):
            idx_et = df_1m.index.tz_convert(TIMEZONE)
        else:
            idx_et = df_1m.index
        today_mask = [i.date() == today for i in idx_et]
        df = df_1m[today_mask]

        if len(df) < 1:
            return None

        # Get daily data for context
        df_d = yf.Ticker(symbol).history(period="2mo", interval="1d")
        if df_d is None or df_d.empty or len(df_d) < 20:
            return None

        first_bar = df.iloc[0]
        first_vol = float(first_bar["Volume"])
        open_p    = float(first_bar["Open"])
        close_p   = float(first_bar["Close"])

        avg_daily_vol = float(df_d["Volume"].tail(20).mean())
        vol_ratio = first_vol / (avg_daily_vol / 390.0) if avg_daily_vol > 0 else 0

        prev_close = float(df_d["Close"].iloc[-2]) if len(df_d) >= 2 else open_p
        gap_pct = (open_p - prev_close) / prev_close * 100 if prev_close else 0

        high_20d = float(df_d["High"].tail(20).max())
        new_20d_high = close_p > high_20d

        if vol_ratio < 10 and abs(gap_pct) < 5 and not new_20d_high:
            return None

        stop_loss = round(min(open_p, close_p) * 0.97, 2)
        target    = round(close_p * 1.08, 2)
        risk      = abs(close_p - stop_loss)
        reward    = abs(target - close_p)
        rr        = round(reward / risk, 2) if risk > 0.001 else 0

        if abs(close_p - stop_loss) / close_p * 100 < 1.5:
            stop_loss = round(close_p * 0.985, 2)
            risk = abs(close_p - stop_loss)
            rr = round(reward / risk, 2) if risk > 0.001 else 0

        try:
            from news_scanner import get_stock_news_score
            news = get_stock_news_score(symbol)
            has_news = news.get("score", 0) > 20
            catalyst = (news.get("headline", "") or "").strip() or "NO CATALYST — trade with caution"
        except Exception:
            has_news = False
            catalyst = "NO CATALYST — trade with caution"

        triggers = []
        if vol_ratio >= 10:
            triggers.append(f"First-minute vol {vol_ratio:.0f}x avg")
        if abs(gap_pct) >= 5:
            triggers.append(f"Gap {gap_pct:+.1f}% from prev close")
        if new_20d_high:
            triggers.append(f"New 20-day high at open ${close_p:.2f}")

        components = {
            "scan_base": 80,
            "news": 20 if has_news else 0,
            "new_high": 15 if new_20d_high else 0,
            "volume": min(30, int(vol_ratio)),
        }
        score = components["scan_base"] + components["news"] + components["new_high"] + components["volume"]

        return {
            "symbol":       symbol,
            "signal":       "STRONG BUY" if gap_pct >= 0 else "STRONG SELL",
            "scanner":      "OPEN_SCAN",
            "trade_type":   "DAY TRADE",
            "score":        min(140, score),
            "score_components": components,
            "conviction":   "HIGH" if (vol_ratio >= 20 or new_20d_high) else "SPECULATIVE",
            "price":        round(close_p, 2),
            "stop_loss":    stop_loss,
            "price_target": target,
            "rr_ratio":     rr,
            "intraday_pct": round(gap_pct, 2),
            "vol_ratio":    round(vol_ratio, 1),
            "room_pct":     8.0,
            "has_news":     has_news,
            "news_headline": catalyst,
            "reasons":      [f"OPEN SCAN: {', '.join(triggers)}", catalyst],
            "flags":        [f"9:30 OPEN SCAN — {', '.join(triggers)}"],
        }
    except Exception:
        return None


def run_open_scan(symbols: list, alerted_today: set) -> list:
    global _open_scan_sent
    today = date.today()

    if _open_scan_sent == today:
        return []

    _open_scan_sent = today
    now = datetime.now(TIMEZONE)
    print(f"\n=== 9:30 OPEN SCAN {now.strftime('%I:%M %p ET')} ===")

    eligible = [s for s in symbols if s not in alerted_today]
    results  = []

    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(run_and_release, _analyze_open_scan, s): s for s in eligible[:800]}
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                results.append(r)

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[:3]

    if top:
        print(f"  Open scan alerts: {[r['symbol'] for r in top]}")
    else:
        print("  No abnormal open activity found")

    return top

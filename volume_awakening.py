"""Volume Awakening Detector — highest priority scanner, runs every 60 seconds 9:30-15:30.

Detects stocks ignored all day that suddenly get explosive volume mid-session.
Classic pattern: MASK, SPRC, NTCL, UMAC — flat for hours then explosive.
"""
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from config import TIMEZONE
from data_fetcher import run_and_release

_scan_offset: int = 0
_awakening_alerted: set = set()
_awakening_date: date = None

HOURLY_DEAD_THRESHOLD = 100_000
SPIKE_MULTIPLE_MIN = 10.0
MAX_BARS_SINCE_SPIKE = 20


def _reset_if_new_day():
    global _awakening_alerted, _awakening_date
    today = date.today()
    if _awakening_date != today:
        _awakening_alerted = set()
        _awakening_date = today


def _analyze_awakening(symbol: str) -> dict | None:
    try:
        df_1m = yf.Ticker(symbol).history(period="2d", interval="1m")
        if df_1m is None or df_1m.empty or len(df_1m) < 10:
            return None

        today = datetime.now(TIMEZONE).date()
        if hasattr(df_1m.index, "tz_convert"):
            idx_et = df_1m.index.tz_convert(TIMEZONE)
        else:
            idx_et = df_1m.index
        today_mask = [i.date() == today for i in idx_et]
        df = df_1m[today_mask]

        if len(df) < 30:
            return None

        vol    = df["Volume"].values.astype(float)
        closes = df["Close"].values.astype(float)
        lows   = df["Low"].values.astype(float)
        n      = len(vol)

        # Establish "dead" baseline: average volume per hour in first 3 hours (180 bars)
        first_3h_end = min(180, max(n - 3, 10))
        first_3h_total = float(vol[:first_3h_end].sum())
        hours_covered = first_3h_end / 60.0
        avg_vol_per_hour = first_3h_total / hours_covered if hours_covered > 0 else 0
        avg_per_min = avg_vol_per_hour / 60.0

        # Must have been dead: avg hourly < 100,000
        if avg_vol_per_hour >= HOURLY_DEAD_THRESHOLD:
            return None

        # Last 2 minutes must be spiking to 10x+ that hourly average
        last_2_vol = vol[-2:]
        recent_per_min = float(last_2_vol.mean())

        if avg_per_min <= 0:
            return None

        spike_multiple = recent_per_min / avg_per_min
        if spike_multiple < SPIKE_MULTIPLE_MIN:
            return None

        # Price making higher highs: current price > price 5 minutes ago
        current_price = float(closes[-1])
        price_5m_ago = float(closes[-6]) if n >= 6 else float(closes[0])
        if current_price <= price_5m_ago:
            return None

        # Find when spike started — scan backwards for first bar > 5x baseline
        spike_threshold = avg_per_min * 5
        spike_start_idx = None
        for i in range(n - 1, max(n - MAX_BARS_SINCE_SPIKE - 2, 0), -1):
            if vol[i] > spike_threshold:
                spike_start_idx = i

        if spike_start_idx is None:
            return None

        bars_since_spike = n - spike_start_idx
        if bars_since_spike > MAX_BARS_SINCE_SPIKE:
            return None

        spike_price = float(closes[spike_start_idx])
        price_since_spike = round((current_price - spike_price) / spike_price * 100, 2) if spike_price else 0

        # News check
        try:
            from news_scanner import get_stock_news_score
            news = get_stock_news_score(symbol)
            has_news = news.get("score", 0) > 20
            catalyst = (news.get("headline", "") or "").strip()
            if not catalyst:
                catalyst = "NO CATALYST — trade with caution"
        except Exception:
            has_news = False
            catalyst = "NO CATALYST — trade with caution"

        recent_low = float(lows[-min(5, n):].min())
        stop_loss = round(max(recent_low * 0.99, current_price * 0.97), 2)
        target    = round(current_price * 1.10, 2)
        risk      = abs(current_price - stop_loss)
        reward    = abs(target - current_price)
        rr        = round(reward / risk, 2) if risk > 0.001 else 0

        components = {
            "spike_base": 70,
            "spike_magnitude": min(60, int(spike_multiple)),
            "news": 30 if has_news else 0,
        }
        score = components["spike_base"] + components["spike_magnitude"] + components["news"]

        reasons = [
            f"Was {avg_vol_per_hour:,.0f} shares/hr for 3hrs. Now {recent_per_min*60:,.0f}/hr — {spike_multiple:.0f}x SPIKE",
            f"Spike started {bars_since_spike} min ago at ${spike_price:.2f} → ${current_price:.2f} ({price_since_spike:+.1f}%)",
            f"Higher highs: ${price_5m_ago:.2f} → ${current_price:.2f} in last 5 min",
            catalyst if not has_news else f"NEWS: {catalyst[:70]}",
        ]

        return {
            "symbol":           symbol,
            "signal":           "STRONG BUY",
            "scanner":          "VOLUME_AWAKENING",
            "trade_type":       "DAY TRADE",
            "score":            min(160, score),
            "score_components": components,
            "conviction":       "HIGH" if spike_multiple >= 20 or has_news else "SPECULATIVE",
            "price":            round(current_price, 2),
            "stop_loss":        stop_loss,
            "price_target":     target,
            "rr_ratio":         rr,
            "intraday_pct":     price_since_spike,
            "vol_ratio":        round(spike_multiple, 1),
            "room_pct":         10.0,
            "has_news":         has_news,
            "news_headline":    catalyst,
            "avg_hourly_vol":   round(avg_vol_per_hour),
            "spike_multiple":   round(spike_multiple, 1),
            "bars_since_spike": bars_since_spike,
            "spike_price":      round(spike_price, 2),
            "reasons":          reasons,
            "flags":            [f"VOLUME AWAKENING — {spike_multiple:.0f}x SPIKE from dead baseline"],
        }
    except Exception:
        return None


def run_volume_awakening(symbols: list, alerted_today: set) -> list:
    global _scan_offset
    _reset_if_new_day()

    now = datetime.now(TIMEZONE)
    if now.hour > 15 or (now.hour == 15 and now.minute >= 30):
        return []

    # Rotate through symbol list 200 at a time — covers full universe every ~30 min
    batch_size = 200
    n = len(symbols)
    if n == 0:
        return []

    start = _scan_offset % n
    batch = symbols[start:start + batch_size]
    if len(batch) < batch_size:
        batch += symbols[:batch_size - len(batch)]
    _scan_offset = (start + batch_size) % n

    eligible = [s for s in batch if s not in alerted_today]
    if not eligible:
        return []

    results = []
    with ThreadPoolExecutor(max_workers=15) as ex:
        futs = {ex.submit(run_and_release, _analyze_awakening, s): s for s in eligible}
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                results.append(r)

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[:3]

    if top:
        syms = [r["symbol"] for r in top]
        print(f"[VolumeAwakening] {len(results)} found in batch, alerting: {syms}")

    return top

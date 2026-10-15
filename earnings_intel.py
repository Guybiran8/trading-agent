import yfinance as yf
import requests
import pandas as pd
from datetime import date, timedelta
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from data_fetcher import get_stock_data
from indicators import add_indicators


SECTOR_ETFS = {
    "Technology": "XLK", "Financial Services": "XLF", "Healthcare": "XLV",
    "Consumer Cyclical": "XLY", "Communication Services": "XLC",
    "Industrials": "XLI", "Consumer Defensive": "XLP", "Energy": "XLE",
    "Utilities": "XLU", "Real Estate": "XLRE", "Basic Materials": "XLB",
}


def _get_sector_etf(symbol: str) -> str:
    try:
        info   = yf.Ticker(symbol).info
        sector = info.get("sector", "")
        return SECTOR_ETFS.get(sector, "SPY")
    except:
        return "SPY"


def _get_sector_trend(symbol: str) -> str:
    etf = _get_sector_etf(symbol)
    try:
        df = get_stock_data(etf, period="3mo")
        if df is None:
            return "unknown"
        df   = add_indicators(df)
        last = df.iloc[-1]
        if last["EMA20"] > last["EMA50"] > last["EMA200"]:
            return "bullish"
        elif last["EMA20"] < last["EMA50"] < last["EMA200"]:
            return "bearish"
        return "neutral"
    except:
        return "unknown"


def _get_earnings_history(symbol: str) -> list:
    """Return last 4 quarters as list of dicts with actual, estimate."""
    history = []
    try:
        ticker = yf.Ticker(symbol)
        qe = ticker.quarterly_earnings
        if qe is not None and not qe.empty:
            for i, (idx, row) in enumerate(qe.iterrows()):
                if i >= 4:
                    break
                history.append({
                    "quarter":  str(idx),
                    "actual":   row.get("Earnings"),
                    "estimate": row.get("Revenue"),
                })
    except:
        pass
    return history


def _get_options_iv(symbol: str) -> float:
    """Return ATM implied volatility (%) from nearest expiry options chain."""
    try:
        ticker = yf.Ticker(symbol)
        exps   = ticker.options
        if not exps:
            return 0.0
        chain = ticker.option_chain(exps[0])
        calls = chain.calls
        if calls.empty:
            return 0.0
        spot = ticker.fast_info.last_price or 0
        if not spot:
            return 0.0
        atm = calls.iloc[(calls["strike"] - spot).abs().argsort()[:1]]
        iv  = float(atm["impliedVolatility"].values[0]) * 100
        return round(iv, 1)
    except:
        return 0.0


def get_earnings_intel(symbol: str) -> dict:
    try:
        ticker = yf.Ticker(symbol)
        info   = ticker.info

        # Stock trend from daily candles
        df    = get_stock_data(symbol, period="6mo")
        trend = "unknown"
        if df is not None and len(df) >= 50:
            df   = add_indicators(df)
            last = df.iloc[-1]
            if last["EMA20"] > last["EMA50"] > last["EMA200"]:
                trend = "bullish"
            elif last["EMA20"] < last["EMA50"] < last["EMA200"]:
                trend = "bearish"
            else:
                trend = "neutral"

        sector_trend = _get_sector_trend(symbol)
        iv           = _get_options_iv(symbol)
        hist         = _get_earnings_history(symbol)

        try:
            eps_est = info.get("epsForward") or info.get("forwardEps")
        except:
            eps_est = None

        beats  = 0
        misses = 0
        real_data_points = 0
        for q in hist:
            a = q.get("actual")
            e = q.get("estimate")
            if a is not None and e is not None:
                real_data_points += 1
                if a > e:
                    beats += 1
                else:
                    misses += 1

        if trend == "bullish" and sector_trend == "bullish" and beats >= 3:
            bias = "BULLISH"
        elif trend == "bearish" and sector_trend == "bearish" and misses >= 3:
            bias = "BEARISH"
        elif trend == "bullish" or (beats > misses):
            bias = "LEANING BULLISH"
        elif trend == "bearish" or (misses > beats):
            bias = "LEANING BEARISH"
        else:
            bias = "NEUTRAL/UNCERTAIN"

        return {
            "symbol":           symbol,
            "trend":            trend,
            "sector_trend":     sector_trend,
            "sector_etf":       _get_sector_etf(symbol),
            "iv":               iv,
            "eps_estimate":     eps_est,
            "history":          hist,
            "beats":            beats,
            "misses":           misses,
            "real_data_points": real_data_points,
            "bias":             bias,
        }
    except Exception as e:
        return {"symbol": symbol, "bias": "UNKNOWN", "real_data_points": 0, "iv": 0, "trend": "unknown"}


def send_earnings_alert(intel: dict) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    symbol = intel.get("symbol", "?")
    bias   = intel.get("bias", "UNKNOWN")
    emoji  = "🟢" if "BULLISH" in bias else ("🔴" if "BEARISH" in bias else "🟡")

    hist_lines = []
    for q in intel.get("history", [])[:4]:
        a = q.get("actual")
        e = q.get("estimate")
        beat = ""
        if a is not None and e is not None:
            beat = " ✅ BEAT" if a > e else " ❌ MISS"
        elif a is not None:
            beat = f" actual: {a}"
        hist_lines.append(f"  {q['quarter']}{beat}")

    text = (
        f"{emoji} *EARNINGS SETUP: {symbol}*\n"
        f"Bias: {bias}\n"
        f"Stock trend: {intel.get('trend','?')} | Sector ({intel.get('sector_etf','?')}): {intel.get('sector_trend','?')}\n"
        f"Options IV: {intel.get('iv', 0):.1f}% | Fwd EPS est: {intel.get('eps_estimate','N/A')}\n"
        f"Last 4Q beat/miss: {intel.get('beats',0)} beats / {intel.get('misses',0)} misses\n"
    )
    if hist_lines:
        text += "History:\n" + "\n".join(hist_lines) + "\n"
    text += "\n⚠️ Earnings trades carry high risk — size small."

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except:
        pass


def run_earnings_scan(symbols: list) -> None:
    """
    Find stocks with earnings in 3-7 days. Only alert when ALL quality gates pass:
      - At least 1 real beat/miss data point (actual AND estimate both known)
      - IV above 30%
      - Stock has a clear bullish or bearish trend (not neutral/unknown)
    """
    today = date.today()
    print("[EarningsIntel] Scanning for upcoming earnings (3-7 days)...")
    checked = 0
    sent    = 0

    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            cal    = ticker.calendar
            if cal is None or not isinstance(cal, dict):
                continue
            dates = cal.get("Earnings Date", [])
            if not dates:
                continue
            earnings_date = pd.Timestamp(dates[0]).date()
            days_away     = (earnings_date - today).days
            if not (3 <= days_away <= 7):
                continue

            checked += 1
            print(f"  {symbol} earnings in {days_away}d — analyzing...")
            intel = get_earnings_intel(symbol)

            # ── Quality gate 1: real beat/miss history ─────────────────────
            if intel.get("real_data_points", 0) < 1:
                print(f"    {symbol} skipped — no real beat/miss data")
                continue

            # ── Quality gate 2: IV must be above 30% ──────────────────────
            if intel.get("iv", 0) < 30:
                print(f"    {symbol} skipped — IV {intel.get('iv',0):.1f}% < 30%")
                continue

            # ── Quality gate 3: clear trend direction ──────────────────────
            if intel.get("trend") in ("neutral", "unknown"):
                print(f"    {symbol} skipped — trend is {intel.get('trend')}")
                continue

            send_earnings_alert(intel)
            sent += 1

        except:
            continue

    print(f"[EarningsIntel] Done — checked {checked}, sent {sent} earnings alerts")

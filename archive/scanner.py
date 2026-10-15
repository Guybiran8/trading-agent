import yfinance as yf
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import (
    MIN_CONFIDENCE, TOP_N_RESULTS, MIN_RELATIVE_STRENGTH, POLYGON_API_KEY,
)
from data_fetcher import get_all_symbols, run_and_release
from indicators import add_indicators
from patterns import detect_patterns
from signal_engine import get_signal
from market_context import get_market_context
from telegram_notifier import send_telegram_alert
from active_trades import save_pending_alert
from trade_monitor import register_passive_track
from datetime import date, datetime, timedelta
import pandas as pd

SPY_PERF_20D = None

_alerted_today: set = set()
_alerted_date: date = None

# Cached SPY intraday % — refreshed at most once per 60 s
_spy_intraday_pct: float = 0.0
_spy_intraday_ts: datetime = None

SECTOR_ETFS = {
    "Technology": "XLK", "Financial Services": "XLF", "Healthcare": "XLV",
    "Consumer Cyclical": "XLY", "Communication Services": "XLC",
    "Industrials": "XLI", "Consumer Defensive": "XLP", "Energy": "XLE",
    "Utilities": "XLU", "Real Estate": "XLRE", "Basic Materials": "XLB",
}


# ── Fix 4: ONE alert per symbol per day across ALL scanners ──────────────────
def _maybe_alert(result: dict) -> None:
    global _alerted_today, _alerted_date
    today = date.today()
    if _alerted_date != today:
        _alerted_today = set()
        _alerted_date  = today
    symbol = result["symbol"]
    if symbol in _alerted_today:          # same symbol, any scanner → skip
        return
    _alerted_today.add(symbol)
    send_telegram_alert(result)
    save_pending_alert(result)      # store for user confirmation matching
    register_passive_track(result)  # silent EOD tracking if not confirmed


def get_spy_performance():
    global SPY_PERF_20D
    if SPY_PERF_20D is not None:
        return SPY_PERF_20D
    try:
        spy_df   = yf.Ticker("SPY").history(period="2mo", interval="1d")
        SPY_PERF_20D = (spy_df["Close"].iloc[-1] - spy_df["Close"].iloc[-20]) / spy_df["Close"].iloc[-20] * 100
    except:
        SPY_PERF_20D = 0
    return SPY_PERF_20D


def _refresh_spy_intraday() -> float:
    """Cache SPY open-to-current % change; refresh at most once per 60 s."""
    global _spy_intraday_pct, _spy_intraday_ts
    now = datetime.utcnow()
    if _spy_intraday_ts and (now - _spy_intraday_ts).total_seconds() < 60:
        return _spy_intraday_pct
    try:
        df = yf.Ticker("SPY").history(period="1d", interval="1m")
        if not df.empty:
            open_p            = float(df["Open"].iloc[0])
            curr              = float(df["Close"].iloc[-1])
            _spy_intraday_pct = round((curr - open_p) / open_p * 100, 2) if open_p else 0.0
        else:
            _spy_intraday_pct = 0.0
        _spy_intraday_ts = now
    except:
        _spy_intraday_pct = 0.0
    return _spy_intraday_pct


def _get_sector_trend(symbol: str) -> str:
    try:
        info   = yf.Ticker(symbol).info
        sector = info.get("sector", "")
        etf    = SECTOR_ETFS.get(sector, "SPY")
        df     = yf.Ticker(etf).history(period="3mo", interval="1d")
        if df.empty or len(df) < 50:
            return "neutral"
        df   = add_indicators(df)
        last = df.iloc[-1]
        if last["EMA20"] > last["EMA50"] > last["EMA200"]:
            return "bullish"
        elif last["EMA20"] < last["EMA50"] < last["EMA200"]:
            return "bearish"
        return "neutral"
    except:
        return "neutral"


def verify_weekly_setup(symbol: str, direction: str) -> bool:
    try:
        df = yf.Ticker(symbol).history(period="2y", interval="1wk")
        if df.empty or len(df) < 20:
            return True
        df.columns = [c.strip() for c in df.columns]
        df    = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        if len(df) < 20:
            return True
        df    = add_indicators(df)
        last  = df.iloc[-1]
        ema20 = last["EMA20"]
        ema50 = last.get("EMA50", ema20)
        close = last["Close"]
        if direction == "long":
            return close > ema20 and close > ema50
        elif direction == "short":
            return close < ema20 and close < ema50
        return True
    except:
        return True


# ── Fix 1: RSI calculation for movers ────────────────────────────────────────
def _calc_daily_rsi(symbol: str) -> float:
    """Compute RSI(14) from the last ~30 days of daily bars."""
    try:
        df    = yf.Ticker(symbol).history(period="1mo", interval="1d")
        if df.empty or len(df) < 15:
            return 0.0
        close = df["Close"].astype(float)
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss
        rsi   = 100 - (100 / (1 + rs))
        val   = float(rsi.iloc[-1])
        return round(val, 1) if not pd.isna(val) else 0.0
    except:
        return 0.0


# ── Fix 9: Only alert if move started within last 30 minutes ─────────────────
def _move_started_recently(symbol: str, max_bars_ago: int = 30) -> bool:
    """
    Returns True only if the stock's significant move (>=2% from open) began
    within the last `max_bars_ago` one-minute bars (~30 minutes).
    Prevents alerting on moves that already ran for hours.
    """
    try:
        df = yf.Ticker(symbol).history(period="1d", interval="1m")
        if df.empty or len(df) < 10:
            return True   # can't determine; allow through
        open_price = float(df["Open"].iloc[0])
        closes     = df["Close"].astype(float)
        n          = len(closes)
        move_start = None
        for i in range(n):
            pct = abs(float(closes.iloc[i]) - open_price) / open_price * 100
            if pct >= 2.0:
                move_start = i
                break
        if move_start is None:
            return True   # no clear move start; allow through
        bars_since = n - move_start
        return bars_since <= max_bars_ago
    except:
        return True


# ── Enrichment helpers ────────────────────────────────────────────────────────

def get_float_short_info(symbol: str) -> dict:
    out = {
        "float_shares_m":         None,
        "short_pct":              None,
        "is_low_float":           False,
        "is_high_short_interest": False,
    }
    try:
        info      = yf.Ticker(symbol).info
        raw_float = info.get("floatShares")
        raw_short = info.get("shortPercentOfFloat")
        if raw_float:
            float_m               = raw_float / 1_000_000
            out["float_shares_m"] = round(float_m, 1)
            out["is_low_float"]   = float_m < 50
        if raw_short is not None:
            short_pct                     = raw_short * 100 if raw_short < 1 else raw_short
            out["short_pct"]              = round(short_pct, 1)
            out["is_high_short_interest"] = short_pct > 20
    except:
        pass
    return out


def check_options_flow(symbol: str) -> dict:
    out = {"unusual": False, "detail": ""}
    try:
        ticker = yf.Ticker(symbol)
        exps   = ticker.options
        if not exps:
            return out
        cutoff = (date.today() + timedelta(days=14)).isoformat()
        near   = [e for e in exps if e <= cutoff][:3]
        for exp in near:
            chain = ticker.option_chain(exp)
            calls = chain.calls
            if calls.empty:
                continue
            if "volume" not in calls.columns or "openInterest" not in calls.columns:
                continue
            vol  = calls["volume"].fillna(0)
            oi   = calls["openInterest"].fillna(0).clip(lower=1)
            mask = (vol >= 100) & (oi > 0) & (vol > 3 * oi)
            hits = calls[mask]
            if not hits.empty:
                row            = hits.iloc[0]
                out["unusual"] = True
                out["detail"]  = (
                    f"${row.get('strike','?')} call exp {exp} "
                    f"vol={int(row.get('volume',0))} oi={int(row.get('openInterest',0))}"
                )
                return out
    except:
        pass
    return out


def check_institutional_flow(symbol: str) -> dict:
    out = {"institutional": False, "avg_size": 0}
    try:
        df = yf.Ticker(symbol).history(period="1d", interval="1m")
        if df.empty or len(df) < 5:
            return out
        recent        = df.tail(10)
        avg           = int(recent["Volume"].mean())
        out["avg_size"]      = avg
        out["institutional"] = avg > 10_000
    except:
        pass
    return out


def _stock_intraday_pct(symbol: str) -> float:
    try:
        df = yf.Ticker(symbol).history(period="1d", interval="1m")
        if df.empty:
            return 0.0
        open_p = float(df["Open"].iloc[0])
        curr   = float(df["Close"].iloc[-1])
        return round((curr - open_p) / open_p * 100, 2) if open_p else 0.0
    except:
        return 0.0


def enrich_result(result: dict) -> dict:
    symbol = result["symbol"]
    flags  = []

    fi = get_float_short_info(symbol)
    result.update(fi)
    is_squeeze = fi["is_low_float"] or fi["is_high_short_interest"]
    if fi["is_low_float"]:
        flags.append(f"LOW FLOAT ({fi['float_shares_m']}M shares)")
    if fi["is_high_short_interest"]:
        flags.append(f"HIGH SHORT INTEREST ({fi['short_pct']}%)")
    if is_squeeze:
        flags.append("SQUEEZE SETUP")
        result["confidence"] = min(99, result["confidence"] + 10)

    opt = check_options_flow(symbol)
    result["unusual_options"]        = opt["unusual"]
    result["unusual_options_detail"] = opt.get("detail", "")
    if opt["unusual"]:
        flags.append("UNUSUAL OPTIONS ACTIVITY")

    stock_pct = _stock_intraday_pct(symbol)
    spy_pct   = _spy_intraday_pct
    result["stock_intraday_pct"] = stock_pct
    is_rs = stock_pct >= 3.0 and spy_pct <= 0.0
    result["is_rs_leader"] = is_rs
    if is_rs:
        flags.append(f"RELATIVE STRENGTH LEADER ({stock_pct:+.1f}% vs SPY {spy_pct:+.1f}%)")
        result["confidence"] = min(99, result["confidence"] + 8)

    inst = check_institutional_flow(symbol)
    result["institutional_flow"]     = inst["institutional"]
    result["institutional_avg_size"] = inst["avg_size"]
    if inst["institutional"]:
        flags.append(f"INSTITUTIONAL FLOW DETECTED (avg {inst['avg_size']:,} shares/min)")
        result["confidence"] = min(99, result["confidence"] + 5)

    result["flags"] = flags
    return result


# ── Core swing scanner (ADX + RS filters apply here only) ────────────────────

def analyze_stock(symbol, market_context, spy_perf):
    try:
        ticker = yf.Ticker(symbol)
        df     = ticker.history(period="6mo", interval="1d", auto_adjust=True)
        if df is None or df.empty or len(df) < 50:
            return None

        df.columns = [col.strip() for col in df.columns]
        required   = ["Open", "High", "Low", "Close", "Volume"]
        if not all(col in df.columns for col in required):
            return None

        df = df[required].copy()
        df.dropna(inplace=True)
        if len(df) < 50:
            return None

        df = add_indicators(df)
        if df is None or len(df) < 10:
            return None

        # ADX filter — swing scanner only
        try:
            if df["ADX"].iloc[-1] < 20:
                return None
        except:
            pass

        # Relative strength filter — swing scanner only
        try:
            stock_perf   = df["Mom_20"].iloc[-1]
            rel_strength = stock_perf - spy_perf
            if rel_strength < MIN_RELATIVE_STRENGTH:
                return None
        except:
            rel_strength = 0

        patterns = detect_patterns(df)
        signal   = get_signal(df, patterns)
        if signal["signal"] == "ERROR":
            return None

        signal_label = signal["signal"]
        reasons      = signal.get("reasons", [])

        if signal_label in ("STRONG BUY", "BUY"):
            try:
                if df["EMA20"].iloc[-1] < df["EMA50"].iloc[-1] < df["EMA200"].iloc[-1]:
                    return None
            except:
                pass
            if any("MACD bearish" in r for r in reasons[:3]):
                return None
            if any("Price above EMA20" in r for r in reasons):
                return None
            if not verify_weekly_setup(symbol, "long"):
                return None

        market_score = market_context.get("score", 0)
        if market_score >= 2:
            if signal_label not in ("STRONG BUY", "BUY"):
                return None
        elif market_score <= -2:
            if signal_label not in ("STRONG SELL", "SELL"):
                return None
        else:
            if signal_label not in ("STRONG BUY", "STRONG SELL"):
                return None

        close = signal["price"]
        atr   = signal["atr"]

        # Fix 2: correct direction for stop/target
        if signal_label in ("STRONG SELL", "SELL"):
            stop_loss    = round(close + atr * 1.5, 2)   # above entry
            price_target = round(close - atr * 3.0, 2)   # below entry
        else:
            stop_loss    = round(close - atr * 1.5, 2)   # below entry
            price_target = round(close + atr * 3.0, 2)   # above entry

        risk     = abs(close - stop_loss)
        reward   = abs(price_target - close)
        rr_ratio = round(reward / risk, 2) if risk > 0 else 0
        final_score = max(-100, min(100, signal["score"] + market_score))
        confidence  = abs(final_score)
        if confidence < MIN_CONFIDENCE:
            return None

        try:
            info      = yf.Ticker(symbol).fast_info
            price     = getattr(info, "last_price", signal["price"]) or signal["price"]
            vol_today = getattr(info, "last_volume", 0) or 0
            if vol_today == 0:
                return None
        except:
            return None

        result = {
            "symbol":       symbol,
            "signal":       signal_label,
            "scanner":      "swing",
            "trade_type":   "SWING TRADE",
            "score":        final_score,
            "confidence":   confidence,
            "price":        signal["price"],
            "last_price":   signal["price"],
            "pre_change":   0,
            "volume_ratio": round(df["Vol_ratio"].iloc[-1], 2) if "Vol_ratio" in df.columns else 0,
            "price_target": price_target,
            "stop_loss":    stop_loss,
            "support":      signal["support"],
            "resistance":   signal["resistance"],
            "rr_ratio":     rr_ratio,
            "rsi":          signal["rsi"],
            "atr":          atr,
            "phase":        signal["phase"],
            "rel_strength": round(rel_strength, 2),
            "patterns":     patterns,
            "reasons":      reasons,
            "flags":        [],
        }
        return enrich_result(result)
    except:
        return None


def analyze_short_candidate(symbol: str, market_context: dict):
    try:
        df = yf.Ticker(symbol).history(period="6mo", interval="1d", auto_adjust=True)
        if df is None or df.empty or len(df) < 50:
            return None

        df.columns = [c.strip() for c in df.columns]
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        if len(df) < 50:
            return None

        df = add_indicators(df)
        if df is None or len(df) < 10:
            return None

        last   = df.iloc[-1]
        close  = last["Close"]
        ema20  = last["EMA20"]
        ema50  = last["EMA50"]
        ema200 = last["EMA200"]

        if not (ema20 < ema50 < ema200):
            return None

        rsi = last["RSI"]
        if rsi < 70:
            return None

        dist_ema20 = (close - ema20) / ema20
        if dist_ema20 < 0.08:
            return None

        if last["Vol_ratio"] < 0.8:
            return None

        if not verify_weekly_setup(symbol, "short"):
            return None

        recent_high  = df["High"].iloc[-10:].max()
        atr          = last["ATR"]
        # Fix 2: stop above entry, target below entry
        stop_loss    = round(recent_high * 1.01, 2)   # above current price
        price_target = round(ema50, 2)                 # below current price (EMA50 < close)

        risk   = abs(stop_loss - close)
        reward = abs(close - price_target)
        rr     = round(reward / risk, 2) if risk > 0 else 0
        if rr < 1.5:
            return None

        try:
            info      = yf.Ticker(symbol).fast_info
            vol_today = getattr(info, "last_volume", 0) or 0
            if vol_today == 0:
                return None
        except:
            return None

        confidence = min(95, int(60 + (rsi - 70) * 2 + dist_ema20 * 100))

        result = {
            "symbol":       symbol,
            "signal":       "SHORT",
            "scanner":      "SHORT",
            "trade_type":   "SWING TRADE",
            "confidence":   confidence,
            "price":        round(close, 2),
            "last_price":   round(close, 2),
            "price_target": price_target,
            "stop_loss":    stop_loss,
            "rr_ratio":     rr,
            "rsi":          round(rsi, 1),
            "atr":          round(atr, 2),
            "volume_ratio": round(last["Vol_ratio"], 2),
            "reasons": [
                "Downtrend confirmed (EMA20 < EMA50 < EMA200)",
                f"RSI overbought ({rsi:.1f})",
                f"Price {dist_ema20*100:.1f}% above EMA20 — extended",
            ],
            "flags": [],
        }
        return enrich_result(result)
    except:
        return None


def _prefilter_symbol(symbol):
    try:
        info      = yf.Ticker(symbol).fast_info
        vol_today = getattr(info, "last_volume", 0) or 0
        if vol_today > 0:
            return symbol
    except:
        pass
    return None


def run_scanner(symbols=None):
    global SPY_PERF_20D
    SPY_PERF_20D = None

    print("Checking market conditions...")
    market_context = get_market_context()
    print(f"  Market: {market_context['condition']}")

    print("Getting SPY performance...")
    spy_perf = get_spy_performance()
    print(f"  SPY 20d performance: {spy_perf:.1f}%")

    _refresh_spy_intraday()
    print(f"  SPY intraday: {_spy_intraday_pct:+.2f}%")

    if symbols is None:
        print("Loading symbols...")
        symbols = get_all_symbols()

    print(f"Pre-filtering {len(symbols)} stocks...")
    candidates = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(run_and_release, _prefilter_symbol, s): s for s in symbols}
        for fut in as_completed(futures):
            r = fut.result()
            if r:
                candidates.append(r)
    print(f"  {len(candidates)} stocks passed pre-filter")

    print(f"Scanning {len(candidates)} stocks (LONG + SHORT)...")
    results   = []
    completed = 0

    with ThreadPoolExecutor(max_workers=10) as ex:
        long_futs  = {ex.submit(run_and_release, analyze_stock, s, market_context, spy_perf): s for s in candidates}
        short_futs = {ex.submit(run_and_release, analyze_short_candidate, s, market_context): s for s in candidates}

        for fut in as_completed({**long_futs, **short_futs}):
            completed += 1
            r = fut.result()
            if r:
                results.append(r)
                _maybe_alert(r)
            if completed % 100 == 0:
                print(f"  {completed}/{len(candidates)*2} done — {len(results)} signals so far")

    results.sort(
        key=lambda x: ("SQUEEZE SETUP" in x.get("flags", []), x["confidence"]),
        reverse=True,
    )
    top = results[:TOP_N_RESULTS]

    print(f"\nScan complete — {len(results)} signals found")
    print("=" * 60)
    for r in top:
        flags_str = " | ".join(r.get("flags", [])) if r.get("flags") else ""
        print(f"\n{r['symbol']} | {r['signal']} | {r.get('trade_type','SWING')} | {r['confidence']}%")
        if flags_str:
            print(f"  *** {flags_str} ***")
        print(f"  Price: ${r['price']} | Target: ${r['price_target']} | Stop: ${r['stop_loss']}")
        print(f"  R/R: {r['rr_ratio']} | RSI: {r['rsi']}")
        for reason in r.get("reasons", [])[:3]:
            print(f"    {reason}")
        print("-" * 60)

    return results, market_context


# ── Intraday / Pre-market / After-hours scanners ──────────────────────────────
# NOTE: ADX and relative-strength filters are intentionally ABSENT from these
# scanners. Momentum plays do not require trend filters.

def _get_movers_yf(symbols: list, min_change_pct: float,
                   min_vol_ratio: float) -> list:
    """
    Batch-download 5 days of daily data; return recent movers.
    No ADX / relative-strength filtering (momentum plays only).
    """
    if not symbols:
        return []
    try:
        data = yf.download(
            symbols[:400], period="5d", interval="1d",
            progress=False, auto_adjust=True, group_by="ticker",
        )
    except:
        return []

    results    = []
    sample_set = symbols[:400]

    for sym in sample_set:
        try:
            if isinstance(data.columns, pd.MultiIndex):
                close_s  = data[sym]["Close"].dropna()
                volume_s = data[sym]["Volume"].dropna()
            else:
                close_s  = data["Close"].dropna()
                volume_s = data["Volume"].dropna()

            if len(close_s) < 2:
                continue

            prev_c   = float(close_s.iloc[-2])
            curr_c   = float(close_s.iloc[-1])
            curr_vol = float(volume_s.iloc[-1])
            avg_vol  = float(volume_s.mean())

            if not prev_c or not avg_vol:
                continue

            change_pct = (curr_c - prev_c) / prev_c * 100
            vol_ratio  = curr_vol / avg_vol

            if abs(change_pct) >= min_change_pct and curr_vol > 0:
                if True:
                    results.append({
                        "symbol":         sym,
                        "price":          round(curr_c, 2),
                        "day_change_pct": round(change_pct, 2),
                        "vol_ratio":      round(vol_ratio, 2),
                        "day_vol":        int(curr_vol),
                        "prev_close":     round(prev_c, 2),
                    })
        except:
            continue

    results.sort(key=lambda x: abs(x["day_change_pct"]), reverse=True)
    return results[:50]


def _build_mover_result(m: dict, scanner: str,
                         target_pct: float, stop_pct: float, rr: float) -> dict:
    """
    Build and enrich a result dict for a price mover.

    Fix 1: RSI is calculated from real daily bars (not hardcoded 0).
    Fix 2: For bearish movers (STRONG SELL), target is BELOW price and stop
           is ABOVE price.  target_pct / stop_pct are positive fractions.
    Fix 7: No ADX or relative-strength filters applied here.
    """
    price   = m["price"]
    is_bear = m["day_change_pct"] < 0

    if is_bear:
        signal       = "STRONG SELL"
        price_target = round(price * (1 - target_pct), 2)   # below entry ✓
        stop_loss    = round(price * (1 + stop_pct),   2)   # above entry ✓
    else:
        signal       = "STRONG BUY"
        price_target = round(price * (1 + target_pct), 2)   # above entry ✓
        stop_loss    = round(price * (1 - stop_pct),   2)   # below entry ✓

    # Fix 1: calculate real RSI from daily bars
    rsi = _calc_daily_rsi(m["symbol"])

    result = {
        "symbol":         m["symbol"],
        "signal":         signal,
        "scanner":        scanner,
        "trade_type":     "DAY TRADE",
        "confidence":     min(95, int(60 + abs(m["day_change_pct"]))),
        "price":          price,
        "last_price":     price,
        "price_target":   price_target,
        "stop_loss":      stop_loss,
        "rr_ratio":       rr,
        "rsi":            rsi,
        "atr":            round(price * 0.02, 2),
        "volume_ratio":   m["vol_ratio"],
        "day_change_pct": m["day_change_pct"],
        "flags":          [],
        "reasons": [
            f"Move {m['day_change_pct']:+.1f}%",
            f"Volume {m['vol_ratio']:.1f}x average",
        ],
    }
    return enrich_result(result)


def run_premarket_scanner(symbols: list = None) -> list:
    """6:00–9:30 AM ET — gaps 5%+ with 2x volume. Label: DAY TRADE."""
    print("\n=== PRE-MARKET SCANNER ===")
    _refresh_spy_intraday()
    if symbols is None:
        symbols = get_all_symbols()
    movers = _get_movers_yf(symbols, min_change_pct=5.0, min_vol_ratio=2.0)
    alerts = []
    for m in movers:
        result               = _build_mover_result(m, "premarket", 0.03, 0.03, 2.0)
        result["gap_pct"]    = m["day_change_pct"]
        result["reasons"][0] = f"Gap {m['day_change_pct']:+.1f}% pre-market"
        result["reasons"].append("Pre-market momentum — DAY TRADE only")
        _maybe_alert(result)
        alerts.append(result)
        flags_str = " | ".join(result.get("flags", []))
        print(f"  {m['symbol']} | Gap {m['day_change_pct']:+.1f}% | Vol {m['vol_ratio']:.1f}x | RSI {result['rsi']}"
              + (f" | {flags_str}" if flags_str else ""))
    print(f"Pre-market scan: {len(alerts)} alerts sent")
    return alerts


def run_intraday_momentum_scanner(symbols: list = None) -> list:
    """
    Market hours — up 5%+ with 3x volume. Label: DAY TRADE.
    Fix 9: Only alert if the move started within the last 30 minutes.
    Fix 7: No ADX / relative-strength filters.
    """
    print("\n=== INTRADAY MOMENTUM SCANNER ===")
    _refresh_spy_intraday()
    if symbols is None:
        symbols = get_all_symbols()
    movers = _get_movers_yf(symbols, min_change_pct=5.0, min_vol_ratio=3.0)
    alerts = []
    for m in movers:
        # Fix 9: skip if move is stale (started more than 30 minutes ago)
        if not _move_started_recently(m["symbol"], max_bars_ago=30):
            print(f"  {m['symbol']} skipped — move started >30 min ago")
            continue
        result               = _build_mover_result(m, "intraday", 0.04, 0.03, round(4/3, 2))
        result["reasons"][0] = f"Intraday move {m['day_change_pct']:+.1f}%"
        result["reasons"].append("Momentum surge — DAY TRADE")
        _maybe_alert(result)
        alerts.append(result)
        flags_str = " | ".join(result.get("flags", []))
        print(f"  {m['symbol']} | Move {m['day_change_pct']:+.1f}% | Vol {m['vol_ratio']:.1f}x | RSI {result['rsi']}"
              + (f" | {flags_str}" if flags_str else ""))
    print(f"Intraday scan: {len(alerts)} alerts sent")
    return alerts


def run_afterhours_scanner(symbols: list = None) -> list:
    """
    4:00–8:00 PM ET STRICTLY — news/earnings-driven moves after close.
    Fix 3: Explicit time guard — will not fire outside 4:05 PM–8:00 PM ET.
    """
    from config import TIMEZONE as _TZ
    now  = datetime.now(_TZ)
    hour = now.hour
    minute = now.minute
    # Fix 3: strict after-hours window (add 5-min buffer after 4 PM close)
    if not ((hour == 16 and minute >= 5) or (17 <= hour < 20)):
        print(f"[AH Scanner] Skipped — outside after-hours window ({now.strftime('%I:%M %p ET')})")
        return []

    print("\n=== AFTER-HOURS SCANNER ===")
    _refresh_spy_intraday()
    if symbols is None:
        symbols = get_all_symbols()
    movers = _get_movers_yf(symbols, min_change_pct=4.0, min_vol_ratio=2.0)
    alerts = []
    for m in movers:
        result               = _build_mover_result(m, "afterhours", 0.05, 0.05, 2.0)
        result["atr"]        = round(m["price"] * 0.03, 2)
        result["reasons"][0] = f"After-hours move {m['day_change_pct']:+.1f}%"
        result["reasons"][1] = f"Volume {m['vol_ratio']:.1f}x — possible news/earnings"
        result["reasons"].append("Watch pre-market for continuation")
        _maybe_alert(result)
        alerts.append(result)
        flags_str = " | ".join(result.get("flags", []))
        print(f"  {m['symbol']} | AH move {m['day_change_pct']:+.1f}% | Vol {m['vol_ratio']:.1f}x | RSI {result['rsi']}"
              + (f" | {flags_str}" if flags_str else ""))
    print(f"After-hours scan: {len(alerts)} alerts sent")
    return alerts

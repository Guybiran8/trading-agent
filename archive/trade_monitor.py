"""
trade_monitor.py — Real-time monitoring for confirmed and passive trades.

Active trades  : one thread per confirmed trade, checks every 60 s via yfinance 1-min bars.
Passive trades : single background thread checks every 5 min for EOD summary.
"""
import csv
import os
import threading
import time
from datetime import datetime, date

import pandas as pd
import requests
import yfinance as yf

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TIMEZONE
from active_trades import (
    load_trades, remove_trade, update_trade_fields,
    get_all_pending_alerts, clear_pending_alerts,
)

# ── Thread state ──────────────────────────────────────────────────────────────
_active_threads: dict = {}          # symbol -> True
_active_lock = threading.Lock()

_passive_cache: dict = {}           # symbol -> latest price info for EOD
_passive_lock = threading.Lock()


# ── Telegram helper ───────────────────────────────────────────────────────────

def _tg(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except:
        pass


# ── 1-minute indicators ───────────────────────────────────────────────────────

def _get_indicators(symbol: str) -> dict:
    """
    Fetch today's 1-minute bars and compute:
      price, RSI(14), EMA20, vol_ratio (last-3-bar avg / session avg).
    Returns {} on failure.
    """
    try:
        df = yf.Ticker(symbol).history(period="1d", interval="1m")
        if df.empty or len(df) < 20:
            return {}

        close = df["Close"].astype(float)
        price = float(close.iloc[-1])

        # RSI(14)
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss
        rsi_s = 100 - (100 / (1 + rs))
        rsi   = float(rsi_s.iloc[-1])
        if pd.isna(rsi):
            rsi = 50.0

        # EMA20 (1-min)
        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])

        # Volume ratio: last-3-bar avg vs full-session avg
        vol      = df["Volume"].astype(float)
        vol3     = float(vol.iloc[-3:].mean())
        vol_avg  = float(vol.mean())
        vol_ratio = (vol3 / vol_avg) if vol_avg > 0 else 1.0

        return {
            "price":     round(price, 2),
            "rsi":       round(rsi, 1),
            "ema20":     round(ema20, 2),
            "vol_ratio": round(vol_ratio, 3),
        }
    except:
        return {}


# ── Performance logging (confirmed trades only) ───────────────────────────────

TRADES_CSV = "/root/trading-agent/trades.csv"
FIELDNAMES = [
    "symbol", "date", "signal_type", "trade_type",
    "entry_price", "stop", "target",
    "status", "exit_price", "exit_date", "pnl_pct",
]


def _log_closed_trade(trade: dict, exit_price: float, status: str) -> None:
    exists = os.path.exists(TRADES_CSV)
    with open(TRADES_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not exists:
            w.writeheader()
        entry    = float(trade.get("entry_price", 0))
        is_short = trade.get("signal", "") in ("SHORT", "STRONG SELL", "SELL")
        pnl      = round((exit_price - entry) / entry * 100 * (-1 if is_short else 1), 2) if entry else 0
        w.writerow({
            "symbol":      trade["symbol"],
            "date":        str(trade.get("confirmed_at", ""))[:10],
            "signal_type": trade.get("signal", ""),
            "trade_type":  trade.get("trade_type", ""),
            "entry_price": entry,
            "stop":        trade.get("stop_loss", 0),
            "target":      trade.get("price_target", 0),
            "status":      status,
            "exit_price":  exit_price,
            "exit_date":   date.today().isoformat(),
            "pnl_pct":     pnl,
        })


# ── Active trade monitor loop ─────────────────────────────────────────────────

def _monitor_loop(symbol: str) -> None:
    print(f"[TradeMonitor] Started watching {symbol}")

    # One-shot alert flags stored in memory (the trade dict on disk has persistent flags)
    ema20_break_alerted = False
    close_warn_alerted  = False

    while True:
        # Stop if externally cancelled
        with _active_lock:
            if not _active_threads.get(symbol):
                break

        time.sleep(60)

        # Skip outside extended hours
        now = datetime.now(TIMEZONE)
        if now.weekday() >= 5 or now.hour < 4 or now.hour >= 20:
            continue

        # Re-read trade from disk (user may have changed it)
        trades = load_trades()
        if symbol not in trades:
            print(f"[TradeMonitor] {symbol} removed from active_trades — stopping")
            break
        t = trades[symbol]

        data = _get_indicators(symbol)
        if not data:
            continue

        price     = data["price"]
        rsi       = data["rsi"]
        ema20     = data["ema20"]
        vol_ratio = data["vol_ratio"]

        entry      = float(t.get("entry_price", 0))
        stop       = float(t.get("stop_loss", 0))
        target     = float(t.get("price_target", 0))
        is_short   = t.get("signal", "") in ("SHORT", "STRONG SELL", "SELL")
        trade_type = t.get("trade_type", "SWING TRADE")
        new_high   = float(t.get("new_high", entry) or entry)
        low_vol_ct = int(t.get("low_vol_count", 0))
        rsi_above  = bool(t.get("rsi_was_above_60", False))

        pnl = round((price - entry) / entry * 100 * (-1 if is_short else 1), 2) if entry else 0

        # ── Stop loss ─────────────────────────────────────────────────────────
        if stop and ((is_short and price >= stop) or (not is_short and price <= stop)):
            _tg(
                f"🛑 *STOP HIT — EXIT NOW: {symbol}*\n"
                f"Price: ${price:.2f} | Stop was: ${stop:.2f}\n"
                f"P&L: {pnl:+.2f}%"
            )
            _log_closed_trade(t, price, "STOPPED")
            remove_trade(symbol)
            break

        # ── Target hit ────────────────────────────────────────────────────────
        if target and ((is_short and price <= target) or (not is_short and price >= target)):
            new_target = round(price * (0.95 if is_short else 1.05), 2)
            _tg(
                f"🎯 *TARGET HIT — consider taking profit: {symbol}*\n"
                f"Price: ${price:.2f} | Original target: ${target:.2f}\n"
                f"Raising target to ${new_target:.2f} | P&L: {pnl:+.2f}%"
            )
            update_trade_fields(symbol, {"price_target": new_target})

        # ── RSI fading: was above 60, now below 45 ────────────────────────────
        if rsi >= 60:
            update_trade_fields(symbol, {"rsi_was_above_60": True})
            rsi_above = True
        if rsi_above and rsi < 45:
            _tg(
                f"📉 *MOMENTUM FADING — consider exiting: {symbol}*\n"
                f"RSI dropped to {rsi} after being above 60\n"
                f"Price: ${price:.2f} | P&L: {pnl:+.2f}%"
            )
            break

        # ── Volume dried up: <30% of average for 3 consecutive checks ─────────
        if vol_ratio < 0.30:
            low_vol_ct += 1
            update_trade_fields(symbol, {"low_vol_count": low_vol_ct})
            if low_vol_ct >= 3:
                _tg(
                    f"📊 *VOLUME DRYING UP — move may be over: {symbol}*\n"
                    f"Volume at {vol_ratio:.0%} of average (3 consecutive minutes)\n"
                    f"Price: ${price:.2f} | P&L: {pnl:+.2f}%"
                )
                break
        else:
            if low_vol_ct:
                update_trade_fields(symbol, {"low_vol_count": 0})
            low_vol_ct = 0

        # ── Broke below 1-minute EMA20 (one-shot, longs only) ─────────────────
        if not is_short and not ema20_break_alerted and price < ema20 and entry and price > entry * 0.97:
            _tg(
                f"⚠️ *BROKE BELOW EMA20 — momentum shifting: {symbol}*\n"
                f"Price: ${price:.2f} | EMA20: ${ema20:.2f}\n"
                f"P&L: {pnl:+.2f}%"
            )
            ema20_break_alerted = True

        # ── New high (longs only, must exceed prev high by >0.5%) ─────────────
        if not is_short and new_high and price > new_high * 1.005:
            new_target = round(price * 1.03, 2)
            update_trade_fields(symbol, {"new_high": price, "price_target": new_target})
            _tg(
                f"🚀 *NEW HIGH — stay in: {symbol}*\n"
                f"Price: ${price:.2f} | Raising target to ${new_target:.2f}\n"
                f"P&L: {pnl:+.2f}%"
            )
            new_high = price

        # ── 30-min close warning for day trades (one-shot) ────────────────────
        if trade_type == "DAY TRADE" and not close_warn_alerted:
            mkt_close  = now.replace(hour=16, minute=0, second=0, microsecond=0)
            mins_left  = (mkt_close - now).total_seconds() / 60
            if 27 <= mins_left <= 33:
                _tg(
                    f"⏰ *MARKET CLOSES IN 30 MIN — exit your day trade now: {symbol}*\n"
                    f"Price: ${price:.2f} | P&L: {pnl:+.2f}%\n"
                    f"Day trades must be closed by 4:00 PM ET."
                )
                close_warn_alerted = True

    with _active_lock:
        _active_threads.pop(symbol, None)
    print(f"[TradeMonitor] Stopped watching {symbol}")


# ── Public API: active monitoring ────────────────────────────────────────────

def start_monitoring(symbol: str, trade: dict) -> None:
    """Kick off a background thread to actively monitor a confirmed trade."""
    with _active_lock:
        if symbol in _active_threads:
            return
        _active_threads[symbol] = True
    t = threading.Thread(target=_monitor_loop, args=(symbol,), daemon=True)
    t.start()


def stop_monitoring(symbol: str) -> None:
    with _active_lock:
        _active_threads[symbol] = False


def resume_monitoring_on_startup() -> None:
    """On service start, resume monitoring all trades saved in active_trades.json."""
    trades = load_trades()
    for sym, trade in trades.items():
        start_monitoring(sym, trade)
    if trades:
        print(f"[TradeMonitor] Resumed {len(trades)} active trade(s): {list(trades.keys())}")


# ── Passive tracking (alerted but not confirmed) ──────────────────────────────

def register_passive_track(result: dict) -> None:
    """Register an alerted stock for silent passive tracking."""
    sym = result["symbol"]
    with _passive_lock:
        if sym not in _passive_cache:
            _passive_cache[sym] = {
                "symbol":       sym,
                "alert_price":  result.get("price", 0),
                "stop_loss":    result.get("stop_loss", 0),
                "price_target": result.get("price_target", 0),
                "signal":       result.get("signal", ""),
                "hit_target":   False,
                "hit_stop":     False,
                "final_price":  result.get("price", 0),
            }


def _passive_update_loop() -> None:
    """Background thread: check all passive tracks every 5 minutes."""
    while True:
        time.sleep(300)
        now = datetime.now(TIMEZONE)
        if now.weekday() >= 5 or now.hour < 9 or now.hour >= 20:
            continue

        active_syms = set(load_trades().keys())

        with _passive_lock:
            syms = [s for s in _passive_cache if s not in active_syms]

        for sym in syms:
            try:
                info  = yf.Ticker(sym).fast_info
                price = float(getattr(info, "last_price", 0) or 0)
                if not price:
                    continue

                with _passive_lock:
                    if sym not in _passive_cache:
                        continue
                    t = _passive_cache[sym]
                    t["final_price"] = price
                    is_short = t.get("signal", "") in ("SHORT", "STRONG SELL", "SELL")
                    if is_short:
                        if t["stop_loss"]    and price >= t["stop_loss"]:
                            t["hit_stop"]   = True
                        if t["price_target"] and price <= t["price_target"]:
                            t["hit_target"] = True
                    else:
                        if t["stop_loss"]    and price <= t["stop_loss"]:
                            t["hit_stop"]   = True
                        if t["price_target"] and price >= t["price_target"]:
                            t["hit_target"] = True
            except:
                continue


def start_passive_tracker() -> None:
    t = threading.Thread(target=_passive_update_loop, daemon=True)
    t.start()


def load_passive_from_pending() -> None:
    """
    On startup, seed passive_cache from pending_alerts.json for any symbols
    NOT already in active_trades.json.  Survives service restarts.
    """
    pending = get_all_pending_alerts()
    active  = set(load_trades().keys())
    with _passive_lock:
        for sym, alert in pending.items():
            if sym not in active and sym not in _passive_cache:
                _passive_cache[sym] = {
                    "symbol":       sym,
                    "alert_price":  alert.get("price", 0),
                    "stop_loss":    alert.get("stop_loss", 0),
                    "price_target": alert.get("price_target", 0),
                    "signal":       alert.get("signal", ""),
                    "hit_target":   False,
                    "hit_stop":     False,
                    "final_price":  alert.get("price", 0),
                }
    if _passive_cache:
        print(f"[TradeMonitor] Loaded {len(_passive_cache)} passive track(s) from pending_alerts.json")


def send_eod_passive_summary() -> None:
    """Send one Telegram message with EOD results of all passive tracks."""
    active_syms = set(load_trades().keys())

    with _passive_lock:
        tracks = {s: v for s, v in _passive_cache.items() if s not in active_syms}

    if not tracks:
        return

    lines  = ["📋 *EOD — Unconfirmed Alert Results*\n(Stocks you did NOT confirm buying)"]
    hit_t  = 0
    hit_s  = 0
    open_  = 0
    for sym, t in sorted(tracks.items()):
        fp = t.get("final_price", 0)
        ap = t.get("alert_price", 0)
        ch = round((fp - ap) / ap * 100, 1) if ap else 0
        if t["hit_target"]:
            lines.append(f"  ✅ *{sym}* — HIT TARGET ${t['price_target']:.2f} | alert ${ap:.2f} → ${fp:.2f} ({ch:+.1f}%)")
            hit_t += 1
        elif t["hit_stop"]:
            lines.append(f"  ❌ *{sym}* — HIT STOP ${t['stop_loss']:.2f} | alert ${ap:.2f} → ${fp:.2f} ({ch:+.1f}%)")
            hit_s += 1
        else:
            lines.append(f"  ➡️ *{sym}* — Still open | alert ${ap:.2f} → ${fp:.2f} ({ch:+.1f}%)")
            open_ += 1

    lines.append(f"\n✅ Targets: {hit_t}  ❌ Stopped: {hit_s}  ➡️ Open: {open_}")
    _tg("\n".join(lines))

    # Reset for the next day
    with _passive_lock:
        _passive_cache.clear()
    clear_pending_alerts()

"""Telegram long-polling bot — handles trade confirmation and account commands."""
import re
import threading
import time
import requests
import yfinance as yf

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TIMEZONE
from active_trades import (
    save_trade, remove_trade, get_all_trades,
    get_pending_alert, save_pending_alert,
)
from account import get_summary, update_balance, add_position, close_position
from datetime import datetime

_last_update_id: int = 0

ALERT_HEADER_RE = re.compile(
    r"\*(?:NEWS PLAY|BREAKOUT|SWING TRADE|SHORT TRADE|DAY TRADE)[^:]*:\s*([A-Z]{1,5})\*",
    re.IGNORECASE,
)
PRICE_RE     = re.compile(r"(?:at|@)\s*\$?(\d+(?:\.\d+)?)", re.IGNORECASE)
FREEFORM_RE  = re.compile(
    r"(?:bought|in|entered|filled)\s+([A-Z]{1,5})(?:\s+(?:at|@)\s*\$?(\d+(?:\.\d+)?))?",
    re.IGNORECASE,
)
BUY_KEYWORDS = {"bought", "in", "filled", "entered", "long"}


def _send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"[TelegramBot] Send error: {e}")


def _current_price(symbol: str) -> float:
    try:
        info = yf.Ticker(symbol).fast_info
        return float(getattr(info, "last_price", 0) or 0)
    except Exception:
        return 0.0


def _extract_symbol_from_alert(text: str) -> str | None:
    m = ALERT_HEADER_RE.search(text or "")
    return m.group(1).upper() if m else None


def _extract_price(text: str) -> float | None:
    m = PRICE_RE.search(text or "")
    return float(m.group(1)) if m else None


def _handle_status() -> None:
    trades = get_all_trades()
    if not trades:
        _send(
            "📊 *No active trades right now.*\n\n"
            "Reply *BOUGHT* or *IN* to any alert to register a trade."
        )
        return
    lines = ["📊 *ACTIVE TRADES*\n━━━━━━━━━━━━━━━━"]
    for sym, t in trades.items():
        price    = _current_price(sym)
        entry    = float(t.get("entry_price", 0))
        stop     = float(t.get("stop_loss", 0))
        target   = float(t.get("price_target", 0))
        is_short = t.get("signal", "") in ("SHORT", "STRONG SELL", "SELL")
        pnl      = round((price - entry) / entry * 100 * (-1 if is_short else 1), 2) if entry and price else 0
        emoji    = "🟢" if pnl >= 0 else "🔴"
        lines.append(
            f"{emoji} *{sym}* ({t.get('trade_type','?')})\n"
            f"   Entry: ${entry:.2f} | Now: ${price:.2f} | P&L: {pnl:+.2f}%\n"
            f"   Stop: ${stop:.2f} | Target: ${target:.2f}"
        )
    _send("\n".join(lines))


def _handle_portfolio() -> None:
    summary = get_summary()
    trades  = get_all_trades()

    lines = [
        f"💼 *PORTFOLIO SNAPSHOT*\n━━━━━━━━━━━━━━━━",
        f"Balance: ${summary['balance']:,.2f}",
        f"Deployed: ${summary['deployed']:,.2f} | Available: ${summary['available']:,.2f}",
        f"Total P&L: ${summary['total_pnl']:+,.2f} ({summary['pnl_pct']:+.1f}%)",
        f"Open positions: {summary['open_count']}/3",
    ]

    if trades:
        lines.append("━━━━━━━━━━━━━━━━")
        for sym, t in trades.items():
            price    = _current_price(sym)
            entry    = float(t.get("entry_price", 0))
            is_short = t.get("signal", "") in ("SHORT", "STRONG SELL", "SELL")
            pnl      = round((price - entry) / entry * 100 * (-1 if is_short else 1), 2) if entry and price else 0
            emoji    = "🟢" if pnl >= 0 else "🔴"
            lines.append(f"{emoji} *{sym}*: ${entry:.2f} → ${price:.2f} ({pnl:+.2f}%)")

    _send("\n".join(lines))


def _handle_balance_command(raw: str) -> None:
    parts = raw.strip().split()
    if len(parts) >= 2:
        try:
            new_bal = float(parts[1].replace("$", "").replace(",", ""))
            update_balance(new_bal)
            summary = get_summary()
            _send(
                f"✅ *Balance updated to ${new_bal:,.2f}*\n"
                f"Total P&L vs start: ${summary['total_pnl']:+,.2f} ({summary['pnl_pct']:+.1f}%)"
            )
            return
        except ValueError:
            pass

    summary = get_summary()
    _send(
        f"💰 *Account Balance*\n"
        f"Current: ${summary['balance']:,.2f}\n"
        f"Starting: ${summary['starting']:,.2f}\n"
        f"Total P&L: ${summary['total_pnl']:+,.2f} ({summary['pnl_pct']:+.1f}%)\n"
        f"Deployed: ${summary['deployed']:,.2f} | Available: ${summary['available']:,.2f}\n"
        f"\n_To update: reply_ `balance 13500`"
    )


def _handle_close(symbol: str) -> None:
    trades = get_all_trades()
    if symbol not in trades:
        _send(f"⚠️ *{symbol}* is not in your active trades.")
        return
    t        = trades[symbol]
    price    = _current_price(symbol)
    entry    = float(t.get("entry_price", 0))
    is_short = t.get("signal", "") in ("SHORT", "STRONG SELL", "SELL")
    pnl      = round((price - entry) / entry * 100 * (-1 if is_short else 1), 2) if entry and price else 0

    from trade_follower import _log_closed_trade, stop_monitoring
    _log_closed_trade(t, price, "MANUAL_CLOSE")
    stop_monitoring(symbol)
    remove_trade(symbol)
    pnl_dollar = close_position(symbol, price, is_short)

    _send(
        f"✅ *{symbol} closed manually*\n"
        f"Exit: ${price:.2f} | Entry: ${entry:.2f} | P&L: {pnl:+.2f}%\n"
        f"Dollar P&L: ${pnl_dollar:+.2f} | Logged to trades.csv"
    )


def _handle_buy_confirmation(symbol: str, entry_price: float | None, raw: str) -> None:
    pending = get_pending_alert(symbol)
    if not pending:
        _send(
            f"⚠️ No recent alert found for *{symbol}*.\n"
            "Reply to the original alert message, or say:\n"
            "`bought AAPL at 195`"
        )
        return

    if not entry_price:
        entry_price = _extract_price(raw) or _current_price(symbol) or float(pending.get("price", 0))

    from account import can_open_trade
    from trade_follower import start_monitoring

    conviction = pending.get("conviction", "SPECULATIVE")
    trade = {
        "symbol":           symbol,
        "entry_price":      round(entry_price, 2),
        "stop_loss":        pending.get("stop_loss", 0),
        "price_target":     pending.get("price_target", 0),
        "signal":           pending.get("signal", "STRONG BUY"),
        "trade_type":       pending.get("trade_type", "SWING TRADE"),
        "scanner":          pending.get("scanner", ""),
        "conviction":       conviction,
        "alert_price":      pending.get("price", entry_price),
        "confirmed_at":     datetime.now(TIMEZONE).isoformat(),
        "rsi_was_above_60": False,
        "new_high":         entry_price,
        "low_vol_count":    0,
        "entry_time":       datetime.now(TIMEZONE).isoformat(),
    }
    save_trade(trade)

    pos_size = pending.get("position_size", 0)
    if not pos_size:
        from account import get_position_size
        pos_size = get_position_size(conviction)
    add_position(symbol, pos_size, entry_price, pending.get("trade_type", "SWING"))

    start_monitoring(symbol, trade)

    stop  = pending.get("stop_loss", 0)
    tgt   = pending.get("price_target", 0)
    risk  = round(abs(entry_price - float(stop)) * (pos_size / entry_price), 2) if entry_price else 0

    _send(
        f"✅ *Trade Confirmed: {symbol}*\n"
        f"Entry: ${entry_price:.2f} | Stop: ${stop:.2f} | Target: ${tgt:.2f}\n"
        f"Position: ${pos_size:,.0f} | Risk: ${risk:.2f}\n"
        f"Monitoring every 60s via 5-min candles. Send *status* for P&L."
    )


def _handle_update(update: dict) -> None:
    global _last_update_id
    uid = update.get("update_id", 0)
    if uid <= _last_update_id:
        return
    _last_update_id = uid

    msg = update.get("message", {})
    if not msg:
        return

    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != str(TELEGRAM_CHAT_ID):
        return

    raw   = msg.get("text", "").strip()
    if not raw:
        return
    lower = raw.lower()

    if lower == "status":
        _handle_status()
        return

    if lower == "portfolio":
        _handle_portfolio()
        return

    if lower.startswith("balance"):
        _handle_balance_command(raw)
        return

    close_m = re.match(r"^close\s+([A-Z]{1,5})$", raw.strip(), re.IGNORECASE)
    if close_m:
        _handle_close(close_m.group(1).upper())
        return

    words  = set(lower.split())
    is_buy = bool(words & BUY_KEYWORDS)
    if not is_buy:
        return

    reply_to = msg.get("reply_to_message", {})
    if reply_to:
        symbol = _extract_symbol_from_alert(reply_to.get("text", ""))
        if symbol:
            price = _extract_price(raw)
            _handle_buy_confirmation(symbol, price, raw)
            return

    fm = FREEFORM_RE.search(raw)
    if fm:
        symbol = fm.group(1).upper()
        price  = float(fm.group(2)) if fm.group(2) else None
        _handle_buy_confirmation(symbol, price, raw)
        return

    _send(
        "⚠️ Please reply directly to the alert message, or say:\n"
        "`bought AAPL` or `in AAPL at 195.50`"
    )


def _polling_loop() -> None:
    global _last_update_id
    print("[TelegramBot] Starting long-poll loop...")
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params={"timeout": 0, "limit": 1, "offset": -1},
            timeout=10,
        )
        results = r.json().get("result", [])
        if results:
            _last_update_id = results[-1]["update_id"]
            print(f"[TelegramBot] Skipping to update #{_last_update_id}")
    except Exception:
        pass

    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={
                    "timeout":         30,
                    "limit":           20,
                    "offset":          _last_update_id + 1,
                    "allowed_updates": ["message"],
                },
                timeout=40,
            )
            for upd in r.json().get("result", []):
                try:
                    _handle_update(upd)
                except Exception as e:
                    print(f"[TelegramBot] Handler error: {e}")
        except Exception as e:
            print(f"[TelegramBot] Poll error: {e}")
            time.sleep(5)


def start_bot() -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TelegramBot] No token/chat_id — bot disabled.")
        return
    t = threading.Thread(target=_polling_loop, daemon=True, name="TelegramBotPoller")
    t.start()
    print("[TelegramBot] Bot started.")

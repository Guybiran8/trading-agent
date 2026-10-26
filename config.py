import os
import pytz
from dotenv import load_dotenv

load_dotenv()

POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY")
if not POLYGON_API_KEY:
    raise RuntimeError(
        "POLYGON_API_KEY is not set. Add it to /root/trading-agent/.env — "
        "there is no hardcoded fallback."
    )
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

TIMEZONE = pytz.timezone("US/Eastern")

# Per-scanner Telegram trade alerts. False = scanner still runs and logs to
# tracking.csv, but sends no Telegram message. Unlisted scanners default to True.
TELEGRAM_ALERTS_ENABLED = {
    "SWING":            False,
    "DAY_BREAKOUT":     False,
    "SHORT":            False,
    "VOLUME_AWAKENING": False,
    "OPEN_SCAN":        False,
    "OPEN_DRIVE":       False,
    "DAY_NEWS":         False,
    "AFTERHOURS":       False,
    "PREMARKET":        False,
}

# Scanners whose results are dropped before alerting (code kept, not deleted).
# Unlisted scanners default to True. Off since 2026-10-26: worst live results
# (10-day excess vs random liquid stocks: DAY_NEWS -10.2%, OPEN_SCAN -2.8%).
SCANNERS_ENABLED = {
    "DAY_NEWS":  False,
    "OPEN_SCAN": False,
}

# ── Account ───────────────────────────────────────────────────────────────────
STARTING_BALANCE         = 13424.0
HIGH_CONVICTION_SIZE     = 1000.0
SPECULATIVE_SIZE         = 500.0
MAX_OPEN_TRADES          = 3
MAX_DEPLOYED             = 3000.0
BALANCE_THRESHOLD_MEDIUM = 10000.0
BALANCE_THRESHOLD_LOW    = 8000.0
MAX_TRADE_MEDIUM         = 750.0
MAX_TRADE_LOW            = 500.0
DAILY_LOSS_LIMIT         = 500.0

# ── Scan intervals (seconds) ──────────────────────────────────────────────────
NEWS_SCAN_INTERVAL    = 60
DAY_SCAN_INTERVAL     = 60
SWING_SCAN_INTERVAL   = 1800
PREMARKET_SCAN_INTERVAL = 900

# ── Quality filters ───────────────────────────────────────────────────────────
MIN_DAY_SCORE     = 80
MIN_SWING_SCORE   = 75
MAX_ALERTS_PER_SCAN = 3
MIN_GAIN_PCT      = 5.0

# ── Market regime thresholds ──────────────────────────────────────────────────
VIX_BULL_MAX = 20.0
VIX_BEAR_MIN = 25.0

# ── Sector ETFs ───────────────────────────────────────────────────────────────
SECTOR_ETFS = {
    "Technology":            "XLK",
    "Financial Services":    "XLF",
    "Healthcare":            "XLV",
    "Energy":                "XLE",
    "Industrials":           "XLI",
    "Consumer Cyclical":     "XLY",
    "Communication Services": "XLC",
    "Consumer Defensive":    "XLP",
    "Utilities":             "XLU",
    "Real Estate":           "XLRE",
    "Basic Materials":       "XLB",
}

# ── File paths ────────────────────────────────────────────────────────────────
BASE_DIR            = "/root/trading-agent"
ACCOUNT_FILE        = f"{BASE_DIR}/account.json"
ACTIVE_TRADES_FILE  = f"{BASE_DIR}/active_trades.json"
PENDING_ALERTS_FILE = f"{BASE_DIR}/pending_alerts.json"
TRADES_CSV          = f"{BASE_DIR}/trades.csv"
TRACKING_CSV        = f"{BASE_DIR}/tracking.csv"
IMPROVEMENTS_FILE   = f"{BASE_DIR}/improvements.json"
RECOMMENDATIONS_FILE = f"{BASE_DIR}/self_improvement_recommendations.json"
AGENT_LOG           = f"{BASE_DIR}/agent.log"

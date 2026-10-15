# Trading Agent

[WRITE THIS YOURSELF: what this is and why I built it]

## Setup

### Requirements

- Python 3.12
- A Polygon.io API key (free tier works, 5 requests/min)
- A Telegram bot token and chat ID (optional, for alerts)

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### .env

Create a `.env` file in the project root:

```
POLYGON_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

### Run

Live agent (scheduler, scanners, Telegram bot):

```bash
venv/bin/python main.py
```

Backtester (daily bars, in `backtest/`):

```bash
# 1. Download ~2 years of daily bars into backtest/data/daily.sqlite (~570 MB, hours on the free tier)
venv/bin/python backtest/download.py

# 2. Run the engine tests
cd backtest && ../venv/bin/python test_engine.py

# 3. Run strategies (--is-only = in-sample only)
../venv/bin/python report.py --is-only [strategy names...]
```

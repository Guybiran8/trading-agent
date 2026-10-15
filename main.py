import warnings
import logging

warnings.filterwarnings("ignore")
logging.getLogger().setLevel(logging.ERROR)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.CRITICAL)

from datetime import datetime
from config import TIMEZONE
from scheduler import run_auto_scheduler, get_current_session


def main():
    print("Trading Agent — Rebuilt for Big Moves")
    print(f"Time: {datetime.now(TIMEZONE).strftime('%Y-%m-%d %I:%M %p ET')}")
    print(f"Session: {get_current_session()}")
    print()
    run_auto_scheduler()


if __name__ == "__main__":
    main()

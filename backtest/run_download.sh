#!/bin/bash
# Run the grouped-daily download, then resume the paused path-outcome backfill
# (it skips rows already done). Resumes even if the download fails, so the
# backfill is never left paused indefinitely; rerun this script to finish a
# failed download.
cd /root/trading-agent
venv/bin/python -u backtest/download.py >> backtest/download.log 2>&1
echo "[$(date -Is)] download exited with $?; resuming path backfill" >> backtest/download.log
exec venv/bin/python -u one_time_backfill_path_outcomes.py >> backfill_path_outcomes_v2.log 2>&1

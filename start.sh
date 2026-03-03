#!/bin/bash
# Start both the Telegram bot and the monitor together
set -e

echo "Starting BMS Monitor + Bot..."

# Start monitor in background
python monitor.py &
MONITOR_PID=$!
echo "monitor.py started (PID $MONITOR_PID)"

# Start bot in foreground (keeps container alive)
python bot.py
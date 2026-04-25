@echo off
set BROKER_MODE=demo
python -m tradebot.cli trade-once
python -m tradebot.cli dashboard

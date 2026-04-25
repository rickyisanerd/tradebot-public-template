#!/usr/bin/env bash
set -euo pipefail
export BROKER_MODE=demo
python -m tradebot.cli trade-once
python -m tradebot.cli dashboard

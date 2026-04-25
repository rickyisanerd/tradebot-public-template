#!/usr/bin/env bash
set -euo pipefail
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp -n .env.example .env || true
printf "\nReady. Run: source .venv/bin/activate && python run_tests.py\n"

#!/usr/bin/env bash
set -euo pipefail
if [[ -x ".venv/bin/python" ]]; then
  .venv/bin/python run_tests.py
else
  python run_tests.py
fi

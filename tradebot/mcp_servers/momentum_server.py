from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tradebot.analytics import analyze_momentum


def main() -> int:
    metrics = json.loads(sys.stdin.read() or "{}")
    score, reasons = analyze_momentum(metrics)
    sys.stdout.write(json.dumps({"score": score, "reasons": reasons}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import uvicorn

from .config import get_settings
from .db import Database
from .engine import TradingEngine
from .providers import build_broker


def build_engine() -> TradingEngine:
    settings = get_settings()
    db = Database(settings.db_path)
    broker = build_broker(settings)
    return TradingEngine(settings=settings, broker=broker, db=db)


def export_brain(db: Database, out_path: str) -> dict:
    """Export learning weights from the database to a JSON file."""
    weights = db.learning_weights()
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "learning": {},
    }
    for strategy, row in weights.items():
        payload["learning"][strategy] = {
            "wins": row["wins"],
            "losses": row["losses"],
            "total_return": row["total_return"],
            "weight": row["weight"],
        }
    dest = Path(out_path)
    dest.write_text(json.dumps(payload, indent=2))
    return {"exported": len(payload["learning"]), "file": str(dest.resolve())}


def import_brain(db: Database, in_path: str) -> dict:
    """Import learning weights from a JSON file into the database."""
    src = Path(in_path)
    if not src.exists():
        return {"error": f"File not found: {src}"}
    data = json.loads(src.read_text())
    learning = data.get("learning", {})
    now = datetime.now(timezone.utc).isoformat()
    imported = 0
    with db.connect() as con:
        for strategy, values in learning.items():
            con.execute(
                """
                INSERT INTO learning(strategy, wins, losses, total_return, weight, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy) DO UPDATE SET
                    wins=excluded.wins,
                    losses=excluded.losses,
                    total_return=excluded.total_return,
                    weight=excluded.weight,
                    updated_at=excluded.updated_at
                """,
                (
                    strategy,
                    values.get("wins", 0),
                    values.get("losses", 0),
                    values.get("total_return", 0.0),
                    values.get("weight", 1.0),
                    now,
                ),
            )
            imported += 1
    return {
        "imported": imported,
        "source": str(src.resolve()),
        "exported_at": data.get("exported_at", "unknown"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="TradeBot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="Run a single market scan")
    sub.add_parser("trade-once", help="Advance the market one step, manage open positions, and buy new candidates")
    sub.add_parser("refresh-signals", help="Refresh all cached external decision-support signals")
    sub.add_parser("refresh-congress", help="Refresh cached congressional PTR trades from configured official report URLs")
    sub.add_parser("refresh-sec", help="Refresh cached SEC filing signals for the current scan universe")
    sub.add_parser("refresh-earnings", help="Refresh cached earnings events for the current scan universe")
    sub.add_parser("refresh-macro", help="Refresh cached FOMC calendar events")
    sub.add_parser("status", help="Print dashboard snapshot as JSON")
    sub.add_parser("dashboard", help="Run the FastAPI dashboard")

    export_parser = sub.add_parser("export-brain", help="Export learning weights to a JSON file")
    export_parser.add_argument("--out", default="brain.json", help="Output file path (default: brain.json)")

    import_parser = sub.add_parser("import-brain", help="Import learning weights from a JSON file")
    import_parser.add_argument("--file", default="brain.json", help="Input file path (default: brain.json)")

    args = parser.parse_args()

    engine = build_engine()
    settings = engine.settings

    if args.command == "scan":
        candidates = [c.model_dump() for c in engine.scan_market()]
        print(json.dumps(candidates, indent=2))
        return 0
    if args.command == "trade-once":
        result = engine.trade_once()
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "refresh-signals":
        result = engine.refresh_all_signals()
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "refresh-congress":
        result = engine.refresh_congress_trades()
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "refresh-sec":
        result = engine.refresh_sec_filings()
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "refresh-earnings":
        result = engine.refresh_earnings_events()
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "refresh-macro":
        result = engine.refresh_macro_events()
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "status":
        print(json.dumps(engine.dashboard_snapshot(), indent=2))
        return 0
    if args.command == "dashboard":
        uvicorn.run("tradebot.dashboard:app", host=settings.dashboard_host, port=settings.dashboard_port, reload=False)
        return 0
    if args.command == "export-brain":
        result = export_brain(engine.db, args.out)
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "import-brain":
        result = import_brain(engine.db, args.file)
        print(json.dumps(result, indent=2))
        return 1 if "error" in result else 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

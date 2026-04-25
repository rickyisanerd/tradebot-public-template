from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

from .analytics import analyze_decision_support, analyze_momentum, analyze_reversion, analyze_risk

_SERVER_MAP = {
    "decision_support": "decision_support_server.py",
    "momentum": "momentum_server.py",
    "reversion": "reversion_server.py",
    "risk": "risk_server.py",
}


def _server_path(server_name: str) -> Path:
    return Path(__file__).parent / "mcp_servers" / _SERVER_MAP[server_name]


def _run_server(server_name: str, metrics: Dict[str, float]) -> Tuple[float, List[str]]:
    proc = subprocess.run(
        [sys.executable, str(_server_path(server_name))],
        input=json.dumps(metrics),
        text=True,
        capture_output=True,
        timeout=10,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{server_name} server failed: {proc.stderr.strip()}")
    payload = json.loads(proc.stdout)
    return float(payload["score"]), list(payload.get("reasons", []))


def analyze(metrics: Dict[str, float], mode: str = "embedded") -> Dict[str, Tuple[float, List[str]]]:
    if mode == "subprocess":
        return {
            "decision_support": _run_server("decision_support", metrics),
            "momentum": _run_server("momentum", metrics),
            "reversion": _run_server("reversion", metrics),
            "risk": _run_server("risk", metrics),
        }
    return {
        "decision_support": analyze_decision_support(metrics),
        "momentum": analyze_momentum(metrics),
        "reversion": analyze_reversion(metrics),
        "risk": analyze_risk(metrics),
    }

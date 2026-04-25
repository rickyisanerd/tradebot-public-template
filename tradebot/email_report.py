"""Daily email report sent at market close via Resend API."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

import requests as http_requests

log = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _dashboard_url() -> str:
    return _env("REPORT_DASHBOARD_URL", "http://127.0.0.1:8008/")


def email_configured() -> bool:
    return bool(_env("RESEND_API_KEY") and _env("REPORT_EMAIL"))


def _daily_and_total_summary(snapshot: Dict[str, Any]) -> Dict[str, float]:
    account = snapshot.get("account", {}) or {}
    performance = snapshot.get("performance", {}) or {}
    safety = snapshot.get("safety_status", {}) or {}

    equity = float(account.get("equity", 0) or 0)
    daily_anchor = float(safety.get("daily_equity_anchor", equity) or equity)
    daily_pnl = equity - daily_anchor
    daily_pct = (daily_pnl / daily_anchor * 100) if daily_anchor > 0 else 0.0

    total_pnl = float(performance.get("total_pnl", 0) or 0)
    total_pct = float(performance.get("total_return_pct", 0) or 0)

    return {
        "daily_pnl": round(daily_pnl, 2),
        "daily_pct": round(daily_pct, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pct": round(total_pct, 2),
    }


def build_report_html(snapshot: Dict[str, Any]) -> str:
    summary = _daily_and_total_summary(snapshot)
    today_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    dashboard_url = _dashboard_url()

    daily_color = "#22c55e" if summary["daily_pnl"] >= 0 else "#ef4444"
    total_color = "#22c55e" if summary["total_pnl"] >= 0 else "#ef4444"
    daily_arrow = "▲" if summary["daily_pnl"] >= 0 else "▼"
    total_arrow = "▲" if summary["total_pnl"] >= 0 else "▼"

    return f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;max-width:640px;margin:0 auto;background:#1a1a2e;color:#e0e0e0;border-radius:12px;overflow:hidden">
        <div style="background:linear-gradient(135deg,#0f0f23,#1a1a3e);padding:24px 28px;border-bottom:2px solid #333">
            <h1 style="margin:0;font-size:22px;color:#fff">TradeBot Daily Report</h1>
            <p style="margin:4px 0 0;color:#888;font-size:14px">{today_str}</p>
        </div>

        <div style="padding:24px 28px;border-bottom:1px solid #333">
            <div style="background:#0f0f23;border:1px solid #333;border-radius:12px;padding:20px 18px;margin-bottom:16px">
                <p style="margin:0;color:#888;font-size:12px;text-transform:uppercase;letter-spacing:1px">Daily Gain / Loss</p>
                <p style="margin:6px 0 0;font-size:30px;font-weight:bold;color:{daily_color}">{daily_arrow} ${summary['daily_pnl']:+.2f}</p>
                <p style="margin:4px 0 0;color:{daily_color};font-size:15px">({summary['daily_pct']:+.2f}%)</p>
            </div>

            <div style="background:#0f0f23;border:1px solid #333;border-radius:12px;padding:20px 18px">
                <p style="margin:0;color:#888;font-size:12px;text-transform:uppercase;letter-spacing:1px">Total Gain / Loss</p>
                <p style="margin:6px 0 0;font-size:30px;font-weight:bold;color:{total_color}">{total_arrow} ${summary['total_pnl']:+.2f}</p>
                <p style="margin:4px 0 0;color:{total_color};font-size:15px">({summary['total_pct']:+.2f}%)</p>
            </div>
        </div>

        <div style="padding:20px 28px;border-bottom:1px solid #333">
            <p style="margin:0;color:#888;font-size:13px;line-height:1.5">
                Daily gain or loss is based on current equity versus the account's start-of-day equity anchor.
                Total gain or loss is the bot's tracked overall P&amp;L from realized and unrealized performance.
            </p>
        </div>

        <div style="padding:16px 28px;text-align:center;color:#555;font-size:11px">
            TradeBot • Automated Daily Report • <a href="{dashboard_url}" style="color:#6366f1">Dashboard</a>
        </div>
    </div>
    """


def send_daily_report(snapshot: Dict[str, Any]) -> bool:
    recipient = _env("REPORT_EMAIL")
    sender = _env("REPORT_SENDER_EMAIL", "TradeBot <reports@example.com>")
    api_key = _env("RESEND_API_KEY")
    dashboard_url = _dashboard_url()

    if not api_key or not recipient:
        log.warning("Email reporting is not fully configured - skipping daily email report")
        return False

    summary = _daily_and_total_summary(snapshot)
    html = build_report_html(snapshot)
    today_str = datetime.now(timezone.utc).strftime("%m/%d/%Y")
    subject = (
        f"TradeBot {today_str}: Daily ${summary['daily_pnl']:+.2f} ({summary['daily_pct']:+.2f}%) | "
        f"Total ${summary['total_pnl']:+.2f} ({summary['total_pct']:+.2f}%)"
    )
    plain = (
        f"TradeBot Daily Report - {today_str}\n"
        f"Daily Gain / Loss: ${summary['daily_pnl']:+.2f} ({summary['daily_pct']:+.2f}%)\n"
        f"Total Gain / Loss: ${summary['total_pnl']:+.2f} ({summary['total_pct']:+.2f}%)\n"
        f"View dashboard: {dashboard_url}"
    )

    try:
        resp = http_requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": sender,
                "to": [recipient],
                "subject": subject,
                "html": html,
                "text": plain,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            log.info("Daily report emailed to %s", recipient)
            return True
        log.error("Resend API error %s: %s", resp.status_code, resp.text)
        return False
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to send daily report via Resend: %s: %s", type(exc).__name__, exc)
        return False

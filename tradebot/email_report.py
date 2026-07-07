"""Daily email report sent at market close via Resend API."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from html import escape
from typing import Any, Dict, Iterable

import requests as http_requests

from .etrade import ETradeClient

log = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _dashboard_url() -> str:
    return _env("REPORT_DASHBOARD_URL", "http://127.0.0.1:8008/")


def email_configured() -> bool:
    return bool(_env("RESEND_API_KEY") and _env("REPORT_EMAIL"))


def _send_resend_email(subject: str, html: str, text: str, *, log_label: str) -> bool:
    recipient = _env("REPORT_EMAIL")
    sender = _env("REPORT_SENDER_EMAIL", "TradeBot <reports@example.com>")
    api_key = _env("RESEND_API_KEY")

    if not api_key or not recipient:
        log.warning("RESEND_API_KEY / REPORT_EMAIL not set - skipping %s email", log_label)
        return False

    try:
        log.info("Sending %s email via Resend API to %s...", log_label, recipient)
        resp = http_requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": sender,
                "to": [recipient],
                "subject": subject,
                "html": html,
                "text": text,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            log.info("%s email sent to %s via Resend (id=%s)", log_label.title(), recipient, resp.json().get("id"))
            return True
        log.error("Resend API error %s while sending %s email: %s", resp.status_code, log_label, resp.text)
        return False
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to send %s email via Resend: %s: %s", log_label, type(exc).__name__, exc)
        return False


def send_failure_alert(title: str, message: str, details: Dict[str, Any] | None = None) -> bool:
    """Send an immediate failure alert using the daily report email settings."""
    when = datetime.now(timezone.utc).strftime("%B %d, %Y %H:%M:%S UTC")
    details = details or {}
    detail_rows = "".join(
        f"<tr><td style=\"padding:6px 8px;color:#aaa;border-bottom:1px solid #333\">{escape(str(key))}</td>"
        f"<td style=\"padding:6px 8px;color:#eee;border-bottom:1px solid #333\"><code>{escape(str(value))}</code></td></tr>"
        for key, value in details.items()
    )
    if not detail_rows:
        detail_rows = '<tr><td colspan="2" style="padding:8px;color:#888;text-align:center">No additional details</td></tr>'
    html = f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;max-width:680px;margin:0 auto;background:#1a1a2e;color:#e0e0e0;border-radius:12px;overflow:hidden">
        <div style="background:#3b1111;padding:22px 26px;border-bottom:2px solid #7f1d1d">
            <h1 style="margin:0;font-size:22px;color:#fff">TradeBot Alert</h1>
            <p style="margin:6px 0 0;color:#fecaca;font-size:14px">{escape(when)}</p>
        </div>
        <div style="padding:22px 26px;border-bottom:1px solid #333">
            <h2 style="margin:0 0 12px;font-size:18px;color:#fff">{escape(title)}</h2>
            <p style="margin:0;color:#fca5a5;line-height:1.5;white-space:pre-wrap">{escape(message)}</p>
        </div>
        <div style="padding:20px 26px">
            <table style="width:100%;border-collapse:collapse;font-size:13px">{detail_rows}</table>
        </div>
    </div>
    """
    text_lines = [f"TradeBot Alert - {when}", title, message]
    if details:
        text_lines.append("")
        text_lines.extend(f"{key}: {value}" for key, value in details.items())
    return _send_resend_email(f"TradeBot ALERT: {title}", html, "\n".join(text_lines), log_label="failure alert")


def _daily_and_total_summary(snapshot: Dict[str, Any]) -> Dict[str, float | str]:
    account = snapshot.get("account", {}) or {}
    performance = snapshot.get("performance", {}) or {}
    safety = snapshot.get("safety_status", {}) or {}

    equity = float(account.get("equity", 0) or 0)
    broker_previous_close = _safe_float(account.get("last_equity"))
    safety_anchor = _safe_float(safety.get("daily_equity_anchor"), default=equity)
    daily_anchor = broker_previous_close if broker_previous_close > 0 else safety_anchor
    daily_pnl = equity - daily_anchor
    daily_pct = (daily_pnl / daily_anchor * 100) if daily_anchor > 0 else 0.0

    total_pnl = float(performance.get("total_pnl", 0) or 0)
    total_pct = float(performance.get("total_return_pct", 0) or 0)
    unrealized_pnl = float(performance.get("unrealized_pnl", 0) or 0)

    return {
        "label": "TradeBot / Alpaca",
        "equity": round(equity, 2),
        "daily_anchor": round(daily_anchor, 2),
        "daily_anchor_source": "broker_previous_close" if broker_previous_close > 0 else "local_start_of_day",
        "daily_pnl": round(daily_pnl, 2),
        "daily_pct": round(daily_pct, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pct": round(total_pct, 2),
    }


def _walk_nodes(node: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_nodes(item)


def _first_numeric(payload: Any, *keys: str) -> float | None:
    for node in _walk_nodes(payload):
        for key in keys:
            if key in node:
                value = _safe_float(node.get(key), default=float("nan"))
                if value == value:
                    return value
    return None


def _extract_etrade_position_rows(payload: Dict[str, Any]) -> list[Dict[str, float | str]]:
    rows: list[Dict[str, float | str]] = []
    seen: set[tuple[str, float]] = set()

    def maybe_symbol(node: Dict[str, Any]) -> str:
        product = node.get("Product")
        if isinstance(product, dict):
            value = product.get("symbol")
            if isinstance(value, str) and value.strip():
                return value.strip().upper()
        value = node.get("symbol")
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
        return ""

    for node in _walk_nodes(payload):
        symbol = maybe_symbol(node)
        quantity = _safe_float(node.get("quantity"))
        market_value = _safe_float(node.get("marketValue"))
        if not symbol or quantity <= 0 or market_value <= 0:
            continue
        row = {
            "symbol": symbol,
            "quantity": quantity,
            "market_value": market_value,
            "day_gain": _safe_float(
                node.get("todayGainLoss")
                or node.get("todayGain")
                or node.get("dayGain")
                or node.get("daysGain")
            ),
            "total_gain": _safe_float(
                node.get("totalGain")
                or node.get("gain")
                or node.get("totalGainLoss")
            ),
        }
        key = (symbol, round(quantity, 8))
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def get_etrade_report_summary() -> Dict[str, float | str] | None:
    account_id_key = _env("ETRADE_REPORT_ACCOUNT_ID_KEY") or _env("ETRADE_ACCOUNT_ID_KEY")
    env_name = _env("ETRADE_REPORT_ENV") or _env("ETRADE_MIRROR_ENV", "live")
    if not account_id_key:
        return None
    try:
        client = ETradeClient(env_name)
        balance_payload = client.balance(account_id_key)
        positions_payload = client.positions(account_id_key)
        positions = _extract_etrade_position_rows(positions_payload)
        equity = _first_numeric(balance_payload, "totalAccountValue", "accountValue", "netAccountValue")
        cash = _first_numeric(
            balance_payload,
            "cashAvailableForInvestment",
            "cashBuyingPower",
            "cashAvailableForWithdrawal",
            "settledCashForInvestment",
        )
        daily_pnl = round(sum(_safe_float(row.get("day_gain")) for row in positions), 2)
        total_pnl = round(sum(_safe_float(row.get("total_gain")) for row in positions), 2)
        daily_pct = (daily_pnl / (equity - daily_pnl) * 100) if equity and (equity - daily_pnl) > 0 else 0.0
        total_pct = (total_pnl / (equity - total_pnl) * 100) if equity and (equity - total_pnl) > 0 else 0.0
        return {
            "label": "E*TRADE",
            "equity": round(_safe_float(equity), 2),
            "cash": round(_safe_float(cash), 2),
            "daily_pnl": round(daily_pnl, 2),
            "daily_pct": round(daily_pct, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pct": round(total_pct, 2),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("Unable to load E*TRADE report summary: %s", exc)
        return None


def _metric_card(title: str, amount: float, pct: float) -> str:
    color = "#22c55e" if amount >= 0 else "#ef4444"
    arrow = "▲" if amount >= 0 else "▼"
    return (
        f"<div style=\"background:#0f0f23;border:1px solid #333;border-radius:12px;padding:20px 18px;margin-bottom:16px\">"
        f"<p style=\"margin:0;color:#888;font-size:12px;text-transform:uppercase;letter-spacing:1px\">{title}</p>"
        f"<p style=\"margin:6px 0 0;font-size:30px;font-weight:bold;color:{color}\">{arrow} ${amount:+.2f}</p>"
        f"<p style=\"margin:4px 0 0;color:{color};font-size:15px\">({pct:+.2f}%)</p>"
        f"</div>"
    )


def _comparison_table(primary: Dict[str, float | str], secondary: Dict[str, float | str] | None) -> str:
    rows = [primary]
    if secondary:
        rows.append(secondary)
    body = "".join(
        (
            "<tr>"
            f"<td>{row['label']}</td>"
            f"<td>${_safe_float(row.get('equity')):.2f}</td>"
            f"<td>${_safe_float(row.get('daily_pnl')):+.2f} ({_safe_float(row.get('daily_pct')):+.2f}%)</td>"
            f"<td>${_safe_float(row.get('total_pnl')):+.2f} ({_safe_float(row.get('total_pct')):+.2f}%)</td>"
            "</tr>"
        )
        for row in rows
    )
    return f"""
        <div style="padding:24px 28px;border-bottom:1px solid #333">
            <h2 style="margin:0 0 14px;font-size:18px;color:#fff">Account comparison</h2>
            <table style="width:100%;border-collapse:collapse;font-size:14px">
                <thead>
                    <tr>
                        <th style="text-align:left;padding:10px 8px;border-bottom:1px solid #333">Account</th>
                        <th style="text-align:left;padding:10px 8px;border-bottom:1px solid #333">Equity</th>
                        <th style="text-align:left;padding:10px 8px;border-bottom:1px solid #333">Today P&amp;L</th>
                        <th style="text-align:left;padding:10px 8px;border-bottom:1px solid #333">All-time tracked P&amp;L</th>
                    </tr>
                </thead>
                <tbody>{body}</tbody>
            </table>
        </div>
    """


def _weekly_signal_table(snapshot: Dict[str, Any]) -> str:
    weekly = snapshot.get("weekly_signal_performance") or {}
    realized = weekly.get("realized_strategies") or []
    shadow = weekly.get("shadow_signals") or []
    days = int(weekly.get("days") or 7)

    def rows(items: Iterable[Dict[str, Any]], empty: str) -> str:
        body = ""
        for item in list(items)[:8]:
            body += (
                "<tr>"
                f"<td style=\"padding:8px;border-bottom:1px solid #333\">{escape(str(item.get('name', '')))}</td>"
                f"<td style=\"padding:8px;border-bottom:1px solid #333\">{int(item.get('count') or 0)}</td>"
                f"<td style=\"padding:8px;border-bottom:1px solid #333\">{int(item.get('wins') or 0)} / {int(item.get('losses') or 0)}</td>"
                f"<td style=\"padding:8px;border-bottom:1px solid #333\">{_safe_float(item.get('avg_pnl_pct')):+.2f}%</td>"
                "</tr>"
            )
        if body:
            return body
        return f'<tr><td colspan="4" style="padding:10px 8px;color:#888;text-align:center">{empty}</td></tr>'

    return f"""
        <div style="padding:24px 28px;border-bottom:1px solid #333">
            <h2 style="margin:0 0 14px;font-size:18px;color:#fff">Weekly Signal Performance</h2>
            <p style="margin:0 0 12px;color:#888;font-size:13px">Last {days} day(s), combining realized exits and shadow picks.</p>
            <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:16px">
                <thead>
                    <tr>
                        <th style="text-align:left;padding:8px;border-bottom:1px solid #333">Actual Strategy</th>
                        <th style="text-align:left;padding:8px;border-bottom:1px solid #333">Count</th>
                        <th style="text-align:left;padding:8px;border-bottom:1px solid #333">W/L</th>
                        <th style="text-align:left;padding:8px;border-bottom:1px solid #333">Avg P&amp;L</th>
                    </tr>
                </thead>
                <tbody>{rows(realized, "No realized exits in this window.")}</tbody>
            </table>
            <table style="width:100%;border-collapse:collapse;font-size:13px">
                <thead>
                    <tr>
                        <th style="text-align:left;padding:8px;border-bottom:1px solid #333">Shadow Signal</th>
                        <th style="text-align:left;padding:8px;border-bottom:1px solid #333">Count</th>
                        <th style="text-align:left;padding:8px;border-bottom:1px solid #333">W/L</th>
                        <th style="text-align:left;padding:8px;border-bottom:1px solid #333">Avg P&amp;L</th>
                    </tr>
                </thead>
                <tbody>{rows(shadow, "No shadow picks recorded yet.")}</tbody>
            </table>
        </div>
    """


def _put_shadow_section() -> str:
    """Render the put-shadow readiness progress card, or '' when there's nothing
    to show (feature off / no paper trades yet). Never raises — a failure here
    must not break the daily report."""
    try:
        from .put_shadow import PutShadowLedger, default_ledger_path, READY_MIN_CLOSED

        ledger = PutShadowLedger(default_ledger_path())
        r = ledger.readiness()
    except Exception:  # noqa: BLE001
        return ""
    if r.n_closed == 0 and r.n_open == 0:
        return ""  # nothing happening yet — keep the report clean

    target = max(1, READY_MIN_CLOSED)
    pct = min(100, int(round(r.n_closed / target * 100)))
    exp_color = "#22c55e" if r.expectancy >= 0 else "#ef4444"
    if r.ready:
        headline = "&#9989; READY — track record cleared the bar"
        bar_color = "#22c55e"
        note = "Strict bar met (40+ closed, positive expectancy). Consider a paper-first real experiment."
    else:
        headline = f"Building paper track record &middot; {r.n_closed}/{target} closed"
        bar_color = "#6366f1"
        note = "No real money at risk. Go-live is gated by this bar clearing, not a date."

    return f"""
        <div style="padding:24px 28px;border-bottom:1px solid #333">
            <h2 style="margin:0 0 12px;font-size:18px;color:#fff">Put-Shadow Readiness <span style="color:#666;font-size:13px;font-weight:normal">(paper only)</span></h2>
            <p style="margin:0 0 10px;color:#cbd5e1;font-size:14px">{headline}</p>
            <div style="background:#0f0f23;border-radius:999px;height:14px;overflow:hidden;border:1px solid #333">
                <div style="width:{pct}%;height:100%;background:{bar_color}"></div>
            </div>
            <table style="width:100%;border-collapse:collapse;margin-top:14px;font-size:13px">
                <tr>
                    <td style="padding:6px 8px;color:#888">Closed / open</td>
                    <td style="padding:6px 8px;color:#eee;text-align:right">{r.n_closed} closed &middot; {r.n_open} open</td>
                </tr>
                <tr>
                    <td style="padding:6px 8px;color:#888">Win rate</td>
                    <td style="padding:6px 8px;color:#eee;text-align:right">{r.win_rate:.0f}%</td>
                </tr>
                <tr>
                    <td style="padding:6px 8px;color:#888">Expectancy / trade (after costs)</td>
                    <td style="padding:6px 8px;text-align:right;color:{exp_color};font-weight:bold">${r.expectancy:+,.2f}</td>
                </tr>
                <tr>
                    <td style="padding:6px 8px;color:#888">Total paper P&amp;L</td>
                    <td style="padding:6px 8px;text-align:right;color:{exp_color}">${r.total_pnl:+,.2f}</td>
                </tr>
            </table>
            <p style="margin:10px 0 0;color:#777;font-size:12px;line-height:1.5">{note}</p>
        </div>
    """


def build_report_html(snapshot: Dict[str, Any], etrade_summary: Dict[str, float | str] | None = None) -> str:
    """Build a concise HTML email focused on daily and total account P&L."""
    summary = _daily_and_total_summary(snapshot)
    today_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    is_trading_day = bool(snapshot.get("is_trading_day", True))

    comparison = _comparison_table(summary, etrade_summary)

    closed_banner = "" if is_trading_day else (
        "<div style=\"background:#3a2e0a;border-bottom:1px solid #a16207;padding:14px 28px\">"
        "<p style=\"margin:0;color:#fde68a;font-size:14px\">&#128197; <strong>Market closed today</strong>"
        " (weekend or holiday) &mdash; no trading expected, so a $0.00 daily P&amp;L is normal.</p></div>"
    )

    return f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;max-width:720px;margin:0 auto;background:#1a1a2e;color:#e0e0e0;border-radius:12px;overflow:hidden">
        <div style="background:linear-gradient(135deg,#0f0f23,#1a1a3e);padding:24px 28px;border-bottom:2px solid #333">
            <h1 style="margin:0;font-size:22px;color:#fff">TradeBot Daily Report</h1>
            <p style="margin:4px 0 0;color:#888;font-size:14px">{today_str}</p>
        </div>
        {closed_banner}

        <div style="padding:24px 28px;border-bottom:1px solid #333">
            <h2 style="margin:0 0 14px;font-size:18px;color:#fff">{summary['label']}</h2>
            {_metric_card("Today P&L", _safe_float(summary['daily_pnl']), _safe_float(summary['daily_pct']))}
            {_metric_card("All-Time Tracked P&L", _safe_float(summary['total_pnl']), _safe_float(summary['total_pct']))}
            <div style="background:#0f0f23;border:1px solid #333;border-radius:12px;padding:16px 18px;margin-top:12px">
                <p style="margin:0;color:#888;font-size:12px;text-transform:uppercase;letter-spacing:1px">Open Positions P&amp;L</p>
                <p style="margin:6px 0 0;font-size:22px;font-weight:bold;color:{'#22c55e' if _safe_float(summary['unrealized_pnl']) >= 0 else '#ef4444'}">
                    ${_safe_float(summary['unrealized_pnl']):+.2f}
                </p>
                <p style="margin:4px 0 0;color:#888;font-size:13px">Unrealized gain or loss on current holdings.</p>
            </div>
        </div>

        {comparison}

        {_weekly_signal_table(snapshot)}

        {_put_shadow_section()}

        <div style="padding:20px 28px;border-bottom:1px solid #333">
            <p style="margin:0;color:#888;font-size:13px;line-height:1.5">
                `Today P&amp;L` compares current equity to the account's start-of-day equity anchor.
                `Open Positions P&amp;L` is the unrealized gain or loss on holdings that are still open.
                `All-Time Tracked P&amp;L` combines realized closed-trade results with open-position P&amp;L.
                E*TRADE figures are derived from the current E*TRADE balance and position data returned at report time.
            </p>
        </div>

        <div style="padding:16px 28px;text-align:center;color:#555;font-size:11px">
            TradeBot • Automated Daily Report • <a href="{_dashboard_url()}" style="color:#6366f1">Dashboard</a>
        </div>
    </div>
    """


def _zero_pnl_diagnosis(snapshot: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """Inspect the account snapshot and work out the most likely reason daily
    P&L came through flat at $0.00 on a trading day, plus the key signals to
    show. Most-severe causes first."""
    account = snapshot.get("account", {}) or {}
    equity = _safe_float(account.get("equity"))
    last_equity = _safe_float(account.get("last_equity"))
    cash = _safe_float(account.get("cash"))
    buying_power = _safe_float(account.get("buying_power"))
    positions = snapshot.get("positions") or []
    regime = snapshot.get("market_regime", {}) or {}
    safety = snapshot.get("safety_status", {}) or {}
    paused = str(snapshot.get("buying_paused_reason") or "")
    degraded = bool(snapshot.get("degraded_mode"))
    buy_errors = int(_safe_float(safety.get("consecutive_buy_errors")))
    allow_buys = bool(regime.get("allow_long_buys", True))
    trades = snapshot.get("trades") or []
    last_trade = str(trades[0].get("created_at")) if trades and isinstance(trades[0], dict) else "unknown"

    if degraded:
        cause = "Bot is in DEGRADED mode — core functions are limited. Check the logs."
    elif buy_errors > 0:
        cause = f"Buys are failing ({buy_errors} consecutive errors) — likely a broker/API problem."
    elif positions and abs(equity - last_equity) < 0.005:
        cause = ("Equity did NOT change despite holding positions — almost certainly STALE "
                 "account/equity data from the broker. Positions should move equity during the "
                 "day, so this is the real red flag: check the Alpaca account feed.")
    elif not positions:
        cause = ("Account is all cash with no open positions — the bot isn't holding anything. "
                 "Verify it is actually finding and placing trades.")
    elif not allow_buys:
        cause = (f"Market-regime filter is risk-off (state '{regime.get('state', '?')}'), so new "
                 "buys are blocked by design — cautious, not a fault.")
    elif buying_power < 1.0:
        cause = "Fully invested (~$0 buying power) — no cash to trade, so the day was flat by default."
    else:
        cause = "No obvious cause from the snapshot — worth a manual look at the logs."

    details = {
        "Likely cause": cause,
        "Equity vs prev close": f"${equity:.2f} vs ${last_equity:.2f}",
        "Cash / buying power": f"${cash:.2f} / ${buying_power:.2f}",
        "Open positions": len(positions),
        "Market regime": f"{regime.get('state', '?')} — buys {'allowed' if allow_buys else 'blocked'}",
        "Buys paused": paused or "no",
        "Degraded mode": degraded,
        "Consecutive buy errors": buy_errors,
        "Last trade": last_trade,
    }
    return cause, details


def _maybe_alert_zero_pnl(snapshot: Dict[str, Any], summary: Dict[str, float | str]) -> None:
    """When daily P&L is ~$0.00 on a real trading day, automatically inspect the
    account, work out the most likely cause, and email the findings. The market
    being open means equity should have moved, so a flat $0.00 is abnormal.
    Weekends/holidays are excluded (those legitimately show $0.00)."""
    if not bool(snapshot.get("is_trading_day", True)):
        return
    daily_pnl = _safe_float(summary.get("daily_pnl"))
    if abs(daily_pnl) >= 0.005:
        return
    cause, details = _zero_pnl_diagnosis(snapshot)
    details = {"Daily P&L": f"${daily_pnl:+.2f}", **details}
    log.warning("Daily P&L is $0.00 on a trading day — %s", cause)
    send_failure_alert(
        "Daily P&L is $0.00 on a trading day — auto-diagnosis attached",
        "The market was open today, but daily P&L came through flat at $0.00. I ran an "
        "automatic check of the account; the most likely cause and the key signals are "
        f"below.\n\nLikely cause: {cause}",
        details,
    )


def send_daily_report(snapshot: Dict[str, Any]) -> bool:
    """Send the daily market-close email via Resend API. Returns True on success."""
    summary = _daily_and_total_summary(snapshot)
    etrade_summary = get_etrade_report_summary()
    html = build_report_html(snapshot, etrade_summary=etrade_summary)
    today_str = datetime.now(timezone.utc).strftime("%m/%d/%Y")
    closed_tag = "" if bool(snapshot.get("is_trading_day", True)) else "[Market closed] "
    subject = (
        f"{closed_tag}TradeBot {today_str}: Alpaca Today ${summary['daily_pnl']:+.2f} | All-Time ${summary['total_pnl']:+.2f}"
    )
    if etrade_summary:
        subject += (
            f" | E*TRADE Today ${_safe_float(etrade_summary['daily_pnl']):+.2f}"
            f" | All-Time ${_safe_float(etrade_summary['total_pnl']):+.2f}"
        )
    plain = (
        f"TradeBot Daily Report - {today_str}\n"
        f"TradeBot / Alpaca Today P&L: ${summary['daily_pnl']:+.2f} ({summary['daily_pct']:+.2f}%)\n"
        f"TradeBot / Alpaca Open Positions P&L: ${summary['unrealized_pnl']:+.2f}\n"
        f"TradeBot / Alpaca All-Time Tracked P&L: ${summary['total_pnl']:+.2f} ({summary['total_pct']:+.2f}%)\n"
    )
    weekly = snapshot.get("weekly_signal_performance") or {}
    realized = weekly.get("realized_strategies") or []
    shadow = weekly.get("shadow_signals") or []
    if realized:
        top = realized[0]
        plain += (
            f"Top realized signal ({weekly.get('days', 7)}d): {top.get('name')} "
            f"{_safe_float(top.get('avg_pnl_pct')):+.2f}% avg across {int(top.get('count') or 0)} result(s)\n"
        )
    if shadow:
        top = shadow[0]
        plain += (
            f"Top shadow signal ({weekly.get('days', 7)}d): {top.get('name')} "
            f"{_safe_float(top.get('avg_pnl_pct')):+.2f}% avg across {int(top.get('count') or 0)} pick(s)\n"
        )
    if etrade_summary:
        plain += (
            f"E*TRADE Today P&L: ${_safe_float(etrade_summary['daily_pnl']):+.2f}"
            f" ({_safe_float(etrade_summary['daily_pct']):+.2f}%)\n"
            f"E*TRADE All-Time Tracked P&L: ${_safe_float(etrade_summary['total_pnl']):+.2f}"
            f" ({_safe_float(etrade_summary['total_pct']):+.2f}%)\n"
        )
    plain += "View dashboard: " + _dashboard_url()
    sent = _send_resend_email(subject, html, plain, log_label="daily report")
    _maybe_alert_zero_pnl(snapshot, summary)
    return sent

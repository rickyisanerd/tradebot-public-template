from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Dict, List, Tuple


def sma(values: List[float], period: int) -> float:
    if len(values) < period:
        return mean(values) if values else 0.0
    return mean(values[-period:])


def rsi(values: List[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains = []
    losses = []
    for prev, cur in zip(values[-period - 1 : -1], values[-period:]):
        delta = cur - prev
        if delta >= 0:
            gains.append(delta)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(delta))
    avg_gain = mean(gains) if gains else 0.0
    avg_loss = mean(losses) if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return max(0.01, mean([h - l for h, l in zip(highs, lows)]))
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return max(0.01, mean(trs[-period:]))


def compute_metrics(bars: List[dict]) -> Dict[str, float]:
    closes = [float(b["c"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    volumes = [float(b["v"]) for b in bars]
    returns = []
    for prev, cur in zip(closes[:-1], closes[1:]):
        if prev:
            returns.append((cur - prev) / prev)
    latest = closes[-1]
    momentum_5 = ((latest / closes[-6]) - 1) * 100 if len(closes) >= 6 else 0.0
    momentum_20 = ((latest / closes[-21]) - 1) * 100 if len(closes) >= 21 else momentum_5
    vol20 = (pstdev(returns[-20:]) * math.sqrt(20) * 100) if len(returns) >= 2 else 0.0
    atr_value = atr(highs, lows, closes, 14)
    atr_pct = (atr_value / latest) * 100 if latest else 0.0
    avg_dollar_volume = mean([c * v for c, v in zip(closes[-20:], volumes[-20:])]) if closes else 0.0
    avg_volume_20 = mean(volumes[-20:]) if len(volumes) >= 20 else mean(volumes) if volumes else 1.0
    latest_volume = volumes[-1] if volumes else 0.0
    volume_ratio = latest_volume / max(1.0, avg_volume_20)
    if len(bars) >= 2:
        prev_close = float(bars[-2]["c"])
        current_open = float(bars[-1]["o"])
        gap_pct = ((current_open - prev_close) / prev_close) * 100 if prev_close else 0.0
    else:
        gap_pct = 0.0
    metrics = {
        "latest": latest,
        "sma10": sma(closes, 10),
        "sma20": sma(closes, 20),
        "sma50": sma(closes, 50),
        "rsi14": rsi(closes, 14),
        "momentum5": momentum_5,
        "momentum20": momentum_20,
        "volatility20": vol20,
        "atr": atr_value,
        "atr_pct": atr_pct,
        "avg_dollar_volume": avg_dollar_volume,
        "swing_high20": max(closes[-20:]) if len(closes) >= 20 else max(closes),
        "swing_low20": min(closes[-20:]) if len(closes) >= 20 else min(closes),
        "volume_ratio": volume_ratio,
        "avg_volume_20": avg_volume_20,
        "gap_pct": gap_pct,
    }
    return metrics


def analyze_momentum(metrics: Dict[str, float]) -> Tuple[float, List[str]]:
    score = 40.0
    reasons: List[str] = []
    if metrics["latest"] > metrics["sma20"] > metrics["sma50"]:
        score += 25
        reasons.append("price is above the 20 and 50 day trend")
    if 1.0 <= metrics["momentum20"] <= 20.0:
        score += 20
        reasons.append("20 day momentum is positive without looking too manic")
    if 48 <= metrics["rsi14"] <= 68:
        score += 15
        reasons.append("RSI is in the healthy trend zone")
    if metrics["momentum5"] < -3:
        score -= 10
        reasons.append("recent pullback is sharper than comfy")
    volume_ratio = metrics.get("volume_ratio", 1.0)
    if volume_ratio >= 2.0:
        score += 15
        reasons.append("volume spike suggests institutional interest or catalyst")
    elif volume_ratio >= 1.5:
        score += 8
        reasons.append("above average volume adds conviction")
    gap_pct = metrics.get("gap_pct", 0.0)
    if 1.0 <= gap_pct <= 5.0:
        score += 10
        reasons.append("positive gap up signals overnight demand")
    elif gap_pct > 8.0:
        score -= 5
        reasons.append("gap may be overextended for a safe entry")
    elif gap_pct < -3.0:
        score -= 8
        reasons.append("gap down suggests negative sentiment")
    return max(0.0, min(100.0, score)), reasons


def analyze_reversion(metrics: Dict[str, float]) -> Tuple[float, List[str]]:
    score = 35.0
    reasons: List[str] = []
    if metrics["latest"] > metrics["sma50"]:
        score += 20
        reasons.append("longer trend is still up")
    if metrics["latest"] < metrics["sma10"] and metrics["latest"] > metrics["sma20"] * 0.95:
        score += 20
        reasons.append("price pulled back without fully falling through the floor")
    if 38 <= metrics["rsi14"] <= 55:
        score += 15
        reasons.append("RSI suggests a bounce setup rather than exhaustion")
    if metrics["momentum20"] < -8:
        score -= 15
        reasons.append("medium trend is too soggy")
    return max(0.0, min(100.0, score)), reasons


def analyze_risk(metrics: Dict[str, float]) -> Tuple[float, List[str]]:
    score = 50.0
    reasons: List[str] = []
    if metrics["avg_dollar_volume"] >= 2_000_000:
        score += 20
        reasons.append("liquidity is decent")
    elif metrics["avg_dollar_volume"] < 1_000_000:
        score -= 20
        reasons.append("liquidity is a little swampy")
    if metrics["atr_pct"] <= 5:
        score += 15
        reasons.append("ATR percent is tame")
    else:
        score -= min(20, (metrics["atr_pct"] - 5) * 2)
        reasons.append("ATR percent says this one can buck like a caffeinated mule")
    if metrics["volatility20"] <= 35:
        score += 10
    else:
        score -= min(20, (metrics["volatility20"] - 35) * 0.5)
    return max(0.0, min(100.0, score)), reasons


def analyze_decision_support(metrics: Dict[str, float]) -> Tuple[float, List[str]]:
    score = 45.0
    reasons: List[str] = []

    reward_risk = metrics.get("reward_risk", 0.0)
    min_reward_risk = metrics.get("min_reward_risk", 1.8)
    if reward_risk >= min_reward_risk + 0.4:
        score += 20
        reasons.append("reward to risk leaves room for the setup to breathe")
    elif reward_risk >= min_reward_risk:
        score += 10
        reasons.append("reward to risk clears the minimum bar")
    else:
        score -= min(20, (min_reward_risk - reward_risk) * 15)
        reasons.append("reward to risk is a little skinny")

    if metrics["latest"] > metrics["sma20"] > metrics["sma50"]:
        score += 15
        reasons.append("trend alignment supports follow through")
    elif metrics["latest"] < metrics["sma20"]:
        score -= 10
        reasons.append("price is fighting the near term trend")

    if 45 <= metrics["rsi14"] <= 70:
        score += 10
        reasons.append("RSI says momentum is firm without looking cooked")
    elif metrics["rsi14"] > 78:
        score -= 12
        reasons.append("RSI looks stretched enough to invite profit taking")

    if metrics["atr_pct"] <= 6 and metrics["volatility20"] <= 40:
        score += 10
        reasons.append("volatility profile is manageable")
    elif metrics["atr_pct"] > 8:
        score -= min(15, (metrics["atr_pct"] - 8) * 2)
        reasons.append("volatility can yank this setup around")

    if metrics["avg_dollar_volume"] >= 3_000_000:
        score += 8
        reasons.append("liquidity should make entries and exits cleaner")
    elif metrics["avg_dollar_volume"] < 1_000_000:
        score -= 10
        reasons.append("thin liquidity makes trade management tougher")

    if metrics["momentum20"] < -5:
        score -= 8
        reasons.append("medium term momentum is still leaning the wrong way")
    elif metrics["momentum20"] > 25:
        score -= 6
        reasons.append("the move may already be a bit overextended")

    congress_buy_count = metrics.get("congress_buy_count", 0.0)
    congress_sell_count = metrics.get("congress_sell_count", 0.0)
    congress_net_count = metrics.get("congress_net_count", 0.0)
    days_since_congress_trade = metrics.get("days_since_congress_trade", 999.0)
    congress_weight = max(0.0, metrics.get("congress_weight", 1.0))
    if congress_buy_count > 0 and congress_sell_count == 0:
        score += min(18, congress_buy_count * 6) * congress_weight
        if congress_weight > 0 and days_since_congress_trade <= 14:
            reasons.append("recent congress buying adds an external conviction signal")
        elif congress_weight > 0:
            reasons.append("congress buying lines up with the setup")
    elif congress_sell_count > congress_buy_count:
        score -= min(22, congress_sell_count * 8) * congress_weight
        if congress_weight > 0:
            reasons.append("recent congress selling leans against the trade")
    elif congress_net_count == 0 and (congress_buy_count + congress_sell_count) > 0:
        score -= 6 * congress_weight
        if congress_weight > 0:
            reasons.append("mixed congress activity keeps the signal muddy")

    sec_form4_count = metrics.get("sec_form4_count", 0.0)
    sec_disclosure_count = metrics.get("sec_disclosure_count", 0.0)
    sec_offering_filing_count = metrics.get("sec_offering_filing_count", 0.0)
    days_since_sec_filing = metrics.get("days_since_sec_filing", 999.0)
    sec_weight = max(0.0, metrics.get("sec_weight", 1.0))
    if sec_form4_count > 0:
        score += min(12, sec_form4_count * 4) * sec_weight
        if sec_weight > 0:
            reasons.append("recent SEC insider filings add a real external signal")
    if sec_disclosure_count > 0 and sec_offering_filing_count == 0 and days_since_sec_filing <= 10:
        score += min(6, sec_disclosure_count * 2) * sec_weight
        if sec_weight > 0:
            reasons.append("fresh company disclosures reduce the odds of trading stale info")
    if sec_offering_filing_count > 0:
        score -= min(28, sec_offering_filing_count * 14) * sec_weight
        if sec_weight > 0:
            reasons.append("recent SEC offering paperwork can weigh on the setup")

    has_upcoming_earnings = metrics.get("has_upcoming_earnings", 0.0)
    days_until_earnings = metrics.get("days_until_earnings", 999.0)
    earnings_weight = max(0.0, metrics.get("earnings_weight", 1.0))
    if has_upcoming_earnings:
        if days_until_earnings <= 1:
            score -= 18 * earnings_weight
            if earnings_weight > 0:
                reasons.append("earnings are too close to pretend the setup is normal")
        elif days_until_earnings <= 3:
            score -= 12 * earnings_weight
            if earnings_weight > 0:
                reasons.append("near-term earnings add gap risk")
        elif days_until_earnings <= 7:
            score -= 6 * earnings_weight
            if earnings_weight > 0:
                reasons.append("earnings are close enough to keep position sizing honest")

    # --- Short volume / squeeze signal ---
    short_volume_ratio = metrics.get("short_volume_ratio", 0.0)
    short_volume_available = metrics.get("short_volume_available", 0.0)
    short_volume_weight = max(0.0, metrics.get("short_volume_weight", 1.0))
    if short_volume_available > 0 and short_volume_weight > 0:
        if short_volume_ratio >= 50:
            # Heavy shorting (>50% of volume) — potential squeeze setup
            score += min(12, (short_volume_ratio - 45) * 0.8) * short_volume_weight
            reasons.append("high short volume ratio suggests squeeze potential")
        elif short_volume_ratio >= 40:
            score += 4 * short_volume_weight
            reasons.append("elevated short interest adds speculative upside")
        elif short_volume_ratio < 20 and short_volume_ratio > 0:
            score -= 3 * short_volume_weight
            reasons.append("low short interest means less squeeze catalyst")

    has_near_macro_event = metrics.get("has_near_macro_event", 0.0)
    days_until_macro_event = metrics.get("days_until_macro_event", 999.0)
    near_fomc_count = metrics.get("near_fomc_count", 0.0)
    near_cpi_count = metrics.get("near_cpi_count", 0.0)
    macro_weight = max(0.0, metrics.get("macro_weight", 1.0))
    if has_near_macro_event:
        if days_until_macro_event <= 1:
            score -= 12 * macro_weight
            if macro_weight > 0:
                reasons.append("macro event risk is immediate enough to distort normal price action")
        elif days_until_macro_event <= 3:
            score -= 8 * macro_weight
            if macro_weight > 0:
                reasons.append("major macro events are close enough to add market-wide whiplash risk")
        if near_fomc_count > 0:
            score -= 4 * macro_weight
            if macro_weight > 0:
                reasons.append("FOMC timing can override single-name setups")
        elif near_cpi_count > 0:
            score -= 3 * macro_weight
            if macro_weight > 0:
                reasons.append("CPI timing can jolt the whole tape")

    return max(0.0, min(100.0, score)), reasons

"""
gmx_signal.py
Strategy A: GMX v2 Funding Rate Basis Trade signal.
Second signal to diversify from bridge inflow momentum.

Hypothesis: GMX funding rates periodically diverge from CEX perpetual rates.
When GMX longs overpay funding, a delta-neutral basis trade captures the spread.
"""

import requests
import json
import os
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
# Minimum basis spread to enter (after all costs)
MIN_SPREAD_BPS     = 15      # 0.15% — minimum viable spread
EXIT_SPREAD_BPS    = 5       # 0.05% — exit when spread compresses
GMX_OI_CAP_BUFFER  = 0.10   # exit if OI within 10% of cap
GLP_UTIL_MAX       = 0.85   # exit if GLP utilization > 85%

CACHE_DIR = Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _get(url: str, timeout: int = 10):
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "arb-quant/1.0"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [warn] GMX fetch failed: {e}")
        return None


# ── GMX funding rate ──────────────────────────────────────────────────────────
def fetch_gmx_funding_rate(market: str = "ETH") -> dict:
    """
    Fetch current GMX v2 funding rate via GMX Stats API.
    Returns annualized funding rate for the specified market.
    """
    result = {"rate_8h": 0.0, "rate_annual": 0.0, "source": "unavailable"}

    # GMX v2 stats endpoint
    url = "https://arbitrum-api.gmxinfra.io/prices/tickers"
    data = _get(url)

    if data and isinstance(data, list):
        for ticker in data:
            symbol = ticker.get("tokenSymbol", "")
            if market.upper() in symbol.upper():
                # GMX reports funding as 8h rate
                funding_8h = float(ticker.get("fundingRateLong", 0) or 0)
                result = {
                    "rate_8h":     round(funding_8h * 100, 6),       # as %
                    "rate_annual": round(funding_8h * 3 * 365 * 100, 4),  # annualized
                    "source":      "gmx-v2",
                    "market":      symbol,
                }
                return result

    # Fallback: DeFiLlama derivatives
    url  = "https://api.llama.fi/overview/derivatives"
    data = _get(url)
    if data:
        protos = data.get("protocols", [])
        gmx    = next((p for p in protos if "gmx" in p.get("name", "").lower()), None)
        if gmx:
            result["source"] = "defillama_fallback"

    return result


def fetch_cex_funding_rate(market: str = "ETH") -> dict:
    """
    Fetch reference CEX perpetual funding rate (Binance).
    Used to compute the basis spread vs GMX.
    """
    result = {"rate_8h": 0.0, "rate_annual": 0.0, "source": "unavailable"}

    url  = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={market.upper()}USDT&limit=1"
    data = _get(url)

    if data and isinstance(data, list) and len(data) > 0:
        rate_8h = float(data[0].get("fundingRate", 0)) * 100
        result  = {
            "rate_8h":     round(rate_8h, 6),
            "rate_annual": round(rate_8h * 3 * 365, 4),
            "source":      "binance",
        }

    return result


# ── Basis spread ──────────────────────────────────────────────────────────────
def compute_basis_spread(gmx_rate: dict, cex_rate: dict) -> dict:
    """
    Compute the funding rate basis spread.
    Positive spread = GMX longs overpaying vs CEX = tradeable edge.

    E[R] = (GMX_rate - CEX_rate) * notional - gas - price_impact - borrow_cost
    """
    spread_8h_pct  = gmx_rate["rate_8h"] - cex_rate["rate_8h"]
    spread_bps     = spread_8h_pct * 100   # convert % to bps
    spread_annual  = spread_bps * 3 * 365  # annualize

    # Cost estimate
    gas_cost_bps   = 0.002 * 100           # ~0.2bps gas on Arbitrum
    slippage_bps   = 0.08 * 100            # 8bps slippage
    total_cost_bps = gas_cost_bps + slippage_bps

    net_spread_bps = spread_bps - total_cost_bps
    viable         = net_spread_bps >= MIN_SPREAD_BPS

    return {
        "gross_spread_bps": round(spread_bps, 4),
        "net_spread_bps":   round(net_spread_bps, 4),
        "annual_pct":       round(spread_annual / 100, 4),
        "viable":           viable,
        "gmx_rate_8h":      gmx_rate["rate_8h"],
        "cex_rate_8h":      cex_rate["rate_8h"],
    }


# ── Fee drag check ────────────────────────────────────────────────────────────
def fee_drag_check(spread: dict, position_size_usd: float = 500.0) -> dict:
    """
    Full E[R] calculation for GMX basis trade.
    E[R] = (GMX_rate - CEX_rate) * notional - fees - slippage - gas
    """
    notional       = position_size_usd
    gross_return   = spread["gmx_rate_8h"] / 100 * notional  # per 8h period
    gas_cost       = 0.05   # Arbitrum gas
    protocol_fee   = notional * 0.003   # 0.30% GMX fee
    slippage_cost  = notional * 0.0008  # 8bps

    net_return_usd = gross_return - gas_cost - protocol_fee - slippage_cost
    net_return_pct = net_return_usd / notional * 100

    return {
        "gross_return_usd": round(gross_return, 4),
        "total_cost_usd":   round(gas_cost + protocol_fee + slippage_cost, 4),
        "net_return_usd":   round(net_return_usd, 4),
        "net_return_pct":   round(net_return_pct, 6),
        "viable":           net_return_usd > 0,
    }


# ── Main signal ───────────────────────────────────────────────────────────────
def run_gmx_signal(market: str = "ETH", position_size_usd: float = 500.0) -> dict:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"\n{'='*55}")
    print(f"  GMX FUNDING RATE BASIS SIGNAL")
    print(f"  {timestamp}  |  Market: {market}")
    print(f"{'='*55}")

    gmx_rate = fetch_gmx_funding_rate(market)
    cex_rate = fetch_cex_funding_rate(market)
    spread   = compute_basis_spread(gmx_rate, cex_rate)
    fee_drag = fee_drag_check(spread, position_size_usd)

    print(f"\n  GMX funding (8h):  {gmx_rate['rate_8h']:>+8.4f}%  ({gmx_rate['source']})")
    print(f"  CEX funding (8h):  {cex_rate['rate_8h']:>+8.4f}%  ({cex_rate['source']})")
    print(f"  {'─'*45}")
    print(f"  Gross spread:      {spread['gross_spread_bps']:>+8.2f} bps")
    print(f"  After costs:       {spread['net_spread_bps']:>+8.2f} bps")
    print(f"  Threshold:         {MIN_SPREAD_BPS:>+8.0f} bps to enter")
    print(f"  {'─'*45}")
    print(f"  Fee drag:")
    print(f"    Gross return:    ${fee_drag['gross_return_usd']:>+8.4f}")
    print(f"    Total costs:     ${fee_drag['total_cost_usd']:>+8.4f}")
    print(f"    Net return:      ${fee_drag['net_return_usd']:>+8.4f} ({fee_drag['net_return_pct']:+.4f}%)")

    if spread["viable"] and fee_drag["viable"]:
        decision = "ENTRY SIGNAL"
        print(f"\n  ✅ ENTRY SIGNAL — basis spread viable after costs")
        print(f"  Setup: Long {market} on GMX + Short {market} on Binance perp")
        print(f"  Capture spread of {spread['net_spread_bps']:.1f} bps per 8h period")
    else:
        decision = "NO SIGNAL"
        reason   = f"spread {spread['net_spread_bps']:.1f} bps < {MIN_SPREAD_BPS} bps threshold" \
                   if not spread["viable"] else "fee drag negative"
        print(f"\n  ⏸ NO SIGNAL — {reason}")

    print(f"{'='*55}\n")

    result = {
        "timestamp":   timestamp,
        "market":      market,
        "decision":    decision,
        "spread":      spread,
        "fee_drag":    fee_drag,
        "gmx_rate":    gmx_rate,
        "cex_rate":    cex_rate,
    }

    # Cache result
    (CACHE_DIR / "gmx_signal.json").write_text(
        json.dumps(result, indent=2, default=str)
    )
    return result


if __name__ == "__main__":
    run_gmx_signal(market="ETH", position_size_usd=500.0)

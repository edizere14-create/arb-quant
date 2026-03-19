"""
yield_router.py  (v2 — adds Yearn V3 vaults)
Idle capital router for Strategy D.
Compares Aave V3, Radiant, and Yearn V3 on Arbitrum.
Recommends the highest risk-adjusted yield for USDC while waiting for signal.

Book reference: How to DeFi Advanced Ch.12 (Yield Aggregators) —
  Yearn automatically rotates capital between highest-yielding strategies
  and compounds continuously, typically outperforming raw Aave supply APY.
"""

import requests
from datetime import datetime, timezone

# ── Data sources ──────────────────────────────────────────────────────────────
DEFILLAMA_YIELDS = "https://yields.llama.fi/pools"

# DeFiLlama pool IDs for Arbitrum USDC positions
# These are stable identifiers — verify at yields.llama.fi if APY seems off
POOL_IDS = {
    "Aave V3 USDC (Arbitrum)":   "cefa9bb8-c230-459a-a855-3dac26b8b00b",
    "Radiant USDC (Arbitrum)":   "d4b3c3d3-4f8c-4b3e-8f3e-3b3c3d4f8c4b",
    "Yearn USDC V3 (Arbitrum)":  "7da72d09-56ca-4ec5-a45f-59114353e487",
}

# Risk weights — Yearn adds smart contract risk from its vault layer
RISK_ADJUSTMENT = {
    "Aave V3 USDC (Arbitrum)":   0.0,   # baseline — well-audited, immutable
    "Radiant USDC (Arbitrum)":   0.3,   # slight discount — newer, smaller TVL
    "Yearn USDC V3 (Arbitrum)":  0.5,   # discount for vault strategy risk layer
}

# Minimum TVL to consider a pool safe for our position size
MIN_TVL_USD = 5_000_000  # $5M

# ── Helpers ───────────────────────────────────────────────────────────────────
def _get(url: str, timeout: int = 12) -> dict | list | None:
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "arb-quant/1.0"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [warn] fetch failed: {e}")
        return None


# ── Fetch APYs ────────────────────────────────────────────────────────────────
def fetch_pool_apys() -> dict:
    """
    Fetch current APYs for all tracked yield pools from DeFiLlama.
    Falls back to broad Arbitrum USDC search if pool ID lookup fails.
    """
    data = _get(DEFILLAMA_YIELDS)
    results = {}

    if not data:
        return results

    pools = data.get("data", data) if isinstance(data, dict) else data

    # Build lookup by pool ID
    id_lookup = {p.get("pool"): p for p in pools if p.get("pool")}

    for name, pool_id in POOL_IDS.items():
        pool = id_lookup.get(pool_id)
        if pool:
            results[name] = {
                "apy":          pool.get("apy") or pool.get("apyBase") or 0,
                "apy_7d_avg":   pool.get("apyMean30d") or pool.get("apy7d") or 0,
                "tvl_usd":      pool.get("tvlUsd") or 0,
                "il_risk":      pool.get("ilRisk") or "no",
                "source":       "defillama",
            }
        else:
            results[name] = None  # mark as unavailable

    # Fallback: search by project name and chain for any nulls
    if any(v is None for v in results.values()):
        arb_usdc = [
            p for p in pools
            if p.get("chain", "").lower() == "arbitrum"
            and "usdc" in p.get("symbol", "").lower()
        ]
        for name, val in results.items():
            if val is not None:
                continue
            keyword = name.split()[0].lower()  # "aave", "radiant", "yearn"
            match = next(
                (p for p in arb_usdc
                 if keyword in p.get("project", "").lower()),
                None
            )
            if match:
                results[name] = {
                    "apy":        match.get("apy") or match.get("apyBase") or 0,
                    "apy_7d_avg": match.get("apyMean30d") or 0,
                    "tvl_usd":    match.get("tvlUsd") or 0,
                    "il_risk":    match.get("ilRisk") or "no",
                    "source":     "defillama_fallback",
                }

    return results


# ── Yearn-specific note ───────────────────────────────────────────────────────
def yearn_advantage_note(yearn_apy: float, aave_apy: float) -> str:
    """
    Explain why Yearn often beats Aave raw supply APY (Ch.12 of book).
    Yearn auto-compounds and rotates between Aave, Compound, and other
    lenders to always capture the highest rate.
    """
    diff = yearn_apy - aave_apy
    if diff > 0:
        return (
            f"Yearn outperforms Aave by {diff:.2f}% APY by auto-compounding "
            f"and rotating between lenders. Consider the +0.5% risk discount "
            f"for the extra vault layer."
        )
    else:
        return (
            f"Aave currently offers better raw APY than Yearn. "
            f"Yearn's rotation advantage is not active this cycle."
        )


# ── Main router ───────────────────────────────────────────────────────────────
def run_yield_router(capital_usd: float = 500.0) -> dict:
    """
    Compare all yield options and recommend the best pool
    for idle capital while waiting for an entry signal.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"\n{'='*58}")
    print(f"  YIELD ROUTER  (Strategy D — Idle Capital)")
    print(f"  {timestamp}")
    print(f"  Capital to deploy: ${capital_usd:,.0f}")
    print(f"{'='*58}")

    pools = fetch_pool_apys()

    if not pools:
        print("\n  ❌ Could not fetch yield data. Deploy to Aave manually:")
        print("  → app.aave.com → Arbitrum → Supply USDC")
        return {"recommendation": "Aave V3 (manual)", "apy": None}

    print(f"\n  {'Pool':<35} {'APY':>7}  {'7d Avg':>8}  "
          f"{'Adj APY':>8}  {'TVL':>12}  Status")
    print(f"  {'-'*35} {'-'*7}  {'-'*8}  {'-'*8}  {'-'*12}  {'-'*10}")

    ranked = []

    for name, data in pools.items():
        if not data:
            print(f"  {name:<35} {'N/A':>7}  {'N/A':>8}  {'N/A':>8}  {'N/A':>12}  unavailable")
            continue

        apy        = data["apy"] or 0
        apy_7d     = data["apy_7d_avg"] or apy
        tvl        = data["tvl_usd"] or 0
        discount   = RISK_ADJUSTMENT.get(name, 0)
        adj_apy    = apy - discount
        daily_usd  = capital_usd * (apy / 100) / 365
        tvl_ok     = tvl >= MIN_TVL_USD

        status = "✅ OK" if tvl_ok else "⚠️  Low TVL"

        print(f"  {name:<35} {apy:>6.2f}%  {apy_7d:>7.2f}%  "
              f"{adj_apy:>7.2f}%  ${tvl:>10,.0f}  {status}")

        if tvl_ok:
            ranked.append({
                "name":      name,
                "apy":       apy,
                "apy_7d":    apy_7d,
                "adj_apy":   adj_apy,
                "tvl":       tvl,
                "daily_usd": daily_usd,
                "data":      data,
            })

    if not ranked:
        print("\n  ⚠️  No pools met minimum TVL threshold. Defaulting to Aave.")
        return {"recommendation": "Aave V3 (TVL fallback)", "apy": None}

    # Sort by risk-adjusted APY
    ranked.sort(key=lambda x: x["adj_apy"], reverse=True)
    best = ranked[0]

    print(f"\n{'─'*58}")
    print(f"  🏆 RECOMMENDATION: {best['name']}")
    print(f"  APY:          {best['apy']:.2f}% (risk-adj: {best['adj_apy']:.2f}%)")
    print(f"  Daily yield:  ${best['daily_usd']:.3f} on ${capital_usd:,.0f}")
    print(f"  Annual yield: ${capital_usd * best['apy'] / 100:.2f} on ${capital_usd:,.0f}")
    print(f"  Pool TVL:     ${best['tvl']:,.0f}")

    # Yearn-specific note if it wins or loses to Aave
    aave_data   = pools.get("Aave V3 USDC (Arbitrum)")
    yearn_data  = pools.get("Yearn USDC V3 (Arbitrum)")
    if aave_data and yearn_data:
        note = yearn_advantage_note(
            yearn_data.get("apy", 0),
            aave_data.get("apy", 0),
        )
        print(f"\n  ℹ️  Yearn note: {note}")

    # Deposit instructions
    print(f"\n  Deposit instructions:")
    if "Aave" in best["name"]:
        print(f"  → app.aave.com → switch to Arbitrum → Supply USDC")
    elif "Radiant" in best["name"]:
        print(f"  → app.radiant.capital → Arbitrum → Deposit USDC")
    elif "Yearn" in best["name"]:
        print(f"  → yearn.fi → Arbitrum → USDC Vault")
        print(f"  → Or zap in via app.yearn.fi/vaults?chainId=42161")

    print(f"{'='*58}\n")

    return {
        "recommendation": best["name"],
        "apy":            best["apy"],
        "adj_apy":        best["adj_apy"],
        "daily_usd":      best["daily_usd"],
        "all_pools":      ranked,
        "timestamp":      timestamp,
    }


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_yield_router(capital_usd=500.0)

"""
data_fetcher.py
Centralised DeFiLlama helpers for the arb-quant bot.

Priority: Aave V3 → Radiant V2  (lending lead)
"""

import asyncio
import httpx

_HEADERS = {"User-Agent": "arb-quant/1.0"}
_TIMEOUT = 10


# ── low-level helper ──────────────────────────────────────────────────────────

async def call_defillama(slug: str, chain: str) -> float:
    """Return latest TVL (USD) for *slug* on *chain* via DeFiLlama, or 0.0."""
    url = f"https://api.llama.fi/protocol/{slug}"
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=_TIMEOUT) as c:
            r = await c.get(url)
            r.raise_for_status()
            data = r.json()
        for key, series in (data.get("chainTvls") or {}).items():
            if chain.lower() in key.lower():
                # DeFiLlama returns either a list or {"tvl": [...]}
                if isinstance(series, dict):
                    series = series.get("tvl", [])
                if isinstance(series, list) and series:
                    return float(series[-1].get("totalLiquidityUSD", 0))
    except Exception as exc:
        print(f"  [data_fetcher] {slug}/{chain} error: {exc}")
    return 0.0


# ── lending lead ──────────────────────────────────────────────────────────────

AAVE_TVL_THRESHOLD = 500_000_000  # $500 M


async def fetch_lending_lead() -> dict:
    """
    Primary: Aave V3 on Arbitrum (more stable, deeper liquidity).
    Fallback: Radiant V2 only if Aave TVL is below the $500 M threshold.
    Returns {"protocol": str, "tvl": float}.
    """
    aave_tvl = await call_defillama("aave-v3", "arbitrum")

    if aave_tvl > AAVE_TVL_THRESHOLD:
        return {"protocol": "Aave V3", "tvl": aave_tvl}

    # Aave below threshold — try Radiant as fallback
    radiant_tvl = await call_defillama("radiant-v2", "arbitrum")
    if radiant_tvl > 0:
        return {"protocol": "Radiant V2", "tvl": radiant_tvl}

    # Both unavailable — return whatever Aave reported
    return {"protocol": "Aave V3", "tvl": aave_tvl}


def fetch_lending_lead_sync() -> dict:
    """Blocking wrapper for callers that aren't async (e.g. monitor.py)."""
    return asyncio.run(fetch_lending_lead())

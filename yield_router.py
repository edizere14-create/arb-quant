"""
Yield Router — Strategy D: Capital Recycling (Stable Yield Enhancement)
Per strategy-framework.md §2 Strategy D

Activates when Strategy C (Bridge Inflow Momentum) signal is blocked
due to correlation decay. Routes idle $500 USDC to the highest-APY
supply pool between Aave V3 (Arbitrum) and Radiant.
"""

import urllib.request
import urllib.error
import json
import sys

POSITION_SIZE_USD = 500


# ── HTTP helper ──────────────────────────────────────────────────────────────

def _get(url: str, timeout: int = 15) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "arb-quant/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ── Data sources ─────────────────────────────────────────────────────────────

def fetch_aave_usdc_apy() -> float | None:
    """
    Aave V3 pools API — look for USDC supply on Arbitrum.
    Endpoint: https://aave-api-v3.aave.com/data/pools
    Returns APY as a percentage (e.g. 3.5 for 3.5%), or None on failure.
    """
    try:
        pools = _get("https://aave-api-v3.aave.com/data/pools")
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print(f"      ⚠️  Aave API error: {e}")
        return None

    # pools is a list; find USDC on Arbitrum (chainId 42161)
    for pool in pools:
        symbol = pool.get("symbol", "").upper()
        chain_id = pool.get("chainId")
        # Aave API may use chainId or network name
        is_arbitrum = (
            chain_id == 42161
            or str(chain_id) == "42161"
            or pool.get("chain", "").lower() == "arbitrum"
            or pool.get("network", "").lower() == "arbitrum"
        )
        if symbol in ("USDC", "USDC.E") and is_arbitrum:
            # liquidityRate is the supply APY as a ray (1e27) or percentage
            rate = pool.get("liquidityRate") or pool.get("supplyAPY")
            if rate is not None:
                rate = float(rate)
                # If rate looks like a ray value (> 1e20), convert
                if rate > 1e10:
                    return rate / 1e25  # ray to percentage
                elif rate < 1:
                    return rate * 100   # decimal to percentage
                return rate
    return None


def fetch_radiant_usdc_apy() -> float | None:
    """
    DeFiLlama yields API — search for Radiant USDC pool on Arbitrum.
    Endpoint: https://yields.llama.fi/pools
    Returns APY as a percentage, or None on failure.
    """
    try:
        data = _get("https://yields.llama.fi/pools")
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print(f"      ⚠️  DeFiLlama yields API error: {e}")
        return None

    pools = data.get("data", data) if isinstance(data, dict) else data

    best_apy = None
    for pool in pools:
        project = (pool.get("project") or "").lower()
        chain = (pool.get("chain") or "").lower()
        symbol = (pool.get("symbol") or "").upper()

        if "radiant" in project and chain == "arbitrum" and "USDC" in symbol:
            apy = pool.get("apy")
            if apy is not None and (best_apy is None or apy > best_apy):
                best_apy = apy
    return best_apy


def fetch_defillama_aave_usdc_apy() -> float | None:
    """
    Fallback: query DeFiLlama yields for Aave V3 USDC on Arbitrum.
    """
    try:
        data = _get("https://yields.llama.fi/pools")
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print(f"      ⚠️  DeFiLlama yields API fallback error: {e}")
        return None

    pools = data.get("data", data) if isinstance(data, dict) else data

    best_apy = None
    for pool in pools:
        project = (pool.get("project") or "").lower()
        chain = (pool.get("chain") or "").lower()
        symbol = (pool.get("symbol") or "").upper()

        if "aave" in project and "v3" in project and chain == "arbitrum" and "USDC" in symbol:
            apy = pool.get("apy")
            if apy is not None and (best_apy is None or apy > best_apy):
                best_apy = apy
    return best_apy


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 64)
    print("  Strategy D — Yield Router (Idle Capital Parking)")
    print(f"  Capital: ${POSITION_SIZE_USD}")
    print("=" * 64)

    # ── Aave V3 ──────────────────────────────────────────────────────────
    print("\n[1/3] Querying Aave V3 USDC supply APY (Arbitrum) …")
    aave_apy = fetch_aave_usdc_apy()
    aave_source = "Aave API"

    if aave_apy is None:
        print("      Aave direct API failed — trying DeFiLlama fallback …")
        aave_apy = fetch_defillama_aave_usdc_apy()
        aave_source = "DeFiLlama (aave-v3)"

    if aave_apy is not None:
        print(f"      Aave V3 USDC APY : {aave_apy:.2f}%  (source: {aave_source})")
    else:
        print("      ⚠️  Could not fetch Aave V3 USDC APY")

    # ── Radiant ──────────────────────────────────────────────────────────
    print("\n[2/3] Querying Radiant USDC APY (Arbitrum) via DeFiLlama …")
    radiant_apy = fetch_radiant_usdc_apy()

    if radiant_apy is not None:
        print(f"      Radiant USDC APY : {radiant_apy:.2f}%")
    else:
        print("      ⚠️  Could not fetch Radiant USDC APY")

    # ── Decision ─────────────────────────────────────────────────────────
    print("\n[3/3] Yield comparison & recommendation")

    candidates = {}
    if aave_apy is not None:
        candidates["Aave V3 (Arbitrum)"] = aave_apy
    if radiant_apy is not None:
        candidates["Radiant (Arbitrum)"] = radiant_apy

    if not candidates:
        print("      ❌ No yield data available — cannot route capital")
        sys.exit(1)

    best_pool = max(candidates, key=candidates.get)
    best_apy = candidates[best_pool]
    daily_yield = (best_apy / 100) / 365 * POSITION_SIZE_USD

    print()
    print("─" * 64)
    for pool, apy in sorted(candidates.items(), key=lambda x: -x[1]):
        marker = " ◀ SELECTED" if pool == best_pool else ""
        dy = (apy / 100) / 365 * POSITION_SIZE_USD
        print(f"  {pool:30s}  APY {apy:6.2f}%  "
              f"(${dy:.4f}/day on ${POSITION_SIZE_USD}){marker}")
    print("─" * 64)
    print(f"\n  ✅ RECOMMENDED: Deposit ${POSITION_SIZE_USD} USDC → {best_pool}")
    print(f"     Current APY  : {best_apy:.2f}%")
    print(f"     Daily yield  : ${daily_yield:.4f}")
    print(f"     Monthly est. : ${daily_yield * 30:.2f}")
    print()
    print("  ⚠️  Monitor health factor if looping (kill switch: HF < 1.3)")
    print("  ⚠️  Recheck when bridge momentum signal reactivates")
    print("─" * 64)

    return best_pool, best_apy, daily_yield


if __name__ == "__main__":
    main()

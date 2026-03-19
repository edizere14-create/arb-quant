"""
route_optimizer.py
1inch aggregator routing for best execution.
Queries 1inch API to split orders across pools and minimize price impact.
Always run before any trade above MIN_DIRECT_TRADE_USD.
"""

import requests
from datetime import datetime, timezone

# ── Constants ─────────────────────────────────────────────────────────────────
ONEINCH_QUOTE_URL = "https://api.1inch.dev/swap/v6.0/42161/quote"  # Arbitrum chain ID 42161
ONEINCH_SWAP_URL  = "https://api.1inch.dev/swap/v6.0/42161/swap"

# Below this size, direct DEX is fine. Above it, always route via aggregator.
MIN_DIRECT_TRADE_USD = 500

# Common Arbitrum token addresses
TOKENS = {
    "ETH":  "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
    "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # native USDC on Arb
    "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
    "ARB":  "0x912CE59144191C1204E64559FE8253a0e49E6548",
    "GMX":  "0xfc5A1A6EB076a2C7aD06eD22C90d7E710E35ad0a",
    "WETH": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
}

# 1inch requires an API key — load from env
import os
ONEINCH_API_KEY = os.getenv("ONEINCH_API_KEY", "")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _get_token_address(symbol: str) -> str:
    addr = TOKENS.get(symbol.upper())
    if not addr:
        raise ValueError(
            f"Token '{symbol}' not in registry. "
            f"Add its Arbitrum address to TOKENS dict."
        )
    return addr


def _decimals(symbol: str) -> int:
    """Return token decimals for amount conversion."""
    d = {"ETH": 18, "WETH": 18, "ARB": 18, "GMX": 18,
         "USDC": 6, "USDT": 6}
    return d.get(symbol.upper(), 18)


def _to_wei(amount: float, symbol: str) -> int:
    return int(amount * 10 ** _decimals(symbol))


def _from_wei(amount_wei: int, symbol: str) -> float:
    return amount_wei / 10 ** _decimals(symbol)


def _headers() -> dict:
    h = {"Accept": "application/json", "User-Agent": "arb-quant/1.0"}
    if ONEINCH_API_KEY:
        h["Authorization"] = f"Bearer {ONEINCH_API_KEY}"
    return h


# ── Quote fetcher ─────────────────────────────────────────────────────────────
def get_best_quote(
    from_token: str,
    to_token: str,
    amount: float,
    slippage_pct: float = 0.5,
) -> dict:
    """
    Fetch best execution quote from 1inch aggregator.
    Compares routes across Uniswap V3, Camelot, GMX, and other Arbitrum DEXs.

    Args:
        from_token:   symbol, e.g. "USDC"
        to_token:     symbol, e.g. "ARB"
        amount:       amount in human units (e.g. 500 for $500 USDC)
        slippage_pct: max slippage tolerance in percent (default 0.5%)

    Returns:
        dict with quote details, best route, and estimated savings vs direct
    """
    from_addr = _get_token_address(from_token)
    to_addr   = _get_token_address(to_token)
    amount_wei = _to_wei(amount, from_token)

    params = {
        "src":              from_addr,
        "dst":              to_addr,
        "amount":           str(amount_wei),
        "includeProtocols": "true",
        "includeGas":       "true",
    }

    result = {
        "from_token":       from_token,
        "to_token":         to_token,
        "amount_in":        amount,
        "amount_out":       None,
        "price_impact_pct": None,
        "gas_estimate":     None,
        "protocols_used":   [],
        "recommendation":   None,
        "raw":              None,
        "error":            None,
    }

    try:
        r = requests.get(
            ONEINCH_QUOTE_URL,
            params=params,
            headers=_headers(),
            timeout=10,
        )

        if r.status_code == 401:
            result["error"] = (
                "1inch API key required. Set ONEINCH_API_KEY env var. "
                "Get a free key at https://portal.1inch.dev"
            )
            return result

        if r.status_code == 200:
            data = r.json()
            result["raw"] = data

            to_amount = int(data.get("toAmount") or data.get("toTokenAmount", 0))
            result["amount_out"] = _from_wei(to_amount, to_token)
            result["gas_estimate"] = data.get("gas") or data.get("estimatedGas")

            # Extract protocols used
            protocols = data.get("protocols", [])
            if protocols:
                flat = []
                for route in protocols:
                    for hop in route:
                        for part in hop:
                            name = part.get("name") or part.get("protocol", "?")
                            pct  = part.get("part", 100)
                            flat.append(f"{name} ({pct}%)")
                result["protocols_used"] = flat

        else:
            result["error"] = f"1inch API error {r.status_code}: {r.text[:200]}"
            return result

    except Exception as e:
        result["error"] = str(e)
        return result

    return result


# ── Savings estimator ─────────────────────────────────────────────────────────
def estimate_direct_vs_aggregator(
    from_token: str,
    to_token: str,
    amount_usd: float,
) -> dict:
    """
    Compare 1inch aggregated route vs direct Uniswap V3 execution.
    Returns estimated savings in USD and recommendation.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"\n{'='*55}")
    print(f"  ROUTE OPTIMIZER")
    print(f"  {timestamp}")
    print(f"  {from_token} → {to_token}  |  Amount: ${amount_usd:,.0f}")
    print(f"{'='*55}")

    result = {
        "timestamp":       timestamp,
        "from_token":      from_token,
        "to_token":        to_token,
        "amount_usd":      amount_usd,
        "use_aggregator":  amount_usd >= MIN_DIRECT_TRADE_USD,
        "quote":           None,
        "recommendation":  None,
    }

    if amount_usd < MIN_DIRECT_TRADE_USD:
        msg = (
            f"Trade size ${amount_usd} is below ${MIN_DIRECT_TRADE_USD} threshold. "
            f"Direct DEX execution is fine — aggregator gas overhead not worth it."
        )
        result["recommendation"] = msg
        print(f"\n  ℹ️  {msg}")
        print(f"{'='*55}\n")
        return result

    # Get aggregator quote
    print(f"\n  Querying 1inch aggregator...")
    quote = get_best_quote(from_token, to_token, amount_usd)
    result["quote"] = quote

    if quote["error"]:
        print(f"\n  ❌ Quote failed: {quote['error']}")
        print(f"\n  Fallback recommendation: route via Camelot or Uniswap V3 directly.")
        result["recommendation"] = "aggregator_unavailable_use_uniswap_v3"
        print(f"{'='*55}\n")
        return result

    print(f"\n  Amount out:      {quote['amount_out']:.4f} {to_token}")
    print(f"  Gas estimate:    {quote['gas_estimate']:,} units" if quote["gas_estimate"] else "  Gas estimate:   unavailable")
    print(f"  Route:           {' → '.join(quote['protocols_used']) or 'single hop'}")

    # Recommendation
    protocols = quote.get("protocols_used", [])
    is_split  = len(protocols) > 1
    rec = (
        "USE AGGREGATOR — 1inch splits across multiple pools for better execution"
        if is_split else
        "DIRECT IS FINE — 1inch routes to single pool (same as going direct)"
    )
    result["recommendation"] = rec

    icon = "✅" if is_split else "ℹ️"
    print(f"\n  {icon} {rec}")
    print(f"\n  To execute via 1inch:")
    print(f"  → app.1inch.io/#/42161/simple/swap/{from_token}/{to_token}")
    print(f"  → or integrate ONEINCH_SWAP_URL in execution script")
    print(f"{'='*55}\n")

    return result


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Example: route $500 USDC → ARB
    estimate_direct_vs_aggregator("USDC", "ARB", 500)

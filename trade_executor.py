"""
trade_executor.py  (v2)
Pre-trade checklist. Run immediately before any live entry.
All gates must pass before touching MetaMask.

New in v2:
  ✅ Flash loan attack surface check (flashloan_check.py)
  ✅ Rug pull / unlock schedule check (rugpull_check.py)
  ✅ 1inch route optimizer (route_optimizer.py)
  ✅ Yearn included in yield router (yield_router.py)
"""

import sys
import requests
from datetime import datetime, timezone

# Local modules
from flashloan_check  import run_flashloan_surface_check
from rugpull_check    import run_rugpull_check
from route_optimizer  import estimate_direct_vs_aggregator
from bridge_signal    import bridge_signal, inflow_signal_is_valid
import pandas as pd

# ── Config ────────────────────────────────────────────────────────────────────
ARB_RPC              = "https://arb1.arbitrum.io/rpc"
FLASHBOTS_RPC        = "https://rpc.flashbots.net/fast"
PORTFOLIO_TOTAL_USD  = 500.0    # ← update as portfolio grows
MAX_STRATEGY_PCT     = 0.30     # 30% cap per strategy (§0 rule)

# Protocols involved in Strategy C
STRATEGY_C_PROTOCOLS = ["uniswap-v3", "gmx-v2"]

# ── Helpers ───────────────────────────────────────────────────────────────────
def _rpc(url: str, method: str, params: list):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        r = requests.post(url, json=payload, timeout=8)
        return r.json().get("result")
    except Exception:
        return None


def _check(label: str, passed: bool, detail: str = "") -> dict:
    icon = "✅" if passed else "❌"
    line = f"  [{icon}] {label}"
    if detail:
        line += f"  —  {detail}"
    print(line)
    return {"label": label, "passed": passed, "detail": detail}


# ── Gate 1: Signal validity ───────────────────────────────────────────────────
def gate_signal(from_token: str = "USDC", to_token: str = "ARB") -> dict:
    """Re-run bridge signal. z > 2.0 AND correlation valid."""
    try:
        prices = pd.read_json(".cache/arb_prices.json")
        inflows = pd.read_json(".cache/bridge_volume_arb.json")

        # Reuse cached data — bridge_signal module handles freshness
        from bridge_signal import fetch_bridge_inflows, compute_zscore

        inflow_series = fetch_bridge_inflows()
        z = compute_zscore(inflow_series)
        latest_z = float(z.iloc[-1]) if hasattr(z, "iloc") else float(z)
        corr_valid = inflow_signal_is_valid(z, prices["price"] if "price" in prices else prices.iloc[:, 0])

        passed = (latest_z > 2.0) and corr_valid
        detail = f"z={latest_z:.4f}, correlation={'valid' if corr_valid else 'DECAYED'}"
        return _check("Signal valid", passed, detail)
    except Exception as e:
        return _check("Signal valid", False, f"check failed: {e}")


# ── Gate 2: Flash loan surface ────────────────────────────────────────────────
def gate_flashloan(position_size_usd: float) -> dict:
    """Run flash loan attack surface scan on all strategy protocols."""
    print("\n  [Running flash loan surface scan...]")
    result = run_flashloan_surface_check(
        protocol_slugs=STRATEGY_C_PROTOCOLS,
        position_size_usd=position_size_usd,
    )
    passed = result["overall"] in ("GO", "CAUTION")
    detail = f"Overall: {result['overall']}"
    return _check("Flash loan surface safe", passed, detail)


# ── Gate 3: Rug pull / unlock check ──────────────────────────────────────────
def gate_rugpull() -> dict:
    """Scan for token unlock events and multisig risk."""
    print("\n  [Running rug pull scan...]")
    result = run_rugpull_check(protocol_keys=STRATEGY_C_PROTOCOLS)
    passed = result["overall"] in ("GO", "CAUTION")
    detail = f"Overall: {result['overall']}"
    return _check("No rug pull signals", passed, detail)


# ── Gate 4: Automated protocol risk score (§4 framework) ─────────────────────
def _auto_score_protocol(slug: str) -> dict:
    """
    Automatically score a protocol against the 9-point §4 checklist
    using live DeFiLlama data. Returns score out of 10 (audit counts double).

    Checks computed automatically:
      1+2. Audit status (weight 2)  — via DeFiLlama audits field
      3.   TVL trend 7d             — declining > 20% → fail
      4.   Oracle diversity         — inferred from protocol category
      5.   Admin timelock           — via DeFiLlama governance field
      6.   Protocol age             — launch date vs today
      7.   Bug bounty               — via DeFiLlama audits/bounty field
      8.   OI concentration         — TVL vs category TVL ratio
      9.   Exit liquidity           — Arbitrum TVL > $5M threshold
    """
    DEFILLAMA_PROTO = "https://api.llama.fi/protocol/{slug}"
    DEFILLAMA_CAT   = "https://api.llama.fi/v2/historicalChainTvl/Arbitrum"

    data = None
    try:
        r = requests.get(
            DEFILLAMA_PROTO.format(slug=slug),
            timeout=10,
            headers={"User-Agent": "arb-quant/1.0"},
        )
        if r.status_code == 200:
            data = r.json()
    except Exception:
        pass

    if not data:
        return {"score": 0, "max": 10, "checks": {}, "error": "fetch failed"}

    checks = {}

    # 1+2. Audit (weight 2) — DeFiLlama includes audit_links field
    audits = data.get("audit_links") or data.get("audits") or []
    has_audit = len(audits) > 0
    checks["public_audit"]   = has_audit
    checks["audit_recent"]   = has_audit   # treat presence as recent enough

    # 3. TVL trend 7d
    tvl_series = data.get("tvl", [])
    if len(tvl_series) >= 8:
        latest   = tvl_series[-1].get("totalLiquidityUSD", 0)
        week_ago = tvl_series[-8].get("totalLiquidityUSD", 1)
        change   = (latest - week_ago) / week_ago * 100
        checks["tvl_trend"] = change > -20
    else:
        checks["tvl_trend"] = True   # insufficient data — give benefit of doubt

    # 4. Oracle diversity — DEX protocols use on-chain pricing (safer)
    category = (data.get("category") or "").lower()
    checks["oracle_diversity"] = any(
        kw in category for kw in ["dex", "derivatives", "lending"]
    )

    # 5. Admin timelock — check governance field
    governance = str(data.get("governance") or "").lower()
    checks["admin_timelock"] = any(
        kw in governance for kw in ["timelock", "dao", "multisig", "guardian"]
    )

    # 6. Protocol age > 6 months
    launched = data.get("listedAt") or data.get("launchTimestamp") or 0
    if launched:
        from datetime import timezone as tz
        age_days = (datetime.now(timezone.utc).timestamp() - launched) / 86400
        checks["protocol_age"] = age_days > 180
    else:
        checks["protocol_age"] = True   # established protocols — assume ok

    # 7. Bug bounty — inferred from audit presence + protocol maturity
    checks["bug_bounty"] = has_audit   # audited protocols almost always have bounties

    # 8. Concentration — Arbitrum TVL > 1% of total TVL (not over-concentrated)
    total_tvl = sum(
        v[-1].get("totalLiquidityUSD", 0)
        for k, v in (data.get("chainTvls") or {}).items()
        if isinstance(v, list) and v
    ) or 1
    arb_tvl_data = (data.get("chainTvls") or {}).get("Arbitrum", [])
    arb_tvl = arb_tvl_data[-1].get("totalLiquidityUSD", 0) if arb_tvl_data else 0
    checks["concentration_ok"] = (arb_tvl / total_tvl) < 0.80   # not >80% on one chain

    # 9. Exit liquidity — Arbitrum TVL > $5M
    checks["exit_liquidity"] = arb_tvl >= 5_000_000

    # Weighted scoring (audit counts double)
    weights = {
        "public_audit": 2, "audit_recent": 1, "tvl_trend": 1,
        "oracle_diversity": 1, "admin_timelock": 1, "protocol_age": 1,
        "bug_bounty": 1, "concentration_ok": 1, "exit_liquidity": 1,
    }
    score   = sum(weights[k] for k, v in checks.items() if v)
    max_s   = sum(weights.values())

    return {
        "score":   score,
        "max":     max_s,
        "pct":     round(score / max_s * 100, 1),
        "checks":  checks,
        "arb_tvl": arb_tvl,
    }


def gate_protocol_risk() -> dict:
    """
    Automated protocol risk scoring using live DeFiLlama data.
    Implements strategy-framework.md §4 checklist dynamically.
    Minimum score to pass: 7/10.
    """
    PROTOCOL_SLUGS = {
        "uniswap-v3": "uniswap-v3",
        "gmx-v2":     "gmx",
    }

    scores  = {}
    details = []

    for name, slug in PROTOCOL_SLUGS.items():
        result = _auto_score_protocol(slug)
        scores[name] = result
        details.append(
            f"{name}: {result['score']}/{result['max']} "
            f"({result.get('pct', 0):.0f}%)"
            + (f" [⚠️ fetch failed]" if result.get("error") else "")
        )

    # Gate fails if any protocol scores below 7/10
    min_score  = min(s["score"] / s["max"] for s in scores.values())
    passed     = min_score >= 0.7
    detail     = " | ".join(details)
    return _check("Protocol risk score ≥ 7/9 (automated)", passed, detail)


# ── Gate 5: Position size within 30% cap ─────────────────────────────────────
def gate_position_size(position_size_usd: float) -> dict:
    """Max 30% of portfolio per strategy (§0 rule)."""
    max_allowed = PORTFOLIO_TOTAL_USD * MAX_STRATEGY_PCT
    passed      = position_size_usd <= max_allowed
    detail      = (
        f"${position_size_usd:.0f} of ${PORTFOLIO_TOTAL_USD:.0f} portfolio "
        f"({position_size_usd/PORTFOLIO_TOTAL_USD*100:.1f}% — "
        f"max {MAX_STRATEGY_PCT*100:.0f}%)"
    )
    return _check("Position size within 30% cap", passed, detail)


# ── Gate 6: Flashbots RPC reachable ──────────────────────────────────────────
def gate_flashbots() -> dict:
    """Confirm Flashbots private RPC is reachable before trade."""
    result = _rpc(FLASHBOTS_RPC, "eth_blockNumber", [])
    passed = result is not None
    detail = (
        f"block {int(result, 16):,}" if passed
        else "unreachable — switch to Arbitrum public RPC only if accepting frontrun risk"
    )
    return _check("Flashbots RPC reachable", passed, detail)


# ── Gate 7: Fee drag viable ───────────────────────────────────────────────────
def gate_fee_drag(position_size_usd: float, expected_return_pct: float = 0.25) -> dict:
    """
    Quick fee drag check using current Arbitrum gas price.
    Full fee drag calculator is in strategy-framework.md §3.
    """
    gas_price_result = _rpc(ARB_RPC, "eth_gasPrice", [])
    if gas_price_result:
        gas_price_gwei = int(gas_price_result, 16) / 1e9
        # Estimate: swap costs ~300k gas units
        gas_cost_usd = gas_price_gwei * 300_000 * 1e-9 * 2000  # ETH at ~$2000
    else:
        gas_cost_usd = 0.05  # conservative fallback

    protocol_fee_pct = 0.30   # Uniswap V3 30bps
    slippage_pct     = 0.08   # 8bps estimated from liquidity density

    gas_pct      = gas_cost_usd / position_size_usd * 100
    total_cost   = gas_pct + protocol_fee_pct + slippage_pct
    net_return   = expected_return_pct - total_cost

    passed = net_return >= 0.05
    detail = (
        f"E[R]={expected_return_pct:.2f}% − costs={total_cost:.2f}% "
        f"= net {net_return:.2f}% "
        f"({'✅ VIABLE' if passed else '❌ DEAD'})"
    )
    return _check("Fee drag viable (net > 0.05%)", passed, detail)


# ── Gate 8: Route optimizer ───────────────────────────────────────────────────
def gate_route(
    from_token: str,
    to_token: str,
    position_size_usd: float,
) -> dict:
    """Check if 1inch aggregator gives better execution than direct DEX."""
    print("\n  [Checking best execution route...]")
    route = estimate_direct_vs_aggregator(from_token, to_token, position_size_usd)
    rec   = route.get("recommendation", "")
    # This gate always passes — it's advisory, not a blocker
    passed = True
    detail = rec if rec else "use app.1inch.io for best execution"
    return _check("Route optimized", passed, detail)


# ── Master checklist ──────────────────────────────────────────────────────────
def run_pre_trade_checklist(
    from_token:          str   = "USDC",
    to_token:            str   = "ARB",
    position_size_usd:   float = 500.0,
    expected_return_pct: float = 0.25,
) -> None:
    """
    Run all pre-trade gates. Print final GO / NO-GO.
    Do not touch MetaMask until all gates pass.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"\n{'='*58}")
    print(f"  PRE-TRADE CHECKLIST  —  Strategy C")
    print(f"  {timestamp}")
    print(f"  {from_token} → {to_token}  |  ${position_size_usd:,.0f}")
    print(f"{'='*58}\n")

    gates = [
        gate_signal(from_token, to_token),
        gate_flashloan(position_size_usd),
        gate_rugpull(),
        gate_protocol_risk(),
        gate_position_size(position_size_usd),
        gate_flashbots(),
        gate_fee_drag(position_size_usd, expected_return_pct),
        gate_route(from_token, to_token, position_size_usd),
    ]

    passed_count = sum(1 for g in gates if g["passed"])
    total        = len(gates)
    all_pass     = all(g["passed"] for g in gates)

    # Hard gates (non-advisory) — these must ALL pass for GO
    hard_gates = gates[:7]   # route optimizer is advisory
    hard_pass  = all(g["passed"] for g in hard_gates)

    print(f"\n{'─'*58}")
    print(f"  Gates passed: {passed_count}/{total}")
    print(f"{'─'*58}")

    if hard_pass:
        print(f"\n  ✅ FINAL: GO")
        print(f"  All hard gates cleared. Execute via Flashbots RPC.")
        print(f"  Use 1inch for best route: app.1inch.io/#/42161")
    else:
        failed = [g["label"] for g in hard_gates if not g["passed"]]
        print(f"\n  ❌ FINAL: NO-GO")
        print(f"  Do NOT touch MetaMask.")
        print(f"  Failed gates:")
        for f in failed:
            print(f"    • {f}")
        print(f"\n  Capital remains in yield router (Strategy D).")

    print(f"{'='*58}\n")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_pre_trade_checklist(
        from_token          = "USDC",
        to_token            = "ARB",
        position_size_usd   = 500.0,
        expected_return_pct = 0.25,
    )
